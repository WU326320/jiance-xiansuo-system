"""重试策略：网络 / 超时 / 5xx / 429 指数退避；风控与登录失效绝不重试。"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Callable, TypeVar

from crawler.core.errors import (
    CrawlerError,
    DailyLimitExceeded,
    KeywordEncodingError,
    LoginRequired,
    RetryableError,
    RetryExhausted,
    RiskBlocked,
)

T = TypeVar("T")

_NO_RETRY = (RiskBlocked, LoginRequired, DailyLimitExceeded, KeywordEncodingError)


@dataclass(frozen=True)
class RetryPolicy:
    attempts: int = 3
    backoff_seconds: tuple[float, ...] = (5.0, 15.0, 45.0)
    jitter_seconds: float = 0.0

    def backoff(self, attempt_index: int) -> float:
        if not self.backoff_seconds:
            return 0.0
        index = min(attempt_index, len(self.backoff_seconds) - 1)
        return max(0.0, self.backoff_seconds[index])


def is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, _NO_RETRY):
        return False
    if isinstance(exc, RetryableError):
        return True
    if isinstance(exc, CrawlerError):
        return isinstance(exc, (TimeoutError, ConnectionError))
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    name = type(exc).__name__.lower()
    return any(token in name for token in ("timeout", "connection", "proxyerror"))


def call_with_retry(
    func: Callable[[], T],
    *,
    policy: RetryPolicy,
    sleep: Callable[[float], None] = time.sleep,
    logger=None,
    description: str = "",
    on_attempt: Callable[[int], None] | None = None,
    rng: random.Random | None = None,
) -> T:
    """执行 ``func``，仅在可重试错误上重试。``on_attempt`` 在每次请求前调用。"""
    attempts = max(1, int(policy.attempts))
    rng = rng or random.Random()
    last_error: BaseException | None = None
    for attempt in range(1, attempts + 1):
        if on_attempt is not None:
            on_attempt(attempt)
        try:
            return func()
        except BaseException as exc:  # noqa: BLE001 - 重新抛出非重试异常
            last_error = exc
            if not is_retryable(exc) or attempt >= attempts:
                if attempt >= attempts and is_retryable(exc):
                    if logger:
                        logger.error("重试耗尽 %s attempt=%s error=%s", description, attempt, exc)
                    raise RetryExhausted(f"{description or '请求'}重试耗尽: {exc}") from exc
                raise
            delay = policy.backoff(attempt - 1)
            if policy.jitter_seconds:
                delay += rng.uniform(0, policy.jitter_seconds)
            if logger:
                logger.warning(
                    "请求失败，准备重试 %s attempt=%s/%s delay=%.1fs error=%s",
                    description,
                    attempt,
                    attempts,
                    delay,
                    exc,
                )
            sleep(delay)
    raise RetryExhausted(f"{description or '请求'}重试耗尽: {last_error}")  # pragma: no cover
