"""知识库多源同步的状态表 schema (kb_source / kb_document / kb_sync_run).

为什么单独一个模块?
  - 这三张表承载"多源摄取 + 增量同步"的状态, 与向量库 (kb_chunks) 解耦:
    kb_chunks 存"切好的向量块", 这里存"文档级"的同步元数据 (版本/哈希/状态)。
  - 复用 app.db.postgres 的同一个 asyncpg 连接池, 不引入第二套栈。
  - 建表沿用 pg_vector_store / incident schema 的 advisory-lock 串行化套路,
    避免多 worker 并发 DDL 死锁; 只影响启动, 不碰请求路径。

表职责:
  - kb_source:   一行一个外部数据源实例 (本期只飞书)。
  - kb_document: 一行一篇外部文档, 是增量同步的核心状态 (id 即 kb_chunks.doc_id)。
                 source_version 做版本粗筛, content_hash (对解析后的 markdown 取) 做免重嵌。
  - kb_sync_run: 一行一次同步运行, 审计用。

密钥 (飞书 app_secret / MinerU token) 走配置/环境变量, 不入库;
kb_source.config 只放非敏感的 space_id / scope 等。
"""

from __future__ import annotations

from loguru import logger

from app.db.postgres import get_pool

# 与 incident (990001) / pgvector (990002) 区分, 各自串行化 DDL
_SCHEMA_LOCK_KEY = 990003

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kb_source (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    name         TEXT NOT NULL,
    config       JSONB NOT NULL DEFAULT '{}'::jsonb,
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    sync_cursor  TEXT,
    last_sync_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS kb_document (
    id             TEXT PRIMARY KEY,
    source_id      TEXT NOT NULL REFERENCES kb_source(id),
    external_id    TEXT NOT NULL,
    external_type  TEXT NOT NULL DEFAULT '',
    title          TEXT NOT NULL DEFAULT '',
    uri            TEXT NOT NULL DEFAULT '',
    source_version TEXT NOT NULL DEFAULT '',
    content_hash   TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'pending',
    chunk_count    INTEGER NOT NULL DEFAULT 0,
    last_error     TEXT NOT NULL DEFAULT '',
    last_synced_at TIMESTAMPTZ,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    deleted_at     TIMESTAMPTZ,
    UNIQUE (source_id, external_id)
);
CREATE INDEX IF NOT EXISTS idx_kb_document_source ON kb_document(source_id, status);

CREATE TABLE IF NOT EXISTS kb_sync_run (
    id          BIGSERIAL PRIMARY KEY,
    source_id   TEXT NOT NULL REFERENCES kb_source(id),
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ,
    status      TEXT NOT NULL DEFAULT 'running',
    stats       JSONB NOT NULL DEFAULT '{}'::jsonb,
    error       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_kb_sync_run_source ON kb_sync_run(source_id, started_at DESC);
"""


async def init_kb_sync_schema() -> None:
    """建 kb_source / kb_document / kb_sync_run 三张表 (幂等)。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _SCHEMA_LOCK_KEY)
        try:
            await conn.execute(_SCHEMA_SQL)
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _SCHEMA_LOCK_KEY)
    logger.info("[kb_sync] schema ready: kb_source / kb_document / kb_sync_run")
