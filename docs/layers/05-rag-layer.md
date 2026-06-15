# Layer 5: RAG 知识层

> 目录: `app/rag/`, `app/core/splitter.py`, `app/core/hybrid_retriever.py`, `app/core/reranker.py`, `app/core/vector_store.py`
>
> 上游: Layer 4 (Agent 调用 `search_knowledge_base` 工具) → 下游: Milvus 向量库 + 内存 BM25 索引

RAG 层为诊断提供运维知识支撑，包括 SOP、告警规则、故障处理流程等。核心设计：**Parent-Child 分块 + Vector/BM25 混合召回 + 本地精排**。

## 5.1 Parent-Child Chunking

### 问题

RAG 的经典矛盾：
- **小 chunk** (300字)：嵌入精度高，检索准，但上下文断裂
- **大 chunk** (2000字)：上下文完整，但嵌入噪音大，检索不准

### 方案

```
Markdown 文档
    │
    ▼ Stage 1: 按标题层级切分
    H1/H2/H3 sections
    │
    ▼ Stage 2: Parent 切块 (≤2400 字符)
    超长 section → RecursiveCharacterTextSplitter
    │
    ▼ Stage 3: Child 切块 (300 字符, 50 字符 overlap)
    每个 Parent → 多个 Child

写入 Milvus: Child chunk (用于检索)
返回 LLM:   Parent chunk (用于上下文)
```

### 链接机制

每个 Child 携带：
- `parent_id`: Parent 内容的 MD5 哈希 (12 位)
- `parent_content`: 完整 Parent 文本
- `chunk_index`: 全局序号

检索时按 `parent_id` 去重，同一 Parent 的多个 Child 命中只保留第一个，返回 Parent 全文。

### 标题路径增强

```python
# child 的 page_content 前缀注入标题路径
child.page_content = f"[{h1} / {h2} / {h3}] {child_content}"
```

标题路径提供结构化语义锚点，让 embedding 模型理解 chunk 在文档中的位置。离线测试 R@1 提升约 10%。

## 5.2 结构保护分块

运维文档中大量存在代码块、Markdown 表格、LaTeX 公式。普通分块器会从中间切断，导致 chunk 不可读。

### 保护机制

```python
PROTECTED_PATTERNS = [
    (r'```[\s\S]*?```',         "CODE_BLOCK"),      # 代码块
    (r'\|[^\n]+\|[\n][\|:\-]+', "TABLE"),            # Markdown 表格
    (r'!\[[^\]]*\]\([^\)]+\)',  "IMAGE"),            # 图片链接
    (r'\[[^\]]*\]\([^\)]+\)',   "HYPERLINK"),         # 超链接
    (r'\$\$[\s\S]*?\$\$',      "BLOCK_LATEX"),       # 块级 LaTeX
    (r'\$[^\$]+\$',            "INLINE_LATEX"),       # 行内 LaTeX
]
```

处理流程：占位符替换 → 正常分块 → 还原占位符。保证代码块和表格不会被切到两个 chunk 中。

## 5.3 混合检索 (Vector + BM25 + RRF)

### 为什么需要 BM25

纯向量检索对运维场景有盲区：
- 错误码 `ERR_CONN_REFUSED`：语义嵌入不如关键词匹配
- 组件名 `redis-master-01`：精确匹配比语义相似更有效
- 命令输出 `CRITICAL: Memory Usage 98%`：包含数字和状态码

### BM25 分词设计

```python
def tokenize(text):
    # 英文/数字: 空格分词, 保留下划线/中划线/点号
    # "ERR_CONN_REFUSED" → ["err_conn_refused"]
    # 中文: 字级切分
    # "内存溢出" → ["内", "存", "溢", "出"]
```

字级切分的中文分词避免了引入分词器依赖（jieba），对运维场景下的短查询效果足够。

### BM25 索引管理

- **懒构建**：首次检索时从 Milvus 加载全部 chunk 构建 BM25 索引
- **异步锁**：`asyncio.Lock` 防止并发首次构建
- **可刷新**：文档上传/删除后调用 `refresh_bm25_index()` 重建

### RRF 融合算法

```
score(doc) = w_vector / (rrf_k + rank_vector) + w_bm25 / (rrf_k + rank_bm25)

rrf_k = 60 (TREC 经典值)
w_vector = 1 - bm25_weight
w_bm25 = bm25_weight (默认 0.4, 压测最优)
```

**为什么用 RRF 而不是加权分数？** BM25 分数无界（5.0 或 50.0），向量余弦相似度在 [-1, 1]，直接加权会被 BM25 分数主导。RRF 只用排名，不受量纲差异影响。

### 去重

```python
dedup_key = f"{source}|{chapter}|{content_hash}"
```

防止同一段内容通过 Vector 和 BM25 两条路召回后重复出现。

## 5.4 Rerank 精排

### 双 Provider 支持

| Provider | 模型 | 特点 |
|----------|------|------|
| DashScope | gte-rerank-v2 | API 调用，延迟稍高，无本地 GPU 需求 |
| 本地 FlagEmbedding | bge-reranker-v2-m3 | 本地推理，支持 CUDA/MPS/CPU |

### Parent 上下文增强

```python
if use_parent_context:
    rerank_input = f"{source} | {chapter}\n{parent_content[:1200]}\n---\n{child_content}"
else:
    rerank_input = child_content  # 快速模式
```

给 reranker 看 Parent 上下文而不是孤立的 Child chunk，让精排模型理解完整段落语义。

### 降级策略

API 不可用或超时 → 直接返回上一阶段的 top-k 结果。**永不抛异常，永远返回有效结果。**

## 5.5 完整检索链路

```
用户查询
    │
    ▼ 3x 过采样 (top_k=3 → retrieve 9 个 child)
    Vector Search (Milvus HNSW COSINE)
    │
    ▼ BM25 Search (内存索引)
    │
    ▼ RRF Fusion (rrf_k=60, bm25_weight=0.4)
    │
    ▼ Parent 去重 (同一 parent_id 只保留第一个 child)
    │
    ▼ Rerank (bge-reranker-v2-m3 / gte-rerank-v2)
    │
    ▼ 返回 top-k Parent 上下文给 LLM
```

## 5.6 Milvus 索引配置

```python
collection_name = "multi_agent_kb"
embedding_dim = 1024              # bge-m3
index_type = "HNSW"
metric_type = "COSINE"
M = 8                             # 层间连接数
efConstruction = 64               # 建索引候选池
ef = 128                          # 搜索候选池
```

## 5.7 评测结果

### 检索评测 (50 题)

| 配置 | Hit@3 | MRR@3 | Recall@3 |
|------|------:|------:|--------:|
| 纯向量 | 0.94 | 0.89 | 0.94 |
| +BM25 (weight=0.4) | **1.00** | **0.93** | **1.00** |

### BM25 权重对比

| Weight | Hit@3 | MRR@3 | 趋势 |
|-------:|------:|------:|------|
| 0.0 | 0.94 | 0.89 | 纯向量基线 |
| 0.1-0.3 | 0.94 | 0.89 | 无变化 |
| **0.4** | **1.00** | **0.93** | 最优点 |
| 0.5 | 1.00 | 0.93 | 持平 |

### 端到端评测 (RAGAS + OpenEvals, 50 题)

| Faithfulness | Relevancy | C-Precision | C-Recall | Groundedness | Helpfulness |
|------------:|----------:|-----------:|--------:|------------:|-----------:|
| 0.913 | 0.936 | 0.997 | 0.871 | 0.994 | 0.872 |

## 模拟面试问答

### 🔥 热点拷问

**面试官：说实话，这个项目场景真用得上 RAG 吗？Agent 有 LLM 的训练知识，加上 MCP 工具实时取数据，RAG 的边际价值在哪？**

RAG 提供的不是"LLM 不知道的通用知识"，而是"这个组织的标准操作流程"。LLM 知道 Redis 是什么、知道内存溢出的通用原因，但不知道"我们公司遇到 Redis 内存 98% 的标准处理步骤是：先查 info memory、再查 bigkeys、最后联系 DBA 评估是否需要扩容"。另外，LLM 训练数据有截止日期，新版本中间件的告警规则、最近出过的故障模式，RAG 可以实时更新而 LLM 做不到。

**追问：那你有没有做过消融实验？对比有 RAG 和没 RAG 的诊断质量差异？**

端到端消融实验没做，这确实是一个该补的评测。有的数据是 RAGAS/OpenEvals 的 50 题评测——Faithfulness 0.913、Answer Relevancy 0.936，说明 RAG 返回的上下文质量不错。但这只评了 RAG 自身的检索和生成质量，没有评"有 RAG 的 Agent vs 没 RAG 的 Agent 在真实诊断任务上的差异"。应该建一组端到端诊断评测：固定 10 个故障场景，分别在有/无 RAG 下跑，对比根因命中率和处置建议的可操作性。

---

**面试官：你既有 RAG 又有 LLM Wiki（见 Layer 7 第 7.4 节），这俩看着都是"存知识然后检索回来"，为什么要分成两层？只用其中一个不行吗？**

它们解决的是两个不同性质的问题，差异的根子在**表示方式**，不在"都叫知识库"。RAG 存的是**静态、人写好的 SOP**，用 chunk + embedding 表示——检索强、可扩展，但改一个字就要重切重灌整篇 chunk，更新很重。Wiki 存的是**诊断过程动态沉淀的经验**，用整页 Markdown 表示，每次诊断完让 LLM 把新报告 merge 进已有页面（`app/wiki/store.py` 的 `ingest_diagnosis`）——更新天然增量，但召回靠关键词匹配、规模上不去、每次 merge 还烧 token。一句话：RAG 是"静态可检索"，Wiki 是"动态可改写"。

**追问：那为什么不能只用 RAG？把诊断经验也切成 chunk 灌进 Milvus 不就行了？**

技术上能，但会很别扭。第一，经验是高频增量的——每次诊断都产出新内容，如果都要切 chunk + 调 embedding + 写 Milvus，写放大很严重，而 Wiki 直接追加文件 + 一次 LLM merge 就完了。第二，经验之间会**重叠和演进**——"Redis 内存高"这个故障可能遇到 N 次，每次都灌成独立 chunk，检索时会召回一堆重复的历史片段，而 Wiki 的 LLM-merge 能把多次经验收敛成一页连贯的"我们对这类故障的处置演进"。第三，Wiki 的价值恰恰是"**整页能被 LLM 读，也能被 LLM 改写**"，切成 chunk 就丢了这个整体性。

**追问：那反过来，为什么不能只用 Wiki？SOP 也让 LLM merge 进 Markdown 页面，召回时关键词匹配不就够了？**

规模和检索质量撑不住。SOP 是大体量、相对稳定的语料，根本不需要"每次 LLM merge"这种昂贵的写入方式；而 Wiki 的召回是 read-index-first 的关键词重叠匹配，几十页还行，几千上万页就退化了——精确错误码、语义近义这些场景必须靠向量 + BM25。把静态 SOP 塞进 Wiki，等于用一个为"小规模、高频改写"设计的结构去扛"大规模、低频更新、强检索"的需求，方向反了。

**追问（诚实版）：那这俩会不会职责重叠、内容漂移？有没有更统一的设计？**

会，这是当前设计真实的风险点。两套存储、两套召回，诊断 prompt 里 RAG 的 SOP context 和 Wiki 的经验 block 是并列注入的，没有统一的相关性排序，也没有去重——如果 Wiki 沉淀的经验和 SOP 说法冲突，目前是都丢给 LLM 自己权衡。更统一的做法是：底层统一成一个带 `doc_type`（sop / experience）和 `source` 元数据的向量库，用同一套混合检索召回，只是写入路径不同（SOP 走批量 ingest，经验走"先 LLM 收敛再写入"）。但那样会失去 Wiki "整页可被 LLM 改写"的优势，是一个真实的 trade-off。当前分两层是有意识地选了"职责清晰、各自用最合适的表示"，代价就是缺一个统一检索入口。

---

**面试官：为什么选 Milvus？Chroma、Qdrant、Faiss 不行吗？**

选 Milvus 三个原因：HNSW + COSINE 的检索性能在当前规模下够用；pymilvus SDK 和 Docker 部署工程集成成本低；Attu 可视化界面方便调试。Chroma 更轻量但功能弱一些（比如不支持按字段过滤删除）；Qdrant 性能也不错但社区不如 Milvus 成熟；Faiss 是纯库需要自己封装持久化。对于当前规模几千到几万 chunk，几个选型差异不大，Milvus 是工程上合理的选择但不是唯一正确的。

**追问：你的 BM25 索引是全量加载到内存的，数据量大了怎么办？**

当前 BM25 索引确实是从 Milvus 拉全量 chunk 构建的内存索引，几千条文档没问题，但如果到了几十万条就会有内存和构建时间问题。改进方向有两个：一是 Milvus 2.4+ 已经支持 BM25 索引（Sparse Vector），可以让 BM25 也走 Milvus，省去内存索引维护。二是如果坚持自建，可以做文档级增量更新——每次文档变更只更新对应的 BM25 词项，不全量重建。当前全量重建是因为数据量小，几秒就完成。

---

**面试官：纯向量不够要加 BM25，那你有没有考虑过其他方案？比如 query rewriting、HyDE？**

考虑过。Query rewriting（让 LLM 改写查询）在运维场景下效果有限——用户输入已经是故障描述，改写空间不大，反而增加一次 LLM 调用延迟。HyDE（让 LLM 先生成假设性文档再做检索）对于需要匹配精确错误码的场景帮助不大——HyDE 生成的假设文档不太可能包含 `ERR_CONN_REFUSED` 这样的精确 token。BM25 是成本最低、效果最直接的补充：零额外 LLM 调用，直接解决精确匹配问题。实测 Hit@3 从 0.94 到 1.00，被补回的 3 道题全是精确匹配型查询。

### 深度追问链

**面试官：（接 RAG 必要性问题）如果做消融实验发现 RAG 贡献很小，你会怎么调整？**

两种可能：一是知识库内容太浅，没有提供 LLM 训练数据之外的增量信息——应该增强知识库内容（企业特有 SOP、历史故障案例），而不是去掉 RAG。二是 Agent 的工具取证能力已经足够强，知识库辅助作用有限——可以把 RAG 从"必调"降级为"按需调"，让 Planner 决定是否需要搜索知识库。

**继续追问：那 RAG 的价值到底是检索准确度还是别的什么？**

至少三个维度。一是知识增量——提供 LLM 训练截止日期之后的信息和组织内部信息。二是可追溯性——诊断报告可以引用"根据 SOP-Redis-OOM 第 3 步"，运维人员可以验证建议的来源。三是可控性——通过控制知识库内容来控制 Agent 的行为边界，删掉某个危险操作的 SOP 就能确保 Agent 不会建议那个操作。检索准确度是基础，但可追溯性和可控性在生产环境中可能更重要。

---

**面试官：（接 Milvus 选型问题）向量维度 1024，chunk 几千条，用 Milvus 是不是大炮打蚊子？SQLite + numpy 就够了吧？**

从纯性能角度确实如此。几千条 1024 维向量，numpy brute-force 也只要几毫秒。选 Milvus 的考虑是工程完整性：持久化、按字段删除、Collection 管理都是 Milvus 提供的生产级功能。另外如果知识库扩展到几十万条（接入多个企业知识源后），numpy 就不够用了。最后是学习和展示价值——面试时"用了 Milvus"比"用了 numpy"更能体现对向量检索的理解。如果严格追求轻量，chromadb 确实更合适。

### 常规问题

**面试官：结构保护分块解决什么问题？**

运维文档中大量代码块和 Markdown 表格。普通分块器按字符长度切分，会从代码块中间切断。用 6 种正则做占位符保护 → 分块 → 还原，保证代码块和表格不会被切到两个 chunk 中。小优化但体现对数据质量的关注。

**面试官：RRF 的 k=60 怎么定的？调过吗？**

k=60 是 TREC 信息检索领域的经典值，Cormack et al. 2009 的论文推荐。对这个值做过简单测试（k=30/60/100），在当前 50 题评测集上差异不大，就用了经典值。如果评测集扩大到几百题，可能值得做更细粒度的调优。

### 反思与改进

**面试官：RAG 在这个系统里最大的价值和局限分别是什么？**

最大的价值是可追溯性——报告可以引用"根据 SOP-Redis-Memory-High 第 3 步"，运维人员可以验证来源。最大的局限是知识库内容的覆盖度和时效性——只有公开语料，没有企业经验积累，文档更新了知识库不会自动同步。如果知识库落后于实际运维实践，RAG 反而会给过时建议。

**面试官：如果重来 RAG 这块怎么做？**

三个改动。一是一开始就做增量同步而不是手动上传——哪怕只支持本地文件夹 watch。二是做消融实验——有/无 RAG 的诊断质量对比是证明 RAG 价值的最直接方式。三是 BM25 从一开始就用 Milvus 的 Sparse Vector 而不是自建内存索引——少维护一个组件。
