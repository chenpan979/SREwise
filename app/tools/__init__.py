"""工具模块 - 供 Agent 调用的各种工具"""

from app.tools.knowledge_tool import retrieve_knowledge
from app.tools.query_metrics_alerts import query_prometheus_alerts
from app.tools.time_tool import get_current_time
from app.tools.sre_remediation_tools import DEFAULT_LOCAL_REMEDIATION_TOOLS

# 默认本地工具集：凡绑定「知识库 + 时间」的 Agent 应使用此元组，与 Prometheus 告警查询一并注册
DEFAULT_LOCAL_AGENT_TOOLS = (
    retrieve_knowledge, # 知识检索工具
    get_current_time, # 时间工具
    query_prometheus_alerts, # # 查询 Prometheus 告警工具
)

__all__ = [
    "DEFAULT_LOCAL_AGENT_TOOLS",
    "DEFAULT_LOCAL_REMEDIATION_TOOLS",
    "retrieve_knowledge",
    "get_current_time",
    "query_prometheus_alerts",
]
