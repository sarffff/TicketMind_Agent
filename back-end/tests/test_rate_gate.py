"""评估链路的限流闸。

这个类存在的理由是一次真实事故（2026-08-27：3 变体评估吃了 211 次 429，
``max_retries=1`` 早早放弃，整轮 54/54 答案生成为空）。空答案那一轮如果当时的
报告能说"什么都没测"，就不会有人把它读成一次回归；现在它至少有两道保险——
这里保证不再轻易打空，``ticket_behavior`` 保证打空时报告不可信。

测试全用假时钟与假 429，不碰网络。
"""
import asyncio
import time

import httpx
import pytest
from openai import APITimeoutError, RateLimitError

from config import settings
from eval.rate_gate import LLMRateGate


def _gate(monkeypatch, *, interval=0.0, cooldown=0.0, retries=3, transport_retries=2) -> LLMRateGate:
    monkeypatch.setattr(settings, "EVAL_LLM_MIN_INTERVAL_SECONDS", interval)
    monkeypatch.setattr(settings, "EVAL_LLM_RATE_LIMIT_COOLDOWN_SECONDS", cooldown)
    monkeypatch.setattr(settings, "EVAL_LLM_MAX_RATE_RETRIES", retries)
    monkeypatch.setattr(settings, "EVAL_LLM_MAX_TRANSPORT_RETRIES", transport_retries)
    return LLMRateGate()


def _stub_sleeps(monkeypatch) -> list[float]:
    """把闸的等待换成"记下时长但立刻返回"，否则测一次退避要真等 7 秒。

    必须先抓住原函数再替换：lambda 里直接写 ``asyncio.sleep(0)`` 拿到的是替换之后的
    那一个，会自己递归下去。
    """
    waited: list[float] = []

    async def fake_sleep(delay: float, *args: object, **kwargs: object) -> None:
        waited.append(delay)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    return waited


def _rate_limited(retry_after: str | None = None) -> RateLimitError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    response = httpx.Response(429, headers=headers, request=request)
    return RateLimitError("rate limited", response=response, body=None)


def test_相邻调用被隔开(monkeypatch):
    """闸保证的是"两次**实际发出**的调用至少隔开 min_interval"，所以打点必须打在
    被包的那个函数里。打在 call() 之前测到的是预留时刻，第一条间隔天然是 0——
    间隔是在预留之后、调用之前等的。
    """
    gate = _gate(monkeypatch, interval=0.05)
    stamps: list[float] = []

    async def call_happens() -> None:
        stamps.append(time.monotonic())

    async def probe() -> None:
        for _ in range(3):
            await gate.call(call_happens)

    asyncio.run(probe())
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(gaps) == 2
    assert all(gap >= 0.04 for gap in gaps), gaps


def test_撞429按服务端建议时长退避后重试成功(monkeypatch):
    """SDK 自带的短退避在分钟级配额窗口面前约等于不退避，所以必须自己等。"""
    waited = _stub_sleeps(monkeypatch)
    gate = _gate(monkeypatch, retries=5)

    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _rate_limited(retry_after="7")
        return "答案"

    assert asyncio.run(gate.call(lambda: flaky())) == "答案"
    assert attempts["n"] == 3
    assert 7.0 in waited, "服务端给了 Retry-After 就该听它的，而不是用本地缺省值"


def test_服务端没给建议时长时用本地冷却(monkeypatch):
    waited = _stub_sleeps(monkeypatch)
    gate = _gate(monkeypatch, cooldown=11.0, retries=2)

    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise _rate_limited()
        return "ok"

    asyncio.run(gate.call(lambda: flaky()))
    assert 11.0 in waited


def test_重试耗尽后原样抛出(monkeypatch):
    """吞掉 429 会让一次空运行看起来像"模型变差了"——那是最难查的那种错。"""
    gate = _gate(monkeypatch, retries=2)
    _stub_sleeps(monkeypatch)

    async def always_limited() -> str:
        raise _rate_limited()

    with pytest.raises(RateLimitError):
        asyncio.run(gate.call(always_limited))


def test_space只排间隔不发起调用也不吞异常(monkeypatch):
    """流式那条路用 space 而不是 call：一条已经开始吐字的流不能在半途重放，
    否则同一个回合会被拼两遍。间隔照样要排，突发正是打穿配额窗口的原因。"""
    gate = _gate(monkeypatch, interval=0.05)
    calls = {"n": 0}

    async def probe() -> None:
        await gate.space()
        await gate.space()

    started = time.monotonic()
    asyncio.run(probe())
    assert time.monotonic() - started >= 0.04, "space 没有真的隔开"
    assert calls["n"] == 0


def _timed_out() -> APITimeoutError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    return APITimeoutError(request=request)


def test_超时也重试(monkeypatch):
    """之前只有 429 会重试，超时直接抛。一轮 20 条就是这么被截断掉 7~8 条的
    （2026-10-17 r2/r3），而**被截断的推理循环只会少提工具**——于是
    "模型有多激进"这条轴量到的是一台半死网络上的模型。
    """
    _stub_sleeps(monkeypatch)
    gate = _gate(monkeypatch, transport_retries=3)
    attempts = {"n": 0}

    async def flaky() -> str:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise _timed_out()
        return "答案"

    assert asyncio.run(gate.call(lambda: flaky())) == "答案"
    assert attempts["n"] == 3


def test_超时的重试次数比429小得多(monkeypatch):
    """429 是"等一会儿就有"，超时是"这一条请求本身就慢"。按 12 次 x 8 秒等下去，
    一个真死的 provider 能把整轮评估拖成小时级，而结论还是零。
    """
    waited = _stub_sleeps(monkeypatch)
    gate = _gate(monkeypatch, cooldown=8.0, retries=12, transport_retries=2)
    attempts = {"n": 0}

    async def always_timeout() -> str:
        attempts["n"] += 1
        raise _timed_out()

    with pytest.raises(APITimeoutError):
        asyncio.run(gate.call(always_timeout))
    assert attempts["n"] == 3, "1 次首发 + 2 次重试；不能蹭 429 那 12 次"
    assert all(w < 8.0 for w in waited), f"超时不该按分钟级配额窗口等：{waited}"
