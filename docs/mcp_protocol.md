# MCP 协议笔记 - 从认知模型到项目实践

> 本文档基于项目 `mcp_servers/` 和 `app/agent/mcp_client.py` 实际代码绘制。
> 所有图均使用 **Mermaid** 语法,VSCode / GitHub / Typora 均可预览。
> ⚠️ 编辑时若 Typora (Mermaid 9.1.2) 报错,所有含中文/特殊符号的节点文本一律用双引号包裹。

---

## 1. 大局图: 两层协议的清晰分工

```mermaid
flowchart TB
    USER(["用户 (浏览器)"])

    subgraph HOST["Host (你的 FastAPI + LangGraph Agent)"]
        direction TB
        LLM["LLM<br/>(ChatQwen / GPT / Claude)"]
        AGENT["Agent 编排代码<br/>(LangGraph ReAct / Plan-Execute)"]
        TOOLNODE["ToolNode<br/>本地工具 + MCP 包装工具"]
        MCPCLIENT["MultiServerMCPClient<br/>(MCP Client 池)"]
    end

    subgraph SERVERS["MCP Servers (独立进程)"]
        direction TB
        S1["cls_server.py<br/>FastMCP, port 8003"]
        S2["monitor_server.py<br/>FastMCP, port 8004"]
    end

    EXT["真实业务系统<br/>(CLS / Prometheus / DB / ...)"]

    USER -->|HTTP| AGENT
    AGENT --> LLM
    LLM -.->|"协议 1"| AGENT
    AGENT --> TOOLNODE
    TOOLNODE --> MCPCLIENT
    MCPCLIENT -.->|"协议 2"| S1
    MCPCLIENT -.->|"协议 2"| S2
    S1 --> EXT
    S2 --> EXT

    P1["协议 1: function calling<br/>LLM 表达 '我要调哪个工具'<br/>由 LLM 厂商定义 (OpenAI/通义/Claude)<br/>LLM 完全不知道 MCP 存在"]
    P2["协议 2: MCP (JSON-RPC 2.0)<br/>Host 真正去调工具<br/>由 Anthropic 定义, 开源标准<br/>工具与应用解耦, 可插拔"]

    LLM -.-> P1
    MCPCLIENT -.-> P2

    classDef host fill:#e1f5ff,stroke:#0288d1,color:#000
    classDef server fill:#fff3e0,stroke:#f57c00,color:#000
    classDef ext fill:#f5f5f5,stroke:#616161,color:#000
    classDef note fill:#fff9c4,stroke:#f9a825,color:#000
    class LLM,AGENT,TOOLNODE,MCPCLIENT host
    class S1,S2 server
    class USER,EXT ext
    class P1,P2 note
```

**记住三句话:**
- function calling 是 **LLM ↔ Host** 之间的协议, 跟 MCP 无关
- MCP 是 **Host ↔ Tool Server** 之间的协议, LLM 完全无感
- MCP Client 在中间做的是 **格式翻译**, 不是 LLM 直接说 MCP

---

## 2. MCP 的三个角色

```mermaid
flowchart LR
    subgraph HOST_ROLE["Host 角色"]
        direction TB
        HOST_DESC["跑 LLM 的应用<br/>负责整体编排<br/>例: Claude Desktop, Cursor, 你的 FastAPI"]
    end

    subgraph CLIENT_ROLE["Client 角色"]
        direction TB
        CLIENT_DESC["Host 内部的 MCP 协议客户端<br/>一个 Client 一对一连一个 Server<br/>例: MultiServerMCPClient 内含两个 Client"]
    end

    subgraph SERVER_ROLE["Server 角色"]
        direction TB
        SERVER_DESC["暴露能力的独立服务<br/>提供 tools / resources / prompts<br/>例: cls_server.py, monitor_server.py"]
    end

    HOST_ROLE --> CLIENT_ROLE
    CLIENT_ROLE -.MCP JSON-RPC.-> SERVER_ROLE

    classDef host fill:#bbdefb,stroke:#1565c0,color:#000
    classDef client fill:#c8e6c9,stroke:#2e7d32,color:#000
    classDef server fill:#ffe0b2,stroke:#e65100,color:#000
    class HOST_ROLE,HOST_DESC host
    class CLIENT_ROLE,CLIENT_DESC client
    class SERVER_ROLE,SERVER_DESC server
```

| 角色 | 在你项目里 | 关键代码 |
|---|---|---|
| Host | 整个 FastAPI 服务 | `app/main.py` |
| Client | `MultiServerMCPClient` 实例 | `app/agent/mcp_client.py` |
| Server | 独立 Python 进程 | `mcp_servers/cls_server.py`, `monitor_server.py` |

---

## 3. 传输层与协议方法

```mermaid
flowchart TB
    subgraph TRANSPORT["传输层 (三选一)"]
        direction LR
        T1["stdio<br/>子进程 + stdin/stdout<br/>适合本地 CLI 工具"]
        T2["sse<br/>HTTP + Server-Sent Events<br/>(老版本协议)"]
        T3["streamable-http<br/>HTTP 单端点流式<br/>(新版本协议, 推荐)"]
    end

    subgraph METHODS["MCP 协议核心方法 (JSON-RPC 2.0)"]
        direction TB
        M0["initialize<br/>握手, 协商版本与能力"]
        M1["tools/list<br/>列出该 Server 所有工具"]
        M2["tools/call<br/>实际调用某个工具"]
        M3["resources/list<br/>列出可读资源"]
        M4["prompts/list<br/>列出可复用 prompt 模板"]
    end

    TRANSPORT --> METHODS

    PROJ_NOTE["项目里:<br/>cls_server.py / monitor_server.py 用 streamable-http<br/>腾讯云 CLS 网关用 sse<br/>对应 .env 中 MCP_xxx_TRANSPORT 字段"]

    METHODS -.-> PROJ_NOTE

    classDef trans fill:#e1bee7,stroke:#6a1b9a,color:#000
    classDef meth fill:#bbdefb,stroke:#1565c0,color:#000
    classDef note fill:#fff9c4,stroke:#f9a825,color:#000
    class T1,T2,T3 trans
    class M0,M1,M2,M3,M4 meth
    class PROJ_NOTE note
```

### JSON-RPC 消息样例

`tools/call` 请求:

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "method": "tools/call",
  "params": {
    "name": "search_log",
    "arguments": {
      "topic_id": "topic-001",
      "start_time": 1708011445000,
      "end_time": 1708012345000
    }
  }
}
```

响应:

```json
{
  "jsonrpc": "2.0",
  "id": 42,
  "result": {
    "content": [{"type": "text", "text": "{...日志结果...}"}]
  }
}
```

---

## 4. 一次完整调用的时序图

> 假设用户问: "查最近 15 分钟 data-sync-service 的日志"

```mermaid
sequenceDiagram
    autonumber
    participant U as 用户
    participant HOST as Host (FastAPI Agent)
    participant CLIENT as MCP Client
    participant SERVER as cls_server.py
    participant LLM as LLM (ChatQwen)

    Note over HOST,SERVER: 阶段 A: Host 启动时一次性发现工具

    HOST->>CLIENT: 创建 MultiServerMCPClient
    CLIENT->>SERVER: initialize (握手)
    SERVER-->>CLIENT: 协议版本 + 能力
    CLIENT->>SERVER: tools/list
    SERVER-->>CLIENT: [search_log, get_current_timestamp, ...]
    CLIENT-->>HOST: 工具列表 (转成 LangChain BaseTool)

    Note over HOST: 缓存工具 schema, Agent 初始化完成

    Note over U,LLM: 阶段 B: 用户对话, ReAct 循环开始

    U->>HOST: 查最近 15 分钟日志
    HOST->>LLM: messages + tools schema (function calling 格式)
    Note over HOST,LLM: 协议①: function calling
    LLM-->>HOST: tool_calls = [search_log(topic_id, start_time, end_time)]

    Note over HOST,SERVER: 阶段 C: 真正执行工具

    HOST->>CLIENT: 转发工具调用
    CLIENT->>SERVER: tools/call (JSON-RPC)
    Note over CLIENT,SERVER: 协议②: MCP
    SERVER->>SERVER: 执行 Python 函数 search_log
    SERVER-->>CLIENT: 工具结果 JSON
    CLIENT-->>HOST: 包装成 ToolMessage

    Note over HOST,LLM: 阶段 D: LLM 总结回答

    HOST->>LLM: messages + tool_result
    LLM-->>HOST: 自然语言答案
    HOST-->>U: 最近 15 分钟有 N 条日志, ...
```

**关键观察:**
- 阶段 A 只在启动时跑一次, 之后工具列表被缓存
- 阶段 B 是 **function calling**, 阶段 C 是 **MCP**
- LLM 不知道 server 在哪, 它只看到一份工具 schema

---

## 5. function calling 与 MCP 的格式翻译

```mermaid
flowchart LR
    subgraph MCP_SIDE["MCP 侧 (tools/list 返回)"]
        direction TB
        MCP_TOOL["{<br/>  'name': 'search_log',<br/>  'description': '基于参数搜索日志',<br/>  'inputSchema': {<br/>    'type': 'object',<br/>    'properties': {...},<br/>    'required': [...]<br/>  }<br/>}"]
    end

    ADAPTER["langchain-mcp-adapters<br/>(MultiServerMCPClient 内部)"]

    subgraph LC_SIDE["LangChain 侧 (BaseTool)"]
        direction TB
        LC_TOOL["BaseTool 实例<br/>name, description, args_schema<br/>调用时内部走 MCP tools/call"]
    end

    subgraph LLM_SIDE["LLM 侧 (function calling)"]
        direction TB
        FC_TOOL["{<br/>  'type': 'function',<br/>  'function': {<br/>    'name': 'search_log',<br/>    'description': '...',<br/>    'parameters': {JSON Schema}<br/>  }<br/>}"]
    end

    MCP_SIDE --> ADAPTER
    ADAPTER --> LC_SIDE
    LC_SIDE --> LLM_SIDE

    NOTE_T["三种 schema 表达的是同一个工具<br/>只是包装格式不同"]
    LLM_SIDE -.-> NOTE_T

    classDef mcp fill:#ffe0b2,stroke:#e65100,color:#000
    classDef adapter fill:#e1bee7,stroke:#6a1b9a,color:#000
    classDef lc fill:#c8e6c9,stroke:#2e7d32,color:#000
    classDef llm fill:#bbdefb,stroke:#1565c0,color:#000
    classDef note fill:#fff9c4,stroke:#f9a825,color:#000
    class MCP_SIDE,MCP_TOOL mcp
    class ADAPTER adapter
    class LC_SIDE,LC_TOOL lc
    class LLM_SIDE,FC_TOOL llm
    class NOTE_T note
```

---

## 6. 自己写一个 MCP Server (FastMCP 极简范式)

```mermaid
flowchart TB
    START(["写一个新 MCP Server"])

    S1["1. 安装 fastmcp<br/>pip install fastmcp"]
    S2["2. 创建 FastMCP 实例<br/>mcp = FastMCP('your_name')"]
    S3["3. 定义工具函数<br/>用 @mcp.tool() 装饰<br/>函数签名 -> JSON Schema<br/>docstring -> 工具描述"]
    S4["4. 启动 server<br/>mcp.run(transport='streamable-http',<br/>host=..., port=..., path='/mcp')"]
    S5["5. 在 .env 配置 URL<br/>MCP_XXX_TRANSPORT=streamable-http<br/>MCP_XXX_URL=http://localhost:PORT/mcp"]
    S6["6. 重启 Host (FastAPI)<br/>Agent 自动发现新工具"]

    START --> S1 --> S2 --> S3 --> S4 --> S5 --> S6

    NOTE_AUTO["FastMCP 自动做的事:<br/>- 从函数签名生成 inputSchema<br/>- 从 docstring 抽 description<br/>- 路由 tools/list 和 tools/call<br/>- 处理 JSON-RPC 协议细节"]

    S3 -.-> NOTE_AUTO

    classDef step fill:#e8f5e9,stroke:#388e3c,color:#000
    classDef start fill:#bbdefb,stroke:#1565c0,color:#000
    classDef note fill:#fff9c4,stroke:#f9a825,color:#000
    class S1,S2,S3,S4,S5,S6 step
    class START start
    class NOTE_AUTO note
```

### 最小可运行示例

```python
# mcp_servers/weather_server.py
from fastmcp import FastMCP

mcp = FastMCP("Weather")

@mcp.tool()
def get_weather(city: str) -> dict:
    """查询某城市天气

    Args:
        city: 城市名, 如 北京
    Returns:
        包含 temperature 和 condition 的字典
    """
    fake = {"北京": {"temperature": 22, "condition": "晴"}}
    return fake.get(city, {"error": f"未知城市: {city}"})

if __name__ == "__main__":
    mcp.run(transport="streamable-http",
            host="127.0.0.1", port=8005, path="/mcp")
```

启动: `python mcp_servers/weather_server.py`

`.env` 加配置:
```
MCP_WEATHER_TRANSPORT=streamable-http
MCP_WEATHER_URL=http://localhost:8005/mcp
```

⚠️ 注意: `app/config.py` 的 `mcp_servers` 属性写死了 CLS / Monitor 两个 server, 加新 server 还需在 config 里加一份。

---

## 7. M×N 问题 (MCP 为什么被发明)

```mermaid
flowchart TB
    subgraph BEFORE["没有 MCP: M x N 个集成"]
        direction LR
        A1["LLM 应用 A"] --> T1["GitHub 适配"]
        A1 --> T2["Slack 适配"]
        A1 --> T3["MySQL 适配"]
        A2["LLM 应用 B"] --> T4["GitHub 适配"]
        A2 --> T5["Slack 适配"]
        A2 --> T6["MySQL 适配"]
        A3["LLM 应用 C"] --> T7["GitHub 适配"]
        A3 --> T8["Slack 适配"]
        A3 --> T9["MySQL 适配"]
    end

    subgraph AFTER["有了 MCP: M + N 个组件"]
        direction LR
        APP1["LLM 应用 A"] -.MCP.-> SRV1["GitHub MCP Server"]
        APP2["LLM 应用 B"] -.MCP.-> SRV1
        APP3["LLM 应用 C"] -.MCP.-> SRV1
        APP1 -.MCP.-> SRV2["Slack MCP Server"]
        APP2 -.MCP.-> SRV2
        APP3 -.MCP.-> SRV2
        APP1 -.MCP.-> SRV3["MySQL MCP Server"]
        APP2 -.MCP.-> SRV3
        APP3 -.MCP.-> SRV3
    end

    BEFORE --> AFTER

    NOTE_VAL["MCP 把 M x N 集成<br/>缩减为 M + N 组件<br/>类比: AI 界的 USB-C"]

    AFTER -.-> NOTE_VAL

    classDef bad fill:#ffcdd2,stroke:#c62828,color:#000
    classDef good fill:#c8e6c9,stroke:#2e7d32,color:#000
    classDef note fill:#fff9c4,stroke:#f9a825,color:#000
    class T1,T2,T3,T4,T5,T6,T7,T8,T9 bad
    class APP1,APP2,APP3,SRV1,SRV2,SRV3 good
    class NOTE_VAL note
```

---

## 8. MCP 能暴露的三种能力

```mermaid
flowchart LR
    subgraph CAPS["MCP Server 能暴露什么"]
        direction TB
        TOOLS["**Tools** (主动调用)<br/>@mcp.tool()<br/>LLM 调它来执行动作<br/>例: search_log, send_email"]
        RES["**Resources** (被动读取)<br/>@mcp.resource()<br/>LLM 按需读取的数据<br/>例: 文件内容, 数据库行"]
        PROMPTS["**Prompts** (可复用模板)<br/>@mcp.prompt()<br/>Host 可调用的 prompt 模板<br/>例: 代码 review 模板"]
    end

    NOTE_PROJ["项目里只用了 Tools<br/>Resources / Prompts 未使用<br/>但 FastMCP 一样支持"]

    CAPS -.-> NOTE_PROJ

    classDef cap fill:#e8f5e9,stroke:#388e3c,color:#000
    classDef note fill:#fff9c4,stroke:#f9a825,color:#000
    class TOOLS,RES,PROMPTS cap
    class NOTE_PROJ note
```

---

## 9. 在项目里实际数据流 (代码级追踪)

```mermaid
flowchart TB
    REQ["POST /api/chat<br/>chat.py:18"]

    QUERY["rag_agent_service.query()<br/>rag_agent_service.py:186"]

    INIT["首次: _initialize_agent()<br/>rag_agent_service.py:117"]

    GETCLI["get_mcp_client_with_retry()<br/>mcp_client.py:169<br/>创建 MultiServerMCPClient"]

    LOAD["load_mcp_tools_safe(client)<br/>mcp_client.py:35<br/>15s 超时保护<br/>内部调 client.get_tools()"]

    LSLIST["MCP tools/list<br/>对每个 server 发请求"]

    CREATE["create_agent(<br/>  model, tools=本地+MCP, checkpointer)<br/>构建 ReAct 状态图"]

    INVOKE["agent.ainvoke(messages)<br/>rag_agent_service.py:222"]

    LOOP["ReAct 循环 (LangGraph 内部)"]

    LLMSTEP["LLM 推理<br/>(ChatQwen + function calling)"]

    TOOL["ToolNode 执行<br/>本地工具: 直接 Python 函数<br/>MCP 工具: 走 tools/call"]

    REQ --> QUERY
    QUERY --> INIT
    INIT --> GETCLI
    GETCLI --> LOAD
    LOAD --> LSLIST
    INIT --> CREATE
    QUERY --> INVOKE
    INVOKE --> LOOP
    LOOP --> LLMSTEP
    LLMSTEP --> TOOL
    TOOL --> LOOP

    classDef api fill:#bbdefb,stroke:#1565c0,color:#000
    classDef init fill:#e8f5e9,stroke:#388e3c,color:#000
    classDef mcp fill:#ffe0b2,stroke:#e65100,color:#000
    classDef agent fill:#e1bee7,stroke:#6a1b9a,color:#000
    class REQ api
    class QUERY,INIT,CREATE init
    class GETCLI,LOAD,LSLIST,TOOL mcp
    class INVOKE,LOOP,LLMSTEP agent
```

---

## 10. 编辑提示

| 想改什么 | 改哪里 |
|---|---|
| 加一个新 MCP Server | 第 1 节大局图 SERVERS subgraph 加一个节点 |
| 改某个 server 的端口/transport | 第 3 节 PROJ_NOTE 内容 |
| 新增工具的发现/调用流程 | 第 4 节时序图加 participant 与消息 |
| 协议方法变化 (新版 MCP 增加方法) | 第 3 节 METHODS subgraph 加节点 |
| 想画 Resources / Prompts 用法 | 第 8 节扩展为具体例子 |

Mermaid 语法参考: <https://mermaid.js.org/intro/>

Typora (Mermaid 9.1.2) 兼容性要点:
- 节点文本含中文/特殊符号 (`?`, `>=`, 冒号, 括号) 必须用双引号包裹
- 不要在 classDiagram 用多行 note for
- `**bold**` 在 9.1.2 不渲染但不报错, 显示原文
- 中文标点 (`、` `。`) 安全, 但 `→` `≥` 在未加引号时报词法错误

---

## 附: 关键代码文件对应

| 文件 | 在图中的位置 |
|---|---|
| `mcp_servers/cls_server.py` | 第 1 节 SERVERS, 第 6 节范式 |
| `mcp_servers/monitor_server.py` | 第 1 节 SERVERS |
| `app/agent/mcp_client.py` | 第 1 节 MCPCLIENT, 第 9 节流程 |
| `app/services/rag_agent_service.py` | 第 9 节代码追踪 |
| `app/config.py` (mcp_servers 属性) | 第 6 节注意事项 |
| `.env` (MCP_xxx_TRANSPORT/URL) | 第 3 节 PROJ_NOTE |
