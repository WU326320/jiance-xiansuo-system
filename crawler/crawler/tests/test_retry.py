from __future__ import annotations

import pytest

from crawler.core.errors import RetryableError, RetryExhausted, RiskBlocked
from crawler.core.retry import RetryPolicy, call_with_retry, is_retryable


def test_retry_succeeds_after_failures():
    calls = []
    attempts = []
    sleeps = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise RetryableError("HTTP 503", status=503)
        return "ok"

    result = call_with_retry(
        flaky,
        policy=RetryPolicy(attempts=3, backoff_seconds=(5, 15, 45)),
        sleep=sleeps.append,
        on_attempt=attempts.append,
        description="test",
    )
    assert result == "ok"
    assert len(calls) == 3
    assert attempts == [1, 2, 3]
    assert sleeps == [5, 15]


def test_retry_exhausted():
    def always_fail():
        raise RetryableError("HTTP 500", status=500)

    with pytest.raises(RetryExhausted):
        call_with_retry(always_fail, policy=RetryPolicy(attempts=2, backoff_seconds=(1,)), sleep=lambda _: None)


def test_risk_and_login_never_retry():
    calls = []

    def risk():
        calls.append(1)
        raise RiskBlocked("taobao", "验证码")

    with pytest.raises(RiskBlocked):
        call_with_retry(risk, policy=RetryPolicy(attempts=3, backoff_seconds=(1,)), sleep=lambda _: None)
    assert len(calls) == 1


def test_non_retryable_exception_propagates():
    with pytest.raises(ValueError):
        call_with_retry(lambda: (_ for _ in ()).throw(ValueError("bad")), policy=RetryPolicy(attempts=3))


def test_classification():
    assert is_retryable(RetryableError("x"))
    assert is_retryable(TimeoutError("x"))
    assert is_retryable(ConnectionError("x"))
    assert not is_retryable(RiskBlocked("taobao", "x"))
    assert not is_retryable(ValueError("x"))
