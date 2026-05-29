"""SREwise 多 Agent StateGraph 装配 (Step 3: 含 HITL)。

拓扑
====
                          ┌─→ historian ───────┐
                          ├─→ diagnostician ───┤
                          ├─→ remediator ──────┤
  START → supervisor ────┤                     ├→ supervisor (回到调度)
                          ├─→ human_review ────┤   (interrupt!)
                          ├─→ executor ────────┤
                          └─→ reporter ────────┘
  supervisor 的 next_agent="END" 时整图终止。

每个 worker 节点完成后都回到 supervisor,由 supervisor 决定下一跳。
human_review 内部会触发 LangGraph 的 interrupt(),checkpointer 保存当前 state 后
暂停执行,直到外部用 Command(resume=...) 恢复。

Checkpointer
============
HITL interrupt 必须有 checkpointer 才能恢复。MVP 阶段用 MemorySaver(进程内),
Step 7 可升级为 SqliteSaver / PostgresSaver。
"""

from typing import Any, Dict

from langgraph.graph import StateGraph, START, END
from langgraph.checkpoint.memory import MemorySaver
from loguru import logger

from .state import SREState
from .supervisor import supervisor
from .historian import historian
from .diagnostician import diagnostician
from .remediator import remediator
from .human_review import human_review
from .executor import executor
from .reporter import reporter


_VALID_NEXT = {"historian", "diagnostician", "remediator",
               "human_review", "executor", "reporter", "END"}


def _route_from_supervisor(state: SREState) -> str:
    """conditional edge: 读 state.next_agent 决定跳到哪个 worker 或 END。"""
    nxt = state.get("next_agent") or "END"
    if nxt not in _VALID_NEXT:
        logger.warning(f"非法 next_agent={nxt!r},强制 END")
        return END
    return END if nxt == "END" else nxt


def build_sre_graph():
    """构建并编译 SREwise StateGraph。"""
    g = StateGraph(SREState)

    g.add_node("supervisor", supervisor)
    g.add_node("historian", historian)
    g.add_node("diagnostician", diagnostician)
    g.add_node("remediator", remediator)
    g.add_node("human_review", human_review)
    g.add_node("executor", executor)
    g.add_node("reporter", reporter)

    g.add_edge(START, "supervisor")

    # supervisor → 由 LLM 决定的 worker (或直接 END)
    g.add_conditional_edges(
        "supervisor",
        _route_from_supervisor,
        {
            "historian": "historian",
            "diagnostician": "diagnostician",
            "remediator": "remediator",
            "human_review": "human_review",
            "executor": "executor",
            "reporter": "reporter",
            END: END,
        },
    )

    # 每个 worker 完成后回到 supervisor
    for worker in ("historian", "diagnostician", "remediator",
                   "human_review", "executor", "reporter"):
        g.add_edge(worker, "supervisor")

    # Checkpointer:HITL interrupt 必须有 checkpointer 才能恢复
    # MVP 阶段用 MemorySaver(进程内),Step 7 可升级为 SqliteSaver / PostgresSaver
    checkpointer = MemorySaver()
    compiled = g.compile(checkpointer=checkpointer)
    logger.info("SREwise StateGraph 编译完成")
    return compiled


# 全局单例 (惰性创建)
_GRAPH = None


def get_sre_graph():
    global _GRAPH
    if _GRAPH is None:
        _GRAPH = build_sre_graph()
    return _GRAPH
