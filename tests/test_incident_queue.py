"""Redis Streams 任务队列集成测试 (无真实 Redis)。

用一个最小 FakeRedisStreams 复刻 Redis Streams 的关键语义:
  - xadd / xreadgroup(">") / xack: 投递只发"未投递"的新消息, ACK 后移出 PEL;
  - xautoclaim: 按 idle 阈值把崩溃 Worker 残留的 pending 消息转交他人;
  - consumer group last-id + PEL: 验证"已投递未 ACK"不会被重复正常投递。

覆盖队列对外的完整生命周期 (enqueue → 按优先级读 → ack / 死信 / stale 回收),
这些是 Worker 崩溃恢复与防重复执行的正确性根基, 之前没有任何测试守护。

不依赖 pytest-asyncio / fakeredis, 沿用本仓库 asyncio.run 的跑法。
"""

import asyncio

import pytest

from app.queue import redis_streams as rs


def _run(coro):
    return asyncio.run(coro)


def _id_tuple(msg_id: str) -> tuple[int, int]:
    a, _, b = msg_id.partition("-")
    return int(a), int(b or 0)


class FakeRedisStreams:
    """只实现 RedisIncidentQueue 用到的 Stream 命令, 单线程顺序执行即原子。

    now_ms 是可控时钟: 测 xautoclaim 时把它往前推, 模拟消息空闲超阈值。
    """

    def __init__(self) -> None:
        # stream -> [(msg_id, fields)]  (ACK 不删除, 和 Redis 一致)
        self.streams: dict[str, list[tuple[str, dict]]] = {}
        # (stream, group) -> {"last_id": str, "pending": {msg_id: {consumer, delivery_ms, fields}}}
        self.groups: dict[tuple[str, str], dict] = {}
        self._seq = 0
        self.now_ms = 1_000_000_000

    async def ping(self) -> bool:
        return True

    async def xgroup_create(self, *, name, groupname, id, mkstream=False):
        if mkstream:
            self.streams.setdefault(name, [])
        key = (name, groupname)
        if key in self.groups:
            raise RuntimeError("BUSYGROUP Consumer Group name already exists")
        # id="0" 表示从头消费; 这里 last_id 记录"已投递到哪", "0-0" 即未投递任何
        self.groups[key] = {"last_id": "0-0", "pending": {}}

    async def xadd(self, stream, *, fields, maxlen=None, approximate=True):
        self._seq += 1
        msg_id = f"{self._seq}-0"
        msgs = self.streams.setdefault(stream, [])
        msgs.append((msg_id, dict(fields)))
        if maxlen is not None and len(msgs) > maxlen:
            del msgs[: len(msgs) - maxlen]  # 近似裁剪: 丢最旧
        return msg_id

    async def xreadgroup(self, *, groupname, consumername, streams, count=1, block=None):
        rows = []
        for stream in streams:  # streams: {stream: ">"}
            grp = self.groups.get((stream, groupname))
            if grp is None:
                continue
            last = _id_tuple(grp["last_id"])
            fresh = [
                (mid, f)
                for (mid, f) in self.streams.get(stream, [])
                if _id_tuple(mid) > last
            ]
            take = fresh[:count]
            if not take:
                continue
            grp["last_id"] = take[-1][0]
            for mid, f in take:
                grp["pending"][mid] = {
                    "consumer": consumername,
                    "delivery_ms": self.now_ms,
                    "fields": f,
                }
            rows.append((stream, take))
        return rows

    async def xack(self, stream, group, message_id):
        grp = self.groups.get((stream, group))
        if grp is not None:
            grp["pending"].pop(message_id, None)

    async def xautoclaim(self, stream, group, consumer, min_idle_ms, *, start_id="0-0", count=1):
        grp = self.groups.get((stream, group))
        claimed = []
        if grp is not None:
            for mid in sorted(grp["pending"], key=_id_tuple):
                info = grp["pending"][mid]
                if self.now_ms - info["delivery_ms"] >= min_idle_ms:
                    info["consumer"] = consumer
                    info["delivery_ms"] = self.now_ms  # 重新计时
                    claimed.append((mid, info["fields"]))
                    if len(claimed) >= count:
                        break
        return start_id, claimed, []

    async def xlen(self, stream):
        return len(self.streams.get(stream, []))

    def pending_ids(self, stream, group):
        return set(self.groups.get((stream, group), {}).get("pending", {}))


@pytest.fixture
def fake(monkeypatch):
    fr = FakeRedisStreams()
    # 直接注入连接, 绕过真实 connect()
    monkeypatch.setattr(rs.incident_queue, "_client", fr)
    # 固定的可预测配置
    monkeypatch.setattr(rs.settings, "incident_queue_stream", "test:tasks")
    monkeypatch.setattr(rs.settings, "incident_queue_dlq_stream", "test:tasks:dlq")
    monkeypatch.setattr(rs.settings, "incident_queue_consumer_group", "test-workers")
    monkeypatch.setattr(rs.settings, "incident_queue_maxlen", 1000)
    monkeypatch.setattr(rs.settings, "diagnosis_worker_block_ms", 10)
    yield fr
    rs.incident_queue._client = None  # 清理全局单例


def _enqueue(task_id, *, level=None, severity=None, priority=100):
    payload = {"severity": severity} if severity else {}
    return rs.incident_queue.enqueue_task(
        task_id=task_id,
        incident_group_id=f"g-{task_id}",
        incident_id=f"i-{task_id}",
        diagnosis_mode="fast",
        priority=priority,
        payload=payload,
        level=level,
    )


# --------- 单流 (优先级关闭) ---------

def test_single_stream_fifo_read_then_ack(fake, monkeypatch):
    monkeypatch.setattr(rs.settings, "incident_queue_priority_enabled", False)

    async def go():
        await rs.incident_queue.ensure_group()
        await _enqueue("a")
        await _enqueue("b")
        first = await rs.incident_queue.read_tasks(consumer_name="w1", count=1, block_ms=1)
        second = await rs.incident_queue.read_tasks(consumer_name="w1", count=1, block_ms=1)
        return first, second

    first, second = _run(go())
    assert [t[1]["task_id"] for t in first] == ["a"]  # FIFO: 先进先出
    assert [t[1]["task_id"] for t in second] == ["b"]
    # 读出来带上 __stream__, 便于 ack/DLQ 定位
    assert first[0][1]["__stream__"] == "test:tasks"


def test_acked_message_not_redelivered(fake, monkeypatch):
    monkeypatch.setattr(rs.settings, "incident_queue_priority_enabled", False)

    async def go():
        await rs.incident_queue.ensure_group()
        await _enqueue("a")
        msg_id, item = (await rs.incident_queue.read_tasks(consumer_name="w1", block_ms=1))[0]
        await rs.incident_queue.ack(msg_id, stream=item["__stream__"])
        # ACK 后 PEL 应清空, 再读没有新消息
        again = await rs.incident_queue.read_tasks(consumer_name="w1", block_ms=1)
        return msg_id, item["__stream__"], again

    msg_id, stream, again = _run(go())
    assert again == []
    assert msg_id not in fake.pending_ids(stream, "test-workers")


# --------- 多流 (优先级开启) ---------

def test_priority_dispatch_critical_before_low(fake, monkeypatch):
    monkeypatch.setattr(rs.settings, "incident_queue_priority_enabled", True)

    async def go():
        await rs.incident_queue.ensure_group()
        # 先入低优先, 再入高优先 —— 读取必须高优先先出 (严格插队, 非 FIFO)
        await _enqueue("low1", level="low")
        await _enqueue("crit1", level="critical")
        await _enqueue("norm1", level="normal")
        order = []
        for _ in range(3):
            got = await rs.incident_queue.read_tasks(consumer_name="w1", count=1, block_ms=1)
            order.append(got[0][1]["task_id"])
        return order

    order = _run(go())
    assert order == ["crit1", "norm1", "low1"]


def test_enqueue_infers_level_from_severity(fake, monkeypatch):
    monkeypatch.setattr(rs.settings, "incident_queue_priority_enabled", True)

    async def go():
        await rs.incident_queue.ensure_group()
        # 不传 level, 让 enqueue 从 payload.severity 推断
        await _enqueue("p0", severity="critical")
        await _enqueue("p3", severity="info")

    _run(go())
    # critical → :critical 流, info → :low 流
    assert len(fake.streams.get("test:tasks:critical", [])) == 1
    assert len(fake.streams.get("test:tasks:low", [])) == 1


# --------- 死信队列 ---------

def test_dead_letter_moves_message_and_acks_original(fake, monkeypatch):
    monkeypatch.setattr(rs.settings, "incident_queue_priority_enabled", True)

    async def go():
        await rs.incident_queue.ensure_group()
        await _enqueue("bad", level="high")
        msg_id, item = (await rs.incident_queue.read_tasks(consumer_name="w1", block_ms=1))[0]
        dlq_id = await rs.incident_queue.dead_letter(
            message_id=msg_id, item=item, reason="超过最大重试次数"
        )
        return msg_id, item["__stream__"], dlq_id

    msg_id, src_stream, dlq_id = _run(go())
    # 进入 DLQ
    dlq = fake.streams["test:tasks:dlq"]
    assert len(dlq) == 1
    assert dlq[0][1]["task_id"] == "bad"
    assert dlq[0][1]["reason"] == "超过最大重试次数"
    assert dlq[0][1]["original_message_id"] == msg_id
    # 原消息在其真实所在的优先级流上被 ACK (不是 base stream)
    assert msg_id not in fake.pending_ids(src_stream, "test-workers")


# --------- 崩溃恢复: XAUTOCLAIM ---------

def test_claim_stale_recovers_crashed_worker_pending(fake, monkeypatch):
    monkeypatch.setattr(rs.settings, "incident_queue_priority_enabled", True)

    async def go():
        await rs.incident_queue.ensure_group()
        await _enqueue("stuck", level="normal")
        # w1 读到任务后"崩溃": 既不 ACK 也不处理, 消息卡在 PEL
        crashed = await rs.incident_queue.read_tasks(consumer_name="w1", block_ms=1)
        # 时间推进, 超过 idle 阈值
        fake.now_ms += 60_000
        # w2 回收 idle > 30s 的 stale pending
        claimed = await rs.incident_queue.claim_stale_tasks(
            consumer_name="w2", min_idle_ms=30_000, count=5
        )
        return crashed, claimed

    crashed, claimed = _run(go())
    assert [t[1]["task_id"] for t in crashed] == ["stuck"]
    assert [t[1]["task_id"] for t in claimed] == ["stuck"]  # w2 接管
    assert claimed[0][1]["__stream__"] == "test:tasks:normal"


def test_claim_skips_fresh_pending(fake, monkeypatch):
    """空闲未超阈值的 pending 不应被抢走, 否则会重复执行正在跑的任务。"""
    monkeypatch.setattr(rs.settings, "incident_queue_priority_enabled", True)

    async def go():
        await rs.incident_queue.ensure_group()
        await _enqueue("running", level="normal")
        await rs.incident_queue.read_tasks(consumer_name="w1", block_ms=1)
        # 只过了 1s, 远小于 30s 阈值
        fake.now_ms += 1_000
        return await rs.incident_queue.claim_stale_tasks(
            consumer_name="w2", min_idle_ms=30_000, count=5
        )

    claimed = _run(go())
    assert claimed == []  # 还在正常处理中, 不回收
