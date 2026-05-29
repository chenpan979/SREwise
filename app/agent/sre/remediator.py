"""Remediator 节点 — 生成候选修复动作。

Step 2 范围
============
**只生成不执行**。基于 Diagnosis 给出 1~3 个候选 action,每个标注:
- tool_name + args (要调用的写工具)
- risk_level (read/write/destructive)
- rationale (为什么这么修)
- expected_outcome (预期效果)

Step 3 会在这个节点之后插入 HITL interrupt,只有用户审批后才把 approved_actions
喂给一个新的 executor 节点真正执行。

为什么不让 Diagnostician 一并出修复方案?
========================================
分离关注点:
- Diagnostician 看的是 read 工具,目标是"找根因"
- Remediator 看的是 write 工具,目标是"找最佳干预手段"
两个 Agent 的 prompt / 工具集 / 评估标准都不同,分开实现日志和测试都更清晰,
也让简历能讲"职责单一原则的多 Agent 拆分"。
"""

import json
from textwrap import dedent
from typing import Any, Dict, List

from langchain_core.prompts import ChatPromptTemplate
from langchain_qwq import ChatQwen
from loguru import logger
from pydantic import BaseModel, Field

from app.agent.mcp_client import get_mcp_client_with_retry, load_mcp_tools_safe
from app.config import config
from app.tools import DEFAULT_LOCAL_REMEDIATION_TOOLS
from .state import SREState
from .tool_filter import extract_risk_level, filter_write_tools


# ============================================================
# 输出 schema
# ============================================================

class Action(BaseModel):
    tool_name: str = Field(description="要调用的工具名,必须来自给定的可用工具列表")
    args: Dict[str, Any] = Field(default_factory=dict,
                                 description="工具参数 (key-value)")
    rationale: str = Field(description="为什么提议这个动作 (一句话)")
    expected_outcome: str = Field(description="预期效果 (一句话)")
    priority: int = Field(description="优先级 1=最优, 3=兜底", ge=1, le=3)


class ActionPlan(BaseModel):
    actions: List[Action] = Field(
        description="按优先级排序的候选动作 (1~3 个)",
        min_length=0,
        max_length=3,
    )
    overall_strategy: str = Field(
        description="总体修复策略一句话总结",
    )


# ============================================================
# Prompt
# ============================================================

_SYSTEM = dedent("""
    你是 SREwise 的修复策略 Agent (Remediator)。

    任务: 基于已知根因,从可用的"写"工具中挑选最合适的 1~3 个候选修复动作。
    **只提议,不执行** —— 这些动作随后会送给人审批。

    可用写工具
    ===========
    {tools_block}

    选择原则
    ========
    1. **逆操作优先**: 如果根因是"最近一次发布引入",优先 rollback_deployment
       回到上一个稳定版本 —— 这是最安全的恢复方式
    2. **风险最小化**: 同等效果下偏向 write 而非 destructive
    3. **可观测**: 每个动作都要写明 expected_outcome,方便审批人判断
    4. **不重复发明轮子**: 历史故障 resolved_by 字段提示了过去成功的修复手段,
       优先复用
    5. **不要硬塞动作**: 如果根因不明 (confidence < 0.5),宁可只给 1 个低风险
       动作或返回空列表,等待补充诊断

    输出
    ====
    严格按 ActionPlan schema 输出,actions 按 priority 升序排列。
""").strip()

_prompt = ChatPromptTemplate.from_messages([
    ("system", _SYSTEM),
    ("user", dedent("""
        # 任务
        {input}

        # 诊断结论
        ```json
        {diagnosis_json}
        ```

        # 历史成功修复手段 (供参考)
        {historical_remedies}

        请输出候选修复动作。
    """).strip()),
])


# ============================================================
# 节点实现
# ============================================================

async def remediator(state: SREState) -> Dict[str, Any]:
    """生成候选修复动作。"""
    logger.info("=== Remediator: 生成候选修复动作 ===")

    diagnosis = state.get("diagnosis") or {}
    if not diagnosis or not diagnosis.get("root_cause"):
        logger.warning("无诊断结论,跳过修复动作生成")
        return {"proposed_actions": []}

    # 1. 拿写工具: 优先 MCP, 失败/超时 fallback 到本地 mock 工具,保证 HITL 链路不断
    mcp_tools: List[Any] = []
    try:
        client = await get_mcp_client_with_retry()
        mcp_tools, err = await load_mcp_tools_safe(client, timeout=15.0)
        if err:
            logger.warning(f"MCP get_tools 异常 (将降级到本地工具):\n{err}")
    except Exception as e:
        logger.warning(f"MCP 客户端初始化失败 (将降级到本地工具): {e!r}")

    # 始终把本地修复工具加入候选集; 即使 MCP 在线,本地工具也提供 deterministic 兜底
    candidate_tools = list(mcp_tools) + list(DEFAULT_LOCAL_REMEDIATION_TOOLS)
    write_tools = filter_write_tools(candidate_tools)
    if not write_tools:
        logger.warning("无可用写工具,无法生成修复动作")
        return {"proposed_actions": []}
    logger.info(
        f"修复可用写工具: {len(write_tools)} "
        f"(MCP={len(filter_write_tools(list(mcp_tools)))}, "
        f"local={len(DEFAULT_LOCAL_REMEDIATION_TOOLS)})"
    )

    tools_block = _format_tools(write_tools)
    historical_remedies = _format_historical(state.get("similar_incidents") or [])

    # 2. LLM 生成动作
    llm = ChatQwen(model=config.rag_model,
                   api_key=config.dashscope_api_key,
                   temperature=0)
    chain = _prompt | llm.with_structured_output(ActionPlan)

    try:
        plan = await chain.ainvoke({
            "input": state.get("input", ""),
            "diagnosis_json": json.dumps(diagnosis, ensure_ascii=False, indent=2),
            "historical_remedies": historical_remedies or "(无历史修复记录)",
            "tools_block": tools_block,
        })
        actions = (plan.actions if isinstance(plan, ActionPlan)
                   else plan.get("actions", []))  # type: ignore
        strategy = (plan.overall_strategy if isinstance(plan, ActionPlan)
                    else plan.get("overall_strategy", ""))  # type: ignore
    except Exception as e:
        logger.error(f"修复动作生成失败: {e}")
        return {"proposed_actions": []}

    # 3. 二次校验 + 标 risk_level
    valid_tool_names = {getattr(t, "name", None) for t in write_tools}
    proposed: List[Dict[str, Any]] = []
    for a in actions:
        a_dict = a.model_dump() if isinstance(a, Action) else dict(a)
        if a_dict["tool_name"] not in valid_tool_names:
            logger.warning(f"丢弃非法工具名: {a_dict['tool_name']}")
            continue
        # 从工具描述里取 risk_level
        tool_obj = next((t for t in write_tools
                         if getattr(t, "name", None) == a_dict["tool_name"]), None)
        a_dict["risk_level"] = extract_risk_level(tool_obj) if tool_obj else "write"
        a_dict["strategy"] = strategy
        proposed.append(a_dict)

    logger.info(f"生成 {len(proposed)} 个候选动作,strategy: {strategy}")
    for i, a in enumerate(proposed, 1):
        logger.info(f"  [{i}] {a['tool_name']}({a.get('args')}) "
                    f"risk={a['risk_level']} prio={a.get('priority')}")

    return {"proposed_actions": proposed}


# ============================================================
# 辅助
# ============================================================

def _format_tools(tools: List[Any]) -> str:
    """渲染工具描述给 LLM 看,带上 risk_level 标签。"""
    lines = []
    for t in tools:
        name = getattr(t, "name", "?")
        desc = (getattr(t, "description", "") or "").strip()
        # 截短 description 防止 prompt 过长
        if len(desc) > 400:
            desc = desc[:400] + "..."
        risk = extract_risk_level(t)
        lines.append(f"- **{name}** (risk={risk})\n  {desc}")
    return "\n".join(lines) if lines else "(无可用工具)"


def _format_historical(incidents: List[Dict[str, Any]]) -> str:
    """从 similar_incidents 抽取历史成功修复手段。

    优先级:
      1. KG action 模板 (kg_action_template) -- 带 hit_count,最权威
      2. KG incident 的 resolved_actions
      3. alertmanager_alert 的 resolved_by 字段 (降级路径)
      4. pattern_summary
    """
    lines = []

    # 1. KG action 模板
    templates = [i for i in incidents if i.get("_kind") == "kg_action_template"]
    if templates:
        lines.append("## KG Action 模板 (按历史命中数排序,优先复用)")
        for t in templates[:5]:
            lines.append(
                f"- {t.get('tool_name')} | hit_count={t.get('hit_count')} "
                f"| sample_args={t.get('sample_args')} "
                f"| last_used={t.get('last_used_at')}"
            )

    # 2. KG incident 的 resolved_actions
    kg_inc = [i for i in incidents if i.get("_kind") == "kg_incident"]
    if kg_inc:
        lines.append("## 类似 Incident 的成功修复历史")
        for inc in kg_inc[:5]:
            actions = inc.get("resolved_actions") or []
            for a in actions[:2]:
                if a.get("success"):
                    lines.append(
                        f"- {inc.get('alert_name')}/{inc.get('service')}: "
                        f"{a.get('tool_name')}(args≈{a.get('sample_args')})"
                    )

    # 3. alertmanager 降级路径
    am = [i for i in incidents if i.get("_kind") == "alertmanager_alert"]
    if am:
        lines.append("## (降级) Alertmanager 历史")
        for h in am[:3]:
            rb = h.get("resolved_by")
            if rb:
                lines.append(f"- {h.get('name')} → {rb}")

    # 4. 模式总结
    summaries = [i for i in incidents if i.get("_kind") == "pattern_summary"]
    for s in summaries:
        lines.append(f"## 模式: {s.get('summary')}")

    return "\n".join(lines) if lines else ""
