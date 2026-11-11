from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select, text

from pipeline.aggregate import aggregate_snapshots, needs_jd_detail
from pipeline.backup import backup_database, backup_files
from pipeline.config import BACKEND_ROOT, load_config
from pipeline.db import create_engine_for, make_session_factory, session_scope
from pipeline.lock import AlreadyRunning, acquire_lock
from pipeline.models import (
    ConfigVersion,
    Lead,
    LeadLink,
    LeadStatusHistory,
    Product,
    ProductSnapshot,
    RunLog,
    Shop,
    Whitelist,
)
from pipeline.review import import_review_excel, mark_saas
from pipeline.runner import PipelineRunner
from pipeline.scoring import combine_scores, compute_amount, score_snapshot

from tests.conftest import FakeAdapter, FakeSMTP, make_item, platform_data


def _run_platform_items(session, settings, items, day: date, run_id: str = "test"):
    from pipeline.store import upsert_many

    snapshots, dropped = upsert_many(
        session,
        platform=items[0]["platform"] if items else "taobao",
        items=items,
        settings=settings,
        run_id=run_id,
        snapshot_date=day,
    )
    for snapshot in snapshots:
        product = session.get(Product, snapshot.product_id)
        shop = (
            session.scalar(
                select(Shop).where(
                    Shop.platform == product.platform, Shop.shop_id == product.shop_id
                )
            )
            if product and product.shop_id
            else None
        )
        score_snapshot(
            session,
            settings=settings,
            snapshot=snapshot,
            product=product,
            shop=shop,
        )
    return snapshots, dropped


def _view_rows(session, view: str):
    result = session.execute(text(f"SELECT * FROM {view}"))
    columns = list(result.keys())
    return [dict(zip(columns, row)) for row in result.all()]


# ---------------------------------------------------------------------------
# 1. 空库 alembic upgrade head 建出全部表和两个视图
# ---------------------------------------------------------------------------
def test_01_alembic_upgrade_creates_tables_and_views(tmp_path: Path):
    db_path = tmp_path / "empty.db"
    env = dict(os.environ)
    env["IP_LEAD_DB_PATH"] = str(db_path)
    env["PYTHONIOENCODING"] = "utf-8"
    paths = [str(BACKEND_ROOT)]
    if os.name != "nt" and (BACKEND_ROOT / ".deps").is_dir():
        paths.insert(0, str(BACKEND_ROOT / ".deps"))
    env["PYTHONPATH"] = os.pathsep.join(paths)
    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "alembic.ini", "upgrade", "head"],
        cwd=BACKEND_ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert db_path.exists()
    con = sqlite3.connect(db_path)
    try:
        tables = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        views = {
            row[0]
            for row in con.execute(
                "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
            )
        }
        expected_tables = {
            "alembic_version",
            "config_versions",
            "shops",
            "products",
            "product_snapshots",
            "vision_cache",
            "whitelist",
            "leads",
            "lead_links",
            "lead_status_history",
            "run_logs",
        }
        assert expected_tables <= tables
        assert {"v_lead_summary", "v_lead_links"} <= views
        assert "v_lead_verify_summary" not in views
        assert "v_lead_verify_links" not in views
        summary_info = list(con.execute("PRAGMA table_info(v_lead_summary)"))
        link_info = list(con.execute("PRAGMA table_info(v_lead_links)"))
        assert [row[1] for row in summary_info] == [
            "lead_id",
            "run_id",
            "snapshot_date",
            "platform",
            "platform_group",
            "shop_key",
            "shop_id",
            "shop_name",
            "shop_type",
            "shop_label_raw",
            "ip_codes",
            "link_count",
            "shop_amount",
            "tier",
            "payment_ability",
            "confidence",
            "hit_reasons_summary",
            "missing_flags",
            "review_result",
            "reviewer",
            "shop_type_rank",
            "caliber_version",
            "config_version",
        ]
        assert [row[1] for row in link_info] == [
            "lead_id",
            "run_id",
            "snapshot_date",
            "ip_code",
            "product_id",
            "platform",
            "shop_id",
            "shop_name",
            "image_path",
            "main_image_url",
            "title",
            "url",
            "price",
            "sales_raw",
            "comments_raw",
            "estimated_sales",
            "amount",
            "amount_formula",
            "text_score",
            "visual_score",
            "final_score",
            "hit_reasons",
            "missing_flags",
        ]
        # 第 6 节类型：文本 TEXT，计数 INTEGER（SQLite 报 INT），金额/分数 REAL。
        summary_types = {row[1]: row[2] for row in summary_info}
        link_types = {row[1]: row[2] for row in link_info}
        for column in (
            "lead_id",
            "run_id",
            "snapshot_date",
            "platform",
            "platform_group",
            "shop_key",
            "shop_id",
            "shop_name",
            "shop_type",
            "shop_label_raw",
            "ip_codes",
            "tier",
            "payment_ability",
            "hit_reasons_summary",
            "missing_flags",
            "review_result",
            "reviewer",
            "caliber_version",
            "config_version",
        ):
            assert summary_types[column] == "TEXT"
        assert summary_types["link_count"] in ("INT", "INTEGER")
        assert summary_types["shop_type_rank"] in ("INT", "INTEGER")
        assert summary_types["shop_amount"] == "REAL"
        assert summary_types["confidence"] == "REAL"
        for column in (
            "lead_id",
            "run_id",
            "snapshot_date",
            "ip_code",
            "product_id",
            "platform",
            "shop_id",
            "shop_name",
            "image_path",
            "main_image_url",
            "title",
            "url",
            "sales_raw",
            "comments_raw",
            "amount_formula",
            "hit_reasons",
            "missing_flags",
        ):
            assert link_types[column] == "TEXT"
        assert link_types["estimated_sales"] in ("INT", "INTEGER")
        for column in ("price", "amount", "text_score", "visual_score", "final_score"):
            assert link_types[column] == "REAL"
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 2. 同任务重入幂等，不同任务独立保存同一商品的快照
# ---------------------------------------------------------------------------
def test_02_same_product_same_day_and_next_day(settings, session_factory):
    day = date(2026, 11, 5)
    item = make_item(product_id="20001", price=1, sales=1, comments=1)
    adapter = FakeAdapter({"taobao": platform_data("taobao", [item])})
    runner = PipelineRunner(settings, adapter=adapter)
    for _ in range(3):
        runner.run(
            platforms=["taobao"], run_date=day, allow_alert=False, run_id="same-task"
        )
    assert list(settings.backups_dir.glob("leads_*.db")), (
        "第二次运行前应自动备份已存在的数据库"
    )
    with session_scope(session_factory) as session:
        assert session.query(Product).filter_by(product_id="20001").count() == 1
        assert session.query(ProductSnapshot).count() == 1

    runner.run(
        platforms=["taobao"], run_date=day, allow_alert=False, run_id="another-task"
    )
    with session_scope(session_factory) as session:
        snapshots = session.query(ProductSnapshot).all()
        assert len(snapshots) == 2
        assert {snap.run_id for snap in snapshots} == {"same-task", "another-task"}

    runner.run(
        platforms=["taobao"],
        run_date=day + timedelta(days=1),
        allow_alert=False,
        run_id="next-day-task",
    )
    with session_scope(session_factory) as session:
        assert session.query(Product).filter_by(product_id="20001").count() == 1
        snapshots = session.query(ProductSnapshot).filter_by().all()
        assert len(snapshots) == 3
        assert {snap.snapshot_date for snap in snapshots} == {
            day,
            day + timedelta(days=1),
        }


# ---------------------------------------------------------------------------
# 3. 已录入SaaS 后次日被压制；驳回 30 天内同样被压制
# ---------------------------------------------------------------------------
def test_03_processed_lead_suppressed_next_day(settings, session_factory):
    day = date(2026, 11, 5)
    item = make_item(product_id="30001", price=600, sales=1000, comments=100)
    adapter = FakeAdapter({"taobao": platform_data("taobao", [item])})
    runner = PipelineRunner(settings, adapter=adapter)
    first = runner.run(platforms=["taobao"], run_date=day, allow_alert=False)
    assert first.leads == 1
    with session_scope(session_factory) as session:
        lead = session.query(Lead).one()
        mark_saas(session, [lead.lead_no])
        lead.reviewed_at = datetime.combine(day, datetime.min.time()) + timedelta(
            hours=12
        )
        lead.saas_entered_at = lead.reviewed_at
        session.commit()
        assert lead.status == "已录入SaaS"

    second = runner.run(
        platforms=["taobao"], run_date=day + timedelta(days=1), allow_alert=False
    )
    assert second.leads == 0
    with session_scope(session_factory) as session:
        assert session.query(Lead).count() == 1
        latest = (
            session.query(ProductSnapshot)
            .order_by(ProductSnapshot.snapshot_date.desc())
            .first()
        )
        reasons = json.loads(latest.hit_reasons)
        assert any("已处理压制:已录入SaaS" in reason for reason in reasons)


def test_03b_rejected_suppression_window(settings, session_factory):
    day = date(2026, 11, 5)
    item = make_item(product_id="30002", price=600, sales=1000, comments=100)
    adapter = FakeAdapter({"taobao": platform_data("taobao", [item])})
    runner = PipelineRunner(settings, adapter=adapter)
    assert runner.run(platforms=["taobao"], run_date=day, allow_alert=False).leads == 1
    with session_scope(session_factory) as session:
        lead = session.query(Lead).one()
        lead.status = "驳回"
        lead.review_result = "驳回"
        lead.reviewed_at = datetime(2026, 11, 5, 12, 0, 0)
        session.commit()
    # 30 天内压制
    assert (
        runner.run(
            platforms=["taobao"], run_date=day + timedelta(days=29), allow_alert=False
        ).leads
        == 0
    )
    # 第 31 天恢复（旧线索仍在，但新一天可再次生成）
    assert (
        runner.run(
            platforms=["taobao"], run_date=day + timedelta(days=31), allow_alert=False
        ).leads
        == 1
    )


# ---------------------------------------------------------------------------
# 4. 改 yaml 后 config_version 变化，旧记录不变
# ---------------------------------------------------------------------------
def _write_config(tmp_path: Path, *, multiplier: int, threshold: int) -> Path:
    raw = yaml.safe_load(
        (BACKEND_ROOT / "config" / "rules.yaml").read_text(encoding="utf-8")
    )
    raw["storage"] = {"db_path": str(tmp_path / "data" / "leads.db")}
    raw["paths"] = {
        "backups": str(tmp_path / "backups"),
        "logs": str(tmp_path / "logs"),
        "images": str(tmp_path / "data" / "images"),
        "crawler_dir": str(tmp_path / "crawler"),
        "crawler_config": str(tmp_path / "crawler" / "config" / "rules.yaml"),
        "crawler_output_dir": str(tmp_path / "crawler" / "out"),
    }
    raw["run"] = {
        "lock_file": str(tmp_path / "data" / "run.lock"),
        "backup_keep": 14,
        "log_keep_days": 30,
    }
    raw["alert"] = {"enabled": False}
    raw["export"] = {"enabled": False, "dir": str(tmp_path / "excel")}
    raw["amount"]["comment_multiplier"] = multiplier
    raw["aggregate"]["shop_threshold"] = threshold
    path = tmp_path / f"rules_{multiplier}_{threshold}.yaml"
    path.write_text(
        yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    return path


def test_04_config_version_change_keeps_history(tmp_path: Path):
    day = date(2026, 11, 5)
    path_a = _write_config(tmp_path, multiplier=10, threshold=500000)
    path_b = _write_config(tmp_path, multiplier=5, threshold=300000)
    settings_a = load_config(path_a)
    settings_b = load_config(path_b)
    assert settings_a.config_version != settings_b.config_version

    item = make_item(product_id="40001", price=600, sales=1000, comments=100)
    adapter = FakeAdapter({"taobao": platform_data("taobao", [item])})
    first = PipelineRunner(settings_a, adapter=adapter).run(
        platforms=["taobao"], run_date=day, allow_alert=False
    )
    assert first.leads == 1
    second = PipelineRunner(settings_b, adapter=adapter).run(
        platforms=["taobao"], run_date=day + timedelta(days=1), allow_alert=False
    )
    assert second.leads == 1  # 金额公式固定乘10，旧配置键不再改变金额。

    engine = create_engine_for(settings_b)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            snapshots = (
                session.query(ProductSnapshot)
                .order_by(ProductSnapshot.snapshot_date)
                .all()
            )
            assert len(snapshots) == 2
            assert snapshots[0].config_version == settings_a.config_version
            assert snapshots[1].config_version == settings_b.config_version
            assert snapshots[0].amount == Decimal("600000.00")
            assert snapshots[1].amount == Decimal("600000.00")
            assert session.query(Lead).count() == 2
            versions = {
                row.config_version for row in session.query(ConfigVersion).all()
            }
            assert versions == {settings_a.config_version, settings_b.config_version}
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 5. 登录失效只停该平台、其他平台继续、run_logs 有记录、低条数报警
# ---------------------------------------------------------------------------
def test_05_login_failure_and_low_items_alert(settings, fake_smtp, monkeypatch):
    settings.raw["alert"]["enabled"] = True
    settings.raw["alert"]["min_items_per_platform"] = 10
    monkeypatch.setattr(
        "keyring.get_password", lambda service, user: "auth-code-for-test"
    )
    taobao = platform_data(
        "taobao",
        [],
        login_ok=False,
        status="login_failed",
        items_ok=0,
        errors=["登录态失效"],
    )
    jd = platform_data(
        "jd",
        [make_item(platform="jd", product_id="50001", price=1, sales=1, comments=1)],
        items_ok=1,
    )
    adapter = FakeAdapter({"taobao": taobao, "jd": jd})
    summary = PipelineRunner(settings, adapter=adapter).run(
        platforms=["taobao", "jd"], run_date=date(2026, 11, 5)
    )
    assert summary.platforms["taobao"].status == "login_failed"
    assert summary.platforms["jd"].status == "ok"
    assert summary.alerted is True
    assert len(fake_smtp.instances) == 1
    message, _ = fake_smtp.instances[0].messages[0]
    body = message.get_content()
    assert "taobao: 登录失效" in body
    assert "jd: 采集条数 1 < 下限 10" in body
    assert "auth-code-for-test" not in body

    engine = create_engine_for(settings)
    factory = make_session_factory(engine)
    try:
        with session_scope(factory) as session:
            rows = session.query(RunLog).all()
            assert len(rows) == 3
            assert {row.platform for row in rows} == {None, "taobao", "jd"}
            assert any(
                row.platform == "taobao" and row.login_ok is False for row in rows
            )
            assert any(row.platform == "jd" and row.items_count == 1 for row in rows)
    finally:
        engine.dispose()


# ---------------------------------------------------------------------------
# 6. 双击两次 run.bat：第二次提示已在运行并退出
# ---------------------------------------------------------------------------
def test_06_single_instance_lock(settings):
    lock_path = settings.lock_path
    first = acquire_lock(lock_path, command="test")
    try:
        with pytest.raises(AlreadyRunning):
            acquire_lock(lock_path, command="test-second")
    finally:
        first.release()
    second = acquire_lock(lock_path, command="after-release")
    second.release()

    lock_path.write_text(json.dumps({"pid": 99999999}), encoding="utf-8")
    stale = acquire_lock(lock_path, command="stale")
    stale.release()
    # 只检查ASCII命令，Windows批处理文件可使用本地代码页。
    assert b"pause" in (BACKEND_ROOT / "run.bat").read_bytes().lower()


# ---------------------------------------------------------------------------
# 7. 运行前自动备份，超过 14 份清理
# ---------------------------------------------------------------------------
def test_07_backup_and_keep_14(settings, engine, session_factory):
    with session_scope(session_factory) as session:
        session.add(
            ConfigVersion(
                config_version="backup-marker",
                caliber_version="v1.0",
                yaml_text="marker",
                first_used_at=datetime.now(),
            )
        )
        session.commit()
    settings.backups_dir.mkdir(parents=True, exist_ok=True)
    for index in range(20):
        path = settings.backups_dir / f"leads_20200101_0000{index:02d}.db"
        path.write_bytes(b"old")
        old = 1_600_000_000 + index
        os.utime(path, (old, old))
    created = backup_database(settings)
    assert created is not None
    files = backup_files(settings.backups_dir)
    assert len(files) == 14
    assert created in files
    con = sqlite3.connect(created)
    try:
        value = con.execute(
            "SELECT config_version FROM config_versions WHERE config_version='backup-marker'"
        ).fetchone()
        assert value == ("backup-marker",)
    finally:
        con.close()


# ---------------------------------------------------------------------------
# 8. 数据库和日志目录里搜不到授权码
# ---------------------------------------------------------------------------
def test_08_no_auth_code_on_disk(tmp_path, settings, fake_smtp, monkeypatch):
    sentinel = "SUPER_SECRET_AUTH_CODE_9f3a"
    store: dict[tuple[str, str], str] = {}

    def fake_get(service, user):
        return store.get((service, user))

    def fake_set(service, user, value):
        store[(service, user)] = value

    monkeypatch.setattr("keyring.get_password", fake_get)
    monkeypatch.setattr("keyring.set_password", fake_set)
    settings.raw["alert"]["enabled"] = True
    settings.raw["alert"]["min_items_per_platform"] = 10
    from pipeline.smtp_setup import setup_smtp

    result = setup_smtp(settings, password=sentinel)
    assert result.stored is True

    item = make_item(product_id="80001", price=1, sales=1, comments=1)
    adapter = FakeAdapter({"taobao": platform_data("taobao", [item], items_ok=1)})
    summary = PipelineRunner(settings, adapter=adapter).run(
        platforms=["taobao"], run_date=date(2026, 11, 5)
    )
    assert summary.alerted is True
    assert fake_smtp.instances[0].password == sentinel

    forbidden = []
    search_roots = [
        settings.logs_dir,
        settings.backups_dir,
        settings.database_path.parent,
        settings.images_dir,
    ]
    for root in search_roots:
        candidates = list(Path(root).rglob("*")) if root.exists() else []
        for path in candidates:
            if not path.is_file() or path.suffix in {".pyc"}:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if sentinel.encode() in data:
                forbidden.append(path)
    assert forbidden == []
    assert sentinel.encode() not in settings.database_path.read_bytes()
    for log_file in settings.logs_dir.glob("run_*.log"):
        assert sentinel not in log_file.read_text(encoding="utf-8", errors="replace")
    assert sentinel not in (BACKEND_ROOT / "config" / "rules.yaml").read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# 9. B1-B7 聚合用例通过视图结果复核
# ---------------------------------------------------------------------------
def test_09_b1_b7_aggregation_via_views(settings, session_factory):
    today = date(2026, 11, 5)
    yesterday = date(2026, 11, 4)
    with session_scope(session_factory) as session:
        # B1: 同店 300000 + 250000
        b1 = [
            make_item(
                product_id="B1001",
                shop_id="S1",
                shop_name="S1店",
                price=300,
                sales=1000,
                comments=100,
            ),
            make_item(
                product_id="B1002",
                shop_id="S1",
                shop_name="S1店",
                price=250,
                sales=1000,
                comments=100,
            ),
        ]
        b1_snaps, _ = _run_platform_items(session, settings, b1, today, "today")
        # B2: 恰好 500000
        b2 = [
            make_item(
                product_id="B2001",
                shop_id="S2",
                shop_name="S2店",
                price=500,
                sales=1000,
                comments=100,
            )
        ]
        b2_snaps, _ = _run_platform_items(session, settings, b2, today, "today")
        # B3: 昨天 300000、今天 250000
        b3_y = [
            make_item(
                product_id="B3001", shop_id="S3", price=300, sales=1000, comments=100
            )
        ]
        b3_y_snaps, _ = _run_platform_items(session, settings, b3_y, yesterday, "b3y")
        aggregate_snapshots(
            session,
            settings=settings,
            snapshots=b3_y_snaps,
            snapshot_date=yesterday,
            run_id="b3y",
        )
        b3_t = [
            make_item(
                product_id="B3002", shop_id="S3", price=250, sales=1000, comments=100
            )
        ]
        b3_t_snaps, _ = _run_platform_items(session, settings, b3_t, today, "today")
        # B4: 同名不同 shop_id
        b4 = [
            make_item(
                product_id="B4001",
                shop_id="S4A",
                shop_name="同名店",
                price=600,
                sales=1000,
                comments=100,
            ),
            make_item(
                product_id="B4002",
                shop_id="S4B",
                shop_name="同名店",
                price=600,
                sales=1000,
                comments=100,
            ),
        ]
        b4_snaps, _ = _run_platform_items(session, settings, b4, today, "today")
        # B5: shop_id 缺失
        b5 = [
            make_item(
                product_id="B5001",
                shop_id=None,
                shop_name="无ID店",
                price=600,
                sales=1000,
                comments=100,
            )
        ]
        b5_snaps, _ = _run_platform_items(session, settings, b5, today, "today")
        # B6: 当日下架
        b6 = [
            make_item(
                product_id="B6001",
                shop_id="S6",
                price=600,
                sales=1000,
                comments=100,
                listed=False,
            )
        ]
        b6_snaps, _ = _run_platform_items(session, settings, b6, today, "today")

        unrelated, _ = _run_platform_items(
            session,
            settings,
            [
                make_item(
                    product_id="OTHER-1",
                    shop_id="S1",
                    shop_name="S1店",
                    price=900,
                    sales=1000,
                    comments=100,
                )
            ],
            today,
            "another-task",
        )
        all_today = (
            b1_snaps
            + b2_snaps
            + b3_t_snaps
            + b4_snaps
            + b5_snaps
            + b6_snaps
            + unrelated
        )
        aggregate_snapshots(
            session,
            settings=settings,
            snapshots=all_today,
            snapshot_date=today,
            run_id="today",
        )
        session.commit()

        result = session.execute(
            text("SELECT * FROM v_lead_summary WHERE snapshot_date = :day"),
            {"day": today.isoformat()},
        )
        rows = [dict(zip(result.keys(), row)) for row in result.all()]
        by_shop = {row["shop_id"]: row for row in rows}
        # B5 店名有值、无 shop_id：按店名正常聚合（shop_id 为空串）。
        assert set(by_shop) == {"S1", "S2", "S3", "S4A", "S4B", ""}
        assert by_shop["S1"]["shop_amount"] == Decimal("550000")
        assert by_shop["S1"]["link_count"] == 2
        assert by_shop["S1"]["shop_type_rank"] == 1
        assert by_shop["S2"]["shop_amount"] == 500000
        assert by_shop["S2"]["tier"] == "达标"
        assert by_shop["S3"]["shop_amount"] == 250000  # 不跨日
        assert by_shop["S4A"]["shop_name"] == "同名店"
        assert by_shop["S4B"]["shop_name"] == "同名店"
        assert by_shop[""]["shop_name"] == "无ID店"
        assert by_shop[""]["shop_amount"] == Decimal("600000")
        assert "S6" not in by_shop
        assert all(row["hit_reasons_summary"] for row in rows)
        assert all(row["snapshot_date"] == "2026-11-05" for row in rows)
        assert all(row["run_id"] == "today" for row in rows)

        link_rows = _view_rows(session, "v_lead_links")
        s1_links = [
            row for row in link_rows if row["lead_id"] == by_shop["S1"]["lead_id"]
        ]
        assert len(s1_links) == 2
        assert all(row["hit_reasons"] for row in s1_links)
        assert all(row["amount_formula"] for row in s1_links)
        assert all("；" in row["hit_reasons"] for row in s1_links)

        b5_snapshot = b5_snaps[0]
        b5_product = session.get(Product, b5_snapshot.product_id)
        assert b5_product.shop_name == "无ID店"
        assert not any(
            "店铺ID缺失" in flag for flag in json.loads(b5_snapshot.missing_flags)
        )
        b6_snapshot = b6_snaps[0]
        assert any("下架" in reason for reason in json.loads(b6_snapshot.hit_reasons))

    # B7 京东临界带
    assert needs_jd_detail(Decimal("400000"), settings) is True
    assert needs_jd_detail(Decimal("200000"), settings) is False


# ---------------------------------------------------------------------------
# 附：金额 A1-A6、综合分托底/封顶、排除硬规则
# ---------------------------------------------------------------------------
def test_amount_cases_a1_a6(settings):
    a1 = compute_amount(
        price=Decimal("50"),
        sales=3000,
        comments=200,
        sales_raw="3000",
        comments_raw="200",
        existing_flags=[],
        settings=settings,
    )
    assert a1.amount == Decimal("150000.00")
    assert a1.formula == "50.00×3000=150000.00"

    a2 = compute_amount(
        price=Decimal("30"),
        sales=500,
        comments=100,
        sales_raw="500",
        comments_raw="100",
        existing_flags=[],
        settings=settings,
    )
    assert a2.amount == Decimal("15000.00")

    a3 = compute_amount(
        price=Decimal("99"),
        sales=None,
        comments=800,
        sales_raw=None,
        comments_raw="800",
        existing_flags=[],
        settings=settings,
    )
    assert a3.amount == Decimal("79200.00")
    assert a3.estimated_sales == 800
    assert "按评论数估算" in a3.flags
    assert "金额待核验" not in a3.flags
    assert a3.formula == "99.00×800（按评论数估算）=79200.00"

    a4 = compute_amount(
        price=Decimal("20"),
        sales=10000,
        comments=2000,
        sales_raw="1万+",
        comments_raw="2000",
        existing_flags=[],
        settings=settings,
    )
    assert a4.amount == Decimal("200000.00")
    assert "按下限估算" in a4.flags
    assert "按下限估算" in a4.formula

    a5 = compute_amount(
        price=Decimal("20"),
        sales=100,
        comments=None,
        sales_raw="100",
        comments_raw=None,
        existing_flags=[],
        settings=settings,
    )
    assert a5.amount == Decimal("2000.00")
    assert a5.estimated_sales == 100
    assert "评论缺失,待核验" not in a5.flags

    a6 = compute_amount(
        price=None,
        sales=100,
        comments=10,
        sales_raw="100",
        comments_raw="10",
        existing_flags=[],
        settings=settings,
    )
    assert a6.amount is None
    assert a6.estimated_sales is None
    assert "金额待核验" in a6.flags


def test_combined_score_floor_and_cap(settings):
    assert combine_scores(20, 80, settings) >= 75
    assert combine_scores(90, 10, settings) <= 35
    assert combine_scores(42, None, settings) == 42


def test_hard_exclusion_and_whitelist(settings, session_factory):
    day = date(2026, 11, 5)
    with session_scope(session_factory) as session:
        c1 = [
            make_item(
                product_id="C1001",
                title="官方旗舰店 正版授权 葫芦娃 童装",
                price=600,
                sales=1000,
                comments=100,
            )
        ]
        snaps, _ = _run_platform_items(session, settings, c1, day, "c")
        assert any(
            "排除:正版信号" in reason for reason in json.loads(snaps[0].hit_reasons)
        )
        assert snaps[0].amount == Decimal("600000.00")  # 金额仍计算，只是不生成线索

        session.add(
            Whitelist(
                platform="taobao", shop_id="S9", ip_code="huluxiongdi", note="授权"
            )
        )
        session.flush()
        c2 = [
            make_item(
                product_id="C2001", shop_id="S9", price=600, sales=1000, comments=100
            )
        ]
        snaps2, _ = _run_platform_items(session, settings, c2, day, "c")
        assert any(
            "排除:白名单" in reason for reason in json.loads(snaps2[0].hit_reasons)
        )
        aggregate_snapshots(
            session,
            settings=settings,
            snapshots=snaps + snaps2,
            snapshot_date=day,
            run_id="c",
        )
        assert session.query(Lead).count() == 0


def test_review_excel_import_and_invalid_value(settings, session_factory, tmp_path):
    from openpyxl import Workbook

    day = date(2026, 11, 5)
    with session_scope(session_factory) as session:
        items = [make_item(product_id="70001", price=600, sales=1000, comments=100)]
        snapped, _ = _run_platform_items(session, settings, items, day, "review")
        aggregate_snapshots(
            session,
            settings=settings,
            snapshots=snapped,
            snapshot_date=day,
            run_id="review",
        )
        session.commit()
        lead = session.query(Lead).one()
        lead_no = lead.lead_no

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "线索汇总"
    sheet.append(["线索编号", "复核结论", "复核人"])
    sheet.append([lead_no, "已确认", "Xuan"])
    sheet.append([lead_no, "随便写", "Xuan"])
    path = tmp_path / "review.xlsx"
    workbook.save(path)

    with session_scope(session_factory) as session:
        stats = import_review_excel(session, path)
        assert stats.updated == 1
        assert stats.skipped == 1
        session.commit()
    with session_scope(session_factory) as session:
        lead = session.query(Lead).one()
        assert lead.status == "已确认"
        assert lead.reviewer == "Xuan"
        assert session.query(LeadStatusHistory).count() == 1


def test_cli_second_instance_exits_2(tmp_path, settings):
    """模拟双击两次 run.bat：第二个 CLI 进程提示已在运行并以 2 退出。"""

    config_path = tmp_path / "rules.yaml"
    config_path.write_text(
        yaml.safe_dump(settings.raw, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    env = dict(os.environ)
    paths = [str(BACKEND_ROOT)]
    if os.name != "nt" and (BACKEND_ROOT / ".deps").is_dir():
        paths.insert(0, str(BACKEND_ROOT / ".deps"))
    env["PYTHONPATH"] = os.pathsep.join(paths)
    holder_code = (
        "import sys; from pathlib import Path; from pipeline.lock import acquire_lock; "
        f"lock=acquire_lock(Path({str(settings.lock_path)!r}), command='holder'); "
        "print('locked', flush=True); sys.stdin.read(); lock.release()"
    )
    env["PYTHONIOENCODING"] = "utf-8"
    holder = subprocess.Popen(
        [sys.executable, "-c", holder_code],
        cwd=BACKEND_ROOT,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "locked"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pipeline",
                "--config",
                str(config_path),
                "run",
                "--dry-run",
                "--platform",
                "taobao",
                "--no-alert",
            ],
            cwd=BACKEND_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )
        assert completed.returncode == 2
        assert "已在运行" in completed.stdout
    finally:
        assert holder.stdin is not None
        holder.stdin.close()
        try:
            holder.wait(timeout=10)
        except subprocess.TimeoutExpired:
            holder.terminate()
            holder.wait(timeout=10)


def test_whitelist_is_scoped_by_ip(settings, session_factory):
    day = date(2026, 11, 5)
    with session_scope(session_factory) as session:
        session.add(
            Whitelist(
                platform="taobao", shop_id="S9", ip_code="huluxiongdi", note="授权"
            )
        )
        session.flush()
        first = [
            make_item(
                product_id="C3001", shop_id="S9", price=600, sales=1000, comments=100
            )
        ]
        first_snaps, _ = _run_platform_items(session, settings, first, day, "c3")
        assert any("排除:白名单" in r for r in json.loads(first_snaps[0].hit_reasons))

        second = [
            make_item(
                product_id="C3002",
                shop_id="S9",
                title="叶罗丽 童装 批发",
                ip_code="yeluoli",
                price=600,
                sales=1000,
                comments=100,
            )
        ]
        second_snaps, _ = _run_platform_items(session, settings, second, day, "c3")
        assert not any(
            "排除:白名单" in r for r in json.loads(second_snaps[0].hit_reasons)
        )
        aggregate_snapshots(
            session,
            settings=settings,
            snapshots=first_snaps + second_snaps,
            snapshot_date=day,
            run_id="c3",
        )
        leads = session.query(Lead).all()
        assert len(leads) == 1
        assert "yeluoli" in leads[0].ip_codes


@pytest.mark.parametrize(
    "run_id,expected_leads", [("direct-crawler", 1), ("different-task", 0)]
)
def test_runner_scores_and_aggregates_direct_table_writes(
    settings, session_factory, run_id, expected_leads
):
    """只补判定和聚合本任务直接写入的快照，不能读取其他任务数据。"""
    from pipeline.store import upsert_many

    day = date(2026, 11, 5)
    with session_scope(session_factory) as session:
        upsert_many(
            session,
            platform="taobao",
            items=[make_item(product_id="D1001", price=600, sales=1000, comments=100)],
            settings=settings,
            run_id="direct-crawler",
            snapshot_date=day,
        )
        session.commit()
    adapter = FakeAdapter({"taobao": platform_data("taobao", [])})
    summary = PipelineRunner(settings, adapter=adapter).run(
        platforms=["taobao"], run_date=day, allow_alert=False, run_id=run_id
    )
    assert summary.leads == expected_leads
    with session_scope(session_factory) as session:
        snapshot = session.query(ProductSnapshot).one()
        if expected_leads:
            lead = session.query(Lead).one()
            assert lead.total_amount == Decimal("600000.00")
            assert lead.run_id == run_id
            assert snapshot.final_score is not None
        else:
            assert session.query(Lead).count() == 0
            assert snapshot.final_score is None
