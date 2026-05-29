"""Human-in-the-Loop 审批节点。

机制
====
使用 LangGraph 的 `interrupt()` 在此节点暂停整个图执行,把候选动作 (proposed_actions)
"举到" interrupt payload 里,等待外部审批。

- 调用方第一次 ainvoke 时:执行到此节点 → interrupt() 抛 GraphInterrupt → checkpointer
  保存当前状态 → 调用方在 stream 中拿到 __interrupt__ 事件
- 用户审批后:调用方再次 ainvoke 并传入 `Command(resume=decision)`,LangGraph 会
  从此节点恢复,interrupt() 返回 resume 传入的值,节点继续执行,把 decision 翻译为
  approved_actions 写回 state

decision schema (调用方传入)
============================
{
  "approve": bool,                      # 全批准 or 全拒绝
  "selected_indices": [0, 2],           # 部分批准时,要批的动作索引 (可选)
  "comment": str,                       # 审批人备注 (可选)
  "reviewer": str,                      # 审批人 ID (可选)
}

约定: approve=true 且 selected_indices 为空时,代表批准所有 proposed_actions
"""

from typing import Any, Dict, List

from langgraph.types import interrupt
from loguru import logger

from .state import SREState


async def human_review(state: SREState) -> Dict[str, Any]:
    """HITL 审批节点。"""
    proposed = state.get("proposed_actions") or []
    if not proposed:
        logger.info("无候选动作,跳过审批")
        return {"approved_actions": []}

    logger.info(f"=== Human Review: 等待审批 {len(proposed)} 个候选动作 ===")

    # 把要给审批人看的信息打包,触发 interrupt
    payload = {
        "kind": "approval_required",
        "session_id": state.get("session_id"),
        "diagnosis": state.get("diagnosis"),
        "proposed_actions": proposed,
        "instructions": (
            "请审批候选修复动作。返回 decision dict: "
            "{approve: bool, selected_indices: [int]?, comment: str?, reviewer: str?}"
        ),
    }

    # 这里执行会暂停;恢复时 interrupt(payload) 的返回值就是外部传入的 decision
    decision: Any = interrupt(payload)
    logger.info(f"收到审批结果: {decision}")

    # 兼容多种返回结构
    decision = decision or {}
    if isinstance(decision, dict):
        approve = bool(decision.get("approve", False))
        selected = decision.get("selected_indices") or []
        comment = decision.get("comment", "")
        reviewer = decision.get("reviewer", "anonymous")
    else:
        # 简单情况: resume 传一个 bool
        approve = bool(decision)
        selected, comment, reviewer = [], "", "anonymous"

    if not approve:
        logger.info(f"审批拒绝 (reviewer={reviewer}, comment={comment!r})")
        return {
            "approved_actions": [],
            "messages": [],  # 占位,如果需要可以加 SystemMessage 注释
        }

    if selected:
        approved = [proposed[i] for i in selected
                    if isinstance(i, int) and 0 <= i < len(proposed)]
    else:
        approved = list(proposed)  # 全批准

    # 给每个批准的动作打上审批元数据,便于后续报告
    for a in approved:
        a["_approval"] = {"reviewer": reviewer, "comment": comment}

    logger.info(f"审批通过 {len(approved)}/{len(proposed)} 个动作 (reviewer={reviewer})")
    return {"approved_actions": approved}
