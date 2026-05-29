"""Executor 节点 — 真正执行已批准的修复动作。

职责
====
1. 遍历 approved_actions
2. 用 MCP client 找到对应工具,按 args 调用
3. 把每次调用的结果(success/error + output)写入 execution_results
4. 关键: 不管单个动作成功还是失败,都继续执行下一个,确保 execution_results 完整

设计要点
========
- **double-check risk_level**: 即使是 approved 动作,执行前再校验一次工具的 risk_level
  必须是 write/destructive,杜绝 state 被篡改而调用了 read 工具(防御性编程)
- **失败不抛异常**: 每个动作的失败仅记录到 execution_results,不让一个失败拖垮整图
- **顺序执行**: 不并发,因为修复动作之间通常有依赖(回滚后才能 scale)
"""

import json
import time
from typing import Any, Dict, List

from loguru import logger

from app.agent.mcp_client import get_mcp_client_with_retry, load_mcp_tools_safe
from app.services.observability import emit_event
from app.tools import DEFAULT_LOCAL_REMEDIATION_TOOLS
from .state import SREState
from .tool_filter import extract_risk_level


async def executor(state: SREState) -> Dict[str, Any]:
    """执行已批准的修复动作。"""
    approved = state.get("approved_actions") or []
    if not approved:
        logger.info("=== Executor: 无已批准动作,跳过 ===")
        return {"execution_results": []}

    logger.info(f"=== Executor: 执行 {len(approved)} 个已批准动作 ===")

    # 拿工具: MCP 优先, 失败/超时降级到本地 mock,执行链不被外部依赖拖死
    mcp_tools: List[Any] = []
    try:
        client = await get_mcp_client_with_retry()
        mcp_tools, err = await load_mcp_tools_safe(client, timeout=15.0)
        if err:
            logger.warning(f"MCP get_tools 异常 (将降级到本地工具):\n{err}")
    except Exception as e:
        logger.warning(f"MCP 客户端初始化失败 (将降级到本地工具): {e!r}")

    all_tools = list(mcp_tools) + list(DEFAULT_LOCAL_REMEDIATION_TOOLS)
    tools_by_name: Dict[str, Any] = {}
    for t in all_tools:
        name = getattr(t, "name", None)
        if name and name not in tools_by_name:  # MCP 同名工具优先
            tools_by_name[name] = t
    results: List[Dict[str, Any]] = []

    # 顺序执行
    for idx, action in enumerate(approved, 1):
        tool_name = action.get("tool_name")
        args = action.get("args") or {}
        risk = action.get("risk_level", "")
        logger.info(f"  [{idx}/{len(approved)}] {tool_name}({args}) risk={risk}")

        result_entry: Dict[str, Any] = {
            "index": idx,
            "tool_name": tool_name,
            "args": args,
            "risk_level": risk,
            "rationale": action.get("rationale"),
            "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        tool = tools_by_name.get(tool_name)
        if tool is None:
            result_entry.update(success=False,
                                error=f"工具 {tool_name} 不在可用工具集")
            results.append(result_entry)
            continue

        # double-check risk_level: 不允许误把只读工具走到这里
        actual_risk = extract_risk_level(tool)
        if actual_risk == "read":
            result_entry.update(success=False,
                                error=f"工具 {tool_name} 实际是 read,拒绝执行")
            logger.warning(f"  拒绝: read 工具不应在 executor 中执行")
            results.append(result_entry)
            continue

        # 调工具
        try:
            output = await tool.ainvoke(args)
            output_dict = _coerce(output)
            success = bool(output_dict.get("success", True)) if isinstance(output_dict, dict) else True
            result_entry.update(
                success=success,
                output=output_dict,
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )
            logger.info(f"  ✓ 执行完成: success={success}")
        except Exception as e:
            logger.error(f"  ✗ 执行异常: {e}")
            result_entry.update(
                success=False,
                error=str(e),
                finished_at=time.strftime("%Y-%m-%d %H:%M:%S"),
            )

        results.append(result_entry)

    summary = {"ok": sum(1 for r in results if r.get("success")),
               "fail": sum(1 for r in results if not r.get("success"))}
    logger.info(f"Executor 完成: {summary}")

    # Langfuse: 修复执行是关键审计点,单独 event 出来 (写动作 != LLM 调用)
    emit_event(
        "executor.completed",
        session_id=state.get("session_id"),
        metadata={
            "ok": summary["ok"], "fail": summary["fail"],
            "actions": [{"tool": r.get("tool_name"), "risk": r.get("risk_level"),
                         "success": r.get("success")} for r in results],
        },
        level="DEFAULT" if summary["fail"] == 0 else "WARNING",
    )
    return {"execution_results": results}


def _coerce(out: Any) -> Any:
    """把 MCP 工具返回值标准化为 dict / 原值。"""
    if isinstance(out, (dict, list)):
        return out
    if isinstance(out, str):
        try:
            return json.loads(out)
        except Exception:
            return {"raw": out}
    return {"raw": str(out)}
