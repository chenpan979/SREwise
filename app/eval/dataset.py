"""Eval 数据集加载。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


DEFAULT_SCENARIOS = Path(__file__).parent / "scenarios.json"


@dataclass
class Expected:
    """单个 case 的期望值与验收准则。"""
    root_cause_categories: List[str] = field(default_factory=list)
    must_include_any_tool: List[str] = field(default_factory=list)
    forbidden_tools: List[str] = field(default_factory=list)
    forbidden_risk_levels: List[str] = field(default_factory=list)
    min_confidence: float = 0.0
    must_have_report: bool = True
    approval_policy: str = "approve_all_non_destructive"
    # 该 policy 下,执行环节应为 0(用于测试安全门是否真的把所有动作拦住)
    expect_zero_executions: bool = False


@dataclass
class Scenario:
    id: str
    description: str
    input: Dict[str, Any]
    expected: Expected

    @property
    def alert(self) -> Optional[Dict[str, Any]]:
        return self.input.get("alert")

    @property
    def query(self) -> Optional[str]:
        return self.input.get("query")


def load_scenarios(path: Optional[Path] = None) -> List[Scenario]:
    """从 JSON 文件加载场景列表。"""
    p = Path(path) if path else DEFAULT_SCENARIOS
    raw = json.loads(p.read_text(encoding="utf-8"))
    out: List[Scenario] = []
    for item in raw:
        exp = Expected(**(item.get("expected") or {}))
        out.append(Scenario(
            id=item["id"],
            description=item.get("description", ""),
            input=item["input"],
            expected=exp,
        ))
    return out
