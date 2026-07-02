"""Ürün iadesi — product_returns + product_return_items

Çıkışı yapılmış (stoktan düşülmüş) ürünlerin geri alınması: teslimata bağlı kısmi
iade (aşırı-iade SUM guard) + serbest iade (online platform). SAĞLAM kalem stoğa
döner (Transaction Input 'İade (RET-…)'), HASARLI/AÇILMIŞ girmez (fire izi).
init_db() create_all bu tabloları açılışta kurar; migration alembic geçmişi içindir.

Revision ID: c4f6a8b2d1e5
Revises: d7f9b1c3e5a2
Create Date: 2026-06-30 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4f6a8b2d1e5'
down_revision: Union[str, Sequence[str], None] = 'd7f9b1c3e5a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'product_returns',
        sa.Column('id',                   sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('document_no',          sa.String(40), nullable=True),
        sa.Column('delivery_id',          sa.Integer(), sa.ForeignKey('deliveries.id'), nullable=True),
        sa.Column('delivery_document_no', sa.String(40), nullable=True),
        sa.Column('channel',              sa.String(30), server_default='teslimat'),
        sa.Column('reason',               sa.Text(), nullable=True),
        sa.Column('returned_by',          sa.String(150), nullable=True),
        sa.Column('received_by',          sa.String(80), nullable=True),
        sa.Column('doc_lang',             sa.String(8), server_default='TR'),
        sa.Column('domain',               sa.String(20), nullable=False, server_default='cosmetics'),
        sa.Column('created_at',           sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_product_returns_document_no', 'product_returns', ['document_no'], unique=True)
    op.create_index('ix_product_returns_delivery_id', 'product_returns', ['delivery_id'])
    op.create_index('ix_product_returns_domain', 'product_returns', ['domain'])

    op.create_table(
        'product_return_items',
        sa.Column('id',               sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('return_id',        sa.Integer(), sa.ForeignKey('product_returns.id'), nullable=False),
        sa.Column('delivery_item_id', sa.Integer(), sa.ForeignKey('delivery_items.id'), nullable=True),
        sa.Column('item_id',          sa.Integer(), sa.ForeignKey('items.id'), nullable=True),
        sa.Column('item_name',        sa.String(150), nullable=False),
        sa.Column('quantity',         sa.Float(), nullable=False),
        sa.Column('unit',             sa.String(20), nullable=True),
        sa.Column('condition',        sa.String(20), nullable=False, server_default='saglam'),
        sa.Column('restocked',        sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('note',             sa.Text(), nullable=True),
    )
    op.create_index('ix_product_return_items_return_id', 'product_return_items', ['return_id'])
    op.create_index('ix_product_return_items_delivery_item_id', 'product_return_items', ['delivery_item_id'])


def downgrade() -> None:
    op.drop_index('ix_product_return_items_delivery_item_id', table_name='product_return_items')
    op.drop_index('ix_product_return_items_return_id', table_name='product_return_items')
    op.drop_table('product_return_items')
    op.drop_index('ix_product_returns_domain', table_name='product_returns')
    op.drop_index('ix_product_returns_delivery_id', table_name='product_returns')
    op.drop_index('ix_product_returns_document_no', table_name='product_returns')
    op.drop_table('product_returns')
