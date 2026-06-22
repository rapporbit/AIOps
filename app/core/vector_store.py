"""向量检索编排层: Vector → [Hybrid] → [Rerank].

底层向量库操作在 app/core/pg_vector_store.py (pgvector, 复用 Postgres 池)。
本模块只负责高级检索流水线的编排, 与具体向量库解耦:
  用户 query → Vector top-N → [Hybrid 融合 BM25 top-N] → [Reranker top-K] → 返回
  每一层都可通过 settings 开关; 任一环节失败都自动降级到上一层结果.

历史: 原先是 langchain_milvus.Milvus 封装, 已于向量库迁移 (Milvus → pgvector) 移除。
"""

from typing import List, Optional

from langchain_core.documents import Document
from loguru import logger

from app.config import settings
from app.core import pg_vector_store


async def safe_similarity_search(
    query: str,
    k: Optional[int] = None,
    filter: Optional[str] = None,
) -> List[Document]:
    """纯向量检索, 对表不存在 / PG 不可用等异常做兜底 (返回 [])。

    业务侧 (knowledge_tool) 经 advanced_search 间接调用, 不用关心底层是否就绪。

    Args:
        query:  查询文本
        k:      返回 top-k (None 用 settings.rag_top_k)
        filter: 可选, 按 source 字段做元数据过滤 (原 Milvus expr 已简化为 source 过滤)
    """
    return await pg_vector_store.similarity_search(query, k=k, source=filter or None)


async def advanced_search(
    query: str,
    k: Optional[int] = None,
    *,
    filter: Optional[str] = None,
    retrieve_k: Optional[int] = None,
    use_hybrid: Optional[bool] = None,
    use_rerank: Optional[bool] = None,
) -> List[Document]:
    """高级检索流水线: Vector → [Hybrid] → [Rerank] → 返回 top-k.

    流水线:
      1) 向量检索粗排:     top = rag_retrieve_k (比如 20)
      2) Hybrid 融合:      与 BM25 的 top-N RRF 融合, 取前 rag_retrieve_k
      3) Rerank 精排:      交给 reranker 取 top-k (默认 3)
      任一环节故障都自动降级到上一层结果.

    Args:
        query:       查询文本
        k:           最终返回的 top-k (None = settings.rag_top_k)
        filter:      source 过滤, 透传给向量检索
        retrieve_k:  送入 hybrid/rerank 前的候选数 (None = settings.rag_retrieve_k)
        use_hybrid:  是否做 Hybrid (None = settings.rag_hybrid_enabled)
        use_rerank:  是否做 Rerank (None = settings.rag_rerank_enabled)

    Returns:
        List[Document]: 最终 top-k, 不抛异常.
    """
    # 延迟导入避免循环依赖
    from app.core.hybrid_retriever import _bm25_index, hybrid_search, refresh_bm25_index
    from app.core.reranker import rerank_docs

    final_k = k or settings.rag_top_k
    use_hybrid = settings.rag_hybrid_enabled if use_hybrid is None else use_hybrid
    use_rerank = settings.rag_rerank_enabled if use_rerank is None else use_rerank

    # 送进 reranker 前的候选数 (Hybrid / Vector 都取这么多)
    if use_hybrid or use_rerank:
        retrieve_k = max(final_k, retrieve_k or settings.rag_retrieve_k)
    else:
        retrieve_k = final_k

    # ---------- Step 1: 向量粗排 ----------
    vector_docs = await safe_similarity_search(query, k=retrieve_k, filter=filter)
    if not vector_docs:
        return []

    # ---------- Step 2: Hybrid 融合 ----------
    candidates = vector_docs
    if use_hybrid:
        # 首次调用时惰性构建 BM25 索引 (从 pgvector 拉全量)
        if not _bm25_index.is_ready:
            try:
                await refresh_bm25_index()
            except Exception as e:
                logger.warning(f"[advanced_search] BM25 lazy build 失败: {type(e).__name__}: {e}")
        candidates = hybrid_search(query, vector_docs, k=retrieve_k, retrieve_k=retrieve_k)

    # ---------- Step 3: Rerank 精排 ----------
    if use_rerank and len(candidates) > final_k:
        try:
            candidates = await rerank_docs(query, candidates, top_n=final_k)
        except Exception as e:
            # rerank_docs 内部已有兜底, 这里只是二次保险
            logger.warning(f"[advanced_search] rerank 异常兜底: {type(e).__name__}: {e}")
            candidates = candidates[:final_k]
    else:
        candidates = candidates[:final_k]

    return candidates
