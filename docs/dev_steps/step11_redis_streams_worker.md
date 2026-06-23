# Step 11：Redis Streams 任务队列 & Worker

---

## 这一步要解决的问题

基础版 AIOps 是"用户点诊断 → API 进程当场跑 Agent → 返回结果"。问题很多：

- **阻塞 API**：一次诊断 30 秒 - 2 分钟，API 进程被长时间占住
- **无法排队**：同时来 10 个告警，全部并发跑 Agent，LLM API 配额瞬间打爆
- **崩了就没了**：进程挂了，正在跑的诊断直接丢失
- **无法水平扩展**：想加机器也没有分发机制

V3 引入 Redis Streams 作为任务队列，Worker 独立进程消费，实现**异步解耦、排队调度、崩溃恢复、水平扩展**。

---

## 整体架构

```
告警/手动诊断
    │
    ▼
FastAPI API
    │
    ├── 同步路径（SSE 直连）→ distributed_slot 准入 → run_diagnosis_graph
    │
    └── 异步路径（提交排队）→ Postgres 建 task → Redis Streams 入队
                                                        │
                    ┌───────────────────────────────────┤
                    │                │                  │
                Worker-1         Worker-2           Worker-3
                    │                │                  │
                    └── distributed_slot（全局并发槽）──┘
                              │
                    run_diagnosis_graph
                              │
                    Postgres 写审计 + ACK
```

两条路径共享 `run_diagnosis_graph()`，但准入机制不同：SSE 直连满了立刻拒绝（返回"请排队"），Worker 路径满了等待（队列天然排队）。

---

## 1. 优先级队列

### 为什么需要优先级

P0 告警（"生产数据库宕机"）和 P3 告警（"测试环境磁盘 80%"）不应该排在同一个队列里按先来后到处理。

### 四级优先级

| Level | severity 映射 | 消费顺序 |
|---|---|---|
| `critical` | critical / page / P0 | 最先 |
| `high` | high / P1 | 次之 |
| `normal` | warning / P2 / 手动诊断 | 默认 |
| `low` | info / P3 | 最后 |

### 实现方式：多 Stream 严格插队

每个优先级对应一个独立 Stream：

```
aiops:diagnosis:critical  ← critical 告警
aiops:diagnosis:high      ← high 告警
aiops:diagnosis:normal    ← normal / 手动
aiops:diagnosis:low       ← low 告警
aiops:diagnosis           ← 旧 base stream（兜底兼容）
```

Worker 的 `read_tasks()` 消费策略：

1. 非阻塞逐级扫描：critical → high → normal → low → base，命中即返回
2. 全空时，对所有 Stream 做一次阻塞读（`block_ms` 超时），醒来后下一轮重新按优先级取

### 面试话术

> "我们用多 Stream 实现严格优先级。Worker 每轮先非阻塞地从 critical 到 low 逐级扫，有消息就立刻处理，保证高优先级插队。全空时才阻塞等。为什么不用单 Stream + score 排序？因为 Redis Streams 是 append-only 的，消息 ID 是自增的，没有原生的优先级排序能力。多 Stream 是 Redis 社区推荐的优先级模式。"

### 面试追问

**Q：为什么还保留 base stream？**

> 升级兼容。优先级开启前入队的旧消息在 base stream 里。Worker 把 base stream 追加到消费列表末尾，保证旧消息不丢。新消息按 level 写入对应 stream，base stream 最终会排空。

---

## 2. Worker 消费循环

### 核心循环（槽前置模式）

```python
while not stopping:
    try:
        # Step 0: 先抢全局并发槽 (wait=False), 拿到才去读 stream
        async with distributed_slot("worker_diagnosis", limit=2, wait=False):
            # Step 1: 先回收崩溃 Worker 的 stale pending 任务
            tasks = await claim_stale_tasks_once()

            # Step 2: 没有 stale → 读新任务（按优先级）
            if not tasks:
                tasks = await read_tasks(consumer_name, count=1, block_ms=5000)

            # Step 3: 处理任务
            for message_id, item in tasks:
                await handle_message(message_id, item)
    except DistributedLimitBusy:
        await asyncio.sleep(0.5)  # 容量满: 不碰 stream, 短暂退避
```

**为什么把并发槽前置到读取阶段？** 之前是先 XREADGROUP 读消息，再在 handle_message 里抢槽。问题是 XREADGROUP 一旦返回消息就进入了该 Worker 的 PEL（Pending Entries List），如果此时容量满拿不到槽，消息会堆在 PEL、状态虚标 running，还可能被其他 Worker 的 XAUTOCLAIM 回收导致双跑。改成"没容量就不碰 stream"，起再多 Worker 也只是多几个退避轮询者。

为什么每轮先 reclaim 再读新？

> "因为普通 `XREADGROUP` 只读新消息，不会自动处理崩溃 Worker 留下的 pending。每轮先 reclaim，确保旧任务优先恢复。"

### handle_message 的校验链

在真正跑诊断之前，Worker 做了四层校验：

| 校验 | 不通过的行为 |
|---|---|
| task_id 缺失 | 直接 DLQ |
| Postgres 里找不到 task | DLQ |
| task 已经是 succeeded | ACK 跳过（幂等保护） |
| attempts ≥ max_attempts | 标 failed + DLQ |

只有全部通过才进入 `_run_one_task()`。

---

## 3. 全局并发槽（distributed_slot）

### 问题

起 3 个 Worker 进程，每个进程内部用 `asyncio.Semaphore(2)` 控制并发。实际总并发 = 3 × 2 = 6。但 LLM API 配额可能只允许同时 2 个诊断。

### 解决方案

用 Redis 实现分布式并发槽（`distributed_slot`），所有 Worker 共享同一个全局上限。槽前置到读取阶段——先抢槽再读 stream：

```python
# 主循环: 槽前置
while not stopping:
    try:
        async with distributed_slot(
            "worker_diagnosis",
            limit=settings.worker_diagnosis_concurrency,  # 默认 2
            ttl_seconds=...,
            refresh_interval_seconds=...,
            wait=False,  # 拿不到立刻退避
        ):
            tasks = claim_stale() or read_tasks(block=5000ms)
            for msg in tasks:
                await handle_message(msg)  # 槽已由主循环持有
    except DistributedLimitBusy:
        await asyncio.sleep(0.5)  # 容量满: 不碰 stream
```

`wait=False`（Worker + SSE）：槽满了不碰 stream / 直接拒绝。Worker 短暂退避后重试，SSE 直接返回"请改用提交排队"。

### 槽的实现

底层是 Redis 的 key-value + TTL：

- 申请槽：`INCR aiops:slot:worker_diagnosis` → 超过 limit 就等/拒
- 释放槽：`DECR`
- TTL 兜底：防止进程崩溃后槽永久被占
- 心跳续期：长任务期间定期刷新 TTL，防止还在跑就被回收

### 面试话术

> "并发控制用 Redis 分布式槽，不是进程内 Semaphore。不管起多少个 Worker，真正在跑的诊断不会超过全局上限。槽前置到读取阶段——先抢槽再读 stream（wait=False），拿不到就退避不碰消息，避免消息堆在 PEL 被误回收双跑。SSE 路径同样 wait=False 满了直接拒绝引导用户排队。槽带 TTL 和心跳续期，进程崩了不会永久占位。"

---

## 4. Heartbeat 机制

### 为什么需要心跳

Redis Streams 能告诉你消息是否 pending（已投递未 ACK），但不能告诉你 Worker 进程是否还活着。心跳填补这个缺口。

### 实现

Worker 启动一个后台 `asyncio.Task`，每 10 秒写一个 TTL=30 秒的 Redis key：

```
aiops:worker:{group}:{consumer_name}:heartbeat = "1"  (TTL 30s)
```

正常 Worker 每 10 秒刷新一次，key 永远不过期。Worker 崩了 → 30 秒后 key 过期 → 可以被识别为离线。

### 用途

- `/api/v1/queue/status` 接口检查 heartbeat key 是否存在，返回每个 Worker 的 `alive` 状态
- 后续可以基于 heartbeat 做更精细的调度（比如只给 alive 的 Worker 分配任务）

---

## 5. Pending 回收（XAUTOCLAIM）

### 问题

Worker-1 拿了一条消息开始跑诊断，跑到一半进程崩了。这条消息在 Redis 的 PEL（Pending Entries List）里，状态是"已投递未 ACK"。新的 `XREADGROUP` 读不到它（它已经被投递过了），如果没有回收机制就永远卡住。

### 解决方案

`XAUTOCLAIM`：把空闲超过 `min_idle_ms`（默认 15 分钟）的 pending 消息转给当前 Worker。

```python
tasks = await claim_stale_tasks(
    consumer_name=self.consumer_name,
    min_idle_ms=settings.diagnosis_worker_reclaim_idle_ms,  # 900000 = 15min
    count=settings.diagnosis_worker_reclaim_count,          # 最多 5 条
)
```

### 关键参数

**为什么 min_idle_ms 要 15 分钟这么长？**

> "诊断任务本身可能跑 5-10 分钟（deep 模式更久），墙钟超时设的是 30 分钟（含等人工审批时间）。如果 idle 阈值太短（比如 2 分钟），正常运行中的长任务会被另一个 Worker 误回收，导致同一个任务被两个 Worker 同时执行。15 分钟阈值远大于实际诊断执行时间（通常 10 分钟以内），确保只有真正崩溃的任务才会被回收。"

---

## 6. 重试与 DLQ

### 重试流程

```
任务失败
    │
    ▼
检查 attempts vs max_attempts（默认 3）
    │
    ├── attempts < max → 重试
    │   ① Postgres: mark_task_retry_pending（状态回 pending）
    │   ② Redis: XADD 新消息（重新入队，保持原优先级）
    │   ③ Redis: ACK 当前失败消息
    │
    └── attempts >= max → DLQ
        ① Postgres: mark_task_failed
        ② Redis: XADD 到 DLQ stream + ACK 原消息
```

### DLQ 设计

DLQ（Dead Letter Queue）是一个独立的 Redis Stream（`aiops:diagnosis:dlq`），保存：

- original_message_id
- reason（失败原因，截断 2000 字）
- 原始 task 的所有字段
- payload 原文（JSON）

DLQ 的消息不会被自动处理。人工排查后可以选择：
- 修复问题后手动重新入队
- 确认是无效任务后直接清理

### 面试话术

> "重试策略是：失败后检查 attempts，没到上限就重新入队（XADD 新消息 + ACK 旧消息），保持原优先级不降级。到了上限就进 DLQ。DLQ 保留了完整的原始信息和失败原因，不会丢数据。为什么不在同一条消息上重试（用 XCLAIM）？因为重新 XADD 可以让消息获得新的 ID，在优先级队列里按正确位置排列，而不是永远排在队首阻塞后续任务。"

---

## 7. 集成测试覆盖

队列是 Worker 崩溃恢复与防重复执行的正确性根基，用一个最小 `FakeRedisStreams` 复刻 Redis Streams 的关键语义（xadd / xreadgroup / xack / xautoclaim + consumer group + PEL），覆盖以下场景：

| 测试场景 | 验证点 |
|---|---|
| 单流 FIFO 读取 + ACK | 先进先出、ACK 后不重复投递 |
| 多流严格优先级插队 | critical 先于 normal 先于 low |
| severity → level 推断 | enqueue 不传 level 时从 payload.severity 自动映射 |
| 死信队列搬运 | DLQ 保留原始信息 + ACK 原消息（在其真实优先级流上） |
| XAUTOCLAIM 回收崩溃 Worker | idle 超阈值的 pending 被转交 |
| 不抢新鲜 pending | idle 未超阈值的正在跑任务不会被误回收 |

不依赖真实 Redis 或 pytest-asyncio，沿用本仓库 `asyncio.run` 跑法。FakeRedisStreams 的 `now_ms` 是可控时钟，测 xautoclaim 时把它往前推即可模拟消息空闲超阈值。

### 面试话术

> "队列模块有完整的集成测试，用 FakeRedisStreams 复刻了 Stream/Consumer Group/PEL 语义。覆盖了 FIFO 投递、多流优先级插队、死信搬运、XAUTOCLAIM 回收和不误抢正在跑的任务。不需要真实 Redis，但验证的是 Worker 崩溃恢复和防重复执行的正确性。"

---

## 8. 为什么选 Redis Streams 而不是 Celery/RabbitMQ

这是面试高频追问。

| 维度 | Redis Streams | Celery + RabbitMQ |
|---|---|---|
| 部署复杂度 | 项目已经用 Redis（RAG 记忆），零额外组件 | 需要额外部署 RabbitMQ/Redis broker |
| 优先级 | 多 Stream 天然支持 | Celery 优先级需要额外配置 |
| 消息确认 | XACK 精确到消息级 | Celery 的 ack 语义较复杂 |
| 消息持久化 | Redis 自动持久化 | RabbitMQ 需要配置持久化 |
| 回溯能力 | Stream 是 append-only log，可回溯 | Queue 消费后消息消失 |
| 生态重量 | 几百行适配代码 | Celery 引入大量抽象层 |

> **面试话术**："Redis 是项目已有的依赖（RAG 记忆用它），不需要额外部署。Redis Streams 提供了 consumer group、XACK、XAUTOCLAIM 这些原语，足够实现可靠的任务队列。Celery 太重了——我们需要的不是一个通用任务框架，而是一个'可靠的消息传递 + 简单的消费确认'。几百行代码就够了。"

---

## 9. 遇到的难点总结

### 难点 1：重试时消息重复

最初重试策略是只 `XCLAIM` 把消息转给自己重新处理。但 XCLAIM 不改变消息在 Stream 里的位置——如果是优先级队列，一条 low 级别的消息永远待在 low stream 的队首，每次重试都要先轮到它。

解决方案：重试时 XADD 一条新消息 + ACK 旧消息。新消息获得新 ID，按正常顺序排列。

### 难点 2：全局并发槽的 TTL vs 长任务

最初 TTL 设短了（60 秒），deep 模式诊断跑 5 分钟，TTL 过期后槽被释放，另一个 Worker 拿到了槽，导致实际并发超过上限。

解决方案：`distributed_slot` 内部启动一个心跳续期 loop，每 N 秒刷新 TTL。TTL 初始值设长（比如 120 秒），心跳间隔设短（比如 30 秒），正常运行时永远不过期。进程崩了才会在 TTL 后自动释放。

### 难点 3：先读后抢槽导致 PEL 堆积和双跑

最初 Worker 的逻辑是先 XREADGROUP 读消息、再在 handle_message 里抢并发槽。问题是 XREADGROUP 返回消息后就进了当前 Worker 的 PEL，如果此时全局槽满（比如其他 Worker 正在跑），消息堆在 PEL 且状态已虚标 running，其他 Worker 的 XAUTOCLAIM 看到 idle 超阈值后会回收这些消息，导致同一任务被两个 Worker 同时执行。

解决方案：把并发槽前置到读取阶段——先抢槽（`wait=False`），拿到才读 stream；拿不到就短暂退避，不碰 stream。这样起再多 Worker，真正从 stream 拉消息的永远不超过全局槽上限。

### 难点 4：Worker 启动时 MCP 偶发失败

Worker 是独立进程，启动时也要连 MCP Server。但 Worker 可能比 MCP Server 先启动。

解决方案：`fail_silently=True`——MCP 连不上就以无工具模式继续。Worker 依然能处理知识库检索类的任务。MCP Server 起来后，下次诊断就能用工具了（因为 MCP 连接在每次 `get_all_tools()` 时会检查状态）。

---

## 快速回顾清单

| 模块 | 核心设计 | 面试关键词 |
|---|---|---|
| 优先级队列 | 四级 Stream + 严格插队消费 | 多 Stream 模式、兼容旧 base stream |
| Worker 循环 | 先 reclaim 再读新 + 四层校验 | 幂等保护、attempts 检查 |
| 全局并发槽 | Redis 分布式 INCR/DECR + TTL + 心跳 | 跨 Worker 共享、槽前置到读取阶段 |
| Heartbeat | TTL key + 定期刷新 | 判断 Worker 存活、30s 过期 |
| Pending 回收 | XAUTOCLAIM + 15min idle | 崩溃恢复、不误回收长任务 |
| 重试 | XADD 新消息 + ACK 旧消息 | 保持原优先级、不阻塞队首 |
| DLQ | 独立 Stream + 原始信息保留 | 不丢数据、人工排查 |
| 集成测试 | FakeRedisStreams 复刻关键语义 | FIFO/优先级/DLQ/XAUTOCLAIM/不误抢 |

---

*准备好了就说"开始 Step 12"，我们进入限流与权限控制。或者直接跳到 Step 14 评测体系也可以。*
