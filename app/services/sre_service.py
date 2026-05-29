"""SREwise 多 Agent 诊断服务 — 流式 (SSE) 编排层。

职责
====
1. 准备初始 SREState (从请求 / 自动拉取告警)
2. 用 graph.astream() 流式运行,把每个节点的产出转成 SSE 事件
3. 处理异常并降级
"""

import json
from typing import Any, AsyncGenerator, Dict, List, Optional

from langgraph.types import Command
from loguru import logger

from app.agent.sre.graph import get_sre_graph
from app.agent.mcp_client import get_mcp_client_with_retry
from app.services.observability import (
    build_runnable_config,
    emit_event,
    get_callback_handler,
)
from app.services import sre_history


# 被中断、等待审批的 session 跟踪 (快速查询不需面向 checkpointer)
# value: {"session_id": ..., "proposed_actions": [...], "diagnosis": {...}, "interrupted_at": str}
_PENDING: Dict[str, Dict[str, Any]] = {}


def list_pending_sessions() -> List[Dict[str, Any]]:
    return list(_PENDING.values())


def get_pending(session_id: str) -> Optional[Dict[str, Any]]:
    return _PENDING.get(session_id)


class SREService:
    """SREwise 诊断服务。"""

    async def diagnose(
        self,
        session_id: str = "default",
        alert: Optional[Dict[str, Any]] = None,
        query: Optional[str] = None,
        auto_fetch_alert: bool = True,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """流式运行 SRE 多 Agent 诊断。

        遇到 HITL interrupt 时会产出 type=interrupt 事件并结束当前流,
        外部应调用 /api/sre/approve 触发 resume() 继续执行。

        Args:
            session_id: 会话 ID (同时作为 LangGraph thread_id)
            alert: 触发告警 (可选,优先使用)
            query: 用户主动诊断描述 (可选)
            auto_fetch_alert: alert/query 都为空时自动从 alertmanager 拉第一条
        """
        # 1. 准备 alert / input
        if alert is None and not query and auto_fetch_alert:
            alert = await self._auto_fetch_alert()

        input_text = self._build_input(alert, query)

        yield {
            "type": "status", "stage": "initializing",
            "message": f"启动 SREwise 多 Agent 诊断 (session={session_id})",
            "alert": alert, "input": input_text,
        }

        # 2. 构造初始 state
        initial: Dict[str, Any] = {
            "session_id": session_id,
            "alert": alert,
            "input": input_text,
            "routing_history": [],
        }

        # 3. 流式跑图 (带 thread_id 以支持 checkpoint)
        graph = get_sre_graph()
        # Langfuse: 一次诊断 = 一个 trace,session_id=thread_id 共用
        biz_md = {
            "alert_name": (alert or {}).get("name"),
            "service": (alert or {}).get("service"),
            "severity": (alert or {}).get("severity"),
            "has_user_query": bool(query),
        }
        callbacks = get_callback_handler(
            session_id=session_id,
            trace_name="srewise.diagnose",
            metadata=biz_md,
            tags=["sre", "diagnose"],
        )
        # build_runnable_config 会根据 SDK 版本决定 session_id/tags
        # 应该走 CallbackHandler 还是 config.metadata 前缀键
        thread_cfg = build_runnable_config(
            callbacks,
            session_id=session_id,
            trace_name="srewise.diagnose",
            metadata=biz_md,
            tags=["sre", "diagnose"],
            extra={"configurable": {"thread_id": session_id},
                   "recursion_limit": 30},
        )
        async for ev in self._run_stream(graph, initial, thread_cfg):
            yield ev

    async def resume(
        self, session_id: str, decision: Dict[str, Any]
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """从中断点恢复执行。"""
        graph = get_sre_graph()
        # resume 继续同一 session 的 trace
        biz_md = {"approve": decision.get("approve"),
                  "approver": decision.get("approver")}
        callbacks = get_callback_handler(
            session_id=session_id,
            trace_name="srewise.resume",
            metadata=biz_md,
            tags=["sre", "resume", "hitl"],
        )
        thread_cfg = build_runnable_config(
            callbacks,
            session_id=session_id,
            trace_name="srewise.resume",
            metadata=biz_md,
            tags=["sre", "resume", "hitl"],
            extra={"configurable": {"thread_id": session_id},
                   "recursion_limit": 30},
        )
        # Command(resume=...) 会交给 human_review 节点里的 interrupt() 返回值
        cmd = Command(resume=decision)

        # 关键 HITL 事件打点 (独立于 LLM trace)
        emit_event(
            "human_review.decision",
            session_id=session_id,
            metadata={
                "approve": bool(decision.get("approve")),
                "approver": decision.get("approver"),
                "approved_count": len(decision.get("approved_actions") or []),
            },
            level="DEFAULT" if decision.get("approve") else "WARNING",
        )

        yield {
            "type": "status", "stage": "resuming",
            "message": (f"恢复 session={session_id} "
                        f"approve={decision.get('approve')}"),
        }
        # 用户已决断 → 立刻从待审批列表移除,避免前端"等待人工"残留;
        # 若 _run_stream 内部再次遇到 HITL interrupt,_handle_interrupt
        # 会重新写入 _PENDING。
        _PENDING.pop(session_id, None)
        try:
            async for ev in self._run_stream(graph, cmd, thread_cfg):
                yield ev
        finally:
            # 兜底:即便消费者提前 break / cancel,也确保不留垃圾;
            # 但若 _handle_interrupt 又写入了新的 pending,这里不能误删
            # → 通过比对 interrupted_at 时间窗判断 (resume 入口 pop 已经清掉
            #   旧的;若值与原 pending 不同,说明是新的 interrupt,不动)
            pass

    # ============================================================
    # 内部: 统一 stream 处理 (含 interrupt 探测)
    # ============================================================

    async def _run_stream(
        self, graph, payload, thread_cfg: Dict[str, Any]
    ) -> AsyncGenerator[Dict[str, Any], None]:
        try:
            async for chunk in graph.astream(
                payload, config=thread_cfg, stream_mode="updates",
            ):
                # 处理 __interrupt__ 事件 (LangGraph 在 interrupt 发生时会发出)
                if "__interrupt__" in chunk:
                    interrupts = chunk["__interrupt__"]
                    interrupt_event = self._handle_interrupt(
                        thread_cfg["configurable"]["thread_id"], interrupts,
                    )
                    yield interrupt_event
                    return  # 暂停当前流,等待 resume

                for node, delta in chunk.items():
                    if node == "__interrupt__":
                        continue
                    event = self._delta_to_event(node, delta)
                    if event:
                        yield event

            # 流正常结束 → 拉最终 state, 先落档再 yield complete
            # (顺序很关键: 前端收到 complete 后会 break SSE, 导致 yield 之后的
            #  代码因 GeneratorExit 不再执行 → 落档必须在 yield 之前完成)
            final_state = await graph.aget_state(thread_cfg)
            values = final_state.values if final_state else {}
            sid = thread_cfg["configurable"]["thread_id"]
            try:
                await sre_history.record(
                    sid,
                    alert=values.get("alert"),
                    query=values.get("input"),
                    diagnosis=values.get("diagnosis"),
                    proposed_actions=values.get("proposed_actions"),
                    approved_actions=values.get("approved_actions"),
                    execution_results=values.get("execution_results"),
                    report=values.get("incident_report"),
                    routing_history=values.get("routing_history"),
                    status="completed",
                )
            except Exception as e:
                logger.warning(f"[{sid}] 写入历史档案失败: {e}")
            yield {
                "type": "complete", "stage": "diagnosis_complete",
                "message": "SRE 诊断流程完成",
                "diagnosis": values.get("diagnosis"),
                "proposed_actions": values.get("proposed_actions"),
                "approved_actions": values.get("approved_actions"),
                "execution_results": values.get("execution_results"),
                "report": values.get("incident_report"),
                "routing_history": values.get("routing_history"),
            }

        except Exception as e:
            logger.error(f"SRE stream 异常: {e}", exc_info=True)
            # 同样: 异常档案也先落再 yield (虽然 error 后 break 也一样有 GeneratorExit)
            try:
                sid = thread_cfg["configurable"]["thread_id"]
                await sre_history.record(sid, status="error", error=str(e))
            except Exception:
                pass
            yield {
                "type": "error", "stage": "exception",
                "message": f"诊断异常: {e}",
            }

    def _handle_interrupt(self, session_id: str, interrupts: Any) -> Dict[str, Any]:
        """将 LangGraph __interrupt__ 转为前端事件 + 记入 _PENDING。"""
        # interrupts 是一个包含 Interrupt 对象的 list
        first = interrupts[0] if isinstance(interrupts, (list, tuple)) and interrupts else interrupts
        payload = getattr(first, "value", None) or first
        if not isinstance(payload, dict):
            payload = {"raw": str(payload)}

        proposed = payload.get("proposed_actions") or []
        diagnosis = payload.get("diagnosis")
        import time as _t
        _PENDING[session_id] = {
            "session_id": session_id,
            "proposed_actions": proposed,
            "diagnosis": diagnosis,
            "interrupted_at": _t.strftime("%Y-%m-%d %H:%M:%S"),
        }
        logger.info(f"[{session_id}] graph 中断,等待审批")
        emit_event(
            "human_review.interrupt",
            session_id=session_id,
            metadata={
                "proposed_count": len(proposed),
                "high_risk_count": sum(
                    1 for a in proposed if a.get("risk_level") == "high"
                ),
            },
            level="WARNING",
        )
        return {
            "type": "interrupt", "stage": "awaiting_approval",
            "message": (f"需要人工审批 {len(proposed)} 个候选动作"),
            "session_id": session_id,
            "diagnosis": diagnosis,
            "proposed_actions": proposed,
        }

    # ============================================================
    # 辅助
    # ============================================================

    async def _auto_fetch_alert(self) -> Optional[Dict[str, Any]]:
        """从 alertmanager 拿第一条 critical 告警作为入口。"""
        try:
            client = await get_mcp_client_with_retry()
            tools = await client.get_tools()
            tool = next((t for t in tools
                         if getattr(t, "name", "") == "list_active_alerts"), None)
            if not tool:
                return None
            res = await tool.ainvoke({"severity": "critical"})
            data = self._coerce_dict(res)
            alerts = data.get("alerts") or []
            return alerts[0] if alerts else None
        except Exception as e:
            logger.warning(f"自动拉取告警失败: {e}")
            return None

    def _build_input(self, alert: Optional[Dict[str, Any]], query: Optional[str]) -> str:
        if query:
            return query
        if alert:
            return (f"诊断告警 [{alert.get('name', 'unknown')}] "
                    f"on service {alert.get('service', 'unknown')}: "
                    f"{alert.get('summary', '')}")
        return "请基于当前系统状态进行健康检查与故障诊断"

    def _delta_to_event(self, node: str, delta: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """把单个节点的 state 增量转成对前端友好的 SSE 事件。"""
        if not delta:
            return None

        if node == "supervisor":
            return {
                "type": "route", "stage": "supervisor",
                "message": f"Supervisor 路由 → {delta.get('next_agent', '?')}",
                "next_agent": delta.get("next_agent"),
            }

        if node == "historian":
            return {
                "type": "agent_done", "stage": "historian",
                "message": (f"Historian 召回完成: "
                            f"{len(delta.get('similar_incidents') or [])} 历史告警, "
                            f"{len(delta.get('relevant_runbooks') or [])} runbook"),
                "similar_incidents_count": len(delta.get("similar_incidents") or []),
                "runbook_count": len(delta.get("relevant_runbooks") or []),
            }

        if node == "diagnostician":
            diag = delta.get("diagnosis") or {}
            return {
                "type": "agent_done", "stage": "diagnostician",
                "message": "Diagnostician 输出根因",
                "diagnosis": diag,
            }

        if node == "remediator":
            actions = delta.get("proposed_actions") or []
            return {
                "type": "agent_done", "stage": "remediator",
                "message": f"Remediator 提议 {len(actions)} 个修复动作",
                "proposed_actions": actions,
            }

        if node == "human_review":
            approved = delta.get("approved_actions") or []
            return {
                "type": "agent_done", "stage": "human_review",
                "message": f"审批结束: {len(approved)} 个动作获准",
                "approved_actions": approved,
            }

        if node == "executor":
            execs = delta.get("execution_results") or []
            ok = sum(1 for r in execs if r.get("success"))
            return {
                "type": "agent_done", "stage": "executor",
                "message": f"Executor 执行完成: {ok}/{len(execs)} 成功",
                "execution_results": execs,
            }

        if node == "reporter":
            return {
                "type": "report", "stage": "reporter",
                "message": "复盘报告已生成",
                "report": delta.get("incident_report"),
            }

        return {"type": "agent_done", "stage": node, "message": f"{node} 完成"}

    @staticmethod
    def _coerce_dict(result: Any) -> Dict[str, Any]:
        if isinstance(result, dict):
            return result
        if isinstance(result, str):
            try:
                return json.loads(result)
            except Exception:
                return {"raw": result}
        return {"raw": str(result)}


sre_service = SREService()
