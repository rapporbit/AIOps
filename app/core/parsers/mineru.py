"""MinerU 官方在线 API 解析器 (mineru.net, Precision /api/v4).

异步流程 (已核实):
  1. POST /api/v4/file-urls/batch  → 拿 batch_id + 预签名 OSS 上传地址 (file_urls)
  2. PUT 预签名地址 (上传二进制, **不带 Authorization**, 否则 403); 上传完成系统自动提交解析
  3. 轮询 GET /api/v4/extract-results/batch/{batch_id} 看 state (done/failed)
  4. done → 下载 full_zip_url, 解压取 full.md

约定 (无 fallback): 任意失败 → raise ParseError, 不返回降级结果。
"""

from __future__ import annotations

import asyncio
import io
import time
import zipfile
from typing import Any

import httpx
from loguru import logger

from app.config import settings
from app.core.parsers.base import ParseError

_DONE = "done"
_FAILED = "failed"


def _auth_headers() -> dict:
    return {"Authorization": f"Bearer {settings.mineru_token}"}


def _check_api(resp: httpx.Response, what: str) -> dict:
    """校验 MinerU JSON 响应: HTTP 2xx 且业务 code==0, 返回 data。否则 ParseError。"""
    if resp.status_code // 100 != 2:
        raise ParseError(f"MinerU {what} HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        j = resp.json()
    except Exception as e:
        raise ParseError(f"MinerU {what} 响应非 JSON: {e}") from e
    if j.get("code") not in (0, "0", None):
        raise ParseError(f"MinerU {what} 业务失败 code={j.get('code')} msg={j.get('msg')}")
    return j.get("data") or {}


class MinerUParser:
    """MinerU 官方 API 解析器 (实现 DocumentParser 协议)。"""

    async def to_markdown(
        self, blob: bytes, *, filename: str, need_ocr: bool = False
    ) -> str:
        if not settings.mineru_token.strip():
            raise ParseError("未配置 MINERU_TOKEN, 无法解析文档")
        if not blob:
            raise ParseError("空文件")
        if len(blob) > settings.mineru_max_bytes:
            raise ParseError(
                f"文件过大 {len(blob)} 字节, 超过上限 {settings.mineru_max_bytes}"
            )

        deadline = time.monotonic() + settings.mineru_timeout_sec
        # 分别设超时: 文件最大 200MB, PUT 上传/zip 下载需要较长 write/read; 轮询 GET 很快。
        timeout = httpx.Timeout(connect=30.0, read=180.0, write=300.0, pool=30.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            batch_id = await self._submit(client, blob, filename, need_ocr)
            zip_url = await self._poll(client, batch_id, filename, deadline)
            return await self._fetch_markdown(client, zip_url)

    async def _submit(
        self, client: httpx.AsyncClient, blob: bytes, filename: str, need_ocr: bool
    ) -> str:
        """申请预签名上传地址并 PUT 上传, 返回 batch_id。"""
        body = {
            "files": [{"name": filename, "is_ocr": need_ocr}],
            "model_version": settings.mineru_model_version,
            "enable_formula": settings.mineru_enable_formula,
            "enable_table": settings.mineru_enable_table,
            "language": settings.mineru_language,
        }
        resp = await client.post(
            f"{settings.mineru_api_base}/api/v4/file-urls/batch",
            json=body,
            headers=_auth_headers(),
        )
        data = _check_api(resp, "file-urls/batch")
        batch_id = data.get("batch_id")
        file_urls = data.get("file_urls") or []
        if not batch_id or not file_urls:
            raise ParseError(f"file-urls/batch 未返回 batch_id/file_urls: {data}")

        # PUT 到预签名地址: 注意不带 Authorization, 否则 OSS 返回 403
        put = await client.put(file_urls[0], content=blob)
        if put.status_code // 100 != 2:
            raise ParseError(f"上传到预签名地址失败 HTTP {put.status_code}: {put.text[:200]}")
        logger.info(f"[mineru] 已提交解析: {filename} batch_id={batch_id}")
        return batch_id

    async def _poll(
        self, client: httpx.AsyncClient, batch_id: str, filename: str, deadline: float
    ) -> str:
        """轮询批任务直到本文件 done, 返回 full_zip_url; failed/超时则 ParseError。"""
        url = f"{settings.mineru_api_base}/api/v4/extract-results/batch/{batch_id}"
        while True:
            if time.monotonic() > deadline:
                raise ParseError(f"MinerU 解析超时 (>{settings.mineru_timeout_sec}s) batch={batch_id}")
            resp = await client.get(url, headers=_auth_headers())
            data = _check_api(resp, "extract-results")
            item = _pick_result(data, filename)
            state = (item.get("state") or "").lower()
            if state == _DONE:
                zip_url = item.get("full_zip_url")
                if not zip_url:
                    raise ParseError(f"任务 done 但无 full_zip_url: {item}")
                return zip_url
            if state == _FAILED:
                raise ParseError(f"MinerU 解析失败: {item.get('err_msg') or item}")
            await asyncio.sleep(settings.mineru_poll_interval_sec)

    async def _fetch_markdown(self, client: httpx.AsyncClient, zip_url: str) -> str:
        """下载结果 zip (CDN, 无需鉴权), 解压取 full.md。"""
        resp = await client.get(zip_url)
        if resp.status_code // 100 != 2:
            raise ParseError(f"下载结果 zip 失败 HTTP {resp.status_code}")
        try:
            zf = zipfile.ZipFile(io.BytesIO(resp.content))
        except zipfile.BadZipFile as e:
            raise ParseError(f"结果不是合法 zip: {e}") from e
        names = zf.namelist()
        # 优先 full.md, 否则任意 .md
        md_name = next((n for n in names if n.endswith("full.md")), None) or next(
            (n for n in names if n.lower().endswith(".md")), None
        )
        if not md_name:
            raise ParseError(f"结果 zip 内无 markdown: {names}")
        md = zf.read(md_name).decode("utf-8", errors="replace").strip()
        if not md:
            raise ParseError("解析结果 markdown 为空")
        return md


def _pick_result(data: dict, filename: str) -> dict[str, Any]:
    """从批结果里挑出本文件的那条 (字段名在不同版本间略有差异, 做兜底)。"""
    results = (
        data.get("extract_result")
        or data.get("results")
        or data.get("extract_results")
        or []
    )
    if not isinstance(results, list) or not results:
        raise ParseError(f"extract-results 无结果列表: {data}")
    for r in results:
        if r.get("file_name") == filename or r.get("name") == filename:
            return r
    return results[0]  # 单文件提交, 退化取第一条


# 模块级单例 (无状态, 复用即可)
mineru_parser = MinerUParser()
