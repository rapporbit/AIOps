"""联网搜索 Provider + 结果格式化 (单一实现).

调用方:
  - app.services.rag_service: 走结构化 hits, 自己拼提示
  - mcp_servers.websearch_server: 加黑名单/脱敏/限频后转 Markdown 返回给 Agent
"""

from __future__ import annotations

import os
from typing import Any, Dict, List


def get_provider() -> str:
    """从 settings (优先) 或环境变量解析 provider 名."""
    try:
        from app.config import settings
        val = settings.web_search_provider
    except Exception:
        val = os.environ.get("WEB_SEARCH_PROVIDER", "")
    return (val or "mock").lower().strip()


def get_tavily_api_key() -> str:
    try:
        from app.config import settings
        return (settings.tavily_api_key or "").strip()
    except Exception:
        return (os.environ.get("TAVILY_API_KEY") or "").strip()


def search_tavily(query: str, max_results: int) -> List[Dict[str, Any]]:
    """Tavily Search API. 申请: https://tavily.com (免费 1000 次/月)."""
    api_key = get_tavily_api_key()
    if not api_key:
        raise RuntimeError(
            "TAVILY_API_KEY 未配置. 请在 .env 设置, 或切换 WEB_SEARCH_PROVIDER=mock"
        )
    import httpx

    resp = httpx.post(
        "https://api.tavily.com/search",
        json={
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        },
        timeout=15.0,
    )
    resp.raise_for_status()
    data = resp.json()
    return [
        {
            "title": r.get("title", "(无标题)"),
            "url": r.get("url", ""),
            "snippet": r.get("content", ""),
        }
        for r in data.get("results", [])
    ]


def search_ddgs(query: str, max_results: int) -> List[Dict[str, Any]]:
    """DuckDuckGo (零配置, 国内访问不稳, 需 pip install ddgs)."""
    try:
        from ddgs import DDGS  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "ddgs 未安装. 运行 `pip install ddgs`, 或切换 WEB_SEARCH_PROVIDER=mock"
        ) from e

    with DDGS() as client:
        return [
            {
                "title": r.get("title", "(无标题)"),
                "url": r.get("href", ""),
                "snippet": r.get("body", ""),
            }
            for r in client.text(query, max_results=max_results)
        ]


def search_mock(query: str, max_results: int) -> List[Dict[str, Any]]:
    """Mock provider (默认无外部依赖, 内容标注 [MOCK])."""
    return [
        {
            "title": f"[MOCK] 关于 '{query}' 的搜索示例",
            "url": "https://example.com/mock-search-result",
            "snippet": (
                f"这是 web_search 的 Mock 占位返回 (provider=mock). "
                f"配置 WEB_SEARCH_PROVIDER=tavily 并提供 TAVILY_API_KEY 可使用真实搜索. "
                f"原始查询: {query!r}. "
                f"Mock 模式下不要把结果当真实信源引用."
            ),
        }
    ]


def search(query: str, max_results: int, provider: str | None = None) -> List[Dict[str, Any]]:
    """按 provider 调度. 调用方负责异常处理."""
    p = (provider or get_provider()).lower().strip()
    if p == "tavily":
        return search_tavily(query, max_results)
    if p == "ddgs":
        return search_ddgs(query, max_results)
    if p == "mock":
        return search_mock(query, max_results)
    raise ValueError(f"未知 web_search provider: {p!r} (可选 mock / tavily / ddgs)")


def format_results(results: List[Dict[str, Any]], *, provider: str) -> str:
    """把搜索结果格式化为 Markdown."""
    header = ""
    if provider == "mock":
        header = (
            "> **[WARN] provider=mock**: 以下结果为占位数据, 仅供演示, "
            "不要在最终报告中作为真实信源引用.\n\n"
        )
    lines = []
    for i, r in enumerate(results, 1):
        title = r.get("title") or "(无标题)"
        url = r.get("url") or "(无 URL)"
        snippet = (r.get("snippet") or "").strip()
        lines.append(f"### {i}. {title}\n来源: {url}\n\n{snippet}")
    return header + "\n\n---\n\n".join(lines)
