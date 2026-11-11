from __future__ import annotations

import random

import pytest

from crawler.core.errors import DailyLimitExceeded
from crawler.core.throttle import DailyBudget, Throttle, _normalize_pair
from crawler.tests.conftest import assert_private_mode


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def clock(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def _throttle(tmp_path, fake, **kwargs):
    defaults = dict(
        platform="taobao",
        date="2026-11-05",
        state_dir=tmp_path,
        daily_max_pages=10,
        requests_per_minute=100,
        page_interval=(4, 9),
        keyword_interval=(20, 40),
        detail_interval=(6, 12),
        sleep=fake.sleep,
        clock=fake.clock,
        rng=random.Random(1),
    )
    defaults.update(kwargs)
    return Throttle(**defaults)


def test_page_keyword_detail_intervals(tmp_path):
    fake = FakeTime()
    throttle = _throttle(tmp_path, fake)
    throttle.wait_keyword_interval()
    assert fake.sleeps == []  # 第一次不等待
    throttle.wait_keyword_interval()
    assert 20 <= fake.sleeps[-1] <= 40
    fake.sleeps.clear()
    throttle.wait_page_interval()
    assert fake.sleeps == []  # 第一次翻页不等待
    throttle.wait_page_interval()
    assert 4 <= fake.sleeps[-1] <= 9
    fake.sleeps.clear()
    throttle.wait_detail_interval()
    assert fake.sleeps == []
    throttle.wait_detail_interval()
    assert 6 <= fake.sleeps[-1] <= 12


def test_daily_budget_persists_and_limits(tmp_path):
    fake = FakeTime()
    throttle = _throttle(tmp_path, fake, daily_max_pages=2)
    assert throttle.reserve_request() == 1
    assert throttle.reserve_request() == 2
    with pytest.raises(DailyLimitExceeded):
        throttle.reserve_request()
    again = _throttle(tmp_path, fake, daily_max_pages=2)
    assert again.pages_used == 2
    with pytest.raises(DailyLimitExceeded):
        again.reserve_request()


def test_daily_budget_new_date_resets(tmp_path):
    path = tmp_path / "daily.json"
    first = DailyBudget(path, 2, "2026-11-05")
    first.reserve()
    second = DailyBudget(path, 2, "2026-11-06")
    assert second.used == 0
    assert_private_mode(path, 0o600)


def test_rpm_waits(tmp_path):
    fake = FakeTime()
    throttle = _throttle(tmp_path, fake, requests_per_minute=2, daily_max_pages=10)
    throttle.reserve_request()
    throttle.reserve_request()
    assert fake.sleeps == []
    throttle.reserve_request()
    assert any(59 <= value <= 61 for value in fake.sleeps)


def test_normalize_pair():
    assert _normalize_pair(3) == (3.0, 3.0)
    assert _normalize_pair([1, 2]) == (1.0, 2.0)
    with pytest.raises(ValueError):
        _normalize_pair([3, 1])
