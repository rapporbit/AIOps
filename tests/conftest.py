"""测试共享工具: 用 httpx.MockTransport 拦截外部调用, 不碰真实网络/DB。

这些单测只覆盖纯逻辑 + HTTP 协议层 (MinerU 解析器 / 飞书连接器 / 上传归一化路由),
不依赖 Postgres 或真实 API Key, 可离线快速运行。
"""

from __future__ import annotations

import httpx


def patch_async_client(monkeypatch, module, handler) -> dict:
    """把 module.httpx.AsyncClient 替换成走 MockTransport 的版本。

    返回一个 state dict, handler 可往里记东西 (调用计数等)。
    """
    transport = httpx.MockTransport(handler)
    orig = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("transport", None)
        return orig(transport=transport, **kwargs)

    monkeypatch.setattr(module.httpx, "AsyncClient", factory)
    return {}
