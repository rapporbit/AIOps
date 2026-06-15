# 系统架构总览

Multi-Agent AIOps Platform 采用七层分层架构，从用户请求到最终诊断报告，每一层只做一件事，层间通过明确的接口解耦。

```
┌─────────────────────────────────────────────────────────────────────┐
│  Layer 7 │ 存储与审计层     Postgres · Evidence · Wiki · AgentRun  │
├─────────────────────────────────────────────────────────────────────┤
│  Layer 6 │ 工具与 MCP 层    MCP Servers · 工具白名单 · 权限三态    │
├─────────────────────────────────────────────────────────────────────┤
│  Layer 5 │ RAG 知识层       Parent-Child · BM25+Vector · Rerank    │
├─────────────────────────────────────────────────────────────────────┤
│  Layer 4 │ 智能体层         LangGraph · Skill Router · 双模式诊断  │
├─────────────────────────────────────────────────────────────────────┤
│  Layer 3 │ 编排与调度层     DiagnosisRunner · 分布式执行槽 · 审计  │
├─────────────────────────────────────────────────────────────────────┤
│  Layer 2 │ 队列与缓冲层     Redis Streams · 优先级 · DLQ · 恢复    │
├─────────────────────────────────────────────────────────────────────┤
│  Layer 1 │ API 接入层       FastAPI · SSE · Webhook · 限流 · 健康  │
└─────────────────────────────────────────────────────────────────────┘
```

## 各层职责

| 层级 | 目录 | 核心职责 | 关键技术 |
|:---:|------|---------|---------|
| 1 | `app/api/`, `app/schemas/` | 请求接入、参数校验、限流、SSE 流式推送 | FastAPI, SSE, Fixed-window 限流 |
| 2 | `app/queue/` | 任务排队、优先级调度、故障恢复 | Redis Streams, XREADGROUP, DLQ |
| 3 | `app/orchestration/`, `app/diagnosis_worker.py` | 诊断编排、全局并发控制、证据审计 | Redis ZSET + Lua 原子槽、Worker 心跳 |
| 4 | `app/agents/`, `app/diagnosis_graphs/`, `app/skills/`, `app/runtime/` | 智能体编排、Skill 路由、双模式诊断图 | LangGraph, Plan-Execute-Replan, Fan-out/Fan-in |
| 5 | `app/rag/`, `app/core/hybrid_retriever.py`, `app/core/reranker.py` | 知识检索、混合召回、精排 | Parent-Child Chunking, BM25+Vector RRF, bge-reranker |
| 6 | `app/tools/`, `mcp_servers/`, `app/runtime/tool_filter.py` | 工具发现与加载、白名单过滤、权限管控 | MCP 协议, 三层过滤, allow/ask/deny |
| 7 | `app/db/`, `app/incidents/`, `app/evidence/`, `app/wiki/` | 事实持久化、证据链、经验沉淀 | Postgres 8 表, JSONB, LLM Wiki |

## 请求全链路

```
用户/告警
  │
  ▼
┌─ Layer 1: API 接入 ──────────────────────────────────────────────┐
│  POST /diagnose (SSE)         │  POST /diagnose/submit (队列)    │
│  POST /webhook/alertmanager   │  POST /chat/stream (RAG 对话)    │
│  限流 → 校验 → 分流           │  限流 → 校验 → 落库 → 入队       │
└──────────┬────────────────────┴──────────────┬───────────────────┘
           │ (同步 SSE)                         │ (异步)
           │                                    ▼
           │                    ┌─ Layer 2: 队列缓冲 ──────────────┐
           │                    │  Redis Streams 四级优先级          │
           │                    │  critical → high → normal → low   │
           │                    └──────────────┬───────────────────┘
           │                                    │ Worker 消费
           ▼                                    ▼
┌─ Layer 3: 编排调度 ──────────────────────────────────────────────┐
│  DiagnosisRunner 选择模式 (fast/deep)                             │
│  distributed_slot 全局执行槽 (Redis ZSET + Lua)                   │
│  AuditTrail 持久化每一步证据                                       │
└──────────────────────────────┬───────────────────────────────────┘
                               │
┌─ Layer 4: 智能体 ────────────┴───────────────────────────────────┐
│  fast: SkillRouter → Planner → Executor → Replanner → Report     │
│  deep: IncidentMgr → EvidencePlan → 4 Agent 并行 → Reducer → RCA │
└──────────┬───────────────────────────────┬───────────────────────┘
           │ 检索知识                       │ 调用工具
           ▼                               ▼
┌─ Layer 5: RAG ───────────┐  ┌─ Layer 6: MCP 工具 ───────────────┐
│  Parent-Child Chunking    │  │  system / network / docker / web   │
│  Vector + BM25 → RRF     │  │  Skill 白名单 → 权限三态 → 执行    │
│  bge-reranker 精排        │  │  并行分组 → 结果截断               │
└──────────┬───────────────┘  └──────────────┬────────────────────┘
           │                                  │
           ▼                                  ▼
┌─ Layer 7: 存储与审计 ───────────────────────────────────────────┐
│  Postgres: alerts / incidents / tasks / evidence / tool_calls    │
│  AgentRun + ToolCall 全链路审计                                   │
│  LLM Wiki 经验沉淀 (Markdown, 关键词召回)                         │
└─────────────────────────────────────────────────────────────────┘
```

## 技术栈全景

| 领域 | 技术选型 |
|------|---------|
| API 框架 | FastAPI 0.115+ / Uvicorn / sse-starlette |
| Agent 编排 | LangGraph 1.0+ / LangChain Core |
| LLM | DeepSeek / DashScope (Qwen), OpenAI-compatible |
| Embedding | BAAI/bge-m3 (1024 维, Ollama 或 DashScope) |
| Rerank | BAAI/bge-reranker-v2-m3 (本地) / DashScope gte-rerank-v2 |
| 向量库 | Milvus 2.4 (HNSW, COSINE) |
| 关系库 | PostgreSQL (asyncpg, SQLAlchemy) |
| 队列 | Redis 7 Streams |
| 工具协议 | MCP (Model Context Protocol) |
| 容器化 | Docker Compose (Milvus + Redis + Postgres + Attu) |

## 分层文档索引

- [分层文档导读](layers/README.md)
- [跨层并发治理总览](layers/00-concurrency-design.md)
- [Layer 1: API 接入层](layers/01-api-layer.md)
- [Layer 2: 队列与缓冲层](layers/02-queue-layer.md)
- [Layer 3: 编排与调度层](layers/03-orchestration-layer.md)
- [Layer 4: 智能体层](layers/04-agent-layer.md) — 双模式图 / Executor / Replanner
- [Layer 4a: Skill 系统](layers/04a-skill-system.md) — Skill Router / Planner / 注册表
- [Layer 4b: AgentHarness](layers/04b-agent-harness.md) — 统一管控 / 状态管理 / Transition History
- [Layer 5: RAG 知识层](layers/05-rag-layer.md)
- [Layer 6: 工具与 MCP 层](layers/06-tool-layer.md)
- [Layer 7: 存储与审计层](layers/07-storage-layer.md)
- [全链路故障矩阵](layers/08-failure-matrix.md)
- [模拟面试题库](layers/interview-questions.md)
- [开发优化故事](development-stories.md)
