# AIOps 四个 Prompt 设计笔记

> 本文档基于 `app/agent/aiops/planner.py`、`executor.py`、`replanner.py` 实际代码绘制。
> 所有图均使用 **Mermaid** 语法,VSCode / GitHub / Typora 均可预览。
> ⚠️ Typora (Mermaid 9.1.2) 兼容性要点见文末。

---

## 1. 四个 Prompt 在状态图中的位置

```mermaid
flowchart TB
    USER(["用户任务输入"])

    subgraph PLANNER_NODE["planner 节点"]
        P1["Prompt 1: planner_prompt<br/>角色: 规划者<br/>注入: tools_description, experience_context<br/>输出: Plan(steps: List[str])"]
    end

    subgraph EXECUTOR_NODE["executor 节点"]
        P2["Prompt 2: 内嵌 SystemMessage<br/>角色: 执行者<br/>绑定工具 (bind_tools)<br/>输出: AIMessage 可能含 tool_calls"]
    end

    subgraph REPLANNER_NODE["replanner 节点"]
        P3["Prompt 3: replanner_prompt<br/>角色: 重新规划专家<br/>三选一决策<br/>输出: Act(action, new_steps)"]
        P4["Prompt 4: response_prompt<br/>角色: 报告生成器<br/>汇总 past_steps<br/>输出: Response(response: str Markdown)"]
    end

    END_NODE(["END (返回前端)"])

    USER --> P1
    P1 --> P2
    P2 --> P3
    P3 -->|"action == continue / replan"| P2
    P3 -->|"action == respond"| P4
    P4 --> END_NODE

    classDef prompt fill:#e8f5e9,stroke:#388e3c,color:#000
    classDef node fill:#bbdefb,stroke:#1565c0,color:#000
    classDef terminal fill:#f5f5f5,stroke:#616161,color:#000
    class P1,P2,P3,P4 prompt
    class PLANNER_NODE,EXECUTOR_NODE,REPLANNER_NODE node
    class USER,END_NODE terminal
```

**对应代码:**
- Prompt 1: `app/agent/aiops/planner.py:28-60`
- Prompt 2: `app/agent/aiops/executor.py:61-76` (SystemMessage 内嵌)
- Prompt 3: `app/agent/aiops/replanner.py:41-89`
- Prompt 4: `app/agent/aiops/replanner.py:91-108`

---

## 2. Planner Prompt 解剖

```mermaid
flowchart TB
    subgraph PLANNER["planner_prompt 组成结构"]
        direction TB
        ROLE["角色定位<br/>'作为一个专家级别的规划者'"]
        TOOLS["{tools_description} 占位符<br/>注入所有可用工具清单"]
        BOUND["职责边界<br/>'你的职责是制定计划<br/>实际调用由 Executor 负责'"]
        EXP["{experience_context} 占位符<br/>RAG 经验文档 (可为空)"]
        RULES["五条计划质量准则<br/>逻辑独立 / 明确工具 / 清晰依赖 / 可操作 / 参考经验"]
        FEW_SHOT["few-shot 示例<br/>'分析性能问题' 任务的示例计划"]
        MSGS["{messages} 占位符<br/>实际用户任务"]
    end

    OUT["LLM 输出 -> with_structured_output(Plan)<br/>得到 Plan(steps: List[str])"]

    PLANNER --> OUT

    NOTE_P["设计意图:<br/>- 角色 + 边界防止 Planner 越权<br/>- few-shot 学到 '步骤要带工具名' 模式<br/>- experience_context 实现 RAG 增强规划"]

    PLANNER -.-> NOTE_P

    classDef block fill:#fff9c4,stroke:#f9a825,color:#000
    classDef out fill:#c8e6c9,stroke:#2e7d32,color:#000
    classDef note fill:#e1bee7,stroke:#6a1b9a,color:#000
    class ROLE,TOOLS,BOUND,EXP,RULES,FEW_SHOT,MSGS block
    class OUT out
    class NOTE_P note
```

### Pydantic 输出契约

```python
class Plan(BaseModel):
    steps: List[str] = Field(
        description="完成任务所需的不同步骤..."
    )
```

---

## 3. Executor Prompt 解剖

```mermaid
flowchart TB
    subgraph EXECUTOR["executor SystemMessage 组成"]
        direction TB
        ROLE_E["角色定位<br/>'你是一个能力强大的助手'"]
        STEPS_E["四步执行流程<br/>1. 理解步骤目标<br/>2. 选择/使用指定工具<br/>3. 调用工具<br/>4. 返回结果"]
        GUARD_E["四条护栏<br/>- 失败说明原因<br/>- 不编造数据<br/>- 结果清晰准确<br/>- 专注当前步骤"]
        HUMAN_E["HumanMessage<br/>'请执行以下任务: {task}'"]
    end

    BIND["llm.bind_tools(all_tools)<br/>(本地 + MCP)"]
    LLM_CALL["第 1 次 LLM 调用<br/>看是否要 tool_calls"]
    TOOLNODE["如有 tool_calls:<br/>ToolNode.ainvoke 执行工具"]
    LLM_FINAL["第 2 次 LLM 调用<br/>把工具结果喂回, 拿最终回答"]
    OUT_E["返回:<br/>{plan: plan[1:],<br/>past_steps: [(task, result)]}"]

    EXECUTOR --> BIND
    BIND --> LLM_CALL
    LLM_CALL --> TOOLNODE
    TOOLNODE --> LLM_FINAL
    LLM_FINAL --> OUT_E

    NOTE_E["关键差异:<br/>- 没用 with_structured_output<br/>- 内部只跑一轮工具调用<br/>- 真正的循环在更高层 (executor 与 replanner 之间)"]

    EXECUTOR -.-> NOTE_E

    classDef block fill:#fff9c4,stroke:#f9a825,color:#000
    classDef llmstep fill:#bbdefb,stroke:#1565c0,color:#000
    classDef out fill:#c8e6c9,stroke:#2e7d32,color:#000
    classDef note fill:#e1bee7,stroke:#6a1b9a,color:#000
    class ROLE_E,STEPS_E,GUARD_E,HUMAN_E block
    class BIND,LLM_CALL,TOOLNODE,LLM_FINAL llmstep
    class OUT_E out
    class NOTE_E note
```

---

## 4. Replanner Prompt 解剖 (最复杂)

```mermaid
flowchart TB
    subgraph REPLANNER["replanner_prompt 组成结构"]
        direction TB
        ROLE_R["角色定位<br/>'作为一个重新规划专家'"]
        TOOLS_R["{tools_description} 占位符"]
        BOUND_R["职责边界 (同 Planner)"]
        OPT1["选项 1: respond [最高优先级]<br/>判据: past_steps >= 3 且关键信息齐<br/>或 past_steps >= 5 (无论结果)<br/>anti-pattern: 不要等完美"]
        OPT2["选项 2: continue [次优先级]<br/>判据: 剩余步骤确实必需<br/>anti-pattern: 不必需就 respond"]
        OPT3["选项 3: replan [最低优先级]<br/>判据: 原计划明显错误<br/>限制: new_steps <= 剩余 plan 数<br/>past_steps >= 5 时禁用"]
        CRITERIA["四条评估标准<br/>(信息够吗 / 已成功吗 / 剩余必需吗 / 步数过多吗)"]
        SLOGAN["决策口诀 (放在 prompt 末尾)<br/>'优先结束 > 保持不变 > 调整计划'<br/>'信息足够就响应, 不要追求完美'"]
    end

    OUT_R["LLM 输出 -> with_structured_output(Act)<br/>得到 Act(action, new_steps)"]

    REPLANNER --> OUT_R

    classDef block fill:#fff9c4,stroke:#f9a825,color:#000
    classDef priority fill:#c8e6c9,stroke:#2e7d32,color:#000
    classDef out fill:#bbdefb,stroke:#1565c0,color:#000
    class ROLE_R,TOOLS_R,BOUND_R,CRITERIA,SLOGAN block
    class OPT1,OPT2,OPT3 priority
    class OUT_R out
```

### 这个 Prompt 的 5 个精彩设计

```mermaid
flowchart LR
    D1["1. 显式标注优先级<br/>用 [最高/次/最低] 标签<br/>对抗 LLM '想多做事' 的倾向"]
    D2["2. 量化决策标准<br/>用具体数字 >= 3, >= 5, >= 8<br/>比模糊形容词强 10 倍"]
    D3["3. anti-pattern 显式声明<br/>'不要等完美'<br/>'不必需就 respond'"]
    D4["4. 收尾口诀<br/>放在 prompt 最末尾<br/>利用 LLM 近因效应"]
    D5["5. 软约束 + 代码硬护栏<br/>prompt 99 percent 生效<br/>代码 100 percent 兜底"]

    D1 --> D2 --> D3 --> D4 --> D5

    classDef tip fill:#ffe0b2,stroke:#e65100,color:#000
    class D1,D2,D3,D4,D5 tip
```

### Pydantic 输出契约

```python
class Act(BaseModel):
    action: str  # 'continue' | 'replan' | 'respond'
    new_steps: List[str]  # 仅 replan 时使用
```

---

## 5. Response Prompt 解剖 (最简短)

```mermaid
flowchart TB
    subgraph RESPONSE["response_prompt 组成结构"]
        direction TB
        ROLE_RES["简单指令<br/>'根据原始任务和已执行步骤<br/>生成全面的最终响应'"]
        REQ["四条响应要求<br/>清晰结构化 / 基于实际数据 / 失败诚实说明 / Markdown 格式"]
    end

    USER_TASK["用户任务里嵌入的<br/>具体 Markdown 模板<br/>(70+ 行业务格式要求)"]

    OUT_RES["LLM 输出 -> with_structured_output(Response)<br/>得到 Response(response: str)<br/>response 内容是 Markdown 字符串"]

    RESPONSE --> OUT_RES
    USER_TASK --> OUT_RES

    NOTE_RES["分层设计:<br/>- 通用要求写在 response_prompt (可复用)<br/>- 业务特定 Markdown 模板写在用户任务里 (一次性)<br/>这样 response_prompt 能给任何 Plan-Execute-Replan 任务复用"]

    OUT_RES -.-> NOTE_RES

    classDef block fill:#fff9c4,stroke:#f9a825,color:#000
    classDef task fill:#bbdefb,stroke:#1565c0,color:#000
    classDef out fill:#c8e6c9,stroke:#2e7d32,color:#000
    classDef note fill:#e1bee7,stroke:#6a1b9a,color:#000
    class ROLE_RES,REQ block
    class USER_TASK task
    class OUT_RES out
    class NOTE_RES note
```

---

## 6. with_structured_output 工作原理

```mermaid
flowchart LR
    PROMPT["Prompt + Pydantic 类<br/>Plan / Act / Response"]

    LC["LangChain 内部<br/>把 Pydantic 转成虚拟 'tool'<br/>schema = JSON Schema"]

    LLM_FC["LLM 走 function calling<br/>被强制用这个虚拟 tool 输出"]

    PARSE["LangChain 自动解析<br/>tool_calls -> Pydantic 实例"]

    CODE["Python 代码消费<br/>plan.steps / act.action / response.response"]

    PROMPT --> LC
    LC --> LLM_FC
    LLM_FC --> PARSE
    PARSE --> CODE

    NOTE_FC["好处:<br/>- 不需要正则解析 LLM 输出<br/>- 类型安全, 字段缺失会重试<br/>- LLM 不会在头尾加废话"]

    PARSE -.-> NOTE_FC

    classDef step fill:#bbdefb,stroke:#1565c0,color:#000
    classDef note fill:#fff9c4,stroke:#f9a825,color:#000
    class PROMPT,LC,LLM_FC,PARSE,CODE step
    class NOTE_FC note
```

---

## 7. 软约束 (Prompt) vs 硬护栏 (代码) 双层防御

```mermaid
flowchart TB
    LLM_DECISION["LLM 决策"]

    SOFT["软约束: replanner_prompt<br/>'past_steps >= 5 时禁止 replan'<br/>'优先 respond'<br/>(LLM 99 percent 会遵守)"]

    HARD1["硬护栏 1: replanner.py:131<br/>if past_steps >= 8:<br/>  强制 _generate_response"]

    HARD2["硬护栏 2: replanner.py:208<br/>if len(new_steps) > len(plan):<br/>  截断 new_steps"]

    HARD3["硬护栏 3: replanner.py:216<br/>if past_steps >= 5 and action == replan:<br/>  强制 _generate_response"]

    LLM_DECISION --> SOFT
    SOFT --> HARD1
    HARD1 --> HARD2
    HARD2 --> HARD3
    HARD3 --> FINAL["实际执行的决策<br/>(LLM 想法 + 代码兜底)"]

    NOTE_H["生产 Agent 必须双层防御:<br/>- 只靠 prompt 会被 LLM 偶尔违反<br/>- 只靠代码 LLM 决策不智能<br/>- 两者结合才稳健"]

    FINAL -.-> NOTE_H

    classDef soft fill:#bbdefb,stroke:#1565c0,color:#000
    classDef hard fill:#ffcdd2,stroke:#c62828,color:#000
    classDef final fill:#c8e6c9,stroke:#2e7d32,color:#000
    classDef note fill:#fff9c4,stroke:#f9a825,color:#000
    class LLM_DECISION,SOFT soft
    class HARD1,HARD2,HARD3 hard
    class FINAL final
    class NOTE_H note
```

---

## 8. Prompt 设计 5 条心法 (写好任何多步 Agent 的通用准则)

```mermaid
flowchart TB
    H1["心法 1: 角色显式化<br/>每个 prompt 第一句<br/>'作为 XX, 你需要...'"]
    H2["心法 2: 职责边界明确<br/>Planner / Replanner 强调<br/>'实际调用由 Executor 负责'"]
    H3["心法 3: 量化标准胜过形容词<br/>用 >= 3 / >= 5 / >= 8<br/>而非 '差不多够了'"]
    H4["心法 4: anti-pattern 显式声明<br/>'不要等完美'<br/>纠正 LLM 已知偏好"]
    H5["心法 5: 软约束 + 硬护栏<br/>prompt 引导 + 代码兜底"]

    H1 --> H2 --> H3 --> H4 --> H5

    classDef heart fill:#ffe0b2,stroke:#e65100,color:#000
    class H1,H2,H3,H4,H5 heart
```

---

## 9. 四个 Prompt 对比表

| 维度 | Planner | Executor | Replanner | Response |
|---|---|---|---|---|
| **代码位置** | `planner.py:28-60` | `executor.py:61-76` | `replanner.py:41-89` | `replanner.py:91-108` |
| **实现形式** | `ChatPromptTemplate` | 内嵌 `SystemMessage` | `ChatPromptTemplate` | `ChatPromptTemplate` |
| **结构化输出** | `Plan` | 无 (开放对话) | `Act` | `Response` |
| **绑工具** | 否 | 是 (`bind_tools`) | 否 | 否 |
| **复杂度** | 中等 | 简单 | 高 (有决策树) | 极简 |
| **调用次数** | 每任务 1 次 | 每步 1-2 次 | 每步 1 次 | 每任务 1 次 (终态) |

---

## 10. 编辑提示

| 想改什么 | 改哪里 |
|---|---|
| 角色风格 (改 Agent 个性) | 第 2/3/4/5 节对应 ROLE 节点 + 同步代码 |
| 质量准则 / 评估标准 | 第 4 节 CRITERIA 节点 + replanner.py |
| 优先级数字阈值 (3/5/8) | 第 4 节 OPT1/OPT2/OPT3 + replanner.py:130-218 |
| 加新决策选项 (如 rollback) | 第 4 节加 OPT4 + Act schema 改 + 状态图改 |
| 改业务报告模板 | 第 5 节 USER_TASK 节点 + aiops_service.py:174 |

Mermaid 语法参考: <https://mermaid.js.org/intro/>

---

## Typora (Mermaid 9.1.2) 兼容性要点

- 节点文本含中文/特殊符号 (`?`, `>=`, 冒号, 括号) 必须用双引号包裹
- 不要使用 Unicode 圈数字 `①②` 或箭头 `→ ≥ ←`,改成 ASCII (`>= -> 1 2`)
- 不要在 `classDiagram` 用多行 `note for`
- `**bold**` 在 9.1.2 不渲染但不报错, 显示原文 (本文档已避免使用)
- 边标签含中文一律用 `-->|"标签"|` 形式包双引号

---

## 附: 关键代码文件对应

| 文件 | 在图中的位置 |
|---|---|
| `app/agent/aiops/planner.py` | 第 1 节 P1, 第 2 节 |
| `app/agent/aiops/executor.py` | 第 1 节 P2, 第 3 节 |
| `app/agent/aiops/replanner.py` (replanner_prompt) | 第 1 节 P3, 第 4 节, 第 7 节 |
| `app/agent/aiops/replanner.py` (response_prompt) | 第 1 节 P4, 第 5 节 |
| `app/services/aiops_service.py` (diagnose 任务模板) | 第 5 节 USER_TASK |
