# Step 9：Deep 诊断图 — 多 Agent 并行取证与证据归并

---

## 这一步要解决的问题

Fast 模式是单 Agent 线性排查（Plan → Execute → Replan）。对于简单故障足够了，但复杂故障需要**多个维度同时调查**：查指标、查日志、查基础设施、查 SOP——这些如果串行做，太慢；如果让一个 Agent 同时干所有事，context window 很快就爆了。

Deep 模式的核心思想：**让不同专业 Agent 分工取证，但避免多个 Agent 互相聊天导致上下文失控。**

---

## Deep vs Fast 的核心区别

| 维度 | Fast | Deep |
|---|---|---|
| Agent 数量 | 1 个（Executor 包办） | 4 个专业 + 3 个判定/报告 |
| 编排方式 | 动态循环（Replanner 决定走向） | 确定性编排图（固定 8 个节点） |
| 上下文管理 | 单一共享 state | 专业 Agent 隔离上下文，只通过 Evidence 交换 |
| LLM 交互 | Agent 之间可能看到彼此的工具调用 | Agent 之间**完全不读**彼此的中间推理 |
| 适用场景 | 日常快速诊断 | 复杂/多组件/需要深度根因分析 |

---

## 图结构（8 个节点）

```
[START]
    │
    ▼
① IncidentManager         载入诊断对象（task/incident/group）
    │
    ▼
② CorrelationContext      聚合同组告警 + 相邻事件 + Wiki 经验
    │
    ▼
③ EvidencePlan            规则路由 → 决定派哪些专业 Agent
    │
    ┌────────┼──────────┬──────────┐     ← fan-out（并行）
    ▼        ▼          ▼          ▼
④ LogAgent MetricAgent InfraAgent RunbookAgent
    └────────┴────┬─────┴──────────┘     ← fan-in（join barrier）
                  ▼
⑤ EvidenceReducer   归并去重 → 候选根因 + 证据打分
                  ▼
⑥ RCAJudge          只看 summary → 排序定根因
                  ▼
⑦ RemediationPlanner 处置建议（写操作必须人工确认）
                  ▼
⑧ ReportAgent        渲染最终报告 → 填 response → [END]
```

---

## 1. 专业 Agent 的隔离设计（面试核心亮点）

### 设计原则

这个设计借鉴了 Claude Code 课程 s06 的 subagent 范式：

> **专业 Agent 是"一次性、隔离上下文、只回 Evidence"的 subagent**，不是持久互聊的 teammate。

四个专业 Agent 各自做一件事：

| Agent | 数据源 | 输出 Evidence 类型 | 工具限制 |
|---|---|---|---|
| MetricAgent | Prometheus + 本机 psutil | `metric_snapshot` | 最多 4 轮工具 |
| LogAgent | RAG 知识库 | `log_excerpt` | 最多 3 轮工具 |
| InfraAgent | Docker / Network MCP | `infra_snapshot` | 最多 4 轮工具 |
| RunbookAgent | RAG 知识库（SOP 类） | `runbook_match` | 最多 3 轮工具 |

### 隔离的含义

每个专业 Agent 内部是一个独立的 LLM + 工具循环（`run_parallel_agent`），有自己的 system prompt、自己的工具集、自己的消息历史。它**只读 `state.input`**（告警原文）作为输入，**不读其他 Agent 产的 Evidence**。

输出也很节制：一条压缩后的 Evidence dict（summary ≤ 2000 字），通过 `operator.add` 写入共享的 `state.evidences`。中间推理过程（工具调用日志、LLM 思考链）不进入共享 state。

### 面试话术

> "四个专业 Agent 就像四个独立的调查员：指标组去看 CPU/内存，日志组去查告警规则，基础设施组去检查容器和网络，SOP 组去搜排查手册。他们互不干扰——各自有独立的 context window，各自调各自的工具。完成后各交一份调查报告（Evidence），由 EvidenceReducer 统一归并。这样做的好处是：第一，并行执行更快；第二，单个 Agent 的 context window 不会因为其他 Agent 的信息而膨胀；第三，某个 Agent 失败了不影响其他 Agent 的取证。"

### 为什么不让 Agent 互相聊天

> "多个 LLM 无约束互相聊天会导致三个问题：上下文爆炸（互相引用越聊越长）、幻觉传播（A 的错误结论被 B 引用后进一步发散）、不可追溯（最终结论不知道来自谁的推理）。我们的设计是'黑板模式'——Agent 只往 evidences 这块黑板上写，不读彼此的中间过程。"

---

## 2. dispatch_guard — 按需派遣

### 问题

LangGraph 的 fan-out 是图编译时固定的——四条边永远存在。但 EvidencePlan 可能只需要派 MetricAgent 和 LogAgent 两个。

### 解决方案

每个专业 Agent 外面套一层 `dispatch_guard`：执行前先检查自己是不是在 EvidencePlan 的派遣列表里。不在就直接 skip——产一条 "skipped" transition，零条 Evidence，不调 LLM。

```
fan-out 四条边全部触发
    ├── MetricAgent  → 在派遣列表 → 执行
    ├── LogAgent     → 在派遣列表 → 执行
    ├── InfraAgent   → 不在列表  → skip（0 cost）
    └── RunbookAgent → 不在列表  → skip（0 cost）
```

### 面试追问

**Q：为什么不用条件边动态选择 fan-out？**

> LangGraph 的条件边返回的是固定的节点名字符串，不支持"动态选择 fan-out 的子集"。要实现真正的动态 fan-out 得用 `Send()` API 或 map-reduce 模式，但这增加了复杂度。dispatch_guard 是更简单的方案——四条边永远在，但被 skip 的节点零成本跳过。

---

## 3. EvidencePlan — 规则路由

### 为什么不用 LLM 做取证规划

规则路由（确定性）优先于 LLM 智能路由。原因：

- 确定性路由**不飘**：同样的告警文本永远派同样的 Agent 组合
- 调试容易：关键词匹配规则可以直接 grep 日志
- 不消耗 LLM token：EvidencePlan 不调模型

### 路由规则

按关键词匹配故障域，命中多个域取并集：

```
"cpu memory disk 内存 磁盘 负载"  → MetricAgent
"log 日志 error exception 报错"   → LogAgent
"docker container 端口 dns http"  → InfraAgent + LogAgent
"sop runbook 手册 怎么处理"        → RunbookAgent
```

特殊情况：
- "全面诊断" / "深度排查" → 派出全部四个
- 什么都没命中 → 默认派 MetricAgent + LogAgent（信息密度最高的两类）

---

## 4. EvidenceReducer — 确定性归并打分

### 设计取舍

不用 LLM 做归并（避免幻觉），用确定性评分表：

| Evidence 类型 | 基础分 | 理由 |
|---|---|---|
| `metric_snapshot` | 1.00 | 现场实测数据，信息密度最高 |
| `infra_snapshot` | 0.90 | 运行环境/依赖的现场证据 |
| `log_excerpt` | 0.85 | 知识检索结果 |
| `runbook_match` | 0.75 | SOP 匹配 |
| `incident_history` | 0.60 | 历史辅证 |

**error 证据**（Agent 执行失败产生的）score 归零，不作为根因候选，但仍保留在 evidence_ids 里——RCAJudge 和 ReportAgent 能看到"哪些 Agent 失败了"，失败信号本身也是诊断信息。

输出 top-5 候选根因列表，传给 RCAJudge。

### 面试话术

> "Reducer 是纯确定性的——不调 LLM，按 Evidence 类型查评分表打分，metric 最高因为是现场实测，SOP 最低因为是知识匹配。error 证据分数归零但不丢弃，下游报告会标注'InfraAgent 执行失败，基础设施信息缺失'。"

---

## 5. RCAJudge — LLM 做根因裁决

### 为什么 RCAJudge 用 LLM

Reducer 的确定性打分只看 Evidence 类型，不看内容语义。RCAJudge 需要**理解**候选根因的含义，比如判断"Redis 内存 98%"和"Redis 连接超时"之间的因果关系。

### 关键约束

RCAJudge 有严格的输入限制：

1. **只看 summary，不读 content 原文**——content 可能包含几 KB 的原始指标/日志，直接喂 LLM 会导致 prompt 膨胀和幻觉
2. **不调工具**——只调一次 LLM 做判断
3. **解析失败降级到 candidates[0]**——Reducer 已经排好序了，LLM 失败就用 Reducer 的第一名

输出：`rca` 字段（root_cause + ranked_candidates + reasoning + confidence + supporting_evidence_ids）。同时产一条 `rca` 类型的 Evidence，让 ReportAgent 也能拿到。

---

## 6. RemediationPlanner — 确定性处置建议

### 设计原则

**不自动执行任何写操作**。建议分两类：

- **只读验证类**（无副作用）：默认推荐执行，比如"查看 Redis slow log"
- **写操作类**（重启/扩容/限流）：必须 `requires_human_confirm=True`

基于 rca.root_cause 的关键词做规则模板匹配（redis/mysql/docker/network 等），生成对应的处置步骤。

---

## 7. ReportAgent — 确定性渲染

### 为什么不用 LLM 写报告

RCAJudge 已经产出了结构化判定（root_cause + reasoning），Evidence 链也是结构化数据。Report 就是把这些结构化数据**格式化为 Markdown**。再调一次 LLM 增加成本和不确定性，无明显收益。

### 报告结构

```markdown
# Deep Diagnosis Report

## 根因判定
- 根因: Redis 内存使用率达到 98%
- 置信度: 0.85
- 关键支持证据: ev_0, ev_1, ev_3

## 候选根因 (按可能性排序)
1. metric_snapshot: Redis used_memory 达到 maxmemory 上限 (score=1.0)
2. log_excerpt: 知识库匹配到 Redis OOM 排查手册 (score=0.85)

## 证据链 (共 5 条)
- [ev_0] metric_snapshot by metric_agent — Redis memory 98%, CPU 12%
- [ev_1] log_excerpt by log_agent — 匹配到 Redis 内存优化 SOP
- [ev_2] **[ERROR]** infra_snapshot by infra_agent — Docker MCP 不可用
...

## 处置建议
⚠️ 以下处置含写操作, 需人工确认后执行
1. [只读] 检查 Redis slowlog 最近 10 条慢查询
2. [只读] 查看当前大 key (redis-cli --bigkeys)
3. [写操作·需人工] 设置 maxmemory-policy allkeys-lru
```

### evidence_id 的引用设计

Evidence 在 deep graph 里还是内存 dict，真实 DB id 要等 worker 写库时才生成。所以 ReportAgent 用 `ev_0`/`ev_1` 这种列表下标作为临时引用。将来落库时做 id 映射替换即可。

---

## 8. 并发安全：operator.add 的妙用

四个专业 Agent 并行执行，同时往 `state.evidences` 写数据。为什么不会冲突？

LangGraph 的 `Annotated[List, operator.add]` reducer 处理了并发归并：并行节点各自返回自己的 `{"evidences": [ev1]}`，LangGraph 在 fan-in 的 join barrier 处自动把所有返回值 `operator.add` 合并。这不是锁机制，是**图编排层面的数据流归并**——并行节点在内存中互不可见，只在 join 点统一合并。

---

## 9. 遇到的难点总结

### 难点 1：延迟导入避免启动时强依赖

每个专业 Agent 模块都依赖 langchain、LLM 工厂、MCP 工具链。如果在 `build_deep_graph()` 时就 import 这些模块，没装 langchain 的环境直接启动失败。

解决方案：`_resolve_specialist_node_fn()` 做延迟导入——图编译时只注册函数引用，真正的 import 推迟到节点首次执行时。这样 deep graph 可以在任何环境下编译，只在真正执行 deep 诊断时才需要完整依赖。

### 难点 2：Agent 失败的优雅降级

某个 Agent 调工具失败（比如 Docker MCP 不可用），不能让整个 deep graph 崩溃。

解决方案：每个 Agent 的 `run_xxx_agent()` 有 try-except 兜底——失败时产一条带 `error_type` 的 Evidence，走正常的 Evidence 路径。Reducer 看到 error 证据给 0 分但不丢弃，Report 会标注"InfraAgent 执行失败，基础设施信息缺失"。

### 难点 3：确定性 vs LLM 的边界选择

Deep graph 的 8 个节点里，只有两个用了 LLM：专业 Agent 的工具循环（必须用 LLM 决定调什么工具）和 RCAJudge（需要语义理解做根因判断）。其余节点全部确定性实现。

这个边界是刻意选择的——确定性节点不飘、可审计、快速。LLM 只用在**真正需要推理**的环节。

---

## 快速回顾清单

| 节点 | 确定性/LLM | 核心设计 | 面试关键词 |
|---|---|---|---|
| IncidentManager | 确定性 | DB 载入 + 手动路径降级 | 两条路径（worker/SSE） |
| CorrelationContext | 确定性 | 聚合同组告警 + Wiki 经验 | DB 失败不阻断 |
| EvidencePlan | 确定性 | 关键词规则路由 | 不飘、可 grep |
| 专业 Agent ×4 | LLM | 隔离上下文 + 黑板模式 | 不互聊、operator.add |
| dispatch_guard | 确定性 | 不在派遣列表就 skip | 零成本跳过 |
| EvidenceReducer | 确定性 | 类型评分表 + error 归零 | 不用 LLM 归并 |
| RCAJudge | LLM | 只看 summary + 降级 candidates[0] | 不读 content 原文 |
| RemediationPlanner | 确定性 | 规则模板 + 写操作人工确认 | 不自动修复 |
| ReportAgent | 确定性 | 格式化渲染 + evidence_id 引用 | 不再调 LLM |

---

*准备好了就说"开始 Step 10"，我们进入 Postgres 事实库与审计链。*
