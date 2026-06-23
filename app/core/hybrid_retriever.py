"""Hybrid Retriever: BM25 (稀疏) + Vector (稠密) + RRF 融合.

为什么要加 Hybrid Search
======================
纯向量检索在语义泛化上强, 但有两个典型失手场景:
  1) **精确 token 匹配丢失**: "ERR_CONN_REFUSED" / "redis.exception.TimeoutError"
     这些固定字符串/错误码/服务名, 向量编码会把它们"揉"进语义空间, 不如 BM25 精确命中。
  2) **罕见长尾词**: 内部自定义组件名 "oncall-dispatcher", 训练语料几乎没有, embedding
     质量差; BM25 不依赖语义, 见字如面。

Hybrid 策略: 让 BM25 (sparse) 和 Vector (dense) 各出候选, 再用 RRF 融合去重排名。

BM25 现在跑在哪
======================
  - 已从"进程内存 rank_bm25"迁到 **ParadeDB pg_search** (DB 侧 BM25 索引)。
  - 索引随写入自动维护: 无内存占用、无 10 万行截断、多进程共享同一份、上传/删除即时生效。
  - 中文分词由 settings.kb_bm25_tokenizer 决定 (默认 chinese_compatible = 按字, 与旧行为一致)。
  - 本模块只剩"纯函数 RRF 融合": 上游 (vector_store.advanced_search) 把向量候选和 BM25
    候选都取好传进来, 这里只负责合并排名, 不碰 DB、不持有任何索引状态。

为什么选 RRF (Reciprocal Rank Fusion) 而不是加权分数
======================
  - BM25 分数无上界 (依赖文档长度), 向量 cosine 在 [-1,1], 量纲不同, 直接加权要先归一化。
  - RRF 只用"排名"不看绝对分数: score = Σ weight_i/(k + rank_i), 对量纲不敏感, 是学术和
    工业界默认选择 (k=60 是 TREC 经典值)。
"""

from __future__ import annotations

from typing import Dict, List, Optional

from langchain_core.documents import Document
from loguru import logger

from app.config import settings


def hybrid_search(
    query: str,
    vector_docs: List[Document],
    bm25_docs: List[Document],
    *,
    k: int,
    bm25_weight: Optional[float] = None,
) -> List[Document]:
    """将 Vector 结果和 BM25 结果用 RRF 融合, 返回 top-k.

    Args:
        query:        用户查询 (仅用于日志)
        vector_docs:  向量检索 top-N (调用方传入, 已按相关性排序)
        bm25_docs:    pg_search BM25 top-N (调用方传入, 已按相关性排序)
        k:            融合后返回的文档数
        bm25_weight:  BM25 路权重 (None = settings.rag_hybrid_bm25_weight)

    Returns:
        List[Document]: 融合去重后的 top-k

    降级策略:
        - bm25_docs 为空 → 直接返回 vector_docs[:k]
        - 两者都空 → 返回 []
    """
    if not bm25_docs:
        # BM25 不可用 (索引没建/查询失败) 时退回纯向量
        return vector_docs[:k]

    bm25_weight = bm25_weight if bm25_weight is not None else settings.rag_hybrid_bm25_weight
    vec_weight = 1.0 - bm25_weight

    # RRF: score(d) = Σ weight_i / (rrf_k + rank_i(d)); rrf_k=60 TREC 经典默认值
    rrf_k = max(1, int(settings.rag_hybrid_rrf_k or 60))
    scores: Dict[str, float] = {}
    doc_map: Dict[str, Document] = {}

    def _key(doc: Document) -> str:
        """用 (source, chapter, content hash) 做唯一键, 跨两路去重."""
        meta = doc.metadata or {}
        return f"{meta.get('source', '')}|{meta.get('chapter', '')}|{hash(doc.page_content)}"

    for rank, doc in enumerate(vector_docs):
        kk = _key(doc)
        scores[kk] = scores.get(kk, 0.0) + vec_weight / (rrf_k + rank + 1)
        doc_map.setdefault(kk, doc)

    for rank, doc in enumerate(bm25_docs):
        kk = _key(doc)
        scores[kk] = scores.get(kk, 0.0) + bm25_weight / (rrf_k + rank + 1)
        doc_map.setdefault(kk, doc)

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    top = [doc_map[kk] for kk, _ in ranked[:k]]

    logger.info(
        f"[hybrid] fused: query={query[:40]!r} "
        f"vec={len(vector_docs)} bm25={len(bm25_docs)} -> top={len(top)}"
    )
    return top
