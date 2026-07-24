"""Shopify stok senkron durumu — shopify_sync_state

IMS → Shopify tek yön stok push'unun mağaza (marka) başına durum satırı
(CrmIntegrationState kalıbı). Global buffer/enabled AppSetting'te tutulur.
init_db() create_all bu tabloyu açılışta kurar; migration alembic geçmişi +
temiz kurulum içindir.

Revision ID: a1b2c3d4e5f6
Revises: e9c1a3b5d7f0
Create Date: 2026-07-24 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'e9c1a3b5d7f0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'shopify_sync_state',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('store_key',     sa.String(20), nullable=False),
        sa.Column('brand',         sa.String(40), nullable=True),
        sa.Column('enabled',       sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('last_sync_at',  sa.DateTime(), nullable=True),
        sa.Column('last_run_at',   sa.DateTime(), nullable=True),
        sa.Column('last_status',   sa.String(255), nullable=True),
        sa.Column('matched_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('pushed_count',  sa.Integer(), nullable=False, server_default='0'),
        sa.Column('unmatched',     sa.Text(), nullable=True),
        sa.Column('updated_at',    sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_shopify_sync_state_store_key', 'shopify_sync_state',
                    ['store_key'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_shopify_sync_state_store_key', table_name='shopify_sync_state')
    op.drop_table('shopify_sync_state')
