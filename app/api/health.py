"""健康检查接口"""

import asyncio
import time
from typing import Any, Dict
from fastapi import APIRouter
from fastapi.responses import JSONResponse
from app.config import config
from app.core.milvus_client import milvus_manager
from app.services.incident_kg import incident_kg
from app.services.observability import (
    is_enabled as langfuse_enabled,
    live_probe as langfuse_live_probe,
)
from loguru import logger

router = APIRouter()

# 5 秒缓存,避免前端 12s 轮询 + 用户手刷把后端压垮
_CACHE: Dict[str, Any] = {"ts": 0.0, "value": None, "status": 200}
_CACHE_TTL = 5.0


@router.get("/health")
async def health_check():
    
    """健康检查接口
    检查服务状态和数据库连接状态
    
    Returns:
        JSONResponse: 健康检查结果
    """
    # 命中缓存
    now = time.monotonic()
    if _CACHE["value"] is not None and (now - _CACHE["ts"]) < _CACHE_TTL:
        return JSONResponse(status_code=_CACHE["status"], content=_CACHE["value"])

    health_data: dict[str, Any] = {  # pyright: ignore[reportExplicitAny]
        "service": config.app_name,
        "version": config.app_version,
        "status": "healthy",
    }

    # ---- 并发三路活检 ----
    async def _probe_milvus() -> bool:
        try:
            return await asyncio.to_thread(milvus_manager.health_check)
        except Exception as e:
            logger.warning(f"Milvus 健康检查异常: {e}")
            return False

    async def _probe_kg() -> bool:
        try:
            return await incident_kg.live_probe(timeout=2.0)
        except Exception as e:
            logger.warning(f"KG 健康检查异常: {e}")
            return False

    async def _probe_langfuse() -> bool:
        if not langfuse_enabled():
            return False
        try:
            return await langfuse_live_probe(timeout=2.0)
        except Exception:
            return False

    milvus_ok, kg_ok, lf_ok = await asyncio.gather(
        _probe_milvus(), _probe_kg(), _probe_langfuse(),
    )

    # ---- 组装结果 ----
    health_data["milvus"] = {
        "status": "connected" if milvus_ok else "disconnected",
        "message": "Milvus 连接正常" if milvus_ok else "Milvus 连接异常 (探活失败)",
    }
    health_data["incident_kg"] = {
        "status": "connected" if kg_ok else "disconnected",
        "message": "Neo4j 已就绪" if kg_ok
                   else ("Neo4j 不可用 (将走降级路径)" if config.neo4j_enabled
                         else "Neo4j 未启用"),
    }
    if not langfuse_enabled():
        health_data["langfuse"] = {
            "status": "disabled", "message": "未启用 (可观测性关闭)",
        }
    else:
        health_data["langfuse"] = {
            "status": "connected" if lf_ok else "disconnected",
            "message": ("已接入 " + config.langfuse_host) if lf_ok
                       else f"探活失败: {config.langfuse_host}",
        }

    # 整体状态:Milvus 不通 → unhealthy(503);其他降级 → degraded(200)
    if not milvus_ok:
        overall, status_code = "unhealthy", 503
        health_data["error"] = "数据库不可用"
    elif (not kg_ok and config.neo4j_enabled) or \
         (langfuse_enabled() and not lf_ok):
        overall, status_code = "degraded", 200
    else:
        overall, status_code = "healthy", 200
    health_data["status"] = overall

    body = {
        "code": status_code,
        "message": {"healthy": "服务运行正常",
                    "degraded": "服务降级运行",
                    "unhealthy": "服务不可用"}[overall],
        "data": health_data,
    }
    _CACHE["ts"] = now
    _CACHE["value"] = body
    _CACHE["status"] = status_code
    return JSONResponse(status_code=status_code, content=body)
