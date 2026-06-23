# 全链路故障矩阵

> 面试高频问题："如果 X 挂了会怎样？"
>
> 本文按组件列出故障场景、系统表现和恢复方式。每个场景对应一个可以在面试中直接展开的回答。

## Redis 故障

| 场景 | 影响范围 | 系统表现 | 恢复方式 |
|------|---------|---------|---------|
| Redis 完全不可用 | 队列、限流、执行槽、心跳 | 限流器 Fail-Open 放行；执行槽返回 `__fail_open__` 允许执行；队列入队/消费失败，异步诊断不可用；同步 SSE 诊断仍可工作 | Redis 恢复后自动重连，Worker 重新注册心跳；积压任务需人工检查 |
| Redis 短暂抖动 (< 5s) | 队列消费、心跳 | Worker XREADGROUP BLOCK 5s 超时后自动重试；心跳 TTL 30s，短暂断连不会被判死亡 | 自动恢复，无需干预 |
| Redis 内存满 | 队列写入 | XADD 失败，新任务无法入队；API 返回 500；已在队列中的任务不受影响 | 清理过期数据或扩容；`maxlen=10000` 限制了单流最大长度 |

### 设计要点

Redis 故障时系统的核心决策是 **Fail-Open**：宁可临时多跑几个诊断（多花 LLM token），也不完全拒绝服务。同步 SSE 诊断不依赖 Redis 队列，是 Redis 故障时的降级通路。

## Postgres 故障

| 场景 | 影响范围 | 系统表现 | 恢复方式 |
|------|---------|---------|---------|
| Postgres 完全不可用 | 任务落库、审计、证据链、Wiki | 异步诊断提交失败（无法创建 task）；同步 SSE 诊断仍可工作但无审计记录；Readiness 返回 503 | Postgres 恢复后自动重连；诊断结果可从 LLM 输出中恢复，但审计链断裂 |
| Postgres 连接池耗尽 | 同上 | 新连接获取超时；高并发写入场景下可能出现 | 调整 pool_size；任务状态分阶段写入减少长事务 |
| DDL 并发冲突 | 多 Worker 启动 | 多个 Worker 同时 CREATE TABLE | Advisory Lock (`pg_advisory_lock(8207440167)`) 序列化 DDL |

### 设计要点

Postgres 是事实库，不是执行依赖。它挂了诊断能力不会完全丧失，但会丢失过程审计。`incident_pipeline_enabled=False` 可以彻底关闭事实库依赖，退化为纯同步诊断。

## 向量库 / 词法检索故障 (pgvector + pg_search, 在 Postgres 内)

向量库已从独立的 Milvus 迁到 Postgres 内的 **pgvector**（向量）+ **ParadeDB pg_search**（BM25），不再是独立服务。故障模式相应变化，且检索内部分层降级：

| 场景 | 影响范围 | 系统表现 | 恢复方式 |
|------|---------|---------|---------|
| Postgres 不可用 | RAG 检索 + 事故台账 | 向量/BM25 检索一并不可用（见「Postgres 故障」）；`search_knowledge_base` 退化为纯 LLM 推理；Readiness 返回 503 | Postgres 恢复后连接池自动重连 |
| pg_search 缺失 / 未 preload | BM25 词法检索 | `bm25_search` 返回 `[]`，混合检索**自动降级为纯向量**；不报错、不阻断 | 用 `paradedb/paradedb` 镜像并把 `pg_search` 加入 `shared_preload_libraries` |
| 向量检索失败（维度不符 / 索引缺失） | RAG 检索 | `safe_similarity_search` 返回 `[]`，`search_knowledge_base` 退化为纯 LLM 推理 | 检查 `kb_chunks` 维度 / HNSW 索引，必要时重建并重灌 |
| 向量检索延迟高 | 检索性能 | 单次诊断总耗时增加；全局执行槽间接限制并发检索压力 | 查 Postgres 资源；`hnsw.ef_search` 可调 |

### 设计要点

向量/BM25 检索都在 Postgres 内，所以"向量库可用性"等同于"Postgres 可用性"——Postgres 是 Readiness 的必要依赖。但检索内部是分层降级的：BM25(pg_search) 失败 → 退回纯向量；向量失败 → 退回纯 LLM 推理。任一环节失败都不抛异常、不阻断诊断图。

## LLM API 故障

| 场景 | 影响范围 | 系统表现 | 恢复方式 |
|------|---------|---------|---------|
| LLM API 超时/限流 | 所有诊断 | Skill Router: 降级到规则路由 + `generic_oncall`；Executor: 单步失败，Replanner 决定重试或收敛；Report: 生成失败，任务标记 failed | 检查 RPM/TPM 配额；全局执行槽已限制并发 LLM 调用 |
| LLM API 返回格式异常 | 结构化输出 | `with_structured_output` 解析失败；Replanner 判为执行失败，按失败路径处理 | AgentHarness 的预算控制 + 步骤上限防止无限重试 |
| LLM API Key 失效 | 所有诊断 | 所有 LLM 调用返回 401；Skill Router 降级到规则层但无法分类，走 `generic_oncall`；Executor 无法执行 | 更换 API Key，无需重启（环境变量重载） |

### 设计要点

LLM 是核心能力依赖，完全不可用时系统无法提供有价值的诊断。但 AgentHarness 的**模型分层**允许不同阶段使用不同 LLM Provider，单个 Provider 故障不一定影响全部阶段。

## MCP 工具服务故障

| 场景 | 影响范围 | 系统表现 | 恢复方式 |
|------|---------|---------|---------|
| 单个 MCP Server 不可用 | 该 Server 的工具 | `fail_silently=True`：APP 正常启动，只缺少该 Server 的工具；Agent 使用剩余可用工具继续诊断 | 重启 MCP Server，主 APP 需重启以重新加载工具 |
| 所有 MCP Server 不可用 | 外部工具 | 只剩本地工具（`search_knowledge_base`, `get_current_time`）；Agent 退化为纯 RAG 知识库诊断 | 检查 Docker Compose 和端口占用 |
| MCP 工具执行超时 | 单次诊断 | 工具返回超时错误；Executor 记录失败，Replanner 决定下一步；deep 模式中单个 Agent 失败变成带 `error_type` 的 Evidence | 工具超时上限由各 MCP Server 自行控制 |

### 设计要点

MCP 的进程隔离是核心价值：MCP Server 崩溃不影响主 API 进程。工具失败在 deep 模式中不会拖垮整条 graph，而是变成"信息缺失"的证据，报告会说明哪些数据没能采集到。

## Worker 故障

| 场景 | 影响范围 | 系统表现 | 恢复方式 |
|------|---------|---------|---------|
| Worker 进程崩溃 | 该 Worker 正在处理的任务 | 心跳停止 → 30s TTL 过期 → `/queue/status` 显示 Worker 死亡；已领取的消息留在 PEL | 其他 Worker 通过 XAUTOCLAIM (15min) 回收 stale 任务；或新 Worker 启动后自动回收 |
| Worker 在写 Evidence 中途崩溃 | 审计完整性 | AgentRun 状态停在 running；已写入的 Evidence 保留，未写入的丢失 | 重试任务时，审计层复用已有 AgentRun，不创建新记录；已有 Evidence 不会重复 |
| 所有 Worker 死亡 | 异步诊断 | 队列中的任务持续积压（depth 上升）；同步 SSE 诊断不受影响 | 重启 Worker；积压任务按优先级自动消费 |

### 设计要点

Worker 崩溃是最常见的故障场景。`attempts` 计数 + `max_attempts` 上限保证任务不会无限重试；超过重试上限进入 DLQ，需人工排查。**重新入队在 ACK 之前完成**是关键细节——中间崩溃时任务仍在 PEL 中，不会丢失。

## Reranker 故障

| 场景 | 影响范围 | 系统表现 | 恢复方式 |
|------|---------|---------|---------|
| 本地 Reranker 模型加载失败 | 精排 | 降级：直接返回 RRF 融合后的 top-k 结果，跳过精排 | 检查模型文件和 GPU/MPS/CPU 后端 |
| DashScope Rerank API 不可用 | 精排 (API 模式) | 同上，降级到无精排 | 切换到本地 Reranker 或等 API 恢复 |

### 设计要点

Reranker 是可选增强，不是必要依赖。降级策略：**永不抛异常，永远返回有效结果**。

## 组合故障场景

| 场景 | 系统表现 | 最小可用能力 |
|------|---------|------------|
| Redis + Postgres 同时挂 | 只有同步 SSE 诊断可用，无审计、无队列 | 单人即时诊断（退化到基础版能力） |
| LLM + Postgres 同时挂 | 诊断完全不可用（向量/BM25 知识库随 Postgres 一并不可用） | 健康检查仍可响应，前端可展示历史任务 |
| 单个 MCP + Reranker 同时挂 | 诊断可用但能力降级 | RAG 无精排 + 少部分工具缺失，报告会标注信息缺失 |

## 模拟面试问答

### 🔥 热点拷问

**面试官：你列了这么多故障场景，有没有真实遇到过？处理过吗？**

开发过程中遇到过几个。Redis 连接抖动——Docker 重启 Redis 时 Worker 的 XREADGROUP 报错，验证了重试逻辑和 Fail-Open 策略确实有效。MCP Server 启动顺序问题——主 APP 启动时某个 MCP Server 还没 ready，`fail_silently=True` 让 APP 正常启动但少了部分工具（这是一个可改进点，可以做热加载）。Worker 崩溃回收——kill -9 一个 Worker 后观察到 XAUTOCLAIM 在 15 分钟后成功回收了 stale 任务。但大规模组合故障（Redis + Postgres 同时挂）没有在真实环境中测过，只是代码层面的降级路径分析。

**追问：如果让你给这个系统做一次混沌工程测试，你会怎么设计？**

三个层次。一是组件故障注入：用 `docker pause` / `docker stop` 模拟 Redis、Postgres（含向量/BM25 知识库）、MCP Server 的单点故障和恢复，验证降级和自动恢复是否按预期工作。二是网络故障：用 `tc netem` 给 MCP Server 加延迟和丢包，看 Agent 的重试和超时处理。三是资源压力：限制 Worker 容器的 CPU 和内存，看 OOM 时任务是否正确进入 DLQ。每个测试场景需要预定义"通过标准"——比如 Redis 停 30 秒后恢复，队列中的任务应在 5 分钟内被正常消费，不丢失。

---

**面试官：这个系统目前最大的技术短板是什么？如果给你一个月时间，你优先补什么？**

三个短板。第一是评测覆盖不全——Skill Router 已有 16 题分类评测（full vs index A/B 对比均 16/16），但还需扩展到 100+ 题覆盖模糊场景；fast vs deep 没有对比评测、MCP 工具调用没有期望序列评测。第二是知识库能力弱——只支持 Markdown、没有多源同步、没有增量更新，离生产级差距大。第三是 deep 模式验证不足——设计了多 Agent 证据图但缺乏复杂 RCA 场景的端到端验证。如果一个月，优先扩展评测——扩大 Router 评测集、建诊断场景 benchmark，覆盖工具调用和根因命中率，让系统能力可量化。

### 反思与改进

**面试官：如果重来整个系统，架构层面你会怎么改？**

保留的：七层分层、Skill-first 路由、队列 + Worker、MCP 隔离、Postgres 事实库。改变的：一是 RAG 从一开始就做增量同步和多源接入。二是 deep 模式做成可配置的 Agent 池——按 EvidencePlan 动态组合，而不是 4 个固定 Agent。三是加独立的评测服务——每次代码变更自动跑评测，CI/CD 集成质量门禁。四是 Wiki 经验沉淀从 LLM 合并改为结构化追加——不信任 LLM 做内容合并，只让它做摘要提取。

**面试官：开发过程中最有成就感的一件事？**

压测验证那一刻。设计了队列、Worker、执行槽、心跳、DLQ 一整套东西，都是单元级验证。第一次跑完完整并发压测——8 个任务同时提交、3 个 Worker 竞争消费、执行槽稳定 2/2、全部成功、队列清零——看到整套设计在真实并发下按预期工作，那种感觉是写代码最大的乐趣。
