"""SREwise Step 7 — Eval 框架。

目标
====
把"多 Agent 系统跑得怎么样"量化成几个可以 nightly 跑的指标。
- root_cause_hit:  诊断根因是否命中预期类别
- action_recall:   修复建议中是否包含必选工具集
- forbidden_violation: 是否提议了禁止的高风险动作
- report_present:  最终复盘报告是否生成
- latency_seconds: 端到端耗时
- tokens_total:    LLM token 消耗 (从 Langfuse 拉,可选)

子模块
======
- `dataset`:  加载 scenarios.json
- `runner`:   单 case 跑图 + 自动审批
- `scorer`:   per-case 指标 + 聚合
- `cli`:      `python -m app.eval` 入口
"""
