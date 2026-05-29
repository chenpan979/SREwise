"""Incident KG 种子数据 (Step 4)。

启动时灌入若干"已发生过的历史故障",让 Historian 的 KG 召回不至于空空如也。
所有种子故障都跟 mock MCP server 的剧本对齐 (data-sync-service / api-gateway-service)。

种子设计原则
============
- **多样性**: 涵盖 4 类根因 (memory_oom, config_change, capacity, dependency_outage),
  让 Remediator 在不同诊断下都能从 KG 拿到不同的 action 模板
- **复用同 args_signature**: 多次 rollback 用相同 deployment + namespace 触发同一
  Action 节点,这样 KG 的 hit_count 才有意义
- **时间散布**: 故障跨越过去 90 天,模拟真实运维历史
- **当前 OOM 故障已在 KG 里出现过 2 次**: 这样 Historian 召回时能给出"反复发作"的强信号
"""

from datetime import datetime, timedelta
from typing import Any, Dict, List

from loguru import logger

from app.services.incident_kg import incident_kg


def _seed_specs() -> List[Dict[str, Any]]:
    """构造一组历史 Incident 规格(从远到近排序)。"""
    now = datetime.now()

    def t(days_ago: int, hours: int = 0) -> str:
        return (now - timedelta(days=days_ago, hours=hours)
                ).strftime("%Y-%m-%d %H:%M:%S")

    return [
        # ============ 90 天前: data-sync 容量不足 ============
        {
            "alert_name": "PodCrashLooping",
            "service": "data-sync-service",
            "namespace": "production",
            "severity": "critical",
            "started_at": t(90),
            "summary": "data-sync-service 连接池耗尽,Pod 持续重启",
            "status": "resolved",
            "root_cause_category": "capacity",
            "root_cause_description": "数据库连接池上限 50,业务高峰打到 80+,导致请求排队超时",
            "symptoms": ["connection pool exhausted",
                         "request timeout",
                         "Pod restart"],
            "actions": [
                {"tool_name": "scale_deployment",
                 "args": {"name": "data-sync-service", "replicas": 6,
                          "namespace": "production"},
                 "success": True},
            ],
            "confidence": 0.85,
        },
        # ============ 60 天前: api-gateway 依赖故障 ============
        {
            "alert_name": "HighErrorRate",
            "service": "api-gateway-service",
            "namespace": "production",
            "severity": "critical",
            "started_at": t(60),
            "summary": "API Gateway 5xx 飙升,根源为下游 user-service 不可用",
            "status": "resolved",
            "root_cause_category": "dependency_outage",
            "root_cause_description": "user-service 数据库主从切换期间不可写,网关重试无效",
            "symptoms": ["5xx error rate", "downstream timeout"],
            "actions": [
                {"tool_name": "restart_deployment",
                 "args": {"name": "api-gateway-service",
                          "namespace": "production"},
                 "success": True},
            ],
            "confidence": 0.7,
        },
        # ============ 42 天前: data-sync 第一次 OOM (我们故事里 history 提及) ============
        {
            "alert_name": "PodCrashLooping",
            "service": "data-sync-service",
            "namespace": "production",
            "severity": "critical",
            "started_at": t(42),
            "summary": "data-sync-service v38 引入大缓存导致 OOMKilled",
            "status": "resolved",
            "root_cause_category": "memory_oom",
            "root_cause_description": "v38 加载全量字典到内存,峰值 580MB 超过 512MB 上限",
            "symptoms": ["OOMKilled", "exit code 137",
                         "container_memory_usage > 90%"],
            "actions": [
                {"tool_name": "scale_deployment",
                 "args": {"name": "data-sync-service", "replicas": 5,
                          "namespace": "production"},
                 "success": True},
                {"tool_name": "rollback_deployment",
                 "args": {"name": "data-sync-service", "namespace": "production"},
                 "success": True},
            ],
            "confidence": 0.9,
        },
        # ============ 30 天前: api-gateway 配置变更 ============
        {
            "alert_name": "HighLatency",
            "service": "api-gateway-service",
            "namespace": "production",
            "severity": "warning",
            "started_at": t(30),
            "summary": "API Gateway 配置变更引入路由错误,P99 延迟翻倍",
            "status": "resolved",
            "root_cause_category": "config_change",
            "root_cause_description": "v15 → v16 路由表 typo 导致部分请求绕远路",
            "symptoms": ["high latency", "P99 spike"],
            "actions": [
                {"tool_name": "rollback_deployment",
                 "args": {"name": "api-gateway-service",
                          "namespace": "production"},
                 "success": True},
            ],
            "confidence": 0.95,
        },
        # ============ 14 天前: data-sync 第二次 OOM (跟当前剧本最相似) ============
        {
            "alert_name": "PodCrashLooping",
            "service": "data-sync-service",
            "namespace": "production",
            "severity": "critical",
            "started_at": t(14),
            "summary": "data-sync-service v40 → v41 内存使用上升 25%,Pod CrashLoop",
            "status": "resolved",
            "root_cause_category": "memory_oom",
            "root_cause_description": "v41 新增缓存层,在 512MB 上限下高峰期触发 OOM",
            "symptoms": ["OOMKilled", "exit code 137",
                         "container_memory_usage > 94%"],
            "actions": [
                {"tool_name": "rollback_deployment",
                 "args": {"name": "data-sync-service", "namespace": "production"},
                 "success": True},
            ],
            "confidence": 0.92,
        },
        # ============ 7 天前: data-sync 容量预警 (warning 级别,不是事故但留痕) ============
        {
            "alert_name": "HighMemoryUsage",
            "service": "data-sync-service",
            "namespace": "production",
            "severity": "warning",
            "started_at": t(7),
            "summary": "data-sync-service 持续接近 80% 内存限,提前扩副本以缓冲",
            "status": "resolved",
            "root_cause_category": "capacity",
            "root_cause_description": "业务量上升,内存上限不变,水位线长期偏高",
            "symptoms": ["high memory usage"],
            "actions": [
                {"tool_name": "scale_deployment",
                 "args": {"name": "data-sync-service", "replicas": 4,
                          "namespace": "production"},
                 "success": True},
            ],
            "confidence": 0.6,
        },
    ]


async def seed_if_empty() -> Dict[str, Any]:
    """启动时调用:若 KG 为空则灌入种子。返回操作摘要。"""
    if not incident_kg.ready:
        return {"seeded": False, "reason": "KG not ready"}
    stats = await incident_kg.stats()
    if (stats.get("nodes_by_kind", {}).get("Incident", 0) or 0) > 0:
        incident_kg.mark_seeded()
        logger.info(f"[KG] 已存在 {stats['nodes_by_kind']['Incident']} 条 Incident,"
                    f"跳过种子写入")
        return {"seeded": False, "reason": "already populated", **stats}

    logger.info("[KG] 首次启动,写入历史故障种子 ...")
    written = 0
    for spec in _seed_specs():
        try:
            await incident_kg.upsert_incident(**spec)
            written += 1
        except Exception as e:
            logger.error(f"[KG] 种子写入失败 {spec.get('alert_name')}: {e}")
    incident_kg.mark_seeded()
    logger.info(f"[KG] 种子写入完成: {written}/{len(_seed_specs())} 条 Incident")
    return {"seeded": True, "written": written}
