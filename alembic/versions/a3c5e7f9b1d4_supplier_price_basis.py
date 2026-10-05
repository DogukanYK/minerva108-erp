"""Tedarikçi fiyatları — fiyat temeli (birim + kaynak + tarih)

supplier_prices'a price_unit (kg | l | adet), source (stok_son_durum |
manual), source_label ve quoted_at eklenir. Stok Son Durum listesi USD/kg'dir;
eski satırlar model varsayılanıyla currency='TRY' yazılmış ve birimsizdi →
price_unit IS NULL olanlar USD + kg (adet/sayılan birimli malzemede 'adet')
olarak işaretlenir. Kural `core.supplier_prices.default_price_unit` ile
aynıdır (ağırlık g/gr/kg ve hacim ml/l/lt dışındaki her birim 'adet').

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — prod şeması `init_db()` alter_safe
bloğuyla evrilir; aynı kolonlar orada da eklenir ve aynı doldurma
`_backfill_supplier_price_units()` (AppSetting sentinel
'backfill.supplier_price_units.v1') ile bir kez yapılır.  Bu dosya alembic
geçmişi ve temiz kurulum içindir.  Kolonları init_db zaten eklemiş bir DB'de
(prod, dev) `alembic upgrade head` ilk `add_column`'da duplicate-column
hatasıyla DÜŞER — orada `alembic stamp head` kullan, `make migrate` DEĞİL.
Yalnız veri UPDATE'i sıra-bağımsızdır (WHERE price_unit IS NULL).

Revision ID: a3c5e7f9b1d4
Revises: d1f3a5c7e9b2
Create Date: 2026-10-05 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a3c5e7f9b1d4'
down_revision: Union[str, Sequence[str], None] = 'd1f3a5c7e9b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('supplier_prices', sa.Column('price_unit',   sa.String(8),   nullable=True))
    op.add_column('supplier_prices', sa.Column('source',       sa.String(30),  nullable=True))
    op.add_column('supplier_prices', sa.Column('source_label', sa.String(120), nullable=True))
    op.add_column('supplier_prices', sa.Column('quoted_at',    sa.Date(),      nullable=True))
    op.execute("""
        UPDATE supplier_prices AS sp
           SET currency   = 'USD',
               source     = 'stok_son_durum',
               price_unit = CASE
                   WHEN lower(trim(coalesce(i.unit, ''))) IN ('g', 'gr', 'kg', 'ml', 'l', 'lt')
                   THEN 'kg' ELSE 'adet' END
          FROM items AS i
         WHERE i.id = sp.item_id
           AND sp.price_unit IS NULL
    """)


def downgrade() -> None:
    # currency geri alınmaz — 'TRY' zaten yanlıştı (liste USD).
    op.drop_column('supplier_prices', 'quoted_at')
    op.drop_column('supplier_prices', 'source_label')
    op.drop_column('supplier_prices', 'source')
    op.drop_column('supplier_prices', 'price_unit')
