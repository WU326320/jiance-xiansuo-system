from __future__ import annotations

from datetime import datetime, timedelta

from workbench.whitelist import (
    IPService,
    build_whitelist_text,
    match_whitelist,
    normalize_entry,
)


def test_ip_first_seen_record_and_auto_load(client, supervisor):
    assert client.login("supervisor", "Super1234")[0] == 200
    run_at = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    payload = {
        "ip_name": "奥特曼  X",
        "platforms": ["taobao"],
        "mode": "formal",
        "notify_email": "owner@example.test",
        "keywords": ["奥特曼"],
        "scheduled_at": run_at,
        "whitelist": [
            {"scope_type": "ip", "platform_group": "taobao", "shop_name": "某某旗舰店"},
            {"scope_type": "ip", "platform_group": "jd", "shop_name": "京东专营店", "shop_id": "J1"},
        ],
    }
    status, body, _ = client.request("POST", "/api/tasks", json_body=payload)
    assert status == 201, body
    assert body["task"]["status"] == "scheduled"
    status, body, _ = client.request("POST", "/api/tasks/preview-ip", json_body={"name": "奥特曼x"})
    assert status == 200
    assert body["ip"]["name"] == "奥特曼  X"
    assert body["ip"]["name_norm"] == "奥特曼x"
    names = {(entry["platform_group"], entry["shop_name"]) for entry in body["whitelist"]}
    assert ("taobao", "某某旗舰店") in names
    assert ("jd", "京东专营店") in names

    # 第二次出现带出白名单
    status, body, _ = client.request("POST", "/api/tasks/preview-ip", json_body={"name": "奥特曼x"})
    assert len([e for e in body["whitelist"] if e["scope_type"] == "ip"]) == 2


def test_task_whitelist_edit_persists_and_audit(tmp_config, db, supervisor):
    service = IPService(tmp_config, db)
    ip = service.get_or_create("葫芦兄弟", actor=supervisor)
    first = service.save_task_whitelist(
        ip["id"],
        [
            {"platform": "taobao", "shop_name": "甲店"},
            {"platform": "jd", "shop_name": "乙店", "shop_id": "B2"},
        ],
        actor=supervisor,
    )
    assert len(first) == 2
    second = service.save_task_whitelist(
        ip["id"],
        [
            {"platform": "taobao", "shop_name": "甲店"},
            {"platform": "1688", "shop_name": "丙店"},
        ],
        actor=supervisor,
    )
    assert {(e["platform_group"], e["shop_name"]) for e in second} == {
        ("taobao", "甲店"),
        ("1688", "丙店"),
    }
    old = db.fetchall(
        "SELECT * FROM whitelist_entries WHERE ip_id=? AND shop_name='乙店'", (ip["id"],)
    )
    assert old and old[0]["active"] == 0 and old[0]["deleted_at"]
    audits = db.fetchall("SELECT * FROM audit_log WHERE action='whitelist.save'")
    assert len(audits) == 2


def test_match_ip_and_company_scope(tmp_config, db, supervisor):
    service = IPService(tmp_config, db)
    ip = service.get_or_create("奥特曼", company_code="company-a", actor=supervisor)
    service.save_task_whitelist(
        ip["id"],
        [
            {"platform": "taobao", "shop_name": "授权旗舰店"},
            {"platform": "jd", "shop_name": "同名不同店", "shop_id": "ID-1"},
        ],
        actor=supervisor,
    )
    service.add_company_entries(
        supervisor,
        "company-a",
        [{"platform": "1688", "shop_name": "公司共享店", "shop_id": "C-9"}],
    )
    entries = service.entries(ip["id"], include_company=True)
    assert len(entries) == 3
    assert len(service.entries(ip["id"], include_company=False)) == 2

    # 平台组 + NFKC + 去空白 + 小写
    hit = match_whitelist(entries, "tmall", "授权 旗舰店", None)
    assert hit.matched and hit.marker == "按店铺名排除-存在同名风险" and hit.risk

    # 双方都有 ID 且不同 -> 不排除
    miss = match_whitelist(entries, "jd", "同名不同店", "ID-2")
    assert not miss.matched and miss.marker == "同名不同店"

    # 双方 ID 相同 -> 排除
    exact = match_whitelist(entries, "jd", "同名不同店", "ID-1")
    assert exact.matched and exact.marker == "按店铺ID+店名排除"

    # 公司共享条目只有并入时才命中
    assert match_whitelist(entries, "1688", "公司共享店", "C-9").matched
    assert not match_whitelist(service.entries(ip["id"], include_company=False), "1688", "公司共享店", "C-9").matched


def test_whitelist_text_and_normalize():
    entries = [
        normalize_entry({"platform": "tmall", "shop_name": "A 店"}),
        normalize_entry({"platform": "jd", "shop_name": "B店", "shop_id": "B1"}),
    ]
    text = build_whitelist_text(entries)
    assert "taobao,A 店" in text
    assert "jd,B店,B1" in text


def test_1688_shop_name_is_url_decoded_before_matching():
    entry = normalize_entry(
        {"platform": "1688", "shop_name": "%E6%8E%88%E6%9D%83%E5%BA%97"}
    )
    assert entry["shop_name"] == "授权店"
    assert match_whitelist([entry], "1688", "授权 店", None).matched
