"""按 risk_level 过滤 MCP 工具集。

约定: 工具的 risk_level 写在 docstring 末尾,格式 `risk_level: read|write|destructive`。
LangChain MCP adapter 会把 docstring 完整保留为 tool.description。

本模块提供:
- extract_risk_level(tool) -> str   从 description 中解析风险等级
- partition_tools_by_risk(tools)    按风险等级分组
- filter_read_tools(tools)          只保留 read 工具(给 Diagnostician)
- filter_write_tools(tools)         只保留 write/destructive(给 Remediator)
"""

import re
from typing import Any, Dict, List, Tuple

_RISK_RE = re.compile(r"risk_level\s*:\s*(read|write|destructive)", re.IGNORECASE)


def extract_risk_level(tool: Any, default: str = "read") -> str:
    """从工具的 description 中解析 risk_level。

    Args:
        tool: 任何带 description 字段的对象 (LangChain BaseTool / dict)
        default: 未声明时的默认值,出于安全考虑默认为 "read"
                 (拿不准的工具不给 Remediator 用,避免误执行)

    Returns:
        "read" | "write" | "destructive"
    """
    desc = getattr(tool, "description", "") or ""
    if not desc and isinstance(tool, dict):
        desc = tool.get("description", "")
    m = _RISK_RE.search(desc)
    if m:
        return m.group(1).lower()
    return default


def partition_tools_by_risk(tools: List[Any]) -> Dict[str, List[Any]]:
    """按 risk_level 把工具分到三组,返回 {"read": [...], "write": [...], "destructive": [...]}。"""
    buckets: Dict[str, List[Any]] = {"read": [], "write": [], "destructive": []}
    for t in tools:
        level = extract_risk_level(t)
        buckets.setdefault(level, []).append(t)
    return buckets


def filter_read_tools(tools: List[Any]) -> List[Any]:
    """诊断阶段用:只保留只读工具,杜绝诊断时误改集群状态。"""
    return [t for t in tools if extract_risk_level(t) == "read"]


def filter_write_tools(tools: List[Any]) -> List[Any]:
    """修复阶段用:只保留 write/destructive 工具。"""
    return [t for t in tools if extract_risk_level(t) in ("write", "destructive")]


def annotate_tool_with_risk(tool: Any) -> Tuple[Any, str]:
    """返回 (tool, risk_level) 二元组,供 prompt 渲染使用。"""
    return tool, extract_risk_level(tool)
