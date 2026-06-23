"""上传归一化路由单测: md/txt 直通, 其他走 MinerU, 不支持的拒绝。"""

import asyncio

import pytest

from app.core.parsers import ParseError
from app.exceptions import UnsupportedFileTypeError
from app.services import document_service as ds


def _run(coro):
    return asyncio.run(coro)


def test_md_passthrough(monkeypatch):
    # md 不应调用 MinerU
    async def _boom(*a, **k):
        raise AssertionError("md 不应走 MinerU")
    monkeypatch.setattr(ds.mineru_parser, "to_markdown", _boom)
    out = _run(ds.normalize_to_markdown("# 标题\n正文".encode(), "a.md"))
    assert out == "# 标题\n正文"


def test_txt_passthrough(monkeypatch):
    out = _run(ds.normalize_to_markdown(b"plain text", "a.txt"))
    assert out == "plain text"


def test_pdf_routes_to_mineru(monkeypatch):
    seen = {}

    async def _fake(raw, *, filename, need_ocr=False):
        seen["filename"], seen["need_ocr"] = filename, need_ocr
        return "# parsed"
    monkeypatch.setattr(ds.mineru_parser, "to_markdown", _fake)
    out = _run(ds.normalize_to_markdown(b"%PDF", "a.pdf"))
    assert out == "# parsed"
    assert seen == {"filename": "a.pdf", "need_ocr": False}


def test_image_need_ocr_default(monkeypatch):
    seen = {}

    async def _fake(raw, *, filename, need_ocr=False):
        seen["need_ocr"] = need_ocr
        return "# img"
    monkeypatch.setattr(ds.mineru_parser, "to_markdown", _fake)
    _run(ds.normalize_to_markdown(b"PNG", "a.png"))
    assert seen["need_ocr"] is True            # 图片默认需 OCR


def test_unsupported_ext_raises():
    with pytest.raises(UnsupportedFileTypeError):
        _run(ds.normalize_to_markdown(b"x", "a.xyz"))


def test_empty_raises():
    with pytest.raises(UnsupportedFileTypeError):
        _run(ds.normalize_to_markdown(b"", "a.md"))


def test_non_utf8_md_raises():
    with pytest.raises(UnsupportedFileTypeError):
        _run(ds.normalize_to_markdown(b"\xff\xfe\x00bad", "a.md"))


def test_parse_error_propagates(monkeypatch):
    async def _fail(*a, **k):
        raise ParseError("mineru down")
    monkeypatch.setattr(ds.mineru_parser, "to_markdown", _fail)
    with pytest.raises(ParseError):
        _run(ds.normalize_to_markdown(b"%PDF", "a.pdf"))
