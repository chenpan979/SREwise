"""SREwise Step 6 — Langfuse 可观测性接入。

设计原则
========
1. **可选启用**: 默认关闭 (langfuse_enabled=False),配置全空也不会报错。
   即便 Langfuse 服务挂了也不能让主流程崩。
2. **零侵入**: 业务代码不显式 import Langfuse,而是通过本模块拿 callbacks。
   关掉 Langfuse 时所有调用都返回 [] / no-op,业务无感知。
3. **三层覆盖**:
   - LLM 调用      → LangChain `CallbackHandler` 自动埋点 (历史/诊断/修复/复盘)
   - 关键服务函数  → `@observe()` 装饰器 (KG / GraphRAG 的内部步骤)
   - 自定义事件    → `langfuse_client.event(...)` (审批通过/拒绝、interrupt 触发等)
4. **Session 模型**: 一次 SRE 诊断对应一个 Langfuse session_id (= sre session_id),
   多 Agent 节点共享 trace,可以在 UI 看到节点瀑布图。

外部接口
========
- `init_langfuse()`     — 启动时调用,缓存全局客户端
- `shutdown_langfuse()` — 关闭时 flush 队列
- `get_callback_handler(session_id, trace_name, metadata, user_id)` — 给 LangGraph
  invoke 用,返回 list[BaseCallbackHandler] (空数组表示禁用)
- `get_langfuse()` — 给手工埋点用,可能返回 None (业务方需做空判断)
- `is_enabled()` — 业务端轻量判断
"""

from __future__ import annotations

import os
from typing import Any, Callable, Dict, List, Optional

from loguru import logger

from app.config import config

# 延迟 import:Langfuse 是可选依赖,关闭时不强制可用
_langfuse_client: Optional[Any] = None
_init_attempted: bool = False
_sdk_major_version: Optional[int] = None   # 2 或 3 ,init 后填


# ============================================================
# SDK 兼容层 — v2 / v3 的 import 路径不一样,运行时探测
# ============================================================
#
# Langfuse python SDK 在 v2 → v3 间挪动了好几个符号:
#   - observe 装饰器:  v2 `langfuse.decorators.observe` → v3 `langfuse.observe`
#   - CallbackHandler:  v2 `langfuse.callback`         → v3 `langfuse.langchain`
#   - CallbackHandler 构造签名也变了:v3 不再接 session_id/trace_name/metadata 等
#     kwargs,这些信息要通过 chain config 的 metadata 字段传 (`langfuse_session_id` 等)
#
# 下面所有 import 都做 try/except,任何一个版本都能跑起来。
# ============================================================


def _import_observe():
    """返回 observe 装饰器 (兼容 v2/v3),失败返回 None。"""
    try:
        # v3
        from langfuse import observe as _o  # type: ignore[import-untyped]
        return _o
    except ImportError:
        pass
    try:
        # v2
        from langfuse.decorators import observe as _o  # type: ignore[import-untyped]
        return _o
    except ImportError:
        return None


def _import_callback_handler():
    """返回 CallbackHandler 类 + sdk major version。"""
    try:
        # v3
        from langfuse.langchain import CallbackHandler as _CH  # type: ignore[import-untyped]
        return _CH, 3
    except ImportError:
        pass
    try:
        # v2
        from langfuse.callback import CallbackHandler as _CH  # type: ignore[import-untyped]
        return _CH, 2
    except ImportError:
        return None, None


def is_enabled() -> bool:
    """是否真正启用并初始化成功 (供业务方做轻量分支)。"""
    return _langfuse_client is not None


async def live_probe(timeout: float = 2.0) -> bool:
    """对 langfuse_host /api/public/health 做一次真实 HTTP 探活。
    SDK 客户端虽在,但宿主进程可能已停;失败返回 False (前端会显示降级)。
    """
    if _langfuse_client is None:
        return False
    host = (config.langfuse_host or "").rstrip("/")
    if not host:
        return False
    try:
        import httpx
        async with httpx.AsyncClient(timeout=timeout) as cli:
            r = await cli.get(f"{host}/api/public/health")
            return r.status_code < 500
    except Exception as e:
        logger.debug(f"[Langfuse] live_probe 失败: {type(e).__name__}: {e}")
        return False


def init_langfuse() -> bool:
    """初始化 Langfuse 客户端。失败直接吞,不抛。

    Returns:
        True 表示已成功连上;False 表示禁用 / 初始化失败 (后续调用全 no-op)。
    """
    global _langfuse_client, _init_attempted
    _init_attempted = True

    if not config.langfuse_enabled:
        logger.info("[Langfuse] 已禁用 (langfuse_enabled=False)")
        return False

    if not config.langfuse_public_key or not config.langfuse_secret_key:
        logger.warning("[Langfuse] PK/SK 未配置,跳过初始化 "
                       "(请在 http://localhost:3000 创建 project 后填到 .env)")
        return False

    # 把配置回灌到 env,让 @observe 装饰器内部 langfuse_context 拿到同一份凭证
    os.environ.setdefault("LANGFUSE_PUBLIC_KEY", config.langfuse_public_key)
    os.environ.setdefault("LANGFUSE_SECRET_KEY", config.langfuse_secret_key)
    os.environ.setdefault("LANGFUSE_HOST", config.langfuse_host)

    global _sdk_major_version

    try:
        from langfuse import Langfuse  # type: ignore[import-untyped]

        # 探测版本 (用 CallbackHandler 所在路径反推)
        _, _sdk_major_version = _import_callback_handler()
        if _sdk_major_version is None:
            # 至少 Langfuse 类本身能 import,默认按 v3 走
            _sdk_major_version = 3
        logger.info(f"[Langfuse] 检测到 SDK major version = v{_sdk_major_version}")

        # 不同版本的构造参数不同 — v3 收敛了一些 kwargs
        common_kwargs: Dict[str, Any] = dict(
            public_key=config.langfuse_public_key,
            secret_key=config.langfuse_secret_key,
            host=config.langfuse_host,
        )
        # 只在 v2 上传 sample_rate / flush_at / flush_interval (v3 改成了
        # OpenTelemetry-based,这些 kwargs 名字变了,直接传会报 unexpected kwarg)
        if _sdk_major_version == 2:
            common_kwargs.update(
                flush_at=config.langfuse_flush_at,
                flush_interval=config.langfuse_flush_interval,
                sample_rate=config.langfuse_sample_rate,
                release=config.app_version,
                environment=config.langfuse_environment,
            )
        try:
            _langfuse_client = Langfuse(**common_kwargs)
        except TypeError as e:
            # 哪怕基础 kwargs 都不被接受,降级到最最最简模式
            logger.warning(f"[Langfuse] 构造失败 (kwargs 不兼容),降级到默认参数: {e}")
            _langfuse_client = Langfuse(
                public_key=config.langfuse_public_key,
                secret_key=config.langfuse_secret_key,
                host=config.langfuse_host,
            )

        # 软化的连通性检查:auth_check 在 SDK/server 版本错配时常会因 schema
        # 不一致抛 pydantic 错,但实际 tracking 仍能工作。只 debug log,不警告。
        try:
            ok = _langfuse_client.auth_check()
            if ok is True:
                logger.info(f"[Langfuse] ✅ auth_check 通过 host={config.langfuse_host} "
                            f"env={config.langfuse_environment}")
            else:
                logger.debug(f"[Langfuse] auth_check 返回 {ok!r} (可能版本错配,不影响发送)")
        except Exception as e:
            logger.debug(f"[Langfuse] auth_check 异常 (常见于 SDK/server 版本错配,"
                         f"trace 发送不受影响): {type(e).__name__}: {e}")

        logger.info(f"[Langfuse] ✅ client 已初始化 sdk=v{_sdk_major_version} "
                    f"host={config.langfuse_host}")
        return True

    except ImportError:
        logger.warning("[Langfuse] langfuse 包未安装,关闭 observability")
        _langfuse_client = None
        return False
    except Exception as e:
        logger.exception(f"[Langfuse] 初始化失败,降级到无 trace 模式: {e}")
        _langfuse_client = None
        return False


def shutdown_langfuse() -> None:
    """关闭时调用,把队列里的 trace flush 出去。"""
    global _langfuse_client
    if _langfuse_client is None:
        return
    try:
        # flush 在 v2/v3 都存在;shutdown 在 v3 有些版本被改名/移除
        if hasattr(_langfuse_client, "flush"):
            _langfuse_client.flush()
        if hasattr(_langfuse_client, "shutdown"):
            _langfuse_client.shutdown()
        logger.info("[Langfuse] flush + shutdown 完成")
    except Exception as e:
        logger.warning(f"[Langfuse] shutdown 异常 (忽略): {e}")
    finally:
        _langfuse_client = None


def get_langfuse() -> Optional[Any]:
    """返回原始 Langfuse 客户端;未启用返回 None。"""
    return _langfuse_client


def get_callback_handler(
    session_id: Optional[str] = None,
    trace_name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
) -> List[Any]:
    """获取一组 LangChain 回调,用于 `Runnable.astream(..., config={"callbacks": ...})`。

    业务用法 (SREService.diagnose):
        from app.services.observability import get_callback_handler
        callbacks = get_callback_handler(
            session_id=session_id,
            trace_name="srewise.diagnose",
            metadata={"alert_name": ..., "service": ...},
        )
        async for ev in graph.astream(state, config={"callbacks": callbacks, ...}):
            ...

    禁用 / 初始化失败时返回 `[]`,LangChain 视为无 callback。
    """
    if _langfuse_client is None:
        return []

    CallbackHandler, ver = _import_callback_handler()
    if CallbackHandler is None:
        logger.warning("[Langfuse] CallbackHandler 不可导入,跳过 callback")
        return []

    try:
        if ver == 2:
            # v2: 构造函数接所有 kwargs
            handler = CallbackHandler(
                public_key=config.langfuse_public_key,
                secret_key=config.langfuse_secret_key,
                host=config.langfuse_host,
                session_id=session_id,
                trace_name=trace_name or "srewise.run",
                metadata=metadata or {},
                user_id=user_id,
                tags=tags or [],
                release=config.app_version,
                environment=config.langfuse_environment,
            )
        else:
            # v3: CallbackHandler 不再接业务 kwargs,只能用单例 client。
            # session_id/tags/metadata 改成调用方在 chain config.metadata 里传:
            #   config={"callbacks": [...], "metadata": {
            #       "langfuse_session_id": session_id,
            #       "langfuse_tags": tags,
            #       "langfuse_user_id": user_id,
            #   }}
            # 见下面 build_runnable_config() 辅助函数。
            try:
                handler = CallbackHandler()
            except Exception as e:
                logger.warning(f"[Langfuse v3] CallbackHandler() 创建失败: {e}")
                return []
        return [handler]
    except Exception as e:
        logger.warning(f"[Langfuse] CallbackHandler 创建失败: {e}")
        return []


def build_runnable_config(
    callbacks: List[Any],
    *,
    session_id: Optional[str] = None,
    trace_name: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    user_id: Optional[str] = None,
    tags: Optional[List[str]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """构造 LangGraph/LangChain `astream(config=...)` 用的 dict。

    自动按 SDK 版本把 session_id / tags / user_id 放到正确位置:
    - v2:CallbackHandler 自己持有这些字段,config.metadata 只放业务 metadata
    - v3:CallbackHandler 不持,业务方要在 config.metadata 里塞 `langfuse_*` 前缀键

    用法 (SREService.diagnose):
        callbacks = get_callback_handler()  # 不传 session_id 给 v3
        config = build_runnable_config(callbacks, session_id=sid, ...)
        async for ev in graph.astream(state, config=config): ...
    """
    cfg: Dict[str, Any] = {**(extra or {})}
    if callbacks:
        cfg["callbacks"] = list(callbacks) + list(cfg.get("callbacks", []))

    # v3 走 metadata 前缀键,v2 走 CallbackHandler 自带字段
    md: Dict[str, Any] = dict(metadata or {})
    if _sdk_major_version == 3:
        if session_id:
            md["langfuse_session_id"] = session_id
        if user_id:
            md["langfuse_user_id"] = user_id
        if tags:
            md["langfuse_tags"] = list(tags)
        if trace_name:
            md["langfuse_trace_name"] = trace_name
    if md:
        cfg["metadata"] = md
    return cfg


def emit_event(
    name: str,
    *,
    session_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    level: str = "DEFAULT",
) -> None:
    """打点一个独立事件 (例如 human_approval_granted, interrupt_raised)。

    本函数不抛异常 (Langfuse 故障不能影响业务)。
    """
    if _langfuse_client is None:
        return
    # v2 用 client.event(),v3 改成 OTel span,API 完全不同
    if _sdk_major_version == 2:
        try:
            _langfuse_client.event(
                name=name,
                metadata=metadata or {},
                level=level,
                session_id=session_id,
            )
        except Exception as e:
            logger.debug(f"[Langfuse v2] emit_event({name}) 失败 (忽略): {e}")
        return

    # v3: 用 start_as_current_span 立刻关闭 → 等价于 event
    try:
        span_kwargs: Dict[str, Any] = {"name": name}
        full_md = {"level": level, **(metadata or {})}
        if session_id:
            full_md["langfuse_session_id"] = session_id
        # v3 client 有 start_as_current_span 或 create_event,这里用通用 span
        if hasattr(_langfuse_client, "start_as_current_span"):
            with _langfuse_client.start_as_current_span(
                name=name,
            ) as span:
                try:
                    span.update(metadata=full_md, level=level)
                except Exception:
                    pass
        elif hasattr(_langfuse_client, "create_event"):
            _langfuse_client.create_event(name=name, metadata=full_md)
    except Exception as e:
        logger.debug(f"[Langfuse v3] emit_event({name}) 失败 (忽略): {e}")


# ============================================================
# `@traced` 装饰器 — 装饰关键服务函数,自动产出 span
# ============================================================
#
# 装饰器在模块**导入时**评估,而 init_langfuse() 是在 lifespan 中执行的。
# 我们在装饰阶段只读取 config.langfuse_enabled 决定要不要包 @observe;
# 配置为 False 时直接返回原函数,零开销零侵入。
#
# 使用方式:
#     from app.services.observability import traced
#
#     @traced(name="incident_kg.find_similar")
#     async def find_similar_incidents(...): ...
# ============================================================

def traced(
    name: Optional[str] = None,
    *,
    as_type: Optional[str] = None,  # "generation" / None (默认 span)
    capture_input: bool = True,
    capture_output: bool = True,
) -> Callable:
    """条件式 @observe 装饰器:仅当 langfuse_enabled 时启用,否则透明降级。"""

    def decorator(func: Callable) -> Callable:
        if not config.langfuse_enabled:
            return func
        observe_fn = _import_observe()
        if observe_fn is None:
            # 只在首次失败时 warn 一次,避免日志爆炸 (这里图省事不做去重)
            return func
        try:
            return observe_fn(
                name=name,
                as_type=as_type,
                capture_input=capture_input,
                capture_output=capture_output,
            )(func)
        except TypeError:
            # v3 的 observe 不一定接 capture_input/capture_output,降级最简调用
            try:
                return observe_fn(name=name)(func)
            except Exception as e:
                logger.warning(f"[Langfuse] @traced({name}) 装饰失败,降级: {e}")
                return func

    return decorator
