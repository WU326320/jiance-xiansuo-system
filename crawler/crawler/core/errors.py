"""采集层异常类型。

风控 / 登录失效是“一等错误”：不重试、不自动打码，由 runner 立即停止该平台。
"""

from __future__ import annotations


class CrawlerError(Exception):
    """采集层可预期错误的基类。"""


class ConfigError(CrawlerError):
    """配置缺失或不合法。"""


class RiskBlocked(CrawlerError):
    """出现验证码 / 滑块 / 访问受限。不做任何破解，立即停止平台。"""

    def __init__(
        self,
        platform: str,
        reason: str,
        *,
        evidence: str = "",
        screenshot_path: str | None = None,
    ):
        self.platform = platform
        self.reason = reason
        self.evidence = evidence
        self.screenshot_path = screenshot_path
        super().__init__(f"{platform} 触发风控: {reason}")


class LoginRequired(CrawlerError):
    """登录态缺失或失效。"""

    def __init__(
        self, platform: str, reason: str, *, screenshot_path: str | None = None
    ):
        self.platform = platform
        self.reason = reason
        self.screenshot_path = screenshot_path
        super().__init__(f"{platform} 登录失效: {reason}")


class DailyLimitExceeded(CrawlerError):
    """当日请求数达到配置上限。"""


class KeywordEncodingError(CrawlerError):
    """关键词无法按平台要求的编码编码。"""


class RetryableError(CrawlerError):
    """可重试的网络 / 5xx / 429 错误。"""

    def __init__(self, message: str, *, status: int | None = None):
        self.status = status
        super().__init__(message)


class RetryExhausted(CrawlerError):
    """重试次数耗尽。"""


class BrowserUnavailable(CrawlerError):
    """Scrapling / 浏览器无法启动。"""


class PaginationError(CrawlerError):
    """分页或列表加载未确认；不能将未采集页标记为完成。"""


class InvalidItem(CrawlerError):
    """解析结果不满足最低输出要求。"""
