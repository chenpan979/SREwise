"""Reporter 节点 — 生成 Markdown 故障复盘。

职责
====
1. 生成 Markdown 复盘 (基于 state 中的告警/历史/诊断/修复动作/执行结果)
2. **Step 4 新增**: 将本次 Incident 写回 Neo4j 知识图谱,设置
   kg_writeback_done = True 供后续事件追溯到原节点。
   写回使用 incident_kg.upsert_incident,如果 KG 不可用会静默跳过。
"""

import json
from textwrap import dedent
from typing import Any, Dict, List

from langchain_core.prompts import ChatPromptTemplate
from langchain_qwq import ChatQwen
from loguru import logger

from app.config import config
from app.services.graph_rag import graph_rag
from app.services.incident_kg import incident_kg
from .state import SREState


_SYSTEM = dedent("""
    你是 SREwise 的故障复盘 Agent (Reporter)。

    根据收集到的信息生成一份结构化的 Markdown 故障复盘,作为最终交付物。

    复盘必须包含以下小节 (按顺序):
    ============================
    ## 1. 故障摘要
    一段话说明:发生了什么、影响范围、严重等级、目前状态

    ## 2. 时间线
    bullet 列表,按时间顺序记录关键事件 (告警触发 / 关键证据发现 / 提议动作 / 执行结果)

    ## 3. 根因分析
    完整说明根因结论,引用具体证据 (来自 diagnosis.evidence)

    ## 4. 处置动作
    列出 proposed_actions / approved_actions / execution_results,清楚标注:
    - 哪些已批准、已执行
    - 哪些被拒绝或还在等待
    - 每个动作的 risk_level

    ## 5. 历史关联
    如果 similar_incidents 显示此问题反复发生,在这里点出,并建议长期改进

    ## 6. 后续改进建议 (Action Items)
    针对根因给出 2~4 条可落地的改进项

    要求
    ====
    - 不要编造 state 中不存在的信息
    - 用 Markdown,标题层级使用 ## (二级)
    - 用中文撰写
    - 总长度 600~1500 字
""").strip()

_prompt = ChatPromptTemplate.from_messages([
    ("system", _SYSTEM),
    ("user", dedent("""
        # 任务
        {input}

        # 触发告警
        ```json
        {alert_json}
        ```

        # 历史召回 (similar_incidents)
        {similar_block}

        # 相关 runbook
        {runbook_block}

        # 诊断结论
        ```json
        {diagnosis_json}
        ```

        # 候选/已批/已执行动作
        - proposed_actions: {proposed_count} 条
        ```json
        {proposed_json}
        ```
        - approved_actions: {approved_count} 条
        ```json
        {approved_json}
        ```
        - execution_results: {executed_count} 条
        ```json
        {executed_json}
        ```

        请生成完整的 Markdown 复盘报告。
    """).strip()),
])


async def reporter(state: SREState) -> Dict[str, Any]:
    """生成 Markdown 复盘报告。"""
    logger.info("=== Reporter: 生成复盘报告 ===")

    llm = ChatQwen(model=config.rag_model,
                   api_key=config.dashscope_api_key,
                   temperature=0.2)
    chain = _prompt | llm

    inputs = {
        "input": state.get("input", ""),
        "alert_json": _safe_json(state.get("alert"), "无告警 (用户主动发起)"),
        "similar_block": _format_similar(state.get("similar_incidents") or []),
        "runbook_block": _format_runbooks(state.get("relevant_runbooks") or []),
        "diagnosis_json": _safe_json(state.get("diagnosis"), "无诊断结论"),
        "proposed_count": len(state.get("proposed_actions") or []),
        "proposed_json": _safe_json(state.get("proposed_actions") or [], "[]"),
        "approved_count": len(state.get("approved_actions") or []),
        "approved_json": _safe_json(state.get("approved_actions") or [], "[]"),
        "executed_count": len(state.get("execution_results") or []),
        "executed_json": _safe_json(state.get("execution_results") or [], "[]"),
    }

    try:
        resp = await chain.ainvoke(inputs)
        report = resp.content if hasattr(resp, "content") else str(resp)
    except Exception as e:
        logger.error(f"复盘生成失败: {e}")
        report = _fallback_report(state)

    logger.info(f"复盘报告生成完成,长度: {len(report)}")

    # Step 4: 将本次 Incident 写回 KG
    kg_done = await _writeback_to_kg(state)

    return {
        "incident_report": report,
        "kg_writeback_done": kg_done,
    }


# ============================================================
# KG 写回
# ============================================================

_SYMPTOM_KEYWORDS = [
    "OOMKilled", "OutOfMemory", "exit code 137",
    "CrashLoopBackOff", "Pod restart",
    "high memory", "high cpu", "cpu saturation",
    "5xx error", "high error rate",
    "high latency", "P99 spike",
    "connection pool", "request timeout",
    "downstream timeout",
]


async def _writeback_to_kg(state: SREState) -> bool:
    """将本次 Incident 以 (诊断+执行结果) 譯出,写入 Neo4j KG。

    返回 True / False 标识是否成功。KG 不可用时静默返回 False。
    """
    if not incident_kg.ready:
        logger.debug("[Reporter] KG 不可用,跳过写回")
        return False

    diagnosis = state.get("diagnosis") or {}
    if not diagnosis.get("root_cause"):
        logger.debug("[Reporter] 无诊断结论,不写 KG")
        return False

    alert = state.get("alert") or {}
    service = (alert.get("service")
               or (diagnosis.get("affected_services") or [""])[0]
               or "unknown-service")
    namespace = ((alert.get("labels") or {}).get("namespace")
                 or alert.get("namespace") or "production")
    alert_name = (alert.get("name")
                  or (alert.get("labels") or {}).get("alertname")
                  or "ManualDiagnosis")
    started_at = alert.get("started_at")
    severity = alert.get("severity", "warning")
    summary = alert.get("summary") or diagnosis.get("root_cause", "")[:200]

    # 抽取 symptoms (从告警 + diagnosis.evidence + state.input 中淸取)
    symptoms = _extract_symptoms(alert, diagnosis, state.get("input", ""))

    # 从 evidence 中推断 root_cause_category (LLM 输出不保证是受控 key)
    rc_cat = _infer_rc_category(diagnosis, symptoms)

    # actions: 合并 approved + execution_results 取 success
    actions = _build_actions(state)

    # status 按执行结果让步: 有任一成功 → resolved, 全失败/未执行 → ongoing
    if any(r.get("success") for r in (state.get("execution_results") or [])):
        status = "resolved"
    else:
        status = "ongoing"

    try:
        inc_id = await incident_kg.upsert_incident(
            alert_name=alert_name, service=service, namespace=namespace,
            severity=severity, started_at=started_at, summary=summary,
            status=status, root_cause_category=rc_cat,
            root_cause_description=diagnosis.get("root_cause", ""),
            symptoms=symptoms, actions=actions,
            confidence=float(diagnosis.get("confidence", 0.0) or 0.0),
        )
        logger.info(f"[Reporter] Incident 已写回 KG: {inc_id} "
                    f"(rc={rc_cat}, status={status}, actions={len(actions)})")

        # Step 5: 同步写入向量库,后续可被 GraphRAG 语义召回
        if inc_id:
            try:
                vec_summary = (f"{alert_name} on {service}: "
                               f"{diagnosis.get('root_cause', '')}")
                extra = "症状: " + ", ".join(symptoms) if symptoms else ""
                await graph_rag.index_incident_text(
                    incident_id=inc_id, summary=vec_summary,
                    service=service, root_cause_category=rc_cat,
                    severity=severity, started_at=started_at,
                    extra_text=extra,
                )
            except Exception as e:
                logger.warning(f"[Reporter] GraphRAG 向量库写入失败 (不影响主流程): {e}")

        return True
    except Exception as e:
        logger.warning(f"[Reporter] KG 写回失败: {e}")
        return False


def _extract_symptoms(alert: Dict[str, Any],
                      diagnosis: Dict[str, Any],
                      input_text: str) -> List[str]:
    bag = " ".join([
        alert.get("name", "") or "",
        alert.get("summary", "") or "",
        alert.get("description", "") or "",
        diagnosis.get("root_cause", "") or "",
        " ".join(e.get("fact", "") if isinstance(e, dict) else str(e)
                 for e in (diagnosis.get("evidence") or [])),
        input_text or "",
    ]).lower()
    hits = [k for k in _SYMPTOM_KEYWORDS if k.lower() in bag]
    # 判断 alertname 补充
    name = alert.get("name", "")
    if name == "PodCrashLooping" and "CrashLoopBackOff" not in hits:
        hits.append("CrashLoopBackOff")
    if name == "HighMemoryUsage" and "high memory" not in hits:
        hits.append("high memory")
    return hits


def _infer_rc_category(diagnosis: Dict[str, Any], symptoms: List[str]) -> str:
    """从诊断结论 + symptoms 推断 root_cause 受控类别 key。"""
    text = " ".join([
        diagnosis.get("root_cause", "") or "",
        " ".join(symptoms),
    ]).lower()
    if any(k in text for k in ("oom", "memory", "heap", "exit code 137")):
        return "memory_oom"
    if any(k in text for k in ("cpu", "throttle", "saturat")):
        return "cpu_saturation"
    if any(k in text for k in ("deploy", "release", "rollout", "version", "config")):
        return "config_change"
    if any(k in text for k in ("upstream", "downstream", "dependency", "5xx")):
        return "dependency_outage"
    if any(k in text for k in ("pool", "limit", "capacity", "quota")):
        return "capacity"
    if any(k in text for k in ("dns", "network partition")):
        return "network_partition"
    if any(k in text for k in ("disk full", "no space")):
        return "disk_full"
    return "unknown"


def _build_actions(state: SREState) -> List[Dict[str, Any]]:
    """从 approved_actions + execution_results 提取写回用的 actions。"""
    execs = state.get("execution_results") or []
    if execs:
        return [
            {"tool_name": r.get("tool_name"),
             "args": r.get("args") or {},
             "success": bool(r.get("success"))}
            for r in execs if r.get("tool_name")
        ]
    # 没有执行过,则只记录获准但未执行的动作 (success=False)
    approved = state.get("approved_actions") or []
    return [
        {"tool_name": a.get("tool_name"),
         "args": a.get("args") or {},
         "success": False}
        for a in approved if a.get("tool_name")
    ]


# ============================================================
# 辅助
# ============================================================

def _safe_json(obj: Any, fallback: str) -> str:
    if obj is None:
        return fallback
    try:
        return json.dumps(obj, ensure_ascii=False, indent=2, default=str)
    except Exception:
        return fallback


def _format_similar(incidents: List[Dict[str, Any]]) -> str:
    if not incidents:
        return "(无)"
    lines = []
    for inc in incidents[:5]:
        if inc.get("_kind") == "pattern_summary":
            lines.append(f"- 模式总结: {inc.get('summary')}")
        else:
            lines.append(f"- {inc.get('started_at', '?')} | {inc.get('name', '?')}"
                         f" | resolved_by={inc.get('resolved_by', '?')}")
    return "\n".join(lines)


def _format_runbooks(runbooks: List[Dict[str, Any]]) -> str:
    if not runbooks:
        return "(无)"
    out = []
    for r in runbooks[:2]:
        content = (r.get("content") or "")[:600]
        out.append(f"- 来源 {r.get('source', '?')}:\n{content}")
    return "\n\n".join(out)


def _fallback_report(state: SREState) -> str:
    diag = state.get("diagnosis") or {}
    return dedent(f"""
        # 故障复盘 (降级版本)

        ## 任务
        {state.get('input', '(无)')}

        ## 根因
        {diag.get('root_cause', '诊断不可用')}

        ## 候选动作数
        {len(state.get('proposed_actions') or [])}

        > ⚠️ 由于 LLM 调用失败,本报告为降级版本。
    """).strip()
