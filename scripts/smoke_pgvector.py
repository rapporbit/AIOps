"""pgvector 迁移冒烟验证: 不依赖 embedding API, 只需要一个跑着的 pgvector Postgres.

验证 app/core/pg_vector_store.py 的全部 DB 侧代码路径:
  CREATE EXTENSION vector / 建表 / HNSW 索引 / ::vector 字面量 / <=> 距离 /
  SET LOCAL hnsw.ef_search / source 过滤 / list_sources / bm25_search (pg_search) /
  delete_by_source / is_ready。

为什么能免 embedding API:
  把 embedding 换成"由文本 hash 决定的确定性假向量"——同样的文本永远得到同样的向量,
  于是用某条文档的原文当 query, 它的距离必然 ~0, top-1 可断言。这样就能纯靠 DB 验证
  我们写的 SQL 是否正确, 而不用真的调 DashScope/Ollama。

用法:
  1) 先起一个带 pgvector 的库:  docker compose up -d postgres   (镜像已是 pgvector/pgvector:pg16)
  2) python scripts/smoke_pgvector.py
  退出码: 0=全过, 2=DB 连不上, 1=断言失败
"""

from __future__ import annotations

import asyncio
import hashlib
import random
import sys
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from langchain_core.documents import Document  # noqa: E402
from loguru import logger  # noqa: E402

SMOKE_SOURCE = "__smoke_pgvector__"  # 独立 source, 不碰真实数据


class _FakeEmbeddings:
    """确定性假 embedding: 文本 hash 播种, 生成单位长度向量 (维度取 settings)。"""

    def __init__(self, dim: int) -> None:
        self.dim = dim

    def _vec(self, text: str) -> List[float]:
        seed = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16) % (2**32)
        rng = random.Random(seed)
        v = [rng.gauss(0, 1) for _ in range(self.dim)]
        norm = sum(x * x for x in v) ** 0.5 or 1.0
        return [x / norm for x in v]

    def embed_documents(self, texts: List[str]) -> List[List[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._vec(text)


def _docs() -> List[Document]:
    payloads = [
        ("Redis 内存打满 OOM 的处理步骤", "redis 章节"),
        ("MySQL 主从同步延迟排查", "mysql 章节"),
        ("Kubernetes Pod CrashLoopBackOff 定位", "k8s 章节"),
    ]
    out: List[Document] = []
    for i, (content, chapter) in enumerate(payloads):
        out.append(
            Document(
                page_content=content,
                metadata={
                    "source": SMOKE_SOURCE,
                    "chapter": chapter,
                    "parent_id": f"p{i}",
                    "parent_content": f"[父块] {content} —— 完整段落正文",
                    "chunk_index": i,
                },
            )
        )
    return out


def _check(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)
    logger.info(f"  ✓ {msg}")


async def _run() -> None:
    from app.core import pg_vector_store
    from app.db.postgres import close_postgres

    # 注入假 embedding (pg_vector_store 是 `from app.core.embedding import get_embeddings`,
    # 所以要替换它命名空间里的这个名字)
    dim = pg_vector_store._embedding_dim()
    pg_vector_store.get_embeddings = lambda: _FakeEmbeddings(dim)  # type: ignore[attr-defined]

    docs = _docs()
    try:
        logger.info("1) init_vector_schema (CREATE EXTENSION + 表 + HNSW 索引)")
        await pg_vector_store.init_vector_schema()
        _check(await pg_vector_store.is_ready(), "is_ready() == True")

        # 干净起步: 先清掉可能残留的本测试数据
        await pg_vector_store.delete_by_source(SMOKE_SOURCE)

        logger.info("2) add_documents (向量化 + 写入)")
        n = await pg_vector_store.add_documents(docs)
        _check(n == 3, f"add_documents 返回 3 (实际 {n})")

        logger.info("3) list_sources 聚合计数")
        counts = dict(await pg_vector_store.list_sources())
        _check(counts.get(SMOKE_SOURCE) == 3, f"{SMOKE_SOURCE} 计数为 3 (实际 {counts.get(SMOKE_SOURCE)})")

        logger.info("4) similarity_search + source 过滤 + <=> 排序 + SET LOCAL ef_search")
        target = docs[1].page_content  # 用原文当 query, 距离应 ~0
        hits = await pg_vector_store.similarity_search(target, k=3, source=SMOKE_SOURCE)
        _check(len(hits) == 3, f"返回 3 条 (实际 {len(hits)})")
        _check(hits[0].page_content == target, "top-1 命中与 query 同文的那条 (向量/距离正确)")
        top_score = hits[0].metadata.get("score")
        _check(top_score is not None and top_score > 0.99, f"top-1 score≈1.0 (实际 {top_score})")
        _check(hits[0].metadata.get("parent_content", "").startswith("[父块]"), "parent_content 正确回传")

        logger.info("5) bm25_search (ParadeDB pg_search 词法检索 + source 过滤)")
        bm = await pg_vector_store.bm25_search("Redis OOM", k=5, source=SMOKE_SOURCE)
        _check(len(bm) >= 1, f"bm25_search 至少命中 1 条 (实际 {len(bm)})")
        _check(
            any(d.page_content == docs[0].page_content for d in bm),
            "BM25 命中含 'Redis ... OOM' 的那条 (词法匹配正确)",
        )
        _check(bm[0].metadata.get("bm25_score") is not None, "bm25_score 已回传")

        logger.info("6) delete_by_source 清理 + 计数")
        deleted = await pg_vector_store.delete_by_source(SMOKE_SOURCE)
        _check(deleted == 3, f"删除返回 3 (实际 {deleted})")
        counts2 = dict(await pg_vector_store.list_sources())
        _check(SMOKE_SOURCE not in counts2, "删除后 source 不再出现")

        logger.info("✅ 全部通过: pgvector DB 侧代码路径正确")
    finally:
        # 兜底清理, 避免留测试行
        try:
            await pg_vector_store.delete_by_source(SMOKE_SOURCE)
        except Exception:
            pass
        await close_postgres()


def main() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass

    try:
        asyncio.run(_run())
    except AssertionError as e:
        logger.error(f"❌ 断言失败: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(
            f"❌ 验证未跑通 (多半是 DB 连不上 / pgvector 扩展缺失): {type(e).__name__}: {e}\n"
            f"   请确认: docker compose up -d postgres (pgvector 镜像), 或本地 PG 已装 vector 扩展。"
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
