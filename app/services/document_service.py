"""文档管理服务: 上传 / 列表 / 删除.

设计要点:
  - 上传: 读文件 → 分块 (core/splitter) → 写入 pgvector (core/pg_vector_store)
  - 列表: 按 source 字段聚合 chunk 数
  - 删除: 按 source 字段删除所有相关 chunk
"""

from typing import List

from fastapi import UploadFile
from loguru import logger

from app.config import settings
from app.core import pg_vector_store
from app.core.splitter import split_markdown
from app.exceptions import (
    UnsupportedFileTypeError,
    VectorStoreError,
)
from app.schemas.document import DocumentInfo, UploadResponse

ALLOWED_EXTENSIONS = {".md", ".markdown", ".txt"}


# ============================================================
# 上传
# ============================================================
async def upload_document(file: UploadFile) -> UploadResponse:
    """处理上传文件: 解析 → 分块 → 写入向量库."""
    filename = file.filename or "unknown"
    ext = _get_extension(filename)
    if ext not in ALLOWED_EXTENSIONS:
        raise UnsupportedFileTypeError(
            f"不支持的文件类型 '{ext}', 仅支持 {sorted(ALLOWED_EXTENSIONS)}"
        )

    # 读取文件
    raw = await file.read()
    if not raw:
        raise UnsupportedFileTypeError("文件为空")

    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise UnsupportedFileTypeError(f"文件不是 UTF-8 编码: {e}") from e

    bytes_count = len(raw)
    logger.info(f"[document] 收到上传: {filename} ({bytes_count} bytes)")

    # 分块
    chunks = split_markdown(content, source=filename)
    if not chunks:
        raise UnsupportedFileTypeError(f"文件 {filename} 切分后无有效内容")

    # 写入向量库
    try:
        await pg_vector_store.add_documents(chunks)
    except Exception as e:
        logger.exception(f"[document] 写入向量库失败: {e}")
        raise VectorStoreError(f"向量库写入失败: {e}") from e

    logger.info(f"[document] {filename}: 索引 {len(chunks)} 个 chunk")

    # 若启用 Hybrid Search, 上传后立刻重建 BM25 索引
    # 为什么放在这里: 保证下一次 RAG 查询就能用新文档; 失败不阻断上传
    if settings.rag_hybrid_enabled and settings.rag_bm25_refresh_on_upload:
        try:
            from app.core.hybrid_retriever import refresh_bm25_index

            await refresh_bm25_index()
        except Exception as e:
            logger.warning(f"[document] BM25 刷新失败 (忽略): {type(e).__name__}: {e}")

    return UploadResponse(
        source=filename,
        chunks_indexed=len(chunks),
        bytes=bytes_count,
    )


# ============================================================
# 列表
# ============================================================
async def list_documents() -> List[DocumentInfo]:
    """列出所有已索引的文档 (按 source 聚合)."""
    sources = await pg_vector_store.list_sources()
    return [DocumentInfo(source=src, chunk_count=cnt) for src, cnt in sources]


# ============================================================
# 删除
# ============================================================
async def delete_document(source: str) -> int:
    """按 source 删除所有相关 chunks.

    Returns:
        删除的 chunk 数量
    """
    try:
        deleted = await pg_vector_store.delete_by_source(source)
    except Exception as e:
        logger.exception(f"[document] 删除失败: {e}")
        raise VectorStoreError(f"删除失败: {e}") from e

    if deleted == 0:
        return 0

    logger.info(f"[document] 删除 {source}: {deleted} 个 chunk")

    # 同步刷新 BM25 索引 (避免删除后 BM25 仍能命中已删文档)
    if settings.rag_hybrid_enabled and settings.rag_bm25_refresh_on_upload:
        try:
            from app.core.hybrid_retriever import refresh_bm25_index

            await refresh_bm25_index()
        except Exception as e:
            logger.warning(f"[document] BM25 刷新失败 (忽略): {type(e).__name__}: {e}")

    return deleted


# ============================================================
# 辅助
# ============================================================
def _get_extension(filename: str) -> str:
    """提取扩展名 (含点, 小写)."""
    if "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower()
