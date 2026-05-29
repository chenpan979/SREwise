"""Historian 节点 — 召回历史故障与相关 runbook。

Step 4 升级
===========
1. **主召回走 Incident KG (Neo4j)**: 同 service 、同 root_cause 、共享 symptom 的
   Incident 节点,带心历史上成功修复 Action 模板。
2. **降级走 alertmanager.get_alert_history**: KG 不可用 / 未命中时才走 MCP
   历史,保证最少 demo 可跑。
3. **runbook**: 仍走 retrieve_knowledge (RAG)。Step 5 (GraphRAG) 会该为图+向量
   混合召回。

similar_incidents 输出结构
========================
[
  {"_kind": "kg_incident", "id": ..., "alert_name": ..., "service": ...,
   "started_at": ..., "summary": ...,
   "root_cause_category": ..., "resolved_actions": [...]},
  {"_kind": "kg_action_template", "tool_name": ..., "sample_args": {...},
   "hit_count": int, "last_used_at": ...},
  {"_kind": "alertmanager_alert", ...},   # 仅降级路径
  {"_kind": "pattern_summary", "summary": str},
]
下游 Diagnostician / Remediator 只需靠 _kind 区分来源。
"""

from typing import Any, Dict, List

from loguru import logger

from app.agent.mcp_client import get_mcp_client_with_retry
from app.services.graph_rag import graph_rag
from app.services.incident_kg import incident_kg, normalize_root_cause
from .state import SREState


async def historian(state: SREState) -> Dict[str, Any]:
    """召回相似历史故障 + 相关 runbook。"""
    logger.info("=== Historian: 历史召回 ===")

    input_text = state.get("input", "")
    alert = state.get("alert") or {}
    service = alert.get("service") or _guess_service_from_input(input_text)

    similar_incidents: List[Dict[str, Any]] = []
    relevant_runbooks: List[Dict[str, Any]] = []
    symptoms = _extract_symptoms(alert, input_text)
    rc_hint = _guess_root_cause(alert, input_text, symptoms)
    query_text = _build_runbook_query(input_text, alert)

    # ---------- 1. GraphRAG 主召回 (KG + Vector + Cross-seed 并发) ----------
    kg_hit = False
    try:
        gr = await graph_rag.query(
            query_text=query_text,
            service=service or None,
            root_cause=rc_hint or None,
            symptom_keywords=symptoms or None,
            top_k_kg=5, top_k_vector=4, top_k_cross=3,
        )

        # KG incidents → similar_incidents
        for inc in gr.kg_incidents:
            similar_incidents.append({
                "_kind": "kg_incident",
                "id": inc["incident"].get("id"),
                "alert_name": inc["incident"].get("alert_name"),
                "service": inc["incident"].get("service"),
                "severity": inc["incident"].get("severity"),
                "started_at": inc["incident"].get("started_at"),
                "summary": inc["incident"].get("summary"),
                "status": inc["incident"].get("status"),
                "root_cause_category": inc.get("root_cause_category"),
                "root_cause_description": inc.get("root_cause_description"),
                "symptoms": inc.get("symptoms"),
                "resolved_actions": inc.get("resolved_actions"),
                "score": inc.get("score"),
            })
        for tpl in gr.kg_action_templates:
            similar_incidents.append({
                "_kind": "kg_action_template",
                "tool_name": tpl.get("tool_name"),
                "args_signature": tpl.get("args_signature"),
                "sample_args": tpl.get("sample_args"),
                "hit_count": tpl.get("hit_count"),
                "last_used_at": tpl.get("last_used_at"),
            })
        kg_hit = bool(gr.kg_incidents)

        # 向量召回 → relevant_runbooks (区分主路径和 cross-seed)
        for ch in gr.vector_chunks:
            relevant_runbooks.append({
                "source": "graphrag_vector",
                "channel": ch.get("channel"),
                "score": ch.get("score"),
                "metadata": ch.get("metadata"),
                "content": ch.get("content"),
            })
        for ch in gr.cross_seeded_chunks:
            relevant_runbooks.append({
                "source": "graphrag_cross_seed",
                "seed_incident_id": ch.get("seed_incident_id"),
                "score": ch.get("score"),
                "metadata": ch.get("metadata"),
                "content": ch.get("content"),
            })

        # Pattern summary
        if kg_hit:
            similar_incidents.append({
                "_kind": "pattern_summary",
                "summary": (f"GraphRAG 召回 {len(gr.kg_incidents)} 条相似 Incident "
                            f"+ {len(gr.kg_action_templates)} 个 Action 模板, "
                            f"runbook {len(gr.vector_chunks)} 段 / "
                            f"cross-seed 补召 {len(gr.cross_seeded_chunks)} 段, "
                            f"根因类别: {rc_hint or '不明'}"),
            })
        logger.info(f"GraphRAG 召回: kg_inc={len(gr.kg_incidents)} "
                    f"kg_act={len(gr.kg_action_templates)} "
                    f"vec={len(gr.vector_chunks)} cross={len(gr.cross_seeded_chunks)}")
    except Exception as e:
        logger.warning(f"GraphRAG 召回失败 (降级到 alertmanager): {e}")

    # ---------- 2. 降级:KG 完全空时拉 alertmanager 历史 ----------
    if not kg_hit and service:
        try:
            client = await get_mcp_client_with_retry()
            tools = await client.get_tools()
            history_tool = next(
                (t for t in tools if getattr(t, "name", "") == "get_alert_history"),
                None,
            )
            if history_tool is not None:
                result = await history_tool.ainvoke({"service": service, "days": 30})
                hist = _coerce_dict(result)
                for h in (hist.get("alerts", []) or []):
                    h_kind = dict(h)
                    h_kind["_kind"] = "alertmanager_alert"
                    similar_incidents.append(h_kind)
                pattern = hist.get("recurrence_pattern", "")
                if pattern:
                    similar_incidents.append({
                        "_kind": "pattern_summary", "summary": pattern,
                    })
                logger.info(f"降级路径召回: {len(similar_incidents)} 条")
        except Exception as e:
            logger.warning(f"历史告警降级召回失败: {e}")
    elif not service:
        logger.info("无 service 信息,跳过降级召回")

    return {
        "similar_incidents": similar_incidents,
        "relevant_runbooks": relevant_runbooks,
    }


# ============================================================
# 辅助
# ============================================================

_SYMPTOM_KEYWORDS = [
    "OOMKilled", "OutOfMemory", "exit code 137",
    "CrashLoopBackOff", "Pod restart",
    "high memory", "high cpu", "cpu saturation",
    "5xx error", "high error rate",
    "high latency", "P99 spike",
    "connection pool", "request timeout",
    "downstream timeout",
    "DNS", "network partition",
    "disk full",
]


def _extract_symptoms(alert: Dict[str, Any], input_text: str) -> List[str]:
    """从告警字段 + 用户输入中抽取症状关键字 (供 KG 查询使用)。"""
    bag = " ".join([
        alert.get("name", "") or "",
        alert.get("summary", "") or "",
        alert.get("description", "") or "",
        input_text or "",
    ]).lower()
    hits = [k for k in _SYMPTOM_KEYWORDS if k.lower() in bag]
    # 兜底:从 alertname 推断
    name = alert.get("name", "")
    if name == "PodCrashLooping" and "CrashLoopBackOff" not in hits:
        hits.append("CrashLoopBackOff")
    if name == "HighMemoryUsage" and "high memory" not in hits:
        hits.append("high memory")
    return hits


_RC_HINTS = [
    ("memory_oom",
     ["oom", "outofmemory", "memory", "exit code 137"]),
    ("cpu_saturation",
     ["cpu saturat", "high cpu", "throttling"]),
    ("config_change",
     ["rollout", "v\\d+", "release", "deploy", "config change"]),
    ("dependency_outage",
     ["upstream", "downstream", "5xx", "dependency", "database outage"]),
    ("capacity",
     ["pool", "limit", "capacity", "quota"]),
    ("network_partition",
     ["dns", "network partition"]),
    ("disk_full",
     ["disk full", "no space"]),
]


def _guess_root_cause(alert: Dict[str, Any], input_text: str,
                     symptoms: List[str]) -> str:
    """根据告警 + 输入 + symptoms 粗推 root_cause 类别(供 KG hint)。"""
    import re as _re
    bag = " ".join([
        alert.get("name", "") or "",
        alert.get("summary", "") or "",
        alert.get("description", "") or "",
        input_text or "",
        " ".join(symptoms),
    ]).lower()
    for rc, patterns in _RC_HINTS:
        for p in patterns:
            if _re.search(p, bag):
                return rc
    return ""


def _guess_service_from_input(text: str) -> str:
    """从用户输入里粗暴猜测服务名(后续 Step 4 用 LLM 抽取)。"""
    candidates = ["data-sync-service", "api-gateway-service"]
    for c in candidates:
        if c in text:
            return c
    return ""


def _build_runbook_query(input_text: str, alert: Dict[str, Any]) -> str:
    """构造 runbook 检索 query: 优先用告警的 alertname,否则用用户输入。"""
    alert_name = (alert.get("labels") or {}).get("alertname") or alert.get("name")
    summary = alert.get("summary")
    if alert_name and summary:
        return f"{alert_name}: {summary}"
    return input_text or "故障诊断"


def _coerce_dict(result: Any) -> Dict[str, Any]:
    """MCP 工具返回值兼容化: 可能是 dict / JSON str / 包装对象。"""
    if isinstance(result, dict):
        return result
    if isinstance(result, str):
        import json
        try:
            return json.loads(result)
        except Exception:
            return {"raw": result}
    return {"raw": str(result)}
