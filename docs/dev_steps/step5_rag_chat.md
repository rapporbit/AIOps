# Step 5：RAG Chat 多轮对话 — 会话记忆、问题改写、摘要压缩、受限联网

---

## 这一步要解决的问题

Step 3-4 我们有了一个能"问一次答一次"的 RAG 系统。但真实使用时，用户不会只问一个问题就走：

- **"上面那个 Redis 的问题，具体怎么看慢查询日志？"** — 指代消解："上面那个"指什么？如果不联系上下文，检索系统不知道在问 Redis
- **聊了 20 轮后，context window 快爆了** — 不可能把所有历史都塞进 prompt
- **知识库里没有的信息** — 有时需要联网搜索官方文档补充

RAG Chat 多轮对话要解决这三个问题：**问题改写**（指代消解）、**会话压缩**（控制 token 预算）、**受限联网**（知识库外的补充检索）。

---

## 整体架构

```
用户发送消息 (session_id + question)
    │
    ▼
① 从 Redis 加载 session: summary + recent_messages
    │
    ▼
② Query Rewrite: 用历史 + summary 改写问题为独立检索 query
    │
    ▼
③ 知识库检索: 用改写后的 query 调 advanced_search
    │
    ▼
④ 联网搜索 (可选): 受限主题词过滤，只搜运维相关
    │
    ▼
⑤ LLM 流式生成: system prompt + 历史 + 知识库 context + 联网 context + 问题
    │
    ▼
⑥ 收尾: 写 memory (user + assistant 消息) → compact_if_needed
    │
    ▼
⑦ SSE 事件流返回前端
```

---

## 1. 会话记忆（Redis 存储）

### 存储设计

每个 session 在 Redis 里有三个 key：

| Key | 类型 | 内容 |
|---|---|---|
| `rag:chat:{digest}:messages` | List | 所有原文消息（role + content + ts + sources） |
| `rag:chat:{digest}:summary` | String | 压缩后的会话摘要 |
| `rag:chat:{digest}:meta` | String | 元数据 |

其中 `digest` 是 session_id 的 SHA256 前 32 位，避免用户传入的任意字符串直接作为 Redis key（安全 + 长度控制）。

所有 key 都设了 TTL（默认 7 天），自动过期清理。

### Redis 不可用时的降级

这个降级设计体现了工程思维：

```python
async def _get_redis():
    if not settings.rag_chat_memory_enabled:
        return None      # 功能开关关闭
    if _redis_import_failed:
        return None      # redis 包没装
    if _redis_client is not None:
        return _redis_client  # 已有连接，复用
    try:
        client = Redis.from_url(...)
        await client.ping()
        _redis_client = client
        return client
    except Exception:
        return None      # 连不上，降级
```

Redis 连不上 → 所有 memory 函数返回空 → RAG Chat 退化成单轮问答。日志只打一次 WARNING（`_redis_connect_failed_logged` 标志位），不会每次请求都刷日志。

### 面试话术

> "会话记忆存在 Redis 里，每个 session 三个 key：消息列表、摘要、元数据。Redis 不可用时自动降级成单轮问答，不影响核心功能。这个'可选增强而非必须依赖'的设计在整个项目里一直贯彻——Redis 挂了 RAG 还能用，只是没了多轮能力。"

---

## 2. Query Rewrite（问题改写）

### 解决什么问题

用户的第三轮问题可能是："那如果是集群模式呢？"

单独拿去检索，检索系统不知道在问什么。改写后变成："Redis 集群模式下连接超时如何排查？"——这就是一个自包含的检索 query 了。

### 改写流程

```
输入:
  summary: "用户在问 Redis 连接超时的排查方案，已讨论过单机模式的处理方法"
  recent_messages: [{role: user, content: "Redis 连接超时..."}, {role: assistant, content: "..."}]
  question: "那如果是集群模式呢？"

LLM 改写后:
  "Redis 集群模式下连接超时如何排查"
```

改写用的是轻量快速的 router 级模型（如 `qwen-turbo`），不需要重型模型，控制延迟和成本。

### 关键设计决策

**改写失败回退到原文**：LLM 改写可能超时或返回空，这时直接用用户的原始问题去检索。宁可检索不精准，也不能让用户等着报错。

**输入包含 summary + recent 而不是全部历史**：因为全部历史可能几十条消息几万 token，改写模型的 context window 放不下。用 summary（压缩后的全局上下文）+ 最近 3 轮原文，信息足够且 token 可控。

### 面试追问

**Q：改写会不会引入幻觉？比如改写后的 query 包含了用户没提到的信息？**

> 会有可能，但影响是可控的：改写后的 query 只用于检索（从知识库里找文档），不直接展示给用户。即使改写不准，最差情况是检索到了不太相关的文档，LLM 看完上下文后仍然可以基于真实信息回答。而且 metadata 里记录了 `rewritten_query`，可以回溯排查。

---

## 3. 长会话摘要压缩（Compact）

### 解决什么问题

用户和 RAG Chat 聊了 20 轮，消息列表越来越长。如果全部塞进 LLM 的 prompt：
- token 数可能超过模型的 context window
- token 费用线性增长
- 早期消息的边际价值很低（用户可能已经换了话题）

### Compact 策略

触发条件：消息总数超过 `rag_chat_max_messages`（默认 12 条）。

压缩过程：

```
当前 20 条消息

compact 后:
  summary = LLM 把前 14 条消息压缩成 ≤1200 字的摘要
  messages = 只保留最近 6 条原文消息

下次 LLM 调用时:
  system prompt + summary + 最近 6 条消息 + 新的知识库 context + 新问题
```

### 面试怎么讲

> "我们的 compact 策略是：消息超过 12 条时，把较早的消息用 LLM 压缩成一段 1200 字以内的摘要，只保留最近 6 条原文。这样 prompt 的 token 预算是恒定的——不管用户聊了多少轮，LLM 看到的始终是'一段摘要 + 最近几轮原文'。摘要负责全局上下文，原文保证最近的细节不丢。"

### 关键设计决策

**为什么不直接截断？** 截断（只保留最后 N 条）会丢失前面的上下文。比如用户在第 3 轮说了"我们的 Redis 是 6.2 版本"，到第 15 轮时这个信息已经被截断了，但可能仍然很重要。摘要能保留关键信息。

**compact 失败怎么办？** 保留原历史不动，日志 WARNING。下次触发时再试。不会因为 compact 失败就丢消息。

**为什么 summary 有字符上限？** 防止 LLM 生成的摘要越来越长。如果不限制，每次 compact 都把新信息追加进 summary，summary 本身就会膨胀。

---

## 4. 受限联网搜索

### 设计哲学

RAG Chat 的联网搜索不是"随便搜"，而是**受限联网**——只允许搜索运维技术相关的内容。这个约束贯穿了三层：

### 第一层：前端开关

用户请求里有一个 `web_search: bool` 字段。默认关闭，需要用户主动开启。而且全局开关 `RAG_CHAT_WEB_SEARCH_ENABLED` 必须为 true 才生效（管理员层面的控制）。

### 第二层：主题词白名单

只有当用户的问题命中了预定义的技术主题词时才触发联网：

```
redis, mysql, postgresql, mongodb, elasticsearch, kafka, nginx,
linux, docker, kubernetes, prometheus, grafana, jvm, java, python,
go, nodejs, fastapi, langchain, langgraph, milvus...
```

用户问"明天天气怎么样"→ 不命中任何主题词 → 不搜索。

### 第三层：黑名单 + 脱敏

即使通过了白名单，还要过黑名单和脱敏检查：

- **黑名单**：`password`、`api_key`、`secret`、`私钥` 等
- **脱敏正则**：IP 地址、API Key（`sk-xxx`）、Bearer Token、手机号、身份证号

任何一个命中 → 直接拒绝搜索，返回拒绝原因。

### MCP WebSearch Server 的额外硬约束

联网搜索的 provider 是一个独立的 MCP Server（`websearch_server.py`），它自己又加了一层限制：

- **内容黑名单**：动漫、游戏、娱乐、政治、色情等关键词
- **限频**：60 秒内最多 20 次调用

### 面试话术

> "联网搜索是受限的，三层过滤：第一层前端开关 + 全局开关；第二层主题词白名单，只允许搜运维技术关键词；第三层黑名单 + 脱敏正则，拦截敏感信息。即使 LLM 被 prompt injection 诱导去搜不该搜的东西，这三层硬约束也会拦住。这个设计参考的是'零信任'原则——不信任用户输入，也不信任 LLM 的判断。"

---

## 5. SSE 流式输出

### 事件类型

`stream_chat` 是一个 async generator，yield 不同类型的事件：

| 事件类型 | 含义 | 示例 |
|---|---|---|
| `progress` | 阶段提示 | "正在改写问题..."、"正在检索知识库..." |
| `thinking` | 思维链 token | Qwen3 等支持思考的模型会输出推理过程 |
| `token` | 答案 token | LLM 生成的答案文本片段 |
| `sources` | 引用来源 | 知识库命中的文档列表 |
| `retrieval` | 检索详情 | top_k、candidate_k、是否开了 hybrid/rerank |
| `usage` | token 统计 | input_tokens、output_tokens、延迟 |
| `error` | 错误 | LLM 调用失败等 |
| `end` | 流结束 | — |

### 两条路径

`stream_chat` 有两条执行路径：

**有 MCP 工具时**：用 `run_parallel_agent`，让 LLM 自主决定要不要调工具（比如查系统状态）。LLM 调工具 → 拿到结果 → 继续生成答案。最多 3 轮工具调用。

**无 MCP 工具时**：纯流式 `llm.astream()`，直接生成答案。

### 面试追问

**Q：为什么 RAG Chat 也能调 MCP 工具？这和 Agent 诊断有什么区别？**

> RAG Chat 调的是**只读**工具——只能查看系统状态（比如 `check_disk_usage`），不能执行写操作（比如重启容器）。工具筛选器 `_select_rag_tools()` 会过滤掉所有非只读工具和诊断专用工具。这样用户在聊天时可以说"帮我看看当前 Redis 内存占用多少"，LLM 调工具查完数据后结合知识库内容给出建议。和 Agent 诊断的区别是：诊断是自主多步推理（Plan → Execute → Replan），Chat 是"看一眼就回答"，最多 3 轮工具调用。

---

## 6. 完整的一次请求流程（面试串讲版）

面试时可以用这个叙述串起来：

> "用户发来一条消息，带着 session_id。我先从 Redis 加载这个 session 的 summary 和最近几轮对话。然后用一个轻量模型做 query rewrite——把用户的指代消解掉，比如把'那如果是集群模式呢'改写成'Redis 集群模式下连接超时如何排查'。
>
> 改写后的 query 送入 advanced_search 做知识库检索（Vector + BM25 + Reranker 三级流水线，上一步讲过的）。如果用户开了联网搜索，还会过一遍主题词白名单和脱敏检查，通过了才去搜。
>
> 然后把 system prompt、历史消息、知识库 context、联网 context、用户问题拼成 messages，送给 LLM 流式生成。如果有 MCP 只读工具可用，LLM 可以自主决定要不要调工具查实时数据。
>
> 最后把这轮 Q&A 写回 Redis。如果消息总数超过 12 条，触发 compact——把旧消息压缩成摘要，只保留最近 6 条原文。这样不管聊多少轮，prompt 的 token 预算始终可控。"

---

## 7. 遇到的难点总结

### 难点 1：检索和联网的串行延迟

改写必须先完成（后续检索和联网都依赖改写后的 query），这一步的延迟不可压缩（~500ms）。但改写之后，知识库检索（~300ms）和联网搜索（~500ms）如果串行执行就要再等 800ms。

**解决方案**：代码里用 `asyncio.create_task()` 把 `build_context(rewritten_question)` 和 `build_web_context(rewritten_question)` 并行发出，两者都用改写后的 query，总延迟取两者的 max 而不是 sum。这是实际代码里已经做了的优化，不是"可以做"而是"已经做了"。

### 难点 2：compact 时机的选择

compact 调一次 LLM 生成摘要需要几秒钟。如果放在用户等待的请求路径里，用户会感知到延迟。

**解决思路**：compact 放在**回答完成后**（写完 memory 之后），这样不阻塞当前回答的流式输出。用户看到回答后，后台异步做 compact，下次请求时 summary 就已经更新好了。

### 难点 3：诊断报告的跨 session 共享

AIOps 诊断模块生成的报告（比如"Redis OOM 根因是大 key"）存在诊断的 session 里，但 RAG Chat 用的是另一个 session（`web-chat`）。

**解决方案**：用一个全局的 Redis list（`rag:diagnosis:reports`），诊断完成后把报告写进去（最多保留最近 5 份，每份截断到 8KB）。RAG Chat 联网搜索时会读取这些报告，提取术语作为联网搜索的白名单依据。

---

## 快速回顾清单

| 模块 | 核心设计 | 面试关键词 |
|---|---|---|
| 会话记忆 | Redis 三 key + SHA256 digest + TTL | 降级到单轮、不阻断核心功能 |
| Query Rewrite | summary + recent → LLM 改写 → 独立 query | 指代消解、轻量模型、失败回退原文 |
| Compact | 超 12 条 → 压缩旧消息 → 保留最近 6 条 | 恒定 token 预算、失败不丢消息 |
| 受限联网 | 前端开关 + 主题词白名单 + 黑名单/脱敏 | 零信任、三层过滤 |
| SSE 流式 | progress/thinking/token/sources/usage 事件 | 两条路径（有/无 MCP 工具） |

---

*准备好了就说"开始 Step 6"，我们进入 Agent 诊断链路的核心——Skill Registry & Router。*
