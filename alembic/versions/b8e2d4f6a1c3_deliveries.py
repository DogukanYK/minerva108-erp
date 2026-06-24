"""Teslimat (hediye/numune çıkışı) — deliveries + delivery_items

Barkod okutarak market-kasası mantığıyla stok düşülen hediye/numune teslimatları
ve imzalı teslim belgesi. init_db() create_all bu tabloları açılışta kurar;
migration alembic geçmişi + temiz kurulum içindir.

NOT: Bu revision ile a2c4e6081357 (CRM/Kommo) aynı parent'tan (f7b1c3d5e9a2)
dallanır — iki iş birleşince `alembic merge` ile tek head'e indirilir. Prod yeni
tabloları create_all ile kurduğundan bu dallanma çalışmayı etkilemez.

Revision ID: b8e2d4f6a1c3
Revises: f7b1c3d5e9a2
Create Date: 2026-06-24 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8e2d4f6a1c3'
down_revision: Union[str, Sequence[str], None] = 'f7b1c3d5e9a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'deliveries',
        sa.Column('id',              sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('document_no',     sa.String(40), nullable=True),
        sa.Column('recipient_name',  sa.String(150), nullable=False),
        sa.Column('recipient_org',   sa.String(150), nullable=True),
        sa.Column('recipient_phone', sa.String(40), nullable=True),
        sa.Column('delivery_type',   sa.String(20), server_default='hediye'),
        sa.Column('method',          sa.String(20), server_default='elden'),
        sa.Column('note',            sa.Text(), nullable=True),
        sa.Column('dispatched_by',   sa.String(80), nullable=True),
        sa.Column('domain',          sa.String(20), nullable=False, server_default='cosmetics'),
        sa.Column('created_at',      sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_deliveries_document_no', 'deliveries', ['document_no'], unique=True)
    op.create_index('ix_deliveries_domain',      'deliveries', ['domain'])
    op.create_table(
        'delivery_items',
        sa.Column('id',          sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('delivery_id', sa.Integer(), sa.ForeignKey('deliveries.id'), nullable=False),
        sa.Column('item_id',     sa.Integer(), sa.ForeignKey('items.id'), nullable=True),
        sa.Column('item_name',   sa.String(150), nullable=False),
        sa.Column('quantity',    sa.Float(), nullable=False),
        sa.Column('unit',        sa.String(20), nullable=True),
    )
    op.create_index('ix_delivery_items_delivery_id', 'delivery_items', ['delivery_id'])


def downgrade() -> None:
    op.drop_index('ix_delivery_items_delivery_id', table_name='delivery_items')
    op.drop_table('delivery_items')
    op.drop_index('ix_deliveries_domain',      table_name='deliveries')
    op.drop_index('ix_deliveries_document_no', table_name='deliveries')
    op.drop_table('deliveries')
