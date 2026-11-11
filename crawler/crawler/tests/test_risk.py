from __future__ import annotations

import pytest

from crawler.core.risk import classify_page, detect_payload_risk
from crawler.tests.conftest import fixture_text


def test_normal_page_is_ok_despite_login_link():
    state = classify_page(
        "https://s.taobao.com/search?q=x",
        fixture_text("normal.html"),
        platform="taobao",
    )
    assert state.state == "ok"


@pytest.mark.parametrize(
    ("platform", "url"),
    (
        ("taobao", "https://i.taobao.com/my_taobao.htm"),
        ("taobao", "https://s.taobao.com/search?q=x"),
        ("jd", "https://home.jd.com/"),
        ("1688", "https://work.1688.com/"),
    ),
)
def test_normal_page_with_baxia_scripts_and_hidden_layers_is_ok(platform, url):
    """baxia 是淘宝/1688 每页都会加载的防爬脚本，不能作为风控文案。"""
    state = classify_page(url, fixture_text("normal_baxia.html"), platform=platform)
    assert state.state == "ok", state


def test_visible_baxia_dialog_or_iframe_is_risk():
    state = classify_page(
        "https://i.taobao.com/my_taobao.htm",
        '<html><body><div id="baxia-dialog-content">请拖动滑块完成验证</div></body></html>',
        platform="taobao",
    )
    assert state.is_risk
    state = classify_page(
        "https://i.taobao.com/my_taobao.htm",
        '<html><body><iframe src="https://g.alicdn.com/sd/baxia/2.5.37/baxiaCommon.js"></iframe></body></html>',
        platform="taobao",
    )
    assert state.is_risk
    state = classify_page(
        "https://i.taobao.com/my_taobao.htm",
        '<html><body><iframe src="https://s.taobao.com/_____tmd_____/page/verify"></iframe></body></html>',
        platform="taobao",
    )
    assert state.is_risk


def test_login_iframe_redirect_url_with_tmd_query_is_not_risk():
    """正常淘宝页面的登录 iframe 查询参数里会带 _____tmd_____，不能误报。"""
    html = (
        '<html><body><iframe src="https://login.m.taobao.com/login.htm?'
        'redirectURL=https%3A%2F%2Fh5api.m.taobao.com%2F_____tmd_____%2Fpage"></iframe>'
        "<h1>我的淘宝</h1></body></html>"
    )
    state = classify_page("https://i.taobao.com/my_taobao.htm", html, platform="taobao")
    assert state.state == "ok", state


def test_visible_slider_without_text_is_risk_for_all_platforms():
    cases = (
        ("taobao", "https://s.taobao.com/search?q=x", '<div id="nc_1_wrapper"></div>'),
        (
            "jd",
            "https://search.jd.com/Search?keyword=x",
            '<div class="JDJRV-suspend-slide"></div>',
        ),
        (
            "1688",
            "https://s.1688.com/selloffer/offer_search.htm",
            '<div class="nc-container"></div>',
        ),
    )
    for platform, url, body in cases:
        state = classify_page(
            url, f"<html><body>{body}</body></html>", platform=platform
        )
        assert state.is_risk, (platform, state)


@pytest.mark.parametrize(
    "url",
    (
        "https://s.taobao.com/punish?x=1",
        "https://s.taobao.com/_____tmd_____/page/verify",
        "https://verify.jd.com/challenge",
        "https://cfe.m.jd.com/privatedomain/risk_handler/1",
        "https://sec.1688.com/verify",
    ),
)
def test_verification_page_url_is_risk(url):
    state = classify_page(url, "<html><body>normal</body></html>", platform="taobao")
    assert state.is_risk


def test_broad_words_in_scripts_or_product_titles_are_not_risk():
    html = (
        "<html><head><script>var captcha='验证码'; var baxia='baxia';</script></head>"
        "<body><div class='item'>验证码 T恤 captcha 图案</div></body></html>"
    )
    state = classify_page("https://s.taobao.com/search?q=x", html, platform="taobao")
    assert state.state == "ok", state


def test_login_by_url_and_form():
    state = classify_page(
        "https://login.taobao.com/member/login.jhtml",
        "<html></html>",
        platform="taobao",
    )
    assert state.is_login
    state = classify_page(
        "https://i.taobao.com/my_taobao.htm",
        fixture_text("login_taobao.html"),
        platform="taobao",
        dom_hints=["#J_LoginBox", "input[type='password']"],
    )
    assert state.is_login


def test_risk_page_text_and_dom():
    state = classify_page(
        "https://s.taobao.com/search?q=x",
        fixture_text("risk_taobao.html"),
        platform="taobao",
        dom_hints=["#nc_1_wrapper"],
    )
    assert state.is_risk
    assert "验证" in state.reason or "DOM" in state.reason
    state2 = classify_page("https://s.jd.com/x", "<html>访问受限</html>", platform="jd")
    assert state2.is_risk


def test_payload_risk_detection():
    assert detect_payload_risk([{"data": {"rgv587_flag": "sm"}}]).blocked
    assert detect_payload_risk([{"data": {"needLogin": True}}]).blocked
    assert detect_payload_risk([{"data": {"auctions": []}}]).blocked is False
    assert detect_payload_risk([{"msg": "FAIL_SYS_USER_VALIDATE"}]).blocked
    assert (
        detect_payload_risk([{"title": "验证码 T恤", "price": "9.9"}]).blocked is False
    )


@pytest.mark.parametrize("platform", ["taobao", "jd", "1688"])
@pytest.mark.parametrize(
    "message",
    [
        "抱歉由于访问频繁导致无法搜索，请稍后再试！",
        "请求频繁，请稍后再试",
        "操作过于频繁",
        "访问受限",
        "抱歉由于<span>访问</span><strong>频繁</strong>导致无法搜索",
    ],
)
def test_platform_limit_message_stops_all_platforms(platform, message):
    state = classify_page(
        "https://example.com/search",
        f"<html><body>{message}</body></html>",
        platform=platform,
    )
    assert state.is_risk
    assert "页面文案命中" in state.reason


@pytest.mark.parametrize("platform", ["taobao", "jd", "1688"])
def test_hidden_limit_message_is_not_a_block(platform):
    html = "<script>访问频繁</script><div hidden>请求频繁</div><p>商品列表</p>"
    assert not classify_page(
        "https://example.com/search", html, platform=platform
    ).is_risk
