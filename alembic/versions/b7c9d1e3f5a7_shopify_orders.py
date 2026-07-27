"""Shopify sipariş kaydı — shopify_orders (Faz 2: orders/paid webhook)

Webhook idempotency (store_key+shopify_order_id unique) + stok düşümü ve Paraşüt
fatura otomasyonunun adım bazlı durum makinesi. init_db() create_all bu tabloyu
açılışta kurar; migration alembic geçmişi + temiz kurulum içindir.

Revision ID: b7c9d1e3f5a7
Revises: a1b2c3d4e5f6
Create Date: 2026-07-27 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c9d1e3f5a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'shopify_orders',
        sa.Column('id',                 sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('store_key',          sa.String(20), nullable=False),
        sa.Column('shopify_order_id',   sa.BigInteger(), nullable=False),
        sa.Column('order_number',       sa.String(40), nullable=True),
        sa.Column('status',             sa.String(30), nullable=False, server_default='received'),
        sa.Column('step',               sa.String(30), nullable=True),
        sa.Column('stock_applied',      sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('total',              sa.Float(), nullable=True),
        sa.Column('currency',           sa.String(8), nullable=True),
        sa.Column('country',            sa.String(8), nullable=True),
        sa.Column('customer_email',     sa.String(150), nullable=True),
        sa.Column('customer_name',      sa.String(150), nullable=True),
        sa.Column('vkn_tckn',           sa.String(20), nullable=True),
        sa.Column('attempts',           sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_error',         sa.String(500), nullable=True),
        sa.Column('parasut_contact_id', sa.BigInteger(), nullable=True),
        sa.Column('parasut_invoice_id', sa.BigInteger(), nullable=True),
        sa.Column('parasut_doc_type',   sa.String(20), nullable=True),
        sa.Column('parasut_doc_id',     sa.BigInteger(), nullable=True),
        sa.Column('trackable_job_id',   sa.String(80), nullable=True),
        sa.Column('lines_json',         sa.Text(), nullable=True),
        sa.Column('created_at',         sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('updated_at',         sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('store_key', 'shopify_order_id', name='uq_shopify_orders_store_order'),
    )
    op.create_index('ix_shopify_orders_store_key', 'shopify_orders', ['store_key'])
    op.create_index('ix_shopify_orders_status', 'shopify_orders', ['status'])


def downgrade() -> None:
    op.drop_index('ix_shopify_orders_status', table_name='shopify_orders')
    op.drop_index('ix_shopify_orders_store_key', table_name='shopify_orders')
    op.drop_table('shopify_orders')
