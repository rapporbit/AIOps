"""文档解析器: 把多格式二进制归一化成 Markdown (本期 MinerU 官方 API)。"""

from app.core.parsers.base import DocumentParser, ParseError
from app.core.parsers.mineru import MinerUParser, mineru_parser

__all__ = ["DocumentParser", "ParseError", "MinerUParser", "mineru_parser"]
