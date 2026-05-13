"""performance: indexes on transactions/inventory/items/recipes

Tablolardaki en sık WHERE / JOIN / ORDER BY kolonlarına BTREE index ekler.
Mevcut yükte (500 row mertebesi) tablo taramaları milisaniyeler tutuyor;
ama 5K+ row'a çıkınca her endpoint'te 100ms+ kayıp olabilir.  Bu migration
buna karşı önleyici:

  • transactions.timestamp           → user-activity feed, ledger sort
  • transactions.performed_by        → audit-by-user query
  • transactions.transaction_type    → top-usage report filtreleri
  • inventory.status                 → QC quarantine listesi
  • inventory.expiry_date            → daily expiry scan (scheduler)
  • inventory.qc_required            → QC üretim listesi
  • recipe_ingredients.recipe_id     → recipe detail join
  • recipe_ingredients.item_id       → "this item is used in N recipes" lookup
  • items.supplier_id                → supplier filter (yeni eklenen)
  • items.category                   → kategori tab'i filtresi

Tüm index'ler "IF NOT EXISTS" şeklinde — idempotent.  Tablolar küçük
olduğu için lock süresi milisaniyeler — lab aktifken bile takılmaz.

Revision ID: a7af35288275
Revises: 1e82b4e52ec3
Create Date: 2026-05-13 10:52:52.744763
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'a7af35288275'
down_revision: Union[str, Sequence[str], None] = '1e82b4e52ec3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (index_name, table, column, optional partial WHERE clause)
INDEXES = [
    ("ix_transactions_timestamp",       "transactions",       "timestamp",        None),
    ("ix_transactions_performed_by",    "transactions",       "performed_by",     None),
    ("ix_transactions_type",            "transactions",       "transaction_type", None),
    ("ix_inventory_status",             "inventory",          "status",           None),
    ("ix_inventory_expiry_date",        "inventory",          "expiry_date",      "expiry_date IS NOT NULL"),
    ("ix_inventory_qc_required",        "inventory",          "qc_required",      "qc_required = true"),
    ("ix_recipe_ingredients_recipe_id", "recipe_ingredients", "recipe_id",        None),
    ("ix_recipe_ingredients_item_id",   "recipe_ingredients", "item_id",          None),
    ("ix_items_supplier_id",            "items",              "supplier_id",      "supplier_id IS NOT NULL"),
    ("ix_items_category",               "items",              "category",         None),
]


def upgrade() -> None:
    for name, table, col, where in INDEXES:
        if where:
            op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({col}) WHERE {where}")
        else:
            op.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({col})")


def downgrade() -> None:
    for name, _t, _c, _w in INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
