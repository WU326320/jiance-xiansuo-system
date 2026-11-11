"""数据库引擎、会话与迁移入口。"""

from __future__ import annotations

import os
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Iterator

from alembic import command
from alembic.config import Config as AlembicConfig
from sqlalchemy import Engine, create_engine, event, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from pipeline.config import BACKEND_ROOT, Settings, load_config
from pipeline.models import Base

ALEMBIC_INI = BACKEND_ROOT / "alembic.ini"
ALEMBIC_DIR = BACKEND_ROOT / "alembic"


def build_url(database_path: Path) -> str:
    return f"sqlite:///{database_path.as_posix()}"


def database_url(settings: Settings) -> str:
    """连接串；设置 IP_LEAD_DB_URL 即可换成 PostgreSQL（只需改环境变量）。"""

    return os.environ.get("IP_LEAD_DB_URL") or build_url(settings.database_path)


def create_engine_for(settings: Settings | None = None) -> Engine:
    settings = settings or load_config()
    url = database_url(settings)
    if url.startswith("sqlite"):
        database_path = settings.database_path
        database_path.parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(
            url,
            future=True,
            connect_args={"timeout": 30, "check_same_thread": False},
        )

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection, _connection_record):  # noqa: ANN001
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
                cursor.execute("PRAGMA synchronous=NORMAL")
            finally:
                cursor.close()

        return engine
    return create_engine(url, future=True, pool_pre_ping=True)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ensure_schema(settings: Settings | None = None) -> None:
    """执行 ``alembic upgrade head``；一键运行入口会自动调用。"""

    settings = settings or load_config()
    if database_url(settings).startswith("sqlite"):
        settings.database_path.parent.mkdir(parents=True, exist_ok=True)
        if settings.database_path.exists():
            import sqlite3
            from datetime import datetime

            with closing(sqlite3.connect(str(settings.database_path))) as source:
                columns = {
                    row[1]: row
                    for row in source.execute("PRAGMA table_info(product_snapshots)")
                }
                link_columns = {
                    row[1]: row
                    for row in source.execute("PRAGMA table_info(lead_links)")
                }
                needs_amount_migration = columns and (
                    "price_raw" not in columns
                    or bool(columns.get("amount", (None,) * 4)[3])
                    or bool(link_columns.get("amount", (None,) * 4)[3])
                )
                if needs_amount_migration:
                    destination = settings.database_path.with_name(
                        settings.database_path.stem
                        + f".before_amount_{datetime.now():%Y%m%d_%H%M%S_%f}.db"
                    )
                    with closing(sqlite3.connect(str(destination))) as target:
                        source.backup(target)
    cfg = AlembicConfig(str(ALEMBIC_INI))
    cfg.set_main_option("script_location", str(ALEMBIC_DIR))
    cfg.set_main_option("sqlalchemy.url", database_url(settings))
    cfg.attributes["settings"] = settings
    command.upgrade(cfg, "head")


def database_url_for_alembic(settings: Settings) -> str:
    return database_url(settings)


def existing_tables(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def existing_views(engine: Engine) -> set[str]:
    return set(inspect(engine).get_view_names())


def drop_all(engine: Engine) -> None:
    """测试辅助：先删视图再删表。"""

    with engine.begin() as conn:
        conn.execute(text("DROP VIEW IF EXISTS v_near_threshold_shops"))
        conn.execute(text("DROP VIEW IF EXISTS v_lead_links"))
        conn.execute(text("DROP VIEW IF EXISTS v_lead_summary"))
    Base.metadata.drop_all(engine)


__all__ = [
    "ALEMBIC_DIR",
    "ALEMBIC_INI",
    "build_url",
    "create_engine_for",
    "database_url",
    "database_url_for_alembic",
    "drop_all",
    "ensure_schema",
    "existing_tables",
    "existing_views",
    "make_session_factory",
    "session_scope",
]
