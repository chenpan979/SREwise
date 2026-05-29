"""K8s MCP Server (mock)

模拟 Kubernetes API 的 MCP 服务,提供 Pod / Deployment 的查询与运维操作。

设计要点
========
1. **风险分级 (risk_level)**: 每个工具 docstring 末尾固定一行
   `risk_level: read | write | destructive`,供 Remediator + HITL 审批层使用。
2. **故事一致 mock**: 模块级 `_CLUSTER_STATE` 保存集群状态,destructive 工具
   会修改它,后续 read 工具看到变化。剧本: data-sync-service v42 OOM,
   回滚 v41 后恢复。
3. **可重置**: `reset_cluster_state` 工具供 Eval 框架重放前调用。
"""

import functools
import json
import logging
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("K8s_MCP_Server")

mcp = FastMCP("K8s")


def log_tool_call(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        logger.info(f"[{func.__name__}] args={json.dumps(kwargs, ensure_ascii=False, default=str)}")
        try:
            return func(*args, **kwargs)
        except Exception as e:
            logger.error(f"[{func.__name__}] ERROR: {e}")
            raise
    return wrapper


# ============================================================
# Mock 集群状态 (会被 destructive 工具修改)
# ============================================================

def _build_initial_state() -> Dict[str, Any]:
    now = datetime.now()
    incident = now - timedelta(minutes=18)
    incident_str = incident.strftime("%Y-%m-%d %H:%M:%S")
    return {
        "deployments": {
            "production/data-sync-service": {
                "namespace": "production",
                "name": "data-sync-service",
                "current_revision": 42,
                "available_revisions": [41, 42],
                "replicas_desired": 3,
                "replicas_available": 0,
                "replicas_ready": 0,
                "image": "registry.example.com/data-sync:v42",
                "status": "Progressing",
                "reason": "MinimumReplicasUnavailable",
                "last_update_time": incident_str,
                "history": [
                    {"revision": 41, "image": "registry.example.com/data-sync:v41",
                     "deployed_at": (now - timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S"),
                     "note": "stable"},
                    {"revision": 42, "image": "registry.example.com/data-sync:v42",
                     "deployed_at": incident_str,
                     "note": "introduced larger in-memory cache, memory limit not bumped"},
                ],
            },
            "production/api-gateway-service": {
                "namespace": "production", "name": "api-gateway-service",
                "current_revision": 17, "available_revisions": [16, 17],
                "replicas_desired": 2, "replicas_available": 2, "replicas_ready": 2,
                "image": "registry.example.com/api-gateway:v17",
                "status": "Available", "reason": "",
                "last_update_time": (now - timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S"),
                "history": [],
            },
        },
        "pods": {
            "production/data-sync-service-7b9c8d-x4k2p": _make_oom_pod(
                "data-sync-service-7b9c8d-x4k2p", incident_str, restart=7),
            "production/data-sync-service-7b9c8d-z9m4n": _make_oom_pod(
                "data-sync-service-7b9c8d-z9m4n", incident_str, restart=6),
            "production/api-gateway-service-5f8b9c-q2w3e": {
                "namespace": "production", "name": "api-gateway-service-5f8b9c-q2w3e",
                "deployment": "api-gateway-service", "phase": "Running", "status": "Running",
                "restart_count": 0, "last_state": {},
                "image": "registry.example.com/api-gateway:v17", "node": "node-prod-1",
                "memory_limit_mb": 1024,
            },
        },
    }


def _make_oom_pod(name: str, created: str, restart: int) -> Dict[str, Any]:
    return {
        "namespace": "production", "name": name, "deployment": "data-sync-service",
        "phase": "Running", "status": "CrashLoopBackOff", "restart_count": restart,
        "last_state": {
            "terminated": {"reason": "OOMKilled", "exit_code": 137,
                           "finished_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
        },
        "image": "registry.example.com/data-sync:v42",
        "node": "node-prod-2", "memory_limit_mb": 512, "created_at": created,
    }


_CLUSTER_STATE: Dict[str, Any] = _build_initial_state()


def _key(namespace: str, name: str) -> str:
    return f"{namespace}/{name}"


# ============================================================
# 只读工具 (risk_level: read)
# ============================================================

@mcp.tool()
@log_tool_call
def list_pods(namespace: str = "production",
              deployment: Optional[str] = None) -> Dict[str, Any]:
    """列出指定 namespace 下的所有 Pod,可按 deployment 过滤。

    Args:
        namespace: 命名空间,默认 production
        deployment: 仅返回属于该 deployment 的 pod (可选)

    Returns:
        {"total": int, "pods": [{name, status, restart_count, image, node}, ...]}

    risk_level: read
    """
    pods = []
    for k, p in _CLUSTER_STATE["pods"].items():
        if p["namespace"] != namespace:
            continue
        if deployment and p.get("deployment") != deployment:
            continue
        pods.append({
            "name": p["name"], "status": p["status"],
            "restart_count": p["restart_count"], "image": p["image"],
            "node": p["node"], "deployment": p.get("deployment"),
        })
    return {"namespace": namespace, "deployment_filter": deployment,
            "total": len(pods), "pods": pods}


@mcp.tool()
@log_tool_call
def describe_pod(name: str, namespace: str = "production") -> Dict[str, Any]:
    """查看 Pod 的详细信息(状态、上次终止原因、镜像、节点等)。

    Args:
        name: Pod 名称(全名)
        namespace: 命名空间,默认 production

    Returns:
        Pod 完整状态字典;不存在时返回 {"error": ...}

    risk_level: read
    """
    pod = _CLUSTER_STATE["pods"].get(_key(namespace, name))
    if not pod:
        return {"error": f"pod {namespace}/{name} not found"}
    return deepcopy(pod)


@mcp.tool()
@log_tool_call
def get_pod_logs(name: str, namespace: str = "production",
                 tail: int = 50) -> Dict[str, Any]:
    """获取 Pod 最近的容器日志(tail N 行)。

    Args:
        name: Pod 名称
        namespace: 命名空间,默认 production
        tail: 返回最近 N 行,默认 50

    Returns:
        {"pod": str, "lines": [str, ...], "truncated": bool}

    risk_level: read
    """
    pod = _CLUSTER_STATE["pods"].get(_key(namespace, name))
    if not pod:
        return {"error": f"pod {namespace}/{name} not found"}

    # 根据 pod 状态生成对应日志
    lines: List[str] = []
    now = datetime.now()
    if pod["status"] == "CrashLoopBackOff" and pod["last_state"].get("terminated", {}).get("reason") == "OOMKilled":
        for i in range(min(tail, 30)):
            t = (now - timedelta(seconds=30 - i)).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"{t} INFO  loading large in-memory cache batch {i + 1}/30")
        lines.extend([
            f"{now.strftime('%Y-%m-%d %H:%M:%S')} WARN  memory usage 480MB / 512MB",
            f"{now.strftime('%Y-%m-%d %H:%M:%S')} ERROR java.lang.OutOfMemoryError: Java heap space",
            f"{now.strftime('%Y-%m-%d %H:%M:%S')} ERROR   at com.example.sync.CacheLoader.load(CacheLoader.java:142)",
            f"{now.strftime('%Y-%m-%d %H:%M:%S')} FATAL container terminated by OOMKiller (exit 137)",
        ])
    else:
        for i in range(min(tail, 10)):
            t = (now - timedelta(seconds=10 - i)).strftime("%Y-%m-%d %H:%M:%S")
            lines.append(f"{t} INFO  request handled successfully")

    return {"pod": name, "namespace": namespace,
            "lines": lines[-tail:], "truncated": len(lines) > tail}


@mcp.tool()
@log_tool_call
def describe_deployment(name: str, namespace: str = "production") -> Dict[str, Any]:
    """查看 Deployment 详情(当前版本、副本状态、最近发布历史)。

    Args:
        name: Deployment 名称
        namespace: 命名空间,默认 production

    Returns:
        Deployment 详细字典,包含 history 字段记录最近发布

    risk_level: read
    """
    dep = _CLUSTER_STATE["deployments"].get(_key(namespace, name))
    if not dep:
        return {"error": f"deployment {namespace}/{name} not found"}
    return deepcopy(dep)


# ============================================================
# 写工具 (risk_level: write / destructive)
# ============================================================

@mcp.tool()
@log_tool_call
def restart_deployment(name: str, namespace: str = "production") -> Dict[str, Any]:
    """重启 Deployment 下所有 Pod (kubectl rollout restart)。⚠️ 高危操作。

    Args:
        name: Deployment 名称
        namespace: 命名空间,默认 production

    Returns:
        {"success": bool, "message": str, "restarted_pods": [...]}

    risk_level: destructive
    """
    dep = _CLUSTER_STATE["deployments"].get(_key(namespace, name))
    if not dep:
        return {"success": False, "error": f"deployment {namespace}/{name} not found"}

    restarted = []
    for pk, p in _CLUSTER_STATE["pods"].items():
        if p["namespace"] == namespace and p.get("deployment") == name:
            p["restart_count"] += 1
            p["created_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            restarted.append(p["name"])
            # 重启了但镜像还是 v42, 仍然会 OOM (诊断 Agent 不应该选这个动作)
    return {"success": True, "message": f"deployment/{name} 重启完成 (image 未变,问题可能仍存在)",
            "restarted_pods": restarted}


@mcp.tool()
@log_tool_call
def scale_deployment(name: str, replicas: int,
                     namespace: str = "production") -> Dict[str, Any]:
    """调整 Deployment 副本数 (kubectl scale)。⚠️ 高危操作。

    Args:
        name: Deployment 名称
        replicas: 目标副本数 (0~10)
        namespace: 命名空间,默认 production

    Returns:
        {"success": bool, "old_replicas": int, "new_replicas": int}

    risk_level: destructive
    """
    if not 0 <= replicas <= 10:
        return {"success": False, "error": "replicas must be in [0, 10]"}
    dep = _CLUSTER_STATE["deployments"].get(_key(namespace, name))
    if not dep:
        return {"success": False, "error": f"deployment {namespace}/{name} not found"}

    old = dep["replicas_desired"]
    dep["replicas_desired"] = replicas
    return {"success": True, "deployment": name,
            "old_replicas": old, "new_replicas": replicas,
            "message": f"deployment/{name} scaled {old} -> {replicas}"}


@mcp.tool()
@log_tool_call
def rollback_deployment(name: str, to_revision: Optional[int] = None,
                        namespace: str = "production") -> Dict[str, Any]:
    """回滚 Deployment 到上一个或指定版本 (kubectl rollout undo)。⚠️ 高危操作。

    Args:
        name: Deployment 名称
        to_revision: 目标版本号,不传则回滚到上一个可用版本
        namespace: 命名空间,默认 production

    Returns:
        {"success": bool, "from_revision": int, "to_revision": int, "message": str}

    risk_level: destructive
    """
    dep = _CLUSTER_STATE["deployments"].get(_key(namespace, name))
    if not dep:
        return {"success": False, "error": f"deployment {namespace}/{name} not found"}

    available = dep["available_revisions"]
    current = dep["current_revision"]
    if to_revision is None:
        # 回滚到上一个可用版本
        candidates = [r for r in available if r != current]
        if not candidates:
            return {"success": False, "error": "no previous revision available"}
        to_revision = max(candidates)
    if to_revision not in available:
        return {"success": False,
                "error": f"revision {to_revision} not in available {available}"}

    # 触发恢复:更新镜像、清理 OOM 状态
    target_history = next((h for h in dep["history"] if h["revision"] == to_revision), None)
    new_image = target_history["image"] if target_history else dep["image"].replace(
        f"v{current}", f"v{to_revision}")

    dep["current_revision"] = to_revision
    dep["image"] = new_image
    dep["status"] = "Available"
    dep["reason"] = ""
    dep["replicas_available"] = dep["replicas_desired"]
    dep["replicas_ready"] = dep["replicas_desired"]
    dep["last_update_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 修复对应 Pod
    for p in _CLUSTER_STATE["pods"].values():
        if p["namespace"] == namespace and p.get("deployment") == name:
            p["status"] = "Running"
            p["last_state"] = {}
            p["image"] = new_image
            p["restart_count"] = 0

    return {"success": True, "deployment": name,
            "from_revision": current, "to_revision": to_revision,
            "new_image": new_image,
            "message": f"已回滚到 revision {to_revision},Pod 状态恢复为 Running"}


# ============================================================
# Eval / 调试辅助
# ============================================================

@mcp.tool()
@log_tool_call
def reset_cluster_state() -> Dict[str, Any]:
    """重置集群 mock 状态到初始故障剧本(供 Eval 重放使用)。

    risk_level: write
    """
    global _CLUSTER_STATE
    _CLUSTER_STATE = _build_initial_state()
    return {"success": True, "message": "cluster state reset to initial incident scenario"}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8005, path="/mcp")
