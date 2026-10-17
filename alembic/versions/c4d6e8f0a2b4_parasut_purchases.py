"""Paraşüt alış faturası fiyat onay kuyruğu (16.10.2026)

`core/parasut_purchases.py`: her gece Paraşüt'ten SALT OKUMA alış faturaları
(`parasut_purchase_bills` / `parasut_purchase_lines`) + onaylanan satırın
ambalaj hafızası (`parasut_product_packs`).  Paraşüt'e hiçbir şey yazılmaz;
onaylanan satır `supplier_prices`'a source='parasut' ile düşer.
KVKK: tedarikçinin yalnız Paraşüt id'si + adı tutulur (e-posta / telefon /
vergi no / adres kolonu YOK).

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — tablolar prod'a `init_db()`
create_all ile gelir.  Bu dosya alembic geçmişi ve temiz kurulum içindir;
init_db'nin zaten kurduğu DB'de `alembic stamp head` kullan.

Revision ID: c4d6e8f0a2b4
Revises: b917b245c7c5
Create Date: 2026-10-16 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4d6e8f0a2b4'
down_revision: Union[str, Sequence[str], None] = 'b917b245c7c5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'parasut_purchase_bills',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('parasut_id', sa.BigInteger(), nullable=False, unique=True),
        sa.Column('item_type', sa.String(30), nullable=True),
        sa.Column('invoice_no', sa.String(60), nullable=True),
        sa.Column('issue_date', sa.Date(), nullable=True),
        sa.Column('due_date', sa.Date(), nullable=True),
        sa.Column('currency', sa.String(8), nullable=True),
        sa.Column('exchange_rate', sa.Float(), nullable=True),
        sa.Column('net_total', sa.Float(), nullable=True),
        sa.Column('gross_total', sa.Float(), nullable=True),
        sa.Column('description', sa.String(250), nullable=True),
        sa.Column('supplier_parasut_id', sa.BigInteger(), nullable=True),
        sa.Column('supplier_name', sa.String(150), nullable=True),
        sa.Column('expense_hint', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('first_seen_at', sa.DateTime(), nullable=True),
        sa.Column('synced_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_parasut_purchase_bills_issue_date', 'parasut_purchase_bills', ['issue_date'])
    op.create_table(
        'parasut_purchase_lines',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('bill_id', sa.Integer(), sa.ForeignKey('parasut_purchase_bills.id', ondelete='CASCADE'),
                  nullable=False),
        sa.Column('parasut_detail_id', sa.BigInteger(), nullable=False, unique=True),
        sa.Column('description', sa.String(250), nullable=True),
        sa.Column('product_parasut_id', sa.BigInteger(), nullable=True),
        sa.Column('product_name', sa.String(150), nullable=True),
        sa.Column('product_unit', sa.String(20), nullable=True),
        sa.Column('quantity', sa.Float(), nullable=True),
        sa.Column('unit_price', sa.Float(), nullable=True),
        sa.Column('discount_rate', sa.Float(), nullable=True),
        sa.Column('vat_rate', sa.Float(), nullable=True),
        sa.Column('net_total', sa.Float(), nullable=True),
        sa.Column('expense_hint', sa.Boolean(), nullable=False, server_default=sa.text('false')),
        sa.Column('status', sa.String(12), nullable=False, server_default='pending'),
        sa.Column('suggested_item_id', sa.Integer(), sa.ForeignKey('items.id', ondelete='SET NULL'), nullable=True),
        sa.Column('suggested_score', sa.Float(), nullable=True),
        sa.Column('chosen_item_id', sa.Integer(), sa.ForeignKey('items.id', ondelete='SET NULL'), nullable=True),
        sa.Column('chosen_supplier_id', sa.Integer(), sa.ForeignKey('suppliers.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('chosen_supplier_name', sa.String(150), nullable=True),
        sa.Column('package_qty', sa.Float(), nullable=True),
        sa.Column('price_unit', sa.String(8), nullable=True),
        sa.Column('decided_by', sa.String(100), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.Column('decision_note', sa.String(255), nullable=True),
        sa.Column('supplier_price_id', sa.Integer(), sa.ForeignKey('supplier_prices.id', ondelete='SET NULL'),
                  nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_parasut_purchase_lines_bill_id', 'parasut_purchase_lines', ['bill_id'])
    op.create_index('ix_parasut_purchase_lines_product_parasut_id', 'parasut_purchase_lines', ['product_parasut_id'])
    op.create_index('ix_parasut_purchase_lines_status', 'parasut_purchase_lines', ['status'])
    op.create_table(
        'parasut_product_packs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('product_parasut_id', sa.BigInteger(), nullable=False, unique=True),
        sa.Column('product_name', sa.String(150), nullable=True),
        sa.Column('package_qty', sa.Float(), nullable=False),
        sa.Column('price_unit', sa.String(8), nullable=False),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id', ondelete='SET NULL'), nullable=True),
        sa.Column('updated_by', sa.String(100), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('parasut_product_packs')
    op.drop_index('ix_parasut_purchase_lines_status', table_name='parasut_purchase_lines')
    op.drop_index('ix_parasut_purchase_lines_product_parasut_id', table_name='parasut_purchase_lines')
    op.drop_index('ix_parasut_purchase_lines_bill_id', table_name='parasut_purchase_lines')
    op.drop_table('parasut_purchase_lines')
    op.drop_index('ix_parasut_purchase_bills_issue_date', table_name='parasut_purchase_bills')
    op.drop_table('parasut_purchase_bills')
