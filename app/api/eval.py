"""Eval HTTP 接口。

设计原则
========
- 同步接口太慢 (一次 eval 跑 6 case 可能要 2-5 分钟),所以用 SSE 流式输出
- /api/eval/scenarios   列出可用 case
- /api/eval/run         触发 eval,SSE 推送进度
- /api/eval/last        取最近一次结果摘要 (前端可定时拉)
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from loguru import logger
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.eval.dataset import load_scenarios
from app.eval.runner import run_case
from app.eval.scorer import aggregate, score_case


router = APIRouter()

# 缓存最近一次结果 (内存里,重启清空)
_LAST_RESULT: Optional[Dict[str, Any]] = None


@router.get("/eval/scenarios")
async def list_scenarios():
    """列出所有 eval 场景 (前端展示用)。"""
    scenarios = load_scenarios()
    return {
        "total": len(scenarios),
        "scenarios": [
            {
                "id": s.id, "description": s.description,
                "approval_policy": s.expected.approval_policy,
                "expected_categories": s.expected.root_cause_categories,
                "must_include_any_tool": s.expected.must_include_any_tool,
            }
            for s in scenarios
        ],
    }


class EvalRunRequest(BaseModel):
    case_ids: Optional[List[str]] = None


@router.post("/eval/run")
async def trigger_eval_run(req: Optional[EvalRunRequest] = None):
    """触发一次 eval,SSE 推送 per-case 结果。

    Body (json, 可选):
        {"case_ids": ["oom_canonical_v1", ...]}
    """
    case_ids = req.case_ids if req else None
    scenarios = load_scenarios()
    if case_ids:
        wanted = set(case_ids)
        scenarios = [s for s in scenarios if s.id in wanted]
        if not scenarios:
            raise HTTPException(404, f"no matching cases: {case_ids}")

    async def event_source() -> AsyncGenerator[Dict[str, Any], None]:
        global _LAST_RESULT
        all_scores = []
        raw_states = []
        yield {"event": "start", "data": json.dumps({
            "total": len(scenarios),
            "scenario_ids": [s.id for s in scenarios],
        }, ensure_ascii=False)}

        for sc in scenarios:
            t0 = time.monotonic()
            yield {"event": "case_start", "data": json.dumps({
                "case_id": sc.id, "description": sc.description,
            }, ensure_ascii=False)}
            try:
                final_state, latency, errors = await run_case(sc)
            except Exception as e:
                logger.exception(f"eval case {sc.id} crashed")
                final_state, latency, errors = {}, 0.0, [f"crash: {e}"]
            score = score_case(sc, final_state,
                               latency_seconds=latency, errors=errors)
            all_scores.append(score)
            raw_states.append(final_state)
            yield {"event": "case_done", "data": json.dumps(
                score.to_dict(), ensure_ascii=False,
            )}

        agg = aggregate(all_scores, raw_states=raw_states)
        _LAST_RESULT = agg.to_dict()
        yield {"event": "done", "data": json.dumps(_LAST_RESULT, ensure_ascii=False)}

    return EventSourceResponse(event_source())


@router.get("/eval/last")
async def eval_last():
    """返回最近一次 eval 的聚合结果。"""
    if _LAST_RESULT is None:
        return {"message": "尚未跑过 eval", "result": None}
    return {"result": _LAST_RESULT}
