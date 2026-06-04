"""domain kolonları — Kozmetik / Food Supplement ayrımı (Faz 3)

Item, Supplier, Recipe, Inventory, ProductionHistory, Quotation tablolarına
domain VARCHAR(20) NOT NULL DEFAULT 'cosmetics' eklenir.  PostgreSQL'de
DEFAULT'lu ADD COLUMN mevcut satırları otomatik 'cosmetics' ile doldurur,
böylece tüm geçmiş veri kozmetik panelinde kalır.

init_db() alter_safe bloğu da bu kolonları her açılışta idempotent ekler;
migration alembic geçmişi + temiz kurulum içindir.

Revision ID: d4a2e9c1f6b8
Revises: c3f1a8d2b5e7
Create Date: 2026-06-04 09:50:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd4a2e9c1f6b8'
down_revision: Union[str, Sequence[str], None] = 'c3f1a8d2b5e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("items", "suppliers", "recipes", "inventory", "production_history",
           "quotations", "stock_snapshot")


def upgrade() -> None:
    for tbl in _TABLES:
        op.add_column(
            tbl,
            sa.Column("domain", sa.String(20), nullable=False, server_default="cosmetics"),
        )
        op.create_index(f"ix_{tbl}_domain", tbl, ["domain"])


def downgrade() -> None:
    for tbl in _TABLES:
        op.drop_index(f"ix_{tbl}_domain", table_name=tbl)
        op.drop_column(tbl, "domain")
