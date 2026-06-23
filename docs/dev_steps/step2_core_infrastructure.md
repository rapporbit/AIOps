# Step 2：Core 基础设施层 — LLM 工厂、Embedding 封装、Milvus 连接管理

## 这一步要解决的问题

AIOps 平台的上层模块（Planner、Executor、RAG 检索、Skill Router……）都需要调用大模型和向量数据库，但这些底层依赖有很多变数：

- **LLM 可能换**：开发阶段用 DashScope Qwen，有人想试 DeepSeek，断网时要能切 Ollama 本地模型。
- **Embedding 可能换**：DashScope `text-embedding-v4` 和 Ollama `bge-m3` 都要支持。
- **Milvus 连接需要统一管理**：多个模块不能各自建连接，启动/关闭要有序。

Core 层的职责就是：**把这些变数封装起来，让上层只关心"给我一个 LLM / 给我一个向量库"，不关心底层用了什么。**

---

## 模块总览

```
app/core/
├── llm.py             # LLM 工厂 — 根据模型名自动路由到 DashScope / DeepSeek / Ollama
├── llm_health.py      # LLM 健康探测 — TCP 层探测主 LLM 是否可达
├── llm_parse.py       # LLM 输出解析 — 从 LLM 的自由文本里抠出 JSON
├── structured.py      # 结构化输出 — 让 LLM 返回 Pydantic 对象而不是自由文本
├── embedding.py       # Embedding 工厂 — DashScope / Ollama 双后端
├── milvus.py          # Milvus 底层连接管理（pymilvus 直连）
├── vector_store.py    # Milvus 高层封装（langchain_milvus，RAG 用）
└── mcp_client.py      # MCP 工具客户端管理（Step 8 细讲）
```

---

## 1. LLM 工厂 (`llm.py`)

### 设计思路

整个项目只有一个入口获取 LLM：`get_chat_llm()`。所有上层模块（Planner、Executor、Router、RAG Chat……）都调这一个函数，不允许直接 `new ChatOpenAI()`。

这样做的好处：**改模型、加降级、加监控，只需要改这一个文件。**

### 路由逻辑

`get_chat_llm()` 内部的路由是三级瀑布式的：

```
调用 get_chat_llm(model="qwen-max")
     │
     ▼
① 检查是否需要走本地 LLM（_should_use_local_llm）
   ├── local_llm_force=True？ → 直接走 Ollama
   ├── API Key 没配？ → 走 Ollama
   └── TCP 探测主 LLM 不可达？ → 走 Ollama
     │
     ▼（不走本地）
② 看 model 名字
   ├── 以 "deepseek" 开头 → 路由到 DeepSeek 的 base_url + api_key
   └── 其他 → 走 DashScope（默认）
```

### 面试怎么讲这个设计

> "我们做了一个 LLM 工厂，上层不感知底层用了哪个模型提供商。路由逻辑是：先看要不要降级到本地（断网或没配 Key），再按模型名分发到 DashScope 或 DeepSeek。所有提供商都走 OpenAI 兼容协议，所以底层统一用 LangChain 的 ChatOpenAI，只是 base_url 和 api_key 不同。"

### 亮点：自动降级到本地 LLM

这个是面试可以重点展开的亮点。降级判断逻辑 `_should_use_local_llm()` 的优先级很清晰：

1. `local_llm_force=True` → 强制本地（离线开发用）
2. API Key 没配或无效 → 没法调云端，只能走本地
3. `local_llm_enabled=True` 且 TCP 探测不可达 → 自动降级

> **面试话术**："真实运维场景下，网络不一定稳定。我们做了一个自动降级机制：在每次调用 LLM 之前，先用 TCP 探测看主 LLM（DashScope）能不能连上。如果连不上，就自动切到本地的 Ollama。探测结果缓存 30 秒，避免每次调用都探测带来的开销。"

### 亮点：TCP 层探测而不是真实 API 调用

`llm_health.py` 的设计也值得讲：

- **不发真实 API 调用**，不消耗 token，不计费，只做 TCP 端口探活
- **探测结果缓存** N 秒（默认 30s），用 `threading.Lock` 做线程安全
- **状态变化才打 INFO 日志**，稳定时只打 DEBUG，避免日志刷屏
- **双重检查锁**：进 Lock 之前先看缓存有没有过期，进了之后再看一次（其它线程可能已经更新了）

### 遇到的难点

**难点 1：DeepSeek v4 的思考模式问题**

DeepSeek v4 默认开启思考模式（thinking mode），在 assistant message 里会多返回一个 `reasoning_content` 字段。如果下一轮请求不把这个字段原样回传，API 直接返回 400 报错。

但是 Agent 的 tool loop 根本不维护 `reasoning_content`（LangChain 的标准消息结构里没这个字段），所以我们在工厂里**默认关掉 DeepSeek 的思考模式**：

```python
ds_extra.setdefault("thinking", {"type": "disabled"})
```

想开的话可以在调用时显式传参，但默认是关的，保证 Agent 能正常跑。

**难点 2：Qwen3 非流式模式必须关 thinking**

类似的问题，DashScope 的 Qwen3/QwQ/QvQ 模型在非流式调用时，如果不显式 `enable_thinking=False`，会走思考模式导致返回格式变化。我们做了模型名检测，自动加上这个参数。

### 面试追问

**Q：为什么不用 lru_cache 缓存 LLM 实例？**

> 因为 `get_chat_llm()` 的参数组合很多：temperature、streaming、timeout 都因调用方不同而不同。Planner 用 `temperature=0`（要确定性），Report 用 `temperature=0.3`（要点创造性），SSE 场景用 `streaming=True`。如果缓存了，不同参数的调用方会拿到同一个实例，参数就串了。所以每次返回新实例，开销只是 Python 对象创建，真正的连接复用在 httpx 层面。

**Q：如果 DashScope 挂了 5 分钟又恢复了，你的系统怎么表现？**

> 前 30 秒的请求发现不可达，自动降级到 Ollama。30 秒后缓存过期，重新探测，发现恢复了，自动切回 DashScope。中间用户可能感受到的是：有 30 秒的回答质量从云端大模型降到了 7B 小模型，但服务没有中断。日志里会打一条 WARNING "主 LLM 不可达" 和一条 INFO "主 LLM 已恢复可达"。

---

## 2. 结构化输出 (`structured.py` + `llm_parse.py`)

### 设计思路

Agent 系统很多地方需要 LLM 返回**结构化数据**而不是自由文本。比如 Planner 要返回一个步骤列表，Skill Router 要返回一个 skill 名字。

LangChain 的 `with_structured_output()` 在 OpenAI 官方 API 上很好用，但 DashScope、DeepSeek 这些国产兼容接口对 Pydantic response_format 的支持参差不齐。

我们的解决方案是：**统一走 JSON 模式 + 手动 Pydantic 校验**，不依赖 SDK 级别的 structured output。

### 核心流程

```
LLM 调用时注入 system prompt："你必须只输出一个合法的 json 对象"
    │
    ▼
设置 response_format={"type": "json_object"}
    │
    ▼
拿到 LLM 返回的原始文本
    │
    ▼
extract_json()：去掉 ```json 围栏 → 找首个 { 到末个 } → json.loads → 校验是 dict
    │
    ▼
schema_cls.model_validate(data)：用 Pydantic 做字段校验
```

### 遇到的难点

**DashScope 对 "json" 关键词的严格校验**

DashScope 的兼容接口有一个坑：当你设置了 `response_format={"type": "json_object"}` 时，它要求 messages 里**必须出现小写的 "json" 字样**，否则直接 400。OpenAI 官方是大小写不敏感的，DashScope 更严格。

解决方案是在注入的 system prompt 里多写几次 "json"：

> "你必须只输出一个合法的 json 对象（严格 json 格式，小写 json）"

看起来啰嗦，但就是为了过 DashScope 的检查。

### 面试追问

**Q：为什么不直接用 LangChain 的 with_structured_output？**

> 试过，但 DashScope 和 DeepSeek 对 Pydantic schema 的 response_format 支持不完整，有的模型会报 400，有的会返回格式不对。我们退一步，用 `response_format={"type": "json_object"}` 加手动解析，兼容性最好。牺牲的是"类型安全由 API 侧保证"这个优势，但我们自己加了 Pydantic model_validate 来兜底，效果一样。

---

## 3. Embedding 工厂 (`embedding.py`)

### 设计思路

和 LLM 工厂类似，Embedding 也做了双后端支持：DashScope `text-embedding-v4` 和 Ollama `bge-m3`。都实现 LangChain 的 `Embeddings` 接口，上层 Milvus/RAG 代码完全不感知用了哪个。

用 `lru_cache(maxsize=1)` 做单例（和 LLM 不同，Embedding 不需要不同参数的实例，一个就够）。

### 遇到的难点

**DashScope 的批量限制**

DashScope `text-embedding-v4` 单次最多接受 10 个文本。但 LangChain 的 `OpenAIEmbeddings` 默认 `chunk_size=2048`，会把所有文本一次性发出去，超过 10 个就 400。

解决方案很简单但容易忽略：显式设置 `chunk_size=10`。

另一个小坑：`check_embedding_ctx_length=False`——因为 DashScope 不走 tiktoken 的 token 计数方式，开着会报错，直接关掉。

### Ollama Embedding 的自定义适配

LangChain 没有直接支持 Ollama `/api/embed` 接口的 Embeddings 实现，我们自己写了一个 `OllamaEmbeddings` 类，实现 `embed_documents()` 和 `embed_query()` 两个方法，内部用 httpx 调 Ollama 的 REST API，支持分批处理。

### 面试追问

**Q：换 Embedding 模型需要做什么？**

> 换 Embedding 模型意味着向量维度可能变了（比如从 1024 变成 768），而旧的 Milvus collection 里存的都是旧维度的向量，不能混用。所以换模型必须：(1) 改 `.env` 里的配置；(2) drop 旧的 collection；(3) 重新跑入库脚本把所有文档重新向量化入库。这是有成本的操作，不能随便换。

---

## 4. Milvus 连接管理：为什么分两层？

这个架构分层是面试值得讲的设计决策。

### 底层：`milvus.py` — 直接用 pymilvus

`MilvusManager` 类负责**连接生命周期管理**：connect / disconnect / is_alive / has_collection。它用 pymilvus 的原生 API，提供精细控制能力（健康检查、维度校验、强制删表重建）。

作为全局单例 `milvus_manager = MilvusManager()`，由 `main.py` 的 lifespan 钩子控制启动和关闭。

### 高层：`vector_store.py` — 用 langchain_milvus 包一层

`get_vector_store()` 返回一个 LangChain 标准的 `VectorStore` 接口实例。这样上层的 RAG 检索、Retriever 等组件可以直接用 LangChain 生态的标准方法（`similarity_search`、`add_documents` 等），不用关心底层是 Milvus 还是别的向量库。

### 为什么不合并？

> "底层给运维用（健康检查、表管理），高层给业务用（检索、入库）。如果合并成一层，要么运维操作要绕过 LangChain 的抽象（比如 drop collection），要么业务检索要直接拼 pymilvus 的 API。分两层各司其职，互不干扰。"

### 遇到的难点：pymilvus 新旧 API 连接注册表冲突

这是一个**真实踩过的坑**，面试讲出来很加分：

`langchain_milvus 0.3+` 底层用了 pymilvus 的两套 API：
- **新 API**（MilvusClient）：创建连接时自动生成一个 alias（形如 `cm-xxxxxx`），注册在自己的内部注册表里
- **旧 API**（pymilvus.orm.Collection）：从另一个注册表（`pymilvus.connections`）查连接

问题是：**这两个注册表不互通**。MilvusClient 建了连接，但 Collection 那边查不到，直接抛 `ConnectionNotExistException`。

解决方案：先创建 MilvusClient 拿到它内部的 alias，再手动用 `connections.connect()` 把同一个 alias 注册到 ORM 的注册表里，让两套 API 共享同一个连接。

> **面试话术**："这个问题花了不少时间排查。报错是 ConnectionNotExistException，表面看像是 Milvus 没连上，但实际上连接是好的，只是 pymilvus 内部新旧两套 API 用了不同的连接注册表。最终的解决方法是手动把新 API 生成的连接 alias 注册到旧 API 的注册表里，让它们共享连接。"

---

## 5. HNSW 索引参数的选择

VectorStore 初始化时配了 HNSW 索引参数，面试可能会问为什么选这些值：

- **`metric_type: COSINE`**：文本语义检索标准选择，关注方向相似度而不是绝对距离
- **`M: 8`**：每个节点的最大邻居数。越大召回越好但内存越大。本地 standalone 环境选 8（偏保守），生产可以调到 16-32
- **`efConstruction: 64`**：建索引时的搜索宽度，影响索引质量。64 是平衡点
- **`ef: 128`（查询时）**：查询时的搜索宽度，必须 ≥ top-k。越大越准但越慢

---

## 6. 整体设计哲学总结

面试最后可以用一两句话收束这一层的设计哲学：

> "Core 层的核心原则是**屏蔽变化、暴露统一接口**。上层模块不关心你用的是 DashScope 还是 DeepSeek，不关心 Milvus 用的是新 API 还是旧 API，也不关心网络断了怎么办。这些变化全部封装在 Core 层里面，通过工厂函数和 Manager 类对外暴露稳定的接口。任何一个底层组件出问题或者要替换，只改 Core 层的一个文件，上层代码不用动。"

---

## 快速回顾清单

| 模块 | 核心设计 | 面试关键词 |
|---|---|---|
| `llm.py` | 三级瀑布路由 + 自动降级 | 工厂模式、OpenAI 兼容协议、断网降级 |
| `llm_health.py` | TCP 探测 + 缓存 + 双重检查锁 | 不消耗 token、探测结果缓存、状态变化日志 |
| `structured.py` | JSON 模式 + 手动 Pydantic 校验 | 兼容国产 API、DashScope json 关键词坑 |
| `embedding.py` | 双后端 + lru_cache 单例 | chunk_size=10 限制、换模型要重建 collection |
| `milvus.py` | 底层连接生命周期管理 | 健康检查、幂等连接/断开 |
| `vector_store.py` | LangChain VectorStore 接口 | 新旧 API 连接注册表冲突、HNSW 参数 |

---

*准备好了就说"开始 Step 3"，我们进入 RAG 知识库的文档 Ingest Pipeline。*
