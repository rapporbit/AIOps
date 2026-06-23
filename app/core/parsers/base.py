"""文档解析器抽象: 把任意格式的二进制归一化成 Markdown.

为什么抽象一层?
  - 本期只接 MinerU 官方 API (见 mineru.py), 但解析器是天然可替换的组件
    (未来可能切自托管 MinerU / DashScope 文档智能 / Docling)。统一接口让上层
    (上传、同步引擎) 不关心具体后端。
  - 失败语义统一: 任何解析失败都抛 ParseError, 由调用方决定"拒收 + 标记 failed",
    绝不返回降级/半成品结果 (项目约定: 无 fallback)。
"""

from __future__ import annotations

from typing import Protocol


class ParseError(Exception):
    """解析失败 (网络/超时/任务 failed/格式不支持等)。调用方据此拒收并稍后重试。"""


class DocumentParser(Protocol):
    async def to_markdown(
        self, blob: bytes, *, filename: str, need_ocr: bool = False
    ) -> str:
        """把文档二进制解析成 Markdown。

        Args:
            blob:     文档二进制内容
            filename: 原始文件名 (带扩展名, 解析端据此判断类型)
            need_ocr: 是否需要 OCR (扫描件/图片=True; 原生文本 docx/pdf=False)

        Returns:
            Markdown 文本

        Raises:
            ParseError: 任何失败, 不返回降级结果
        """
        ...
