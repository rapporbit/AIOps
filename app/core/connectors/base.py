"""数据源连接器抽象: 统一"列出变更 + 拉取内容"接口.

为什么抽象一层?
  - 本期只接飞书 (见 feishu.py), 但数据源是天然可扩展的 (腾讯文档/语雀/对象存储…)。
    统一 DocRef + SourceConnector 让同步引擎 (P3) 不关心具体平台。
  - 与解析器 (parsers) 解耦: connector 只负责"把外部文档拿成字节 + 告知是否需 OCR",
    字节交给 MinerU 解析。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol, Tuple


@dataclass
class DocRef:
    """外部文档的一个引用 (列变更时返回, 不含正文)。"""

    external_id: str          # 源系统内的稳定 ID (飞书: obj_token)
    external_type: str = ""   # 类型 (飞书: docx/doc/sheet/file/...)
    title: str = ""
    uri: str = ""             # 原文链接
    version: str = ""         # 版本/编辑时间, 用于增量粗筛 (飞书: obj_edit_time)
    need_ocr: bool = False    # 拉取后是否需要 OCR (扫描件/图片 True)
    deleted: bool = False
    extra: Dict[str, Any] = field(default_factory=dict)  # 平台特有字段 (如 wiki node_token)


@dataclass
class KbSource:
    """数据源实例 (对应 kb_source 表的一行, 传给 connector)。"""

    id: str
    type: str                 # 'feishu'
    name: str = ""
    config: Dict[str, Any] = field(default_factory=dict)  # {space_id, ...}; 不含密钥


class SourceConnector(Protocol):
    async def list_changes(self, source: KbSource) -> List[DocRef]:
        """列出源下全部 (受支持类型的) 文档及其当前版本。

        本期全量列举, 删除靠同步引擎的快照 diff (远端 id 集合 vs 本地 active)。
        不受支持的类型应被跳过 (不出现在返回里)。
        """
        ...

    async def fetch_blob(self, ref: DocRef) -> Tuple[bytes, str]:
        """拉取一篇文档的二进制 + 建议文件名 (供解析器按扩展名判类型)。

        Raises: 拉取失败时抛异常, 由同步引擎标记该文档 failed。
        """
        ...
