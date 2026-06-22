"""pgvector 向量库 (替换原 Milvus).

为什么是这一层?
  - 项目本就在跑 Postgres (事故台账), 向量直接并进同一实例, 零新增基础设施,
    省掉 Milvus+etcd+MinIO 三容器。规模 (现 ~2.5k chunk, 接飞书后 10万~30万)
    远在 pgvector 舒适区, 用不上专用向量库。
  - 复用 app.db.postgres 的同一个 asyncpg 连接池, 不引入 SQLAlchemy/psycopg 第二套栈。

设计要点:
  - 表 kb_chunks: 一行一个 child 小块, embedding 为 pgvector 的 vector 类型,
    HNSW + cosine 索引 (vector_cosine_ops)。
  - 向量以文本字面量 '[...]' 传参并 ::vector 强转, 避免给连接池挂 per-conn 类型注册,
    也省掉额外的 pgvector python 包。规模下这点开销可忽略。
  - embedding 计算是同步 HTTP, 用 asyncio.to_thread 包一层, 不阻塞事件循环。
  - 字段约定沿用原 Milvus: content / source / chapter / parent_id / parent_content /
    chunk_index, 另存完整 metadata 到 JSONB, 检索/去重逻辑 (rag/retrieval.py) 无需改动。
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, List, Optional, Tuple

from langchain_core.documents import Document
from loguru import logger

from app.config import settings
from app.core.embedding import get_embeddings
from app.db.postgres import get_pool

_SCHEMA_LOCK_KEY = 990002  # 与 incident schema (990001) 区分, 各自串行化 DDL

# pg_search 分词器白名单: tokenizer 名要拼进 DDL (::pdb.<name>), 不能直接来自配置字符串,
# 用白名单防注入。值见 ParadeDB tokenizers 文档。
_BM25_TOKENIZERS = {"chinese_compatible", "chinese_lindera", "icu", "jieba", "default"}


def _bm25_tokenizer() -> str:
    tok = (settings.kb_bm25_tokenizer or "chinese_compatible").strip()
    return tok if tok in _BM25_TOKENIZERS else "chinese_compatible"


def _embedding_dim() -> int:
    """当前 embedding provider 对应的向量维度 (建表时定死)。"""
    if settings.embedding_provider == "ollama":
        return int(settings.ollama_embedding_dim)
    return int(settings.dashscope_embedding_dim)


def _vec_literal(vector: List[float]) -> str:
    """把向量转成 pgvector 文本字面量: [0.1,0.2,...] (无空格)。"""
    return "[" + ",".join(repr(float(x)) for x in vector) + "]"


def _table() -> str:
    return settings.pgvector_table


async def init_vector_schema() -> None:
    """建库: vector 扩展 + kb_chunks 表 + HNSW 索引 (幂等)。

    用 advisory lock 串行化 DDL, 避免多 worker 并发 CREATE INDEX 死锁
    (与 incident schema 同一套路, 只影响启动)。
    """
    dim = _embedding_dim()
    table = _table()
    m = int(settings.pgvector_hnsw_m)
    ef_construction = int(settings.pgvector_hnsw_ef_construction)

    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute("SELECT pg_advisory_lock($1)", _SCHEMA_LOCK_KEY)
        try:
            try:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
            except Exception as exc:  # 权限不足等
                raise RuntimeError(
                    "无法启用 pgvector 扩展 (CREATE EXTENSION vector 失败)。"
                    "请用支持 pgvector 的镜像 (如 pgvector/pgvector:pg16), "
                    f"或让 DBA 预先 CREATE EXTENSION vector。原始错误: {exc}"
                ) from exc

            await conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    id BIGSERIAL PRIMARY KEY,
                    content TEXT NOT NULL,
                    embedding vector({dim}) NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    chapter TEXT NOT NULL DEFAULT '',
                    parent_id TEXT NOT NULL DEFAULT '',
                    parent_content TEXT NOT NULL DEFAULT '',
                    chunk_index INTEGER NOT NULL DEFAULT 0,
                    metadata JSONB NOT NULL DEFAULT '{{}}'::jsonb,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
            # 按 source 删除/聚合用
            await conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_source ON {table}(source)"
            )
            # 向量近邻索引: HNSW + cosine
            await conn.execute(
                f"""
                CREATE INDEX IF NOT EXISTS idx_{table}_embedding ON {table}
                USING hnsw (embedding vector_cosine_ops)
                WITH (m = {m}, ef_construction = {ef_construction})
                """
            )

            # 词法检索: ParadeDB pg_search BM25 索引 (随写入自动维护, 无内存截断)。
            # 失败不致命: 没有 pg_search (非 ParadeDB 镜像) 时降级为纯向量, 只 warning。
            tokenizer = _bm25_tokenizer()
            try:
                await conn.execute("CREATE EXTENSION IF NOT EXISTS pg_search")
                await conn.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS idx_{table}_bm25 ON {table}
                    USING bm25 (id, (content::pdb.{tokenizer}), source)
                    WITH (key_field = 'id')
                    """
                )
            except Exception as exc:
                logger.warning(
                    f"[pgvector] pg_search BM25 索引未建 (混合检索将降级为纯向量): "
                    f"{type(exc).__name__}: {exc}。如需 BM25, 请用 ParadeDB 镜像 "
                    f"(paradedb/paradedb) 并把 pg_search 加入 shared_preload_libraries。"
                )
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _SCHEMA_LOCK_KEY)
    logger.info(f"[pgvector] schema ready: table={table}, dim={dim}, bm25={_bm25_tokenizer()}")


async def add_documents(docs: List[Document]) -> int:
    """向量化并写入 child chunks。返回写入条数。"""
    if not docs:
        return 0

    contents = [d.page_content for d in docs]
    emb = get_embeddings()
    # embed_documents 是同步 HTTP, 丢到线程池避免阻塞事件循环
    vectors = await asyncio.to_thread(emb.embed_documents, contents)
    if len(vectors) != len(docs):
        raise RuntimeError(
            f"embedding 数量不匹配: docs={len(docs)}, vectors={len(vectors)}"
        )

    rows = []
    for doc, vec in zip(docs, vectors):
        meta = doc.metadata or {}
        rows.append(
            (
                doc.page_content,
                _vec_literal(vec),
                str(meta.get("source") or ""),
                str(meta.get("chapter") or ""),
                str(meta.get("parent_id") or ""),
                str(meta.get("parent_content") or ""),
                int(meta.get("chunk_index") or 0),
                json.dumps(meta, ensure_ascii=False),
            )
        )

    table = _table()
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.executemany(
            f"""
            INSERT INTO {table}
                (content, embedding, source, chapter, parent_id,
                 parent_content, chunk_index, metadata)
            VALUES ($1, $2::vector, $3, $4, $5, $6, $7, $8::jsonb)
            """,
            rows,
        )
    return len(rows)


async def similarity_search(
    query: str,
    k: Optional[int] = None,
    *,
    source: Optional[str] = None,
) -> List[Document]:
    """纯向量检索 (cosine top-k)。失败返回 []。

    Args:
        query:  查询文本
        k:      返回条数 (None = settings.rag_top_k)
        source: 可选, 按 source 字段过滤 (元数据过滤)
    """
    k = int(k or settings.rag_top_k)
    if k <= 0:
        return []

    emb = get_embeddings()
    try:
        qvec = await asyncio.to_thread(emb.embed_query, query)
    except Exception as e:
        logger.warning(f"[pgvector] query embedding 失败 (返回空): {type(e).__name__}: {e}")
        return []

    table = _table()
    ef_search = max(int(k), int(settings.pgvector_hnsw_ef_search or 128))
    where = ""
    params: List[Any] = [_vec_literal(qvec)]
    if source:
        params.append(source)
        where = f"WHERE source = ${len(params)}"
    params.append(k)
    limit_pos = len(params)

    sql = f"""
        SELECT content, source, chapter, parent_id, parent_content,
               chunk_index, metadata,
               embedding <=> $1::vector AS distance
        FROM {table}
        {where}
        ORDER BY embedding <=> $1::vector
        LIMIT ${limit_pos}
    """

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            # HNSW 查询期 ef_search: 必须在事务内用 SET LOCAL, 否则影响整连接
            async with conn.transaction():
                await conn.execute(f"SET LOCAL hnsw.ef_search = {int(ef_search)}")
                records = await conn.fetch(sql, *params)
    except Exception as e:
        # 表不存在 / PG 不可用 / 维度不匹配 等, 与原 Milvus 兜底语义一致
        logger.warning(f"[pgvector] similarity_search 失败 (返回空): {type(e).__name__}: {e}")
        return []

    return [_record_to_document(r) for r in records]


async def bm25_search(
    query: str,
    k: Optional[int] = None,
    *,
    source: Optional[str] = None,
) -> List[Document]:
    """ParadeDB pg_search BM25 词法检索, 返回按相关性降序的 top-k。失败返回 []。

    取代原"全量拉进内存建 rank_bm25"的做法: 索引由 pg_search 随写入自动维护,
    无内存占用、无截断上限、多进程共享同一份。

    用 paradedb.match('content', $1) 构造查询 (而非 content @@@ '<原始串>'),
    这样 query 里的 :, +, ", - 等不会被当成 pg_search 查询 DSL 误解析。
    """
    k = int(k or settings.rag_top_k)
    if k <= 0 or not (query or "").strip():
        return []

    table = _table()
    params: List[Any] = [query]
    where_extra = ""
    if source:
        params.append(source)
        where_extra = f"AND source = ${len(params)}"
    params.append(k)
    limit_pos = len(params)

    sql = f"""
        SELECT content, source, chapter, parent_id, parent_content,
               chunk_index, metadata, paradedb.score(id) AS bm25_score
        FROM {table}
        WHERE id @@@ paradedb.match('content', $1)
        {where_extra}
        ORDER BY bm25_score DESC
        LIMIT ${limit_pos}
    """

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch(sql, *params)
    except Exception as e:
        # 没建 BM25 索引 / 非 ParadeDB 等: 降级为空, 让混合检索回退到纯向量
        logger.warning(f"[pgvector] bm25_search 失败 (降级为纯向量): {type(e).__name__}: {e}")
        return []

    docs: List[Document] = []
    for r in records:
        content = r["content"] or ""
        if not content:
            continue
        score = r["bm25_score"]
        try:
            score_val = float(score) if score is not None else None
        except (TypeError, ValueError):
            score_val = None
        docs.append(
            Document(
                page_content=content,
                metadata={
                    "source": r["source"] or "未知",
                    "chapter": r["chapter"] or "",
                    "parent_id": r["parent_id"] or "",
                    "parent_content": r["parent_content"] or "",
                    "chunk_index": r["chunk_index"],
                    "bm25_score": score_val,
                },
            )
        )
    return docs


async def list_sources() -> List[Tuple[str, int]]:
    """按 source 聚合, 返回 [(source, chunk_count), ...] (按 source 排序)。"""
    table = _table()
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            records = await conn.fetch(
                f"""
                SELECT source, count(*) AS cnt
                FROM {table}
                GROUP BY source
                ORDER BY source
                """
            )
    except Exception as e:
        logger.warning(f"[pgvector] list_sources 失败: {type(e).__name__}: {e}")
        return []
    return [(r["source"] or "unknown", int(r["cnt"])) for r in records]


async def delete_by_source(source: str) -> int:
    """按 source 删除所有相关 chunk, 返回删除条数。"""
    table = _table()
    pool = await get_pool()
    async with pool.acquire() as conn:
        status = await conn.execute(f"DELETE FROM {table} WHERE source = $1", source)
    # asyncpg 返回 "DELETE <n>"
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


async def is_ready() -> bool:
    """就绪检查: PG 可达且 kb_chunks 表存在。"""
    table = _table()
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            reg = await conn.fetchval("SELECT to_regclass($1)", f"public.{table}")
        return reg is not None
    except Exception as e:
        logger.warning(f"[pgvector] is_ready 检查失败: {type(e).__name__}: {e}")
        return False


async def count() -> int:
    """当前 chunk 总数 (调试/统计用)。"""
    table = _table()
    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            return int(await conn.fetchval(f"SELECT count(*) FROM {table}") or 0)
    except Exception:
        return 0


def _record_to_document(r: Any) -> Document:
    """asyncpg Record → LangChain Document, 把 distance 放进 metadata.score/distance。"""
    distance = r["distance"]
    try:
        distance_val = float(distance) if distance is not None else None
    except (TypeError, ValueError):
        distance_val = None
    meta = {
        "source": r["source"] or "未知",
        "chapter": r["chapter"] or "",
        "parent_id": r["parent_id"] or "",
        "parent_content": r["parent_content"] or "",
        "chunk_index": r["chunk_index"],
        "distance": distance_val,
        # cosine 距离 <=> 落在 [0,2], 相似度 = 1 - distance, 方便上层展示
        "score": (1.0 - distance_val) if distance_val is not None else None,
    }
    return Document(page_content=r["content"] or "", metadata=meta)
