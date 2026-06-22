"""知识库同步引擎: 从数据源增量同步到 pgvector.

单次 sync(source) 流程 (设计 §4.3):
  1. 建 kb_sync_run (running)
  2. connector.list_changes(source) 拿远端清单
  3. 逐篇:
     - 版本粗筛: source_version 没变且上次 active → skip (免 fetch/解析/嵌入)
     - failed 文档无视版本强制重试
     - fetch_blob → normalize_to_markdown (md/txt 直通, 其他过 MinerU)
     - markdown 哈希免重嵌: 正文没变只更版本号
     - replace_doc 幂等替换 (embedding 事务外算, DELETE+INSERT 单事务)
     - 成功标 active, 失败标 failed (不写向量, 旧数据保留, 下轮重试)
  4. 删除传播 (仅整库枚举模式可信): 远端消失的文档清向量 + 软删
  5. 收尾 kb_sync_run

并发安全: 同一 source 用 advisory lock 串行, 避免定时器与手动触发重入。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

from loguru import logger

from app.core import pg_vector_store
from app.core.connectors import KbSource, feishu_connector
from app.core.connectors.base import DocRef, SourceConnector
from app.core.splitter import split_markdown
from app.db.postgres import get_pool
from app.services.document_service import normalize_to_markdown

# advisory lock 命名空间 (与 schema lock 区分)，按 source_id 哈希成第二个 key
_SYNC_LOCK_NS = 990010


def _connector_for(source: KbSource) -> SourceConnector:
    if source.type == "feishu":
        return feishu_connector
    raise ValueError(f"暂不支持的数据源类型: {source.type}")


def _doc_id(source: KbSource, ref: DocRef) -> str:
    """一篇文档的稳定主键 = kb_document.id = kb_chunks.doc_id。"""
    return f"{source.type}:{ref.external_id}"


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ============================================================
# kb_document / kb_sync_run 仓储 (asyncpg)
# ============================================================
async def _get_document(conn, doc_id: str) -> Optional[Dict[str, Any]]:
    r = await conn.fetchrow(
        "SELECT id, source_version, content_hash, status, chunk_count "
        "FROM kb_document WHERE id = $1",
        doc_id,
    )
    return dict(r) if r else None


async def _upsert_document(conn, *, doc_id, source_id, ref: DocRef, status, content_hash,
                           chunk_count, last_error) -> None:
    await conn.execute(
        """
        INSERT INTO kb_document
            (id, source_id, external_id, external_type, title, uri,
             source_version, content_hash, status, chunk_count, last_error,
             last_synced_at, updated_at, deleted_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11, now(), now(), NULL)
        ON CONFLICT (id) DO UPDATE SET
            external_type = EXCLUDED.external_type,
            title = EXCLUDED.title,
            uri = EXCLUDED.uri,
            source_version = EXCLUDED.source_version,
            content_hash = EXCLUDED.content_hash,
            status = EXCLUDED.status,
            chunk_count = EXCLUDED.chunk_count,
            last_error = EXCLUDED.last_error,
            last_synced_at = now(),
            updated_at = now(),
            deleted_at = NULL
        """,
        doc_id, source_id, ref.external_id, ref.external_type, ref.title, ref.uri,
        ref.version, content_hash, status, chunk_count, last_error,
    )


async def _update_version(conn, doc_id: str, version: str) -> None:
    await conn.execute(
        "UPDATE kb_document SET source_version=$2, last_synced_at=now(), updated_at=now() "
        "WHERE id=$1",
        doc_id, version,
    )


async def _active_documents(conn, source_id: str) -> List[Dict[str, Any]]:
    rows = await conn.fetch(
        "SELECT id, external_id FROM kb_document WHERE source_id=$1 AND status='active'",
        source_id,
    )
    return [dict(r) for r in rows]


async def _soft_delete(conn, doc_id: str) -> None:
    await conn.execute(
        "UPDATE kb_document SET status='deleted', deleted_at=now(), updated_at=now() "
        "WHERE id=$1",
        doc_id,
    )


async def _create_run(conn, source_id: str) -> int:
    return int(await conn.fetchval(
        "INSERT INTO kb_sync_run (source_id, status) VALUES ($1, 'running') RETURNING id",
        source_id,
    ))


async def _finish_run(conn, run_id: int, status: str, stats: dict, error: str = "") -> None:
    await conn.execute(
        "UPDATE kb_sync_run SET status=$2, stats=$3::jsonb, error=$4, finished_at=now() "
        "WHERE id=$1",
        run_id, status, json.dumps(stats, ensure_ascii=False), error,
    )


# ============================================================
# 同步主流程
# ============================================================
async def sync_source(source: KbSource) -> Dict[str, Any]:
    """同步单个数据源, 返回统计。advisory lock 保证同源串行。"""
    connector = _connector_for(source)
    # 整库枚举才是"完整快照", 才能据此做删除传播; 指定文档模式不做删除传播。
    is_full = bool(source.config.get("space_id")) and not source.config.get("node_tokens")

    pool = await get_pool()
    lock_key2 = _hash_lock(source.id)
    async with pool.acquire() as lock_conn:
        got = await lock_conn.fetchval(
            "SELECT pg_try_advisory_lock($1, $2)", _SYNC_LOCK_NS, lock_key2
        )
        if not got:
            logger.warning(f"[kb_sync] source={source.id} 已有同步在跑, 跳过")
            return {"skipped_reason": "locked"}
        try:
            return await _do_sync(pool, connector, source, is_full)
        finally:
            await lock_conn.execute(
                "SELECT pg_advisory_unlock($1, $2)", _SYNC_LOCK_NS, lock_key2
            )


def _hash_lock(s: str) -> int:
    """把 source_id 稳定哈希成 int4 范围, 作为 advisory lock 的第二个 key。"""
    h = int(hashlib.sha256(s.encode()).hexdigest()[:8], 16)
    return h - (1 << 31)  # 落到 int4 [-2^31, 2^31)


async def _do_sync(pool, connector: SourceConnector, source: KbSource, is_full: bool) -> Dict[str, Any]:
    stats = {"scanned": 0, "added": 0, "updated": 0, "skipped": 0, "deleted": 0, "failed": 0}
    async with pool.acquire() as conn:
        run_id = await _create_run(conn, source.id)

    try:
        refs = await connector.list_changes(source)
    except Exception as e:
        logger.exception(f"[kb_sync] list_changes 失败 source={source.id}")
        async with pool.acquire() as conn:
            await _finish_run(conn, run_id, "failed", stats, str(e))
        raise

    stats["scanned"] = len(refs)
    remote_ids = {r.external_id for r in refs}

    for ref in refs:
        doc_id = _doc_id(source, ref)
        try:
            async with pool.acquire() as conn:
                doc = await _get_document(conn, doc_id)
            # 版本粗筛: 上次成功且版本没变 → 跳过
            if doc and doc["status"] == "active" and doc["source_version"] == ref.version:
                stats["skipped"] += 1
                continue
            existed = doc is not None

            blob, name = await connector.fetch_blob(ref)
            md = await normalize_to_markdown(blob, name, need_ocr=ref.need_ocr)
            h = _sha256(md)

            # markdown 哈希免重嵌: 正文没变, 只更版本号
            if doc and doc["status"] == "active" and doc["content_hash"] == h:
                async with pool.acquire() as conn:
                    await _update_version(conn, doc_id, ref.version)
                stats["skipped"] += 1
                continue

            chunks = split_markdown(md, source=ref.title)
            if chunks:
                await pg_vector_store.replace_doc(doc_id, chunks)
            else:
                await pg_vector_store.delete_by_doc_id(doc_id)  # 空文档: 清旧

            async with pool.acquire() as conn:
                await _upsert_document(
                    conn, doc_id=doc_id, source_id=source.id, ref=ref,
                    status="active", content_hash=h, chunk_count=len(chunks), last_error="",
                )
            stats["updated" if existed else "added"] += 1
            logger.info(f"[kb_sync] {'更新' if existed else '新增'} {ref.title!r} ({len(chunks)} chunk)")
        except Exception as e:
            logger.warning(f"[kb_sync] 文档失败 {ref.title!r}: {type(e).__name__}: {e}")
            async with pool.acquire() as conn:
                await _upsert_document(
                    conn, doc_id=doc_id, source_id=source.id, ref=ref,
                    status="failed", content_hash="", chunk_count=0, last_error=str(e)[:500],
                )
            stats["failed"] += 1

    # 删除传播 (仅整库模式可信)
    if is_full:
        async with pool.acquire() as conn:
            for d in await _active_documents(conn, source.id):
                if d["external_id"] not in remote_ids:
                    await pg_vector_store.delete_by_doc_id(d["id"])
                    await _soft_delete(conn, d["id"])
                    stats["deleted"] += 1
                    logger.info(f"[kb_sync] 删除传播 doc_id={d['id']}")

    async with pool.acquire() as conn:
        await _finish_run(conn, run_id, "success", stats)
        await conn.execute(
            "UPDATE kb_source SET last_sync_at=now() WHERE id=$1", source.id
        )
    logger.info(f"[kb_sync] source={source.id} 完成: {stats}")
    return stats
