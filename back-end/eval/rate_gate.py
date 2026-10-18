"""评估链路的 LLM 限流闸。从 ``runner.py`` 提出来，因为工单行为评估也要它。

原来它是 ``runner.py`` 的私有类，文档写着"只在这一个文件里用"——那句话现在错了，
而且错的方式很有代表性：``ticket_behavior.py`` 用的是裸适配器，第一次撞 429 就
让整条图降级成"仅规则"，于是一份 12/12 全限流的运行被打印成「通过 9/12」。
两条评估链路共用一份节流才是本意；复制一份的话，将来只有一条会被修。
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, TypeVar

from openai import APIConnectionError, APITimeoutError, RateLimitError

from config import settings

logger = logging.getLogger("eval.rate_gate")

_T = TypeVar("_T")


class LLMRateGate:
    """防突发 + 撞 429 按配额窗口退避后重试。

    ```
    completion = await gate.call(lambda: adapter.complete(...))
    verdict    = await gate.call(lambda: judge.judge(...))
    ```

    为什么评估需要这个,而线上不需要:线上每次回答之间隔着真实用户的输入节奏,
    突发天然被摊开;评估是一台脚本在几分钟内把几百次调用打出去,``eval_answer``
    和 ``judge`` 又共用同一个账号的配额——2026-08-27 那轮 3 变体评估因此吃了
    **211 次 HTTP 429**。SDK 自带的指数退避(0.5s→1s→2s…)在分钟级配额窗口面前
    约等于不退避,退避完还是 429,于是 ``max_retries=1`` 很快就放弃,
    直接导致 rerank-api 整轮 54/54 答案生成为空。

    只给评估用,不影响线上路径:

    - ``min_interval`` 强制相邻 LLM 调用至少隔开这么长时间,防突发;
    - 撞上 ``RateLimitError`` 时按 ``Retry-After``(缺省则退 ``cooldown``)
      退避后重试,最多 ``max_retries`` 次,把分钟窗口让过去再继续。
    """

    def __init__(self) -> None:
        self._min_interval = settings.EVAL_LLM_MIN_INTERVAL_SECONDS
        self._cooldown = settings.EVAL_LLM_RATE_LIMIT_COOLDOWN_SECONDS
        self._max_retries = settings.EVAL_LLM_MAX_RATE_RETRIES
        self._max_transport_retries = settings.EVAL_LLM_MAX_TRANSPORT_RETRIES
        # 超时的回退基数：比 429 那档短得多（429 是"等一会儿就有"，超时是"这次请求本身慢")
        self._transport_backoff = min(settings.EVAL_LLM_RATE_LIMIT_COOLDOWN_SECONDS, 3.0)
        self._last_start = 0.0
        self._lock = asyncio.Lock()

    @staticmethod
    def _retry_after(exc: Exception) -> float:
        """从 429 响应头里读服务端建议的等待秒数。没有则返回 0。"""
        headers = getattr(getattr(exc, "response", None), "headers", None) or {}
        try:
            return float(headers.get("retry-after", 0))
        except (TypeError, ValueError):
            return 0.0

    async def _reserve(self) -> float:
        """给下一次调用占一个时间槽，返回还要等多久。"""
        async with self._lock:
            now = time.monotonic()
            delay = self._last_start + self._min_interval - now
            self._last_start = now + max(delay, 0.0)
        return delay

    async def space(self) -> None:
        """只排间隔、不做重试。

        给流式调用用：一条已经开始吐字的流没法在半途安全重放，重试会把同一个
        回合的内容拼两遍。但间隔照样要排——突发正是把配额窗口打穿的原因。
        """
        delay = await self._reserve()
        if delay > 0:
            await asyncio.sleep(delay)

    async def call(self, fn: Callable[[], Awaitable[_T]]) -> _T:
        """执行一次受节流的 LLM 调用。撞 429 按窗口退避后重试；超时/断连也重试，但次数小得多。

        超时那一档原来不重试——表现是整轮评估里一大半工单被 `APITimeoutError` 截断
        （2026-10-17 r2/r3 各 7~8/20 条），而**被截断的推理循环只会少提工具**，
        于是"模型有多激进"这条轴读到的是一台半死网络上的模型。重试两次就能把
        一次抖动和一种 provider 病分开。
        """
        for attempt in range(self._max_retries + 1):
            delay = await self._reserve()
            if delay > 0:
                await asyncio.sleep(delay)
            try:
                return await fn()
            except RateLimitError as exc:
                if attempt >= self._max_retries:
                    raise
                wait = max(self._retry_after(exc), self._cooldown)
                logger.warning(
                    "LLM rate limited (attempt %s/%s); backing off %.1fs [Retry-After=%.1fs]",
                    attempt + 1,
                    self._max_retries,
                    wait,
                    self._retry_after(exc),
                )
                await asyncio.sleep(wait)
            except (APITimeoutError, APIConnectionError) as exc:
                if attempt >= self._max_transport_retries:
                    raise
                wait = self._transport_backoff * (attempt + 1)
                logger.warning(
                    "LLM transport failed (attempt %s/%s): %s; retrying in %.1fs",
                    attempt + 1,
                    self._max_transport_retries,
                    type(exc).__name__,
                    wait,
                )
                await asyncio.sleep(wait)
        raise RuntimeError("unreachable")
