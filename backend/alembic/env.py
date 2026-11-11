from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import engine_from_config, pool

BACKEND_ROOT = Path(__file__).resolve().parent.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from pipeline.config import load_config  # noqa: E402
from pipeline.db import build_url  # noqa: E402
from pipeline.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None and os.environ.get("IP_LEAD_ALEMBIC_LOG", "1") == "1":
    try:
        fileConfig(config.config_file_name, disable_existing_loggers=False)
    except Exception:
        pass

target_metadata = Base.metadata


def _database_url() -> str:
    settings = config.attributes.get("settings")
    env_url = os.environ.get("IP_LEAD_DB_URL")
    if env_url:
        return env_url
    env_path = os.environ.get("IP_LEAD_DB_PATH")
    if env_path:
        return build_url(Path(env_path).expanduser().resolve())
    if settings is not None:
        return build_url(settings.database_path)
    configured = config.get_main_option("sqlalchemy.url")
    return configured


def run_migrations_offline() -> None:
    url = _database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    section = config.get_section(config.config_ini_section, {})
    section["sqlalchemy.url"] = _database_url()
    connectable = engine_from_config(
        section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
