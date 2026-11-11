"""工作台测试夹具：全部使用临时数据库，邮件只进内存。"""

from __future__ import annotations

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
DEPS = BACKEND / ".deps"
for path in (BACKEND, DEPS):
    if path.is_dir() and str(path) not in sys.path:
        sys.path.insert(0, str(path))

from workbench.app import WorkbenchApp  # noqa: E402
from workbench.auth import AuthService  # noqa: E402
from workbench.config import WorkbenchConfig  # noqa: E402
from workbench.db import Database  # noqa: E402
from workbench.mailer import Mailer  # noqa: E402
from workbench.tasks import TaskService  # noqa: E402


@pytest.fixture
def tmp_config(tmp_path: Path, monkeypatch) -> WorkbenchConfig:
    data = tmp_path / "wb-data"
    leads_db = tmp_path / "leads.db"
    monkeypatch.setenv("IP_LEAD_DB_PATH", str(leads_db))
    monkeypatch.delenv("IP_LEAD_DB_URL", raising=False)
    config = WorkbenchConfig(
        project_root=ROOT,
        data_dir=data,
        db_path=data / "workbench.db",
        host="127.0.0.1",
        port=8765,
        allowed_networks=["127.0.0.1/32", "192.168.0.0/16"],
        session_idle_hours=8,
        login_lock_threshold=5,
        login_lock_minutes=15,
        rules_path=BACKEND / "config" / "rules.yaml",
        crawler_config_path=ROOT / "crawler" / "config" / "windows.yaml",
        leads_db_path=leads_db,
        smtp={
            "enabled": False,
            "smtp_host": "",
            "smtp_port": 465,
            "smtp_ssl": True,
            "from": "",
            "to": [],
            "keyring_service": "workbench-test",
            "subject_prefix": "[测试]",
        },
    )
    config.ensure_dirs()
    _ensure_leads_db(config)
    return config


def _ensure_leads_db(config: WorkbenchConfig) -> None:
    from workbench import backend_bridge as bridge

    settings = bridge.load_backend_settings(config, db_path=config.leads_db_path)
    bridge.ensure_leads_schema(settings)


@pytest.fixture
def db(tmp_config: WorkbenchConfig) -> Database:
    database = Database(tmp_config.db_path)
    database.init_schema()
    return database


@pytest.fixture
def supervisor(db: Database, tmp_config: WorkbenchConfig) -> dict[str, Any]:
    auth = AuthService(tmp_config, db)
    user = auth.create_supervisor("supervisor", "Super1234")
    return auth.get_user(user["id"])


@pytest.fixture
def admin(db: Database, tmp_config: WorkbenchConfig, supervisor) -> dict[str, Any]:
    auth = AuthService(tmp_config, db)
    invite = auth.create_invite(supervisor, role="admin", max_uses=1)
    user = auth.register(invite["code"], "admin", "Admin1234")
    return auth.get_user(user["id"])


@pytest.fixture
def normal_user(
    db: Database, tmp_config: WorkbenchConfig, supervisor
) -> dict[str, Any]:
    auth = AuthService(tmp_config, db)
    invite = auth.create_invite(supervisor, role="user", max_uses=1)
    user = auth.register(invite["code"], "user1", "User1234")
    return auth.get_user(user["id"])


@pytest.fixture
def mailbox() -> list[tuple[Any, list[str]]]:
    return []


@pytest.fixture
def mailer(tmp_config: WorkbenchConfig, db: Database, mailbox) -> Mailer:
    def transport(message, recipients):
        mailbox.append((message, recipients))

    return Mailer(tmp_config, db, transport=transport)


@pytest.fixture
def app(tmp_config: WorkbenchConfig, db: Database, mailer: Mailer) -> WorkbenchApp:
    return WorkbenchApp(tmp_config, db=db, mailer=mailer)


class Client:
    def __init__(self, app: WorkbenchApp, remote_addr: str = "127.0.0.1"):
        self.app = app
        self.remote_addr = remote_addr
        self.cookies: dict[str, str] = {}
        self.csrf = ""

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        headers: dict[str, str] | None = None,
        remote_addr: str | None = None,
    ):
        body = b""
        request_headers = dict(headers or {})
        if json_body is not None:
            body = json.dumps(json_body, ensure_ascii=False).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        if self.csrf and method.upper() not in ("GET", "HEAD"):
            request_headers.setdefault("X-CSRF-Token", self.csrf)
        cookie = "; ".join(f"{key}={value}" for key, value in self.cookies.items())
        environ: dict[str, Any] = {
            "REQUEST_METHOD": method.upper(),
            "PATH_INFO": path.split("?", 1)[0],
            "QUERY_STRING": path.split("?", 1)[1] if "?" in path else "",
            "SERVER_NAME": "testserver",
            "SERVER_PORT": "80",
            "SERVER_PROTOCOL": "HTTP/1.1",
            "wsgi.version": (1, 0),
            "wsgi.url_scheme": "http",
            "wsgi.input": __import__("io").BytesIO(body),
            "wsgi.errors": __import__("io").StringIO(),
            "wsgi.multithread": False,
            "wsgi.multiprocess": False,
            "wsgi.run_once": False,
            "REMOTE_ADDR": remote_addr or self.remote_addr,
            "CONTENT_LENGTH": str(len(body)),
        }
        for key, value in request_headers.items():
            environ["HTTP_" + key.upper().replace("-", "_")] = value
        if cookie:
            environ["HTTP_COOKIE"] = cookie
        captured: dict[str, Any] = {}

        def start_response(status, headers, exc_info=None):
            captured["status"] = status
            captured["headers"] = headers

        chunks = self.app(environ, start_response)
        payload = b"".join(chunks)
        for key, value in captured.get("headers", []):
            if key.lower() == "set-cookie":
                pair = value.split(";", 1)[0]
                name, _, val = pair.partition("=")
                if val:
                    self.cookies[name] = val
                else:
                    self.cookies.pop(name, None)
        status = int(str(captured.get("status", "200 OK")).split()[0])
        try:
            parsed = json.loads(payload.decode("utf-8")) if payload else None
        except (UnicodeDecodeError, ValueError):
            parsed = payload.decode("utf-8", errors="replace")
        return status, parsed, dict(captured.get("headers", []))

    def login(self, username: str, password: str):
        status, body, _ = self.request(
            "POST", "/api/login", json_body={"username": username, "password": password}
        )
        if status == 200 and isinstance(body, dict):
            self.csrf = body.get("csrf_token", "")
        return status, body


@pytest.fixture
def client(app: WorkbenchApp) -> Client:
    return Client(app)


def make_lead(
    config: WorkbenchConfig,
    *,
    lead_no: str = "taobao-20261106-0001",
    status: str = "待复核",
    amount: float = 600000.0,
    shop_name: str = "测试旗舰店",
    shop_id: str | None = None,
) -> dict[str, Any]:
    """在临时 leads.db 插入一条线索和链接。"""

    from workbench import backend_bridge as bridge
    from pipeline.models import Lead, LeadLink, Product, ProductSnapshot, now_local

    settings = bridge.load_backend_settings(config, db_path=config.leads_db_path)
    bridge.ensure_leads_schema(settings)
    with bridge.leads_session(settings) as session:
        product = Product(
            platform="taobao",
            product_id="P-" + lead_no,
            url="https://item.example/" + lead_no,
            title="测试商品 " + lead_no,
            shop_id=shop_id or ("S-" + lead_no),
            shop_name=shop_name,
            shop_type="企业",
        )
        session.add(product)
        session.flush()
        snapshot = ProductSnapshot(
            product_id=product.id,
            snapshot_date=date(2026, 11, 6),
            crawled_at=datetime(2026, 11, 6, 10, 0, 0),
            run_id="RUN-TEST",
            ip_code="huluxiongdi",
            price=50,
            sales_raw="10000",
            sales=10000,
            comments_raw="1200",
            comments=1200,
            listed=True,
            text_score=88,
            visual_score=90,
            final_score=89,
            need_vision=False,
            eligible=True,
            hit_reasons='["文字:葫芦娃:88"]',
            amount=amount,
            formula="50.00×min(10000,1200×10)=600000.00",
            estimated_sales=10000,
            missing_flags="[]",
            config_version="test",
            caliber_version="v1.0",
        )
        session.add(snapshot)
        session.flush()
        lead = Lead(
            lead_no=lead_no,
            run_id="RUN-TEST",
            snapshot_date=date(2026, 11, 6),
            platform="taobao",
            platform_group="taobao",
            shop_key="taobao|测试旗舰店",
            shop_id=shop_id or ("S-" + lead_no),
            shop_name=shop_name,
            shop_type="企业",
            total_amount=amount,
            link_count=1,
            ip_codes="huluxiongdi",
            payment_tags="企业",
            confidence=89,
            hit_summary="文字:葫芦娃:88",
            missing_flags="[]",
            status=status,
            caliber_version="v1.0",
            config_version="test",
        )
        session.add(lead)
        session.flush()
        session.add(
            LeadLink(
                lead_id=lead.id,
                snapshot_id=snapshot.id,
                ip_code="huluxiongdi",
                amount=amount,
                formula=snapshot.formula,
            )
        )
    return {"lead_no": lead_no}


__all__ = ["Client", "make_lead"]
