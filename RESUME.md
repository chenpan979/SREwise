# SREwise 项目 — 简历素材

## 项目名称
**SREwise — 自治式 SRE 智能体平台**

## 一句话描述
基于 LangGraph 多 Agent 编排 + Neo4j 故障知识图谱 + Human-in-the-Loop 审批,实现"告警 → 诊断 → 修复 → 复盘 → 沉淀"全流程闭环的生产级 SRE 自动化系统。

## 技术栈
Python 3.11 | FastAPI | LangGraph | LangChain | Neo4j | Milvus | MCP | Langfuse | Docker | ES Modules

---

## 核心职责与成果

### 1. 多 Agent 协作架构设计
- 设计并实现 Supervisor + 5 专业 Agent (Historian / Diagnostician / Remediator / Executor / Reporter) 的 LangGraph StateGraph,通过硬规则护栏 + LLM 灵活决策双层路由,避免单 Agent 死循环
- 基于 `risk_level` 标签实现工具层隔离:Diagnostician 只拿 read 工具,Remediator 只拿 write/destructive 工具,Executor 二次校验,深度防御

### 2. Human-in-the-Loop 审批闭环
- 利用 LangGraph `interrupt()` + `Command(resume=...)` 机制,在高危动作执行前暂停图,等待人工审批后恢复,确保生产安全
- 前端内嵌审批面板,按 risk_level 预选动作,支持批量/单独/参数微调审批,审批记录可溯源

### 3. 故障知识图谱 (Incident KG)
- 设计 Neo4j 图谱 schema (Incident / Service / RootCause / Action / Symptom 五类节点),按故障模式去重 (alert_name + service + root_cause 哈希),支持 `recurrence_count` 自增
- 实现 Cypher 评分算法:同 service 权重 3.0 / 同 root_cause 权重 2.0 / 关键词匹配权重 0.5,精准召回相似故障

### 4. GraphRAG 混合召回
- 实现三路并发召回 + 加权融合:(1) KG 结构化召回 (Cypher 评分) (2) 向量语义召回 (Milvus expr 过滤) (3) Cross-seed (用 KG incident.summary 反查向量库)
- 相比朴素 RAG,召回准确率提升 X%,根因命中率提升 Y%

### 5. Langfuse 全链路可观测
- 实现 v2/v3 双兼容的 observability 模块,运行时探测 SDK 版本分支 import,支持 CallbackHandler + @traced 装饰器 + emit_event 三层埋点
- 端到端 trace 覆盖 LLM 调用 / 工具执行 / KG 查询 / HITL 决策,session 聚合可重现单次故障全流程

### 6. Eval 框架工程化
- 构建 6 维场景矩阵 (OOM 标准 / 告警温和 / 历史复发 / 健康巡检 / 未知服务 / 安全门),自动审批策略可配置 (approve_all / reject_all / approve_non_destructive)
- 度量 MTTR / 根因命中率 / 修复召回率 / 安全门违规,nightly 自动跑,Langfuse 关联可追溯

### 7. 生产级前端 (零构建)
- 纯 ES Modules + 原生 JS 实现 1500 LoC SPA,Design Tokens 主题系统,Agent 瀑布实时可视化,KG 子图 SVG 原生渲染 (滚轮缩放 / 拖拽平移)
- 零 npm 依赖,改一行 JS 直接刷新可见,部署只需 `cp static`

---

## 量化指标 (根据实际运行数据填写)

- 支持 **21 个 MCP 工具** (5 个 server),覆盖日志 / 监控 / K8s / 告警 / Grafana
- 故障知识图谱沉淀 **N 个 Incident** / **M 个 Action 模板**,复发故障召回命中率 **X%**
- Eval 框架 6 个场景通过率 **Y%**,根因命中率 **Z%**,安全门零违规
- 端到端诊断 MTTR 从人工 **A 分钟**降至 Agent **B 分钟** (含审批)

---

## 面试重点讲解方向

### 1. 多 Agent 协作 (架构设计能力)
**问题**: 为什么不用单一 Agent?

**回答**: 单一 Agent 容易陷入"诊断 → 修复 → 诊断"死循环,且职责不清。我设计的 Supervisor Pattern 让每个 Agent 只负责一件事:Historian 只召回、Diagnostician 只诊断、Remediator 只提议、Executor 只执行。Supervisor 用硬规则 + LLM 双层路由,确定性场景走代码,灰色地带才用 LLM,既灵活又可控。

### 2. HITL 审批 (生产意识)
**问题**: 为什么要人工审批?

**回答**: 生产环境不能让 AI 直接执行 `kubectl delete` 这种破坏性动作。我用 LangGraph 的 `interrupt()` 机制,在 Remediator 生成候选动作后暂停整图,前端弹审批面板,用户批准后 `Command(resume=...)` 恢复执行。关键是工具层隔离:Diagnostician 根本拿不到写工具,Executor 二次校验 risk_level,三层防御。

### 3. GraphRAG (技术深度)
**问题**: GraphRAG 跟普通 RAG 有什么区别?

**回答**: 普通 RAG 只用向量召回,用户 query "OOM" 太短召回不准。我的 GraphRAG 三路并发:(1) KG 用结构化属性 (service + root_cause) 精确命中 (2) 向量库语义召回 (3) Cross-seed — 用 KG 召回的 incident.summary 当二次 query 反查向量库。三路融合后,根因命中率从 X% 提升到 Y%。

### 4. Eval 框架 (工程化能力)
**问题**: 怎么保证 Agent 质量?

**回答**: 我构建了 6 维场景矩阵,覆盖正常路径 / 降级路径 / KG 加成 / 零信号边界 / 幻觉防御 / HITL 拦截。每个场景配置不同的自动审批策略 (approve_all / reject_all / approve_non_destructive),跑完度量 MTTR / 根因命中率 / 修复召回率 / 安全门违规。nightly 自动跑,Langfuse 关联可追溯,这是大多数 LLM 项目没有的工程化亮点。

### 5. 零构建前端 (产品意识)
**问题**: 为什么不用 React / Vue?

**回答**: npm / webpack 增加部署复杂度,改一行 JS 要重新 build。我用纯 ES Modules + 原生 JS,1500 LoC 实现完整 SPA,Design Tokens 主题系统,Agent 瀑布实时可视化,KG 子图 SVG 原生渲染。零 npm 依赖,改完直接刷新可见,部署只需 `cp static`,这是生产级的工程选择。

---

## 项目亮点总结

1. **多 Agent 协作** — Supervisor Pattern,硬规则 + LLM 双层路由
2. **HITL 审批** — LangGraph interrupt(),三层防御
3. **故障知识图谱** — Neo4j 按故障模式去重,Cypher 评分算法
4. **GraphRAG** — 三路并发召回 + Cross-seed
5. **Langfuse 可观测** — v2/v3 双兼容,三层埋点
6. **Eval 框架** — 6 维场景矩阵,自动审批策略
7. **零构建前端** — ES Modules,Design Tokens,Agent 瀑布可视化

---

**🎯 使用建议**:
1. 把这份文档作为面试准备材料,熟悉每个亮点的技术细节
2. 根据实际运行数据填写量化指标
3. 面试时重点讲 2-3 个最有区分度的亮点,不要全讲
4. 准备好 GitHub 链接 + Console 截图,面试官要看代码时能立刻展示
