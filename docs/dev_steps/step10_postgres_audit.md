# Step 10：Postgres 事实库 & 审计链

---

## 这一步要解决的问题

前面的 Fast/Deep 诊断图跑完后，所有信息都在内存里——过程消失了。但企业级运维需要回答这些问题：

- "上周那次 Redis 告警是怎么处理的？" → 需要**完整的诊断记录**
- "Agent 在诊断过程中调了哪些工具？" → 需要**工具调用审计**
- "根因判定是基于什么证据得出的？" → 需要**证据链追溯**
- "这个告警是不是之前已经处理过了？" → 需要**去重幂等**

Postgres 事实库就是为了把"一次诊断的完整链路"持久化下来，让每一步都可追溯、可审计、可回放。

---

## 数据模型：7 张核心表

### 表关系全景图

```
alerts ─────────────┐
  (原始告警)         │ N:N
                     ▼
incident_groups ◄── incident_group_alerts (关联表)
  (事件组)
    │ 1:N
    ▼
incidents ──────────────────────────────────┐
  (单次事件)                                 │
    │ 1:N                                   │ 1:N
    ▼                                       ▼
diagnosis_tasks                         evidence
  (诊断任务)                              (证据)
    │ 1:N
    ▼
agent_runs
  (Agent 执行记录)
    │ 1:N
    ▼
tool_calls
  (工具调用记录)

approval_requests (审批请求，独立表)
```

### 每张表的职责

| 表 | 存什么 | 关键字段 |
|---|---|---|
| **alerts** | 原始告警（来自 Alertmanager 或手动） | fingerprint, idempotency_key, seen_count |
| **incident_groups** | 告警聚合组（同一根因的多个告警归为一组） | correlation_key, alert_count |
| **incidents** | 单次事件实例 | incident_group_id, status, severity |
| **diagnosis_tasks** | 诊断任务（排队、执行、结束） | status, priority, attempts, dedup_key |
| **agent_runs** | 一次 Agent 执行记录 | input_ref, output_ref, evidence_ids, token 统计 |
| **tool_calls** | 每次工具调用的详细记录 | tool_name, args, elapsed_ms, error |
| **evidence** | 证据（告警原文、工具结果、诊断步骤、报告） | source, type, summary, content(JSONB) |
| **approval_requests** | ASK_DESTRUCTIVE 模式的人工审批 | status, decided_by, expires_at |

---

## 1. 告警去重与事件聚合

### 告警层面：idempotency_key 去重

Alertmanager 可能因为重试或重复发送同一条告警。alerts 表用 `idempotency_key`（UNIQUE 约束）做幂等——重复告警走 `ON CONFLICT DO UPDATE`，只更新 `last_seen` 和 `seen_count`，不插入新行。

### 事件组层面：correlation_key 聚合

同一个根因可能触发多条告警（比如 Redis 内存高 → 触发 memory 告警 + latency 告警 + error_rate 告警）。这些告警通过 `correlation_key` 聚合到同一个 `incident_group`。

correlation_key 的生成规则：`SHA256(service + alertname + time_bucket)`。`time_bucket` 是按配置秒数（默认 300 秒）对齐的时间窗口——同一个 5 分钟窗口内、同一个服务的同名告警归入同一组。

### 面试话术

> "告警去重分两层：告警级别用 idempotency_key 做幂等去重，事件组级别用 correlation_key 做聚合。比如 Redis 在 5 分钟内连续发了 10 条 memory 告警，alerts 表只更新 seen_count=10，incident_groups 只有一条记录。诊断任务只对事件组创建，不是每条告警都跑一遍 Agent。"

---

## 2. 诊断任务的去重幂等

### 问题

Alertmanager webhook 可能在短时间内对同一组告警发多次请求。如果每次都创建一个新的 diagnosis_task，就会有多个 Agent 同时诊断同一个故障。

### 解决方案：数据库层部分唯一索引

```sql
ALTER TABLE diagnosis_tasks ADD COLUMN IF NOT EXISTS dedup_key TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_diagnosis_task_dedup_active
    ON diagnosis_tasks(dedup_key)
    WHERE status IN ('pending', 'running');
```

`dedup_key` = `incident_group_id` 的哈希。部分唯一索引的含义是：**同一个 dedup_key 在 pending/running 状态下只允许存在一条**。如果已经有一个 pending 的任务，再 INSERT 会触发 `ON CONFLICT`，走更新路径（只增加 `repeat_count` 和 `last_seen_at`）。

### 面试怎么讲

> "去重没用 Python 的 SELECT-then-INSERT（有竞态），而是用了 Postgres 的部分唯一索引——只在活跃状态（pending/running）上做唯一约束。已完成的任务不受约束，同一个告警组可以有多条历史任务。这样做到了：活跃任务幂等，历史任务完整保留。"

---

## 3. 审计链 — 从告警到报告的完整链路

### 一次诊断会产生什么

```
告警进来
  │
  ▼
① alerts 表: 存原始告警
② incident_groups: 创建或聚合事件组
③ diagnosis_tasks: 创建诊断任务（pending → running → succeeded/failed）
  │
  ▼ Worker 开始执行
④ evidence (alert_payload): 把告警原文存为第一条证据
⑤ agent_runs: 创建 AgentRun 记录（status=running）
  │
  ▼ Agent 执行中
⑥ tool_calls: 每次工具调用写一行（tool_name, args, elapsed_ms, error）
⑦ evidence (tool_call): 每次工具调用结果存为证据
⑧ evidence (diagnosis_step): 每个 Executor 步骤完成后存为证据
  │
  ▼ Agent 执行完毕
⑨ evidence (diagnosis_report): 最终报告存为证据
⑩ agent_runs: 更新 AgentRun（status=succeeded, output_ref=report_evidence_id, token 统计）
⑪ diagnosis_tasks: 更新任务状态（succeeded + 报告内容）
```

### evidence 表的四种来源

| source | type | 产生时机 |
|---|---|---|
| `ALERT` | `alert_payload` | 诊断开始时 |
| `MCP_TOOL_RESULT` | `tool_call` | 每次工具调用后 |
| `MCP_TOOL_RESULT` | `diagnosis_step` | 每个 Executor 步骤完成后 |
| `RCA` | `diagnosis_report` | 最终报告生成后 |

### 面试话术

> "我们的审计链是从告警原文到最终报告的完整链路。一次诊断会产生一条 AgentRun、N 条 ToolCall、N 条 Evidence。每条 Evidence 都有 source 和 type 标签，可以按来源过滤——比如只看工具调用结果，或只看诊断步骤。前端的证据链页面就是按 evidence 表的时间线渲染的。"

---

## 4. audit.py — 审计包装器

### 设计定位

`audit.py` 是 Worker 路径专用的包装器。它不知道 Redis 队列、SSE、HTTP——只关心"怎么把 LangGraph 的事件流转化为审计记录"。

手动 SSE 路径不经过 audit.py（因为没有 task_id 事实行），只有 Worker 路径才写审计。

### 核心流程

```python
async def run_legacy_langgraph_with_audit(task_id, item):
    # 1. 复用或创建 AgentRun + 初始 Evidence
    # 2. 遍历 run_diagnosis_graph() 产生的事件流
    #    - tool_call → 写 ToolCall 行 + Evidence
    #    - step_complete → 写 Evidence
    #    - report → 写 Evidence
    #    - usage → 累加 token 统计
    # 3. 成功 → finish_run(succeeded) / 失败 → finish_run(failed)
```

### 重试复用设计

这个设计细节面试时可以讲：

任务第一次执行失败后，Worker 会把它重新入队。第二次执行时，audit.py 会先查 `agent_run_repository.list_runs_for_task(task_id)`——如果已经有 AgentRun，就**复用它**（续上 evidence_ids），而不是再建一个新的。

为什么这样做？

> "如果每次重试都创建新的 AgentRun 和 alert_payload Evidence，最终成功的那次 AgentRun 就看不到前几次尝试产生的证据。复用让最终的 AgentRun 能列出跨多次尝试的全部 Evidence。"

### CancelledError 的特殊处理

Worker 超时或进程关闭时，asyncio 会抛 `CancelledError`。audit.py 显式 catch 它，把 AgentRun 标为 `cancelled`（而不是留在 `running`）。否则 Postgres 里会残留 running 状态的幽灵记录。

---

## 5. approval_requests — 人工审批表

### 设计背景

ASK_DESTRUCTIVE 模式下，Agent 想调 `docker_restart` 这类写操作时，tool_runner 不会直接执行，而是写一条 `pending` 状态的审批请求到 `approval_requests` 表，然后轮询等待结果。

管理员在前端看到审批请求后，可以 approve 或 deny。`tool_runner` 轮询到结果后，对应执行或跳过。超时（默认 5 分钟）自动转 `timeout`（等同 deny）。

### 关键字段

| 字段 | 说明 |
|---|---|
| `tool_name` | 要执行的工具名 |
| `tool_args` | 工具参数（JSONB） |
| `impact_summary` | 影响描述（给审批人看） |
| `status` | pending → approved/denied/timeout/cancelled |
| `decided_by` | 谁批的 |
| `expires_at` | 超时时间（默认当前时间 + 5 分钟） |

### 面试话术

> "高危写操作走人工审批。Agent 提出请求后写到 approval_requests 表，进入轮询等待。超时 5 分钟自动 deny。整个审批过程也是可审计的——谁批的、什么时候批的、批了什么操作，全部记录在案。"

---

## 6. Schema 演进策略

### 为什么用 `init_incident_schema()` 而不是 migration 工具

项目目前处于快速迭代阶段，用的是"启动时自动建表"的策略：

```python
async def init_incident_schema() -> None:
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(_SCHEMA_SQL)
```

`_SCHEMA_SQL` 全部用 `CREATE TABLE IF NOT EXISTS` + `ADD COLUMN IF NOT EXISTS` + `CREATE INDEX IF NOT EXISTS`，保证幂等——跑多少次都不会出错。

### 面试追问

**Q：生产环境不用 Alembic 吗？**

> "当前是 MVP 阶段，用启动时自动建表更灵活。所有 DDL 都是幂等的（IF NOT EXISTS），旧库升级平滑。后续稳定后会迁移到 Alembic 做版本化管理——主要是为了记录 schema 变更历史和支持回滚。"

**Q：为什么选 Postgres 而不是 MongoDB？**

> "因为我们的数据模型是**结构化关系型**的——告警属于事件组，事件组有多个诊断任务，任务有多个 AgentRun，AgentRun 有多个 ToolCall。这些实体之间是标准的 1:N 外键关系，用关系型数据库的 JOIN、事务、约束（UNIQUE、FK、部分唯一索引）非常自然。JSONB 字段（payload、content、metadata）给半结构化数据留了灵活性。"

---

## 7. 级联删除设计

删除一个 diagnosis_task 时，要连带清理它关联的所有数据。项目做了精细的级联策略：

```
删除 task
  │
  ├── 检查同组是否还有其他 task
  │   ├── 有 → 只删 task 级别：approval_requests, tool_calls, agent_runs, task 本身
  │   └── 没有 → 整组清理：上述 + evidence + incidents + incident_groups + alerts（如果也不属于其他组）
  │
  └── 孤儿 alert 清理：alert 不属于任何 incident_group → 删除
```

为什么 alert 的删除要检查"是否属于其他组"？因为 alerts 和 incident_groups 是 N:N 关系（通过 `incident_group_alerts` 关联表），一条 alert 可能属于多个组。只有当它不属于任何组时才能删。

---

## 8. 遇到的难点总结

### 难点 1：Python SELECT-then-INSERT 竞态

最初的去重逻辑是：先 SELECT 看有没有活跃任务 → 没有就 INSERT。但两个并发请求可能同时 SELECT 都返回空，然后同时 INSERT，创建了两个重复任务。

解决方案：改用 Postgres 部分唯一索引 + `ON CONFLICT DO UPDATE`，把幂等逻辑下推到数据库层，消除竞态。

### 难点 2：Agent 失败后审计记录的一致性

Agent 执行到一半失败了，已经写了 3 条 Evidence 和 2 条 ToolCall。如果 AgentRun 状态停在 `running`，管理员看到的是一个"还在跑"的任务，但实际已经挂了。

解决方案：audit.py 的 try-except-finally 确保任何退出路径（成功、失败、CancelledError）都会调 `finish_run()` 更新 AgentRun 状态。

### 难点 3：evidence_ids 的跨重试延续

第一次执行产了 evidence A、B、C 后失败。第二次重试产了 D、E 后成功。如果 AgentRun 只记录第二次的 evidence_ids = [D, E]，那 A、B、C 就"脱离"了这个诊断链路。

解决方案：重试时复用 AgentRun，把第一次的 evidence_ids 续上。最终的 AgentRun.evidence_ids = [A, B, C, D, E]，完整覆盖所有尝试。

---

## 快速回顾清单

| 模块 | 核心设计 | 面试关键词 |
|---|---|---|
| alerts | idempotency_key 去重 + seen_count | ON CONFLICT DO UPDATE |
| incident_groups | correlation_key 聚合 + time_bucket | 同根因告警归组 |
| diagnosis_tasks | 部分唯一索引去重 + dedup_key | 数据库层幂等、消除竞态 |
| evidence | 四种来源、JSONB content | 完整证据链追溯 |
| agent_runs | 重试复用 + CancelledError 处理 | evidence_ids 跨重试延续 |
| tool_calls | 每次调用写一行 | elapsed_ms、args、error |
| approval_requests | pending → approve/deny/timeout | 5 分钟超时自动 deny |
| audit.py | Worker 专用包装器 | 事件流 → 审计记录的转化 |

---

*准备好了就说"开始 Step 11"，我们进入 Redis Streams 任务队列与 Worker 机制。*
