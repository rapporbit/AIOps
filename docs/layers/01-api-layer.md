# Layer 1: API 接入层

> 目录: `app/api/`, `app/schemas/`, `app/core/rate_limiter.py`
>
> 上游: 用户浏览器 / Alertmanager → 下游: Layer 2 (Redis XADD 入队) 或 Layer 3 (同步 SSE 直连 DiagnosisRunner)

接入层是系统的唯一入口，负责请求校验、限流保护、SSE 流式推送和同步/异步诊断分流。设计目标：**快速接住请求，绝不在入口做重计算**。

## 1.1 双入口设计

系统提供两种诊断入口，适应不同场景：

| 入口 | 端点 | 特点 | 适用场景 |
|------|------|------|---------|
| **同步 SSE** | `POST /api/v1/aiops/diagnose` | 实时推送诊断过程，浏览器 EventSource 接收 | 单人即时诊断 |
| **异步队列** | `POST /api/v1/aiops/diagnose/submit` | 创建任务 → 入库 → 入队 → 返回 task_id | 批量/高并发场景 |

### 同步 SSE 流

```python
# app/api/v1/aiops.py
async def event_generator() -> AsyncIterator[dict]:
    async for sse_event in aiops_service.stream_diagnose(query, mode, session_id):
        yield {"event": "message", "data": json.dumps(sse_event, ensure_ascii=False)}

return EventSourceResponse(event_generator())
```

SSE 事件类型：`start → plan → step_complete → replan → report → complete`

同步入口内置分布式并发控制（`wait=False`），如果全局执行槽满，**直接拒绝**并建议改用异步提交，不会让用户的浏览器空等。

### 异步队列提交

```python
# app/api/v1/aiops.py
task = await incident_repo.create_task_from_manual(query, mode, severity, service)
await incident_queue.enqueue_task(task_id, group_id, ...)
return {"task_id": ..., "status": "queued", "queue_position": N}
```

API 只做三件事：参数校验 → Postgres 落库 → Redis 入队，然后立即返回。真正的诊断在 Worker 进程中异步执行。

## 1.2 Webhook 告警接入

`POST /api/v1/webhook/alertmanager` 接收 Alertmanager 标准格式告警，处理链路：

```
告警推送 → 双层限流 → 只取 firing → 归一化 → 去重落库 → 优先级入队
```

关键设计：

- **自动诊断模式选择**：critical/p0/p1 告警或 ≥10 条告警 → deep 模式，其余 → fast
- **优先级映射**：critical/page → 10, warning/p1/p2 → 50, 默认 → 100
- **去重**：同一 incident_group 的重复告警只增加 `repeat_count`，不重复创建任务
- **Webhook 只做接收**：不在入口直接跑诊断，避免告警洪峰压垮 Agent 链路

## 1.3 限流机制

采用 **固定窗口计数器 + Lua 原子脚本** 实现，代码在 `app/core/rate_limiter.py`：

```
Key 格式: rate_limit:{scope}:{identity}:{window_bucket}
窗口计算: bucket = now // window_sec
原子操作: Lua 脚本内 INCR + (首次)EXPIRE，一次 RTT 完成
```

早期用分步 INCR + EXPIRE，并发首请求各自拿到 count==1 都去 EXPIRE，窗口被反复重置。合进 Lua 后竞态消除。

| 限流维度 | 范围 | 默认值 | 窗口 |
|---------|------|--------|------|
| 手动诊断 | 单 IP | 20 次/分 | 60s |
| Webhook (源) | 单 receiver | 500 次/分 | 60s |
| Webhook (IP) | 单 IP | 50 次/秒 | 1s |

设计要点：

- **Fail-Open**：Redis 不可用时放行请求、记录 warning，不因限流组件故障拒绝所有请求
- **429 + Retry-After**：超限返回标准 HTTP 429，附带 `Retry-After` 头
- **IP 提取优先级**：X-Forwarded-For → request.client.host → "unknown"

## 1.4 请求上下文与中间件

三层中间件链（按执行顺序）：

| 中间件 | 职责 |
|--------|------|
| `RequestIDMiddleware` | 生成或复用 `X-Request-ID`，注入 contextvars 供 logger 使用 |
| `LoggingMiddleware` | 记录 method/path/status/elapsed_ms，自动跳过静态文件和健康检查 |
| `CORSMiddleware` | 开发环境允许所有来源 |

## 1.5 健康检查（K8s 探针）

| 探针 | 端点 | 判断逻辑 |
|------|------|---------|
| Liveness | `GET /health` | 进程存活即 200 |
| Readiness | `GET /health/ready` | Postgres 必须可用（含 pgvector 知识库, 探测 `vector_store`/`kb_chunks`）；Redis 在开启 incident pipeline 时必须可用；MCP 可选 |

Readiness 返回每个依赖的状态详情（`ok / down / not_connected`），方便排障。

## 1.6 统一响应格式

所有 API 响应包裹在 `ApiResponse[T]` 中：

```json
{
  "code": "SUCCESS",
  "message": "ok",
  "data": { ... },
  "request_id": "req-xxx"
}
```

异常处理三级兜底：
- `AppException` → 业务错误码 + 可读消息
- `RequestValidationError` → 422 + Pydantic 字段级校验详情
- `Exception` → 500，仅 DEBUG 模式返回堆栈

## 1.7 完整端点清单

| 模块 | 方法 | 路径 | 说明 |
|------|------|------|------|
| 诊断 | POST | `/aiops/diagnose` | 同步 SSE 诊断 |
| 诊断 | POST | `/aiops/diagnose/submit` | 异步队列提交 |
| 对话 | POST | `/chat/stream` | RAG 流式对话 |
| 对话 | GET | `/chat/sessions/{id}/history` | 会话历史 |
| 文档 | POST | `/documents/upload` | 上传知识库文档 |
| 文档 | GET/DELETE | `/documents`, `/documents/{source}` | 文档管理 |
| 事件 | GET | `/incidents/tasks` | 任务列表 |
| 事件 | GET | `/incidents/tasks/{id}` | 任务详情 + 队列位置 |
| 事件 | GET | `/incidents/tasks/{id}/evidence` | 证据链 |
| 事件 | GET | `/incidents/tasks/{id}/agent-runs` | Agent 执行记录 |
| 事件 | GET | `/incidents/tasks/{id}/tool-calls` | 工具调用记录 |
| 队列 | GET | `/queue/status` | 队列深度 + Worker 状态 + 执行槽占用 |
| Webhook | POST | `/webhook/alertmanager` | Alertmanager 告警接入 |
| 审批 | GET/POST | `/approvals/*` | 审批流程 |
| Skill | GET | `/skills` | Skill 注册表 |
| Wiki | GET | `/wiki/overview`, `/wiki/pages/*` | 经验库 |
| 评测 | GET | `/eval/reports/*` | 评测报告 |
| 健康 | GET | `/health`, `/health/ready` | K8s 探针 |

## 模拟面试问答

### 🔥 热点拷问

**面试官：你同时搞了同步 SSE 和异步队列两种入口，这不是过度设计吗？直接全部走队列不就完了？**

不行，体验差异太大。同步 SSE 下用户能实时看到 Agent 在想什么、调了什么工具、得到了什么结果，整个思考过程透明可见。如果全走队列，用户提交后只能看到一个 task_id，然后轮询等结果——对于"我电脑卡了帮我看看"这种即时场景，等 30 秒看到思考过程和等 30 秒看到最终报告，用户感受完全不同。另外同步 SSE 不依赖 Redis 队列，是 Redis 故障时的降级通路。

**追问：那 SSE 长连接多了不会拖垮 API 进程吗？**

会，所以同步入口有独立的并发控制。执行槽满时直接拒绝（`wait=False`），返回"请改用异步提交"的提示，不会让用户浏览器空等。默认 `manual_diagnosis_concurrency=2`，就是说同时最多 2 个 SSE 诊断。批量场景必须走队列，SSE 只服务少量即时需求。

---

**面试官：你的限流用的固定窗口，不觉得粗糙吗？窗口边界突刺怎么办？**

知道这个问题。固定窗口在窗口交界处确实可能放行 2 倍的请求。滑动窗口或令牌桶更精确，但实现复杂度也更高。当前场景下这个突刺可以接受——限流的目的是防刷爆而不是精确控速，突刺最多就是多放行一个窗口的请求量，后面有队列和执行槽兜底。计数和设过期已经合进 Lua 脚本保证原子性（早期分步 INCR + EXPIRE 踩过竞态坑），如果上生产发现突刺是实际问题，可以在 Lua 内升级到滑动窗口 + ZSET 方案，接口不变。

### 常规问题

**面试官：Webhook 告警去重怎么做的？同一个告警反复推怎么办？**

用 `correlation_key` 做告警分组，同一 key 的告警聚合到同一个 incident group。同一 group 如果已经有 pending/running 状态的诊断任务，重复推送只增加 `repeat_count`，不重复创建任务。Partial Unique Index `WHERE status IN ('pending', 'running')` 保证活跃任务唯一。

**面试官：Readiness 和 Liveness 为什么分开？**

Liveness 只看进程是否存活。Readiness 检查核心依赖（Postgres 必须——向量/BM25 知识库都在其中、Redis 按配置）。Redis 临时不可用时，Liveness 仍然 200（进程没挂），Readiness 返回 503。K8s 看到 503 只停止发送新请求，不会杀进程重启——杀进程反而加重故障。

### 反思与改进

**面试官：API 设计这块如果重来，你会改什么？**

一个。SSE 诊断加断点续传——当前连接断了诊断就丢了，应该在 Redis 里缓存中间状态，客户端重连后从上次断点继续接收。限流器之前用分步 INCR + EXPIRE 踩过竞态坑（并发首请求反复重置窗口），已用 Lua 原子脚本修复。固定窗口突刺问题目前可接受，后续如需更平滑可在 Lua 内升级到滑动窗口。
