"""kb_source 数据源的增删查 + connector 取用.

职责:
  - kb_source 表的注册 / 列出 / 取单条 (asyncpg, 复用全局连接池)。
  - 按 source.type 返回对应 connector (本期只飞书)。
  - 预览 (preview): 调 connector.list_changes 列出源下文档, 不落库, 供接入自检。
"""

from __future__ import annotations

import json
from typing import List, Optional

from loguru import logger

from app.core.connectors import KbSource, feishu_connector
from app.core.connectors.base import DocRef, SourceConnector
from app.db.postgres import get_pool


def _connector_for(source: KbSource) -> SourceConnector:
    if source.type == "feishu":
        return feishu_connector
    raise ValueError(f"暂不支持的数据源类型: {source.type}")


def _row_to_source(r) -> KbSource:
    cfg = r["config"]
    if isinstance(cfg, str):  # JSONB 在某些驱动下回字符串
        cfg = json.loads(cfg or "{}")
    return KbSource(id=r["id"], type=r["type"], name=r["name"], config=cfg or {})


async def create_source(
    *, id: str, type: str, name: str, config: dict
) -> KbSource:
    """注册 (或覆盖) 一个数据源。"""
    pool = await get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO kb_source (id, type, name, config)
            VALUES ($1, $2, $3, $4::jsonb)
            ON CONFLICT (id) DO UPDATE
                SET type = EXCLUDED.type,
                    name = EXCLUDED.name,
                    config = EXCLUDED.config
            """,
            id, type, name, json.dumps(config, ensure_ascii=False),
        )
    logger.info(f"[kb_source] 注册数据源 id={id} type={type}")
    return KbSource(id=id, type=type, name=name, config=config)


async def list_sources() -> List[KbSource]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, type, name, config FROM kb_source ORDER BY created_at"
        )
    return [_row_to_source(r) for r in rows]


async def get_source(source_id: str) -> Optional[KbSource]:
    pool = await get_pool()
    async with pool.acquire() as conn:
        r = await conn.fetchrow(
            "SELECT id, type, name, config FROM kb_source WHERE id = $1", source_id
        )
    return _row_to_source(r) if r else None


async def preview_source(source: KbSource) -> List[DocRef]:
    """列出源下文档 (不落库)。用于接入自检: 验证鉴权/协作者授权/类型白名单。"""
    connector = _connector_for(source)
    return await connector.list_changes(source)
