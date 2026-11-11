"""配置加载与校验。

节流、重试、输出/日志/profile 路径在 YAML 中；采集和补采不设数量上限。
旧数量配置键保留解析兼容，但不再控制采集结束。
"""

from __future__ import annotations

import hashlib
import os
import random
import stat
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from crawler.core.errors import ConfigError

PLATFORM_NAMES = ("taobao", "jd", "1688")


def _pair(value: Any) -> tuple[float, float]:
    if isinstance(value, (int, float)):
        value = [value, value]
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("必须是数字或 [min,max]")
    low, high = float(value[0]), float(value[1])
    if low < 0 or high < 0 or low > high:
        raise ValueError("必须满足 0 <= min <= max")
    return low, high


class RateLimitSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    requests_per_minute: int = 20
    page_interval_seconds: tuple[float, float] = (4.0, 9.0)
    keyword_interval_seconds: tuple[float, float] = (20.0, 40.0)
    detail_interval_seconds: tuple[float, float] = (6.0, 12.0)

    @field_validator("requests_per_minute")
    @classmethod
    def _rpm(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError("requests_per_minute 必须 >= 1")
        return int(value)

    @field_validator(
        "page_interval_seconds",
        "keyword_interval_seconds",
        "detail_interval_seconds",
        mode="before",
    )
    @classmethod
    def _intervals(cls, value: Any) -> tuple[float, float]:
        return _pair(value)


class RetrySettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    attempts: int = 3
    backoff_seconds: tuple[float, ...] = (5.0, 15.0, 45.0)
    jitter_seconds: float = 0.0

    @field_validator("attempts")
    @classmethod
    def _attempts(cls, value: int) -> int:
        if int(value) < 1:
            raise ValueError("retry.attempts 必须 >= 1")
        return int(value)

    @field_validator("backoff_seconds", mode="before")
    @classmethod
    def _backoff(cls, value: Any) -> tuple[float, ...]:
        if isinstance(value, (int, float)):
            value = [value]
        if not isinstance(value, (list, tuple)) or not value:
            raise ValueError("backoff_seconds 必须是非空数组")
        return tuple(max(0.0, float(item)) for item in value)


class ImageSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    max_bytes: int = 15 * 1024 * 1024
    timeout_seconds: float = 20.0


class KeywordSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword: str
    ip_code: str | None = None

    @field_validator("keyword")
    @classmethod
    def _not_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("keyword 不能为空")
        return value


class TaobaoRateLimitSettings(RateLimitSettings):
    """淘宝/天猫专用节流（默认比全局更慢；其他平台继续用 ``crawl.rate_limit``）。

    2026-11-06d 降频：翻页下限 12→30 秒、关键词下限 60→180 秒、详情下限
    15→45 秒、RPM 8→3。所有值都可在 YAML 的 ``crawl.taobao.rate_limit`` 调整。
    """

    requests_per_minute: int = 3
    page_interval_seconds: tuple[float, float] = (30.0, 50.0)
    keyword_interval_seconds: tuple[float, float] = (180.0, 300.0)
    detail_interval_seconds: tuple[float, float] = (45.0, 90.0)


class TaobaoBatchSettings(BaseModel):
    """淘宝分时段分批：关键词按 ``size`` 个一批，批间休息 ``rest_seconds``。

    旧分段数量字段仅保留配置兼容，不再截断本批关键词的页面或商品。
    """

    model_config = ConfigDict(extra="allow")

    size: int = 4
    rest_seconds: float = 1800.0
    segments_per_day: int = 0
    max_pages_per_segment: int = 0
    max_items_per_segment: int = 0

    @field_validator("size")
    @classmethod
    def _size(cls, value: int) -> int:
        value = int(value)
        if value < 1:
            raise ValueError("crawl.taobao.batch.size 必须 >= 1")
        return value

    @field_validator(
        "segments_per_day", "max_pages_per_segment", "max_items_per_segment"
    )
    @classmethod
    def _segment_positive(cls, value: int) -> int:
        value = int(value)
        if value < 0:
            raise ValueError("crawl.taobao.batch 兼容数量字段必须 >= 0")
        return value

    @field_validator("rest_seconds")
    @classmethod
    def _rest(cls, value: float) -> float:
        value = float(value)
        if value < 0:
            raise ValueError("crawl.taobao.batch.rest_seconds 必须 >= 0")
        return value


class TaobaoDetailSettings(BaseModel):
    """淘宝详情业务筛选参数；补采不截断数量。

    ``detail_min_text_score`` 与判定模块的 ``fetch.taobao.detail_min_text_score``
    同名，两者都写进配置；采集层只读这里。``allow_unscored`` 仅用于人工/后端
    显式传 ID 且当天没有列表 JSONL 可打分的情况。
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    detail_min_text_score: float = 60.0
    max_per_run: int = 0  # 旧配置兼容字段，不再限制补采数量。
    # 旧开关保留兼容；工作台逐页补采覆盖全部缺字段商品。
    complete_missing_amount: bool = False
    filter_by_score: bool = True
    # 有列表数据但 ID 不在其中（分数未知）时默认跳过；只有当天完全没有
    # 列表 JSONL 的人工/后端显式 ID 才按“人工指定”保留。
    allow_unscored: bool = False

    @field_validator("detail_min_text_score")
    @classmethod
    def _score(cls, value: float) -> float:
        value = float(value)
        if value < 0:
            raise ValueError("crawl.taobao.detail.detail_min_text_score 必须 >= 0")
        return value

    @field_validator("max_per_run")
    @classmethod
    def _max_per_run(cls, value: int) -> int:
        value = int(value)
        if value < 0:
            raise ValueError("crawl.taobao.detail.max_per_run 必须 >= 0；0表示全部补采")
        return value


class Ali1688ShopNameBackfillSettings(BaseModel):
    """1688 候选店名补抓（列表缺店名时按详情页/店铺页补齐）。

    按缺失字段逐条补抓，复用 ``crawl.rate_limit.detail_interval_seconds``，
    不截断候选数量，不改变 1688 原有的翻页/关键词节奏。
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    max_per_run: int = 0  # 旧配置兼容字段，不再限制补采数量。
    # auto = 先详情页、失败再店铺页；detail/shop 可强制单一来源。
    source: Literal["auto", "detail", "shop"] = "auto"

    @field_validator("max_per_run")
    @classmethod
    def _max_per_run(cls, value: int) -> int:
        value = int(value)
        if value < 0:
            raise ValueError(
                "crawl.ali1688.shop_name_backfill.max_per_run 必须 >= 0；0表示全部补采"
            )
        return value


class Ali1688Settings(BaseModel):
    """1688 专用参数（滚动节奏与店名补抓）。"""

    model_config = ConfigDict(extra="allow")

    scroll_steps: int = 3
    scroll_wait_ms: int = 600
    shop_name_backfill: Ali1688ShopNameBackfillSettings = Field(
        default_factory=Ali1688ShopNameBackfillSettings
    )


class TaobaoRiskSettings(BaseModel):
    """淘宝疑似频控：连续空结果达到阈值立即停，不重试。"""

    model_config = ConfigDict(extra="allow")

    empty_result_streak: int = 2

    @field_validator("empty_result_streak")
    @classmethod
    def _streak(cls, value: int) -> int:
        value = int(value)
        if value < 1:
            raise ValueError("crawl.taobao.risk.empty_result_streak 必须 >= 1")
        return value


class TaobaoSettings(BaseModel):
    """淘宝/天猫降频参数（仅淘宝生效，不改京东/1688）。"""

    model_config = ConfigDict(extra="allow")

    max_keywords_per_run: int = 0
    max_pages: int = 0
    rate_limit: TaobaoRateLimitSettings = Field(default_factory=TaobaoRateLimitSettings)
    batch: TaobaoBatchSettings = Field(default_factory=TaobaoBatchSettings)
    detail: TaobaoDetailSettings = Field(default_factory=TaobaoDetailSettings)
    risk: TaobaoRiskSettings = Field(default_factory=TaobaoRiskSettings)

    @field_validator("max_keywords_per_run", "max_pages")
    @classmethod
    def _positive(cls, value: int) -> int:
        value = int(value)
        if value < 0:
            raise ValueError("crawl.taobao 兼容数量字段必须 >= 0")
        return value


class DetailSettings(BaseModel):
    """详情补抓开关（列表采集不受影响）。

    ``platforms`` 为按平台覆盖；2026-11-06 验证轮 1688 详情默认停用：
    1688 搜索页仍照常采集列表，只是不再进入详情页（历史轮次命中
    ``risk_blocked``）。需要补抓时显式写 ``platforms.1688: true``。
    """

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    platforms: dict[str, bool] = Field(default_factory=lambda: {"1688": False})
    # 详情页原始响应留存：只对列出的平台、只存前 N 个详情页，供定位评论字段。
    debug_capture_limit: int = 2
    debug_capture_platforms: list[str] = Field(default_factory=lambda: ["taobao"])
    detail_wait_ms_range: tuple[float, float] | None = None

    @field_validator("debug_capture_limit")
    @classmethod
    def _debug_limit(cls, value: int) -> int:
        value = int(value)
        if value < 0:
            raise ValueError("crawl.detail.debug_capture_limit 必须 >= 0")
        return value

    @field_validator("detail_wait_ms_range", mode="before")
    @classmethod
    def _detail_wait_range(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        return _pair(value)

    def platform_enabled(self, platform: str) -> bool:
        return bool(self.platforms.get(platform, self.enabled))


class CrawlSettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    # 旧配置键保留加载兼容；列表、关键词、每日请求都不按总量截断。
    max_pages: int = 0
    daily_max_pages: int = 0
    max_items_per_keyword: int = 0
    rate_limit: RateLimitSettings = Field(default_factory=RateLimitSettings)
    retry: RetrySettings = Field(default_factory=RetrySettings)
    image: ImageSettings = Field(default_factory=ImageSettings)
    detail: DetailSettings = Field(default_factory=DetailSettings)
    taobao: TaobaoSettings = Field(default_factory=TaobaoSettings)
    ali1688: Ali1688Settings = Field(default_factory=Ali1688Settings)
    timeout_ms: int = 30000
    page_wait_ms: int = 1200
    # 页面停留时间随机区间；未配置时退回固定 page_wait_ms。
    page_wait_ms_range: tuple[float, float] | None = None
    parallel_platforms: bool = False
    timezone: str = "Asia/Shanghai"
    headless: bool = True
    real_chrome: bool = False
    output_dir: str = "./out"
    log_dir: str = "./logs"
    state_dir: str = "./state"
    keywords: list[KeywordSpec] = Field(default_factory=list)
    keyword_hit_warn_ratio: float = 0.2
    jd_scroll_steps: int = 6
    jd_scroll_wait_ms: int = 800
    jd_price_wait_ms: int = 6000

    def rate_limit_for(self, platform: str):
        """该平台实际节流参数：淘宝用专用降频配置，其余平台保持原值。"""
        return self.taobao.rate_limit if platform == "taobao" else self.rate_limit

    def max_pages_for(self, platform: str) -> int:
        """旧调用兼容：0 表示不按页数截断，所有平台逐页采到真实末页。"""
        return 0

    @field_validator("max_pages", "max_items_per_keyword")
    @classmethod
    def _positive(cls, value: int) -> int:
        if int(value) < 0:
            raise ValueError("兼容数量字段必须 >= 0")
        return int(value)

    @field_validator("daily_max_pages")
    @classmethod
    def _daily_budget(cls, value: int) -> int:
        if int(value) < 0:
            raise ValueError("daily_max_pages 必须 >= 0；0表示不按数量提前结束")
        return int(value)

    @field_validator(
        "timeout_ms", "page_wait_ms", "jd_scroll_wait_ms", "jd_price_wait_ms"
    )
    @classmethod
    def _non_negative(cls, value: int) -> int:
        if int(value) < 0:
            raise ValueError("必须 >= 0")
        return int(value)

    @field_validator("page_wait_ms_range", mode="before")
    @classmethod
    def _page_wait_range(cls, value: Any) -> Any:
        if value in (None, ""):
            return None
        return _pair(value)

    def wait_ms(self, rng: random.Random | None = None) -> int:
        """随机页面停留毫秒数（配置区间优先）。"""
        if self.page_wait_ms_range is None:
            return int(self.page_wait_ms)
        generator = rng or random.Random()
        low, high = self.page_wait_ms_range
        return int(generator.uniform(low, high))

    @field_validator("keyword_hit_warn_ratio")
    @classmethod
    def _ratio(cls, value: float) -> float:
        value = float(value)
        if not 0 <= value <= 1:
            raise ValueError("keyword_hit_warn_ratio 必须在 0..1")
        return value

    @field_validator("timezone")
    @classmethod
    def _timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(str(value))
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"未知时区: {value}") from exc
        return str(value)

    @field_validator("keywords", mode="before")
    @classmethod
    def _keywords(cls, value: Any) -> Any:
        if value is None:
            return []
        if not isinstance(value, list):
            raise ValueError("crawl.keywords 必须是数组")
        normalized: list[Any] = []
        for entry in value:
            if isinstance(entry, str):
                normalized.append({"keyword": entry})
            elif isinstance(entry, dict):
                normalized.append(entry)
            else:
                raise ValueError("crawl.keywords 元素必须是字符串或 {keyword,ip_code}")
        return normalized


class PlatformToggle(BaseModel):
    model_config = ConfigDict(extra="allow")

    enabled: bool = True


class IpSpec(BaseModel):
    model_config = ConfigDict(extra="allow")

    code: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    variants: list[str] = Field(default_factory=list)


class SecuritySettings(BaseModel):
    model_config = ConfigDict(extra="allow")

    profile_dir: str = "./profiles/{platform}"
    profile_key_env: str = "CLUE_PROFILE_KEY"


class BrowserSettings(BaseModel):
    """浏览器启动方式。

    - ``playwright``：Scrapling/Playwright 自带浏览器 + 持久化 profile（原行为）。
    - ``cdp``：连接系统已安装的真实 Chrome（``connect_over_cdp``）；未运行时按
      相同参数自动启动，不附加任何自动化启动参数。

    ``mode`` 是全局默认；``platform_modes`` 按平台覆盖。京东默认 ``cdp``，
    显式写 ``platform_modes.jd: playwright`` 可退回原模式。
    """

    model_config = ConfigDict(extra="allow")

    mode: Literal["playwright", "cdp"] = "playwright"
    platform_modes: dict[str, Literal["playwright", "cdp"]] = Field(
        default_factory=dict
    )
    port: int = 9222
    ports: dict[str, int] = Field(default_factory=dict)
    display: str | None = ":6"
    chrome_executable: str | None = None
    startup_timeout_seconds: float = 20.0
    poll_interval_seconds: float = 0.2

    @field_validator("port")
    @classmethod
    def _port(cls, value: int) -> int:
        value = int(value)
        if not 1 <= value <= 65535:
            raise ValueError("browser.port 必须在 1..65535")
        return value

    @field_validator("ports")
    @classmethod
    def _ports(cls, value: dict[str, int]) -> dict[str, int]:
        result: dict[str, int] = {}
        for platform, port in (value or {}).items():
            port = int(port)
            if not 1 <= port <= 65535:
                raise ValueError(f"browser.ports.{platform} 必须在 1..65535")
            result[str(platform)] = port
        return result

    @field_validator("startup_timeout_seconds", "poll_interval_seconds")
    @classmethod
    def _positive_seconds(cls, value: float) -> float:
        value = float(value)
        if value <= 0:
            raise ValueError("browser 超时/轮询间隔必须 > 0")
        return value

    @model_validator(mode="after")
    def _ensure_platform_modes(self) -> "BrowserSettings":
        # 京东默认 CDP；显式 platform_modes.jd 优先。其余平台继承 browser.mode。
        self.platform_modes.setdefault("jd", "cdp")
        for platform in PLATFORM_NAMES:
            if platform in self.ports:
                continue
            port = self.port + PLATFORM_NAMES.index(platform)
            if not 1 <= port <= 65535:
                raise ValueError(
                    f"browser.port={self.port} 为 {platform} 推导出的端口 {port} 越界（1..65535）"
                )
        return self


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    config_version: str | None = None
    caliber_version: str | None = None
    platforms: dict[str, PlatformToggle] = Field(default_factory=dict)
    ips: list[IpSpec] = Field(default_factory=list)
    crawl: CrawlSettings = Field(default_factory=CrawlSettings)
    security: SecuritySettings = Field(default_factory=SecuritySettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    config_path: Path | None = Field(default=None, exclude=True)
    config_sha256: str | None = Field(default=None, exclude=True)

    @model_validator(mode="after")
    def _ensure_defaults(self) -> "AppConfig":
        for name in PLATFORM_NAMES:
            self.platforms.setdefault(name, PlatformToggle(enabled=True))
        if not self.config_version:
            digest = self.config_sha256 or ""
            self.config_version = digest[:12] if digest else "unversioned"
        if not self.caliber_version:
            self.caliber_version = "v1.0"
        return self

    def enabled_platforms(self) -> list[str]:
        return [
            name
            for name in PLATFORM_NAMES
            if self.platforms.get(name, PlatformToggle()).enabled
        ]

    def platform_enabled(self, name: str) -> bool:
        toggle = self.platforms.get(name)
        return bool(toggle and toggle.enabled)

    def resolve_path(self, value: str | Path) -> Path:
        text = os.path.expandvars(os.path.expanduser(str(value)))
        path = Path(text)
        if not path.is_absolute():
            path = Path.cwd() / path
        return path

    @property
    def output_root(self) -> Path:
        return self.resolve_path(
            os.environ.get("CRAWLER_OUTPUT_DIR", self.crawl.output_dir)
        )

    @property
    def log_root(self) -> Path:
        return self.resolve_path(os.environ.get("CRAWLER_LOG_DIR", self.crawl.log_dir))

    @property
    def state_root(self) -> Path:
        return self.resolve_path(
            os.environ.get("CRAWLER_STATE_DIR", self.crawl.state_dir)
        )

    def browser_mode(self, platform: str) -> str:
        """该平台实际使用的浏览器模式（平台覆盖 > 全局默认）。"""
        return self.browser.platform_modes.get(platform) or self.browser.mode

    def chrome_port(self, platform: str) -> int:
        """CDP 端口：显式 ``browser.ports`` 优先，否则 ``port + 平台序号``。"""
        explicit = self.browser.ports.get(platform)
        if explicit is not None:
            return int(explicit)
        try:
            index = PLATFORM_NAMES.index(platform)
        except ValueError:
            index = 0
        return int(self.browser.port) + index

    def cdp_endpoint(self, platform: str) -> str:
        return f"http://127.0.0.1:{self.chrome_port(platform)}"

    def _profile_path(self, platform: str) -> Path:
        """按配置解析平台 profile 路径，不创建目录。"""
        env_base = os.environ.get("CRAWLER_PROFILES_DIR")
        if env_base:
            return self.resolve_path(env_base) / platform
        template = self.security.profile_dir
        if "{platform}" not in template:
            raise ConfigError(
                "security.profile_dir 必须包含 {platform}，保证每平台独立目录"
            )
        try:
            rendered = template.format(platform=platform)
        except (KeyError, IndexError) as exc:
            raise ConfigError(f"security.profile_dir 模板不合法: {template!r}") from exc
        return self.resolve_path(rendered)

    def profile_dir(self, platform: str) -> Path:
        """Playwright 模式 profile（原行为，目录 0700）。"""
        return ensure_private_dir(self._profile_path(platform))

    def browser_profile_path(self, platform: str) -> Path:
        """CDP 模式真实 Chrome 的 profile 路径（不创建目录）。"""
        base = self._profile_path(platform)
        return base.parent / f"{platform}-chrome"

    def browser_profile_dir(self, platform: str) -> Path:
        """CDP 模式真实 Chrome 的 profile：``<profile 根>/<platform>-chrome``。"""
        return ensure_private_dir(self.browser_profile_path(platform))

    def profile_key_present(self) -> bool:
        return bool(os.environ.get(self.security.profile_key_env))

    @property
    def config_digest(self) -> str:
        return self.config_sha256 or ""


def ensure_private_dir(path: str | Path) -> Path:
    path = Path(path)
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode != 0o700:
            os.chmod(path, 0o700)
    except OSError as exc:
        raise ConfigError(f"无法创建/收紧 profile 目录 {path}: {exc}") from exc
    return path


def _config_candidates() -> list[Path]:
    candidates: list[Path] = []
    env_path = os.environ.get("CRAWLER_CONFIG")
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend([Path.cwd() / "config" / "rules.yaml", Path.cwd() / "rules.yaml"])
    return candidates


def load_config(path: str | Path | None = None) -> AppConfig:
    if path is None:
        for candidate in _config_candidates():
            if candidate.exists():
                path = candidate
                break
        else:
            raise ConfigError(
                "找不到配置文件；请用 --config 指定，或把配置放到 ./config/rules.yaml"
            )
    path = Path(path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    if not path.exists():
        raise ConfigError(f"配置文件不存在: {path}")
    raw_bytes = path.read_bytes()
    try:
        data = yaml.safe_load(raw_bytes)
    except yaml.YAMLError as exc:
        raise ConfigError(f"YAML 解析失败 {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"配置文件顶层必须是对象: {path}")
    try:
        config = AppConfig.model_validate(data)
    except Exception as exc:
        raise ConfigError(f"配置校验失败 {path}: {exc}") from exc
    digest = hashlib.sha256(raw_bytes).hexdigest()
    config = config.model_copy(update={"config_path": path, "config_sha256": digest})
    if not config.config_version or config.config_version == "unversioned":
        config = config.model_copy(update={"config_version": digest[:12]})
    return config
