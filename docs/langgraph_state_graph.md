# LangGraph 状态图笔记 - AIOps Plan-Execute-Replan

> 本文档基于项目 `app/services/aiops_service.py` 和 `app/agent/aiops/` 实际代码绘制。
> 所有图均使用 **Mermaid** 语法,可在 VSCode (装 Markdown Preview Mermaid Support 插件) /
> GitHub / Typora 中直接预览,文字想改直接改即可。

---

## 1. 状态图总览(节点 + 边 + 路由)

```mermaid
flowchart TD
    START([START]) --> PLANNER

    PLANNER["**planner 节点**<br/>📥 读 state.input<br/>🤖 调 LLM 生成步骤列表<br/>📤 写 state.plan"]
    EXECUTOR["**executor 节点**<br/>📥 读 state.plan[0]<br/>🤖 LLM + ToolNode 调用工具<br/>📤 弹出 plan[0]<br/>📤 追加 past_steps"]
    REPLANNER["**replanner 节点**<br/>📥 读 plan / past_steps<br/>🤖 LLM 决策 continue/replan/respond<br/>📤 视情况写 plan / response"]

    PLANNER -->|add_edge<br/>无条件| EXECUTOR
    EXECUTOR -->|add_edge<br/>无条件| REPLANNER
    REPLANNER -->|conditional_edges<br/>should_continue| ROUTER{should_continue}

    ROUTER -->|state.response 非空<br/>= 已生成最终报告| END([END])
    ROUTER -->|state.plan 还有剩余<br/>且 response 为空| EXECUTOR
    ROUTER -->|plan 空 + response 空<br/>= 兜底结束| END

    classDef node fill:#e1f5ff,stroke:#0288d1,stroke-width:2px,color:#000
    classDef router fill:#fff3e0,stroke:#f57c00,stroke-width:2px,color:#000
    classDef terminal fill:#f5f5f5,stroke:#616161,stroke-width:2px,color:#000
    class PLANNER,EXECUTOR,REPLANNER node
    class ROUTER router
    class START,END terminal
```

**对应代码位置:**
- 图骨架: `app/services/aiops_service.py::_build_graph` (29-79 行)
- 路由函数: `should_continue` (49-64 行)

---

## 2. State 数据结构(共享黑板)

```mermaid
flowchart TB
    subgraph STATE["PlanExecuteState (TypedDict)"]
        direction TB
        F1["**input** : str<br/>用户原始任务,只读"]
        F2["**plan** : List[str]<br/>待执行步骤列表<br/>会被 executor 弹出 / replanner 替换<br/>合并语义: 覆盖"]
        F3["**past_steps** : Annotated[List[tuple], operator.add]<br/>已执行历史 [(任务, 结果), ...]<br/>⭐ 合并语义: 自动追加(append),不覆盖"]
        F4["**response** : str<br/>最终 Markdown 报告<br/>非空就触发 END"]
    end

    classDef field fill:#e8f5e9,stroke:#2e7d32,stroke-width:1px,color:#000
    classDef special fill:#fff9c4,stroke:#f9a825,stroke-width:2px,color:#000
    class F1,F2,F4 field
    class F3 special
```

**对应代码:** `app/agent/aiops/state.py`

### 字段读写矩阵

| 字段 | planner | executor | replanner | 合并语义 |
|---|:---:|:---:|:---:|---|
| `input` | 读 | - | 读 | 覆盖(初始化时写) |
| `plan` | **写** | 读+**写** | 读+**写** | 覆盖 |
| `past_steps` | - | **写(追加)** | 读 | **追加(operator.add)** |
| `response` | - | - | **写** | 覆盖 |

---

## 3. 节点返回值 → 状态合并(Reducer 机制)

```mermaid
flowchart LR
    NODE["节点函数<br/>async def executor(state):<br/>...<br/>return {'plan': plan[1:],<br/>'past_steps': [(task, result)]}"]
    
    DIFF["diff dict<br/>(部分更新)"]
    
    ENGINE{"LangGraph 引擎<br/>合并算法"}
    
    OLD_STATE["旧 state<br/>plan: [A, B, C]<br/>past_steps: [(X, ...)]"]
    NEW_STATE["新 state<br/>plan: [B, C] (覆盖)<br/>past_steps: [(X, ...), (A, ...)] (追加)"]
    
    NODE --> DIFF
    DIFF --> ENGINE
    OLD_STATE --> ENGINE
    ENGINE --> NEW_STATE
    
    NOTE["规则:<br/>① 字段有 Annotated[..., reducer] → 用 reducer 合并<br/>② 否则直接覆盖<br/>③ 节点没返回的字段 → 保持不变"]
    ENGINE -.参考.-> NOTE
    
    classDef code fill:#f3e5f5,stroke:#7b1fa2
    classDef state fill:#e8f5e9,stroke:#388e3c
    class NODE,DIFF code
    class OLD_STATE,NEW_STATE state
```

---

## 4. 一次完整执行的状态时序(以"诊断告警"任务为例)

```mermaid
sequenceDiagram
    autonumber
    participant API as /api/aiops 接口
    participant G as StateGraph 引擎
    participant P as planner 节点
    participant E as executor 节点
    participant R as replanner 节点
    participant CP as MemorySaver<br/>(checkpointer)

    API->>G: astream(initial_state, thread_id=session)
    Note over G: state = {input:"诊断告警...",<br/>plan:[], past_steps:[], response:""}

    G->>P: 调用 planner(state)
    P->>P: LLM 生成 Plan(steps=[s1, s2, s3])
    P-->>G: return {"plan": [s1, s2, s3]}
    G->>CP: 保存 checkpoint
    Note over G: state.plan = [s1, s2, s3]
    G-->>API: yield {"planner": diff}  (stream)

    rect rgb(230, 245, 255)
    Note over G,R: 执行循环 (executor ↔ replanner)
    
    G->>E: 调用 executor(state)
    E->>E: 取 task = plan[0] = s1<br/>调工具,得 r1
    E-->>G: return {"plan": [s2, s3],<br/>"past_steps": [(s1, r1)]}
    Note over G: plan 覆盖, past_steps 追加
    G->>CP: 保存 checkpoint
    G-->>API: yield {"executor": diff}

    G->>R: 调用 replanner(state)
    R->>R: LLM 决策 → "continue"
    R-->>G: return {}  (不改 state)
    G->>CP: 保存 checkpoint
    G-->>API: yield {"replanner": diff}

    Note over G: should_continue:<br/>response 空 + plan 非空 → executor
    
    G->>E: 调用 executor(state)
    Note over E: 处理 s2... (同上流程)
    E-->>G: return {plan:[s3], past_steps:[(s2,r2)]}
    
    G->>R: 调用 replanner(state)
    R->>R: past_steps 已 2 条<br/>LLM 决策 → "respond"
    R->>R: _generate_response<br/>用 past_steps 拼 Markdown
    R-->>G: return {"response": "# 报告..."}
    end

    Note over G: should_continue:<br/>state.response 非空 → END
    G-->>API: yield {"replanner": {response: ...}}
    
    API->>G: get_state(thread_id) 取最终态
    G->>CP: 读最终 checkpoint
    CP-->>API: final_state.values["response"]
```

---

## 5. Replanner 三选一决策树(细节)

```mermaid
flowchart TD
    START(["replanner 入口"])

    CHECK_MAX{"past_steps 长度 大于等于 8 ?"}
    GEN1["强制 _generate_response<br/>return response"]

    CHECK_PLAN{"plan 还有剩余 ?"}

    CALL_LLM["LLM 输出 Act<br/>action: continue / replan / respond"]

    ACT_RESPOND{"action == respond ?"}
    GEN2["_generate_response<br/>return response"]

    ACT_REPLAN{"action == replan ?"}
    REPLAN_GUARD{"past_steps 大于等于 5 ?"}
    NEW_STEPS["截断 new_steps<br/>长度不超过当前剩余 plan<br/>return new plan"]
    GEN3["_generate_response<br/>return response"]

    ACT_CONTINUE["默认 continue<br/>return 空 dict"]

    NO_PLAN["plan 为空<br/>_generate_response"]

    START --> CHECK_MAX
    CHECK_MAX -->|是| GEN1
    CHECK_MAX -->|否| CHECK_PLAN

    CHECK_PLAN -->|是| CALL_LLM
    CHECK_PLAN -->|否| NO_PLAN

    CALL_LLM --> ACT_RESPOND
    ACT_RESPOND -->|是| GEN2
    ACT_RESPOND -->|否| ACT_REPLAN

    ACT_REPLAN -->|是| REPLAN_GUARD
    ACT_REPLAN -->|否| ACT_CONTINUE

    REPLAN_GUARD -->|是| GEN3
    REPLAN_GUARD -->|否| NEW_STEPS

    GEN1 --> END_NODE(["should_continue -> END"])
    GEN2 --> END_NODE
    GEN3 --> END_NODE
    NO_PLAN --> END_NODE
    NEW_STEPS --> EXEC_NODE(["should_continue -> executor"])
    ACT_CONTINUE --> EXEC_NODE

    classDef decision fill:#fff3e0,stroke:#f57c00,color:#000
    classDef respond fill:#c8e6c9,stroke:#2e7d32,color:#000
    classDef loop fill:#bbdefb,stroke:#1565c0,color:#000
    class CHECK_MAX,CHECK_PLAN,ACT_RESPOND,ACT_REPLAN,REPLAN_GUARD decision
    class GEN1,GEN2,GEN3,NO_PLAN,END_NODE respond
    class NEW_STEPS,ACT_CONTINUE,EXEC_NODE loop
```

**对应代码:** `app/agent/aiops/replanner.py::replanner` (111-239 行)

**关键护栏(防止 LLM 把循环开飞):**
- 总步数 ≥ 8 → 强制收尾
- 已执行 ≥ 5 步时 → 禁止 replan,只能 respond
- replan 的 `new_steps` 数量必须 ≤ 当前剩余步骤数(否则截断)

---

## 6. StateGraph 编译产物的运行时结构

```mermaid
flowchart TB
    subgraph COMPILED["compiled_graph (StateGraph.compile 产物)"]
        direction TB
        NODES["nodes 字典<br/>{<br/>  'planner': planner_fn,<br/>  'executor': executor_fn,<br/>  'replanner': replanner_fn<br/>}"]
        EDGES["edges 列表<br/>[<br/>  ('__start__', 'planner'),<br/>  ('planner', 'executor'),<br/>  ('executor', 'replanner')<br/>]"]
        COND["conditional_edges<br/>{<br/>  'replanner': should_continue<br/>}"]
        REDUCERS["state schema<br/>+ 各字段 reducer 表<br/>(从 Annotated 提取)"]
    end

    subgraph RUNTIME["运行时"]
        STATE["当前 state<br/>(每次合并后的最新值)"]
        CP["checkpointer<br/>= MemorySaver<br/>{thread_id: [state快照,...]}"]
        STREAM["事件流<br/>(yield 给 astream)"]
    end

    COMPILED --> RUNTIME
    NODES -.被引擎调度.-> STATE
    REDUCERS -.合并 diff.-> STATE
    STATE -.每步保存.-> CP
    STATE -.diff 推送.-> STREAM

    classDef config fill:#e1bee7,stroke:#6a1b9a
    classDef rt fill:#ffe0b2,stroke:#e65100
    class NODES,EDGES,COND,REDUCERS config
    class STATE,CP,STREAM rt
```

---

## 7. 与 RAG Agent (`create_agent`) 的对比

```mermaid
flowchart LR
    subgraph RAG["RAG Agent (rag_agent_service.py)"]
        direction TB
        CA["create_agent(model, tools, checkpointer)"]
        REACT["内置 ReAct 状态图:<br/>agent ↔ tools 自动循环"]
        CA --> REACT
    end

    subgraph AIOPS["AIOps (aiops_service.py)"]
        direction TB
        SG["手写 StateGraph"]
        PER["planner → executor → replanner<br/>显式编排,可控可观察"]
        SG --> PER
    end

    NOTE1["适合场景:<br/>简单问答+工具调用<br/>开箱即用"]
    NOTE2["适合场景:<br/>多步推理+人工护栏<br/>(步数限制/护栏决策)"]
    
    RAG -.-> NOTE1
    AIOPS -.-> NOTE2
```

---

## 8. 编辑提示

如果你要改这个图谱:

| 想改什么 | 改哪里 |
|---|---|
| 新增一个节点(比如加 verifier) | 第 1 节 flowchart 加一个方块,加一条 add_edge |
| 字段含义变化 | 第 2 节 classDiagram 里改 note |
| 决策逻辑增减分支 | 第 5 节 flowchart 加判断节点 |
| 改护栏数字(MAX_STEPS / 5 步上限) | 第 5 节文字描述 + replanner.py 同步 |

Mermaid 完整语法参考: <https://mermaid.js.org/intro/>

---

## 附:每个文件对应到图的哪部分

| 文件 | 在图中的位置 |
|---|---|
| `app/agent/aiops/state.py` | 第 2 节 State 类图 |
| `app/agent/aiops/planner.py` | 第 1 节 PLANNER 节点 |
| `app/agent/aiops/executor.py` | 第 1 节 EXECUTOR 节点 |
| `app/agent/aiops/replanner.py` | 第 1 节 REPLANNER + 第 5 节决策树 |
| `app/services/aiops_service.py` | 第 1 节图骨架 + 第 4 节时序 + 第 6 节运行时 |
