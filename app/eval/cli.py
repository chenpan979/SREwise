"""Eval CLI 入口 — `python -m app.eval` 或 `python -m app.eval.cli`。

用法
====
    # 跑全部场景
    python -m app.eval

    # 只跑某些 case
    python -m app.eval --case oom_canonical_v1 --case destructive_safety_gate

    # 写报告到指定路径
    python -m app.eval --out eval_results/run1.md
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from loguru import logger

from .dataset import Scenario, load_scenarios
from .runner import run_case
from .scorer import AggregateReport, CaseScore, aggregate, score_case


async def run_all(
    scenarios: List[Scenario],
    *,
    out_dir: Path,
    json_path: Optional[Path] = None,
    md_path: Optional[Path] = None,
) -> AggregateReport:
    out_dir.mkdir(parents=True, exist_ok=True)
    scores: List[CaseScore] = []
    raw_states: List[dict] = []

    for sc in scenarios:
        logger.info(f"\n========== EVAL CASE: {sc.id} ==========")
        logger.info(f"description: {sc.description}")
        try:
            final_state, latency, errors = await run_case(sc)
        except Exception as e:
            logger.exception(f"case {sc.id} crashed")
            final_state, latency, errors = {}, 0.0, [f"crash: {e}"]
        s = score_case(sc, final_state, latency_seconds=latency, errors=errors)
        logger.info(f"-> success={s.success} reasons={s.reasons}")
        scores.append(s)
        raw_states.append(final_state)

    agg = aggregate(scores, raw_states=raw_states)

    # 输出
    json_path = json_path or out_dir / "eval_result.json"
    md_path = md_path or out_dir / "eval_report.md"
    json_path.write_text(json.dumps(agg.to_dict(), ensure_ascii=False, indent=2),
                         encoding="utf-8")
    md_path.write_text(_render_markdown(agg), encoding="utf-8")
    logger.info(f"\n📄 JSON 报告: {json_path}")
    logger.info(f"📄 Markdown 报告: {md_path}")
    return agg


def _render_markdown(agg: AggregateReport) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"# SREwise Eval 报告",
        f"",
        f"生成时间: {ts}",
        f"",
        f"## 总体指标",
        f"",
        f"| 指标 | 值 |",
        f"|---|---|",
        f"| 总场景数 | {agg.total} |",
        f"| 通过 | {agg.passed} |",
        f"| 通过率 | **{agg.pass_rate:.1%}** |",
        f"| 根因命中率 | {agg.rc_hit_rate:.1%} |",
        f"| 修复召回率 | {agg.action_hit_rate:.1%} |",
        f"| 平均置信度 | {agg.avg_confidence:.2f} |",
        f"| 平均延迟 | {agg.avg_latency_seconds:.1f}s |",
        f"| 安全门违规总数 | {agg.safety_violations_total} |",
        f"",
        f"## 单场景明细",
        f"",
    ]
    for c in agg.by_case:
        status = "✅" if c.success else "❌"
        lines.append(f"### {status} `{c.case_id}`")
        lines.append("")
        lines.append(f"- root_cause 推断: `{c.root_cause_inferred}` (hit={c.root_cause_hit})")
        lines.append(f"- 诊断结论: {c.diagnosis_root_cause_text!r}")
        lines.append(f"- 提议工具: {c.proposed_tools}")
        lines.append(f"- 实际执行: {c.executed_tools}")
        lines.append(f"- 禁止工具违规: {c.forbidden_violations or '无'}")
        lines.append(f"- 延迟: {c.latency_seconds:.1f}s")
        if c.errors:
            lines.append(f"- ⚠️ 错误: {c.errors}")
        if c.reasons:
            lines.append(f"- 失败原因: {c.reasons}")
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="SREwise eval runner")
    parser.add_argument("--case", action="append", default=[],
                        help="只跑指定 id 的 case (可多次)")
    parser.add_argument("--scenarios", default=None,
                        help="自定义 scenarios.json 路径")
    parser.add_argument("--out", default="eval_results",
                        help="输出目录,默认 ./eval_results")
    args = parser.parse_args(argv)

    scenarios = load_scenarios(Path(args.scenarios) if args.scenarios else None)
    if args.case:
        wanted = set(args.case)
        scenarios = [s for s in scenarios if s.id in wanted]
        if not scenarios:
            logger.error(f"指定的 case 都不存在: {wanted}")
            return 2

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = out_dir / f"run_{timestamp}"

    agg = asyncio.run(run_all(scenarios, out_dir=run_dir))

    # 退出码:全通过 → 0,有失败 → 1
    return 0 if agg.passed == agg.total else 1


if __name__ == "__main__":
    sys.exit(main())
