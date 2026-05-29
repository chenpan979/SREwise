"""SREwise 本地 mock 修复工具集.

设计目标
========
当外部 MCP 写服务 (k8s / cls / monitor / alertmanager / grafana) 不可达时,
让 Remediator 仍然有 ``write`` / ``destructive`` 工具可调,使 ``HITL → Executor`` 整条链路
可以在本地 demo 与 eval 中端到端跑通。

约定
====
- 每个工具 docstring **末尾必须**包含 ``risk_level: write`` 或 ``risk_level: destructive``,
  ``tool_filter.extract_risk_level`` 会从中解析出风险等级。
- 工具仅做日志 + 返回结构化 JSON,**不会真的访问集群**,
  因此安全门 (forbidden_tools / risk_level) 可以被 Eval 真实验证。
- 工具名故意与生产 SRE 习惯保持一致 (``scale_deployment`` / ``rollback_deployment`` ...),
  这样后续接入真实 MCP 时,Remediator prompt 与历史模板可零成本切换。
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from langchain_core.tools import tool
from loguru import logger


def _mock_result(action: str, **fields: Any) -> str:
    """统一返回结构化结果(JSON 字符串),便于 Executor 解析。"""
    payload: Dict[str, Any] = {
        "ok": True,
        "action": action,
        "simulated": True,
        "note": "本地 mock 工具:未真实修改集群,仅供 demo / eval.",
        **fields,
    }
    return json.dumps(payload, ensure_ascii=False)


# ============================================================
# write 等级 (可逆 / 低风险)
# ============================================================

@tool
def rollback_deployment(
    name: str,
    namespace: str = "production",
    to_revision: Optional[int] = None,
) -> str:
    """回滚指定 Deployment 到上一个稳定版本 (或指定 revision).

    适用于近期变更导致的故障(配置/镜像/资源变化引发 OOM/CrashLoop 等).

    Args:
        name: Deployment 名称, 如 ``data-sync-service``.
        namespace: K8s 命名空间, 默认 ``production``.
        to_revision: 可选, 指定回滚目标 revision; 不填则回滚到上一版.

    Returns:
        JSON 字符串, 含 ``rolled_back_from`` / ``rolled_back_to``.

    risk_level: write
    """
    logger.info(f"[mock] rollback_deployment {namespace}/{name} → rev={to_revision or 'previous'}")
    return _mock_result(
        "rollback_deployment",
        namespace=namespace, name=name,
        rolled_back_from="current", rolled_back_to=to_revision or "previous",
    )


@tool
def scale_deployment(
    name: str,
    replicas: int,
    namespace: str = "production",
) -> str:
    """水平扩容/缩容 Deployment 副本数.

    适合短时承载流量上涨, 或临时分担单副本压力使热点 Pod 不被 OOMKilled.

    Args:
        name: Deployment 名称.
        replicas: 目标副本数 (>=0).
        namespace: K8s 命名空间, 默认 ``production``.

    risk_level: write
    """
    logger.info(f"[mock] scale_deployment {namespace}/{name} → replicas={replicas}")
    return _mock_result(
        "scale_deployment",
        namespace=namespace, name=name, replicas=replicas,
    )


@tool
def update_deployment_resources(
    name: str,
    container: str,
    namespace: str = "production",
    memory_limit: Optional[str] = None,
    cpu_limit: Optional[str] = None,
    memory_request: Optional[str] = None,
    cpu_request: Optional[str] = None,
) -> str:
    """更新 Deployment 中指定容器的 resources.limits / requests.

    OOMKilled 的标准长期解法之一: 提升 memory.limit. 也可同时调 request 避免节点压力.

    Args:
        name: Deployment 名称.
        container: 容器名 (Pod 内逻辑容器).
        namespace: K8s 命名空间, 默认 ``production``.
        memory_limit: 如 ``"2Gi"``, 可选.
        cpu_limit:    如 ``"1500m"``, 可选.
        memory_request: 如 ``"512Mi"``, 可选.
        cpu_request:    如 ``"500m"``, 可选.

    risk_level: write
    """
    logger.info(
        f"[mock] update_deployment_resources {namespace}/{name}/{container} "
        f"mem={memory_request}/{memory_limit} cpu={cpu_request}/{cpu_limit}"
    )
    return _mock_result(
        "update_deployment_resources",
        namespace=namespace, name=name, container=container,
        memory_limit=memory_limit, cpu_limit=cpu_limit,
        memory_request=memory_request, cpu_request=cpu_request,
    )


@tool
def restart_pod(namespace: str, pod_name: str) -> str:
    """重启 (delete + 由 controller 重建) 指定 Pod.

    对 Pod 自身状态卡死 (僵尸进程 / GPU 句柄泄漏) 有效, **不能** 解决资源不足类问题.

    Args:
        namespace: K8s 命名空间.
        pod_name: Pod 名称.

    risk_level: write
    """
    logger.info(f"[mock] restart_pod {namespace}/{pod_name}")
    return _mock_result("restart_pod", namespace=namespace, pod_name=pod_name)


@tool
def restart_service(service_name: str, environment: str = "production") -> str:
    """重启服务进程 (非容器场景, 或通过 systemd / supervisor 管理的服务).

    Args:
        service_name: 服务名.
        environment: 环境名, 默认 ``production``.

    risk_level: write
    """
    logger.info(f"[mock] restart_service {environment}/{service_name}")
    return _mock_result("restart_service", service_name=service_name, environment=environment)


@tool
def silence_alert(
    alertname: str,
    duration: str = "1h",
    comment: str = "auto-silence by SREwise",
) -> str:
    """在 Alertmanager 创建临时静默规则, 避免告警风暴干扰处置.

    仅静默 **指定 alertname**, 不影响其他告警. 默认 1 小时.

    Args:
        alertname: 告警名称, 如 ``PodCrashLooping``.
        duration: 静默时长 (Alertmanager 语法), 如 ``"30m"`` / ``"2h"``.
        comment: 备注, 便于事后审计.

    risk_level: write
    """
    logger.info(f"[mock] silence_alert {alertname} for {duration}")
    return _mock_result(
        "silence_alert", alertname=alertname, duration=duration, comment=comment,
    )


# ============================================================
# destructive 等级 (不可逆 / 高风险, 默认应被前端预设为不勾选)
# ============================================================

@tool
def delete_pod(namespace: str, pod_name: str, force: bool = False) -> str:
    """强制删除 Pod (与 restart_pod 区别在于支持 ``--force --grace-period=0``).

    Pod 可能携带数据卷状态, 强制删除会丢失尚未持久化的数据.
    **必须** 经人工审批且选择性使用.

    Args:
        namespace: K8s 命名空间.
        pod_name: Pod 名称.
        force: 是否强制 (``true`` → grace_period=0).

    risk_level: destructive
    """
    logger.info(f"[mock] delete_pod {namespace}/{pod_name} force={force}")
    return _mock_result(
        "delete_pod", namespace=namespace, pod_name=pod_name, force=force,
    )


@tool
def cordon_node(node_name: str) -> str:
    """标记 Node 为不可调度 (Cordon), 阻止新 Pod 调度过去.

    通常配合 ``drain_node`` 使用, 用于隔离怀疑硬件故障的节点.

    Args:
        node_name: 节点名.

    risk_level: destructive
    """
    logger.info(f"[mock] cordon_node {node_name}")
    return _mock_result("cordon_node", node_name=node_name)


@tool
def drain_node(node_name: str, ignore_daemonsets: bool = True) -> str:
    """驱逐 Node 上的 Pod (会触发 Pod 在其他节点重建), 用于节点下线/维护.

    高风险: 大批 Pod 重启会引起服务抖动, **必须** 人工审批且与 cordon_node 联动.

    Args:
        node_name: 节点名.
        ignore_daemonsets: 是否忽略 DaemonSet Pod (生产建议 ``true``).

    risk_level: destructive
    """
    logger.info(f"[mock] drain_node {node_name}")
    return _mock_result(
        "drain_node", node_name=node_name, ignore_daemonsets=ignore_daemonsets,
    )


# ============================================================
# 导出元组,供 Remediator 在 MCP 不可达时 fallback 使用
# ============================================================

DEFAULT_LOCAL_REMEDIATION_TOOLS = (
    # write
    rollback_deployment,
    scale_deployment,
    update_deployment_resources,
    restart_pod,
    restart_service,
    silence_alert,
    # destructive
    delete_pod,
    cordon_node,
    drain_node,
)


__all__ = [
    "DEFAULT_LOCAL_REMEDIATION_TOOLS",
    "rollback_deployment",
    "scale_deployment",
    "update_deployment_resources",
    "restart_pod",
    "restart_service",
    "silence_alert",
    "delete_pod",
    "cordon_node",
    "drain_node",
]
