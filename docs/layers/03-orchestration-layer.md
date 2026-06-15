# Layer 3: 编排与调度层

> 目录: `app/orchestration/`, `app/diagnosis_worker.py`, `app/core/distributed_limiter.py`
>
> 上游: Layer 2 (Worker 从队列消费任务) / Layer 1 (同步 SSE 直连) → 下游: Layer 4 (DiagnosisRunner 调用 LangGraph 诊断图)

编排层解决三个问题：**谁来跑诊断（Worker 生命周期）、同时跑几个（分布式执行槽）、过程怎么留痕（审计链路）**。

## 3.1 Worker 生命周期

```
启动序列:
    connect_postgres() → connect_redis_queue() → connect_mcp(fail_silently)
    → spawn heartbeat_loop()
    → 进入消费主循环

消费主循环 (while not stopping):
    1. claim_stale_tasks()        ← 先回收崩溃 Worker 的遗留任务
    2. read_tasks(block=5000ms)   ← 两阶段优先级消费
    3. handle_message(msg)        ← 执行诊断

关闭序列:
    set stopping → cancel heartbeat → close MCP → close queue → close Postgres
```

### 消息处理流程

```python
async def handle_message(msg):
    # 1. 幂等检查: 已 succeeded 的任务直接 ACK 跳过
    if task.status == "succeeded":
        await queue.ack(msg); return

    # 2. 重试上限检查: 耗尽直接进 DLQ
    if task.attempts >= max_attempts:
        await queue.dead_letter(msg, reason="max_attempts"); return

    # 3. 标记 running, 递增 attempts
    await repo.mark_task_running(task_id)

    # 4. 获取全局执行槽 (wait=True, 排队等待)
    async with distributed_slot("worker_diagnosis", limit=2, wait=True):
        # 5. 超时保护 (600s)
        result = await asyncio.wait_for(run_diagnosis(...), timeout=600)

    # 6. 成功: 更新状态 + ACK
    await repo.mark_task_succeeded(task_id, report=result.report)
    await queue.ack(msg)
```

### 失败处理

```
执行失败
    ├─ attempts < max_attempts → 标记 pending → 重新入队 → ACK 原始消息
    └─ attempts >= max_attempts → 标记 failed → 进 DLQ → ACK 原始消息
```

**关键细节**：重新入队必须在 ACK 之前完成。如果先 ACK 再入队，中间 Worker 崩溃会导致任务丢失（状态 pending 但队列里没有）。

## 3.2 分布式执行槽

### 问题

`asyncio.Semaphore` 只在单进程内有效。Uvicorn `--workers 4` 启动 4 个进程，每个进程各有一个 Semaphore(2)，实际并发变成 8 而不是 2。

### 方案：Redis ZSET + Lua 原子操作

```
ZSET Key: aiops:limiter:worker_diagnosis
Members: {hostname}:{pid}:{uuid}
Score: 过期时间戳 (now_ms + ttl_ms)
```

#### 获取槽位 (Lua 原子脚本)

```lua
-- 1. 清理过期 token
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', now_ms)

-- 2. 计数
local count = redis.call('ZCARD', KEYS[1])

-- 3. 判断 + 占位
if count < limit then
    redis.call('ZADD', KEYS[1], now_ms + ttl_ms, token)
    redis.call('PEXPIRE', KEYS[1], ttl_ms * 2)
    return 1  -- 成功
end
return 0      -- 已满
```

#### 心跳续约 (Lua)

长任务期间定期刷新 Score，防止被清理：

```lua
if redis.call('ZSCORE', KEYS[1], token) then
    redis.call('ZADD', KEYS[1], new_expire_ms, token)
    return 1
end
return 0  -- token 已不存在
```

#### Pause / Resume (审批场景)

```
高风险工具触发审批:
    handle.pause()   → 停止心跳 + 释放槽位 (不占着坑等人审批)
    等待人工决策...
    handle.resume()  → 重新获取槽位 + 恢复心跳
```

这避免了审批等待期间（可能几分钟到几小时）一直占着执行槽。

### 两种获取模式

| 模式 | 场景 | 行为 |
|------|------|------|
| `wait=False` | 同步 SSE 诊断 | 槽满直接拒绝，返回提示"请改用排队" |
| `wait=True` | Worker 消费 | 每 0.5s 轮询等待，有序排队 |

### Fail-Open

Redis 不可用时返回特殊 token `"__fail_open__"`，允许诊断继续执行。设计哲学：**限流组件故障不应导致所有诊断停摆**。

## 3.3 审计链路

`app/orchestration/audit.py` 中的 `run_legacy_langgraph_with_audit()` 在诊断执行过程中持久化每一步：

```
诊断开始
  │
  ├─ 创建 AgentRun 记录 (agent_name, task_id, incident_group_id)
  │
  ├─ 流式消费诊断事件:
  │   ├─ tool_call  → 创建 ToolCall 记录 + Evidence
  │   ├─ step_complete → 创建 Evidence (step 内容)
  │   ├─ report     → 创建 Evidence (最终报告)
  │   └─ usage      → 累计 token 统计
  │
  └─ 完成 AgentRun (status, evidence_ids, tool_call_count, tokens)
```

审计的意义：诊断过程不只停留在最终 Markdown 报告，每一步的工具调用、中间结果和 token 消耗都可以回看。

### 重试幂等

对于重试任务，审计层复用已有的 AgentRun，不创建新记录：

```python
if task.agent_run_id:
    existing_run = await repo.get_agent_run(task.agent_run_id)
    evidence_ids = existing_run.evidence_ids or []
else:
    new_run = await repo.create_agent_run(...)
```

## 3.4 模式选择与降级

`DiagnosisRunner` 负责选择诊断模式和构建 LangGraph 图：

```python
def resolve_effective_mode(requested_mode):
    if requested_mode == "deep" and settings.deep_diagnosis_enabled:
        return ("deep", "deep", False)
    else:
        return (requested_mode, "fast", group_agent_reserved=True)
```

deep 模式可配置关闭，此时即使请求 deep 也降级到 fast，并在 `group_agent_reserved` 标记中记录降级事实。

## 3.5 关键配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `worker_diagnosis_concurrency` | `2` | 全局 Worker 执行槽上限 |
| `manual_diagnosis_concurrency` | `2` | 同步 SSE 诊断并发上限 |
| `limiter_default_ttl_sec` | `90` | 槽位自动过期时间 |
| `limiter_default_refresh_sec` | `30` | 心跳续约间隔 |
| `diagnosis_task_timeout_sec` | `600` | 单次诊断超时 (10 分钟) |
| `diagnosis_task_max_attempts` | `3` | 最大重试次数 |

## 模拟面试问答

### 🔥 热点拷问

**面试官：分布式执行槽为什么不用 asyncio.Semaphore 或 Redlock？**

语义不同。Semaphore 是进程内的——Uvicorn `--workers 4` 启动 4 个进程，每个进程各有一个 Semaphore(2)，实际并发变成 8。Redlock 是分布式互斥锁，语义是"独占一个资源"而不是"限制并发数"，用它实现计数型限流需要持有 N 把锁代表 N 个槽位，acquire/release 管理复杂。ZSET 方案天然支持计数——ZCARD 就是当前占用数，Score 存过期时间戳自动支持 TTL 清理，一个 Lua 脚本原子完成"清理过期 + 计数 + 占位"。

**追问：你说 Fail-Open，Redis 挂了 5 分钟，所有请求都不限流，LLM 不会被打爆吗？**

确实是一个权衡。Fail-Open 的前提假设是 Redis 故障是短暂的，这期间多跑几个诊断的代价（多花 LLM token）远小于完全拒绝服务。如果 Redis 长时间不可用，Readiness 探针会返回 503，K8s 停止向这个 Pod 发送新请求，从集群层面做了兜底。另外 Uvicorn 的 worker 数本身就是并发的物理上限。

---

**面试官：重新入队为什么必须在 ACK 之前完成？能不能先 ACK 再入队？**

不能。先 ACK 再入队的问题：ACK 之后原始消息从 PEL 移除，如果这时 Worker 崩溃，新消息还没入队，任务就丢了——数据库里状态是 pending 但队列里没有消息。先入队后 ACK 保证：即使中间崩溃，原始消息仍在 PEL 中，会被 XAUTOCLAIM 回收。最坏情况是任务被执行两次，但幂等检查（已 succeeded 的任务直接 ACK 跳过）可以防止重复执行。

### 常规问题

**面试官：Pause/Resume 这个机制在审批场景下的实际效果如何？**

审批等待可能几分钟甚至几小时。如果一直占着执行槽等审批，其他诊断就无法执行。Pause 释放槽位但保留诊断上下文，审批完成后 Resume 重新获取槽位继续执行。实际效果是审批等待期间不浪费并发名额，系统吞吐不受审批等待时间的影响。

### 反思与改进

**面试官：分布式执行槽有没有可能是过度设计？单机场景 asyncio.Lock 就够了吧？**

如果确定只跑一个进程，asyncio.Lock 确实够。分布式执行槽的设计初衷是 docker compose 启动 3 个 Worker 进程，它们需要跨进程协调。如果改为单进程多协程部署，Redis ZSET 方案确实过重。但从面试角度，"跨进程并发控制"的技术深度远高于"进程内 Semaphore"，设计选择本身就是一个有价值的讨论点。
