"""Distribütör sipariş portalı: distributors + distributor_prices + quotations alanları

Distribütör = role='Distributor' bir User'a bağlı Distributor profili. Her distribütöre
özel fiyat listesi (distributor_prices, aynı zamanda katalog). Distribütör siparişi =
distributor_id + status='PENDING' bir Quotation; onay CONFIRMED, red REJECTED.
init_db() alter_safe da idempotent olarak ekler.

Revision ID: d7f9b1c3e5a2
Revises: b2d4f6a8c1e3
Create Date: 2026-06-29 13:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd7f9b1c3e5a2'
down_revision: Union[str, Sequence[str], None] = 'b2d4f6a8c1e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'distributors',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, unique=True),
        sa.Column('company_name', sa.String(length=150), nullable=False),
        sa.Column('contact_name', sa.String(length=150), nullable=True),
        sa.Column('email', sa.String(length=150), nullable=True),
        sa.Column('phone', sa.String(length=50), nullable=True),
        sa.Column('address', sa.Text(), nullable=True),
        sa.Column('country', sa.String(length=100), nullable=True),
        sa.Column('vat', sa.String(length=50), nullable=True),
        sa.Column('currency', sa.String(length=3), nullable=False, server_default='TRY'),
        sa.Column('is_active', sa.Boolean(), server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_distributors_user_id', 'distributors', ['user_id'])

    op.create_table(
        'distributor_prices',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('distributor_id', sa.Integer(), sa.ForeignKey('distributors.id'), nullable=False),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id'), nullable=False),
        sa.Column('unit_price', sa.Float(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('distributor_id', 'item_id', name='uq_distprice_dist_item'),
    )
    op.create_index('ix_distributor_prices_distributor_id', 'distributor_prices', ['distributor_id'])
    op.create_index('ix_distributor_prices_item_id', 'distributor_prices', ['item_id'])

    op.add_column('quotations', sa.Column('distributor_id', sa.Integer(), sa.ForeignKey('distributors.id'), nullable=True))
    op.add_column('quotations', sa.Column('submitted_at', sa.DateTime(), nullable=True))
    op.add_column('quotations', sa.Column('rejected_at', sa.DateTime(), nullable=True))
    op.add_column('quotations', sa.Column('rejected_by', sa.String(length=50), nullable=True))
    op.add_column('quotations', sa.Column('reject_reason', sa.Text(), nullable=True))
    op.create_index('ix_quotations_distributor', 'quotations', ['distributor_id'])


def downgrade() -> None:
    op.drop_index('ix_quotations_distributor', table_name='quotations')
    op.drop_column('quotations', 'reject_reason')
    op.drop_column('quotations', 'rejected_by')
    op.drop_column('quotations', 'rejected_at')
    op.drop_column('quotations', 'submitted_at')
    op.drop_column('quotations', 'distributor_id')
    op.drop_index('ix_distributor_prices_item_id', table_name='distributor_prices')
    op.drop_index('ix_distributor_prices_distributor_id', table_name='distributor_prices')
    op.drop_table('distributor_prices')
    op.drop_index('ix_distributors_user_id', table_name='distributors')
    op.drop_table('distributors')
