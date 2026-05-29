"""GraphRAG 服务 (Step 5) — 知识图谱 + 向量库 混合召回。

核心思想
========
现有 SRE 召回原本有两条独立路径:
  - Historian → IncidentKG (Cypher)        结构化匹配 service / root_cause / symptoms
  - Historian → retrieve_knowledge (Milvus) 语义匹配 runbook 段落

它们各有优势:
  - KG 知道"同 service + 同根因"这种结构化属性,但需要文本被提前抽成实体才能匹配
  - 向量擅长长尾语义相似 (e.g. 用户说"数据库慢" → 召回包含"数据库延迟"的 runbook),
    但完全不理解 service/root_cause 是结构化属性

GraphRAG 把两边结果**融合**:
  1. **Vector path**: 用 query 在 Milvus 里 top-k 召回 (可加 metadata filter)
  2. **KG path**: 在 Neo4j 里查相似 Incident + Action 模板
  3. **Cross-seed**: 把 KG 召回的 Incident.summary 当作**额外向量 query 种子**,
     补召一批长尾 runbook 段落 (这一步就是 GraphRAG 的灵魂)
  4. 加权融合 + 去重 → 最终 result

Reporter 在故障复盘后会把 Incident.summary 也 embed 到 Milvus
(metadata 带 incident_id/service/root_cause_category),实现"诊断→入图→可被检索"闭环。

接口
====
- `index_incident_text(...)`        将诊断结论写到 Milvus
- `seed_builtin_runbooks()`         启动时灌入内置 runbook 文档 (幂等)
- `query(query, service, rc, ...)`  混合召回,返回 GraphRAGResult
"""

import asyncio
from typing import Any, Dict, List, Optional

from langchain_core.documents import Document
from loguru import logger

from app.services.incident_kg import incident_kg, normalize_root_cause
from app.services.observability import traced
from app.services.vector_store_manager import vector_store_manager


# ============================================================
# Metadata key (统一在这里管理,前端 / 检索 / 入库都用它)
# ============================================================

META_KIND = "_kind"               # incident_summary / runbook / generic
META_INCIDENT_ID = "incident_id"
META_SERVICE = "service"
META_ROOT_CAUSE = "root_cause"
META_SEVERITY = "severity"
META_STARTED_AT = "started_at"


# ============================================================
# 内置 runbook 种子 (跟剧本一致, 写在代码里,避免依赖磁盘 IO)
# ============================================================

_RUNBOOK_SEEDS: List[Dict[str, Any]] = [
    {
        "id": "runbook::memory_oom::data-sync-service",
        "title": "data-sync-service OOM 故障处置 runbook",
        "service": "data-sync-service",
        "root_cause": "memory_oom",
        "content": """# data-sync-service OOM 故障处置 runbook

## 适用场景
- Pod 出现 `OOMKilled` (exit code 137)
- container_memory_usage 持续 > 90%
- 最近一次发布导致内存基线上移

## 紧急处置 (5 分钟内)
1. **判断是否最近发布**:
   `kubectl rollout history deployment/data-sync-service -n production`
   若有 24h 内变更 → 优先回滚
2. **回滚到上一个稳定版本**:
   `rollback_deployment(name="data-sync-service", namespace="production")`
3. **临时扩容**减小单 Pod 压力:
   `scale_deployment(name="data-sync-service", replicas=5, namespace="production")`

## 根因分析
data-sync-service 的内存使用主要由本地字典缓存驱动。常见诱因:
- v38: 字典懒加载改成全量加载
- v41: 新增 LRU 缓存层,默认无上限
- 上游业务量突增,触发更多缓存条目

## 长期修复
- 在 Deployment 添加 `resources.limits.memory` 硬上限
- 引入外部缓存 (Redis),减小堆内压力
- 增加 `container_memory_working_set_bytes` 告警阈值 (80%)
""",
    },
    {
        "id": "runbook::dependency_outage::api-gateway-service",
        "title": "API Gateway 下游依赖故障 runbook",
        "service": "api-gateway-service",
        "root_cause": "dependency_outage",
        "content": """# API Gateway 下游依赖故障处置 runbook

## 适用场景
- API Gateway 5xx error rate 飙升
- P99 延迟同步上涨
- 下游 service (e.g. user-service) 健康检查失败

## 紧急处置
1. **确认是否单点故障**: 查 alertmanager 看其他服务是否也告警
2. **熔断下游**: 在 Gateway 配置里临时下线问题路由
3. **重启 Gateway** 以清理被卡住的连接:
   `restart_deployment(name="api-gateway-service", namespace="production")`
4. **若下游已恢复但 Gateway 仍 5xx**: 通常是连接池脏数据 → 滚动重启

## 根因
- 下游 database 主从切换 (常见,可预测)
- 下游 service 自身 OOM / CrashLoop
- 网络分区 (DNS 故障)

## 防御
- Gateway 配置 circuit breaker
- 健康检查超时下调,故障下游能更快被踢出 LB
""",
    },
    {
        "id": "runbook::capacity::generic",
        "title": "容量类故障处置 runbook (通用)",
        "service": "",
        "root_cause": "capacity",
        "content": """# 容量类故障处置 runbook

## 适用场景
- 连接池耗尽 (connection pool exhausted)
- 副本数不够导致 Pod 排队 / 资源争抢
- 配额 (quota) 触顶

## 紧急处置
1. **水平扩容**: `scale_deployment(replicas=current*1.5)`,先把火灭掉
2. **检查 HPA**: 若有但没触发,看 metrics-server 是否健康
3. **不要急着调大 limits**: 大概率是流量上来了,不是单 Pod 容量不够

## 防御
- 设置基于 P99 / QPS 的预扩容
- 定期复查连接池大小 (通常历史经验值偏小)
""",
    },
    {
        "id": "runbook::config_change::generic",
        "title": "配置/发布回归处置 runbook (通用)",
        "service": "",
        "root_cause": "config_change",
        "content": """# 配置/发布回归处置 runbook

## 适用场景
- 故障发生时间紧邻最近一次发布 (< 30 分钟)
- 错误率/延迟在发布后阶梯式上涨
- 流量回到前一版本部署后恢复

## 紧急处置
1. **优先级 1: 回滚**, 不要先尝试 hotfix
   `rollback_deployment(name=<service>, namespace=<ns>)`
2. **保留现场**: 截图 metrics, 拉一份新版本的 logs / pprof
3. **冷静诊断**: 故障期发表事故声明,但**先恢复再调查**

## 防御
- Canary 发布 + 自动 rollback (基于 error rate)
- 强制变更窗口 (高峰期禁止变更)
""",
    },
]


# ============================================================
# GraphRAGResult
# ============================================================

class GraphRAGResult:
    """混合召回返回结构。"""

    def __init__(self):
        self.kg_incidents: List[Dict[str, Any]] = []
        self.kg_action_templates: List[Dict[str, Any]] = []
        self.vector_chunks: List[Dict[str, Any]] = []
        self.cross_seeded_chunks: List[Dict[str, Any]] = []
        self.query: str = ""
        self.filters: Dict[str, Any] = {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "query": self.query,
            "filters": self.filters,
            "kg_incidents": self.kg_incidents,
            "kg_action_templates": self.kg_action_templates,
            "vector_chunks": self.vector_chunks,
            "cross_seeded_chunks": self.cross_seeded_chunks,
            "stats": {
                "kg_incidents": len(self.kg_incidents),
                "kg_actions": len(self.kg_action_templates),
                "vector": len(self.vector_chunks),
                "cross_seeded": len(self.cross_seeded_chunks),
            },
        }


# ============================================================
# 服务
# ============================================================

class GraphRAGService:
    """混合检索 (KG + Vector) 协调者。"""

    def __init__(self):
        self._seeded = False

    @property
    def seeded(self) -> bool:
        return self._seeded

    # ---------------- 索引侧 ----------------

    @traced(name="graph_rag.index_incident_text")
    async def index_incident_text(
        self, *,
        incident_id: str,
        summary: str,
        service: str,
        root_cause_category: str,
        severity: str = "warning",
        started_at: Optional[str] = None,
        extra_text: str = "",
    ) -> bool:
        """把一次 Incident 的诊断结论写入 Milvus,后续可被语义召回。"""
        if not summary:
            return False
        rc = normalize_root_cause(root_cause_category)
        text = summary.strip()
        if extra_text:
            text = text + "\n\n" + extra_text.strip()
        try:
            # LangChain Milvus.add_documents 不是 async,丢到线程池
            doc = Document(
                page_content=text,
                metadata={
                    META_KIND: "incident_summary",
                    META_INCIDENT_ID: incident_id,
                    META_SERVICE: service or "",
                    META_ROOT_CAUSE: rc,
                    META_SEVERITY: severity,
                    META_STARTED_AT: started_at or "",
                    "_source": f"incident::{incident_id}",
                    "_file_name": f"{incident_id}.summary",
                },
            )
            await asyncio.to_thread(
                vector_store_manager.add_documents, [doc],
            )
            logger.info(f"[GraphRAG] incident summary 已入向量库: {incident_id} "
                        f"(service={service}, rc={rc})")
            return True
        except Exception as e:
            logger.warning(f"[GraphRAG] 写入向量库失败 {incident_id}: {e}")
            return False

    async def seed_builtin_runbooks(self) -> Dict[str, Any]:
        """启动时把内置 runbook 灌入向量库 (幂等: 用 _source 标记跳过已存在)。"""
        if self._seeded:
            return {"seeded": False, "reason": "already seeded in this process"}
        written = 0
        skipped = 0
        for spec in _RUNBOOK_SEEDS:
            source_key = spec["id"]
            try:
                # 先按 _source 删除旧数据 (确保幂等)
                vector_store_manager.delete_by_source(source_key)
                doc = Document(
                    page_content=spec["content"],
                    metadata={
                        META_KIND: "runbook",
                        META_SERVICE: spec.get("service", "") or "",
                        META_ROOT_CAUSE: normalize_root_cause(spec.get("root_cause", "")),
                        "title": spec.get("title", ""),
                        "_source": source_key,
                        "_file_name": f"{source_key}.md",
                    },
                )
                await asyncio.to_thread(
                    vector_store_manager.add_documents, [doc],
                )
                written += 1
            except Exception as e:
                logger.warning(f"[GraphRAG] runbook seed 失败 {source_key}: {e}")
                skipped += 1
        self._seeded = True
        logger.info(f"[GraphRAG] runbook 种子写入完成: {written} 篇 (跳过 {skipped})")
        return {"seeded": True, "written": written, "skipped": skipped}

    # ---------------- 查询侧 ----------------

    @traced(name="graph_rag.query")
    async def query(
        self,
        query_text: str,
        *,
        service: Optional[str] = None,
        root_cause: Optional[str] = None,
        symptom_keywords: Optional[List[str]] = None,
        top_k_kg: int = 5,
        top_k_vector: int = 4,
        top_k_cross: int = 3,
        enable_cross_seed: bool = True,
    ) -> GraphRAGResult:
        """混合召回。

        三路并发:
          - KG: find_similar_incidents + get_action_templates
          - Vector: similarity_search(query_text) with metadata filter
          - Cross-seed (依赖 KG 结果): 用 KG 召回 incidents 的 summary 作额外向量 query
        """
        result = GraphRAGResult()
        result.query = query_text
        rc = normalize_root_cause(root_cause) if root_cause else None
        result.filters = {
            "service": service, "root_cause": rc,
            "symptoms": symptom_keywords or [],
        }

        # 三路并发
        kg_task = asyncio.create_task(
            self._kg_path(service, rc, symptom_keywords, top_k_kg))
        vec_task = asyncio.create_task(
            self._vector_path(query_text, service, rc, top_k_vector))

        kg_incidents, kg_templates = await kg_task
        vec_chunks = await vec_task

        result.kg_incidents = kg_incidents
        result.kg_action_templates = kg_templates
        result.vector_chunks = vec_chunks

        # Cross-seed: 用 KG incident summary 当 query 在向量里二次召回
        if enable_cross_seed and kg_incidents:
            try:
                cross = await self._cross_seed(kg_incidents, vec_chunks, top_k_cross)
                result.cross_seeded_chunks = cross
            except Exception as e:
                logger.warning(f"[GraphRAG] cross-seed 失败: {e}")

        logger.info(
            f"[GraphRAG] query={query_text!r} service={service} rc={rc}"
            f" → kg_inc={len(result.kg_incidents)} kg_act={len(result.kg_action_templates)}"
            f" vec={len(result.vector_chunks)} cross={len(result.cross_seeded_chunks)}"
        )
        return result

    @traced(name="graph_rag._kg_path")
    async def _kg_path(self, service, rc, symptoms, top_k):
        if not incident_kg.ready:
            return [], []
        try:
            incidents = await incident_kg.find_similar_incidents(
                service=service, root_cause_category=rc,
                symptom_keywords=symptoms, limit=top_k,
            )
        except Exception as e:
            logger.warning(f"[GraphRAG] KG incidents 查询失败: {e}")
            incidents = []
        templates: List[Dict[str, Any]] = []
        if rc and rc != "unknown":
            try:
                templates = await incident_kg.get_action_templates(
                    root_cause_category=rc, service=service, limit=top_k,
                )
            except Exception as e:
                logger.warning(f"[GraphRAG] KG action templates 失败: {e}")
        return incidents, templates

    @traced(name="graph_rag._vector_path")
    async def _vector_path(self, query_text, service, rc, top_k):
        """Milvus 召回。优先带 metadata filter,失败则降级为纯语义。"""
        if not query_text:
            return []
        # 构造 metadata 过滤表达式 (langchain_milvus 支持 expr= 参数)
        filters: List[str] = []
        if service:
            filters.append(f'metadata["{META_SERVICE}"] == "{service}"')
        if rc and rc != "unknown":
            filters.append(f'metadata["{META_ROOT_CAUSE}"] == "{rc}"')
        expr = " and ".join(filters) if filters else None

        store = vector_store_manager.get_vector_store()
        chunks: List[Dict[str, Any]] = []

        # 1. 带过滤的精确召回
        if expr:
            try:
                docs = await asyncio.to_thread(
                    lambda: store.similarity_search_with_score(
                        query_text, k=top_k, expr=expr,
                    ),
                )
                chunks.extend(self._docs_to_chunks(docs, "filtered"))
            except Exception as e:
                logger.debug(f"[GraphRAG] 过滤召回失败,降级到无过滤: {e}")

        # 2. 无过滤兜底召回 (拼接到末尾,_dedup 时去重)
        try:
            docs = await asyncio.to_thread(
                lambda: store.similarity_search_with_score(query_text, k=top_k),
            )
            chunks.extend(self._docs_to_chunks(docs, "semantic"))
        except Exception as e:
            logger.warning(f"[GraphRAG] vector 召回失败: {e}")

        return self._dedup_chunks(chunks)[:top_k]

    @traced(name="graph_rag._cross_seed")
    async def _cross_seed(self, kg_incidents, already, top_k):
        """用 KG incident 的 summary 当新 query,在向量库里多召一批。"""
        if not kg_incidents:
            return []
        store = vector_store_manager.get_vector_store()
        seen_ids = {c.get("id") for c in already}
        out: List[Dict[str, Any]] = []
        for inc in kg_incidents[:3]:  # 只取 KG top-3 当种子,避免爆炸
            seed = (inc.get("incident", {}) or {}).get("summary") \
                or inc.get("summary") or ""
            if not seed:
                continue
            try:
                docs = await asyncio.to_thread(
                    lambda s=seed: store.similarity_search_with_score(s, k=top_k),
                )
                for d in self._docs_to_chunks(docs, "cross_seed"):
                    if d["id"] in seen_ids:
                        continue
                    seen_ids.add(d["id"])
                    d["seed_incident_id"] = (inc.get("incident", {}) or {}).get("id")
                    out.append(d)
            except Exception as e:
                logger.debug(f"[GraphRAG] cross-seed 子查询失败: {e}")
        return out[:top_k]

    @staticmethod
    def _docs_to_chunks(docs_with_score, channel: str) -> List[Dict[str, Any]]:
        chunks = []
        for item in docs_with_score:
            # langchain returns (doc, score) tuples
            if isinstance(item, tuple):
                doc, score = item
            else:
                doc, score = item, None
            meta = doc.metadata or {}
            chunks.append({
                "id": meta.get("_source", "") + "::" + (meta.get("_file_name", "")
                                                       or str(id(doc))),
                "content": doc.page_content[:1500],
                "score": float(score) if score is not None else None,
                "channel": channel,
                "metadata": meta,
            })
        return chunks

    @staticmethod
    def _dedup_chunks(chunks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        seen: set[str] = set()
        out = []
        for c in chunks:
            if c["id"] in seen:
                continue
            seen.add(c["id"])
            out.append(c)
        return out


# 全局单例
graph_rag = GraphRAGService()
