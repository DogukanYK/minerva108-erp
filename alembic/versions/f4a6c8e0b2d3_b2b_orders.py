"""B2B sipariş akışı (banka profilleri/kuralları + b2b_order* tabloları)

Teklif → sipariş (Quotation korunur, status='ORDER'), değişmez ticari/teknik
sürümler, yönetim imzası (şifre doğrulaması + çizilen imza; şifre saklanmaz),
ödeme kayıtları, siparişe bağlı alım satırları, iki aşamalı parti üretimi
(başlat = tüketim, tamamla = bitmiş ürün) ve partiden gerekçeli fiziksel iade.
Banka hesapları artık şablonda sabit değil, `bank_profiles` tablosunda;
ülke + para birimi kuralı (`bank_rules`) öneri üretir.

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — tablolar prod'a `init_db()`
create_all ile gelir; 3 banka profili `_seed_bank_profiles()` (sentinel
`seed.bank_profiles.v1`) ile bir kez eklenir.  Bu dosya alembic geçmişi ve
temiz kurulum içindir; init_db'nin zaten kurduğu DB'de `alembic stamp head`.

Revision ID: f4a6c8e0b2d3
Revises: e3f5a7c9b1d2
Create Date: 2026-10-09 22:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f4a6c8e0b2d3'
down_revision: Union[str, Sequence[str], None] = 'e3f5a7c9b1d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id():
    return sa.Column('id', sa.Integer(), primary_key=True)


def _order():
    return sa.Column('order_id', sa.Integer(), sa.ForeignKey('b2b_orders.id'), nullable=False)


def upgrade() -> None:
    op.create_table(
        'bank_profiles', _id(),
        sa.Column('label', sa.String(80), nullable=False),
        sa.Column('bank_name', sa.String(150), nullable=False),
        sa.Column('branch', sa.String(150), nullable=True),
        sa.Column('swift', sa.String(20), nullable=True),
        sa.Column('account_holder', sa.String(200), nullable=False),
        sa.Column('iban_usd', sa.String(40), nullable=True),
        sa.Column('iban_eur', sa.String(40), nullable=True),
        sa.Column('iban_try', sa.String(40), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False))

    op.create_table(
        'bank_rules', _id(),
        sa.Column('country', sa.String(100), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('bank_profile_id', sa.Integer(), sa.ForeignKey('bank_profiles.id'), nullable=False),
        sa.Column('created_by', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('country', 'currency', name='uq_bank_rule_country_currency'))

    op.create_table(
        'b2b_orders', _id(),
        sa.Column('quotation_id', sa.Integer(), sa.ForeignKey('quotations.id'), nullable=False, unique=True),
        sa.Column('domain', sa.String(20), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('label_language', sa.String(8), nullable=False),
        sa.Column('target_date', sa.Date(), nullable=True),
        sa.Column('payment_terms', sa.String(12), nullable=False),
        sa.Column('advance_percent', sa.Float(), nullable=True),
        sa.Column('bank_profile_id', sa.Integer(), sa.ForeignKey('bank_profiles.id'), nullable=True),
        sa.Column('owner_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('technical_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('signer_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('commercial_revision', sa.Integer(), nullable=False),
        sa.Column('technical_revision', sa.Integer(), nullable=False),
        sa.Column('prep_confirmed_at', sa.DateTime(), nullable=True),
        sa.Column('prep_confirmed_by', sa.String(100), nullable=True),
        sa.Column('delivery_id', sa.Integer(), sa.ForeignKey('deliveries.id'), nullable=True),
        sa.Column('shipped_at', sa.DateTime(), nullable=True),
        sa.Column('shipped_by', sa.String(100), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.Column('cancelled_by', sa.String(100), nullable=True),
        sa.Column('cancel_reason', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False))
    op.create_index('ix_b2b_orders_domain', 'b2b_orders', ['domain'])
    op.create_index('ix_b2b_orders_status', 'b2b_orders', ['status'])

    op.create_table(
        'b2b_order_revisions', _id(), _order(),
        sa.Column('kind', sa.String(12), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('snapshot', sa.Text(), nullable=False),
        sa.Column('snapshot_hash', sa.String(64), nullable=False),
        sa.Column('created_by', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('order_id', 'kind', 'revision', name='uq_b2b_order_revision'))
    op.create_index('ix_b2b_order_revisions_order_id', 'b2b_order_revisions', ['order_id'])

    op.create_table(
        'b2b_order_signatures', _id(), _order(),
        sa.Column('role', sa.String(16), nullable=False),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('signer_name', sa.String(150), nullable=True),
        sa.Column('commercial_revision', sa.Integer(), nullable=False),
        sa.Column('technical_revision', sa.Integer(), nullable=False),
        sa.Column('document_hash', sa.String(64), nullable=False),
        sa.Column('signature_png', sa.Text(), nullable=False),
        sa.Column('signature_sha256', sa.String(64), nullable=False),
        sa.Column('ip_address', sa.String(64), nullable=True),
        sa.Column('user_agent', sa.String(300), nullable=True),
        sa.Column('signed_at', sa.DateTime(), nullable=False))
    op.create_index('ix_b2b_order_signatures_order_id', 'b2b_order_signatures', ['order_id'])

    op.create_table(
        'b2b_order_payments', _id(), _order(),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('currency', sa.String(3), nullable=False),
        sa.Column('received_on', sa.Date(), nullable=True),
        sa.Column('reference', sa.String(150), nullable=True),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('verified_by_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('verified_by', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('amount > 0', name='ck_b2b_payment_amount'))
    op.create_index('ix_b2b_order_payments_order_id', 'b2b_order_payments', ['order_id'])

    op.create_table(
        'b2b_order_purchase_lines', _id(), _order(),
        sa.Column('technical_revision', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id'), nullable=False),
        sa.Column('item_name', sa.String(200), nullable=True),
        sa.Column('unit', sa.String(20), nullable=True),
        sa.Column('need_quantity', sa.Float(), nullable=False),
        sa.Column('supplier_name', sa.String(150), nullable=True),
        sa.Column('status', sa.String(12), nullable=False),
        sa.Column('ordered_quantity', sa.Float(), nullable=False),
        sa.Column('received_quantity', sa.Float(), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('updated_by', sa.String(100), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False))
    op.create_index('ix_b2b_order_purchase_lines_order_id', 'b2b_order_purchase_lines', ['order_id'])

    op.create_table(
        'b2b_order_batches', _id(), _order(),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id'), nullable=False),
        sa.Column('recipe_id', sa.Integer(), sa.ForeignKey('recipes.id'), nullable=False),
        sa.Column('planned_quantity', sa.Float(), nullable=False),
        sa.Column('lot_number', sa.String(100), nullable=False),
        sa.Column('status', sa.String(12), nullable=False),
        sa.Column('label_language', sa.String(8), nullable=False),
        sa.Column('commercial_revision', sa.Integer(), nullable=False),
        sa.Column('technical_revision', sa.Integer(), nullable=False),
        sa.Column('plan_snapshot', sa.Text(), nullable=False),
        sa.Column('started_by', sa.String(100), nullable=True),
        sa.Column('started_at', sa.DateTime(), nullable=False),
        sa.Column('completed_by', sa.String(100), nullable=True),
        sa.Column('completed_at', sa.DateTime(), nullable=True),
        sa.Column('produced_quantity', sa.Float(), nullable=True),
        sa.Column('witness_quantity', sa.Float(), nullable=True),
        sa.Column('production_history_id', sa.Integer(), sa.ForeignKey('production_history.id'), nullable=True),
        sa.Column('cancelled_by', sa.String(100), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.Column('cancel_reason', sa.Text(), nullable=True),
        sa.CheckConstraint('planned_quantity > 0', name='ck_b2b_batch_qty'))
    op.create_index('ix_b2b_order_batches_order_id', 'b2b_order_batches', ['order_id'])

    op.create_table(
        'b2b_order_batch_returns', _id(),
        sa.Column('batch_id', sa.Integer(), sa.ForeignKey('b2b_order_batches.id'), nullable=False),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id'), nullable=False),
        sa.Column('quantity', sa.Float(), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=False),
        sa.Column('created_by', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.CheckConstraint('quantity > 0', name='ck_b2b_batch_return_qty'))
    op.create_index('ix_b2b_order_batch_returns_batch_id', 'b2b_order_batch_returns', ['batch_id'])


def downgrade() -> None:
    for table in ('b2b_order_batch_returns', 'b2b_order_batches', 'b2b_order_purchase_lines',
                  'b2b_order_payments', 'b2b_order_signatures', 'b2b_order_revisions',
                  'b2b_orders', 'bank_rules', 'bank_profiles'):
        op.drop_table(table)
