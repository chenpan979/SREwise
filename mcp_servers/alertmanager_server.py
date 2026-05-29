"""Alertmanager MCP Server (mock)

模拟 Prometheus Alertmanager 的告警查询与静默接口。

设计要点
========
1. **故事入口**: list_active_alerts 返回的 mock 告警是整个故障剧本的触发点。
2. **可静默**: silence_alert 是 write 级动作。
3. **历史告警**: 配合 Historian 召回,辅助判断是否反复发作。
"""

import functools
import json
import logging
from datetime import datetime, timedelta
from typing import Any, Dict, Optional
from uuid import uuid4

from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("Alertmanager_MCP_Server")
mcp = FastMCP("Alertmanager")


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


_SILENCES: Dict[str, Dict[str, Any]] = {}


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _build_active_alerts():
    """与 K8s server 故障剧本对齐: data-sync-service v42 OOM。"""
    start = datetime.now() - timedelta(minutes=18)
    return [
        {
            "id": "alert-001", "name": "PodCrashLooping", "severity": "critical",
            "service": "data-sync-service", "namespace": "production",
            "started_at": start.strftime("%Y-%m-%d %H:%M:%S"),
            "summary": "Pod data-sync-service-* 反复重启 (>5 次/15 分钟)",
            "description": "production/data-sync-service 下 2 个 Pod 在过去 18 分钟内进入 CrashLoopBackOff,最近一次 exit 137 (OOMKilled)。",
            "labels": {"alertname": "PodCrashLooping", "severity": "critical",
                       "namespace": "production", "deployment": "data-sync-service"},
            "annotations": {"runbook_url": "internal://runbooks/pod_crash_loop"},
            "value": "7 restarts in 15m",
        },
        {
            "id": "alert-002", "name": "HighMemoryUsage", "severity": "warning",
            "service": "data-sync-service", "namespace": "production",
            "started_at": (start + timedelta(minutes=2)).strftime("%Y-%m-%d %H:%M:%S"),
            "summary": "data-sync-service 内存使用率超阈值",
            "description": "container_memory_usage_bytes 持续 > 90% memory_limit",
            "labels": {"alertname": "HighMemoryUsage", "severity": "warning",
                       "namespace": "production", "deployment": "data-sync-service"},
            "annotations": {}, "value": "94%",
        },
    ]


def _build_history(service: str):
    if service != "data-sync-service":
        return []
    base = datetime.now()
    return [
        {"id": "alert-h001", "name": "PodCrashLooping", "severity": "critical",
         "service": service,
         "started_at": (base - timedelta(days=14)).strftime("%Y-%m-%d %H:%M:%S"),
         "ended_at": (base - timedelta(days=14) + timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S"),
         "resolved_by": "rollback_deployment to v40", "duration_minutes": 25},
        {"id": "alert-h002", "name": "PodCrashLooping", "severity": "critical",
         "service": service,
         "started_at": (base - timedelta(days=42)).strftime("%Y-%m-%d %H:%M:%S"),
         "ended_at": (base - timedelta(days=42) + timedelta(minutes=40)).strftime("%Y-%m-%d %H:%M:%S"),
         "resolved_by": "scale_deployment + memory limit bump", "duration_minutes": 40},
    ]


@mcp.tool()
@log_tool_call
def list_active_alerts(severity: Optional[str] = None,
                       service: Optional[str] = None) -> Dict[str, Any]:
    """列出当前所有 firing 状态、未被静默的告警。

    Args:
        severity: 严重等级过滤 (critical/warning/info),可选
        service: 服务名过滤,可选

    Returns:
        {"total": int, "alerts": [...]}

    risk_level: read
    """
    alerts = _build_active_alerts()
    silenced = {s["alert_id"] for s in _SILENCES.values()
                if datetime.fromisoformat(s["expires_at"]) > datetime.now()}
    alerts = [a for a in alerts if a["id"] not in silenced]
    if severity:
        alerts = [a for a in alerts if a["severity"] == severity]
    if service:
        alerts = [a for a in alerts if a["service"] == service]
    return {"total": len(alerts), "alerts": alerts, "fetched_at": _now_str()}


@mcp.tool()
@log_tool_call
def get_alert_history(service: str, days: int = 30) -> Dict[str, Any]:
    """查询某服务过去 N 天的告警历史,用于判断是否反复发作。

    Args:
        service: 服务名(必填)
        days: 回溯天数,默认 30

    Returns:
        {"service": str, "days": int, "total": int, "alerts": [...],
         "recurrence_pattern": str}  recurrence_pattern 是简单的反复发作描述

    risk_level: read
    """
    history = _build_history(service)
    cutoff = datetime.now() - timedelta(days=days)
    history = [h for h in history
               if datetime.strptime(h["started_at"], "%Y-%m-%d %H:%M:%S") >= cutoff]

    pattern = "no recurrence detected"
    crash_count = sum(1 for h in history if h["name"] == "PodCrashLooping")
    if crash_count >= 2:
        pattern = (f"PodCrashLooping 在过去 {days} 天内反复发作 {crash_count} 次,"
                   "历史均通过 deployment 回滚或扩容解决,本次告警很可能是同类问题。")
    return {"service": service, "days": days, "total": len(history),
            "alerts": history, "recurrence_pattern": pattern}


@mcp.tool()
@log_tool_call
def silence_alert(alert_id: str, duration_minutes: int = 60,
                  comment: str = "") -> Dict[str, Any]:
    """静默某条告警一段时间。⚠️ 用于"已知问题先压住告警"场景。

    Args:
        alert_id: 告警 ID
        duration_minutes: 静默时长(分钟),默认 60
        comment: 备注

    Returns:
        {"success": bool, "silence_id": str, "expires_at": str}

    risk_level: write
    """
    if duration_minutes <= 0 or duration_minutes > 24 * 60:
        return {"success": False, "error": "duration_minutes must be in (0, 1440]"}
    silence_id = f"silence-{uuid4().hex[:8]}"
    expires = datetime.now() + timedelta(minutes=duration_minutes)
    _SILENCES[silence_id] = {
        "alert_id": alert_id, "comment": comment,
        "created_at": _now_str(),
        "expires_at": expires.isoformat(),
    }
    return {"success": True, "silence_id": silence_id,
            "alert_id": alert_id, "expires_at": expires.strftime("%Y-%m-%d %H:%M:%S"),
            "message": f"alert {alert_id} silenced for {duration_minutes} minutes"}


@mcp.tool()
@log_tool_call
def reset_alertmanager_state() -> Dict[str, Any]:
    """清空所有静默,供 Eval 重放使用。

    risk_level: write
    """
    _SILENCES.clear()
    return {"success": True, "message": "alertmanager state reset"}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8006, path="/mcp")
