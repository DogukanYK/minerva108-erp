"""Tedarikçi fiyatının KDV bilgisi (supplier_prices.vat_included / vat_rate)

Lab fiyat notlarında (07.10.2026) Pharmaterm uçucu yağ fiyatları KDV DAHİL
brüt €, alım belgeleri KDV hariç + oran.  `vat_included` NULL = bilinmiyor
(eski satırlar, Stok Son Durum listesi), False = KDV hariç, True = KDV dahil;
`vat_rate` yüzde (20 = %20).  Satın alma karşılaştırması net fiyatla yapılır
(core/supplier_prices.net_unit_price).  Veri yazılmaz — iki kolon da boş
başlar, davranış eskisiyle aynı kalır.

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — kolonlar prod'a `init_db()`
alter_safe satırlarıyla gelir.  Bu dosya alembic geçmişi ve temiz kurulum
içindir; kolonları init_db zaten eklemiş bir DB'de `alembic stamp head` kullan.

Revision ID: d8f0b2c4e6a9
Revises: c4e6a8b0d2f5
Create Date: 2026-10-08 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd8f0b2c4e6a9'
down_revision: Union[str, Sequence[str], None] = 'c4e6a8b0d2f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('supplier_prices', sa.Column('vat_included', sa.Boolean(), nullable=True))
    op.add_column('supplier_prices', sa.Column('vat_rate', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('supplier_prices', 'vat_rate')
    op.drop_column('supplier_prices', 'vat_included')
