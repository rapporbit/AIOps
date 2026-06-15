# 分层架构文档

Multi-Agent AIOps Platform 采用七层分层架构，每一层只做一件事，层间通过明确的接口解耦。

```
请求方向 ↓                      数据方向 ↑
─────────────────────────────────────────────
Layer 1  API 接入层         FastAPI / SSE / Webhook / 限流
Layer 2  队列与缓冲层       Redis Streams / 优先级 / DLQ
Layer 3  编排与调度层       Worker / 分布式执行槽 / 审计
Layer 4  智能体层           LangGraph / Skill Router / fast+deep
Layer 5  RAG 知识层         Parent-Child / BM25+Vector / Rerank
Layer 6  工具与 MCP 层      MCP Servers / 白名单 / 权限三态
Layer 7  存储与审计层       Postgres / Evidence / Wiki
─────────────────────────────────────────────
```

## 文档索引

| 文档 | 内容 |
|------|------|
| [00-concurrency-design.md](00-concurrency-design.md) | 跨层并发治理总览：压力来源拆解 + 六层治理体系 |
| [01-api-layer.md](01-api-layer.md) | 双入口、Webhook、限流、健康检查、统一响应 |
| [02-queue-layer.md](02-queue-layer.md) | 优先级队列、两阶段消费、Pending 恢复、DLQ |
| [03-orchestration-layer.md](03-orchestration-layer.md) | Worker 生命周期、分布式执行槽、审计链路 |
| [04-agent-layer.md](04-agent-layer.md) | 双模式 LangGraph 图、Executor、Replanner |
| [04a-skill-system.md](04a-skill-system.md) | Skill Router 三级路由、Planner、Skill 注册表 |
| [04b-agent-harness.md](04b-agent-harness.md) | AgentHarness 统一管控、状态管理、Transition History |
| [05-rag-layer.md](05-rag-layer.md) | Parent-Child 分块、混合检索、Rerank、评测 |
| [06-tool-layer.md](06-tool-layer.md) | MCP 接入、三层过滤、权限三态、审批流程 |
| [07-storage-layer.md](07-storage-layer.md) | Postgres 8 表、任务状态机、证据链、LLM Wiki |
| [08-failure-matrix.md](08-failure-matrix.md) | 全链路故障矩阵："如果 X 挂了会怎样" |
| [interview-questions.md](interview-questions.md) | 模拟面试题库：61 题 + 25 追问，纯问题索引 |

## 阅读建议

- **快速了解全貌**：先读 [architecture.md](../architecture.md) 的全链路图，再按需跳到具体层
- **理解并发设计**：从 [00-concurrency-design.md](00-concurrency-design.md) 开始，它串联了 Layer 1-3 的协作关系
- **理解诊断流程**：按 04 → 04a → 04b 顺序阅读智能体层
