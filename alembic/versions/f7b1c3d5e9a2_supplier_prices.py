"""Tedarikçi fiyat listesi — supplier_prices tablosu

Malzeme × tedarikçi başına birim fiyat + alınabilecek paket / min-sipariş miktarı.
Satın alma raporunu (Işık Hanım'ın "Stok Son Durum" tablosu) otomatik doldurur.
init_db() create_all bu tabloyu açılışta kurar; migration alembic geçmişi +
temiz kurulum içindir.

Revision ID: f7b1c3d5e9a2
Revises: f1a2b3c4d5e6
Create Date: 2026-06-23 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f7b1c3d5e9a2'
down_revision: Union[str, Sequence[str], None] = 'f1a2b3c4d5e6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'supplier_prices',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('item_id',       sa.Integer(), sa.ForeignKey('items.id'), nullable=False),
        sa.Column('supplier_id',   sa.Integer(), sa.ForeignKey('suppliers.id'), nullable=True),
        sa.Column('supplier_name', sa.String(150), nullable=True),
        sa.Column('package_size',  sa.Float(), nullable=True),
        sa.Column('unit_price',    sa.Float(), nullable=True),
        sa.Column('currency',      sa.String(8), server_default='TRY'),
        sa.Column('note',          sa.Text(), nullable=True),
        sa.Column('domain',        sa.String(20), nullable=False, server_default='cosmetics'),
        sa.Column('created_at',    sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at',    sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_supplier_prices_item_id', 'supplier_prices', ['item_id'])
    op.create_index('ix_supplier_prices_domain',  'supplier_prices', ['domain'])


def downgrade() -> None:
    op.drop_index('ix_supplier_prices_domain',  table_name='supplier_prices')
    op.drop_index('ix_supplier_prices_item_id', table_name='supplier_prices')
    op.drop_table('supplier_prices')
