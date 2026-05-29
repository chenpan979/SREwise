"""SRE 多 Agent 共享状态定义。

字段分组
========
- 输入上下文:  alert / input / session_id
- 路由:       next_agent (Supervisor 决定下一个 agent)
- 协作消息:    messages (各 Agent 互相能看到的"对话")
- Agent 产出:  similar_incidents / diagnosis / proposed_actions / incident_report

约定
====
- `next_agent == "END"` 时图终止
- `messages` 用 `operator.add` 追加,不覆盖
- `proposed_actions` Step 2 仅生成不执行,Step 3 加 HITL 后才真正执行
"""

import operator
from typing import Annotated, Any, Dict, List, Literal, Optional, TypedDict

from langchain_core.messages import BaseMessage


# Supervisor 路由的目标节点名
# Step 3 新增: human_review (HITL 审批) + executor (真执行修复动作)
AgentName = Literal[
    "historian",
    "diagnostician",
    "remediator",
    "human_review",
    "executor",
    "reporter",
    "END",
]


class SREState(TypedDict, total=False):
    """SREwise 多 Agent 共享状态。

    使用 total=False 让所有字段可选,因为初始状态只填部分字段。
    """

    # ---------- 输入上下文 ----------
    alert: Optional[Dict[str, Any]]   # 触发的告警(可空,用户也可主动发起诊断)
    input: str                        # 用户原始任务描述或告警摘要
    session_id: str

    # ---------- Supervisor 路由 ----------
    next_agent: AgentName             # 下一个要执行的 agent
    routing_history: Annotated[List[str], operator.add]  # 路由历史(防死循环)

    # ---------- Agent 间协作消息 ----------
    messages: Annotated[List[BaseMessage], operator.add]

    # ---------- Historian 输出 ----------
    similar_incidents: List[Dict[str, Any]]    # 相似历史故障
    relevant_runbooks: List[Dict[str, Any]]    # GraphRAG 召回的文档

    # ---------- Diagnostician 输出 ----------
    diagnosis: Optional[Dict[str, Any]]
    # diagnosis schema:
    # {
    #   "root_cause": str,
    #   "evidence": [{"source": str, "fact": str}, ...],
    #   "confidence": float,  # 0~1
    #   "affected_services": [str, ...],
    # }

    # ---------- Remediator 输出 ----------
    proposed_actions: List[Dict[str, Any]]
    # action schema:
    # {
    #   "tool_name": str,
    #   "args": dict,
    #   "risk_level": "read"|"write"|"destructive",
    #   "rationale": str,           # 为什么提议这个动作
    #   "expected_outcome": str,    # 预期效果
    # }
    approved_actions: List[Dict[str, Any]]      # HITL 审批后的动作 (Step 3 用)
    execution_results: List[Dict[str, Any]]     # 执行结果 (Step 3 用)

    # ---------- Reporter 输出 ----------
    incident_report: Optional[str]              # Markdown 复盘
    kg_writeback_done: bool                     # KG 写回标记 (Step 4 用)
