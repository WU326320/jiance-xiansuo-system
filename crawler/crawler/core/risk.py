"""风控 / 登录页判定。

原则：宁可少抓，不触发风控。命中即抛 ``RiskBlocked`` 或 ``LoginRequired``，
runner 停止该平台并写运行记录；绝不自动破解验证码、绝不自动重试风控页。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from urllib.parse import urlparse

from crawler.core.dom import parse_html

LOGIN_HOST_SUFFIXES = {
    "taobao": (
        "login.taobao.com",
        "login.tmall.com",
        "passport.taobao.com",
        "passport.tmall.com",
    ),
    "jd": ("passport.jd.com", "login.jd.com", "passport.m.jd.com"),
    "1688": ("login.1688.com", "passport.1688.com", "login.taobao.com"),
}

# 这些域名本身就是验证/风控入口，出现在最终 URL 即可判定。
RISK_HOST_SUFFIXES = (
    "challenges.cloudflare.com",
    "captcha.taobao.com",
    "sec.taobao.com",
    "verify.jd.com",
    "sec.jd.com",
    "risk.jd.com",
    "sec.1688.com",
)

# 只检查最终页面 URL，不能拿整段 HTML 做子串匹配：正常淘宝/1688 页面的
# 隐藏 iframe、脚本变量和 redirectURL 里也会出现 baxia / _____tmd_____。
RISK_URL_MARKERS = ("_____tmd_____", "punish", "x5sec")
RISK_URL_PATH_MARKERS = (
    "/risk_handler",
    "/nocaptcha",
    "/captcha",
    "/verify",
    "/challenge",
)

# 只保留“验证拦截”本身的强文案。刻意排除 baxia、captcha、nc_1_wrapper、
# 单独的“验证码”等宽泛词：它们会出现在每个页面的防爬脚本、商品标题或
# 正常登录页的短信验证码输入框中。
RISK_TEXT_MARKERS = (
    "滑块验证",
    "滑动验证",
    "拖动滑块",
    "拖动下方滑块",
    "请按住滑块",
    "请完成验证",
    "请完成安全验证",
    "请完成下方验证",
    "安全验证",
    "人机验证",
    "图片验证码",
    "图形验证码",
    "访问受限",
    "访问验证",
    "异常流量",
    "访问过于频繁",
    "请求过于频繁",
    "访问频繁",
    "请求频繁",
    "操作频繁",
    "操作过于频繁",
    "访问次数过多",
    "请求次数过多",
    "访问被拒绝",
    "请求被限制",
    "网络环境存在风险",
    "账号存在风险",
    "rgv587",
    "punish",
    "verify you are human",
    "unusual traffic",
    "captcha challenge",
)

LOGIN_TEXT_MARKERS = (
    "密码登录",
    "扫码登录",
    "账号登录",
    "短信登录",
    "请登录后",
    "登录以继续",
    "欢迎登录",
    "new login",
)

# 只有这些“真正的滑块/验证码/拦截层”选择器命中才算风控。
# 浏览器侧还会再做一次可见性过滤，隐藏的预加载节点不会命中。
DOM_RISK_SELECTORS = (
    "#nc_1_wrapper",
    "#nc_1_n1z",
    "#nc_1_n1t",
    ".nc-container",
    ".nc_wrapper",
    ".nc_scale",
    "#nocaptcha",
    "#baxia-dialog-content",
    "#baxia-dialog",
    ".baxia-dialog",
    ".baxia-dialog-content",
    "#baxia-punish",
    ".baxia-punish",
    "#JDJRV-wrap",
    "#JDJRV-wrap-login",
    ".JDJRV-suspend-slide",
    "[class*='JDJRV']",
    "[id*='JDJRV']",
    "iframe[src*='captcha']",
    "iframe[src*='punish']",
    "iframe[src*='baxia']",
    "iframe[src*='verify.jd.com']",
    "iframe[src*='risk_handler']",
    "img[src*='captcha']",
    "[class*='captcha']",
    "[id*='captcha']",
)

_NON_VISIBLE_TAGS = {"script", "style", "noscript", "template", "svg", "canvas"}
_HIDDEN_CLASS_TOKENS = {
    "hidden",
    "hide",
    "d-none",
    "is-hidden",
    "sr-only",
    "visually-hidden",
    "invisible",
}
_HIDDEN_STYLE_RE = re.compile(
    r"(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*hidden|content-visibility\s*:\s*hidden)"
    r"\b|(?:^|;)\s*opacity\s*:\s*0(?:\.0+)?\s*(?:!important\s*)?(?:;|$)",
    re.IGNORECASE,
)

DOM_LOGIN_SELECTORS = (
    "#J_LoginBox",
    "#login-form",
    ".login-form",
    "form[name='login']",
    "input[type='password']",
    "input[name='password']",
)


@dataclass
class PageClassification:
    state: str  # ok | login | risk
    reason: str = ""
    evidence: str = ""

    @property
    def is_risk(self) -> bool:
        return self.state == "risk"

    @property
    def is_login(self) -> bool:
        return self.state == "login"


@dataclass
class RiskDecision:
    blocked: bool
    reason: str = ""
    evidence: str = ""
    hints: list[str] = field(default_factory=list)


def _host(url: str) -> str:
    try:
        return (urlparse(url).netloc or "").lower()
    except Exception:
        return ""


def _host_matches(host: str, suffixes: tuple[str, ...]) -> bool:
    return any(host == suffix or host.endswith("." + suffix) for suffix in suffixes)


def _element_is_hidden(element) -> bool:
    """保守判断节点是否不可见；脚本/样式节点始终不算可见文案。"""
    tag = getattr(element, "tag", None)
    if not isinstance(tag, str):
        return True
    if tag.lower() in _NON_VISIBLE_TAGS:
        return True
    if element.get("hidden") is not None:
        return True
    if (element.get("type") or "").lower() == "hidden":
        return True
    style = element.get("style") or ""
    if style and _HIDDEN_STYLE_RE.search(style):
        return True
    classes = {token.lower() for token in (element.get("class") or "").split()}
    if classes.intersection(_HIDDEN_CLASS_TOKENS):
        return True
    if any(
        token.startswith(("hidden--", "hidden_", "hide--", "hide_"))
        for token in classes
    ):
        return True
    return False


def _element_is_visible(element) -> bool:
    node = element
    while node is not None:
        if _element_is_hidden(node):
            return False
        node = node.getparent()
    return True


def _visible_text(root) -> str:
    """只拼接可见 DOM 文本，避免脚本/样式里的 baxia、captcha 等误命中。"""
    parts: list[str] = []

    def walk(node) -> None:
        if _element_is_hidden(node):
            return
        if node.text:
            parts.append(node.text)
        for child in node:
            walk(child)
            if child.tail:
                parts.append(child.tail)

    walk(root)
    return unescape(" ".join(parts)).lower()


def _visible_text_fallback(html: str) -> str:
    """lxml 解析失败时的兜底：至少去掉脚本/样式/注释。"""
    text = re.sub(r"(?is)<!--.*?-->", " ", html or "")
    text = re.sub(
        r"(?is)<(script|style|noscript|template|svg|canvas)\b[^>]*>.*?</\1\s*>",
        " ",
        text,
    )
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    return unescape(text).lower()


def _iframe_src_is_risk(src: str) -> bool:
    raw = (src or "").strip()
    if not raw:
        return False
    if raw.startswith("//"):
        raw = "https:" + raw
    host = _host(raw)
    if host and _host_matches(host, RISK_HOST_SUFFIXES):
        return True
    try:
        path = (urlparse(raw).path or "").lower()
    except Exception:
        return False
    return any(
        marker in path
        for marker in (
            "_____tmd_____",
            "punish",
            "risk_handler",
            "/nocaptcha",
            "/captcha",
            "/verify",
            "/challenge",
        )
    )


def _visible_dom_risk_matches(root) -> list[str]:
    matched: list[str] = []
    for selector in DOM_RISK_SELECTORS:
        try:
            nodes = root.cssselect(selector)
        except Exception:
            continue
        if any(_element_is_visible(node) for node in nodes):
            matched.append(selector)
    # 可见验证 iframe 的 src 可能是 _____tmd_____ 验证页路径；只在 path/host
    # 命中时判定，避免正常页面 login iframe 的 redirectURL 查询参数误报。
    try:
        iframes = root.cssselect("iframe[src]")
    except Exception:
        iframes = []
    if any(
        _element_is_visible(node) and _iframe_src_is_risk(node.get("src") or "")
        for node in iframes
    ):
        matched.append("iframe[src=验证页]")
    return matched


def _url_risk_reasons(url: str) -> list[str]:
    raw = (url or "").lower()
    reasons: list[str] = []
    host = _host(raw)
    if host and _host_matches(host, RISK_HOST_SUFFIXES):
        reasons.append(f"风控域名 {host}")
    for marker in RISK_URL_MARKERS:
        if marker in raw:
            reasons.append(f"最终 URL 命中 {marker!r}")
            break
    try:
        path = (urlparse(raw).path or "").lower()
    except Exception:
        path = ""
    for marker in RISK_URL_PATH_MARKERS:
        if marker in path:
            reasons.append(f"最终 URL 路径命中 {marker!r}")
            break
    return reasons


def classify_page(
    url: str,
    html: str | None,
    *,
    platform: str | None = None,
    dom_hints: list[str] | tuple[str, ...] = (),
) -> PageClassification:
    """根据最终 URL + 可见文案 + 可见 DOM 选择器判定页面状态。"""
    root = parse_html(html or "")
    content = (
        _visible_text(root) if root is not None else _visible_text_fallback(html or "")
    )
    static_dom_risk = _visible_dom_risk_matches(root) if root is not None else []
    hints = {str(hint) for hint in dom_hints}
    host = _host(url or "")
    suffixes = LOGIN_HOST_SUFFIXES.get(
        platform or "",
        tuple(suffix for values in LOGIN_HOST_SUFFIXES.values() for suffix in values),
    )

    risk_reasons: list[str] = _url_risk_reasons(url)
    matched_hints = sorted(hints.intersection(DOM_RISK_SELECTORS))
    if matched_hints:
        risk_reasons.append("DOM 选择器命中 " + ", ".join(matched_hints))
    extra_dom = [selector for selector in static_dom_risk if selector not in hints]
    if extra_dom:
        risk_reasons.append("页面可见 DOM 命中 " + ", ".join(extra_dom[:5]))
    compact_content = re.sub(r"\s+", "", content)
    for marker in RISK_TEXT_MARKERS:
        if marker in content or re.sub(r"\s+", "", marker) in compact_content:
            risk_reasons.append(f"页面文案命中 {marker!r}")
            break
    if risk_reasons:
        return PageClassification("risk", risk_reasons[0], "; ".join(risk_reasons[:3]))

    login_reasons: list[str] = []
    if _host_matches(host, suffixes):
        login_reasons.append(f"重定向到登录域名 {host}")
    matched_login_hints = sorted(hints.intersection(DOM_LOGIN_SELECTORS))
    if matched_login_hints:
        login_reasons.append("DOM 选择器命中 " + ", ".join(matched_login_hints))
    for marker in LOGIN_TEXT_MARKERS:
        if marker in content:
            login_reasons.append(f"页面文案命中 {marker!r}")
            break
    if login_reasons:
        return PageClassification(
            "login", login_reasons[0], "; ".join(login_reasons[:3])
        )

    return PageClassification("ok")


def detect_payload_risk(payloads) -> RiskDecision:
    """检查接口 JSON 的风控标记；返回第一个命中的证据。"""
    from crawler.core.jsonutil import payload_has_risk

    for payload in payloads:
        blocked, evidence = payload_has_risk(payload)
        if blocked:
            return RiskDecision(True, "接口返回风控/登录校验标记", evidence)
    return RiskDecision(False)
