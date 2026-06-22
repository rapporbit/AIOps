"""MinerU 解析器单测 (MockTransport, 无真实 API)。"""

import asyncio
import io
import zipfile

import httpx
import pytest

from app.config import settings
from app.core.parsers import ParseError, mineru
from tests.conftest import patch_async_client


def _zip_with(md_name: str, content: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(md_name, content)
    return buf.getvalue()


def _make_handler(state, *, poll_states=("done",), zip_bytes=None, md="# T\n正文"):
    zip_bytes = zip_bytes if zip_bytes is not None else _zip_with("full.md", md)

    def handler(req: httpx.Request) -> httpx.Response:
        url, m = str(req.url), req.method
        if url.endswith("/api/v4/file-urls/batch") and m == "POST":
            return httpx.Response(200, json={"code": 0, "data": {
                "batch_id": "b1", "file_urls": ["https://up.example/put"]}})
        if url == "https://up.example/put" and m == "PUT":
            state["put_auth"] = req.headers.get("authorization")
            return httpx.Response(200)
        if "/extract-results/batch/b1" in url and m == "GET":
            i = min(state["polls"], len(poll_states) - 1)
            state["polls"] += 1
            st = poll_states[i]
            item = {"file_name": "a.pdf", "state": st}
            if st == "done":
                item["full_zip_url"] = "https://cdn.example/r.zip"
            if st == "failed":
                item["err_msg"] = "boom"
            return httpx.Response(200, json={"code": 0, "data": {"extract_result": [item]}})
        if url == "https://cdn.example/r.zip" and m == "GET":
            return httpx.Response(200, content=zip_bytes)
        return httpx.Response(404, text=f"unexpected {m} {url}")
    return handler


def _run(coro):
    return asyncio.run(coro)


def test_full_flow_returns_markdown(monkeypatch):
    settings.mineru_token = "tk"
    settings.mineru_poll_interval_sec = 0.001
    state = {"polls": 0, "put_auth": "x"}
    patch_async_client(monkeypatch, mineru, _make_handler(state, poll_states=("running", "done")))
    md = _run(mineru.mineru_parser.to_markdown(b"%PDF", filename="a.pdf"))
    assert md == "# T\n正文"
    assert state["put_auth"] is None       # 预签名 PUT 不带 Authorization
    assert state["polls"] == 2             # running 后再 done


def test_failed_state_raises(monkeypatch):
    settings.mineru_token = "tk"
    settings.mineru_poll_interval_sec = 0.001
    state = {"polls": 0}
    patch_async_client(monkeypatch, mineru, _make_handler(state, poll_states=("failed",)))
    with pytest.raises(ParseError):
        _run(mineru.mineru_parser.to_markdown(b"%PDF", filename="a.pdf"))


def test_no_token_raises(monkeypatch):
    settings.mineru_token = ""
    with pytest.raises(ParseError):
        _run(mineru.mineru_parser.to_markdown(b"%PDF", filename="a.pdf"))


def test_empty_blob_raises(monkeypatch):
    settings.mineru_token = "tk"
    with pytest.raises(ParseError):
        _run(mineru.mineru_parser.to_markdown(b"", filename="a.pdf"))


def test_oversize_raises(monkeypatch):
    settings.mineru_token = "tk"
    settings.mineru_max_bytes = 4
    with pytest.raises(ParseError):
        _run(mineru.mineru_parser.to_markdown(b"12345", filename="a.pdf"))
    settings.mineru_max_bytes = 200 * 1024 * 1024


def test_zip_without_markdown_raises(monkeypatch):
    settings.mineru_token = "tk"
    settings.mineru_poll_interval_sec = 0.001
    state = {"polls": 0}
    bad_zip = io.BytesIO()
    with zipfile.ZipFile(bad_zip, "w") as z:
        z.writestr("layout.json", "{}")
    patch_async_client(monkeypatch, mineru,
                       _make_handler(state, poll_states=("done",), zip_bytes=bad_zip.getvalue()))
    with pytest.raises(ParseError):
        _run(mineru.mineru_parser.to_markdown(b"%PDF", filename="a.pdf"))
