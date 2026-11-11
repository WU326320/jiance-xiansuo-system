"""节流与每日请求上限。

- 翻页 / 关键词 / 详情间隔从配置读取，随机区间。
- 每日请求数持久化到 state 目录，跨进程累计；达到上限抛 ``DailyLimitExceeded``。
- 所有 sleep / clock 可注入，便于离线单测。
"""

from __future__ import annotations

import json
import os
import random
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Callable

from crawler.core.errors import DailyLimitExceeded


class DailyBudget:
    def __init__(self, path: str | Path, max_requests: int, date: str):
        self.path = Path(path)
        self.max_requests = int(max_requests)
        if self.max_requests < 0:
            raise ValueError("请求额度必须 >= 0；0 表示由任务截止时间控制总量")
        self.date = date
        self._used = self._load()

    def _load(self) -> int:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(data, dict) or data.get("date") != self.date:
            return 0
        try:
            return max(0, int(data.get("count", 0)))
        except (TypeError, ValueError):
            return 0

    @property
    def used(self) -> int:
        return self._used

    @property
    def remaining(self) -> int | None:
        if self.max_requests == 0:
            return None
        return max(0, self.max_requests - self._used)

    def reserve(self) -> int:
        if self.max_requests and self._used >= self.max_requests:
            raise DailyLimitExceeded(
                f"{self.date} 当日请求数已达上限 {self.max_requests}（{self.path}）"
            )
        self._used += 1
        self._save()
        return self._used

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", suffix=".tmp", dir=self.path.parent
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"date": self.date, "count": self._used}, handle)
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)


class Throttle:
    def __init__(
        self,
        *,
        platform: str,
        date: str,
        state_dir: str | Path,
        daily_max_pages: int,
        requests_per_minute: int = 20,
        page_interval: tuple[float, float] = (4.0, 9.0),
        keyword_interval: tuple[float, float] = (20.0, 40.0),
        detail_interval: tuple[float, float] = (6.0, 12.0),
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        rng: random.Random | None = None,
        budget: DailyBudget | None = None,
    ):
        self.platform = platform
        self.date = date
        self.sleep = sleep
        self.clock = clock
        self.rng = rng or random.Random()
        self.page_interval = _normalize_pair(page_interval)
        self.keyword_interval = _normalize_pair(keyword_interval)
        self.detail_interval = _normalize_pair(detail_interval)
        self.requests_per_minute = max(1, int(requests_per_minute))
        self.budget = budget or DailyBudget(
            Path(state_dir) / "daily" / f"{platform}-{date}.json",
            daily_max_pages,
            date,
        )
        self._last_page_at: float | None = None
        self._last_keyword_at: float | None = None
        self._last_detail_at: float | None = None
        self._recent: deque[float] = deque()

    def _sleep_range(self, interval: tuple[float, float]) -> None:
        low, high = interval
        if high <= 0:
            return
        delay = self.rng.uniform(low, high)
        if delay > 0:
            self.sleep(delay)

    def wait_page_interval(self) -> None:
        if self._last_page_at is not None:
            self._sleep_range(self.page_interval)
        self._last_page_at = self.clock()

    def wait_keyword_interval(self) -> None:
        if self._last_keyword_at is not None:
            self._sleep_range(self.keyword_interval)
        self._last_keyword_at = self.clock()

    def wait_detail_interval(self) -> None:
        if self._last_detail_at is not None:
            self._sleep_range(self.detail_interval)
        self._last_detail_at = self.clock()

    def _respect_rpm(self) -> None:
        now = self.clock()
        while self._recent and now - self._recent[0] >= 60.0:
            self._recent.popleft()
        if len(self._recent) >= self.requests_per_minute:
            wait = 60.0 - (now - self._recent[0]) + 0.01
            if wait > 0:
                self.sleep(wait)
                now = self.clock()
                while self._recent and now - self._recent[0] >= 60.0:
                    self._recent.popleft()
        self._recent.append(now)

    def reserve_request(self) -> int:
        """每次实际 HTTP 请求前调用（含重试），先查每日上限，再限 RPM。"""
        used = self.budget.reserve()
        self._respect_rpm()
        return used

    @property
    def pages_used(self) -> int:
        return self.budget.used


def _normalize_pair(value) -> tuple[float, float]:
    if isinstance(value, (int, float)):
        value = (value, value)
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"节流区间必须是数字或 [min,max]，收到: {value!r}")
    low, high = float(value[0]), float(value[1])
    if low < 0 or high < 0 or low > high:
        raise ValueError(f"节流区间不合法: {value!r}")
    return low, high
