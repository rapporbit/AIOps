"""知识库定时同步调度器 (进程内 asyncio 周期任务).

为什么是进程内 asyncio, 而不是塞进 diagnosis_worker 的 Redis Streams:
  - KB 同步是"周期性整源扫描", 不是 per-task 作业, 用不上 DLQ/认领/心跳那套;
  - 本期文档量小, 单源串行同步即可;
  - 多副本/与手动触发并发的安全, 已由 kb_sync_service.sync_source 内的 advisory lock 保证
    (同源同刻只跑一个), 调度器只负责"定时唤醒"。

生命周期: 在 app lifespan 启动时 start_scheduler(), 关闭时 await stop_scheduler()。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Optional

from loguru import logger

from app.config import settings
import app.services.kb_source_service as kb_source_service
import app.services.kb_sync_service as kb_sync_service

_task: Optional[asyncio.Task] = None


async def run_all_once() -> None:
    """对所有启用的数据源各跑一次同步 (失败不影响其它源)。"""
    sources = await kb_source_service.list_enabled_sources()
    if not sources:
        logger.debug("[kb_scheduler] 无启用的数据源, 跳过本轮")
        return
    for src in sources:
        try:
            stats = await kb_sync_service.sync_source(src)
            logger.info(f"[kb_scheduler] {src.id} -> {stats}")
        except Exception as e:
            logger.warning(f"[kb_scheduler] {src.id} 同步失败: {type(e).__name__}: {e}")


async def _loop() -> None:
    # 启动后先等一小会, 让 app 完全就绪再首次同步
    await asyncio.sleep(min(30, settings.kb_sync_interval_sec))
    while True:
        try:
            await run_all_once()
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[kb_scheduler] 轮次异常: {type(e).__name__}: {e}")
        await asyncio.sleep(settings.kb_sync_interval_sec)


def start_scheduler() -> None:
    """启动调度器 (幂等)。KB_SYNC_ENABLED=false 时不启动。"""
    global _task
    if not settings.kb_sync_enabled:
        logger.info("[kb_scheduler] 未启用 (KB_SYNC_ENABLED=false)")
        return
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop())
    logger.info(f"[kb_scheduler] 已启动, 同步间隔 {settings.kb_sync_interval_sec}s")


async def stop_scheduler() -> None:
    """优雅停止调度器。"""
    global _task
    if _task is None:
        return
    _task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await _task
    _task = None
    logger.info("[kb_scheduler] 已停止")
