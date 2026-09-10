"""Eksik Hammaddeler raporu — stock_order_flags

Sıfır / kritik altı / sadece numune hammaddeler için "sipariş verildi"
işareti. Ürün başına en fazla bir AÇIK (closed_at IS NULL) bayrak — kısmi
tekil indeksle zorlanır. Gerçek mal kabul (receive_stock numune-olmayan
dal + numune stoğa çevirme) closed_reason='received' ile otomatik kapatır;
elle yeniden işaretleme eskisini 'manual' ile kapatır. Rapor mantığı
core/stock_gaps.py'dedir. init_db() create_all bu tabloyu açılışta kurar
(eklenen kolon yok, alter_safe'e dokunmaya gerek yok); migration alembic
geçmişi + temiz kurulum içindir (deploy.sh alembic ÇALIŞTIRMIYOR).

Revision ID: d1f3a5c7e9b2
Revises: c7e9a1b3d5f7
Create Date: 2026-09-10 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1f3a5c7e9b2'
down_revision: Union[str, Sequence[str], None] = 'c7e9a1b3d5f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'stock_order_flags',
        sa.Column('id',             sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('item_id',        sa.Integer(), sa.ForeignKey('items.id'), nullable=False),
        sa.Column('supplier_id',    sa.Integer(), sa.ForeignKey('suppliers.id'), nullable=True),
        sa.Column('quantity',       sa.Float(), nullable=True),
        sa.Column('unit',           sa.String(20), nullable=True),
        sa.Column('expected_date',  sa.Date(), nullable=True),
        sa.Column('note',           sa.String(300), nullable=True),
        sa.Column('ordered_by',     sa.String(50), nullable=True),
        sa.Column('ordered_at',     sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('closed_at',      sa.DateTime(), nullable=True),
        sa.Column('closed_reason',  sa.String(10), nullable=True),
        sa.Column('domain',         sa.String(20), nullable=False, server_default='cosmetics'),
        sa.Column('created_at',     sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_stock_order_flags_item_id', 'stock_order_flags', ['item_id'])
    op.create_index('ix_stock_order_flags_domain', 'stock_order_flags', ['domain'])
    op.create_index(
        'uq_stock_order_flags_open_item', 'stock_order_flags', ['item_id'],
        unique=True, postgresql_where=sa.text('closed_at IS NULL'),
    )


def downgrade() -> None:
    op.drop_index('uq_stock_order_flags_open_item', table_name='stock_order_flags')
    op.drop_index('ix_stock_order_flags_domain', table_name='stock_order_flags')
    op.drop_index('ix_stock_order_flags_item_id', table_name='stock_order_flags')
    op.drop_table('stock_order_flags')
