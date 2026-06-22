"""飞书 (Lark) 数据源连接器.

能力:
  - tenant_access_token 内存缓存 + 过期前自动刷新 (asyncio.Lock 防并发重复换取)。
  - list_changes: 递归遍历 Wiki 知识库节点 (分页), 按类型白名单过滤, 返回 DocRef。
  - fetch_blob: 原生文档 (docx/doc) 走 export_task 导出 docx → 下载字节;
    drive 内的真实文件 (file) 直接下载。统一交给 MinerU 解析。

关键坑 (来自调研):
  - Wiki node 与文档 obj 分离: node 只是挂载点, obj_token 才是文档主键。
  - 应用必须被加为目标知识库的"文档应用/协作者", 否则 nodes 列表为空。
  - export_task 不支持导出 Markdown, 故导出 docx 再过 MinerU。
  - 多接口 3-5 QPS, 对 429/99991400 需退避 (本期文档量小, 暂只做基础重试)。
"""

from __future__ import annotations

import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

import httpx
from loguru import logger

from app.config import settings
from app.core.connectors.base import DocRef, KbSource

# Wiki obj_type → 处理方式
#   docx/doc : export_task 导出 docx 再过 MinerU
#   file     : drive 文件, 直接下载 (need_ocr 看扩展名)
# 其余 (sheet/bitable/mindnote/slides/快捷方式) 本期跳过。
_EXPORT_TYPES = {"docx", "doc"}
_FILE_TYPES = {"file"}
_SUPPORTED = _EXPORT_TYPES | _FILE_TYPES

_OCR_EXTS = {"png", "jpg", "jpeg", "bmp", "gif", "tiff"}


class FeishuError(Exception):
    """飞书 API 调用失败。"""


class FeishuConnector:
    def __init__(
        self,
        app_id: Optional[str] = None,
        app_secret: Optional[str] = None,
    ) -> None:
        self._app_id = app_id if app_id is not None else settings.feishu_app_id
        self._app_secret = (
            app_secret if app_secret is not None else settings.feishu_app_secret
        )
        self._base = settings.feishu_api_base.rstrip("/")
        self._token: str = ""
        self._token_exp: float = 0.0  # monotonic 到期时刻
        self._token_lock = asyncio.Lock()

    # ---------------- token ----------------
    async def _get_token(self, client: httpx.AsyncClient) -> str:
        """返回有效 tenant_access_token, 过期前 60s 自动刷新。"""
        if self._token and time.monotonic() < self._token_exp - 60:
            return self._token
        async with self._token_lock:
            if self._token and time.monotonic() < self._token_exp - 60:
                return self._token  # 双检, 避免并发重复换取
            if not self._app_id or not self._app_secret:
                raise FeishuError("未配置 FEISHU_APP_ID / FEISHU_APP_SECRET")
            resp = await client.post(
                f"{self._base}/open-apis/auth/v3/tenant_access_token/internal",
                json={"app_id": self._app_id, "app_secret": self._app_secret},
            )
            j = resp.json()
            if j.get("code") != 0:
                raise FeishuError(f"换取 tenant_access_token 失败: {j}")
            self._token = j["tenant_access_token"]
            self._token_exp = time.monotonic() + int(j.get("expire", 7200))
            logger.info("[feishu] tenant_access_token 已刷新")
            return self._token

    async def _api(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """调用飞书 JSON 接口, 校验 code==0, 返回 data。"""
        token = await self._get_token(client)
        resp = await client.request(
            method,
            f"{self._base}{path}",
            params=params,
            json=json_body,
            headers={"Authorization": f"Bearer {token}"},
        )
        try:
            j = resp.json()
        except Exception as e:
            raise FeishuError(f"{path} 响应非 JSON (HTTP {resp.status_code}): {e}") from e
        if j.get("code") != 0:
            raise FeishuError(f"{path} 失败 code={j.get('code')} msg={j.get('msg')}")
        return j.get("data") or {}

    # ---------------- list_changes ----------------
    async def list_changes(self, source: KbSource) -> List[DocRef]:
        """两种模式 (按 config 自动选择):

        - node_tokens: [...]  指定文档列表模式。只需对每篇文档有"协作者"权限,
          逐个 get_node 取元信息。适合无法把应用加为知识库成员的租户。
        - space_id: "..."     整库枚举模式。需应用是知识库 (space) 成员。
        """
        node_tokens = source.config.get("node_tokens") or []
        if node_tokens:
            return await self._list_by_nodes(node_tokens)
        space_id = str(source.config.get("space_id") or "").strip()
        if not space_id:
            raise FeishuError("kb_source.config 需要 node_tokens 或 space_id 之一")
        return await self._list_by_space(space_id)

    async def _list_by_nodes(self, node_tokens: List[str]) -> List[DocRef]:
        refs: List[DocRef] = []
        skipped = 0
        timeout = httpx.Timeout(settings.feishu_timeout_sec)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for tk in node_tokens:
                node = await self._get_node(client, tk)
                obj_type = (node.get("obj_type") or "").lower()
                if obj_type not in _SUPPORTED:
                    skipped += 1
                    logger.info(
                        f"[feishu] 跳过不支持类型 obj_type={obj_type} title={node.get('title')!r}"
                    )
                    continue
                refs.append(self._node_to_ref(node, obj_type))
        logger.info(f"[feishu] 指定文档: 收录 {len(refs)} 篇, 跳过 {skipped} 个")
        return refs

    async def _list_by_space(self, space_id: str) -> List[DocRef]:
        refs: List[DocRef] = []
        skipped = 0
        timeout = httpx.Timeout(settings.feishu_timeout_sec)
        async with httpx.AsyncClient(timeout=timeout) as client:
            # BFS 遍历整棵知识库树 (None = 根)
            queue: List[Optional[str]] = [None]
            while queue:
                parent = queue.pop(0)
                async for node in self._iter_nodes(client, space_id, parent):
                    if node.get("has_child"):
                        queue.append(node.get("node_token"))
                    obj_type = (node.get("obj_type") or "").lower()
                    if obj_type not in _SUPPORTED:
                        skipped += 1
                        logger.info(
                            f"[feishu] 跳过不支持类型 obj_type={obj_type} "
                            f"title={node.get('title')!r}"
                        )
                        continue
                    refs.append(self._node_to_ref(node, obj_type))
        logger.info(f"[feishu] space={space_id}: 收录 {len(refs)} 篇, 跳过 {skipped} 个")
        return refs

    async def _get_node(self, client: httpx.AsyncClient, token: str) -> Dict[str, Any]:
        """get_node: 由 wiki 节点 token 取节点元信息 (只需该文档的协作者权限)。"""
        data = await self._api(
            client, "GET", "/open-apis/wiki/v2/spaces/get_node", params={"token": token}
        )
        node = data.get("node") or {}
        if not node.get("obj_token"):
            raise FeishuError(f"get_node 无 obj_token: token={token} data={data}")
        return node

    async def _iter_nodes(
        self, client: httpx.AsyncClient, space_id: str, parent: Optional[str]
    ):
        """分页迭代某层节点。"""
        page_token: Optional[str] = None
        while True:
            params: Dict[str, Any] = {"page_size": 50}
            if parent:
                params["parent_node_token"] = parent
            if page_token:
                params["page_token"] = page_token
            data = await self._api(
                client, "GET", f"/open-apis/wiki/v2/spaces/{space_id}/nodes", params=params
            )
            for item in data.get("items") or []:
                yield item
            if not data.get("has_more"):
                break
            page_token = data.get("page_token")
            if not page_token:
                break

    def _node_to_ref(self, node: Dict[str, Any], obj_type: str) -> DocRef:
        title = node.get("title") or node.get("obj_token") or "untitled"
        need_ocr = False
        if obj_type in _FILE_TYPES:
            ext = title.rsplit(".", 1)[-1].lower() if "." in title else ""
            need_ocr = ext in _OCR_EXTS
        return DocRef(
            external_id=node["obj_token"],
            external_type=obj_type,
            title=title,
            uri=node.get("obj_token", ""),
            version=str(node.get("obj_edit_time") or ""),
            need_ocr=need_ocr,
            extra={"node_token": node.get("node_token"), "space_id": node.get("space_id")},
        )

    # ---------------- fetch_blob ----------------
    async def fetch_blob(self, ref: DocRef) -> Tuple[bytes, str]:
        timeout = httpx.Timeout(settings.feishu_timeout_sec, read=120.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            if ref.external_type in _EXPORT_TYPES:
                return await self._export_docx(client, ref)
            if ref.external_type in _FILE_TYPES:
                blob = await self._download_drive_file(client, ref.external_id)
                return blob, ref.title  # title 含扩展名
            raise FeishuError(f"不支持的类型无法拉取: {ref.external_type}")

    async def _export_docx(
        self, client: httpx.AsyncClient, ref: DocRef
    ) -> Tuple[bytes, str]:
        """export_task 导出 docx → 轮询 → 下载, 返回 (docx字节, 文件名)。"""
        data = await self._api(
            client,
            "POST",
            "/open-apis/drive/v1/export_tasks",
            json_body={
                "file_extension": "docx",
                "token": ref.external_id,
                "type": ref.external_type,
            },
        )
        ticket = data.get("ticket")
        if not ticket:
            raise FeishuError(f"创建导出任务无 ticket: {data}")

        deadline = time.monotonic() + settings.feishu_export_timeout_sec
        file_token = ""
        while True:
            if time.monotonic() > deadline:
                raise FeishuError(f"导出超时 ticket={ticket}")
            res = await self._api(
                client,
                "GET",
                f"/open-apis/drive/v1/export_tasks/{ticket}",
                params={"token": ref.external_id},
            )
            result = res.get("result") or {}
            status = result.get("job_status")
            if status == 0:  # 成功
                file_token = result.get("file_token") or ""
                break
            if status in (1, 2):  # 初始化 / 处理中
                await asyncio.sleep(2.0)
                continue
            raise FeishuError(
                f"导出失败 job_status={status} msg={result.get('job_error_msg')}"
            )
        if not file_token:
            raise FeishuError("导出成功但无 file_token")

        blob = await self._download_export(client, file_token)
        return blob, f"{ref.title}.docx"

    async def _download_export(self, client: httpx.AsyncClient, file_token: str) -> bytes:
        token = await self._get_token(client)
        resp = await client.get(
            f"{self._base}/open-apis/drive/v1/export_tasks/file/{file_token}/download",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code // 100 != 2:
            raise FeishuError(f"下载导出文件失败 HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.content

    async def _download_drive_file(self, client: httpx.AsyncClient, file_token: str) -> bytes:
        token = await self._get_token(client)
        resp = await client.get(
            f"{self._base}/open-apis/drive/v1/files/{file_token}/download",
            headers={"Authorization": f"Bearer {token}"},
        )
        if resp.status_code // 100 != 2:
            raise FeishuError(f"下载 drive 文件失败 HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.content


# 模块级单例 (token 缓存随之复用)
feishu_connector = FeishuConnector()
