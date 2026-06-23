"""飞书连接器单测 (MockTransport, 无真实 API)。"""

import asyncio

import httpx
import pytest

from app.core.connectors import KbSource, feishu
from tests.conftest import patch_async_client


def _run(coro):
    return asyncio.run(coro)


def _handler(state):
    def h(req: httpx.Request) -> httpx.Response:
        url, m = str(req.url), req.method
        p = req.url.params
        if url.endswith("/auth/v3/tenant_access_token/internal"):
            state["auth"] += 1
            return httpx.Response(200, json={"code": 0, "tenant_access_token": "t", "expire": 7200})
        if "/wiki/v2/spaces/" in url and "/nodes" in url:
            parent, page = p.get("parent_node_token"), p.get("page_token")
            if not parent and not page:
                return httpx.Response(200, json={"code": 0, "data": {"items": [
                    {"node_token": "n1", "obj_token": "doc1", "obj_type": "docx",
                     "title": "手册", "obj_edit_time": "100", "has_child": True},
                    {"node_token": "n2", "obj_token": "s1", "obj_type": "sheet",
                     "title": "表", "obj_edit_time": "101", "has_child": False},
                ], "has_more": True, "page_token": "P2"}})
            if not parent and page == "P2":
                return httpx.Response(200, json={"code": 0, "data": {"items": [
                    {"node_token": "n3", "obj_token": "img1", "obj_type": "file",
                     "title": "图.png", "obj_edit_time": "102", "has_child": False},
                ], "has_more": False}})
            if parent == "n1":
                return httpx.Response(200, json={"code": 0, "data": {"items": [
                    {"node_token": "n4", "obj_token": "doc2", "obj_type": "docx",
                     "title": "子", "obj_edit_time": "200", "has_child": False},
                ], "has_more": False}})
            return httpx.Response(200, json={"code": 0, "data": {"items": [], "has_more": False}})
        if url.endswith("/drive/v1/export_tasks") and m == "POST":
            return httpx.Response(200, json={"code": 0, "data": {"ticket": "tk"}})
        if "/drive/v1/export_tasks/tk" in url and m == "GET":
            state["poll"] += 1
            if state["poll"] < 2:
                return httpx.Response(200, json={"code": 0, "data": {"result": {"job_status": 2}}})
            return httpx.Response(200, json={"code": 0, "data": {"result": {"job_status": 0, "file_token": "ft"}}})
        if "/export_tasks/file/ft/download" in url:
            return httpx.Response(200, content=b"PKdocx")
        if "/drive/v1/files/img1/download" in url:
            return httpx.Response(200, content=b"PNGbytes")
        return httpx.Response(404, text=f"unexpected {m} {url}")
    return h


def _conn(monkeypatch):
    state = {"auth": 0, "poll": 0}
    patch_async_client(monkeypatch, feishu, _handler(state))
    c = feishu.FeishuConnector(app_id="cli", app_secret="sec")
    return c, state


def test_list_changes_pagination_recursion_whitelist(monkeypatch):
    c, _ = _conn(monkeypatch)
    src = KbSource(id="s", type="feishu", config={"space_id": "S1"})
    refs = _run(c.list_changes(src))
    ids = {r.external_id for r in refs}
    assert ids == {"doc1", "doc2", "img1"}      # 分页(P2) + 递归(n1→doc2), 跳过 sheet
    img = next(r for r in refs if r.external_id == "img1")
    assert img.need_ocr is True                  # 图片按扩展名推断需 OCR
    assert next(r for r in refs if r.external_id == "doc1").need_ocr is False


def test_fetch_blob_docx_export(monkeypatch):
    c, state = _conn(monkeypatch)
    src = KbSource(id="s", type="feishu", config={"space_id": "S1"})
    refs = _run(c.list_changes(src))
    docx = next(r for r in refs if r.external_id == "doc1")
    blob, name = _run(c.fetch_blob(docx))
    assert blob == b"PKdocx" and name == "手册.docx"
    assert state["poll"] == 2                     # 处理中→成功


def test_fetch_blob_file_download(monkeypatch):
    c, _ = _conn(monkeypatch)
    src = KbSource(id="s", type="feishu", config={"space_id": "S1"})
    refs = _run(c.list_changes(src))
    img = next(r for r in refs if r.external_id == "img1")
    blob, name = _run(c.fetch_blob(img))
    assert blob == b"PNGbytes" and name == "图.png"


def test_token_cached(monkeypatch):
    c, state = _conn(monkeypatch)
    src = KbSource(id="s", type="feishu", config={"space_id": "S1"})
    refs = _run(c.list_changes(src))
    _run(c.fetch_blob(refs[0]))
    assert state["auth"] == 1                     # 多次调用只换一次 token


def test_missing_space_id_raises(monkeypatch):
    c, _ = _conn(monkeypatch)
    with pytest.raises(feishu.FeishuError):
        _run(c.list_changes(KbSource(id="s", type="feishu", config={})))
