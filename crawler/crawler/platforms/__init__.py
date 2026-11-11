"""平台注册表。平台模块只在真正创建时导入浏览器依赖。"""

from __future__ import annotations

from typing import Callable

from crawler.config import AppConfig
from crawler.core.stats import RunStats
from crawler.core.throttle import Throttle
from crawler.platforms.ali1688 import Ali1688Platform
from crawler.platforms.base import PlatformBase
from crawler.platforms.jd import JdPlatform
from crawler.platforms.taobao import TaobaoPlatform

PLATFORM_NAMES = ("taobao", "jd", "1688")

_REGISTRY: dict[str, type[PlatformBase]] = {
    "taobao": TaobaoPlatform,
    "jd": JdPlatform,
    "1688": Ali1688Platform,
}


def create_platform(
    name: str,
    config: AppConfig,
    browser,
    *,
    throttle: Throttle | None = None,
    stats: RunStats | None = None,
    logger=None,
    date: str | None = None,
) -> PlatformBase:
    """创建平台采集器；``name`` 只接受 taobao/jd/1688。"""
    try:
        cls: Callable[..., PlatformBase] = _REGISTRY[name]
    except KeyError as exc:  # pragma: no cover - argparse 已限制
        raise ValueError(f"未知平台: {name}") from exc
    return cls(
        config,
        browser,
        throttle=throttle,
        stats=stats,
        logger=logger,
        date=date,
    )


__all__ = ["PLATFORM_NAMES", "PlatformBase", "create_platform"]
