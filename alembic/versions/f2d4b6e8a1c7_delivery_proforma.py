"""Teslimat: çift-dilli ad (items.name_tr) + belge dili + proforma fatura alanları

items.name_tr; deliveries: doc_lang, customer_address/country, currency, status,
approved_by/at, reject_reason; delivery_items: unit_price, weight_ml.
init_db() alter_safe bunları açılışta ekler; migration alembic geçmişi içindir.

Revision ID: f2d4b6e8a1c7
Revises: e1f3a7c9d2b5
Create Date: 2026-06-26 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f2d4b6e8a1c7'
down_revision: Union[str, Sequence[str], None] = 'e1f3a7c9d2b5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('items', sa.Column('name_tr', sa.String(150), nullable=True))
    op.add_column('deliveries', sa.Column('doc_lang', sa.String(8), server_default='TR'))
    op.add_column('deliveries', sa.Column('customer_address', sa.Text(), nullable=True))
    op.add_column('deliveries', sa.Column('customer_country', sa.String(100), nullable=True))
    op.add_column('deliveries', sa.Column('currency', sa.String(3), server_default='USD'))
    op.add_column('deliveries', sa.Column('status', sa.String(20), server_default='completed'))
    op.add_column('deliveries', sa.Column('approved_by', sa.String(80), nullable=True))
    op.add_column('deliveries', sa.Column('approved_at', sa.DateTime(), nullable=True))
    op.add_column('deliveries', sa.Column('reject_reason', sa.Text(), nullable=True))
    op.create_index('ix_deliveries_status', 'deliveries', ['status'])
    op.add_column('delivery_items', sa.Column('unit_price', sa.Float(), nullable=True))
    op.add_column('delivery_items', sa.Column('weight_ml', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('delivery_items', 'weight_ml')
    op.drop_column('delivery_items', 'unit_price')
    op.drop_index('ix_deliveries_status', table_name='deliveries')
    op.drop_column('deliveries', 'reject_reason')
    op.drop_column('deliveries', 'approved_at')
    op.drop_column('deliveries', 'approved_by')
    op.drop_column('deliveries', 'status')
    op.drop_column('deliveries', 'currency')
    op.drop_column('deliveries', 'customer_country')
    op.drop_column('deliveries', 'customer_address')
    op.drop_column('deliveries', 'doc_lang')
    op.drop_column('items', 'name_tr')
