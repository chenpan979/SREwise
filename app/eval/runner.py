"""Eval Runner — 单 case / 批量执行多 Agent 图,带自动审批。

为什么需要自动审批
==================
SREwise 的图在 human_review 节点 interrupt() 等外部审批。Eval 是无人值守,
所以 runner 在收到 __interrupt__ 时立即根据 scenario.expected.approval_policy
决定 decision 并 resume,模拟"自动批准 bot"。

approval_policy:
- "approve_all"               : 全批 (含 destructive,谨慎使用)
- "approve_all_non_destructive": 批所有非 destructive 动作 (默认)
- "reject_all"                : 全拒 (用于测安全门)
- "approve_first_only"        : 只批第一个动作
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from langgraph.types import Command
from loguru import logger

from app.agent.sre.graph import get_sre_graph
from app.services.observability import (
    build_runnable_config,
    emit_event,
    get_callback_handler,
)
from .dataset import Expected, Scenario


async def run_case(
    scenario: Scenario,
    *,
    session_id: Optional[str] = None,
    max_interrupts: int = 3,
    timeout_seconds: float = 240.0,
) -> Tuple[Dict[str, Any], float, List[str]]:
    """跑单个 case,返回 (final_state, latency_seconds, errors_list)。

    跑完后从 graph.aget_state() 拉出最终 state。即便出错也尽量返回部分 state,
    让 scorer 能对已完成的部分打分。
    """
    session_id = session_id or f"eval-{scenario.id}-{uuid.uuid4().hex[:6]}"
    errors: List[str] = []
    start = time.monotonic()

    graph = get_sre_graph()

    # Langfuse 接入,给 eval 加 tag,便于在 UI 过滤
    callbacks = get_callback_handler(
        session_id=session_id,
        trace_name=f"eval.{scenario.id}",
        metadata={"eval_case": scenario.id},
        tags=["eval", scenario.id],
    )
    thread_cfg = build_runnable_config(
        callbacks,
        session_id=session_id,
        trace_name=f"eval.{scenario.id}",
        metadata={"eval_case": scenario.id},
        tags=["eval", scenario.id],
        extra={"configurable": {"thread_id": session_id},
               "recursion_limit": 40},
    )

    # 初始 state
    alert = scenario.alert
    query = scenario.query
    input_text = (
        query
        or (f"诊断告警 [{alert.get('name')}] on {alert.get('service')}: "
            f"{alert.get('summary')}") if alert else "请进行健康巡检"
    )
    payload: Any = {
        "session_id": session_id,
        "alert": alert,
        "input": input_text,
        "routing_history": [],
    }

    emit_event(
        "eval.case.start", session_id=session_id,
        metadata={"case_id": scenario.id},
    )

    # 主循环: 可能 interrupt 多次 (理论上 SREwise 当前只 interrupt 一次)
    for round_idx in range(max_interrupts + 1):
        interrupted = False
        try:
            async for chunk in _astream_with_timeout(
                graph, payload, thread_cfg,
                timeout=timeout_seconds - (time.monotonic() - start),
            ):
                if "__interrupt__" in chunk:
                    interrupted = True
                    # 拿出当前 proposed 决定怎么 resume
                    snapshot = await graph.aget_state(thread_cfg)
                    proposed = (snapshot.values or {}).get("proposed_actions") or []
                    decision = _auto_decision(scenario.expected, proposed)
                    logger.info(
                        f"[eval/{scenario.id}] auto-approve round={round_idx} "
                        f"policy={scenario.expected.approval_policy} "
                        f"approve={decision.get('approve')} "
                        f"selected={decision.get('selected_indices')}"
                    )
                    payload = Command(resume=decision)
                    break  # 跳出 async for,外层 for 进下一轮
        except asyncio.TimeoutError:
            errors.append(f"timeout after {timeout_seconds}s at round {round_idx}")
            break
        except Exception as e:
            logger.exception(f"[eval/{scenario.id}] 图执行异常: {e}")
            errors.append(f"{type(e).__name__}: {e}")
            break

        if not interrupted:
            break  # 正常跑完
    else:
        errors.append(f"超过 {max_interrupts} 次 interrupt 仍未结束")

    # 拿最终 state
    final_state: Dict[str, Any] = {}
    try:
        snapshot = await graph.aget_state(thread_cfg)
        final_state = dict(snapshot.values or {})
    except Exception as e:
        errors.append(f"aget_state failed: {e}")

    latency = time.monotonic() - start
    emit_event(
        "eval.case.end", session_id=session_id,
        metadata={"case_id": scenario.id, "latency_seconds": latency,
                  "errors": len(errors)},
        level="DEFAULT" if not errors else "WARNING",
    )
    return final_state, latency, errors


# ============================================================
# 自动审批策略
# ============================================================

def _auto_decision(expected: Expected,
                   proposed: List[Dict[str, Any]]) -> Dict[str, Any]:
    """根据 expected.approval_policy 计算 resume decision。"""
    policy = expected.approval_policy

    base = {"approver": "eval-bot", "comment": f"auto:{policy}"}

    if policy == "reject_all" or not proposed:
        return {**base, "approve": False, "selected_indices": []}

    if policy == "approve_all":
        return {**base, "approve": True, "selected_indices": []}

    if policy == "approve_first_only":
        return {**base, "approve": True, "selected_indices": [0]}

    # 默认 approve_all_non_destructive
    indices = [i for i, a in enumerate(proposed)
               if a.get("risk_level") != "destructive"]
    if not indices:
        return {**base, "approve": False, "selected_indices": []}
    return {**base, "approve": True, "selected_indices": indices}


# ============================================================
# 超时包装
# ============================================================

async def _astream_with_timeout(graph, payload, cfg, *, timeout: float):
    """给 astream 加 wall-clock 超时,防止某个 case 卡死整个 eval。"""
    if timeout <= 0:
        raise asyncio.TimeoutError(f"budget exhausted")

    async def _gen():
        async for ch in graph.astream(payload, config=cfg, stream_mode="updates"):
            yield ch

    # 用 wait_for 包外层任务太复杂,这里给单次 anext 加超时
    g = _gen()
    deadline = time.monotonic() + timeout
    while True:
        remain = deadline - time.monotonic()
        if remain <= 0:
            raise asyncio.TimeoutError("eval case timeout")
        try:
            chunk = await asyncio.wait_for(g.__anext__(), timeout=remain)
        except StopAsyncIteration:
            return
        yield chunk
