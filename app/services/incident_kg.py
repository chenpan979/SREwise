"""Incident Knowledge Graph 服务 (Step 4) — Neo4j 实现。

设计目标
========
为 SREwise 提供"从过去故障中学习"的能力:
- Reporter 在每次诊断结束后,把 (Incident, RootCause, Action, Service, Symptom) 落入图谱
- Historian 反过来查图谱,召回相似 Incident + 历史成功 Action 模板

Schema (label / relationship)
=============================
节点:
  (:Incident   {id, alert_name, service, namespace, severity, started_at,
                resolved_at, summary, status, confidence})
  (:Service    {name, namespace})
  (:RootCause  {category, description})        category 来自受控字典
  (:Action     {tool_name, args_signature, sample_args})
  (:Symptom    {pattern, hash})

关系:
  (Incident)-[:AFFECTED]→(Service)
  (Incident)-[:CAUSED_BY]→(RootCause)
  (Incident)-[:PRESENTED]→(Symptom)
  (Incident)-[:RESOLVED_BY {success, args_at_runtime}]→(Action)
  (Incident)-[:SIMILAR_TO]→(Incident)        # 同 service + 同 root_cause 自动连接

幂等约束 (constraints)
======================
Incident.id, Service(name+namespace) 复合, RootCause.category, Action.args_signature,
Symptom.hash 都建唯一约束,这样 MERGE 写入幂等。
"""

import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any, Dict, List, Optional

from loguru import logger
from neo4j import AsyncDriver, AsyncGraphDatabase

from app.config import config
from app.services.observability import traced


# ============================================================
# 受控字典 - 根因类别
# ============================================================

ROOT_CAUSE_CATEGORIES = {
    "memory_oom": "内存超限,容器被 OOMKiller 终止",
    "cpu_saturation": "CPU 长期跑满,导致响应变慢或超时",
    "config_change": "最近配置或镜像变更引入的回归",
    "dependency_outage": "依赖服务(数据库/第三方 API)不可用",
    "capacity": "容量不足(连接池/副本数/带宽)",
    "network_partition": "网络分区或 DNS 故障",
    "disk_full": "磁盘满,无法写入",
    "unknown": "根因不明",
}


def normalize_root_cause(category: str) -> str:
    """把任意 root_cause 字符串映射到受控字典 key。"""
    if not category:
        return "unknown"
    c = category.lower().strip().replace(" ", "_").replace("-", "_")
    if c in ROOT_CAUSE_CATEGORIES:
        return c
    if any(k in c for k in ("oom", "memory", "heap")):
        return "memory_oom"
    if any(k in c for k in ("cpu", "throttle", "saturat")):
        return "cpu_saturation"
    if any(k in c for k in ("deploy", "release", "rollout", "version", "config")):
        return "config_change"
    if any(k in c for k in ("dep", "database", "upstream", "outage")):
        return "dependency_outage"
    if any(k in c for k in ("capacity", "pool", "limit", "quota")):
        return "capacity"
    if any(k in c for k in ("network", "dns", "partition")):
        return "network_partition"
    if any(k in c for k in ("disk", "storage", "ebs")):
        return "disk_full"
    return "unknown"


# ============================================================
# ID 生成
# ============================================================

def _hash(s: str, n: int = 12) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:n]


def _incident_id(alert_name: str, service: str, namespace: str,
                 root_cause_category: str) -> str:
    """按"故障模式"去重: alert_name + service + namespace + 根因类别 一致 = 同一节点。

    设计取舍
    ========
    - 不再含 started_at,避免每次手动诊断 / 评测重跑都生成新节点(KG 污染)
    - 把根因类别也纳入,可区分 "同一服务的不同种故障"
      (如 data-sync-service 的 OOM vs 网络分区)
    - 反复发生 / 重跑 → recurrence_count 自增, last_seen_at 更新
    """
    key = f"{alert_name}|{service}|{namespace}|{root_cause_category or 'unknown'}"
    return f"inc_{_hash(key, 14)}"


def _args_signature(tool_name: str, args: Dict[str, Any]) -> str:
    """同工具 + 同 (key 集合 + 稳定 value) = 同 Action。"""
    args = args or {}
    keys = sorted(args.keys())
    parts = [tool_name] + keys
    for stable_key in ("namespace", "name", "deployment", "service"):
        if stable_key in args:
            parts.append(f"{stable_key}={args[stable_key]}")
    return f"{tool_name}::{_hash('|'.join(parts), 14)}"


def _symptom_hash(pattern: str) -> str:
    return _hash(pattern.lower().strip(), 14)


# ============================================================
# Schema 初始化 Cypher
# ============================================================

_SCHEMA_CYPHER = [
    "CREATE CONSTRAINT incident_id_unique IF NOT EXISTS "
    "FOR (n:Incident) REQUIRE n.id IS UNIQUE",
    "CREATE CONSTRAINT service_unique IF NOT EXISTS "
    "FOR (n:Service) REQUIRE (n.name, n.namespace) IS UNIQUE",
    "CREATE CONSTRAINT root_cause_unique IF NOT EXISTS "
    "FOR (n:RootCause) REQUIRE n.category IS UNIQUE",
    "CREATE CONSTRAINT action_unique IF NOT EXISTS "
    "FOR (n:Action) REQUIRE n.args_signature IS UNIQUE",
    "CREATE CONSTRAINT symptom_unique IF NOT EXISTS "
    "FOR (n:Symptom) REQUIRE n.hash IS UNIQUE",
    "CREATE INDEX incident_service IF NOT EXISTS FOR (n:Incident) ON (n.service)",
    "CREATE INDEX incident_started_at IF NOT EXISTS FOR (n:Incident) ON (n.started_at)",
]


# ============================================================
# 服务
# ============================================================

class IncidentKG:
    """Incident Knowledge Graph (Neo4j-backed)。"""

    def __init__(self):
        self._driver: Optional[AsyncDriver] = None
        self._ready: bool = False
        self._seeded: bool = False

    # ---------------- 生命周期 ----------------

    async def connect(self) -> bool:
        """初始化 driver + schema。失败时打 warning,不抛异常(允许整个应用降级)。"""
        if self._driver is not None:
            return self._ready
        if not config.neo4j_enabled:
            logger.warning("[KG] neo4j_enabled=false,跳过初始化 (Historian/Reporter 将降级)")
            return False
        try:
            self._driver = AsyncGraphDatabase.driver(
                config.neo4j_uri,
                auth=(config.neo4j_user, config.neo4j_password),
            )
            await self._driver.verify_connectivity()
            await self._ensure_schema()
            self._ready = True
            logger.info(f"[KG] 已连接 Neo4j: {config.neo4j_uri}")
            return True
        except Exception as e:
            logger.warning(f"[KG] Neo4j 连接失败 {config.neo4j_uri}: {e}; "
                           f"Historian/Reporter 将自动降级")
            self._driver = None
            self._ready = False
            return False

    async def close(self):
        if self._driver is not None:
            await self._driver.close()
            self._driver = None
            self._ready = False

    async def _ensure_schema(self):
        async with self._driver.session(database=config.neo4j_database) as s:
            for cy in _SCHEMA_CYPHER:
                await s.run(cy)
        logger.info("[KG] schema constraints/indexes 已就绪")

    @property
    def ready(self) -> bool:
        return self._ready

    async def live_probe(self, timeout: float = 2.0) -> bool:
        """真正打一条轻量 query 验活 (用于 /health)。
        失败时会把 _ready 翻回 False,后续业务自动降级。
        """
        if self._driver is None:
            return False
        try:
            async with self._driver.session(database=config.neo4j_database) as s:
                await asyncio.wait_for(s.run("RETURN 1"), timeout=timeout)
            if not self._ready:
                self._ready = True
                logger.info("[KG] live_probe 恢复连接")
            return True
        except Exception as e:
            if self._ready:
                logger.warning(f"[KG] live_probe 失败,标记为 disconnected: {e}")
            self._ready = False
            return False

    @property
    def seeded(self) -> bool:
        return self._seeded

    def mark_seeded(self):
        self._seeded = True

    # ---------------- 写入 ----------------

    @traced(name="incident_kg.upsert_incident")
    async def upsert_incident(
        self,
        *,
        alert_name: str,
        service: str,
        namespace: str = "production",
        severity: str = "warning",
        started_at: Optional[str] = None,
        summary: str = "",
        status: str = "ongoing",
        root_cause_category: str = "unknown",
        root_cause_description: str = "",
        symptoms: Optional[List[str]] = None,
        actions: Optional[List[Dict[str, Any]]] = None,
        confidence: float = 0.0,
    ) -> Optional[str]:
        """写入或更新一次 Incident,返回 incident_id。

        actions 元素结构: {"tool_name": str, "args": dict, "success": bool}
        """
        if not self._ready:
            logger.debug("[KG] 未就绪,跳过写入")
            return None

        started_at = started_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        symptoms = [s for s in (symptoms or []) if s]
        actions = actions or []
        rc_cat = normalize_root_cause(root_cause_category)
        rc_desc = root_cause_description or ROOT_CAUSE_CATEGORIES.get(rc_cat, "")
        inc_id = _incident_id(alert_name, service, namespace, rc_cat)
        resolved_at = (datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                       if status == "resolved" else None)

        # 准备 actions 参数:序列化每个 action,Cypher 端再解析
        action_payload = []
        for act in actions:
            tool_name = act.get("tool_name")
            if not tool_name:
                continue
            args = act.get("args") or {}
            action_payload.append({
                "tool_name": tool_name,
                "args_signature": _args_signature(tool_name, args),
                "sample_args_json": json.dumps(args, ensure_ascii=False, default=str),
                "success": bool(act.get("success", True)),
                "args_at_runtime_json": json.dumps(args, ensure_ascii=False, default=str),
            })

        symptom_payload = [{"pattern": s, "hash": _symptom_hash(s)} for s in symptoms]

        # 一个事务搞定 incident + service + root_cause + symptoms + actions + similar_to
        async with self._driver.session(database=config.neo4j_database) as session:
            await session.execute_write(
                self._tx_upsert_incident,
                inc_id=inc_id, alert_name=alert_name, service=service,
                namespace=namespace, severity=severity, started_at=started_at,
                resolved_at=resolved_at, summary=summary, status=status,
                confidence=confidence, rc_cat=rc_cat, rc_desc=rc_desc,
                symptoms=symptom_payload, actions=action_payload,
            )
        logger.info(f"[KG] upsert Incident {inc_id} "
                    f"(service={service}, rc={rc_cat}, "
                    f"actions={len(action_payload)}, symptoms={len(symptom_payload)})")
        return inc_id

    @staticmethod
    async def _tx_upsert_incident(tx, *, inc_id, alert_name, service, namespace,
                                  severity, started_at, resolved_at, summary,
                                  status, confidence, rc_cat, rc_desc,
                                  symptoms, actions):
        # 1. Incident + Service
        # 关键: ON CREATE 写入首次发生时间和初始计数, ON MATCH 仅更新 last_seen_at +
        # recurrence_count + 最新摘要,确保反复发生的同一故障模式只占用一个节点。
        await tx.run(
            """
            MERGE (i:Incident {id: $inc_id})
            ON CREATE SET
                i.first_seen_at  = $started_at,
                i.recurrence_count = 1,
                i.alert_name = $alert_name,
                i.service    = $service,
                i.namespace  = $namespace,
                i.severity   = $severity,
                i.started_at = $started_at,
                i.last_seen_at = $started_at,
                i.resolved_at = $resolved_at,
                i.summary    = $summary,
                i.status     = $status,
                i.confidence = $confidence
            ON MATCH SET
                i.recurrence_count = coalesce(i.recurrence_count, 1) + 1,
                i.last_seen_at = $started_at,
                i.started_at   = $started_at,
                i.severity = $severity,
                i.resolved_at = $resolved_at,
                i.summary  = $summary,
                i.status   = $status,
                i.confidence = $confidence
            MERGE (s:Service {name: $service, namespace: $namespace})
            MERGE (i)-[:AFFECTED]->(s)
            """,
            inc_id=inc_id, alert_name=alert_name, service=service,
            namespace=namespace, severity=severity, started_at=started_at,
            resolved_at=resolved_at, summary=summary, status=status,
            confidence=confidence,
        )
        # 2. RootCause
        await tx.run(
            """
            MATCH (i:Incident {id: $inc_id})
            MERGE (rc:RootCause {category: $rc_cat})
            ON CREATE SET rc.description = $rc_desc
            ON MATCH  SET rc.description = coalesce(rc.description, $rc_desc)
            MERGE (i)-[:CAUSED_BY]->(rc)
            """,
            inc_id=inc_id, rc_cat=rc_cat, rc_desc=rc_desc,
        )
        # 3. Symptoms
        if symptoms:
            await tx.run(
                """
                MATCH (i:Incident {id: $inc_id})
                UNWIND $symptoms AS sym
                MERGE (s:Symptom {hash: sym.hash})
                ON CREATE SET s.pattern = sym.pattern
                MERGE (i)-[:PRESENTED]->(s)
                """,
                inc_id=inc_id, symptoms=symptoms,
            )
        # 4. Actions
        if actions:
            await tx.run(
                """
                MATCH (i:Incident {id: $inc_id})
                UNWIND $actions AS act
                MERGE (a:Action {args_signature: act.args_signature})
                ON CREATE SET a.tool_name = act.tool_name,
                              a.sample_args = act.sample_args_json
                MERGE (i)-[r:RESOLVED_BY]->(a)
                SET r.success = act.success,
                    r.args_at_runtime = act.args_at_runtime_json
                """,
                inc_id=inc_id, actions=actions,
            )
        # 5. SIMILAR_TO: 同 service + 同 root_cause 的最近 5 个 Incident
        await tx.run(
            """
            MATCH (i:Incident {id: $inc_id})-[:CAUSED_BY]->(rc:RootCause)
            MATCH (other:Incident)-[:CAUSED_BY]->(rc)
            WHERE other.id <> $inc_id AND other.service = $service
            WITH i, other ORDER BY other.started_at DESC LIMIT 5
            MERGE (i)-[:SIMILAR_TO]->(other)
            MERGE (other)-[:SIMILAR_TO]->(i)
            """,
            inc_id=inc_id, service=service,
        )

    # ---------------- 查询 ----------------

    @traced(name="incident_kg.find_similar_incidents")
    async def find_similar_incidents(
        self,
        *,
        service: Optional[str] = None,
        namespace: str = "production",
        root_cause_category: Optional[str] = None,
        symptom_keywords: Optional[List[str]] = None,
        limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """召回相似历史 Incident。

        评分规则 (在 Cypher 里实现):
          - 同 service: +3
          - 同 root_cause: +2
          - 共享 symptom (按子串匹配): +0.5/个 上限 1
        """
        if not self._ready:
            return []
        rc_cat = normalize_root_cause(root_cause_category) if root_cause_category else None
        symptom_keywords = [k.lower() for k in (symptom_keywords or []) if k]

        cypher = """
        MATCH (i:Incident)-[:CAUSED_BY]->(rc:RootCause)
        OPTIONAL MATCH (i)-[:PRESENTED]->(sym:Symptom)
        WITH i, rc, collect(DISTINCT sym.pattern) AS syms
        WITH i, rc, syms,
             (CASE WHEN $service IS NOT NULL AND i.service = $service THEN 3.0 ELSE 0 END) +
             (CASE WHEN $rc_cat IS NOT NULL AND rc.category = $rc_cat THEN 2.0 ELSE 0 END) +
             (CASE WHEN size($keywords) > 0
                   THEN toFloat(size([s IN syms WHERE any(k IN $keywords
                                                          WHERE toLower(s) CONTAINS k)]))
                        * 0.5
                   ELSE 0 END) AS score
        WHERE score > 0
        OPTIONAL MATCH (i)-[r:RESOLVED_BY]->(act:Action)
        WITH i, rc, syms, score,
             collect(DISTINCT {
                 tool_name: act.tool_name,
                 args_signature: act.args_signature,
                 sample_args: act.sample_args,
                 success: r.success
             }) AS actions
        RETURN i {
            .id, .alert_name, .service, .namespace, .severity,
            .started_at, .resolved_at, .summary, .status, .confidence
        } AS incident,
        rc.category AS root_cause_category, rc.description AS root_cause_description,
        syms AS symptoms,
        actions AS resolved_actions,
        score
        ORDER BY score DESC, incident.started_at DESC
        LIMIT $limit
        """
        params = {"service": service, "rc_cat": rc_cat,
                  "keywords": symptom_keywords, "limit": limit}
        async with self._driver.session(database=config.neo4j_database) as s:
            result = await s.run(cypher, **params)
            records = await result.data()

        # 清理 actions 中的 None (没有 RESOLVED_BY 时会出现一条全 None 记录)
        for rec in records:
            rec["resolved_actions"] = [a for a in rec.get("resolved_actions", [])
                                       if a and a.get("tool_name")]
        return records

    @traced(name="incident_kg.get_action_templates")
    async def get_action_templates(
        self, *, root_cause_category: str,
        service: Optional[str] = None, limit: int = 5,
    ) -> List[Dict[str, Any]]:
        """给定根因 (可选 service),返回历史成功修复的 Action 模板,按命中数排序。"""
        if not self._ready:
            return []
        rc_cat = normalize_root_cause(root_cause_category)
        cypher = """
        MATCH (i:Incident)-[:CAUSED_BY]->(rc:RootCause {category: $rc_cat})
        WHERE $service IS NULL OR i.service = $service
        MATCH (i)-[r:RESOLVED_BY]->(a:Action)
        WHERE r.success = true
        RETURN a.tool_name AS tool_name,
               a.args_signature AS args_signature,
               a.sample_args AS sample_args_json,
               count(DISTINCT i) AS hit_count,
               max(i.started_at) AS last_used_at
        ORDER BY hit_count DESC, last_used_at DESC
        LIMIT $limit
        """
        async with self._driver.session(database=config.neo4j_database) as s:
            result = await s.run(cypher, rc_cat=rc_cat,
                                 service=service, limit=limit)
            records = await result.data()
        # 反序列化 sample_args
        for r in records:
            try:
                r["sample_args"] = json.loads(r.pop("sample_args_json") or "{}")
            except Exception:
                r["sample_args"] = {}
        return records

    # ---------------- 统计 / 导出 ----------------

    async def stats(self) -> Dict[str, Any]:
        if not self._ready:
            return {"ready": False, "node_count": 0, "edge_count": 0}
        cypher_nodes = ("MATCH (n) "
                        "RETURN labels(n)[0] AS label, count(*) AS cnt")
        cypher_edges = ("MATCH ()-[r]->() "
                        "RETURN type(r) AS rel, count(*) AS cnt")
        async with self._driver.session(database=config.neo4j_database) as s:
            n_res = await (await s.run(cypher_nodes)).data()
            e_res = await (await s.run(cypher_edges)).data()
        nodes_by_kind = {r["label"]: r["cnt"] for r in n_res}
        edges_by_rel = {r["rel"]: r["cnt"] for r in e_res}
        return {
            "ready": True,
            "seeded": self._seeded,
            "node_count": sum(nodes_by_kind.values()),
            "edge_count": sum(edges_by_rel.values()),
            "nodes_by_kind": nodes_by_kind,
            "edges_by_rel": edges_by_rel,
        }

    async def export_subgraph(
        self, around_incident: Optional[str] = None,
        depth: int = 2, limit_nodes: int = 80,
    ) -> Dict[str, Any]:
        """导出 {nodes, edges} 子图给前端可视化。"""
        if not self._ready:
            return {"nodes": [], "edges": []}
        if around_incident:
            cypher = """
            MATCH (start:Incident {id: $center})
            CALL apoc.path.subgraphAll(start, {maxLevel: $depth}) YIELD nodes, relationships
            RETURN nodes[0..$limit] AS nodes, relationships AS edges
            """
            params = {"center": around_incident, "depth": depth, "limit": limit_nodes}
        else:
            cypher = """
            MATCH (n) WITH n LIMIT $limit
            OPTIONAL MATCH (n)-[r]->(m)
            RETURN collect(DISTINCT n) AS nodes, collect(DISTINCT r) AS edges
            """
            params = {"limit": limit_nodes}
        async with self._driver.session(database=config.neo4j_database) as s:
            result = await s.run(cypher, **params)
            row = await result.single()
        if not row:
            return {"nodes": [], "edges": []}
        nodes_out = [self._node_to_dict(n) for n in row["nodes"] if n is not None]
        edges_out = [self._edge_to_dict(e) for e in row["edges"] if e is not None]
        return {"nodes": nodes_out, "edges": edges_out}

    @staticmethod
    def _node_to_dict(n):
        return {"id": n.element_id,
                "labels": list(n.labels),
                "props": dict(n)}

    @staticmethod
    def _edge_to_dict(r):
        return {"id": r.element_id, "type": r.type,
                "start": r.start_node.element_id,
                "end": r.end_node.element_id,
                "props": dict(r)}


# 全局单例
incident_kg = IncidentKG()
