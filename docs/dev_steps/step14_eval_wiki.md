# Step 14：评测体系 & LLM Wiki 经验沉淀

> 本步合并了原规划的 Step 14（评测 & 压测）和附加篇（LLM Wiki），因为两者都是"如何证明系统有效 + 如何让系统越用越好"的闭环。

---

## 一、评测体系

### 为什么需要评测

RAG 系统最大的风险是"看着能用但没有量化"。换了 Embedding 模型、调了 BM25 权重、改了 chunk 大小，效果是好了还是坏了？没有评测就是盲调。

我们建了两套独立评测：**检索侧 50 题**和**端到端 RAGAS 50 题**，覆盖 10 个运维场景。

---

## 1. 检索侧评测（50 题 Recall@K）

### 评测内容

50 道真实运维问题，覆盖 Redis、MySQL、Nginx、JVM、Kafka、Docker、Linux、Prometheus、通用 OnCall、网络连通性，每个场景 5 题。

每道题有标注的 `relevant` 文档（gold answer），检索系统返回 top-k 后，对比 gold 计算指标。

### 三个核心指标

| 指标 | 含义 | 公式 |
|---|---|---|
| **Hit@K** | top-k 里至少命中一个 gold 的比例 | 命中数 / 总题数 |
| **MRR@K** | 第一个命中 gold 的排名倒数的均值 | mean(1/first_hit_rank) |
| **Recall@K** | top-k 覆盖了多少独立知识点组 | 覆盖组数 / 总组数 |

### 知识点组（relevant_groups）设计

这是评测设计里一个值得讲的细节：

旧格式 `relevant: [A, B, C]` 表示 A/B/C 是同一个知识点的三个替代来源，命中任一即可。但有些问题需要同时覆盖两个独立知识点（比如"Redis OOM 的原因和处理方法"需要"原因"和"处理"两块文档）。

新格式 `relevant_groups: [[A, B], [C, D]]`：组内是 OR（A 和 B 是替代来源），组间算覆盖率（两组都要命中 recall 才是 1.0）。

### 实测结果

| 配置 | Hit@3 | MRR@3 | Recall@3 |
|---|---:|---:|---:|
| 纯向量（bm25_weight=0.0） | 0.94 | 0.89 | 0.94 |
| Hybrid（bm25_weight=0.4） | **1.00** | **0.93** | **1.00** |

### 评测驱动的调参

这段"用数据说话"的故事面试时很加分：

> "我们跑了 BM25 权重从 0.0 到 0.5 的对比。0.0-0.3 结果一样（Hit@3=0.94），说明这些 MISS case 的关键 token 不在向量检索的优势区间。bm25_weight=0.4 时 Hit@3 跳到 1.00——正好是那 3 个 MISS case 的精确 token（错误码、服务名）被 BM25 捞上来了。0.5 和 0.4 结果一样，说明 0.4 已经是平衡点。这就是为什么最终选 0.4。"

### 跑评测的命令

```bash
# 基线
python benchmark/run_benchmark.py retrieval --k 3

# A/B 对比：关掉 rerank
python benchmark/run_benchmark.py retrieval --k 3 --no-rerank

# A/B 对比：关掉 hybrid
python benchmark/run_benchmark.py retrieval --k 3 --no-hybrid

# 只跑某个场景
python benchmark/run_benchmark.py retrieval --scenario Kafka --k 3
```

---

## 2. 端到端 RAGAS 评测（50 题）

### 评测内容

在检索基础上，走完整 RAG 流程：检索上下文 → LLM 生成回答 → 评判模型打分。

### 四个 RAGAS 指标

| 指标 | 衡量什么 | 通俗解释 |
|---|---|---|
| **Faithfulness** | 答案是否忠于检索到的上下文 | LLM 有没有"编" |
| **AnswerRelevancy** | 答案是否切题 | 有没有答非所问 |
| **ContextPrecision** | 检索结果是否相关 | 检索回来的东西有没有用 |
| **ContextRecall** | 检索是否覆盖了标准答案的信息 | 有没有漏掉关键信息 |

### 两个 OpenEvals 指标（补充）

| 指标 | 衡量什么 |
|---|---|
| **Groundedness** | 回答是否由检索上下文支持（类似 Faithfulness 但评判逻辑不同） |
| **Helpfulness** | 回答是否真正解决了用户的问题 |

### 实测结果

| faith | rel | cprec | crecall | ground | help |
|---:|---:|---:|---:|---:|---:|
| 0.913 | 0.936 | 0.997 | 0.871 | 0.994 | 0.872 |

### 面试怎么讲这六个指标

> "我们用 RAGAS 的四个标准指标加 OpenEvals 的两个补充指标做端到端评测。Faithfulness 0.913 说明 LLM 偶尔会超出上下文推理，但不严重；ContextRecall 0.871 是最低的，说明有些标准答案的信息在 top-3 里没完全覆盖——这是后续优化的方向，可能需要增加召回数量或补充语料。Groundedness 0.994 说明几乎所有回答都有上下文依据。"

### 低分分析接口

项目提供了一个实用的 API 来定位弱项：

```
GET /api/v1/eval/reports/{name}/low-scores?threshold=0.5&metric=faithfulness
```

返回低于阈值的题目列表，直接告诉你"哪些题需要补语料"。这让评测闭环可操作——不是跑完看个均分就完了，而是精确到每道题。

---

## 3. 面试追问集

**Q：Faithfulness 和 Groundedness 有什么区别？**

> Faithfulness 是 RAGAS 框架的指标，通过分解答案为多个 claim，检查每个 claim 是否能从 context 推导。Groundedness 是 OpenEvals 的指标，用另一个 LLM 直接判断答案是否由 context 支持。两者目标类似但评判方法不同，互相补充。

**Q：Context Recall 0.871 偏低，你打算怎么优化？**

> 有几个方向：增大 retrieve_k（从 20 到 30，给 Reranker 更多候选）；补充语料（low-scores 接口定位了缺失的知识点）；调整 chunk 大小（某些 SOP 文档的关键信息分布在不同段落，可能需要更大的 parent chunk）。每次改动后跑一遍 benchmark 做 A/B 对比。

**Q：评测用的 LLM judge 和生成答案的 LLM 是同一个吗？会不会有 bias？**

> RAGAS 的 judge 和生成答案可以用不同模型来降低 bias。不过即使用同一个模型，Faithfulness 检查的是"答案是否和 context 一致"，这是事实校验而非风格判断，同模型 bias 的影响有限。OpenEvals 的 Groundedness 和 Helpfulness 用的是另一个评判逻辑，可以作为交叉验证。

---

## 二、LLM Wiki 经验沉淀

### 设计哲学

这是整个项目最后的闭环——让系统**越用越好**。

灵感来自 Karpathy 的 LLM Wiki 思想：

> "别用 RAG 每次从原始文档重检索，而让 LLM 持续维护一个结构化、互链的 wiki。知识是'合并沉淀'而非'每次重查'。"

落到 AIOps 场景：每次诊断完成后，把根因和修复方法**合并**到 wiki 里。下次遇到类似故障，先读 wiki 获取历史经验，再开始新诊断。

---

## 4. Wiki 目录结构

```
data/wiki/
├── index.md          # 目录（全部页面的一行摘要 + wikilink）
├── log.md            # 流水（每次 ingest 的时间和摘要）
├── services/         # 按服务聚合
│   ├── redis.md      # Redis 的所有故障模式汇总
│   └── mysql.md
├── patterns/         # 按故障模式聚合
│   ├── redis-oom.md  # Redis OOM 的现象/根因/处置
│   └── disk-full.md
└── .write.lock       # 写锁文件
```

两个维度：`services/` 按服务聚合（Redis 出过哪些问题），`patterns/` 按故障模式聚合（OOM 怎么排查）。用 `[[services/redis]]` 和 `[[patterns/redis-oom]]` 互链。

---

## 5. 写入：ingest_diagnosis

### 触发时机

每次诊断完成后（Fast 模式的 report 或 Deep 模式的 ReportAgent），自动调 `ingest_diagnosis()`。

### 流程

```
诊断报告 + 告警签名
    │
    ▼
解析目标：从告警签名/query 提取 service 和 pattern slug
    │
    ▼
读取现有页面（service 页 + pattern 页）
    │
    ▼
LLM 合并：把这次诊断的现象/根因/修复 merge 进现有页面
    │
    ▼
更新 index.md（一行摘要 + wikilink）
    │
    ▼
追加 log.md（流水记录）
```

### 关键设计：合并而非追加

LLM 的 system prompt 要求"同类信息**更新**而非重复堆叠"。比如 Redis OOM 这个 pattern 页，第一次诊断写入了"大 key 导致 OOM"，第二次诊断发现是"maxmemory 配置过低"，LLM 会把两者合并到"## 根因"章节，而不是追加一段新的。

### LLM 失败时的确定性兜底

这是一个重要的取舍：

```python
except Exception as exc:
    # LLM 不可用 → 确定性兜底：追加模式
    entry = f"\n## [{date}] {mode}\n- 现象: {query[:200]}\n"
    pat_md = existing_pat + entry  # 退化成 append
```

LLM 挂了就退化成简单的追加——信息不会丢失，只是没有"合并去重"的智能。下次 LLM 恢复时，ingest 会看到冗余内容并合并掉。

### 面试话术

> "wiki 的写入是 best-effort 的。LLM 可用时做智能合并——同类信息更新而不是重复堆叠。LLM 不可用时退化成追加——信息不丢，只是等 LLM 恢复后再整理。整个 ingest 在 try-except 里，绝不拖垮诊断主链路。"

---

## 6. 读取：渐进式披露（L1/L2/L3）

借鉴 Claude Skill 的**渐进式披露（progressive disclosure）**：把"一次性整页注入"拆成三层按需加载，每层注入到它真正有用的节点，让 Router 不被大段正文带偏、也省 token。三层共用同一套 read-index-first 选页逻辑（`_select_pages`）。

| 层 | 注入节点 | 内容 | 函数 | 开关 |
|---|---|---|---|---|
| **L1 目录** | Router | 命中页的目录行（`- [[ref]] — 摘要`） | `recall_index_block()` | `wiki_router_recall_level=index`（默认） |
| **L2 正文** | Planner | 命中页的整页正文（现象/根因/处置） | `recall_block()` | `wiki_planner_recall_enabled=true` |
| **L3 钻取** | Executor | 按 `[[链接]]` 拉单页 | `read_wiki_page()` 工具 | `wiki_page_tool_enabled=true` |

核心思想：**Router 看目录、Planner 读正文、Executor 才按链接钻**——而不是开局把整页正文全塞给只负责选 skill 的 Router。Router 可用 `wiki_router_recall_level=full` 一键回退到旧的整页注入行为。

### 选页策略：read-index-first（L1/L2 共用）

```
Step 1: 直达页
    从告警签名提取 service 和 pattern
    如果 services/{service}.md 和 patterns/{pattern}.md 存在 → 直接选中

Step 2: 兜底 — 读 index 关键词匹配
    如果没有直达页 → 读 index.md
    把 query 和 index 每行做关键词重叠打分
    取 top-2 相关页

L1 输出: 选中页的 index 目录行（轻量，给 Router）
L2 输出: 选中页的整页正文拼成 markdown 注入块（≤2000/2500 字，给 Planner）
```

为什么不用 Embedding 向量检索？

> "wiki 的页面数量很少（几十页），用向量检索是大炮打蚊子。关键词匹配加 read-index-first 就够了，零额外依赖。而且直达页是最精确的——告警签名直接映射到 pattern slug，不需要语义理解。"

L3 的 `read_wiki_page()` 接收 LLM 生成的 ref，是任意读入口，做了**双重防御**：正则白名单 `^(services|patterns)/<slug>$`（拒 `..`/绝对路径/额外 `/`）+ `resolve().relative_to(_WIKI_DIR)` 二次确认落点仍在 wiki 目录内。只读工具，由 `tool_filter` 的 auto-readonly 自动对全 Skill 放行，无需改 SKILL.md。

---

## 7. 并发安全：asyncio.Lock + fcntl

Wiki 可能被多个来源同时写：API 进程里的 SSE 诊断 + Worker 进程的后台诊断。

```python
@asynccontextmanager
async def _wiki_write_guard():
    async with _write_lock:            # 单进程内 asyncio.Lock
        with open(_LOCK_FILE, "w") as f:
            fcntl.flock(f, fcntl.LOCK_EX)   # 跨进程 fcntl 文件锁
            try:
                yield
            finally:
                fcntl.flock(f, fcntl.LOCK_UN)
```

双重锁：asyncio.Lock 保护协程级并发，fcntl 保护进程级并发。

---

## 8. lint：结构健康检查

确定性的 wiki 质量检查（不调 LLM）：

| 检查项 | 含义 |
|---|---|
| orphan | 没有任何页面链接到它的孤立页 |
| not_in_index | 存在但没有被 index.md 收录 |
| empty | 空页（建了但没写内容） |

暴露为 API `/api/v1/wiki/lint`，前端"经验库"页面可以展示健康状态。

---

## 9. 遇到的难点总结

### 难点 1：评测题的 gold answer 设计

最初每道题只标注了一个 gold 文档。但运维知识库里同一个知识点可能在多个文档里出现（SOP、告警规则、troubleshooting guide），命中任意一个都算对。

用旧的单 gold 评测，Hit@3 只有 0.80；改成 relevant_groups（组内 OR）后 Hit@3 变成 0.94——不是检索变好了，是评测更公平了。

### 难点 2：Wiki 合并的质量控制

LLM 合并时偶尔会"过度创造"——把两次诊断的信息错误关联。比如 Redis OOM 和 Redis 主从同步延迟是两个不同的故障模式，但 LLM 可能把它们合并到一页里。

解决思路：pattern slug 的粒度要够细。`redis-oom` 和 `redis-repl-lag` 是两个不同的 pattern 页，LLM 只在同一页内合并，不会跨页。slug 的生成来自告警签名（`compute_alert_signature`），确保不同故障类型不会碰撞。

### 难点 3：Wiki 召回注入的 token 预算

Wiki 内容注入到 prompt 里会占 token。如果 Redis 服务页写了 5000 字（累积了十几次诊断经验），全部注入会挤占知识库 context 的空间。

解决方案：`wiki_recall_max_chars=2000` 做硬截断。足够覆盖最相关的根因/修复信息，又不会喧宾夺主。

---

## 快速回顾清单

| 模块 | 核心设计 | 面试关键词 |
|---|---|---|
| 检索评测 | 50 题 × Hit/MRR/Recall + relevant_groups | BM25 调参 0.4 最优、A/B 对比 |
| RAGAS 评测 | 50 题 × 4 指标 + 2 OpenEvals | faith=0.913、crecall 是短板 |
| low-scores API | 低分题精确定位 | 闭环可操作、"补哪些语料" |
| 压测 | loadtest.py 6 场景 | 数据见 Step 12 |
| Wiki 写入 | LLM 合并 + 确定性兜底 | 合并而非追加、best-effort |
| Wiki 读取 | L1 目录/L2 正文/L3 钻取 + read-index-first | 渐进式披露、每层独立开关 |
| Router 评测 | 16 题 × A/B 对比 (full vs index) | 路由零回归、eval_router.py |
| Wiki 并发 | asyncio.Lock + fcntl | 双重锁 |
| Wiki lint | 孤页/未索引/空页检查 | 确定性、不调 LLM |

---

## 面试收尾话术

如果面试官问"项目整体最大的亮点是什么"，可以用评测和 Wiki 收尾：

> "这个项目不只是'能跑'，而是'能量化、能进化'。检索和 RAGAS 评测让每一次参数调整都有数据支撑。LLM Wiki 让系统越用越好——每次诊断的经验自动沉淀，下次类似故障直接召回。整个循环是：**告警 → 诊断 → 沉淀经验 → 下次诊断更快更准 → 评测验证效果**。"

---

*到这里 14 步全部完成！你可以回到任何一步做深入练习，也可以让我针对某个模块做模拟面试追问。*
