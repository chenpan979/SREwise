"""Supervisor 节点 — SREwise 多 Agent 路由 LLM。

职责
====
读 state,决定下一步走哪个 agent。决策依据:
1. 还没召回过历史 → historian
2. 还没诊断过 → diagnostician
3. 已诊断且置信度足够 → remediator
4. 已生成动作或已执行 → reporter
5. 已写报告 → END

设计要点
========
- 用 LLM `with_structured_output` 强约束输出 next_agent
- 同时维护 routing_history,Supervisor 看到自己已经在某个 agent 上路由过 N 次时
  会强制推进,避免死循环
- 路由决策本身也是一次 LLM 调用,可观测性 (Step 6) 时这就是关键 trace
"""

from textwrap import dedent
from typing import Any, Dict, List

from langchain_core.prompts import ChatPromptTemplate
from langchain_qwq import ChatQwen
from loguru import logger
from pydantic import BaseModel, Field

from app.config import config
from .state import AgentName, SREState


# ============================================================
# 路由输出 schema
# ============================================================

class RouteDecision(BaseModel):
    """Supervisor 路由决策。"""
    next_agent: str = Field(
        description=("下一个要执行的 agent,必须是以下之一:"
                     "'historian'(召回相似历史故障)、"
                     "'diagnostician'(基于 MCP 工具诊断根因)、"
                     "'remediator'(生成候选修复动作)、"
                     "'reporter'(生成最终复盘报告)、"
                     "'END'(任务完成,终止)")
    )
    reason: str = Field(description="一句话说明路由理由")


VALID_AGENTS = {"historian", "diagnostician", "remediator",
                "human_review", "executor", "reporter", "END"}


# ============================================================
# 路由提示词
# ============================================================

_SUPERVISOR_SYSTEM = dedent("""
    你是 SREwise 多 Agent 系统的总调度官 (Supervisor)。
    根据当前状态,决定下一个执行的 Agent。

    可调度 Agent
    ============
    - **historian**: 从历史故障库召回相似事件 + 检索相关 runbook。通常**最先调用**。
    - **diagnostician**: 调用只读 MCP 工具定位根因。historian 之后或并行。
    - **remediator**: 基于诊断结论生成候选修复动作。需 diagnosis 后调。
    - **human_review**: HITL 审批 (高危动作执行前的人工闸门)。
      只能在 remediator 之后、proposed_actions 非空、approved_actions 为空时调。
    - **executor**: 执行已批准动作 (写 MCP 工具调用)。
      只能在 approved_actions 非空、execution_results 为空时调。
    - **reporter**: 生成 Markdown 复盘报告。需 diagnosis 才能调。
    - **END**: 任务完成。已生成 incident_report 后才能选 END。

    决策规则 (按优先级从上到下)
    ===========================
    1. incident_report 已生成 → END
    2. execution_results 已生成 (不管成败) → reporter
    3. approved_actions 非空但 execution_results 为空 → executor
    4. proposed_actions 非空但 approved_actions 为空 (且未被拒) → human_review
    5. diagnosis 已生成但 proposed_actions 为空 → remediator
       (取决于诊断置信度:若 < 0.5 可考虑直接走 reporter 跳过修复)
    6. similar_incidents 已召回但还没 diagnosis → diagnostician
    7. 什么都没做 → historian

    特别注意
    ========
    - human_review 拒绝后 (approved_actions 为空且 messages 里有拒绝记录) → 跳过
      executor,直接走 reporter
    - 不要反复调 historian/diagnostician,一轮即可

    防死循环
    ========
    routing_history 列出了你已经路由过的 agent 序列。
    - 同一个 agent 不要连续调度超过 2 次
    - 总路由次数超过 8 次,直接选 reporter 或 END

    输出
    ====
    严格按 RouteDecision schema 输出 next_agent + reason。
""").strip()

_supervisor_prompt = ChatPromptTemplate.from_messages([
    ("system", _SUPERVISOR_SYSTEM),
    ("user", dedent("""
        当前任务: {input}

        告警(可空): {alert}

        已完成情况:
        - similar_incidents 数量: {n_incidents}
        - diagnosis 是否生成: {has_diagnosis}
        - proposed_actions 数量: {n_actions}
        - incident_report 是否生成: {has_report}

        已路由历史 (按时间序): {routing_history}

        请决定下一个 agent。
    """).strip()),
])


# ============================================================
# 节点实现
# ============================================================

async def supervisor(state: SREState) -> Dict[str, Any]:
    """Supervisor 路由节点。"""
    logger.info("=== Supervisor: 路由决策 ===")

    history = state.get("routing_history", []) or []
    n_routes = len(history)

    # ------- 硬规则护栏 (省 LLM 调用 + 防死循环) -------
    if state.get("incident_report"):
        logger.info("incident_report 已生成 → END")
        return _route("END", "report ready", history)

    if n_routes >= 12:
        logger.warning(f"路由次数 {n_routes} 过多,强制 END")
        return _route("END", "max routes reached", history)

    # ---- Step 3 硬规则: HITL 与执行顺序必须严格 ----
    proposed = state.get("proposed_actions") or []
    approved = state.get("approved_actions") or []
    executed = state.get("execution_results") or []

    # 已执行过 → reporter
    if executed:
        return _route("reporter", "execution finished", history)
    # 已批但未执行 → executor
    if approved and not executed:
        return _route("executor", "actions approved, run executor", history)
    # 有候选动作但未审批 → human_review
    # (这里不区分“被拒绝”场景。拒绝时 human_review 节点会返回空 approved_actions,
    # 但为了不重复拉审批,我们靠 history 里是否出现过 human_review 来判断)
    if proposed and not approved and "human_review" not in history:
        return _route("human_review", "need approval", history)
    # 审批被拒 (history 包含 human_review 但 approved 为空) → 跳 reporter
    if proposed and not approved and "human_review" in history:
        return _route("reporter", "approval rejected, skip executor", history)

    # 同一 agent 连续调用 3 次 → 强制推进
    if len(history) >= 3 and history[-1] == history[-2] == history[-3]:
        last = history[-1]
        forced = _next_after(last)
        logger.warning(f"agent {last} 连续 3 次,强制推进到 {forced}")
        return _route(forced, f"force advance from {last}", history)

    # ------- LLM 路由 -------
    llm = ChatQwen(
        model=config.rag_model,
        api_key=config.dashscope_api_key,
        temperature=0,
    )
    chain = _supervisor_prompt | llm.with_structured_output(RouteDecision)

    try:
        decision = await chain.ainvoke({
            "input": state.get("input", ""),
            "alert": _summarize_alert(state.get("alert")),
            "n_incidents": len(state.get("similar_incidents", []) or []),
            "has_diagnosis": bool(state.get("diagnosis")),
            "n_actions": len(state.get("proposed_actions", []) or []),
            "has_report": bool(state.get("incident_report")),
            "routing_history": history or ["(empty)"],
        })
        chosen = (decision.next_agent if isinstance(decision, RouteDecision)
                  else decision.get("next_agent", "END"))  # type: ignore
        reason = (decision.reason if isinstance(decision, RouteDecision)
                  else decision.get("reason", ""))  # type: ignore

        if chosen not in VALID_AGENTS:
            logger.warning(f"LLM 返回非法 agent {chosen!r},降级到 reporter")
            chosen = "reporter"

        logger.info(f"路由 → {chosen}  ({reason})")
        return _route(chosen, reason, history)

    except Exception as e:
        logger.error(f"Supervisor LLM 失败: {e}; 降级 fallback")
        return _route(_fallback_route(state), "fallback", history)


# ============================================================
# 辅助函数
# ============================================================

def _route(next_agent: str, reason: str, prev_history: List[str]) -> Dict[str, Any]:
    """构造 state 更新。next_agent 写入,routing_history 追加(operator.add)。"""
    return {
        "next_agent": next_agent,
        "routing_history": [next_agent],  # operator.add 会追加而非覆盖
    }


def _fallback_route(state: SREState) -> str:
    """LLM 失败时的硬编码路由。"""
    if state.get("incident_report"):
        return "END"
    if state.get("diagnosis"):
        if state.get("proposed_actions"):
            return "reporter"
        return "remediator"
    if state.get("similar_incidents"):
        return "diagnostician"
    return "historian"


def _next_after(agent: str) -> str:
    """连续路由同 agent 时的强制推进规则。"""
    progression = ["historian", "diagnostician", "remediator",
                   "human_review", "executor", "reporter", "END"]
    if agent in progression:
        idx = progression.index(agent)
        return progression[min(idx + 1, len(progression) - 1)]
    return "reporter"


def _summarize_alert(alert: Any) -> str:
    if not alert:
        return "(无,用户主动发起)"
    if isinstance(alert, dict):
        parts = []
        for k in ("name", "severity", "service", "summary"):
            v = alert.get(k)
            if v:
                parts.append(f"{k}={v}")
        return ", ".join(parts) or str(alert)[:200]
    return str(alert)[:200]
