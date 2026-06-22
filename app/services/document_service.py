"""文档管理服务: 上传 / 列表 / 删除.

设计要点:
  - 上传: 读文件 → (md/txt 直通 | 其他格式经 MinerU 解析成 markdown) → 分块 → 幂等写入 pgvector
  - 列表: 按 source 字段聚合 chunk 数
  - 删除: 按 source 字段删除所有相关 chunk
  - 解析无 fallback: MinerU 失败即抛 DocumentParseError (HTTP 503), 不入库, 稍后重试
"""

from typing import List

from fastapi import UploadFile
from loguru import logger

from app.config import settings
from app.core import pg_vector_store
from app.core.parsers import ParseError, mineru_parser
from app.core.splitter import split_markdown
from app.exceptions import (
    DocumentParseError,
    UnsupportedFileTypeError,
    VectorStoreError,
)
from app.schemas.document import DocumentInfo, UploadResponse

# md/txt 直通 (本身就是文本, 无需解析)
TEXT_EXTENSIONS = {".md", ".markdown", ".txt"}
# 走 MinerU 解析的格式; 值 = 默认是否需要 OCR (图片/扫描件 True, 原生文本 False)
MINERU_EXTENSIONS = {
    ".pdf": False,
    ".doc": False,
    ".docx": False,
    ".ppt": False,
    ".pptx": False,
    ".html": False,
    ".png": True,
    ".jpg": True,
    ".jpeg": True,
    ".bmp": True,
    ".gif": True,
}
ALLOWED_EXTENSIONS = TEXT_EXTENSIONS | set(MINERU_EXTENSIONS)


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
    bytes_count = len(raw)
    logger.info(f"[document] 收到上传: {filename} ({bytes_count} bytes)")

    # 归一化成 markdown 文本
    if ext in TEXT_EXTENSIONS:
        # md/txt 直通
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            raise UnsupportedFileTypeError(f"文件不是 UTF-8 编码: {e}") from e
    else:
        # 其他格式经 MinerU 解析。大小预检 (客户端错误); 解析失败 → 503 拒收, 不入库。
        if bytes_count > settings.mineru_max_bytes:
            raise UnsupportedFileTypeError(
                f"文件过大 ({bytes_count} 字节), 超过上限 {settings.mineru_max_bytes}"
            )
        try:
            content = await mineru_parser.to_markdown(
                raw, filename=filename, need_ocr=MINERU_EXTENSIONS[ext]
            )
        except ParseError as e:
            logger.warning(f"[document] MinerU 解析失败 {filename}: {e}")
            raise DocumentParseError(detail=str(e)) from e

    # 分块
    chunks = split_markdown(content, source=filename)
    if not chunks:
        raise UnsupportedFileTypeError(f"文件 {filename} 切分后无有效内容")

    # 写入向量库: 用 doc_id 幂等替换 (同名再传会先删旧 chunk, 不再重复堆积)。
    # 本地直传的稳定 doc_id 约定 = 'local:'+文件名, 与建表时的存量回填一致。
    doc_id = f"local:{filename}"
    try:
        await pg_vector_store.replace_doc(doc_id, chunks)
    except Exception as e:
        logger.exception(f"[document] 写入向量库失败: {e}")
        raise VectorStoreError(f"向量库写入失败: {e}") from e

    logger.info(f"[document] {filename}: 索引 {len(chunks)} 个 chunk")

    # BM25 走 ParadeDB pg_search, 索引随 INSERT 自动维护, 无需手动刷新。

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

    # BM25 走 ParadeDB pg_search, 索引随 DELETE 自动维护, 无需手动刷新。

    return deleted


# ============================================================
# 辅助
# ============================================================
def _get_extension(filename: str) -> str:
    """提取扩展名 (含点, 小写)."""
    if "." not in filename:
        return ""
    return "." + filename.rsplit(".", 1)[-1].lower()
