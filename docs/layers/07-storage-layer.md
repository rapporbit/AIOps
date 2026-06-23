# Layer 7: 存储与审计层

> 目录: `app/db/`, `app/incidents/`, `app/evidence/`, `app/wiki/`
>
> 上游: Layer 1 (任务落库) / Layer 3 (审计写入 AgentRun/ToolCall/Evidence) / Layer 4 (Wiki 经验沉淀) → 下游: 前端查询 / Layer 4 (Wiki 召回注入 Skill Router)

存储层让诊断过程从"一次性报告"升级为"可回溯的事实链"。Postgres 保存结构化事实，LLM Wiki 沉淀经验知识。

## 7.1 Postgres 事实库 (8 张表)

### 表关系

```
alerts ──────────────┐
                     ▼
incident_groups ◄──── incidents
       │
       ▼
diagnosis_tasks ──── agent_runs ──── tool_calls
       │                 │
       ▼                 ▼
   evidence         evidence
       │
       ▼
approval_requests
```

### 表结构详解

#### alerts — 归一化告警

```sql
id                 TEXT PRIMARY KEY
idempotency_key    TEXT UNIQUE          -- 幂等键, 防止重复入库
fingerprint        TEXT                 -- Alertmanager 指纹
status             TEXT                 -- firing / resolved
alertname          TEXT                 -- 告警名称
severity           TEXT                 -- critical / warning / info
service            TEXT                 -- 所属服务
instance           TEXT                 -- 实例标识
labels             JSONB                -- 原始标签
annotations        JSONB                -- 注解 (summary, description)
raw_payload        JSONB                -- 完整原始 payload
query              TEXT                 -- 归一化后的诊断查询
first_seen         TIMESTAMPTZ          -- 首次出现
last_seen          TIMESTAMPTZ          -- 最近出现
seen_count         INTEGER DEFAULT 1    -- 重复次数
```

#### incident_groups — 告警分组

同一 `correlation_key` 的告警聚合为一个 incident group，避免同一故障创建多个诊断任务。

```sql
id                 TEXT PRIMARY KEY
correlation_key    TEXT UNIQUE          -- 聚合键
status             TEXT                 -- open / closed
severity           TEXT                 -- 组内最高严重度
primary_service    TEXT                 -- 主服务
alert_count        INTEGER DEFAULT 0    -- 包含告警数
```

#### diagnosis_tasks — 诊断任务

```sql
id                 TEXT PRIMARY KEY
incident_group_id  TEXT FK → incident_groups ON DELETE CASCADE
status             TEXT                 -- pending / running / succeeded / failed / cancelled
priority           INTEGER              -- 数字优先级
diagnosis_mode     TEXT                 -- fast / deep
max_attempts       INTEGER DEFAULT 3
attempts           INTEGER DEFAULT 0    -- 已尝试次数
payload            JSONB                -- 任务参数
error              TEXT                 -- 失败原因
dedup_key          TEXT                 -- 去重键
repeat_count       INTEGER DEFAULT 0    -- 重复提交次数
claimed_at         TIMESTAMPTZ          -- Worker 领取时间
finished_at        TIMESTAMPTZ          -- 完成时间
```

**去重索引 (Partial Unique)**:

```sql
CREATE UNIQUE INDEX idx_diagnosis_task_dedup_active
ON diagnosis_tasks(dedup_key)
WHERE status IN ('pending', 'running');
```

只对活跃任务去重，已完成的任务不阻止新提交。相同 incident_group 的重复提交只增加 `repeat_count`。

#### evidence — 证据

```sql
id                 TEXT PRIMARY KEY
incident_group_id  TEXT FK
source             TEXT                 -- alert/log/metric/trace/runbook/rca/mcp_tool_result/human_feedback
type               TEXT                 -- 分类标签
summary            TEXT                 -- 一句话摘要
content            JSONB                -- 结构化内容 (因 source 而异)
score              FLOAT                -- 相关性/置信度
occurred_at        TIMESTAMPTZ          -- 证据时间点
metadata           JSONB                -- 附加信息
```

#### agent_runs — Agent 执行记录

```sql
id                 TEXT PRIMARY KEY
task_id            TEXT FK
agent_name         TEXT                 -- skill_router / planner / executor / ...
status             TEXT                 -- running / succeeded / failed / cancelled
evidence_ids       JSONB                -- 产出的 evidence ID 列表
tool_call_count    INTEGER
input_tokens       INTEGER
output_tokens      INTEGER
total_tokens       INTEGER
started_at         TIMESTAMPTZ
finished_at        TIMESTAMPTZ
```

#### tool_calls — 工具调用记录

```sql
id                 TEXT PRIMARY KEY
agent_run_id       TEXT FK
tool_name          TEXT                 -- get_local_cpu_memory / search_knowledge_base / ...
status             TEXT                 -- pending / succeeded / failed
args               JSONB                -- 调用参数
result_ref         TEXT                 -- 结果引用
elapsed_ms         INTEGER              -- 执行耗时
error              TEXT                 -- 失败原因
```

#### approval_requests — 审批请求

```sql
id                 TEXT PRIMARY KEY
task_id            TEXT
tool_name          TEXT                 -- 被审批的工具
tool_args          JSONB                -- 工具参数
reason             TEXT                 -- 需要审批的原因
impact_summary     TEXT                 -- 影响摘要
status             TEXT                 -- pending / approved / denied / timeout / cancelled
decided_by         TEXT                 -- 审批人
decision_reason    TEXT                 -- 审批理由
expires_at         TIMESTAMPTZ          -- 超时时间
```

## 7.2 任务状态机

```
                    ┌──────────── (重试, attempts < max) ────────────┐
                    │                                                │
  create ──→ pending ──→ running ──→ succeeded                      │
                │                       │                            │
                │                       ├──→ failed ← ──────────────┘
                │                       │     (attempts >= max → DLQ)
                │                       └──→ cancelled
                │
                └──→ (dedup: repeat_count++)
```

关键转换：

| 转换 | 方法 | 触发条件 |
|------|------|---------|
| → pending | `create_task` | 新任务创建或重试入队 |
| pending → running | `mark_task_running` | Worker 领取, attempts++ |
| running → succeeded | `mark_task_succeeded` | 诊断完成, 附带 report 和 evidence_ids |
| running → failed | `mark_task_failed` | 重试耗尽 |
| running → pending | `mark_task_retry_pending` | 失败但还有重试次数, claimed_at 清空 |

## 7.3 证据链可追溯

一次诊断完成后，从 task_id 可以追溯完整链路：

```
diagnosis_task (task_id)
    │
    ├─ agent_run: 执行了哪个 Agent, 花了多少 token
    │     │
    │     ├─ tool_calls: 调了哪些工具, 参数是什么, 结果如何, 耗时多少
    │     │
    │     └─ evidence[]: 产出了哪些证据
    │
    ├─ evidence[]: 所有证据 (metric_snapshot, log_excerpt, runbook_match, ...)
    │
    └─ approval_requests[]: 审批记录 (如果有)
```

前端 `/incidents/tasks/{id}` 页面展示这条完整链路。

## 7.4 LLM Wiki 经验沉淀

### 设计理念

诊断完成后，结论不应该只存在最终报告里。相似故障再次发生时，Skill Router 和诊断 Agent 应该能回忆起上次的经验。

### 目录结构

```
data/wiki/
├── index.md              # 目录索引 (read-index-first)
├── log.md                # 追加式诊断日志
├── services/             # 按服务组织的经验页
│   ├── redis.md
│   ├── kubernetes.md
│   └── ...
└── patterns/             # 按故障模式组织的经验页
    ├── oomkiller-redis.md
    ├── cpu-spike.md
    └── ...
```

### 写入流程 (诊断完成后)

```
诊断报告
    │
    ▼ LLM 合并 (ingest_diagnosis)
    读取 services/{service}.md + patterns/{pattern}.md 现有内容
    │
    ▼ 构建 Prompt: "合并本次诊断到已有页面, 保留: 现象/根因/处置"
    │
    ▼ LLM 输出合并后的页面 + 日志条目
    │
    ▼ 写入文件 (fcntl 文件锁, 多进程安全)
    │
    ▼ 更新 index.md (去重)
```

**LLM 不可用时**：退化为确定性追加（append 而不是 merge）。

### 召回流程 (诊断开始前)

```
用户查询 + service
    │
    ▼ 直接路径: 加载 services/{service}.md + patterns/{pattern}.md
    │
    ▼ 回退路径: 解析 index.md, 关键词匹配找到相关页面
    │
    ▼ 返回 top-2 页面作为上下文注入 Skill Router Prompt
```

### 关键设计

- **Best-effort**：写入/召回失败静默捕获，绝不阻断诊断流程
- **去重更新**：同一 pattern 页面更新而不是追加新文件
- **关键词匹配**：不用向量搜索，用 2+ 字母词 + CJK 字符的简单分词
- **Markdown 原生**：人类可读可编辑，不需要专门工具
- **运行时数据**：Wiki 内容不提交到 Git，属于运行时生成的知识

### 维护工具

```python
wiki_store.lint()  # 检查:
# - 孤立页面 (没有被任何页面链接)
# - 不在 index.md 中的页面
# - 空页面
```

## 7.5 DDL 并发安全

多个 Worker 同时启动时，DDL (CREATE TABLE IF NOT EXISTS) 可能冲突：

```python
# app/db/postgres.py
async with conn.begin():
    await conn.execute(text("SELECT pg_advisory_lock(8207440167)"))
    # ... CREATE TABLE IF NOT EXISTS ...
    await conn.execute(text("SELECT pg_advisory_unlock(8207440167)"))
```

使用 Postgres Advisory Lock 序列化 DDL 操作，防止并发建表冲突。

## 7.6 关键配置

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `postgres_url` | `postgresql+asyncpg://...` | Postgres 连接串 |
| `incident_pipeline_enabled` | `True` | 是否启用事实库 |
| `wiki_enabled` | `True` | 是否启用 Wiki |
| `wiki_recall_max_chars` | `2000` | 召回内容最大字符数 |

## 模拟面试问答

### 🔥 热点拷问

**面试官：你的知识库只支持 Markdown 上传，实际运维知识可能在飞书、腾讯文档、Confluence、PDF 里。多源知识管理怎么考虑的？**

当前确实只支持 Markdown 上传，这是 V3 作为验证阶段的简化。生产级知识库管理需要几个能力：一是多源连接器——飞书 API、腾讯文档 API、Confluence API 定期拉取，转换为统一格式。二是增量同步——对比文档的 `updated_at` 或内容 hash，只重新 chunk 和 embedding 变化的部分。三是文件格式支持——PDF 用 pdfplumber、Word 用 python-docx、HTML 用 BeautifulSoup、图片需要 OCR。这些在 V3 架构下可以扩展——分块和检索管道已经解耦，上游加 connector + parser 层就行——但当前没做。

**追问：增量同步具体怎么做？文档更新后旧的 chunk 怎么处理？全量重建索引吗？**

文档级增量：当一个文档更新时，按 `source` 字段 `DELETE FROM kb_chunks WHERE source = $1` 删掉该文档所有旧 chunk，重新分块后写入新 chunk，做到文档级粒度，不需要全量重建。向量(pgvector HNSW)和 BM25(ParadeDB pg_search)两个索引都**随 INSERT/DELETE 自动维护**，不需要任何手动 `refresh`——这正是把 BM25 从进程内存 `rank_bm25` 迁到 pg_search 后的好处：旧版内存 BM25 每次变更要全量重建、且每个进程各建一份，现在 DB 侧单份共享、即时生效，到几十万 chunk 也不再有内存/重建瓶颈。

---

**面试官：你的 Postgres 8 张表，数据量大了之后查询性能怎么样？考虑过分区吗？**

当前开发环境下几百到几千条记录，性能不是问题。但上生产后 `tool_calls` 和 `evidence` 表增长最快——每次诊断 4-6 条 tool_call 和 3-5 条 evidence，一天 100 次诊断就是每月 15000+ 条 tool_call。应该按 `created_at` 做时间范围分区（按月），超过保留期的数据归档到冷存储。`diagnosis_tasks` 的 Partial Unique Index 已经只对活跃任务有效，历史任务不影响索引性能。当前没做分区是因为数据量不支撑这个投入。

---

**面试官：Wiki 经验沉淀这个功能，有没有出现过"经验误导诊断"的情况？**

"经验误导"的风险确实存在——如果历史诊断结论本身是错的，被沉淀到 Wiki 后会误导后续诊断。当前没有经验质量审核机制，这是一个短板。改进方向：一是 Wiki 写入前做置信度检查（诊断状态必须是 succeeded 且有足够 evidence），二是支持人工审核和编辑（Wiki 是 Markdown 文件，可以直接编辑），三是给 Wiki 经验加过期机制，避免过时经验长期存在。

**追问：Wiki 为什么不用向量搜索做召回？**

Wiki 内容量小（几十个页面），向量搜索过重。当前用"直接路径 + 关键词匹配"两级召回：先按 service 名直接加载对应页面（`services/redis.md`），没命中再解析 index.md 做关键词匹配。简单、快速、零额外依赖。如果 Wiki 规模增长到几百页面可以考虑向量搜索，但当前不值得。

### 深度追问链

**面试官：（接多源同步问题）多源同步你说"可以做但没做"，这话面试里说出来会不会减分？**

坦诚讲不足反而加分——面试官更怕候选人不知道系统缺什么。没做的原因是优先级判断：V3 核心目标是验证"后台化诊断系统"的工程可行性。知识库管理是重要但不紧急的能力——手动上传 Markdown 足以验证 RAG 管道有效性。架构上没困难，分块和检索管道已经解耦，上游加 connector 层就行。但每个数据源的 connector 都是独立的工程量——飞书 API 的认证和分页、腾讯文档的权限管理、Confluence 的 REST API 兼容性——不是技术难题但需要时间。

**继续追问：Wiki 用 LLM 合并诊断经验，合并质量有保证吗？怎么验证？**

合并质量没有系统性验证。风险包括：信息丢失（旧经验被覆盖）、幻觉注入（LLM "创造"了原文没有的内容）、格式破坏。当前缓解：LLM 不可用时退化为确定性追加。如果要验证，可以做人工抽检——每周随机抽 5 个 Wiki 页面比对合并前后差异。另一个方向是用另一个 LLM 做自动校验，但这引入了新的幻觉风险。

### 常规问题

**面试官：为什么同时用 Postgres 和 Redis，不能只用一个？**

职责不同。Redis 保存运行态数据：队列、执行槽、心跳、限流计数——高频读写、TTL 自动清理、丢了可重建。Postgres 保存事实数据：告警、任务、证据、工具调用——需要持久化、关联查询和事务一致性、丢了不可恢复。两者的访问模式和持久性要求完全不同。

### 反思与改进

**面试官：存储设计如果重来你会改什么？**

两个。一是 tool_calls 表加原始结果存储——当前只存 `result_ref`，回看诊断时能看到"调了 get_local_cpu_memory"但看不到返回了什么。应该加 `result_content` 字段存完整输出。二是给所有表加 `created_at` 索引——当前按时间范围查询会全表扫描。

**面试官：上线前还差什么？**

四个。一是自身监控——队列堆积告警、Worker 死亡告警、LLM 错误率告警，当前没有。二是配置热更新——改执行槽数量需要重启。三是多租户——当前共享队列和知识库，企业级需要隔离。四是前端 RBAC——谁能看什么任务、谁能审批什么操作。
