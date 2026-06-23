# Step 3：RAG 知识库 — 文档 Ingest Pipeline & 检索流水线

> 这一步内容较重，涵盖了 Step 3（Ingest）和 Step 4（检索）。因为 Parent-Child 的设计横跨切分和检索两侧，拆开讲反而不好理解，面试时也通常是作为一个完整故事来讲。

---

## 这一步要解决的问题

AIOps 平台的核心能力之一是 **知识库增强诊断**：Agent 拿到一个告警，需要去查 SOP 文档、历史告警规则、日志模板，找到匹配的排障方案。

这就是经典的 RAG（Retrieval-Augmented Generation）问题。但运维文档有自己的特点：

- **结构复杂**：包含代码块、表格、PromQL 公式、链接，普通切分器会切碎这些结构
- **精确关键词很重要**：用户可能输入 `ERR_CONN_REFUSED` 或 `redis.exception.TimeoutError`，纯向量检索容易把这些"揉"成语义向量丢失精确匹配
- **需要完整上下文**：embedding 向量适合小块文本（召回准），但 LLM 需要大段上下文才能给出完整的排障步骤

我们的解决方案是一个三级流水线：**Parent-Child 切分 → Hybrid 检索（Vector + BM25） → Reranker 精排**。

---

## 整体数据流

```
原始 Markdown 文档
    │
    ▼
① 按 H1/H2/H3 标题切成天然"父块候选"
    │
    ▼
② 父块超长 → 结构保护二次切分（代码块/表格/公式不被切碎）
    │
    ▼
③ 每个父块再切成 child 小块（~300 字），child 带章节前缀参与 embedding
    │
    ▼
④ child 入库 Milvus（向量 + metadata 含 parent_id、parent_content）
    │
    ▼
⑤ 检索时：Vector top-20 ∪ BM25 top-20 → RRF 融合 → Reranker 精排 → top-3
    │
    ▼
⑥ 按 parent_id 去重，返回 parent_content 给 LLM（完整段落，上下文不缺失）
```

---

## 1. Parent-Child Chunking

### 核心思想

这是整个 RAG 最值得讲的设计决策：

> **child 小块负责"找得准"，parent 大块负责"读得全"。**

- **child 块**（~300 字）：写入 Milvus 参与向量检索。小块的 embedding 语义更聚焦，召回精度高
- **parent 块**（≤2400 字）：不直接参与检索，但 child 命中后，通过 `parent_id` 找回对应的 parent，把 parent 的完整文本送给 LLM

这样就解决了 RAG 的经典矛盾：**chunk 太小找得准但上下文断裂，chunk 太大上下文全但检索不准**。

### 面试话术

> "我们采用 Parent-Child 双层切分策略。child 块 300 字左右，足够小让 embedding 聚焦语义；parent 块 2400 字以内，保留完整的排障步骤和上下文。检索时用 child 的向量去找，找到后按 parent_id 去重，返回 parent 的完整内容给 LLM。这样既保证了召回精度，又保证了 LLM 拿到的上下文是完整可用的。"

### 切分三步骤

**第一步：按标题切**

用 LangChain 的 `MarkdownHeaderTextSplitter` 按 `#`、`##`、`###` 切，得到天然的"节级"块。这些就是 parent 的候选。比如一个 Redis SOP 文档会被切成"连接超时排查"、"内存 OOM 排查"、"主从同步异常"等独立节。

**第二步：父块超长时二次切**

如果某个节的内容超过 2400 字（`rag_parent_max_chars`），用 `RecursiveCharacterTextSplitter` 再切一刀。但这里有个关键：**结构保护**——不能在代码块或表格中间切断。

**第三步：每个 parent 切 child**

每个 parent 块用同样的 `RecursiveCharacterTextSplitter` 切成 ~300 字的 child 小块，overlap 50 字。每个 child 的 metadata 里记录了 `parent_id`（parent 内容的 MD5 前 12 位）和 `parent_content`（parent 全文）。

---

## 2. 结构保护（参考腾讯 WeKnora）

### 问题

运维文档里充满了代码块、Markdown 表格、PromQL 公式、链接。普通的文本切分器按字符数切，可能把一个代码块从中间切成两半，LLM 拿到的就是一个残缺的代码片段，完全无法理解。

### 解决方案

切分前先扫描文本，找出所有"保护区"（不可切的区间），用占位符替换 → 切分 → 再把占位符还原回去。

六种保护模式（正则）：

1. ` ```...``` ` 代码块（含语言标识）
2. Markdown 表格（表头 + 分隔行 + 数据行）
3. `![alt](url)` 图片链接
4. `[text](url)` 普通链接
5. `$$...$$` 块级 LaTeX 公式
6. `$...$` 行内 LaTeX 公式

### 面试怎么讲

> "运维文档里有大量代码块和表格。我们做了一个结构保护机制：切分之前先扫描文本，用正则找出代码块、表格、公式这些不能被切碎的区间，用占位符临时替换掉，切完之后再还原。这样保证了这些结构化内容永远不会被切到一半。这个设计参考了腾讯 WeKnora 的生产实践。"

### 占位符的设计细节

占位符用零宽空格包裹纯字母数字（`\u200BPROT0001\u200B`），长度小于 14 字符。为什么这样设计？因为 `RecursiveCharacterTextSplitter` 的切刀会在 `\n`、`。`、空格等分隔符处切，占位符里不包含这些字符，所以切刀不会从占位符中间切断。

---

## 3. 章节前缀注入 — 一个小优化但效果显著

### 做了什么

把 child 块的 `page_content` 前面拼上章节路径：

```
[Redis 常见故障 / 连接超时排查 / 排查步骤]
1. 检查 Redis 实例是否存活: redis-cli ping
2. 检查网络连通性: telnet redis-host 6379
...
```

这样 embedding 不仅编码了内容本身，还编码了"这段内容属于哪个章节"的信息。

### 效果

离线评测结果：

| 指标 | 不加章节前缀 | 加章节前缀 | 提升 |
|---|---:|---:|---:|
| R@1 | 83.33% | 91.67% | +10% |
| MRR | 0.88 | 0.94 | +6.8% |

### 面试话术

> "我们做了一个简单但有效的优化：把 H1/H2/H3 的标题拼到 child 块前面参与 embedding。这样当用户问'Redis 连接超时怎么排查'时，不仅内容语义匹配，章节路径里的'Redis'和'连接超时'也会被编码进向量。实测 R@1 提升了 10 个百分点。成本几乎为零，就是多拼了一行字符串。"

---

## 4. 入库脚本设计

### 批量入库的工程考量

`ingest_kb_corpus.py` 脚本做了几个实用的工程设计：

- **分批入库**：默认每 100 个 chunk 一批写入 Milvus，避免一次性写入导致 OOM 或超时
- **断点续传**：支持 `--resume` 参数。每批写入成功后保存 checkpoint（JSON 文件记录 dataset_hash + 已完成的 batch index），中断后从上次成功的位置继续
- **数据指纹校验**：用所有 chunk 的内容算 SHA256 指纹。resume 时会比对指纹，如果文档内容变了（比如加了新文件），会提示"指纹不匹配，从头开始"，防止用旧 checkpoint 续跑到新数据上
- **`--dry-run`**：只切分不入库，用来验证切分逻辑是否正确
- **`--reset`**：先 drop 旧 collection 再入库，换 embedding 模型时用

---

## 5. 高级检索流水线（Step 4 内容）

### 为什么需要三级流水线

纯向量检索有两个典型失手场景：

1. **精确 token 匹配丢失**：用户输入 `ERR_CONN_REFUSED`，向量编码会把它"揉"进语义空间，不如 BM25 精确匹配
2. **罕见长尾词**：比如内部组件名 `oncall-dispatcher`，embedding 模型训练语料里几乎没有，向量质量差

所以我们叠加了 BM25 做 Hybrid 检索，再用 Reranker 做精排。

### 流水线详细步骤

```
用户 query: "Redis 连接超时 ERR_CONN_REFUSED 怎么排查"
    │
    ▼
Step 1: Vector 粗排
    Milvus HNSW 向量检索，取 top-20 候选
    │
    ▼
Step 2: Hybrid 融合（可通过 .env 开关）
    BM25 也取 top-20 候选
    用 RRF 公式融合两路结果:
        score(d) = vec_weight/(60+rank_vec) + bm25_weight/(60+rank_bm25)
    取 top-20 融合结果
    │
    ▼
Step 3: Reranker 精排（可通过 .env 开关）
    把 20 个候选连同 query 送入 cross-encoder
    按精排分数取 top-3
    │
    ▼
Step 4: Parent-Child 去重
    按 parent_id 去重，返回 parent_content 给 LLM
```

### 核心设计决策

**为什么选 RRF 而不是加权分数融合？**

> BM25 的分数是无上界的（可能 5.0，也可能 50.0，取决于文档长度），向量 cosine 是 [-1, 1]，量纲完全不同，直接加权要先做归一化。RRF 只用"排名"不看绝对分数（`score = Σ 1/(k + rank)`），天然对量纲不敏感。k=60 是 TREC 竞赛的经典值。

**为什么 BM25 中文不用 jieba 分词？**

> 向量检索已经覆盖了中文语义理解。BM25 的核心价值是"捕获向量漏掉的精确 token"——这些 token 基本是英文错误码、服务名、数字。按字切 + 英文按空格切已经够用。省掉 jieba 依赖（几 MB + 词典加载耗时），启动更轻。

**Reranker 和 Embedding 的区别是什么？**（面试高频问题）

> Embedding 是 bi-encoder：query 和 doc 分别编码成向量再算余弦，它们从未在同一个上下文中交互过。Reranker 是 cross-encoder：把 (query, doc) 作为一对一起送进模型，能捕捉更精细的语义关联。代价是每对都要跑一次模型，不能预先算好，所以只能用在"粗排后重排少量候选"这一步。Anthropic 实测显示 top-20 用 cross-encoder 重排后，检索失败率从 3.7% 降到 1.9%。

### 降级策略 — 这是工程亮点

整条流水线的每一层都有降级兜底：

| 故障场景 | 降级行为 |
|---|---|
| BM25 索引未构建（首次检索前） | 惰性构建：首次检索时从 Milvus 拉全量 chunk 构建 |
| rank_bm25 库未安装 | 跳过 Hybrid，直接返回纯向量结果 |
| BM25 从 Milvus 拉全量失败 | 保持上一份索引（若有），日志 WARNING |
| Reranker API 超时 | 跳过精排，返回 Hybrid 融合后的前 k 个 |
| Reranker API Key 缺失 | 跳过精排 |
| Milvus 不可用 | 返回空列表 |

> **面试话术**："我们的设计原则是**任一环节故障都自动降级到上一层结果，绝不中断业务**。最差情况下，Hybrid 和 Reranker 都挂了，系统退化成纯向量检索——能力打折但不会挂。每一层降级都有日志 WARNING，运维能及时发现。"

---

## 6. 评测体系

### 检索侧评测（50 题）

用 50 道真实的运维问题（覆盖 Redis、MySQL、Nginx、JVM、Kafka 等场景）评测检索质量：

| 指标 | 含义 | 结果 |
|---|---|---:|
| Hit@3 | top-3 里至少有一个相关文档的比例 | 1.000 |
| MRR@3 | 第一个相关文档的排名倒数的均值 | 0.930 |
| Recall@3 | top-3 覆盖了多少个标注的相关文档 | 1.000 |

BM25 权重调参结论：`bm25_weight=0.4` 是最优平衡点（Hit 从 0.94 → 1.00）。

### RAGAS 端到端评测（50 题）

| 指标 | 含义 | 结果 |
|---|---|---:|
| Faithfulness | 答案是否忠于检索到的上下文（有没有编） | 0.913 |
| AnswerRelevancy | 答案是否切题 | 0.936 |
| ContextPrecision | 检索结果是否相关 | 0.997 |
| ContextRecall | 检索是否覆盖了标准答案的信息 | 0.871 |

### 面试怎么讲评测

> "我们建了两套评测：检索侧 50 题评 Recall 和 MRR，端到端 50 题用 RAGAS 评 Faithfulness、Relevancy 等四个维度。检索侧 Hit@3 达到了 100%，RAGAS 四个指标都在 0.87 以上。这些评测脚本也集成到了 benchmark 目录下，可以随时跑 A/B 对比。比如我们就是通过评测发现 BM25 权重 0.4 是最优的——纯向量 Hit@3 只有 0.94，加了 0.4 权重的 BM25 直接到 1.00。"

---

## 7. 遇到的难点总结

### 难点 1：代码块被切碎导致 LLM 幻觉

**现象**：一段 PromQL 表达式被切成两个 chunk，LLM 拿到半截公式后自己"补全"了剩下的部分，给出了错误的告警规则。

**排查过程**：对比切分前后的 chunk 内容，发现 `RecursiveCharacterTextSplitter` 恰好在代码块中间的换行处切了一刀。

**解决方案**：实现结构保护机制（占位符替换 → 切分 → 还原），参考腾讯 WeKnora 的 6 种保护模式。

### 难点 2：Parent-Child 去重逻辑

**现象**：同一个 parent 下有 5 个 child，检索时命中了其中 3 个，结果 top-3 全是同一段内容的不同片段。

**解决方案**：检索拿到 child 后，按 `parent_id` 去重，同一个 parent 只保留首次命中的（分数最高的那个 child）。实际代码里先多拉 3 倍候选（`_CHILD_OVERFETCH = 3`），去重后再截取 top-k。

### 难点 3：BM25 索引的生命周期管理

**问题**：BM25 索引是纯内存的，进程重启就没了。而且文档上传/删除后，如果 BM25 索引不刷新，会出现"向量检索找到了新文档，但 BM25 那一路还在用旧索引"的数据不一致。

**解决方案**：
- 惰性构建：首次检索时自动从 Milvus 拉全量构建
- 主动刷新：文档上传/删除后，document_service 调 `refresh_bm25_index()` 重建
- 线程安全：构建过程用 `threading.Lock` 保护
- 认清局限：多副本部署时每个副本各自构建，大规模场景应改用 Elasticsearch 托管

---

## 快速回顾清单

| 环节 | 核心设计 | 面试关键词 |
|---|---|---|
| 文档切分 | Parent-Child + 结构保护 | child 找得准、parent 读得全；腾讯 WeKnora |
| 章节前缀 | 标题路径拼到 child 前面 | R@1 提升 10%、零成本优化 |
| 入库脚本 | 分批 + 断点续传 + 指纹校验 | checkpoint、dry-run、reset |
| 向量检索 | Milvus HNSW + COSINE | bi-encoder、粗排 |
| Hybrid 融合 | BM25 + Vector + RRF | 精确 token 互补、量纲无关 |
| Reranker | cross-encoder 精排 | 比 bi-encoder 更准、但每对都要推理 |
| 降级策略 | 每层故障自动退化到上一层 | 绝不中断业务 |
| 评测 | 50 题检索 + 50 题 RAGAS | Hit@3=1.0、Faithfulness=0.913 |

---

*准备好了就说"开始 Step 5"（跳过已合并的 Step 4），我们进入 RAG Chat 多轮对话。或者说"开始 Step 6"直接进入 Agent 诊断链路。*
