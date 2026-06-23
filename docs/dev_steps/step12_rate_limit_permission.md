# Step 12：限流 & 权限 — 六层并发治理体系

---

## 这一步要解决的问题

AIOps 平台面临的并发压力不是一种，而是六种不同层面的：

| 压力来源 | 如果不治理会怎样 |
|---|---|
| 用户狂点"开始诊断" | API 进程被长任务占满，页面卡死 |
| Alertmanager 一次推 500 条告警 | 500 个 Agent 同时跑，LLM 配额瞬间打爆 |
| 队列堆了 200 个任务 | 用户不知道排到哪了，以为系统挂了 |
| 3 个 Worker 同时消费 | LLM/MCP/Milvus 被并发打满 |
| 某个 IP 恶意刷接口 | 合法用户排不上队 |
| Worker 崩了 | 任务永久卡在 pending |

V3 的并发治理不是一个组件，而是**六层叠加**的体系。每层解决一个不同的问题。

---

## 六层并发治理全景

```
Layer 1: 接口限流（rate_limiter）
    ├── 手动诊断: 20 次/IP/分钟
    ├── Webhook: 500 条/source/分钟 + 50 次/IP/秒
    └── 超限 → 429

Layer 2: 接入层分流
    ├── 同步 SSE: 直连跑诊断，适合少量即时需求
    └── 异步提交: 快速返回 task_id，任务进队列

Layer 3: 全局并发槽（distributed_slot）
    ├── manual_diagnosis: SSE 直连最多同时 2 个
    └── worker_diagnosis: 所有 Worker 最多同时 2 个

Layer 4: 队列削峰（Redis Streams，Step 11 已讲）

Layer 5: 失败恢复（重试 + DLQ，Step 11 已讲）

Layer 6: 可观测（/api/v1/queue/status 实时暴露所有指标）
```

---

## 1. 接口限流（rate_limiter.py）

### 固定窗口计数器（Lua 原子脚本）

核心是一条 Lua 脚本，在 Redis 端原子完成"计数 + 首次设过期"：

```lua
local n = redis.call('INCR', KEYS[1])
if n == 1 then
  redis.call('EXPIRE', KEYS[1], tonumber(ARGV[1]))
end
return n
```

```
key = rate_limit:{scope}:{identity}:{window_bucket}
Lua(INCR + 首次 EXPIRE) → count
如果 count > limit → 拒绝，返回本窗口剩余秒数
```

`window_bucket = now // window_sec`，同一窗口内所有请求 INCR 同一个 key。窗口过期后 key 自动清理。

**为什么用 Lua 而不是分步 INCR + EXPIRE？** 分步操作在并发首请求时有竞态：多个请求各自拿到 `count==1` 都去 EXPIRE，窗口时长被反复重置，甚至 EXPIRE 丢失时 key 永不过期。Lua 脚本保证计数与首次设过期的原子性，一次 RTT 完成。

### 三组限流规则

| scope | identity | limit | window | 保护目标 |
|---|---|---|---|---|
| `manual` | client IP | 20/min | 60s | 防用户刷诊断接口 |
| `webhook` | source (receiver) | 500/min | 60s | 防单个 Alertmanager 刷爆 |
| `webhook_ip` | client IP | 50/sec | 1s | 防秒级突发 |

手动诊断的同步 SSE 和异步提交**共用**同一个 `manual` scope，避免用户同时刷两个接口绕限流。

### fail-open 设计

```python
async def _redis():
    try:
        return await incident_queue.client()
    except Exception:
        logger.warning("Redis 不可达, 限流降级放行")
        return None  # → hit() 返回 (True, 0) 放行
```

Redis 挂了 → 限流器放行。理由是：限流是保护性措施，不应该成为新的单点故障。"限流器挂了导致系统不可用"比"限流器挂了导致短暂流量无上限"更严重。

### 429 响应设计

```python
raise HTTPException(
    status_code=429,
    detail={"error": "rate_limited", "message": "...", "retry_after": retry_after},
    headers={"Retry-After": str(retry_after)},
)
```

`Retry-After` 头是 HTTP 标准，告诉客户端应该等多久再重试。这让前端可以做倒计时提示，Alertmanager 也能根据这个头做退避。

### 面试话术

> "限流用的是固定窗口计数器——单条 Lua 脚本原子完成 INCR + 首次 EXPIRE，一次往返、无竞态。早期用分步 INCR + EXPIRE 踩过坑：并发首请求各自拿到 count==1 都去 EXPIRE，窗口被反复重置。合进 Lua 后问题消失。三组规则分别保护手动诊断和 Webhook，fail-open 设计保证 Redis 挂了不影响核心功能。"

### 面试追问

**Q：为什么不用滑动窗口或令牌桶？**

> 固定窗口的缺点是窗口交界处有突刺（理论上一瞬间可以通过 2 倍 limit 的请求）。但对运维平台来说，这个突刺完全可以接受——又不是支付接口，多放几个请求进来最多多排几个队。滑动窗口需要 Redis Sorted Set + 更复杂的 Lua，复杂度上去了收益不大。需要更平滑时，接口不变，底层换成 Lua 滑动窗口即可。

**Q：你说用了 Lua，那 INCR + EXPIRE 分两步有什么问题？**

> 并发首请求各自拿到 count==1，都去执行 EXPIRE，窗口时长被反复重置。更严重的是如果 INCR 成功但 EXPIRE 丢失（比如网络抖动），key 永不过期，计数器在所有后续窗口累加，最终所有请求都被限流。合进一条 Lua 脚本后两步操作在 Redis 端原子完成，不存在中间状态。

**Q：手动诊断和 Webhook 为什么分开限流？**

> 流量特征完全不同。手动是人类操作，20 次/分钟足够；Webhook 是机器推送，正常也可能一分钟几百条告警。如果共用一个 limit，要么对人太宽（500 次/分钟没意义），要么对机器太紧（20 次/分钟会丢大量告警）。

---

## 2. 分布式并发槽（distributed_limiter.py）

### 和限流的区别

限流（rate_limiter）控制的是**请求速率**——一分钟能进多少个。
并发槽（distributed_slot）控制的是**同时执行数**——此刻能跑几个。

一个 500 次/分钟的限流放进来 500 个请求，但真正同时在跑 LLM 的只允许 2 个。

### 两个资源的并发控制

| 资源 | 默认上限 | wait 模式 | 用途 |
|---|---|---|---|
| `manual_diagnosis` | 2 | `wait=False` | SSE 直连，满了立刻拒绝 |
| `worker_diagnosis` | 2 | `wait=False` | Worker 消费，满了退避不碰 stream |

### SlotHandle 生命周期

```python
async with distributed_slot("worker_diagnosis", limit=2, wait=False):
    # 持有槽期间才读 stream + 执行诊断
    tasks = read_tasks(...)
    result = await run_legacy_langgraph_with_audit(...)
# 离开 with 自动释放; DistributedLimitBusy 时退避不碰 stream
```

内部做了三件事：

1. **申请**：Redis INCR，超过 limit 则抛 `DistributedLimitBusy`（Worker 主循环 catch 后退避）
2. **心跳续期**：后台 task 每 30 秒刷新 TTL（防长任务被误回收）
3. **释放**：离开 with 块时 DECR（包括异常退出）

Worker 把 `distributed_slot` 前置到消费循环最外层——先抢槽再读 stream。这避免了"先读消息再等槽"导致消息堆在 PEL、被其他 Worker XAUTOCLAIM 回收双跑的问题。

### 审批等待时的 pause/resume

这是一个精巧的设计。当 Agent 的工具调用触发了人工审批（ASK_DESTRUCTIVE 模式），诊断会暂停等待审批结果（可能等 5 分钟）。这期间不应该占着并发槽——否则其他任务都排不上。

`SlotHandle` 提供 `pause()` 和 `resume()` 方法：

- `pause()`：释放槽 + 停止心跳（不占位了）
- `resume()`：重新申请槽 + 恢复心跳（继续占位）

`tool_runner` 在等审批时调 `pause()`，审批通过后调 `resume()`。通过 `ContextVar` 访问当前协程持有的 SlotHandle，不需要层层传参。

### 面试话术

> "并发槽和限流是两个维度：限流管速率，并发槽管同时执行数。两个都需要，缺一不可。并发槽用 Redis INCR/DECR + TTL 实现，跨进程共享。亮点一是槽前置到读取阶段——先抢槽再读 stream，避免消息堆在 PEL 被双跑；亮点二是审批等待时会 pause 释放槽，审批通过后 resume 重新获取，不浪费宝贵的执行名额。"

---

## 3. 压测验证

这部分是面试时展示"端到端验证"的好素材。

### 压测数据总结

| 场景 | 请求/并发 | 结果 |
|---|---|---|
| API 读压（queue/status） | 1000/100 | 100% 成功，367 req/s |
| API 读压（极限） | 3000/200 | 100% 成功，136 req/s，P99 升到 5s |
| 后台诊断提交 | 200/100 | 100% 成功，98 req/s |
| Webhook 洪峰 | 500/100 | 100% 成功，197 req/s |
| 限流验证 | 40/40 | 20 成功 + 20 被 429（符合 20/min 规则） |
| 真实执行 | 8/8 混合优先级 | 全部 succeeded，槽峰值 2/2，队列最终清零 |

### 面试怎么讲

> "我们写了一个纯 Python 的压测脚本（asyncio + httpx），覆盖了六个场景。最关键的验证是：3 个 Worker 同时存活，但真实执行槽始终不超过 2/2。第三个 Worker 领了任务但在等槽，不会超跑。限流场景 40 并发打 40 个请求，正好一半成功一半 429，精确命中 20/分钟的规则。"

---

## 4. PermissionMode 回顾（Step 6 已详讲）

这里做一个面试串讲版的快速回顾，把限流和权限连起来：

```
请求进来
    │
    ▼
Layer 1: 限流 → 429 拦截高频请求
    │
    ▼
Layer 3: 并发槽 → 满了拒绝或排队
    │
    ▼
诊断开始执行
    │
    ▼
Layer: PermissionMode → 控制 Agent 能调什么工具
    ├── READ_ONLY: 只暴露只读工具
    ├── NORMAL: Skill 白名单 + Guardrails 黑名单
    ├── ASK_DESTRUCTIVE: 写操作走人工审批
    └── BYPASS: 开发模式
```

限流管"谁能进来"，并发槽管"谁能跑"，PermissionMode 管"跑的时候能干什么"。三层各管各的，互不替代。

---

## 5. 遇到的难点总结

### 难点 1：限流和并发槽容易混淆

最初把限流和并发槽写在一起，逻辑纠缠不清。后来拆成完全独立的两个模块，各有各的 Redis key 前缀、各有各的 fail-open 策略。

### 难点 2：并发槽泄漏

Worker 进程被 `kill -9` 时，`with` 块的 `finally` 不执行，槽不会被释放。如果没有 TTL 兜底，槽会永久泄漏，最终所有槽都被占满，系统停止处理新任务。

解决方案：每个槽有 TTL（默认 90 秒），心跳续期间隔 30 秒。正常运行时永远不过期，进程被 kill 后 90 秒自动释放。

### 难点 3：INCR + EXPIRE 竞态（已修复）

最初限流器用分步 INCR + EXPIRE：先 `INCR key`，如果 `count == 1` 再 `EXPIRE key window_sec`。压测时发现在高并发首请求场景下，多个请求同时拿到 `count==1`，都去调 EXPIRE，窗口时长被反复重置。极端情况（EXPIRE 命令丢失）key 永不过期，后续所有请求全部命中同一个 key 的累积计数，导致合法请求也被限流。

解决方案：合进一条 Lua 脚本，INCR 和 EXPIRE 在 Redis 端原子执行，彻底消除竞态。

### 难点 4：Webhook 双重限流的必要性

最初 Webhook 只有每分钟 500 条的限制。但压测发现：500 条可以在 1 秒内全部打来（100 并发 × 5 轮），瞬间把 Redis INCR 压到很高。

加了秒级限流（50/IP/秒）后，即使 Alertmanager 瞬间推送，也会被均匀地打散到多秒内。分钟级限流防"刷爆"，秒级限流防"瞬间洪峰"。

---

## 快速回顾清单

| 层级 | 核心设计 | 面试关键词 |
|---|---|---|
| 接口限流 | 固定窗口 INCR + fail-open | 429 + Retry-After、三组规则分开 |
| 接入分流 | SSE 直连 vs 异步提交 | 同步满了引导排队 |
| 全局并发槽 | Redis INCR/DECR + TTL + 心跳 | 跨 Worker 共享、pause/resume |
| 队列削峰 | Redis Streams 优先级队列 | Step 11 |
| 失败恢复 | 重试 + DLQ | Step 11 |
| 可观测 | /queue/status 暴露全部指标 | 深度/pending/lag/DLQ/槽占用 |
| PermissionMode | 四级模式控制工具权限 | Step 6 |

---

## 压测通过标准（面试备用）

如果面试官问"你怎么验证并发治理是有效的"，可以列举这些标准：

1. API 始终可达（health check 不断）
2. 限流场景精确返回 429
3. 同一事件组不出现多个活跃任务（去重有效）
4. Worker 全局并发不突破配置上限（槽有效）
5. 队列最终清零（depth=0, pending=0, lag=0）
6. DLQ 无异常增长
7. 无遗留泄漏的并发槽
8. API 和 Worker 无持续增长的资源占用

---

*准备好了就说"开始 Step 13"（Web UI）或直接跳到"开始 Step 14"（评测体系），看你想练哪块。*
