"""Grafana MCP Server (mock)

模拟 Grafana 数据源查询接口,提供 PromQL 自由查询 + dashboard 摘要。

设计要点
========
1. **PromQL 路由**: query_promql 通过简单关键字匹配,把常见查询路由到对应的
   mock 数据生成器。Agent 不必学完整 PromQL 语法。
2. **Dashboard 摘要**: query_dashboard 一次性返回某个 dashboard 上多个面板的
   "已聚合"指标,比让 Agent 一个个查 PromQL 高效得多 -- 仍然是"工具层做聚合,
   LLM 拿到结论"的设计原则。
3. **故事一致**: 所有 mock 指标都对齐 K8s server 的故障剧本 (data-sync-service
   v42 OOM,内存飙升、CPU 突增、重启次数攀升)。
"""

import functools
import json
import logging
import random
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from fastmcp import FastMCP

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")
logger = logging.getLogger("Grafana_MCP_Server")
mcp = FastMCP("Grafana")


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
# 数据生成
# ============================================================

def _gen_series(start: datetime, end: datetime, step_minutes: int,
                base: float, peak: float, ramp_at_idx: int = 3) -> List[Dict]:
    """生成"先平稳后飙升"的时间序列,模拟故障期间指标变化。"""
    points = []
    cur = start
    idx = 0
    while cur <= end:
        if idx < ramp_at_idx:
            v = base + idx * 0.5
        else:
            grow = (idx - ramp_at_idx + 1) * (peak - base) / max(1, 6)
            v = min(base + grow, peak)
        v += random.uniform(-1.5, 1.5)
        v = max(0, v)
        points.append({"timestamp": cur.strftime("%Y-%m-%d %H:%M:%S"), "value": round(v, 2)})
        cur += timedelta(minutes=step_minutes)
        idx += 1
    return points


def _parse_time_range(time_range: str) -> tuple[datetime, datetime, int]:
    """解析 '30m'/'1h'/'6h' 形式的相对时间范围,返回 (start, end, step_minutes)。"""
    end = datetime.now()
    minutes = 60
    step = 1
    if time_range.endswith("m"):
        minutes = int(time_range[:-1])
        step = max(1, minutes // 30)
    elif time_range.endswith("h"):
        minutes = int(time_range[:-1]) * 60
        step = max(1, minutes // 30)
    elif time_range.endswith("d"):
        minutes = int(time_range[:-1]) * 60 * 24
        step = max(1, minutes // 60)
    return end - timedelta(minutes=minutes), end, step


# ============================================================
# 工具
# ============================================================

@mcp.tool()
@log_tool_call
def query_promql(expr: str, time_range: str = "1h") -> Dict[str, Any]:
    """执行 PromQL 表达式查询(mock,通过关键字路由)。

    支持的查询模式 (Agent 写表达式时可参考):
    - container_memory_usage_bytes{deployment="..."}    内存用量
    - container_cpu_usage_seconds_total{deployment="..."}  CPU 用量
    - kube_pod_container_status_restarts_total{deployment="..."}  重启次数
    - up{job="..."}                                     服务存活

    Args:
        expr: PromQL 表达式
        time_range: 相对时间范围,格式如 "30m" / "1h" / "6h" / "1d",默认 1h

    Returns:
        {"expr": str, "metric": str, "unit": str, "data_points": [...],
         "summary": {"min": float, "max": float, "avg": float, "p95": float},
         "insight": str}  insight 是工具层算出的文字判断

    risk_level: read
    """
    start, end, step = _parse_time_range(time_range)
    expr_l = expr.lower()
    is_data_sync = "data-sync" in expr_l

    if "memory" in expr_l:
        # 内存使用率:基线 60% → 飙升到 94%
        if is_data_sync:
            points = _gen_series(start, end, step, base=60, peak=94, ramp_at_idx=3)
            metric, unit = "container_memory_usage_percent", "%"
        else:
            points = _gen_series(start, end, step, base=45, peak=55, ramp_at_idx=999)
            metric, unit = "container_memory_usage_percent", "%"
    elif "cpu" in expr_l:
        if is_data_sync:
            points = _gen_series(start, end, step, base=15, peak=88, ramp_at_idx=3)
            metric, unit = "container_cpu_usage_percent", "%"
        else:
            points = _gen_series(start, end, step, base=20, peak=35, ramp_at_idx=999)
            metric, unit = "container_cpu_usage_percent", "%"
    elif "restart" in expr_l:
        # 重启次数累加曲线
        n = max(2, int((end - start).total_seconds() / 60 / step))
        points = []
        cnt = 0
        cur = start
        for i in range(n):
            if i >= 3 and is_data_sync:
                cnt += 1
            points.append({"timestamp": cur.strftime("%Y-%m-%d %H:%M:%S"), "value": cnt})
            cur += timedelta(minutes=step)
        metric, unit = "kube_pod_container_status_restarts_total", "count"
    elif "up{" in expr_l or expr_l.startswith("up"):
        n = max(2, int((end - start).total_seconds() / 60 / step))
        points = []
        cur = start
        for i in range(n):
            v = 0 if (is_data_sync and i >= 3) else 1
            points.append({"timestamp": cur.strftime("%Y-%m-%d %H:%M:%S"), "value": v})
            cur += timedelta(minutes=step)
        metric, unit = "up", "bool"
    else:
        # 未识别的查询,返回平稳数据
        points = _gen_series(start, end, step, base=50, peak=55, ramp_at_idx=999)
        metric, unit = "unknown_metric", ""

    values = [p["value"] for p in points]
    summary = {
        "min": round(min(values), 2) if values else 0,
        "max": round(max(values), 2) if values else 0,
        "avg": round(sum(values) / len(values), 2) if values else 0,
        "p95": round(sorted(values)[int(len(values) * 0.95)], 2) if len(values) > 1 else 0,
    }

    # 工具层算 insight
    insight = "metric is stable"
    if metric == "container_memory_usage_percent" and summary["max"] > 90:
        insight = (f"内存使用率在窗口末尾突破 {summary['max']}%,触及容器上限,"
                   "强烈建议检查近期是否有发布或缓存配置变更。")
    elif metric == "container_cpu_usage_percent" and summary["max"] > 80:
        insight = f"CPU 使用率峰值 {summary['max']}%,可能因频繁重启或 GC 导致。"
    elif metric == "kube_pod_container_status_restarts_total" and summary["max"] >= 3:
        insight = f"窗口内累计重启 {int(summary['max'])} 次,服务处于不稳定状态。"
    elif metric == "up" and summary["min"] == 0:
        insight = "目标在窗口内出现 down 状态,服务不可用。"

    return {"expr": expr, "metric": metric, "unit": unit, "time_range": time_range,
            "data_points": points, "summary": summary, "insight": insight}


@mcp.tool()
@log_tool_call
def query_dashboard(uid: str, time_range: str = "1h") -> Dict[str, Any]:
    """一次性获取 dashboard 上所有面板的指标摘要。

    Args:
        uid: dashboard 唯一标识。当前支持:
            - "data-sync"     数据同步服务全景
            - "api-gateway"   API 网关全景
        time_range: 相对时间范围,默认 "1h"

    Returns:
        {"uid": str, "title": str, "panels": [
           {"title": str, "metric": str, "summary": {...}, "insight": str},
           ...
        ], "overall_insight": str}

    risk_level: read
    """
    if uid == "data-sync":
        title = "Data Sync Service Overview"
        deployment = "data-sync-service"
    elif uid == "api-gateway":
        title = "API Gateway Overview"
        deployment = "api-gateway-service"
    else:
        return {"error": f"unknown dashboard uid: {uid}",
                "available_uids": ["data-sync", "api-gateway"]}

    queries = [
        ("Memory Usage", f'container_memory_usage_bytes{{deployment="{deployment}"}}'),
        ("CPU Usage", f'container_cpu_usage_seconds_total{{deployment="{deployment}"}}'),
        ("Pod Restarts", f'kube_pod_container_status_restarts_total{{deployment="{deployment}"}}'),
        ("Service Up", f'up{{job="{deployment}"}}'),
    ]
    panels = []
    for panel_title, expr in queries:
        result = query_promql(expr=expr, time_range=time_range)
        panels.append({
            "title": panel_title, "expr": expr,
            "metric": result["metric"], "unit": result["unit"],
            "summary": result["summary"], "insight": result["insight"],
        })

    # 工具层综合判断
    abnormal = [p for p in panels if "stable" not in p["insight"]]
    if abnormal:
        overall = (f"dashboard 内 {len(abnormal)}/{len(panels)} 个面板异常: "
                   + "; ".join(p["title"] for p in abnormal))
    else:
        overall = "dashboard 全部面板正常"

    return {"uid": uid, "title": title, "time_range": time_range,
            "panels": panels, "overall_insight": overall}


if __name__ == "__main__":
    mcp.run(transport="streamable-http", host="127.0.0.1", port=8007, path="/mcp")
