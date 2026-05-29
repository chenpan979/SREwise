"""Diagnostician 节点 — 根因诊断。

职责
====
1. 拿到所有 risk_level=read 的 MCP 工具(过滤掉任何写工具,杜绝诊断时误改集群)
2. 给 LLM 绑定这些工具,跑 ReAct 风格的工具调用循环,收集证据
3. 循环结束后再调一次 LLM,产出结构化 Diagnosis (根因 + 证据 + 置信度)

为什么不复用旧的 executor?
==========================
旧 executor 一次只执行 plan 里的一个步骤,需要 planner 提前规划。
SRE 诊断更适合让 LLM **自主多轮调用工具**(类似 ReAct):每一轮根据上一轮结果
决定下一步查什么。这是 LangGraph prebuilt `create_react_agent` 模式的简化版。
"""

import json
from textwrap import dedent
from typing import Any, Dict, List

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_qwq import ChatQwen
from langgraph.prebuilt import ToolNode
from loguru import logger
from pydantic import BaseModel, Field

from app.agent.mcp_client import get_mcp_client_with_retry, load_mcp_tools_safe
from app.config import config
from app.tools import DEFAULT_LOCAL_AGENT_TOOLS
from .state import SREState
from .tool_filter import filter_read_tools


# ============================================================
# 结构化输出 schema
# ============================================================

class Evidence(BaseModel):
    source: str = Field(description="证据来源工具名,例如 describe_pod / query_promql")
    fact: str = Field(description="一句话说明这条证据的关键事实")


class Diagnosis(BaseModel):
    root_cause: str = Field(description="根因结论,一段简短描述")
    evidence: List[Evidence] = Field(description="支撑根因的关键证据列表")
    confidence: float = Field(
        description="置信度 0~1。> 0.7 视为可继续修复,< 0.5 视为不充分需要补查",
        ge=0.0, le=1.0,
    )
    affected_services: List[str] = Field(
        default_factory=list,
        description="受影响的服务名列表",
    )


# ============================================================
# Prompt
# ============================================================

_DIAGNOSE_SYSTEM = dedent("""
    你是 SREwise 的诊断专家 Agent。

    任务: 通过调用只读 MCP 工具(日志/监控/k8s/grafana/alertmanager)定位故障根因。

    工作方式 (ReAct)
    ================
    每一轮你可以选择:
    - 调用一个工具收集更多证据
    - 或者输出 "DIAGNOSIS_READY" 表示证据已足够

    诊断思路 (按优先级)
    ===================
    1. 先看告警和历史:它告诉你"哪个服务、什么症状、过去如何解决"
    2. 看 Pod 状态: list_pods + describe_pod 找出异常 pod 的 status / restart_count / last_state
    3. 看日志和指标: get_pod_logs / query_promql 验证假设
    4. 看 Deployment 历史: describe_deployment 找最近发布是不是诱因

    限制
    ====
    - 一次只调一个工具,基于上一轮结果再决定下一步
    - 总工具调用次数 <= {max_steps}
    - 当你确信根因后立即停止,不要"为查而查"
""").strip()

_FINAL_SYSTEM = dedent("""
    根据上方收集到的所有证据,严格按 Diagnosis schema 输出结构化诊断结论。

    要求
    ====
    - root_cause 必须基于实际证据,不要编造
    - evidence 至少 2 条,每条标明来源工具
    - confidence 客观评估:证据链完整且互相印证 -> 0.8+;
      仅有间接证据 -> 0.5~0.7;
      关键信息缺失 -> < 0.5
""").strip()


# ============================================================
# 节点实现
# ============================================================

MAX_TOOL_CALLS = 6


async def diagnostician(state: SREState) -> Dict[str, Any]:
    """诊断 Agent 入口。"""
    logger.info("=== Diagnostician: 根因诊断 ===")

    # 1. 拿工具集 (只读); MCP 失败时打印真实子异常,自动降级到本地工具
    mcp_tools: List[Any] = []
    try:
        client = await get_mcp_client_with_retry()
        mcp_tools, err = await load_mcp_tools_safe(client, timeout=15.0)
        if err:
            logger.warning(f"MCP get_tools 异常 (将降级到本地工具):\n{err}")
    except Exception as e:
        logger.warning(f"MCP 客户端初始化失败 (将降级到本地工具): {e!r}")

    all_tools = list(DEFAULT_LOCAL_AGENT_TOOLS) + list(mcp_tools)
    read_tools = filter_read_tools(all_tools)
    logger.info(f"诊断可用只读工具: {len(read_tools)}")

    if not read_tools:
        logger.error("没有任何只读工具可用,无法诊断")
        return {"diagnosis": {
            "root_cause": "无法诊断: MCP 工具不可用",
            "evidence": [], "confidence": 0.0, "affected_services": [],
        }}

    # 2. 构造初始 messages
    context = _build_context(state)
    llm = ChatQwen(model=config.rag_model,
                   api_key=config.dashscope_api_key,
                   temperature=0)
    llm_with_tools = llm.bind_tools(read_tools)
    tool_node = ToolNode(read_tools)

    messages: List[Any] = [
        SystemMessage(content=_DIAGNOSE_SYSTEM.format(max_steps=MAX_TOOL_CALLS)),
        HumanMessage(content=context),
    ]

    # 3. ReAct 循环
    for step in range(MAX_TOOL_CALLS):
        logger.info(f"  诊断轮次 {step + 1}/{MAX_TOOL_CALLS}")
        ai_msg = await llm_with_tools.ainvoke(messages)
        messages.append(ai_msg)

        tool_calls = getattr(ai_msg, "tool_calls", None)
        if not tool_calls:
            logger.info("  LLM 未再调用工具,结束循环")
            break

        logger.info(f"  调用 {len(tool_calls)} 个工具: "
                    + ", ".join(tc.get("name", "?") for tc in tool_calls))
        tool_result = await tool_node.ainvoke({"messages": messages})
        # ToolNode 返回 {"messages": [ToolMessage, ...]}
        messages.extend(tool_result["messages"])

    # 4. 最后让 LLM 输出结构化 Diagnosis
    logger.info("  生成结构化 Diagnosis ...")
    final_messages = list(messages) + [SystemMessage(content=_FINAL_SYSTEM)]
    structured = llm.with_structured_output(Diagnosis)
    try:
        diag = await structured.ainvoke(final_messages)
        diag_dict = (diag.model_dump() if isinstance(diag, Diagnosis)
                     else dict(diag))  # type: ignore
    except Exception as e:
        logger.error(f"结构化诊断失败: {e}")
        diag_dict = {
            "root_cause": "诊断结构化输出失败,请查看 messages 历史",
            "evidence": [], "confidence": 0.3, "affected_services": [],
        }

    logger.info(f"  根因: {diag_dict.get('root_cause', '')[:120]}")
    logger.info(f"  置信度: {diag_dict.get('confidence', 0)}")

    return {"diagnosis": diag_dict}


# ============================================================
# 上下文拼装
# ============================================================

def _build_context(state: SREState) -> str:
    """把 alert + similar_incidents + runbooks 揉成一段 Human 上下文。"""
    parts: List[str] = []

    parts.append(f"# 任务\n{state.get('input', '')}\n")

    alert = state.get("alert")
    if alert:
        parts.append("# 触发告警\n```json\n"
                     + json.dumps(alert, ensure_ascii=False, indent=2)
                     + "\n```\n")

    incidents = state.get("similar_incidents") or []
    if incidents:
        kg_incidents = [i for i in incidents if i.get("_kind") == "kg_incident"]
        kg_actions = [i for i in incidents if i.get("_kind") == "kg_action_template"]
        am_alerts = [i for i in incidents if i.get("_kind") == "alertmanager_alert"]
        summaries = [i for i in incidents if i.get("_kind") == "pattern_summary"]

        block_lines: List[str] = []

        if kg_incidents:
            block_lines.append("# 故障知识图谱召回 (KG Incident)")
            for i, inc in enumerate(kg_incidents[:5], 1):
                actions = inc.get("resolved_actions") or []
                act_str = ", ".join(f"{a.get('tool_name')}(success={a.get('success')})"
                                    for a in actions[:3]) or "(无)"
                block_lines.append(
                    f"- [{i}] {inc.get('alert_name')} on {inc.get('service')} "
                    f"@ {inc.get('started_at')} (score={inc.get('score'):.1f})\n"
                    f"     根因类别: {inc.get('root_cause_category')} | "
                    f"症状: {inc.get('symptoms')}\n"
                    f"     成功修复: {act_str}"
                )
            block_lines.append("")

        if kg_actions:
            block_lines.append("# KG 推荐 Action 模板 (按命中次数排序)")
            for i, t in enumerate(kg_actions[:5], 1):
                block_lines.append(
                    f"- [{i}] {t.get('tool_name')} 累计命中 {t.get('hit_count')} 次, "
                    f"最近 {t.get('last_used_at')}, 样本参数: {t.get('sample_args')}"
                )
            block_lines.append("")

        if am_alerts:
            block_lines.append("# 降级路径召回 (alertmanager.history)")
            for i, h in enumerate(am_alerts[:5], 1):
                block_lines.append(
                    f"- [{i}] {h.get('name')} | severity={h.get('severity')}"
                    f" | resolved_by={h.get('resolved_by', '?')}"
                )
            block_lines.append("")

        if summaries:
            block_lines.append("# 模式总结")
            for s in summaries:
                block_lines.append(f"- {s.get('summary')}")

        parts.append("\n".join(block_lines))

    runbooks = state.get("relevant_runbooks") or []
    if runbooks:
        primary = [r for r in runbooks if r.get("source") == "graphrag_vector"]
        cross = [r for r in runbooks if r.get("source") == "graphrag_cross_seed"]
        legacy = [r for r in runbooks if r.get("source") not in
                  ("graphrag_vector", "graphrag_cross_seed")]

        block_lines: List[str] = []
        if primary:
            block_lines.append("# Runbook (GraphRAG 主召回)")
            for i, r in enumerate(primary[:3], 1):
                meta = r.get("metadata") or {}
                tag = (f"{meta.get('_kind', '?')}/{meta.get('service', '-')}"
                       f"/{meta.get('root_cause', '-')}")
                content = (r.get("content") or "")[:900]
                block_lines.append(
                    f"## [{i}] {tag} (channel={r.get('channel')}, "
                    f"score={r.get('score')})\n{content}"
                )
        if cross:
            block_lines.append("\n# Runbook (Cross-seed: 用 KG incident.summary 反查向量库)")
            for i, r in enumerate(cross[:3], 1):
                meta = r.get("metadata") or {}
                content = (r.get("content") or "")[:600]
                block_lines.append(
                    f"## [{i}] seed={r.get('seed_incident_id')} "
                    f"({meta.get('_kind', '?')})\n{content}"
                )
        if legacy:
            block_lines.append("\n# Runbook (其他来源)")
            for r in legacy[:2]:
                content = (r.get("content") or "")[:600]
                block_lines.append(f"- {r.get('source', '?')}: {content}")
        parts.append("\n".join(block_lines))

    parts.append("\n请开始诊断,自主调用工具收集证据。")
    return "\n".join(parts)
