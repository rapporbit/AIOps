# Layer 2: 队列与缓冲层

> 目录: `app/queue/redis_streams.py`
>
> 上游: Layer 1 (API XADD 写入任务) → 下游: Layer 3 (Worker XREADGROUP 消费任务)

队列层解决的核心问题：**告警洪峰和并发提交不能直接打到 Agent 执行链路上**。Redis Streams 承担削峰缓冲，支持四级优先级和故障恢复。

## 2.1 优先级队列架构

```
                          ┌─→ aiops:incident_tasks:critical
任务入队 → level_for_severity() ─┼─→ aiops:incident_tasks:high
                          ├─→ aiops:incident_tasks:normal
                          └─→ aiops:incident_tasks:low
```

每个优先级对应一条独立的 Redis Stream。`priority_enabled=False` 时退化为单流 FIFO。

### 优先级映射

```python
# 告警严重度 → 队列层级
"critical" / "page" / "p0"  →  "critical"
"high" / "p1"               →  "high"
"warning" / "p2"            →  "normal"   # 手动诊断也默认 normal
"info" / "low" / "p3"       →  "low"
```

兼容旧版数字 priority：≤10 → critical, ≤50 → high, ≤100 → normal, >100 → low。

## 2.2 两阶段消费模式

这是队列设计的核心创新点。Redis Streams `XREADGROUP` 不原生支持跨流优先级，我们用两阶段模式实现**严格抢占**：

```
Phase 1: 非阻塞优先级扫描
    critical 有数据? → 立即返回 ← 不看后续流
    high 有数据?     → 立即返回
    normal 有数据?   → 立即返回
    low 有数据?      → 立即返回
    全部为空          → 进入 Phase 2

Phase 2: 阻塞等待
    XREADGROUP 同时监听所有流, BLOCK 5000ms
    有数据 → 重新进入 Phase 1 (确保优先级)
    超时   → 重新进入 Phase 1 (检查 stale)
```

**为什么不直接在 Phase 2 返回？** 因为 XREADGROUP 的 BLOCK 模式在多流上是"任一有数据就返回"，不保证返回的是最高优先级。所以每次唤醒后必须回到 Phase 1 做严格优先级扫描。

**为什么不只用 Phase 1 轮询？** 全部为空时 Phase 1 是 busy-wait，浪费 CPU。Phase 2 的 BLOCK 把空闲等待交给 Redis 内核，零 CPU 消耗。

## 2.3 XADD / XREADGROUP 模式

### 入队 (XADD)

```python
message_id = await redis.xadd(
    stream_key,                    # aiops:incident_tasks:{level}
    fields={
        "task_id": task_id,
        "incident_group_id": ...,
        "diagnosis_mode": "fast",
        "priority": 50,
        "level": "normal",
        "payload": json.dumps(payload)
    },
    maxlen=10000,                  # 近似长度限制, 防止无限增长
    approximate=True               # 允许略微超出, 换取写入性能
)
```

### 消费 (XREADGROUP)

```python
# Consumer Group: diagnosis-workers
# Consumer Name: worker-1 (per process)
items = await redis.xreadgroup(
    groupname="diagnosis-workers",
    consumername="worker-1",
    streams={stream_key: ">"},     # ">" = 只读新消息
    count=1,                       # 一次取一条
    block=5000                     # 阻塞 5 秒
)
```

## 2.4 Pending 恢复与 DLQ

### Stale 任务回收

Worker 崩溃后，已领取但未 ACK 的消息会留在 PEL (Pending Entries List) 中。通过 `XAUTOCLAIM` 实现恢复：

```
每次消费循环开始前:
    XAUTOCLAIM min_idle=900000ms (15分钟)
    → 回收闲置超过 15 分钟的 pending 消息
    → 交给当前 Worker 处理
```

**15 分钟阈值**是关键：墙钟超时 1800 秒（30 分钟，含等人工审批时间），但实际诊断执行通常在 10 分钟以内，15 分钟阈值加上安全边际，确保正在执行的任务不会被误回收。

### 死信队列 (DLQ)

重试耗尽的任务进入 DLQ，保留完整上下文：

```python
await redis.xadd("aiops:incident_tasks:dlq", {
    "task_id": task_id,
    "original_msg_id": msg_id,
    "reason": "max_attempts_exceeded",
    "incident_group_id": ...,
    "diagnosis_mode": ...,
    "payload": ...
})
# ACK 原始消息, 防止重复回收
await redis.xack(original_stream, group, msg_id)
```

DLQ 消息不会被自动消费，需要人工排查后决定是否重新入队。

## 2.5 心跳机制

```
Key: aiops:worker:diagnosis-workers:worker-1:heartbeat
TTL: 30 秒
刷新间隔: 10 秒
```

心跳只做运行态存活检测，不是事实权威。`/queue/status` 接口据此判断 Worker 是否存活。

## 2.6 队列状态可观测

`/api/v1/queue/status` 聚合以下指标：

| 指标 | 含义 |
|------|------|
| `depth` | 待处理总量 (pending + lag) |
| `pending` | 已分发但未 ACK |
| `lag` | 未分发 |
| `depth_by_level` | 各优先级待处理量 |
| `dlq_depth` | 死信队列长度 |
| `workers[].alive` | 各 Worker 存活状态 |
| `workers[].pending` | 各 Worker 手上未完成的消息数 |
| `workers[].idle_ms` | 各 Worker 上次活动距今毫秒数 |

## 2.7 关键配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `incident_queue_stream` | `aiops:incident_tasks` | 基础流名称 |
| `incident_queue_priority_enabled` | `True` | 是否启用四级优先级 |
| `incident_queue_maxlen` | `10000` | 单流最大长度 |
| `incident_queue_consumer_group` | `diagnosis-workers` | 消费者组 |
| `diagnosis_worker_block_ms` | `5000` | 阻塞等待超时 |
| `diagnosis_worker_reclaim_idle_ms` | `900000` | Stale 回收阈值 (15min) |
| `diagnosis_worker_reclaim_count` | `5` | 单次回收上限 |

## 2.8 集成测试

`tests/test_incident_queue.py` 用最小 FakeRedisStreams 复刻 Redis Streams 关键语义，不依赖真实 Redis：

| 场景 | 验证点 |
|------|--------|
| 单流 FIFO + ACK | 先进先出、ACK 后移出 PEL 不重复投递 |
| 多流严格优先级 | critical 先于 normal 先于 low（插队，非 FIFO） |
| severity → level 推断 | enqueue 不传 level 时从 payload.severity 自动映射到正确流 |
| 死信搬运 | DLQ 保留原始信息 + 在真实优先级流（非 base）上 ACK |
| XAUTOCLAIM 回收 | idle 超阈值的 pending 被转交新 Worker |
| 不误抢新鲜 pending | idle 未超阈值的在跑任务不被回收（防双跑） |

FakeRedisStreams 的 `now_ms` 是可控时钟，测 xautoclaim 时把它往前推即可模拟消息空闲超阈值。

## 模拟面试问答

### 🔥 热点拷问

**面试官：你的两阶段消费，Phase 1 扫 4 条流 + Phase 2 BLOCK，这个切换开销大不大？**

开销很小。Phase 1 的非阻塞扫描是 4 次 XREADGROUP（count=1, block=0），每次返回几百字节，整个 Phase 1 在毫秒级。Phase 2 的 BLOCK 是 Redis 内核挂起，零 CPU。实际瓶颈完全不在这里——诊断任务的处理时间是 30-60 秒，队列消费速度不是瓶颈。

**追问：你说 15 分钟回收阈值，但墙钟超时是 30 分钟，那岂不是正在执行的任务会被误回收？**

不会。墙钟超时 30 分钟是含人工审批等待的极端情况。审批等待时 Worker 调 `slot.pause()` 释放并发槽——此时任务不在"活跃执行"状态。实际诊断执行时间通常在 10 分钟以内。15 分钟阈值远大于实际执行时间，确保只有 Worker 真正崩溃（没来得及 ACK）的任务才会被回收。另外，并发槽已前置到读取阶段，Worker 只在持槽时才读消息，不会出现"读了消息但没容量跑"导致消息空闲超阈值的情况。

---

**面试官：DLQ 里的任务你说要人工排查，有工具吗？实际怎么操作？**

目前没有专门的 DLQ 管理工具，这是一个工程完善度的短板。当前做法是通过 `/queue/status` 接口看到 `dlq_depth > 0`，然后直接查 Redis 的 DLQ stream 里的任务详情。如果上生产，应该在前端事件中心加一个 DLQ 视图，支持一键重新入队或永久标记丢弃。

### 常规问题

**面试官：优先级队列真的有必要吗？场景能举个例子吗？**

告警洪峰场景。假设监控系统一次推送 50 条告警，其中 2 条是 critical（数据库主从断连）、48 条是 warning（磁盘使用率 80%）。没有优先级时 Worker 按 FIFO 消费，critical 可能排在第 30 个才被处理。有优先级后 critical 直接进最高优先级流，Worker 每次循环先扫 critical 流，确保它们被最先处理。

### 反思与改进

**面试官：队列设计过程中踩过什么坑？**

两个。一是 Consumer Group 的自动创建——Worker 启动时队列 stream 还不存在会报错，解决方法是启动时先 XGROUP CREATE + MKSTREAM。二是 XAUTOCLAIM 的 min-idle 单位是毫秒不是秒——第一次写成了 900（0.9 秒），导致正在执行的任务被立即回收、多个 Worker 重复执行。排查花了半天，教训是看文档要看单位。
