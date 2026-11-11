"""平台接口 JSON 的容错抽取。

真实接口字段会变；这里只做“已知键优先 + 结构启发式”的保守抽取，
选择器和键名集中在平台模块，便于改版时修。
"""

from __future__ import annotations

import json
import re
from typing import Any, Iterable
from urllib.parse import unquote

# 页面内嵌 JSON 的常见全局变量名。2026 年京东/1688 搜索页都会把首屏数据
# 塞进 script，DOM 由前端二次渲染；只靠 CSS 选择器很容易整页 0 条。
_EMBEDDED_JSON_RE = re.compile(
    r"(?:window\s*\.\s*|self\s*\.\s*|var\s+|let\s+|const\s+)?"
    r"(__INIT_DATA__|__INITIAL_STATE__|__INITIAL_DATA__|__PRELOADED_STATE__|"
    r"__SEARCH_DATA__|__SEARCH_RESULT__|__OFFER_LIST__|__OFFER_DATA__|"
    r"__NEXT_DATA__|__NUXT__|__APP_DATA__|__DATA__|_DATA_|__ICE_APP_CONTEXT__|"
    r"pageData|page_data|"
    r"offerList|offer_list|_offerList|searchResult|search_result|initialState)"
    r"\s*=\s*",
    re.I,
)
_JSON_START_RE = re.compile(r"[\{\[]")
_JSONP_PREFIX_RE = re.compile(
    r"^\s*(?:\)\]\}',?|for\s*\(\s*;\s*;\s*\)\s*;|while\s*\(\s*1\s*\)\s*;)\s*"
)
_JSON_SCRIPT_TYPES = {"application/json", "application/ld+json", "text/json"}
_JSON_SCRIPT_IDS = {"__next_data__", "__nuxt_data__"}

_RISK_KEYS = {
    "rgv587_flag": True,
    "rgv587": True,
    "needlogin": True,
    "need_login": True,
    "loginrequired": True,
    "login_required": True,
    "user_validate": True,
    "uservalidate": True,
    "captcha": True,
    "punish": True,
    "riskcontrol": True,
    "risk_control": True,
    "accessdenied": True,
    "access_denied": True,
}
_RISK_VALUE_KEYS = {
    "msg",
    "message",
    "error",
    "errmsg",
    "err_msg",
    "code",
    "ret",
    "retmsg",
    "status",
    "subcode",
    "reason",
    "desc",
}
_RISK_VALUE_MARKERS = (
    "fail_sys_user_validate",
    "rgv587",
    "punish",
    "captcha",
    "访问受限",
    "验证码",
    "人机验证",
)


def parse_json_text(text: str | bytes | None) -> Any | None:
    """解析 JSON / JSONP / 反 XSSI 前缀响应。

    真实接口里淘宝 mtop、1688 h5api 常返回 ``mtopjsonp1({...})``，
    京东价格接口偶尔返回 ``)]}'\\n[...]``。只做去掉包装后的严格 json
    解析，不把普通 JS/HTML 当数据。
    """
    if not text:
        return None
    if isinstance(text, bytes):
        text = text.decode("utf-8", errors="replace")
    raw = str(text).lstrip("\ufeff").strip()
    if not raw or raw[0] == "<":
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        pass
    raw = _JSONP_PREFIX_RE.sub("", raw)
    if not raw or raw[0] == "<":
        return None
    start = _JSON_START_RE.search(raw)
    if start is None:
        return None
    try:
        value, _ = json.JSONDecoder().raw_decode(raw, start.start())
        return value
    except (TypeError, ValueError):
        return None


def _json_from_assignments(script: str) -> Iterable[Any]:
    """从一个 script 文本中抽取 ``window.__INIT_DATA__ = {...}`` 等赋值。"""
    if not script:
        return
    # 淘宝 ICE SSR 的固定包装。只读取JSON字面量，绝不执行JS。
    # 限定完整前后缀，拒绝重赋值、函数调用和未知变换。
    ice = re.match(
        r"\s*!\(function\s*\(\)\s*\{\s*var\s+a\s*=\s*"
        r"window\.__ICE_APP_CONTEXT__\s*\|\|\s*\{\}\s*;\s*var\s+b\s*=\s*",
        script,
    )
    if ice:
        try:
            value, end = json.JSONDecoder().raw_decode(script, ice.end())
            suffix = re.fullmatch(
                r"\s*;\s*for\s*\(var\s+k\s+in\s+a\)\s*\{\s*b\[k\]\s*=\s*a\[k\]\s*;?\s*\}"
                r"\s*window\.__ICE_APP_CONTEXT__\s*=\s*b\s*;\s*\}\)\(\);?\s*",
                script[end:],
            )
            if suffix and isinstance(value, dict):
                yield value
        except (TypeError, ValueError):
            pass
        return
    for match in _EMBEDDED_JSON_RE.finditer(script):
        pos = match.end()
        # 兼容 window.__INIT_DATA__ = JSON.parse('{"..."}')。
        segment = script[pos : pos + 512]
        lead = len(segment) - len(segment.lstrip())
        decoded = re.match(
            r"JSON\.parse\s*\(\s*decodeURIComponent\s*\(\s*(['\"])",
            segment[lead:],
            re.I,
        )
        json_parse = decoded or re.match(
            r"JSON\.parse\s*\(\s*(['\"])", segment[lead:], re.I
        )
        if json_parse is not None:
            quote = json_parse.group(1)
            quote_at = pos + lead + json_parse.end() - 1
            end = quote_at + 1
            escaped = False
            while end < len(script):
                char = script[end]
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    break
                end += 1
            if end >= len(script):
                continue
            try:
                content = script[quote_at + 1 : end]
                if quote == "'":
                    # 单引号 JS 字符串不能直接当 JSON；常见形态内部就是
                    # 双引号 JSON 文本，去掉 JS 转义后直接二次解析。
                    inner = content.replace("\\'", "'").replace('\\"', '"')
                else:
                    inner = json.loads(quote + content + quote)
                if decoded is not None:
                    inner = unquote(inner)
                yield json.loads(inner)
            except (TypeError, ValueError):
                continue
            continue
        # 不向后搜索无关对象（例如赋值给变量后出现的推荐数据）。
        start = _JSON_START_RE.match(script, pos + lead)
        if start is None:
            continue
        try:
            value, _ = json.JSONDecoder().raw_decode(script, start.start())
            yield value
        except (TypeError, ValueError):
            continue


def extract_embedded_json(
    html: str | bytes | None, *, max_payloads: int = 60
) -> list[Any]:
    """抽取页面内嵌 JSON（script 标签 / 全局变量赋值）。

    仅在明确的 JSON script 或已知全局变量赋值后解析，避免把普通 JS 代码
    误当数据。返回顺序与页面出现顺序一致，重复对象只保留一次。
    """
    if not html:
        return []
    if isinstance(html, bytes):
        html = html.decode("utf-8", errors="replace")
    text = str(html)
    if not text:
        return []

    payloads: list[Any] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        if value is None or len(payloads) >= max_payloads:
            return
        try:
            key = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        except (TypeError, ValueError):
            key = repr(value)
        if key in seen:
            return
        seen.add(key)
        payloads.append(value)

    sources: list[str] = []
    try:
        from lxml import html as lxml_html

        root = lxml_html.fromstring(text)
        for script in root.iter("script"):
            script_text = script.text or ""
            if not script_text:
                continue
            script_type = (script.get("type") or "").strip().lower()
            script_id = (script.get("id") or "").strip().lower()
            if script_type in _JSON_SCRIPT_TYPES or script_id in _JSON_SCRIPT_IDS:
                add(parse_json_text(script_text))
            sources.append(script_text)
    except Exception:
        sources = []

    # lxml 解析失败或页面把 JSON 放在非 script 节点时，对原始 HTML 做一次
    # 赋值扫描；只命中白名单全局变量，不会解析整页脚本。
    if not sources:
        sources = [text]
    for source in sources:
        for value in _json_from_assignments(source):
            add(value)
            if len(payloads) >= max_payloads:
                break
    return payloads


def _walk(obj: Any, list_keys: set[str], depth: int, max_depth: int, budget: list[int]):
    if budget[0] <= 0 or depth > max_depth:
        return
    budget[0] -= 1
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(value, list) and str(key).lower() in list_keys:
                yield value
            yield from _walk(value, list_keys, depth + 1, max_depth, budget)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value, list_keys, depth + 1, max_depth, budget)


def _candidate_lists(
    payload: Any, list_keys: Iterable[str], max_depth: int = 10
) -> list[list[dict]]:
    keys = {key.lower() for key in list_keys}
    budget = [50000]
    lists: list[list[dict]] = []
    for value in _walk(payload, keys, 0, max_depth, budget):
        if value and all(isinstance(row, dict) for row in value):
            lists.append(value)
    return lists


_TITLE_HEURISTIC_KEYS = {
    "title",
    "subject",
    "raw_title",
    "warename",
    "wname",
    "skuname",
    "itemtitle",
    "offertitle",
    "offer_title",
    "productname",
    "product_name",
    "goodsname",
    "name",
}
_PRICE_HEURISTIC_KEYS = {
    "price",
    "p",
    "view_price",
    "pricerange",
    "price_range",
    "priceinfo",
    "unitprice",
    "unit_price",
    "showprice",
    "show_price",
    "saleprice",
    "sale_price",
    "minprice",
    "min_price",
}


def _looks_like_product(row: dict) -> bool:
    lowered = {str(key).lower() for key in row}
    return bool(lowered & _TITLE_HEURISTIC_KEYS) and bool(
        lowered & _PRICE_HEURISTIC_KEYS
    )


# 淘宝新版 PC 搜索 mtop 返回 ``resultList: [{item: {...}}]``，1688 首页数据
# 也会包一层 offer；只检查顶层键会把整页记录全部丢掉。
_NESTED_RECORD_KEYS = {
    "item",
    "itemdata",
    "iteminfo",
    "offer",
    "offerinfo",
    "product",
    "productinfo",
    "auction",
    "data",
}


def _record_views(row: dict) -> list[dict]:
    """返回记录本身 + 常见嵌套商品对象，用于识别外层包装。"""
    views = [row]
    for key, value in row.items():
        if isinstance(value, dict) and str(key).lower() in _NESTED_RECORD_KEYS:
            views.append(value)
    return views


def extract_records(
    payloads: Iterable[Any],
    *,
    list_keys: Iterable[str],
    id_keys: Iterable[str],
    max_depth: int = 10,
) -> list[dict]:
    """从多个 JSON payload 中抽取商品记录列表，保持首次出现顺序。"""
    normalized_ids = {key.lower() for key in id_keys}
    seen: set[str] = set()
    records: list[dict] = []
    for payload in payloads:
        found = _candidate_lists(payload, list_keys, max_depth=max_depth)
        if (
            not found
            and isinstance(payload, list)
            and payload
            and all(isinstance(x, dict) for x in payload)
        ):
            found = [payload]
        for rows in found:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                # 接受带商品 ID 的记录，或至少像商品（常见标题键+价格键）的记录。
                # 后者用于淘宝无 nid / 1688 无 offerId 的异常页，便于统计 drop
                # 并落 debug 快照；只有 ID 没有标题的价格接口记录会在平台层忽略。
                views = _record_views(row)
                has_id = False
                for view in views:
                    lowered = {str(key).lower(): value for key, value in view.items()}
                    if any(
                        lowered.get(key) not in (None, "", 0, "0")
                        for key in normalized_ids
                    ):
                        has_id = True
                        break
                if not has_id and not any(_looks_like_product(view) for view in views):
                    continue
                identity = None
                for view in views:
                    lowered = {str(key).lower(): value for key, value in view.items()}
                    for key in normalized_ids:
                        value = lowered.get(key)
                        if value not in (None, "", 0, "0"):
                            identity = str(value)
                            break
                    if identity is not None:
                        break
                if identity is not None:
                    if identity in seen:
                        continue
                    seen.add(identity)
                records.append(row)
    return records


def payload_has_risk(payload: Any, *, max_depth: int = 8) -> tuple[bool, str]:
    """检测接口 JSON 中的风控/登录错误码。"""
    found: list[str] = []

    def walk(obj: Any, depth: int) -> None:
        if depth > max_depth:
            return
        if isinstance(obj, dict):
            for key, value in obj.items():
                key_l = str(key).lower()
                if key_l in _RISK_KEYS and value not in (
                    None,
                    False,
                    0,
                    "",
                    "0",
                    "false",
                    "False",
                ):
                    found.append(f"{key}={value}")
                if isinstance(value, str) and key_l in _RISK_VALUE_KEYS:
                    lower = value.lower()
                    if any(marker in lower for marker in _RISK_VALUE_MARKERS):
                        found.append(f"{key}={value}")
                walk(value, depth + 1)
        elif isinstance(obj, list):
            for value in obj[:200]:
                walk(value, depth + 1)

    walk(payload, 0)
    if found:
        return True, found[0][:300]
    return False, ""


def deep_find(payload: Any, keys: Iterable[str], *, max_depth: int = 8) -> Any:
    """深度优先查找第一个匹配键的值（详情页字段兜底）。"""
    wanted = {key.lower() for key in keys}

    def walk(obj: Any, depth: int) -> Any:
        if depth > max_depth:
            return None
        if isinstance(obj, dict):
            for key, value in obj.items():
                if str(key).lower() in wanted and value not in (None, "", [], {}):
                    return value
            for value in obj.values():
                found = walk(value, depth + 1)
                if found is not None:
                    return found
        elif isinstance(obj, list):
            for value in obj[:200]:
                found = walk(value, depth + 1)
                if found is not None:
                    return found
        return None

    return walk(payload, 0)
