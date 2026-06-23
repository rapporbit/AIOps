"""固定窗口限流器单测 (无真实 Redis)。

用一个最小 FakeRedis 模拟 Redis 的原子语义:
  - eval(): 在单次调用内完成 INCR + (首次)EXPIRE, 对应 _HIT_LUA 的行为;
  - incr()/expire(): 单独命令, 仅用于回归断言 ——
    限流器**不应再**直接调它们 (拆成两条命令正是之前的并发竞态 bug)。

不依赖 pytest-asyncio / fakeredis, 沿用本仓库 asyncio.run 的跑法。
"""

import asyncio

import pytest

from app.core import rate_limiter as rl


def _run(coro):
    return asyncio.run(coro)


class FakeRedis:
    """只实现限流器用到的命令; eval 复刻 Redis 单线程原子执行 _HIT_LUA。"""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttl: dict[str, int] = {}
        self.eval_calls = 0
        self.incr_calls = 0
        self.expire_calls = 0
        self.raise_on_eval = False

    async def eval(self, script, numkeys, *args):
        self.eval_calls += 1
        if self.raise_on_eval:
            raise RuntimeError("boom")
        key = args[0]
        window = int(args[1])
        n = self.store.get(key, 0) + 1
        self.store[key] = n
        if n == 1:  # 首次才设过期, 和脚本一致
            self.ttl[key] = window
        return n

    async def incr(self, key):  # pragma: no cover - 不应被调用
        self.incr_calls += 1
        self.store[key] = self.store.get(key, 0) + 1
        return self.store[key]

    async def expire(self, key, sec):  # pragma: no cover - 不应被调用
        self.expire_calls += 1
        self.ttl[key] = sec


@pytest.fixture
def fake_redis(monkeypatch):
    fr = FakeRedis()

    async def _fake_redis():
        return fr

    monkeypatch.setattr(rl, "_redis", _fake_redis)
    # 固定时间, 让所有请求落在同一个窗口 bucket, 避免跨秒 flaky
    monkeypatch.setattr(rl.time, "time", lambda: 1_000_000.0)
    return fr


def test_allows_up_to_limit_then_blocks(fake_redis):
    async def go():
        results = [await rl.hit("manual", "1.2.3.4", limit=3, window_sec=60) for _ in range(5)]
        return results

    results = _run(go())
    allowed = [ok for ok, _ in results]
    assert allowed == [True, True, True, False, False]
    # 超限时给出本窗口剩余秒数 (1..window)
    _, retry_after = results[-1]
    assert 1 <= retry_after <= 60


def test_uses_atomic_eval_not_split_commands(fake_redis):
    """回归: 必须走单条原子 eval, 不能再拆成 incr + 独立 expire。"""
    async def go():
        for _ in range(4):
            await rl.hit("manual", "ip", limit=2, window_sec=30)

    _run(go())
    assert fake_redis.eval_calls == 4
    assert fake_redis.incr_calls == 0
    assert fake_redis.expire_calls == 0
    # 首次命中即设过期, 杜绝 "INCR 成功但 EXPIRE 丢失 → key 永不过期" 的死锁
    bucket = int(1_000_000.0) // 30
    assert fake_redis.ttl[f"rate_limit:manual:ip:{bucket}"] == 30


def test_new_window_resets_counter(monkeypatch, fake_redis):
    async def go():
        first = await rl.hit("manual", "ip", limit=1, window_sec=60)
        second = await rl.hit("manual", "ip", limit=1, window_sec=60)  # 同窗口 -> 拒
        # 推进到下一个窗口 bucket
        monkeypatch.setattr(rl.time, "time", lambda: 1_000_000.0 + 60)
        third = await rl.hit("manual", "ip", limit=1, window_sec=60)
        return first, second, third

    first, second, third = _run(go())
    assert first[0] is True
    assert second[0] is False
    assert third[0] is True  # 新窗口重新放行


def test_different_identities_isolated(fake_redis):
    async def go():
        a = await rl.hit("manual", "ip-a", limit=1, window_sec=60)
        b = await rl.hit("manual", "ip-b", limit=1, window_sec=60)
        return a, b

    a, b = _run(go())
    assert a[0] is True and b[0] is True  # 不同来源互不影响


def test_concurrent_hits_respect_limit(fake_redis):
    """并发下也只放行 limit 个: 原子 eval 保证计数不被打乱。"""
    async def go():
        return await asyncio.gather(
            *[rl.hit("manual", "ip", limit=5, window_sec=60) for _ in range(20)]
        )

    results = _run(go())
    allowed = sum(1 for ok, _ in results if ok)
    assert allowed == 5


def test_fail_open_when_redis_unavailable(monkeypatch):
    async def _none():
        return None

    monkeypatch.setattr(rl, "_redis", _none)

    async def go():
        return [await rl.hit("manual", "ip", limit=1, window_sec=60) for _ in range(3)]

    results = _run(go())
    assert all(ok for ok, _ in results)  # Redis 挂了不拦截, 放行


def test_fail_open_on_eval_error(fake_redis):
    fake_redis.raise_on_eval = True

    async def go():
        return await rl.hit("manual", "ip", limit=1, window_sec=60)

    ok, retry_after = _run(go())
    assert ok is True and retry_after == 0


def test_non_positive_limit_passes(fake_redis):
    async def go():
        return await rl.hit("manual", "ip", limit=0, window_sec=60)

    ok, _ = _run(go())
    assert ok is True
    assert fake_redis.eval_calls == 0  # limit<=0 直接短路, 不打 Redis


def test_enforce_raises_429_with_retry_after(fake_redis):
    from fastapi import HTTPException

    async def go():
        await rl.enforce("manual", "ip", limit=1, window_sec=60)  # 第一次放行
        await rl.enforce("manual", "ip", limit=1, window_sec=60)  # 第二次超限

    with pytest.raises(HTTPException) as exc_info:
        _run(go())
    exc = exc_info.value
    assert exc.status_code == 429
    assert exc.detail["error"] == "rate_limited"
    assert "Retry-After" in exc.headers
