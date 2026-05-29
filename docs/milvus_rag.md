# Milvus RAG 选型笔记 - Chunk / Embedding / 索引

> 本文档基于项目 `app/services/document_splitter_service.py`、`vector_embedding_service.py`、
> `vector_store_manager.py`、`app/core/milvus_client.py` 实际代码绘制。
> 所有图均使用 **Mermaid** 语法,VSCode / GitHub / Typora 均可预览。
> ⚠️ Typora (Mermaid 9.1.2) 兼容性要点见文末。

---

## 1. 完整 RAG 流程总览

```mermaid
flowchart TB
    DOC(["原始文档 (.md / .txt)"])

    subgraph SPLIT["阶段 1: 切分 (document_splitter_service.py)"]
        direction TB
        S1["MarkdownHeaderTextSplitter<br/>只切 h1 / h2"]
        S2["RecursiveCharacterTextSplitter<br/>chunk_size=1600, overlap=100"]
        S3["_merge_small_chunks<br/>合并小于 300 字符的碎片"]
        S1 --> S2 --> S3
    end

    subgraph EMBED["阶段 2: 向量化 (vector_embedding_service.py)"]
        direction TB
        E1["DashScope text-embedding-v4<br/>维度 1024"]
        E2["批量调用 embed_documents<br/>OpenAI 兼容协议"]
        E1 --> E2
    end

    subgraph STORE["阶段 3: 入库 (vector_store_manager.py)"]
        direction TB
        T1["Milvus collection 'biz'<br/>FLOAT_VECTOR(1024) + content + metadata"]
        T2["索引 IVF_FLAT, nlist=128, L2"]
        T1 --> T2
    end

    subgraph QUERY["阶段 4: 检索"]
        direction TB
        Q1["similarity_search(query, k=3)"]
        Q2["返回 top_k Document 给 LLM"]
        Q1 --> Q2
    end

    DOC --> SPLIT
    SPLIT --> EMBED
    EMBED --> STORE
    STORE --> QUERY

    classDef stage fill:#e8f5e9,stroke:#388e3c,color:#000
    classDef terminal fill:#bbdefb,stroke:#1565c0,color:#000
    class SPLIT,EMBED,STORE,QUERY,S1,S2,S3,E1,E2,T1,T2,Q1,Q2 stage
    class DOC terminal
```

**对应代码:**
- 切分: `app/services/document_splitter_service.py`
- 向量化: `app/services/vector_embedding_service.py`
- 入库: `app/services/vector_store_manager.py`
- 索引创建: `app/core/milvus_client.py:192-208`

---

## 2. Chunk 切分策略 (三阶段 + 设计意图)

```mermaid
flowchart TB
    INPUT(["原始 Markdown 文档"])

    subgraph PHASE1["阶段 1: 标题切分"]
        direction TB
        P1A["MarkdownHeaderTextSplitter<br/>headers=['#', '##']<br/>strip_headers=False (保留标题)"]
        P1B["输出: 按章节切的大块<br/>每块大小不一"]
        P1A --> P1B
    end

    subgraph PHASE2["阶段 2: 字符递归切分"]
        direction TB
        P2A["RecursiveCharacterTextSplitter<br/>chunk_size = 800 x 2 = 1600<br/>overlap = 100"]
        P2B["按 段落 -> 句子 -> 词 优先级<br/>把超大块切到 1600 字符以内"]
        P2A --> P2B
    end

    subgraph PHASE3["阶段 3: 小片合并"]
        direction TB
        P3A["_merge_small_chunks<br/>min_size=300"]
        P3B["把小于 300 字符的碎片<br/>追加到前一个 chunk"]
        P3A --> P3B
    end

    META["注入元数据:<br/>_source / _extension / _file_name"]

    INPUT --> PHASE1
    PHASE1 --> PHASE2
    PHASE2 --> PHASE3
    PHASE3 --> META

    NOTE_DESIGN["设计意图:<br/>- 阶段 1 保证语义边界 (章节内不切散)<br/>- 阶段 2 控制单 chunk 不超长 (embedding 上限)<br/>- 阶段 3 避免过度碎片化 (太小的 chunk 召回意义低)"]

    PHASE3 -.-> NOTE_DESIGN

    classDef phase fill:#fff9c4,stroke:#f9a825,color:#000
    classDef meta fill:#e1bee7,stroke:#6a1b9a,color:#000
    classDef terminal fill:#bbdefb,stroke:#1565c0,color:#000
    classDef note fill:#ffe0b2,stroke:#e65100,color:#000
    class PHASE1,PHASE2,PHASE3,P1A,P1B,P2A,P2B,P3A,P3B phase
    class META meta
    class INPUT terminal
    class NOTE_DESIGN note
```

### chunk_size / overlap 选择权衡

```mermaid
flowchart LR
    SMALL["chunk 偏小 (<400)<br/>定位精确<br/>但上下文破碎<br/>top_k 容易漏召回"]
    MID["chunk 适中 (500-1500)<br/>平衡甜点<br/>适合大多数文档"]
    LARGE["chunk 偏大 (>2000)<br/>上下文完整<br/>但语义稀释<br/>token 成本高"]

    SMALL --> MID --> LARGE

    NOTE_PROJ["项目当前:<br/>- 配置 800 但实际 1600 (有 x2 倍率)<br/>- 适合运维知识库 (需保留方法论上下文)<br/>- 如果换 FAQ 风格应改小到 500"]

    MID -.-> NOTE_PROJ

    classDef bad fill:#ffcdd2,stroke:#c62828,color:#000
    classDef good fill:#c8e6c9,stroke:#2e7d32,color:#000
    classDef note fill:#fff9c4,stroke:#f9a825,color:#000
    class SMALL,LARGE bad
    class MID good
    class NOTE_PROJ note
```

---

## 3. Embedding 模型选型

```mermaid
flowchart TB
    subgraph MODELS["主流 Embedding 模型对比"]
        direction TB
        M1["text-embedding-v4 (阿里通义)<br/>维度 64-2048 可选, 中文 SOTA<br/>API 调用, OpenAI 兼容协议<br/>项目当前选择 ⭐"]
        M2["text-embedding-3-small/large (OpenAI)<br/>1536 / 3072 维, 英文优势<br/>API 调用"]
        M3["bge-large-zh-v1.5 / bge-m3 (智源开源)<br/>1024 维, 中文 SOTA<br/>需自部署 GPU"]
        M4["jina-embeddings-v3<br/>1024 维, 长文档 8k+ token<br/>API/本地"]
    end

    DECIDE{"选型决策点"}

    D1["数据可出网 + 中文场景<br/>-> text-embedding-v4 (项目选择)"]
    D2["数据敏感不出网<br/>-> 自部署 bge"]
    D3["英文为主<br/>-> OpenAI 系"]
    D4["超长文档不切分<br/>-> jina-v3"]

    MODELS --> DECIDE
    DECIDE --> D1
    DECIDE --> D2
    DECIDE --> D3
    DECIDE --> D4

    classDef model fill:#bbdefb,stroke:#1565c0,color:#000
    classDef decide fill:#fff3e0,stroke:#f57c00,color:#000
    classDef path fill:#c8e6c9,stroke:#2e7d32,color:#000
    class M1,M2,M3,M4 model
    class DECIDE decide
    class D1,D2,D3,D4 path
```

---

## 4. 三大不可变决策 (一旦上线很难改)

```mermaid
flowchart TB
    subgraph IMMUTABLE["三个 '一次定终身' 的决策"]
        direction TB

        D1["决策 1: 向量维度<br/>项目: 1024"]
        D1A["权衡: 256 极省, 1024 甜点, 3072 边际收益小"]
        D1B["改了就要全量重建 collection<br/>+ 全量重新调 embedding API"]
        D1 --> D1A --> D1B

        D2["决策 2: 距离度量<br/>项目: L2"]
        D2A["L2 欧氏距离 / Cosine 余弦 / IP 内积"]
        D2B["text-embedding-v4 输出已归一化<br/>L2 与 Cosine 排序结果一致<br/>但 Cosine 更直观, 业界主流"]
        D2 --> D2A --> D2B

        D3["决策 3: 索引类型<br/>项目: IVF_FLAT, nlist=128"]
        D3A["FLAT (暴力, 万级以下)<br/>IVF_FLAT (万-百万, 项目当前)<br/>HNSW (百万-十亿, 推荐)<br/>IVF_PQ (十亿+, 牺牲精度)"]
        D3B["nlist 经验值: sqrt(N)<br/>1.5万向量适合 nlist=128<br/>10万级建议调到 316+"]
        D3 --> D3A --> D3B
    end

    classDef decision fill:#ffcdd2,stroke:#c62828,color:#000
    classDef detail fill:#fff9c4,stroke:#f9a825,color:#000
    class D1,D2,D3 decision
    class D1A,D1B,D2A,D2B,D3A,D3B detail
```

### 自动重建机制 (项目里的防呆设计)

```mermaid
flowchart LR
    CONNECT["milvus_manager.connect()"]
    CHECK["检查 collection<br/>vector_field 的 dim 参数"]
    MATCH{"existing_dim<br/>== VECTOR_DIM ?"}
    KEEP["保持现有 collection"]
    DROP["drop_collection<br/>+ 重新 create_collection"]

    CONNECT --> CHECK --> MATCH
    MATCH -->|"是"| KEEP
    MATCH -->|"否"| DROP

    NOTE_AUTO["对应代码:<br/>milvus_client.py:111-121<br/>开发期友好, 生产期慎用"]

    DROP -.-> NOTE_AUTO

    classDef step fill:#bbdefb,stroke:#1565c0,color:#000
    classDef warn fill:#ffcdd2,stroke:#c62828,color:#000
    classDef note fill:#fff9c4,stroke:#f9a825,color:#000
    class CONNECT,CHECK,KEEP step
    class MATCH,DROP warn
    class NOTE_AUTO note
```

---

## 5. Collection Schema 设计

```mermaid
flowchart TB
    subgraph SCHEMA["biz collection 字段"]
        direction TB
        F1["id : VARCHAR(100)<br/>is_primary=True<br/>auto_id=False, 用 UUID4"]
        F2["vector : FLOAT_VECTOR(1024)<br/>建索引的字段"]
        F3["content : VARCHAR(8000)<br/>原始文本, 给 LLM 用"]
        F4["metadata : JSON<br/>_source, _extension, _file_name<br/>支持任意键过滤"]
    end

    OPT1["enable_dynamic_field=False<br/>强 schema, 数据干净"]
    OPT2["num_shards=2<br/>提升写入吞吐"]

    SCHEMA --> OPT1
    SCHEMA --> OPT2

    DELETE_OP["按文件删除 (增量更新支持):<br/>用 metadata 的 _source 字段做 JSON 路径过滤<br/>表达式形如 metadata[_source] == 文件路径"]

    F4 -.-> DELETE_OP

    classDef field fill:#e8f5e9,stroke:#388e3c,color:#000
    classDef opt fill:#bbdefb,stroke:#1565c0,color:#000
    classDef op fill:#fff9c4,stroke:#f9a825,color:#000
    class F1,F2,F3,F4 field
    class OPT1,OPT2 opt
    class DELETE_OP op
```

---

## 6. 索引参数详解 (IVF_FLAT)

```mermaid
flowchart TB
    QUERY_VEC(["查询向量 q"])

    subgraph IVF["IVF_FLAT 索引内部"]
        direction TB
        C1["建索引时:<br/>把 N 个向量聚成 nlist 个簇<br/>项目: nlist=128"]
        C2["查询时:<br/>找 q 最近的 nprobe 个簇<br/>默认 nprobe=8"]
        C3["在选中簇内暴力比对<br/>返回 top_k"]
        C1 --> C2 --> C3
    end

    RESULT(["top_k 结果"])

    QUERY_VEC --> IVF
    IVF --> RESULT

    NOTE_TUNE["调优经验:<br/>- nlist 太小 -> 簇大, 精扫开销重<br/>- nlist 太大 -> 召回率下降<br/>- nprobe 越大 -> 越准但越慢<br/>- nprobe 经验值: nlist 的 5-10%"]

    IVF -.-> NOTE_TUNE

    NOTE_TOPK["项目调用:<br/>similarity_search(query, k=3)<br/>不传 search_params, 用默认 nprobe=8<br/>如想更准: param={'nprobe': 32}"]

    RESULT -.-> NOTE_TOPK

    classDef node fill:#bbdefb,stroke:#1565c0,color:#000
    classDef note fill:#fff9c4,stroke:#f9a825,color:#000
    classDef terminal fill:#e8f5e9,stroke:#388e3c,color:#000
    class C1,C2,C3 node
    class NOTE_TUNE,NOTE_TOPK note
    class QUERY_VEC,RESULT terminal
```

---

## 7. top_k 权衡

```mermaid
flowchart LR
    K1["k=1-2<br/>LLM 上下文短, 便宜<br/>但漏召风险高"]
    K3["k=3-5<br/>平衡甜点<br/>项目当前 k=3"]
    K10["k=8-10<br/>召回率高<br/>但噪声多, LLM 易被无关内容误导"]

    K1 --> K3 --> K10

    UPGRADE["进阶方案:<br/>召回 k=10 -> 用 BGE-Reranker 重排 -> 取 top 3<br/>精度可提升 10-20 percent<br/>(项目当前未使用)"]

    K10 -.-> UPGRADE

    classDef low fill:#ffcdd2,stroke:#c62828,color:#000
    classDef good fill:#c8e6c9,stroke:#2e7d32,color:#000
    classDef advance fill:#e1bee7,stroke:#6a1b9a,color:#000
    class K1,K10 low
    class K3 good
    class UPGRADE advance
```

---

## 8. 项目当前选型评估 + 优化建议矩阵

```mermaid
flowchart TB
    subgraph GOOD["做得好的地方"]
        direction TB
        G1["Markdown 两阶段切分 + 合并小片"]
        G2["维度变化自动重建 collection"]
        G3["元数据带 _source 支持按文件删除"]
        G4["auto_id=False + UUID 可控主键"]
        G5["enable_dynamic_field=False 强 schema"]
    end

    subgraph IMPROVE["可优化点 (按重要性排序)"]
        direction TB
        I1["1. metric_type: L2 -> COSINE<br/>业界主流, 日志直观"]
        I2["2. overlap: 100 (6 percent) -> 200 (12 percent)<br/>中文长句容易被切断"]
        I3["3. 加入 BGE-Reranker<br/>k=10 召回 + rerank -> 3"]
        I4["4. index_type: IVF_FLAT -> HNSW<br/>中等规模下精度更高"]
        I5["5. chunk_size 配置歧义<br/>声明 800 实际 1600, 改成直接 1600"]
    end

    classDef good fill:#c8e6c9,stroke:#2e7d32,color:#000
    classDef tip fill:#ffe0b2,stroke:#e65100,color:#000
    class G1,G2,G3,G4,G5 good
    class I1,I2,I3,I4,I5 tip
```

---

## 9. 心法总结

```mermaid
flowchart TB
    H1["心法 1: chunk_size 业务匹配<br/>FAQ 200-400, 技术文档 500-1500, 长文 1500-3000"]
    H2["心法 2: overlap 不低于 10 percent<br/>中文长句尤其要 12-15 percent"]
    H3["心法 3: 有结构用结构感知<br/>Markdown / 代码不要按字符暴力切"]
    H4["心法 4: 1024 维是 cost / quality 甜点<br/>不要无脑追求 3072"]
    H5["心法 5: 文本检索一律 Cosine<br/>L2 归一化下等价但不直观"]
    H6["心法 6: 索引按规模选<br/>万级 IVF_FLAT, 百万级 HNSW, 亿级 IVF_PQ"]
    H7["心法 7: 维度 + 度量 = 一次定终身<br/>选型前务必想清楚"]

    H1 --> H2 --> H3 --> H4 --> H5 --> H6 --> H7

    classDef heart fill:#ffe0b2,stroke:#e65100,color:#000
    class H1,H2,H3,H4,H5,H6,H7 heart
```

---

## 10. 编辑提示

| 想改什么 | 改哪里 |
|---|---|
| 换 embedding 模型 | 第 3 节 + `vector_embedding_service.py:23-25` + 维度同步改 |
| 改向量维度 | 第 4 节 D1 + `milvus_client.py:49` (会触发自动重建) |
| 改距离度量 | 第 4 节 D2 + `milvus_client.py:198` |
| 改索引类型 (IVF_FLAT -> HNSW) | 第 6 节 + `milvus_client.py:197-201` |
| 改 chunk_size / overlap | 第 2 节 + `app/config.py:43-44` |
| 改 top_k | 第 7 节 + `app/config.py:39` |
| 加 reranker | 第 7 节 UPGRADE 节点扩展为新章节 |

Mermaid 语法参考: <https://mermaid.js.org/intro/>

---

## Typora (Mermaid 9.1.2) 兼容性要点

- 节点文本含中文/特殊符号 (`?`, `>=`, 冒号, 括号) 必须用双引号包裹
- 不要使用 Unicode 圈数字 `①②` 或箭头 `→ ≥ ←`,改成 ASCII (`>= -> 1 2`)
- 不要在 `classDiagram` 用多行 `note for`
- 不要用 Mermaid 保留字做 classDef 名:`call` `click` `end` `style` `class` `href`
- 边标签含中文一律用 `-->|"标签"|` 形式包双引号
- **节点文本里不能用反斜杠转义引号** `\"`,Mermaid 9.1.2 不识别。代码片段建议放节点外的 `\`\`\`` 代码块,节点里只做描述

---

## 附: 关键代码文件对应

| 文件 | 在图中的位置 |
|---|---|
| `app/services/document_splitter_service.py` | 第 1 节 SPLIT, 第 2 节三阶段 |
| `app/services/vector_embedding_service.py` | 第 1 节 EMBED, 第 3 节模型选型 |
| `app/services/vector_store_manager.py` | 第 1 节 STORE, 第 5 节 schema |
| `app/core/milvus_client.py` | 第 4 节三大决策, 第 5 节 schema, 第 6 节索引 |
| `app/config.py` (chunk / top_k) | 第 2 节 chunk, 第 7 节 top_k |
