"""SREwise 多 Agent SRE API。"""

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import PlainTextResponse, Response
from loguru import logger
from sse_starlette.sse import EventSourceResponse

from app.models.sre import SREApproveRequest, SREDiagnoseRequest
from app.services.graph_rag import graph_rag
from app.services.incident_kg import (
    ROOT_CAUSE_CATEGORIES,
    incident_kg,
    normalize_root_cause,
)
from app.services import sre_history
from app.services.sre_service import (
    get_pending,
    list_pending_sessions,
    sre_service,
)


router = APIRouter()


@router.post("/sre/diagnose")
async def sre_diagnose(request: SREDiagnoseRequest):
    """SREwise 多 Agent 故障诊断 (流式 SSE)。

    SSE 事件类型
    ============
    - status        启动 / 初始化
    - route         Supervisor 路由决策 (含 next_agent 字段)
    - agent_done    某个 worker agent 执行完成 (historian/diagnostician/remediator)
    - report        Reporter 输出 Markdown 复盘
    - complete      整图执行完毕,带最终聚合结果
    - error         异常
    """
    session_id = request.session_id or "default"
    logger.info(f"[会话 {session_id}] SRE diagnose 请求: "
                f"alert={'yes' if request.alert else 'no'}, "
                f"query={'yes' if request.query else 'no'}")

    async def event_generator():
        try:
            async for event in sre_service.diagnose(
                session_id=session_id,
                alert=request.alert,
                query=request.query,
                auto_fetch_alert=request.auto_fetch_alert,
            ):
                yield {
                    "event": "message",
                    "data": json.dumps(event, ensure_ascii=False, default=str),
                }
                if event.get("type") in ("complete", "error"):
                    break
            logger.info(f"[会话 {session_id}] SRE diagnose 流结束")
        except Exception as e:
            logger.error(f"[会话 {session_id}] SRE diagnose 流异常: {e}",
                         exc_info=True)
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "error", "stage": "exception",
                    "message": f"诊断异常: {e}",
                }, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())


# ============================================================
# Step 4: Incident Knowledge Graph 查询接口
# ============================================================

@router.get("/sre/kg/stats")
async def sre_kg_stats():
    """KG 统计:节点 / 关系数 + 分类计数 + ready/seeded 标记。"""
    return await incident_kg.stats()


@router.get("/sre/kg/root-causes")
async def sre_kg_root_causes():
    """返回受控字典:根因类别 → 描述。"""
    return {"categories": ROOT_CAUSE_CATEGORIES}


@router.get("/sre/kg/similar")
async def sre_kg_similar(
    service: str | None = None,
    root_cause: str | None = None,
    keywords: str | None = None,
    limit: int = 5,
):
    """查询相似 Incident。

    Args:
        service: 服务名过滤,可选
        root_cause: 根因类别(支持模糊关键字,内部会归一)
        keywords: 逗号分隔的症状关键字,如 "OOMKilled,CrashLoopBackOff"
        limit: 返回条数
    """
    if not incident_kg.ready:
        raise HTTPException(status_code=503, detail="Incident KG (Neo4j) 不可用")
    sym_list = [k.strip() for k in (keywords or "").split(",") if k.strip()] or None
    rc = normalize_root_cause(root_cause) if root_cause else None
    items = await incident_kg.find_similar_incidents(
        service=service, root_cause_category=rc,
        symptom_keywords=sym_list, limit=limit,
    )
    return {"total": len(items), "items": items,
            "params": {"service": service, "root_cause": rc,
                       "keywords": sym_list, "limit": limit}}


@router.get("/sre/kg/actions")
async def sre_kg_actions(
    root_cause: str,
    service: str | None = None,
    limit: int = 5,
):
    """按根因(可选 service)查询历史成功修复 Action 模板,按命中数排序。"""
    if not incident_kg.ready:
        raise HTTPException(status_code=503, detail="Incident KG (Neo4j) 不可用")
    rc = normalize_root_cause(root_cause)
    items = await incident_kg.get_action_templates(
        root_cause_category=rc, service=service, limit=limit,
    )
    return {"total": len(items), "items": items,
            "root_cause": rc, "service": service}


@router.get("/sre/kg/subgraph")
async def sre_kg_subgraph(
    incident_id: str | None = None,
    depth: int = 2,
    limit_nodes: int = 80,
):
    """导出可视化用子图。

    incident_id 不传则返回全图 (受 limit_nodes 限制) 的快照。
    """
    if not incident_kg.ready:
        raise HTTPException(status_code=503, detail="Incident KG (Neo4j) 不可用")
    return await incident_kg.export_subgraph(
        around_incident=incident_id, depth=depth, limit_nodes=limit_nodes,
    )


# ============================================================
# Step 5: GraphRAG 混合召回接口
# ============================================================

@router.get("/sre/graphrag/query")
async def sre_graphrag_query(
    q: str,
    service: str | None = None,
    root_cause: str | None = None,
    keywords: str | None = None,
    top_k_kg: int = 5,
    top_k_vector: int = 4,
    top_k_cross: int = 3,
    enable_cross_seed: bool = True,
):
    """GraphRAG 混合召回 (KG 结构化 + 向量语义 + cross-seed)。

    Args:
        q: 自然语言 query (必填)
        service: 服务过滤,可选
        root_cause: 根因类别 hint,内部会归一
        keywords: 逗号分隔的症状关键字
        top_k_kg / top_k_vector / top_k_cross: 三路召回上限
        enable_cross_seed: 是否启用 cross-seed (默认开)
    """
    syms = [k.strip() for k in (keywords or "").split(",") if k.strip()] or None
    rc = normalize_root_cause(root_cause) if root_cause else None
    result = await graph_rag.query(
        query_text=q,
        service=service, root_cause=rc, symptom_keywords=syms,
        top_k_kg=top_k_kg, top_k_vector=top_k_vector, top_k_cross=top_k_cross,
        enable_cross_seed=enable_cross_seed,
    )
    return result.to_dict()


@router.post("/sre/graphrag/reseed")
async def sre_graphrag_reseed():
    """手动重灌内置 runbook 种子 (开发期调试用,生产请关闭)。"""
    # 重置标记,允许重新执行
    graph_rag._seeded = False  # type: ignore[attr-defined]
    return await graph_rag.seed_builtin_runbooks()


# ============================================================
# Pending sessions
# ============================================================


@router.get("/sre/pending")
async def sre_list_pending():
    """列出当前所有等待人工审批的 session。"""
    items = list_pending_sessions()
    return {"total": len(items), "items": items}


@router.get("/sre/pending/{session_id}")
async def sre_get_pending(session_id: str):
    """查看某个待审批 session 的详情(候选动作 + 诊断结论)。"""
    item = get_pending(session_id)
    if not item:
        raise HTTPException(status_code=404,
                            detail=f"session {session_id} 不在待审批列表中")
    return item


@router.post("/sre/approve")
async def sre_approve(request: SREApproveRequest):
    """提交审批结果,从中断点恢复 SRE 流程 (流式 SSE)。

    selected_indices 为空且 approve=true 时,代表批准全部 proposed_actions。
    approve=false 时,跳过 executor 直接走 reporter。
    """
    pending = get_pending(request.session_id)
    if not pending:
        raise HTTPException(status_code=404,
                            detail=f"session {request.session_id} 不在待审批列表中"
                                   "(可能已超时或被处理过)")

    decision = {
        "approve": request.approve,
        "selected_indices": request.selected_indices or [],
        "comment": request.comment or "",
        "reviewer": request.reviewer or "anonymous",
    }
    logger.info(f"[会话 {request.session_id}] 收到审批: {decision}")

    async def event_generator():
        try:
            async for event in sre_service.resume(request.session_id, decision):
                yield {
                    "event": "message",
                    "data": json.dumps(event, ensure_ascii=False, default=str),
                }
                if event.get("type") in ("complete", "error"):
                    break
        except Exception as e:
            logger.error(f"[会话 {request.session_id}] resume 异常: {e}",
                         exc_info=True)
            yield {
                "event": "message",
                "data": json.dumps({
                    "type": "error", "stage": "exception",
                    "message": f"恢复异常: {e}",
                }, ensure_ascii=False),
            }

    return EventSourceResponse(event_generator())


# ============================================================
# History (故障档案) — 持久化已完成 session, 供"故障档案"页查看与下载
# ============================================================


@router.get("/sre/history")
async def sre_history_list(limit: int = 50, offset: int = 0):
    """分页列出最近的诊断历史 (倒序)。仅返回摘要字段。"""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    items = sre_history.list_records(limit=limit, offset=offset)
    return {
        "total": sre_history.total_records(),
        "limit": limit,
        "offset": offset,
        "items": items,
    }


@router.get("/sre/history/{session_id}")
async def sre_history_detail(session_id: str):
    """单个 session 的完整档案 (alert / diagnosis / actions / executions / report)。"""
    rec = sre_history.get_record(session_id)
    if not rec:
        raise HTTPException(status_code=404,
                            detail=f"session {session_id} 不在历史档案中")
    return rec


@router.get("/sre/history/{session_id}/report.md")
async def sre_history_report_md(session_id: str):
    """以 Markdown 附件形式下载该 session 的复盘报告。"""
    md = sre_history.get_report_markdown(session_id)
    if md is None:
        raise HTTPException(status_code=404,
                            detail=f"session {session_id} 不在历史档案中")
    headers = {
        "Content-Disposition":
            f'attachment; filename="srewise-{session_id}.md"',
    }
    return Response(content=md, media_type="text/markdown; charset=utf-8",
                    headers=headers)
