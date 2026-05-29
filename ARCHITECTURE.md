# SREwise 系统架构文档

## 整体架构

```
┌─────────────────────────────────────────────────────────────────────┐
│                         📥 输入层 (Trigger)                          │
│  Alertmanager 告警 / 用户手动诊断 / Eval 自动回放                    │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│                    🎛️ Supervisor (LLM Router)                       │
│  硬规则护栏 + LLM 灵活决策,路由到 5 个专业 Agent                      │
└──┬────────┬────────┬────────┬────────┬──────────────────────────────┘
   │        │        │        │        │
   ▼        ▼        ▼        ▼        ▼
┌──────┐┌──────┐┌──────┐┌──────┐┌──────┐
│Histor││Diagno││Remedi││Execut││Report│  🤖 Agent 层 (专业分工)
│ ian  ││sticia││ ator ││ or   ││ er   │
│召回  ││诊断  ││提议  ││执行  ││复盘  │
└──┬───┘└──┬───┘└──┬───┘└──┬───┘└──┬───┘
   │       │       │       │       │
   │       │       │       │       │
   ▼       ▼       │       ▼       ▼
┌─────────────┐   │   ┌─────────────┐
│  Neo4j KG   │   │   │ MCP 工具网关 │  🛠️ 工具 & 记忆层
│ (故障图谱)   │   │   │ 21 个工具    │
│ + Milvus    │   │   │ (read/write) │
│ (向量库)     │   │   └─────────────┘
└─────────────┘   │
                  │
                  ▼
            ┌──────────┐
            │   HITL   │  ⏸️ Human-in-the-Loop
            │ 人工审批  │  (destructive 动作拦截)
            └──────────┘
                  │
                  ▼
            (resume 恢复执行)
                  │
                  ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   👁️ Langfuse 全链路追踪                            │
│  LLM generation / Tool calls / KG queries / 审批事件 全部可观测       │
└─────────────────────────────────────────────────────────────────────┘
```

## 核心组件详解

### 1. Supervisor (路由器)

**职责**: 决定下一步执行哪个 Agent

**实现**: 双层路由
```python
# 第一层:硬规则护栏 (确定性场景)
if state.get("incident_report"):
    return "END"
if state.get("proposed_actions") and not state.get("approved_actions"):
    return "human_review"
if state.get("approved_actions") and not state.get("execution_results"):
    return "executor"

# 第二层:LLM 灵活决策 (灰色地带)
decision = llm.with_structured_output(RouteDecision).invoke(prompt)
return decision.next_agent
```

**关键设计**:
- 确定性场景走代码,不浪费 LLM token
- 防死循环:同 agent 连续 3 次 → 强制推进
- 路由超 10 次 → 强制 END

### 2. Historian (历史召回)

**职责**: 从 KG + Milvus 召回相似故障

**实现**: GraphRAG 三路并发
```python
async def query(...):
    # 并发三路
    kg_task = asyncio.create_task(self._kg_path(...))
    vec_task = asyncio.create_task(self._vector_path(...))
    
    kg_incidents, kg_templates = await kg_task
    vec_chunks = await vec_task
    
    # Cross-seed: 用 KG 结果反查向量库
    cross_chunks = await self._cross_seed(kg_incidents, already=vec_chunks)
    
    return {kg_incidents, kg_templates, vec_chunks, cross_chunks}
```

**输出**:
- `similar_incidents`: 相似故障列表 (含 root_cause / actions / recurrence_count)
- `runbooks`: 召回的 runbook 段落 (按 channel 分组:filtered / semantic / cross_seed)

### 3. Diagnostician (根因诊断)

**职责**: 调用 read 工具,定位根因

**实现**: ReAct 循环 + 结构化输出
```python
# 前 6 轮:工具调用循环
for i in range(MAX_TOOL_CALLS):
    response = llm_with_tools.invoke(messages)
    if not response.tool_calls:
        break
    tool_results = execute_tools(response.tool_calls)
    messages.append(tool_results)

# 最后:结构化输出
diagnosis = llm.with_structured_output(Diagnosis).invoke(messages)
return {"diagnosis": diagnosis}
```

**工具隔离**: 只拿 `risk_level: read` 的工具,根本调不到写工具

**输出**:
```python
{
    "root_cause": "data-sync-service 的 Pod 因内存溢出 (OOMKilled) 而反复重启",
    "root_cause_category": "memory_oom",
    "confidence": 0.85,
    "evidence": [...],
    "affected_services": ["data-sync-service"]
}
```

### 4. Remediator (修复提议)

**职责**: 生成候选修复动作 (不执行)

**实现**: LLM 生成 + KG 历史 action 模板提示
```python
# 从 KG 拿历史成功动作
kg_actions = incident_kg.get_action_templates(root_cause, service)

# 提示词里加上历史模板
prompt = f"""
根因: {diagnosis.root_cause}
历史成功动作 (hit_count 高的优先):
{kg_actions}

生成 1-3 个候选修复动作,按优先级排序
"""

proposed = llm.with_structured_output(ProposedActions).invoke(prompt)
```

**工具隔离**: 只拿 `risk_level: write|destructive` 的工具

**输出**:
```python
[
    {
        "tool_name": "rollback_deployment",
        "args": {"name": "data-sync-service", "namespace": "production"},
        "risk_level": "destructive",
        "priority": 1,
        "rationale": "历史 2 次 OOM 均通过回滚解决",
        "expected_outcome": "Pod 恢复 Running,内存使用率降至 60%"
    },
    ...
]
```

### 5. Human Review (HITL 审批)

**职责**: 暂停图,等待人工审批

**实现**: LangGraph `interrupt()`
```python
def human_review(state: SREState) -> dict:
    if not state.get("proposed_actions"):
        return {}  # 无候选动作,跳过
    
    # 第一次执行:抛 GraphInterrupt,暂停图
    decision = interrupt({
        "proposed_actions": state["proposed_actions"],
        "diagnosis": state["diagnosis"],
    })
    
    # 第二次执行 (resume 后):decision 是前端提交的审批结果
    if decision["approve"]:
        approved = [state["proposed_actions"][i] 
                    for i in decision["selected_indices"]]
        return {"approved_actions": approved, "approval": decision}
    else:
        return {"approved_actions": [], "approval": decision}
```

**前端流程**:
1. SSE 收到 `interrupt` 事件 → 弹审批面板
2. 用户勾选动作 → POST `/api/sre/approve`
3. 后端 `Command(resume=decision)` → 图恢复执行

### 6. Executor (动作执行)

**职责**: 执行已批准动作

**实现**: 二次校验 + 真调用工具
```python
for action in state["approved_actions"]:
    tool = find_tool(action["tool_name"])
    
    # 二次校验 risk_level (防御性编程)
    actual_risk = extract_risk_level(tool)
    if actual_risk == "read":
        results.append({"success": False, "error": "拒绝执行 read 工具"})
        continue
    
    # 真调用
    result = tool.invoke(action["args"])
    results.append({"success": True, "result": result})

return {"execution_results": results}
```

**关键**: 即便 Remediator 出 bug 把 read 工具放进 proposed_actions,即便 human_review 误批,Executor 最后一道闸门仍然挡得住

### 7. Reporter (事后复盘)

**职责**: 生成 Markdown 报告 + 写回 KG

**实现**: LLM 生成 + 双写 (KG + Milvus)
```python
# 1. 生成报告
report = llm.invoke(f"""
根据以下信息生成故障复盘报告:
- 告警: {state["alert"]}
- 根因: {state["diagnosis"]}
- 执行动作: {state["execution_results"]}
""")

# 2. 写回 KG
incident_id = incident_kg.upsert_incident(
    alert_name=...,
    service=...,
    root_cause_category=...,
    actions=[...],
    symptoms=[...]
)

# 3. 写回 Milvus (向量库)
graph_rag.index_incident_text(
    incident_id=incident_id,
    summary=diagnosis.root_cause,
    metadata={"service": ..., "root_cause": ...}
)

return {"incident_report": report}
```

**闭环**: 下次同类故障,Historian 能从 KG 和 Milvus 同时召回这次的诊断结论

---

## 数据流图

```
┌─────────┐
│ Alert   │
│ 触发    │
└────┬────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│ Supervisor: 路由 → historian                                 │
└────┬────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│ Historian: GraphRAG 三路召回                                 │
│   ├─ KG: MATCH (i:Incident {service: $s})-[:CAUSED_BY]→(rc)│
│   ├─ Vector: Milvus.search(query, expr="service == $s")    │
│   └─ Cross-seed: Milvus.search(kg_incident.summary)        │
│ → similar_incidents: [...]                                  │
│ → runbooks: [...]                                           │
└────┬────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│ Supervisor: 路由 → diagnostician                             │
└────┬────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│ Diagnostician: ReAct 工具循环 (只读工具)                     │
│   ├─ list_pods → describe_pod → get_pod_logs               │
│   ├─ query_promql → query_dashboard                        │
│   └─ list_active_alerts                                    │
│ → diagnosis: {root_cause, confidence, evidence}            │
└────┬────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│ Supervisor: 路由 → remediator                                │
└────┬────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│ Remediator: 生成候选动作 (写工具)                            │
│   KG 历史 action 模板: rollback_deployment (hit_count=2)   │
│ → proposed_actions: [{tool, args, risk, priority}, ...]    │
└────┬────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│ Supervisor: 路由 → human_review                              │
└────┬────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│ Human Review: interrupt() 暂停图                             │
│   SSE 推送 interrupt 事件到前端                              │
│   前端弹审批面板,用户勾选动作                                 │
│   POST /api/sre/approve → Command(resume=decision)         │
│ → approved_actions: [...]                                   │
└────┬────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│ Supervisor: 路由 → executor                                  │
└────┬────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│ Executor: 执行已批准动作                                      │
│   二次校验 risk_level                                        │
│   真调用 MCP 工具: rollback_deployment(...)                 │
│ → execution_results: [{success, result}, ...]               │
└────┬────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│ Supervisor: 路由 → reporter                                  │
└────┬────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│ Reporter: 生成复盘 + 写回 KG + Milvus                        │
│   LLM 生成 Markdown 报告                                     │
│   incident_kg.upsert_incident(...)                         │
│   graph_rag.index_incident_text(...)                       │
│ → incident_report: "# 故障复盘\n..."                        │
└────┬────────────────────────────────────────────────────────┘
     │
     ▼
┌─────────────────────────────────────────────────────────────┐
│ Supervisor: incident_report 已生成 → END                     │
└─────────────────────────────────────────────────────────────┘
```

---

## 技术选型理由

| 组件 | 选型 | 理由 |
|------|------|------|
| **编排框架** | LangGraph | 支持 interrupt() / StateGraph / Checkpointer,是 LangChain 生态里唯一能做 HITL 的 |
| **图数据库** | Neo4j | Cypher 查询语言强大,MERGE + ON CREATE/MATCH 天然支持 UPSERT,社区活跃 |
| **向量库** | Milvus | JSON metadata 过滤 (expr),性能强,开源 |
| **可观测** | Langfuse | LLM-native,UI 直接看 prompt/completion/token,v2/v3 都支持 |
| **工具协议** | MCP | 标准化工具接入,FastMCP 零配置启动,未来可接入真实 K8s/Grafana |
| **前端** | 零构建 ES Modules | 部署简单,改完直接刷新,不需要 npm/webpack |

---

## 部署架构

```
┌─────────────────────────────────────────────────────────────┐
│                      Docker Compose                          │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │ Milvus   │  │  Neo4j   │  │ Langfuse │  │ Postgres │   │
│  │ :19530   │  │  :7687   │  │  :3000   │  │  :5432   │   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘   │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      FastAPI (uvicorn)                       │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  /api/sre/diagnose  (SSE)                            │  │
│  │  /api/sre/approve   (POST)                           │  │
│  │  /api/sre/history   (GET)                            │  │
│  │  /api/sre/kg/*      (GET)                            │  │
│  │  /api/eval/run      (SSE)                            │  │
│  │  /console/          (静态文件)                        │  │
│  └──────────────────────────────────────────────────────┘  │
│                         :9900                                │
└─────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                      MCP Servers (5 个)                      │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐   │
│  │   CLS    │  │ Monitor  │  │   K8s    │  │Alertmgr  │   │
│  │  :8003   │  │  :8004   │  │  :8005   │  │  :8006   │   │
│  └──────────┘  └──────────┘  └──────────┘  └──────────┘   │
│  ┌──────────┐                                               │
│  │ Grafana  │                                               │
│  │  :8007   │                                               │
│  └──────────┘                                               │
└─────────────────────────────────────────────────────────────┘
```

---

**📌 关键文件索引**:
- Supervisor: `app/agent/sre/supervisor.py`
- Historian: `app/agent/sre/historian.py`
- Diagnostician: `app/agent/sre/diagnostician.py`
- Remediator: `app/agent/sre/remediator.py`
- Executor: `app/agent/sre/executor.py`
- Reporter: `app/agent/sre/reporter.py`
- Human Review: `app/agent/sre/human_review.py`
- Graph 装配: `app/agent/sre/graph.py`
- State 定义: `app/agent/sre/state.py`
- Incident KG: `app/services/incident_kg.py`
- GraphRAG: `app/services/graph_rag.py`
- Observability: `app/services/observability.py`
- SRE Service: `app/services/sre_service.py`
- Eval Runner: `app/eval/runner.py`
