# 知识库多源摄取与增量同步 — 设计文档 (v3 · 已实现)

> 状态: **已实现 (P0–P4 全部落地, 真实飞书+MinerU+pgvector 端到端验证)** · 日期: 2026-06-23
> · 范围: 飞书数据源 + MinerU 官方 API 解析 + pgvector 增量入库
>
> 提交记录: P0 `58df73d` / P1 `7eeefac` / P2 `0760682` / revert `e66a0e3` / P3 `ecbe540`
> / P4 `1e05b4d` / review 整改 `eac0d65`。各阶段验收与真实验证见 §8。
>
> v2→v3 主要变化 (实现过程中确定): ①飞书"指定文档(node_tokens)"模式一度加入又移除
> (租户最终支持把应用加为 space 成员, 整库枚举即可, 见 §9)；②调度落地为**进程内 asyncio
> 周期任务** (非 diagnosis_worker 的 Redis Streams, 见 §4.5)；③手动 sync 接口**后台执行**
> + runs 轮询 (review 整改, 见 §6.1)；④MinerU 客户端分项超时适配大文件；⑤补 `tests/` (§8)。

---

## 0. 前置依赖与基线

**基线 = pgvector 迁移完成 (已合并, commit `4ebf6eb` 及之前)。** 本特性建于其上。

迁移既有事实 (本设计据此实现):
- BM25 是 **ParadeDB pg_search** 索引 (`pg_vector_store.py` 建在 `kb_chunks` 上)，
  **随 INSERT/DELETE 自动维护，无需任何手动刷新**。
- 原删除按 `source` (`delete_by_source`)；本特性新增按 `doc_id` 的路径 (`delete_by_doc_id`/`replace_doc`)。
- `add_documents` 内部「先算 embedding 再 INSERT」；新增的 `replace_doc` 把 embedding 移到事务外。

---

## 1. 背景与目标

当前知识库是「静态、单格式、手动单向导入」: 仅支持 `.md/.txt` 直传 (`app/services/document_service.py`)，
按文件名 (`source`) 删除 (`app/core/pg_vector_store.py:delete_by_source`)，没有外部数据源、没有增量同步。

本设计要把知识库升级为「**可从飞书自动拉取、多格式、增量更新**」的摄取层。
下游检索流水线 (Hybrid + Rerank + Parent-Child，见 `app/rag/retrieval.py` / `app/core/hybrid_retriever.py`) **完全不动**。

### 1.1 本期范围 (In Scope)

| 维度 | 决策 |
|---|---|
| 外部数据源 | **只接飞书 (Lark)**，先跑通。文档量小，不一次性灌入。 |
| 文档解析 | **只用 MinerU 官方在线 API** (`mineru.net`，云调用，不本地部署)。 |
| 解析失败策略 | **无 fallback。失败即报错、拒绝入库，标记 `failed` 留待下轮重试。** |
| 向量库 | **pgvector** (Milvus 已在另一分支迁移中，本设计基于 `kb_chunks` 表)。 |
| 支持格式 | pdf / doc(x) / ppt(x) / 图片 / html (经 MinerU)；md/txt 直通不走 MinerU。 |
| 同步方式 | 定时轮询增量 (飞书 `obj_edit_time`/`revision` 为主) + markdown 哈希免重嵌 + 幂等 upsert。 |

### 1.2 非目标 (Out of Scope，留后续迭代)

- 腾讯文档 / 语雀 / Notion / 对象存储等其他源 (Connector 接口为其预留)。
- DashScope / Docling 等其他解析器 (Parser 接口为其预留，但本期不实现)。
- 飞书事件订阅 (webhook) 实时同步 (本期只做定时轮询，webhook 留作增量触发优化)。

---

## 2. 总体架构

```
                          ┌─────────────────────────────────────────────┐
   [定时调度]              │                 摄取层 (新增)                  │
  cron / interval ──────► │                                               │
   每源一个 sync 任务      │  ┌──────────────┐   list_changes()           │
                          │  │  Connector   │──────────────►  变更清单     │
                          │  │  (Feishu)    │   fetch_blob()  (增/改/删)   │
                          │  └──────────────┘                             │
                          │         │ docx/pdf/图片 二进制                  │
                          │         ▼                                     │
                          │  ┌──────────────┐  失败→raise→标记 failed      │
                          │  │  Parser      │  (MinerU 官方 API, 无 fallback)│
                          │  │  (MinerU)    │──────────────►  Markdown      │
                          │  └──────────────┘                             │
                          │         │ markdown                            │
                          │         ▼                                     │
                          │  ┌──────────────┐                             │
                          │  │ Sync Engine  │  version+hash 防抖           │
                          │  │ (幂等 upsert) │  delete_by_doc_id + insert  │
                          │  └──────────────┘                             │
                          └─────────┬───────────────────────────────────┘
                                    │ Document[]  (复用现有 splitter)
                                    ▼
        ┌──────────────────────────────────────────────────────────┐
        │  现有流水线 (不改): splitter → embedding → pgvector kb_chunks │
        │  检索: similarity_search + BM25 + RRF + Rerank + parent 去重  │
        └──────────────────────────────────────────────────────────┘

   状态持久化 (Postgres 新增 3 表): kb_source / kb_document / kb_sync_run
```

**核心约定**: 所有格式先归一化成 **Markdown**，复用 `app/core/splitter.py:split_markdown` 的 parent-child 切分，
下游一行不改。飞书原生文档先导出为 docx，再过 MinerU，保持「一切皆文件 → MinerU → markdown」的单一解析路径。

---

## 3. 数据模型

### 3.1 改造 `kb_chunks` (pgvector) — 引入稳定 `doc_id`

现状: `kb_chunks` 只有 `source` (文件名/标题)，删除按 `source`。多源场景下 `source` 既不唯一也不稳定，
无法做增量幂等。**新增 `doc_id` 字段，作为「一篇文档」的稳定主键**，删旧 chunk / upsert 全按 `doc_id`。

```sql
-- 增量改造 (幂等迁移)
ALTER TABLE kb_chunks ADD COLUMN IF NOT EXISTS doc_id TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_kb_chunks_doc_id ON kb_chunks(doc_id);
```

- `doc_id` = `kb_document.id` (见 3.2)。本地直传的 md/txt 也分配一个 doc_id (退化为单文档源)。
- `source` 字段保留 (展示用，存文档标题或飞书链接)，但删除/更新逻辑改走 `doc_id`。
- chunk 不需要确定性 ID: 更新走 `replace_doc(doc_id, chunks)`，「先 `DELETE WHERE doc_id=$1` 清旧，
  再批量 `INSERT` 新」，chunk 数变化也安全。BM25 (pg_search) 索引随这两步 DML **自动维护，无需刷新**。
- **存量回填 (C2/G2)**: `ADD COLUMN doc_id DEFAULT ''` 后，现有 ~2.5k 旧 chunk 的 doc_id 为空，
  会与新逻辑混用。库很小，**P0 直接清库重灌**最干净 (`TRUNCATE kb_chunks` 后重跑 `scripts/ingest_kb_corpus.py`，
  入库时为每个 source 生成 `doc_id='local:'+source`)；不接受清库则按 source 回填 doc_id。
- **原子性 (C1)**: 不能沿用现有 `add_documents` 的「内部先 embedding 再单独 INSERT」——若先 `delete_by_doc_id`
  再调 `add_documents`，而 embedding 在 add 内部失败，旧 chunk 已删、新的没写 → 该文档凭空消失。
  因此新增 `replace_doc(doc_id, chunks)`: **先在外部把 embedding 算好，再在同一连接/同一事务里
  `DELETE ... WHERE doc_id` + `INSERT`**，失败则整体回滚，旧数据不丢。

### 3.2 新增同步状态表 (Postgres，复用 `app/db/postgres.py` 的 asyncpg 连接池)

```sql
-- 数据源 (一行一个外部源实例)
CREATE TABLE IF NOT EXISTS kb_source (
    id           TEXT PRIMARY KEY,              -- 如 'feishu:wiki:<space_id>'
    type         TEXT NOT NULL,                 -- 'feishu' (本期只此一种)
    name         TEXT NOT NULL,                 -- 人类可读名
    config       JSONB NOT NULL DEFAULT '{}',   -- {space_id/folder_token, scope...}; 不存密钥
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    sync_cursor  TEXT,                          -- 预留 (飞书走时间比对，可空)
    last_sync_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 文档 (一行一篇外部文档，承载增量状态)
CREATE TABLE IF NOT EXISTS kb_document (
    id             TEXT PRIMARY KEY,            -- 稳定 doc_id; 飞书用 'feishu:'+obj_token
    source_id      TEXT NOT NULL REFERENCES kb_source(id),
    external_id    TEXT NOT NULL,               -- 飞书 obj_token
    external_type  TEXT NOT NULL DEFAULT '',    -- docx/sheet/file/pdf/...
    title          TEXT NOT NULL DEFAULT '',
    uri            TEXT NOT NULL DEFAULT '',     -- 原文链接
    source_version TEXT NOT NULL DEFAULT '',    -- obj_edit_time / document_revision_id (粗筛)
    content_hash   TEXT NOT NULL DEFAULT '',    -- MinerU 输出 markdown 的 SHA-256 (用于免重嵌, 见 4.3)
    status         TEXT NOT NULL DEFAULT 'pending', -- pending|active|failed|deleted
    chunk_count    INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT NOT NULL DEFAULT '',
    last_synced_at TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at     TIMESTAMPTZ,
    UNIQUE (source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_kb_document_source ON kb_document(source_id, status);

-- 同步审计 (一行一次 source 同步运行)
CREATE TABLE IF NOT EXISTS kb_sync_run (
    id          BIGSERIAL PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES kb_source(id),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'running', -- running|success|failed
    stats       JSONB NOT NULL DEFAULT '{}',     -- {scanned,added,updated,skipped,deleted,failed}
    error       TEXT NOT NULL DEFAULT ''
);
```

> 密钥 (飞书 app_secret、MinerU token) 走配置/环境变量，**不入库** (见第 7 节)。

DDL 建表沿用 `pg_vector_store.init_vector_schema` 的 advisory-lock 串行化套路，放到一个新模块
`app/core/kb_sync_schema.py` 或并入现有 schema 初始化，启动时幂等执行。

---

## 4. 组件设计

### 4.1 Parser 层 — MinerU 官方 API 客户端

新建 `app/core/parsers/base.py` + `app/core/parsers/mineru.py`。接口抽象 (为未来其他解析器留口)：

```python
class DocumentParser(Protocol):
    async def to_markdown(self, blob: bytes, *, filename: str, need_ocr: bool = False) -> str:
        """把文档二进制解析成 Markdown。失败时 raise ParseError，绝不返回降级结果。"""
```

**MinerU 官方 API 调用流程** (Precision `/api/v4`，异步，已核实):

1. `POST https://mineru.net/api/v4/file-urls/batch`
   Header `Authorization: Bearer {MINERU_TOKEN}`
   Body: `{"files":[{"name": filename, "is_ocr": <见下>}], "model_version":"vlm", "enable_formula": true, "enable_table": true, "language":"ch"}`
   → 返回 `data.batch_id` + `data.file_urls[]` (OSS 预签名 PUT 地址，24h 有效)。
   **`is_ocr` 按类型决定 (G3)**: 原生 docx / 文本版 pdf → `false` (有文本层，开 OCR 又慢又掉保真)；
   扫描件 / 图片 → `true`。`to_markdown` 增加 `need_ocr: bool` 入参，由调用方 (Connector/上传) 判定。
2. `PUT {file_urls[0]}`，body = 文件二进制。**注意: 不要带 `Authorization` header** (预签名自带授权，带了会 403)。上传完成系统自动提交解析。
3. 轮询 `GET /api/v4/extract-results/batch/{batch_id}`，看 `state`: `pending|running|converting|done|failed`。
4. `done` → 下载 `full_zip_url` (zip)，解压取 `full.md` 作为 Markdown 结果。
5. `failed` / 轮询超时 → `raise ParseError(detail)`。

实现要点 (与项目现有风格一致):
- 用 `httpx.AsyncClient` 裸 HTTP (不用官方同步 SDK，便于异步轮询与超时控制)。
- 轮询: 间隔 `mineru_poll_interval_sec` (默认 3s)，总超时 `mineru_timeout_sec` (默认 300s)。
- zip 解压在内存 (`zipfile.ZipFile(io.BytesIO(...))`)，只取 `full.md` 与内嵌图片 (图片本期可忽略，仅留文本)。
- **失败语义**: 任何一步异常都向上抛 `ParseError`，由调用方决定「拒收 + 标记 failed」。
- 配额: 提交 50 文件/分、查询 1000 次/分、5000 文件/天 (当前免费)。文档量小，本期不做限流，仅对 429 做指数退避重试。
- **大小预检 (G4)**: 提交前校验 `len(blob) ≤ mineru_max_bytes` (默认 200MB，MinerU 上限)，超限直接 `raise ParseError` 早拒，不浪费一次提交配额。

> 关于飞书原生文档: 飞书 docx 是结构化 block，不是文件。本期为保持「单一 MinerU 路径」，
> 走 **飞书 export_task 导出为 docx → 喂给 MinerU (docx 模式，无需 OCR)**。
> (替代方案: 飞书 blocks API 直转 markdown，保真更高且免解析，但引入第二条解析路径，与「只用 MinerU」的约束相悖，故本期不采用，列为后续优化候选。)

### 4.2 Connector 层 — 飞书

新建 `app/core/connectors/base.py` + `app/core/connectors/feishu.py`。接口抽象：

```python
@dataclass
class DocRef:
    external_id: str        # 飞书 obj_token
    external_type: str      # docx / sheet / file ...
    title: str
    uri: str
    version: str            # obj_edit_time 或 document_revision_id
    deleted: bool = False

class SourceConnector(Protocol):
    async def list_changes(self, source: KbSource) -> list[DocRef]:
        """列出源下全部文档及其当前版本 (本期全量列举，删除靠快照 diff，见 4.3)。"""
    async def fetch_blob(self, ref: DocRef) -> tuple[bytes, str, bool]:
        """取 (文档字节, 建议文件名, need_ocr)。原生 docx 走 export_task→下载(need_ocr=False)；
        Drive 文件直下(扫描件/图片 need_ocr=True)。"""
```

**飞书实现要点** (已核实):

- **鉴权**: 企业自建应用，用 App ID + App Secret 换 `tenant_access_token` (有效期 2h，需缓存+定时刷新)。
  请求头 `Authorization: Bearer {tenant_access_token}`。
  ⚠️ **前置条件**: 必须把应用作为「文档应用/协作者」添加到目标 Wiki 知识库/文件夹，否则列表返回空。
  所需权限 scope: `wiki:wiki(:readonly)`、`docs:doc`、`drive:drive(:readonly)`、`drive:file` (以后台实际名称为准)。
- **列文件 (`list_changes`) — 整库枚举** (`config.space_id`):
  `GET /open-apis/wiki/v2/spaces/{space_id}/nodes` 分页 BFS 遍历整棵知识库树，
  取 `obj_token` / `obj_type` / `obj_edit_time` / `title`。
  ⚠️ 需应用是**知识库 (space) 成员**（应用须先开启「机器人」能力并发布、可用范围含操作者，
  再在知识库设置→成员里加为成员；否则该接口返回 `131006 wiki space permission denied`）。
  - ⚠️ Wiki node 与文档 obj 分离: node 只是挂载点，内容要再用 `obj_token` 调对应文档 API。
  - (预留) Drive 文件夹: `GET /open-apis/drive/v1/files` (返回 `modified_time` / `token` / `type`)。
- **节点类型白名单 (G1)**: Wiki 节点有 docx / sheet / bitable / mindnote / file / 快捷方式 等多种 `obj_type`，
  export_task 也只支持 docx/pdf/xlsx/csv (mindnote 不能导)。本期**只处理 `{docx, doc, file(pdf/图片/office)}`**，
  其余类型 (sheet/bitable/mindnote/shortcut) **跳过并记日志**，不计入 failed。类型映射也决定后续 `need_ocr`。
- **取内容 (`fetch_blob`)**:
  - 原生 docx/doc: `POST /open-apis/drive/v1/export_tasks` (target=docx) → 轮询 ticket → 下载 → 得 docx 字节 (`need_ocr=false`)。
  - Drive 内的真实文件 (pdf/图片/docx 附件): 直接下载字节 (扫描件/图片 `need_ocr=true`)。
  - → 统一交给 MinerU。
- **限频**: 多数接口 3–5 QPS，节点改类每日 1 万次上限；对 429 (错误码 99991400) 指数退避。

### 4.3 Sync Engine — 增量检测 + 幂等 upsert

新建 `app/services/kb_sync_service.py`。核心算法 (单个 source 的一次同步)：

```
1. run = 新建 kb_sync_run(source_id, status=running)
2. refs = connector.list_changes(source)              # 远端当前全量 (已过类型白名单)
3. remote_ids = {ref.external_id for ref in refs}
4. for ref in refs:
     doc = kb_document.get(source_id, ref.external_id)
     # —— 粗筛: 版本号没变且上次成功 → 跳过 (省 fetch+MinerU+embedding)。这是主力省流手段。
     if doc and doc.status == 'active' and doc.source_version == ref.version:
         stats.skipped++; continue
     # —— status==failed 的文档无视版本，强制重试 (实现「稍后重试」)
     try:
         blob, name, need_ocr = connector.fetch_blob(ref)
         md = parser.to_markdown(blob, filename=name, need_ocr=need_ocr)  # MinerU; 失败 raise
         h = sha256(md)                                  # 对 MinerU 输出的 markdown 取哈希 (见下)
         # —— 免重嵌: 内容真的没变 (版本号变了但正文一样) → 只更版本号, 不重切/不重嵌
         if doc and doc.content_hash == h and doc.status == 'active':
             kb_document.update(doc.id, source_version=ref.version); stats.skipped++; continue
         chunks = split_markdown(md, source=ref.title)
         for c in chunks: c.metadata['doc_id'] = doc.id  # 注入稳定 doc_id
         vectors = embed(chunks)                          # 先把 embedding 算好 (易失败的一步前置)
         # —— 原子幂等替换: 同一连接/事务内 DELETE WHERE doc_id + INSERT, 失败整体回滚
         await pg_vector_store.replace_doc(doc.id, chunks, vectors)
         kb_document.upsert(status='active', source_version=ref.version,
                            content_hash=h, chunk_count=len(chunks), last_error='')
         stats.added_or_updated++
     except Exception as e:
         kb_document.upsert(status='failed', last_error=str(e))  # 不写向量, 旧数据保留, 留待下轮重试
         stats.failed++
5. # —— 删除传播: 远端消失的文档, 清向量 + 软删
   for doc in kb_document.active_of(source) if doc.external_id not in remote_ids:
       await pg_vector_store.delete_by_doc_id(doc.id)
       kb_document.soft_delete(doc.id); stats.deleted++
6. run.finish(status=success, stats); source.last_sync_at = now()
   # 注: 无 BM25 手动刷新——pg_search 索引随 replace_doc/delete 的 DML 自动维护。
```

**省流策略 (C2 修订)**: 主力是 ① `source_version` 粗筛 (版本没变直接跳过，免 fetch/解析/嵌入)。
② `content_hash` **改为对 MinerU 输出的 markdown 取**，而非导出文件字节——飞书导出的 docx 是带导出时间戳的
zip，字节哈希每次都变、做不了精筛；markdown 哈希能在「版本变了但正文没变」时省掉重切+重嵌 (但仍付一次 MinerU)。
**原子性 (C1)**: `replace_doc` 在单事务内先 DELETE 再 INSERT，embedding 已在事务外算好；任何失败回滚，旧 chunk 不丢。
**删除传播**: 快照 diff (远端 id 集合 vs 本地 active 集合)，软删 (`deleted_at` + 清向量)。
**重试**: `failed` 文档下轮无条件重试，正好实现「MinerU 不可用时稍后重试」。

### 4.4 pgvector 改造 (`app/core/pg_vector_store.py`)

最小改动:
- 建表 DDL 加 `doc_id TEXT` 列 + 索引 (见 3.1)。`add_documents`: 写入时把 `metadata['doc_id']` 落到新列。
- 新增 `delete_by_doc_id(doc_id) -> int` (照搬 `delete_by_source` 改 WHERE 条件)。
- 新增 `replace_doc(doc_id, chunks, vectors)`: **单连接单事务内** `DELETE WHERE doc_id` + `executemany INSERT`
  (向量由调用方预先算好传入，把易失败的 embedding 移出事务)，整体失败回滚。这是 C1 的核心修复。
- `similarity_search` / `load_all_chunks` / `bm25_search` 等检索路径**不变** (字段约定兼容)。
- **BM25 (pg_search) 无需任何改动**: 索引建在 `kb_chunks` 上，随 `replace_doc` / `delete_by_doc_id` 的
  DML 自动维护，全程无手动刷新 (旧版 `refresh_bm25_index` 已随 pg_search 迁移废弃)。

### 4.5 调度 — 进程内 asyncio 周期任务 (已实现)

实现为 `app/services/kb_scheduler.py`，在 app lifespan (`main.py`) 启动时注册、关闭时优雅停止。
**未塞进 `diagnosis_worker` 的 Redis Streams**——KB 同步是「周期性整源扫描」，不是 per-task 作业，
用不上 DLQ/认领/心跳那套；且本期量小，单源串行同步即可。

| 项 | 实现 |
|---|---|
| 触发 | 定时 (`KB_SYNC_INTERVAL_SEC`，默认 30min) 遍历 `enabled` 数据源 + 手动 API |
| 首轮 | 启动后延迟 `min(30, interval)` 秒再首次同步，让 app 先就绪 |
| 隔离 | 单源失败不影响其它源 (各自 try/except) |
| 并发安全 | `sync_source` **先抢 advisory lock 再 list_changes**，多副本/与手动触发并发时，抢不到锁的直接返回 `{"skipped_reason":"locked"}`，不重复调外部 API |
| 开关 | `KB_SYNC_ENABLED=false` 时不启动调度器 |

> 量大后可再拆 `kb.doc.ingest` 子任务并发 (用 `asyncio.Semaphore` 限并发)，接口已为此预留。

---

## 5. 端到端时序 (飞书原生 docx，首次同步)

```
Scheduler ─► kb_sync_service.sync(source)
  └► FeishuConnector.list_changes()
        └► wiki/v2/.../nodes (分页)  ──► [DocRef(obj_token, obj_edit_time, title)...]
  └► 对每个新 DocRef (版本粗筛未命中):
        ├► FeishuConnector.fetch_blob()  → (docx字节, name, need_ocr=False)
        │     └► drive/v1/export_tasks(docx) → 轮询 ticket → 下载 docx 字节
        ├► MinerUParser.to_markdown(docx, need_ocr=False)
        │     ├► POST /api/v4/file-urls/batch → batch_id + 预签名URL
        │     ├► PUT docx 到预签名URL (无 auth header)
        │     ├► 轮询 GET /api/v4/extract-results/batch/{id} 直到 done
        │     └► 下载 full_zip_url → 解压取 full.md
        ├► sha256(md) → content_hash (对 markdown 取, 不是 docx 字节)
        ├► split_markdown(md) → chunks (注入 doc_id)
        ├► embed(chunks) → vectors            # 易失败步, 放事务外
        ├► pg_vector_store.replace_doc(doc_id, chunks, vectors)  # 单事务 DELETE+INSERT, 失败回滚
        │     └► pg_search BM25 索引随此 DML 自动更新
        └► kb_document.upsert(status=active, version, hash, chunk_count)
  └► 删除传播 (快照 diff) + kb_sync_run.finish   # 无 BM25 手动刷新
```

---

## 6. API 与配置变更

### 6.1 API (`app/api/v1/documents.py` 扩展，沿用 `X-KB-Admin-Token` 鉴权)

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/documents/upload` | 扩展支持 pdf/doc/ppt/图片/html → MinerU；md/txt 直通。MinerU 失败返回 **503**，不入库。 |
| POST | `/api/v1/kb/sources` | 注册飞书源 (body `{space_id, name?}`，写 `kb_source`)。需 admin token。 |
| GET | `/api/v1/kb/sources` | 列出源。 |
| POST | `/api/v1/kb/sources/{id}/preview` | 调 `list_changes` 列出源下文档 (不落库，接入自检)。需 admin token。 |
| POST | `/api/v1/kb/sources/{id}/sync` | 触发同步。**默认后台执行立即返回**；`?wait=true` 内联返回统计。需 admin token。 |
| GET | `/api/v1/kb/sources/{id}/runs` | 最近 `kb_sync_run` 记录 (后台触发后轮询进度)。 |

`document_service.upload_document` 改造: `ALLOWED_EXTENSIONS` 扩为 `{md,markdown,txt}` (直通) +
`{pdf,doc,docx,ppt,pptx,png,jpg,jpeg,bmp,gif,html}` (走 MinerU)；二进制分支先做大小预检 (G4) 再调
`MinerUParser` (`need_ocr` 按扩展名判定，图片/扫描 pdf=true)，失败抛错由路由转 503；直传也分配 `doc_id`。

### 6.2 新增配置项 (`app/config.py`，沿用 pydantic settings 风格)

```python
# —— MinerU 官方 API ——
mineru_api_base: str = "https://mineru.net"
mineru_token: str = ""                  # 必填，空则拒绝一切解析
mineru_model_version: str = "vlm"       # pipeline | vlm | MinerU-HTML
mineru_enable_formula: bool = True
mineru_enable_table: bool = True
mineru_language: str = "ch"
mineru_poll_interval_sec: float = 3.0
mineru_timeout_sec: float = 300.0       # 轮询总超时 (HTTP 请求另设分项超时, 见 mineru.py)
mineru_max_bytes: int = 200 * 1024 * 1024  # 大小预检上限 (G4)

# —— 飞书 ——
feishu_app_id: str = ""
feishu_app_secret: str = ""             # 密钥，走 env，不入库
feishu_api_base: str = "https://open.feishu.cn"
feishu_timeout_sec: float = 30.0
feishu_export_timeout_sec: float = 120.0

# —— 同步调度 ——
kb_sync_enabled: bool = True
kb_sync_interval_sec: int = 1800        # 30min
```

`.env.example` 已补充 `MINERU_TOKEN` / `FEISHU_APP_ID` / `FEISHU_APP_SECRET` /
`KB_SYNC_ENABLED` / `KB_SYNC_INTERVAL_SEC` 等键 (其余有默认值的可不设)。

---

## 7. 失败处理、重试与安全

- **MinerU 失败 = 硬失败**: 直传走 API 返回 503；同步走标记 `kb_document.status=failed` + `last_error`，
  向量库不写入。下一轮 `kb.source.sync` 对 `failed` 文档**无条件重试** (不看版本号)。实现「稍后重试」。
- **幂等 + 原子**: 更新走 `replace_doc` (单事务 DELETE+INSERT，embedding 已预算，失败回滚不丢旧数据)。
- **并发安全**: `kb_sync_run` 重入用 advisory lock (按 source_id)。多副本部署时定时器会各自触发，
  advisory lock 保证同源同一时刻只有一个 sync 真正执行 (其余拿不到锁直接跳过)。
- **飞书 token**: `tenant_access_token` 内存缓存 + 过期前刷新；429 指数退避。
- **密钥管理**: `mineru_token` / `feishu_app_secret` 仅存环境变量；`kb_source.config` 只存非敏感的 space_id/scope。
- **MinerU 数据出境注意**: 文档会上传至 MinerU 云。若后续有敏感库需求，Parser 接口已抽象，可切自托管 (本期不做)。

---

## 8. 分阶段实施计划 (全部完成 ✅)

| 阶段 | 内容 | 状态 / 真实验证 |
|---|---|---|
| **P0 地基** | `kb_chunks` 加 `doc_id` + `delete_by_doc_id` + `replace_doc` 原子替换；存量回填 `local:`+source；建 `kb_source/kb_document/kb_sync_run` 表 | ✅ `58df73d` · 真实 PG: 列/索引/回填(4113 chunk, 空 doc_id=0)/删除均验证 |
| **P1 Parser** | `MinerUParser` (file-urls→PUT→轮询→full.md)；上传扩格式, 二进制走 MinerU, 失败 503 | ✅ `7eeefac` · 真实 MinerU+PDF: 解析(含表格)→检索召回; MinerU 宕→503 |
| **P2 Connector** | `FeishuConnector` (token 缓存 + 整库枚举 + 类型白名单 + export→docx + 下载)；源管理/预览 API | ✅ `0760682` · 真实飞书: 整库枚举/docx 导出/file 下载/token 缓存均验证 |
| **P3 Sync Engine** | `kb_sync_service` 全量+增量+删除传播+版本粗筛/md哈希免重嵌+重试；手动 sync API | ✅ `ecbe540` · 真实飞书库: 注册→同步→检索召回→增量复跑全 skip |
| **P4 调度** | `kb_scheduler` 进程内定时 + lifespan 启停；`kb_sync_run` 审计 | ✅ `1e05b4d` · 调度器自动首轮同步→后续 skip→优雅停止 |
| **review 整改** | MinerU 大文件分项超时；sync 接口后台化 + runs 轮询；补 `tests/` | ✅ `eac0d65` · 25 passed |

**测试** (`tests/`, httpx MockTransport, 无网络/DB, `pytest` 可离线跑):
- `test_mineru_parser.py` — 全流程 (PUT 无 auth/轮询/取 full.md) + failed/无 token/空/超大/无 md zip
- `test_feishu_connector.py` — 分页+递归+类型白名单、docx 导出、file 下载、token 仅换 1 次、缺 space_id
- `test_document_normalize.py` — md/txt 直通、pdf→MinerU、图片 need_ocr、不支持/空/非 UTF8/ParseError 传播
- `test_kb_sync_helpers.py` — `_doc_id`/`_sha256`/`_hash_lock`(int4 范围)/connector 选择
- 运行: `.venv/bin/python -m pytest` (依赖见 `requirements-dev.txt`)

---

## 9. 风险与待确认

| 项 | 说明 | 应对 |
|---|---|---|
| 飞书应用授权 (已解决) | 整库枚举要求应用是**知识库 (space) 成员**, 仅文档级协作者会返回 `131006`。应用须先开启「机器人」能力并发布、可用范围含操作者, 再在知识库设置→成员里加为成员 | 本项目租户最终验证可行 (见操作步骤 §11) |
| 飞书月调用量上限 | 自建应用有月度额度 | 靠 token 缓存 + 版本粗筛减少调用；量小本期无忧 |
| MinerU 配额/计费 | 当前免费但官方写「暂无计费计划」，可能变 | 监控 429；接口已抽象便于替换 |
| MinerU 单文件页数 | docs 写 200 页 / SDK 写 600 页，口径不一 | 以控制台实测为准；超限文档按页拆分 (后续) |
| MinerU token 申请 | 需官方审批，可能等几天 | 提前申请 |
| 原生 docx 经 export→MinerU 的保真 | 比 blocks 直转略损 | 可接受；blocks 直转列为后续优化 |

---

## 10. 涉及文件清单 (实际)

| 动作 | 文件 |
|---|---|
| 改 | `app/core/pg_vector_store.py` (doc_id 列/索引/回填 + delete_by_doc_id + replace_doc) |
| 改 | `app/services/document_service.py` (扩格式 + `normalize_to_markdown` 归一化, 上传/同步共用) |
| 改 | `app/api/v1/documents.py` (上传扩格式 + 503) |
| 改 | `app/main.py` (启动建表 + 启停调度器) |
| 改 | `app/config.py` / `.env.example` (MinerU / 飞书 / 同步调度 配置) |
| 改 | `app/exceptions.py` (`DocumentParseError` 503) |
| 新增 | `app/core/parsers/` (`base.py` 协议+ParseError, `mineru.py` 客户端, `__init__.py`) |
| 新增 | `app/core/connectors/` (`base.py` DocRef/KbSource/协议, `feishu.py`, `__init__.py`) |
| 新增 | `app/core/kb_sync_schema.py` (kb_source/kb_document/kb_sync_run 三表) |
| 新增 | `app/services/kb_source_service.py` (源 CRUD + preview) |
| 新增 | `app/services/kb_sync_service.py` (同步引擎 + launch_sync/recent_runs) |
| 新增 | `app/services/kb_scheduler.py` (进程内定时调度) |
| 新增 | `app/api/v1/kb_sources.py` (源管理 / 预览 / 同步 / runs) |
| 新增 | `tests/` (4 个测试 + conftest), `pytest.ini`, `requirements-dev.txt` |
| 复用不改 | `app/core/splitter.py`, `app/rag/*`, `app/core/hybrid_retriever.py` (检索链路) |

---

## 11. 使用说明 (Quickstart)

### 11.1 配置 (`.env`)
```
MINERU_TOKEN=<mineru.net 申请>
FEISHU_APP_ID=<飞书自建应用>
FEISHU_APP_SECRET=<密钥>
KB_SYNC_ENABLED=true            # 定时同步开关
KB_SYNC_INTERVAL_SEC=1800       # 间隔
KB_ADMIN_TOKEN=<写操作鉴权>      # 调 sources/upload 写接口需带 X-KB-Admin-Token
```

### 11.2 飞书侧一次性准备
1. 开放平台后台: 应用开通权限 `wiki:wiki` / `docs:doc` / `drive:drive` / `drive:file`；
   **添加「机器人」能力**；创建版本并**发布 + 管理员审核通过**；「可用范围」含操作者。
2. 目标 Wiki **知识库设置 → 成员**: 搜索应用名加为成员 (可阅读)。⚠️ 单篇文档加协作者不够。
3. 取 `space_id`: 调 `GET /open-apis/wiki/v2/spaces` 列出可见知识库, 或从 wiki 链接经
   `wiki/v2/spaces/get_node?token=<链接里的token>` 反查。

### 11.3 接入流程 (HTTP, 均带 `X-KB-Admin-Token`)
```
# 1. 注册数据源
POST /api/v1/kb/sources           {"space_id":"<id>","name":"运维知识库"}
# 2. 预览自检 (不落库, 验证授权/类型白名单)
POST /api/v1/kb/sources/{id}/preview
# 3. 手动同步 (后台执行)
POST /api/v1/kb/sources/{id}/sync          # 立即返回; ?wait=true 则内联返回统计
# 4. 查进度
GET  /api/v1/kb/sources/{id}/runs
```
注册后, 定时调度器会按 `KB_SYNC_INTERVAL_SEC` 自动增量同步, 无需再手动触发。

### 11.4 单文件直传 (无需数据源)
```
POST /api/v1/documents/upload     # multipart 文件; md/txt 直通, pdf/doc/ppt/图片/html 经 MinerU
```
