# SREwise — 自治式 SRE 智能体平台 · 改造路线图

> **从「能诊断的 Agent」升级为「会诊断、能修复、会学习」的多 Agent 闭环 SRE 系统。**
>
> 本文档是项目改造的"主线剧本",每完成一步打勾,以便随时接续。

---

## 1. 项目定位

| 项 | 内容 |
|---|---|
| **项目名** | `SREwise` |
| **一句话** | 基于 LangGraph 多 Agent + MCP + 故障知识图谱 + Human-in-the-Loop 的自治式 SRE 智能体平台 |
| **目标场景** | 收到告警 → 自动诊断 → 给出根因 → 在人审通过后执行修复 → 沉淀到故障知识图谱供未来召回 |
| **目标用户** | SRE / 运维工程师 / 平台 Tech Lead |
| **演示叙事** | "凌晨 3 点告警,SREwise 30 秒内输出根因报告 + 1 个修复方案,SRE 在手机上点'批准',系统自动执行 kubectl rollout restart,并把这次故障写进知识图谱,下次同类问题直接命中历史经验。" |

## 2. 与原项目 (SuperBizAgent) 的差异

| 维度 | 原项目 | SREwise |
|---|---|---|
| **Agent 架构** | 单一 Plan-Execute-Replan 图 | Supervisor + 4 个专业 Agent (Historian / Diagnostician / Remediator / Reporter) |
| **闭环深度** | 只到诊断报告 | 诊断 → 修复 → 复盘三段闭环 |
| **人机协作** | 全自动 | Human-in-the-Loop:高危动作前 `interrupt()` 等审批 |
| **记忆** | 无 | Incident Knowledge Graph (Neo4j) + 历史故障向量召回 |
| **RAG** | 朴素向量检索 | GraphRAG:实体抽取 → 子图召回 |
| **MCP 工具** | 2 个 (日志、监控) | 5+ 个 (日志、监控、K8s、Alertmanager、Grafana) |
| **可观测性** | 仅 loguru | Langfuse / OTel 全链路追踪 |
| **Eval** | 无 | 基于历史故障 fixture 自动 replay,度量 MTTR / 根因命中率 |

## 3. 总体架构

```
                         ┌──────────────────┐
   告警/用户提问 ────►   │   Supervisor     │ ◄── LangGraph StateGraph 路由
                         │  (路由 + 终止)   │
                         └────────┬─────────┘
                ┌─────────────────┼─────────────────┬──────────────┐
                ▼                 ▼                 ▼              ▼
         ┌─────────────┐  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐
         │ Historian   │  │Diagnostician│  │ Remediator  │  │  Reporter   │
         │ 历史故障召回│  │  根因诊断   │  │ 修复执行    │  │  事后复盘   │
         │  (KG + RAG) │  │  (MCP 工具) │  │  (HITL审批) │  │ (写回 KG)   │
         └──────┬──────┘  └──────┬──────┘  └──────┬──────┘  └──────┬──────┘
                │                │                │                │
                ▼                ▼                ▼                ▼
         ┌──────────────────────────────────────────────────────────────┐
         │                    MCP 工具网关                              │
         │  cls_logs │ monitor │ k8s │ alertmanager │ grafana           │
         └──────────────────────────────────────────────────────────────┘
                │                                                  │
                ▼                                                  ▼
         ┌─────────────────────┐                       ┌────────────────────┐
         │  Incident KG        │                       │   Langfuse / OTel  │
         │  (Neo4j / NetworkX) │                       │   Agent 全链路追踪 │
         └─────────────────────┘                       └────────────────────┘
```

## 4. State 演进 (LangGraph)

原 `PlanExecuteState` 只有 4 字段。SREwise 扩展为:

```python
class SREState(TypedDict):
    # 输入与上下文
    alert: dict | None              # 触发的告警(可选)
    input: str                      # 用户/告警转换出的任务描述
    session_id: str

    # 多 Agent 协作
    next_agent: Literal["historian", "diagnostician", "remediator", "reporter", "END"]
    messages: Annotated[list[BaseMessage], operator.add]

    # 历史召回 (Historian 输出)
    similar_incidents: list[dict]   # 从 KG / 向量库召回的相似故障
    relevant_runbooks: list[dict]   # GraphRAG 召回的子图/文档

    # 诊断 (Diagnostician 输出)
    diagnosis: dict | None          # {root_cause, evidence, confidence}

    # 修复 (Remediator 输出 + HITL)
    proposed_actions: list[dict]    # 候选修复动作 (含风险等级)
    approved_actions: list[dict]    # 经人审批的动作
    execution_results: list[dict]

    # 复盘 (Reporter 输出)
    incident_report: str | None     # Markdown 复盘报告
    kg_writeback_done: bool         # 是否已写回知识图谱
```

## 5. 改造步骤总览

| Step | 标题 | 关键产出 | 简历价值 |
|---|---|---|---|
| 0 | 项目重定位 | 本文档 + 改名 SREwise | 立项清晰 |
| 1 | 扩充 MCP 工具 | k8s/alertmanager/grafana mock server | MCP 协议落地 |
| 2 | 多 Agent 重构 | Supervisor + 4 Agent | LangGraph 多 Agent 实战 |
| 3 | HITL 审批 | `interrupt()` + 审批 API | LangGraph 高级特性 |
| 4 | 故障知识图谱 | Neo4j Schema + 写入/召回 | 图数据库 + Agent 长期记忆 |
| 5 | GraphRAG | 实体抽取 + 子图检索 | RAG 升级 |
| 6 | 可观测性 | Langfuse / OTel 接入 | LLMOps |
| 7 | Eval 框架 | fixture + replay + 指标 | 工程化 |
| 8 | 前端重造 | 推理可视化 + 审批 UI + KG 图谱 | 体验闭环 |
| 9 | 文档收尾 | 架构图 + README + 简历话术 | 交付完整 |

---

## Step 0 — 项目重定位 ✅ 当前进行中

**目标**: 不动业务逻辑,只完成命名/描述/路线图,让项目"在身份上"已经是 SREwise。

- [x] 落盘本路线图 `docs/SREWISE_ROADMAP.md`
- [ ] `pyproject.toml` description 更新
- [ ] `app/config.py` `app_name` 改为 `SREwise`
- [ ] `app/main.py` FastAPI description 更新
- [ ] README 顶部加 SREwise 立项说明(保留原 setup 流程不动)

**验收**: `python -m uvicorn app.main:app` 启动后,日志和 `/docs` 显示 `SREwise`,所有现有 API 仍正常。

---

## Step 1 — 扩充 MCP 工具集

**目标**: 把"只能查日志和指标"扩成"能动手修复的工具网关"。

新增 3 个 MCP server (mock,但接口逼真):

1. **`mcp_servers/k8s_server.py`** (port 8005)
   - `list_pods(namespace)` / `describe_pod(name)` / `get_pod_logs(name, tail)`
   - `restart_deployment(name)` ⚠️ 高危
   - `scale_deployment(name, replicas)` ⚠️ 高危
   - `rollback_deployment(name)` ⚠️ 高危
2. **`mcp_servers/alertmanager_server.py`** (port 8006)
   - `list_active_alerts()` / `silence_alert(id, duration)` / `get_alert_history(service)`
3. **`mcp_servers/grafana_server.py`** (port 8007)
   - `query_dashboard(uid, time_range)` 返回若干面板的指标摘要
   - `query_promql(expr, range)` 模拟 Prometheus 查询

**关键设计**: 每个工具在元数据里加 `risk_level: read|write|destructive`,后面 Remediator + HITL 用这个字段决定是否要审批。

---

## Step 2 — Supervisor + 4 Agent 多 Agent 重构

**目标**: 拆解原单图为 5 节点多 Agent。

- 新建 `app/agent/sre/` 目录,与原 `aiops/` 并存(灰度)
- 实现:
  - `supervisor.py` — 路由 LLM,根据 state 决定下一个 agent
  - `historian.py` — 调用 KG 召回 + RAG
  - `diagnostician.py` — 收敛原 planner+executor+replanner 的诊断逻辑(只到根因)
  - `remediator.py` — 生成候选修复动作(暂不执行)
  - `reporter.py` — 生成 Markdown 复盘 + 写回 KG
- 新 API `/api/sre/diagnose` (SSE 流) 走新图
- 旧 `/api/aiops` 保留兼容

**LangGraph 模式**: 参考官方 [supervisor pattern](https://langchain-ai.github.io/langgraph/tutorials/multi_agent/agent_supervisor/)。

---

## Step 3 — Human-in-the-Loop 审批

**目标**: 在 Remediator 执行 `risk_level != read` 的工具前 `interrupt()`,等人审批。

- 用 LangGraph `interrupt()` + `Command(resume=...)` API
- 新 API:
  - `POST /api/sre/approve` — 提交审批结果(`approved` / `rejected` / `modified`)
  - `GET  /api/sre/pending` — 查待审批列表
- Checkpointer 用 `MemorySaver` (后期可升级 SQLite/Postgres)

---

## Step 4 — Incident Knowledge Graph

**目标**: 每次故障落库,形成可召回的图谱。

- Schema (Neo4j 或先用 NetworkX 内存图,docker-compose 加 Neo4j 服务):
  - 节点: `Service` / `Metric` / `Symptom` / `RootCause` / `Action` / `Incident`
  - 关系: `(Incident)-[:AFFECTS]->(Service)`、`(Incident)-[:CAUSED_BY]->(RootCause)`、`(Incident)-[:RESOLVED_BY]->(Action)`、`(Service)-[:DEPENDS_ON]->(Service)`
- `app/services/incident_kg.py`: `write_incident()` / `query_similar()` / `get_service_topology()`
- Reporter 完成后调用 `write_incident()`
- Historian 启动时调用 `query_similar()` 用作上下文

---

## Step 5 — GraphRAG 替换 vanilla RAG

**目标**: runbook 文档不再是简单切块入向量库,而是抽实体建图。

- 文档入库流程改为: chunk → LLM 抽实体/关系 → 写入 KG → 同时存向量
- 检索流程改为: query → 抽实体 → 在 KG 中找子图 → 子图 + 向量召回 → 合并 rerank
- 库选型: `langchain-experimental.graph_transformers.LLMGraphTransformer`

---

## Step 6 — 可观测性

**目标**: 每个 Agent 的输入/输出/token/延迟/工具调用全链路可见。

- 接 [Langfuse](https://langfuse.com/) (自托管 docker-compose 一行起)
- LangChain `CallbackHandler` 挂上去自动追踪所有 LLM/Tool 调用
- 给每个 session 一个 trace_id,复盘报告里附 trace 链接

---

## Step 7 — Eval 框架

**目标**: 量化"我这套 Agent 比基线强多少"。

- `evals/fixtures/` 里造 ~10 个历史故障 case (告警 + 期望根因 + 期望动作)
- `evals/runner.py` 自动 replay,度量:
  - 根因命中率 (LLM as judge)
  - 动作正确率
  - 平均步数 / 平均 token / 平均延迟 (MTTR proxy)
- 输出 markdown 报告

---

## Step 8 — 前端

- Agent 推理过程实时可视化(每个 Agent 一个泳道)
- 待审批动作卡片 (批准/驳回/修改)
- Incident KG 浏览器 (D3 / Cytoscape)
- Eval 结果看板

---

## Step 9 — 收官

- 架构图 (excalidraw / mermaid)
- 顶部 README 重写 (英文 + 中文)
- 一段可粘贴到简历的项目话术
- demo gif / 截图

---

## 进度记录

> 每完成一步在这里追加一行,带日期。

- 2026-05-27 Step 0 启动: 路线图落盘
