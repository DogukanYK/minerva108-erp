"""
Alembic env — Minerva108'in SQLAlchemy modelleriyle hizalanmış migration setup.

Tasarım kararı:  DATABASE_URL env'inden okunur (alembic.ini'deki placeholder
yerine), aynı .env akışıyla uyumlu.  Hem dev (lokal PG) hem prod (Turhost PG)
aynı kodla çalışır.
"""
import os
import sys
from logging.config import fileConfig
from pathlib import Path

from sqlalchemy import engine_from_config, pool
from alembic import context

# .env'i en başta yükle (DATABASE_URL set edilmeden engine oluşmamalı)
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except ImportError:
    pass

# Proje root'unu path'e ekle (database.py'yi import edebilelim)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from database import Base  # noqa: E402

config = context.config

# alembic.ini'deki sqlalchemy.url placeholder'ını env'den geleni ile değiştir
db_url = os.getenv("DATABASE_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Autogenerate için target metadata
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # Tip değişikliklerini de yakala (örn. VARCHAR(50) → VARCHAR(100))
            compare_type=True,
            # Server default değişikliklerini de yakala
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
