"""FastAPI 应用入口

主应用程序，配置路由、中间件、静态文件等
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from contextlib import asynccontextmanager
import os

from app.config import config
from loguru import logger
from app.api import chat, health, file, aiops, sre, eval as eval_api
from app.core.milvus_client import milvus_manager
from app.services.incident_kg import incident_kg
from app.services.incident_kg_seed import seed_if_empty
from app.services.graph_rag import graph_rag
from app.services.observability import init_langfuse, shutdown_langfuse


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动时执行
    logger.info("=" * 60)
    logger.info(f"🚀 {config.app_name} v{config.app_version} 启动中...")
    logger.info(f"📝 环境: {'开发' if config.debug else '生产'}")
    logger.info(f"🌐 监听地址: http://{config.host}:{config.port}")
    logger.info(f"📚 API 文档: http://{config.host}:{config.port}/docs")
    
    # 连接 Milvus
    logger.info("🔌 正在连接 Milvus...")
    milvus_manager.connect()
    logger.info("✅ Milvus 连接成功")

    # 连接 Neo4j (Step 4: Incident Knowledge Graph)
    logger.info("🔗 正在连接 Neo4j (Incident KG)...")
    kg_ok = await incident_kg.connect()
    if kg_ok:
        logger.info("✅ Neo4j 已就绪")
        if config.neo4j_seed_on_startup:
            try:
                seed_result = await seed_if_empty()
                logger.info(f"🌱 KG seed: {seed_result}")
            except Exception as e:
                logger.warning(f"⚠️ KG seed 失败: {e}")
    else:
        logger.warning("⚠️ Neo4j 不可用,Historian/Reporter 将使用降级路径")

    # Langfuse 可观测性 (Step 6)
    logger.info("📡 正在初始化 Langfuse 可观测性...")
    init_langfuse()

    # GraphRAG runbook 种子 (Step 5)
    logger.info("🧩 正在初始化 GraphRAG (runbook 种子)...")
    try:
        seed_rb = await graph_rag.seed_builtin_runbooks()
        logger.info(f"📚 GraphRAG runbook seed: {seed_rb}")
    except Exception as e:
        logger.warning(f"⚠️ GraphRAG runbook seed 失败: {e}")

    logger.info("=" * 60)

    yield

    # 关闭时执行
    logger.info("🔌 正在 flush Langfuse 队列...")
    shutdown_langfuse()
    logger.info("🔌 正在关闭 Neo4j 连接...")
    await incident_kg.close()
    logger.info("🔌 正在关闭 Milvus 连接...")
    milvus_manager.close()
    logger.info(f"👋 {config.app_name} 关闭")


# 创建 FastAPI 应用
app = FastAPI(
    title=config.app_name,
    version=config.app_version,
    description="SREwise — 自治式 SRE 智能体平台 (多 Agent + MCP + 故障知识图谱 + Human-in-the-Loop)",
    lifespan=lifespan
)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 生产环境应该限制具体域名
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(health.router, tags=["健康检查"])
app.include_router(chat.router, prefix="/api", tags=["对话"])
app.include_router(file.router, prefix="/api", tags=["文件管理"])
app.include_router(aiops.router, prefix="/api", tags=["AIOps智能运维 (旧)"])
app.include_router(sre.router, prefix="/api", tags=["SREwise 多 Agent SRE"])
app.include_router(eval_api.router, prefix="/api", tags=["SREwise Eval"])

# 挂载静态文件
static_dir = "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/")
async def root():
    """根路径默认重定向到 SREwise Console。"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/console/", status_code=307)


@app.get("/console")
@app.get("/console/")
async def console_index():
    """SREwise 多 Agent SRE 控制台 (Step 8 重构,生产级 SPA)。"""
    index_path = os.path.join(static_dir, "console", "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "console/index.html not found"}


@app.get("/legacy")
async def legacy_index():
    """旧版聊天界面入口 (保留供回退)。"""
    index_path = os.path.join(static_dir, "index.html")
    if os.path.exists(index_path):
        return FileResponse(index_path)
    return {"message": "legacy index.html not found",
            "version": config.app_version, "docs": "/docs"}


if __name__ == "__main__":
    import uvicorn
    
    uvicorn.run(
        "app.main:app",
        host=config.host,
        port=config.port,
        reload=config.debug,
        log_level="info"
    )
