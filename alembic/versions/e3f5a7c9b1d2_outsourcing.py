"""Kodlu fason üretim (outsourcing_* tabloları + inventory.outsourcing_receipt_id)

Fason üretici, üreticiye özel kalıcı malzeme kodu, iş + dondurulmuş paket
sürümleri, üç hesap onayı, tek-lot kaplar, idempotent operasyonlar, sevk +
satırları (yerel Output'a bağlı), dış tüketim/fire/iade hareketleri ve
karantina kabulleri.  `inventory.outsourcing_receipt_id` fason karantina lotunu
kendi kabul kaydına bağlar: normal mal kabul upsert'i / find_twin bu lotlarla
birleşmez, stok yalnız QC onayında tek `Input` ile post edilir.

FK döngüsü inventory → outsourcing_receipts → outsourcing_containers →
inventory; burada sıra açık olduğundan kap→lot FK'sı tablolar kurulduktan
sonra ayrı eklenir (modelde aynı kenar use_alter).

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — tablolar prod'a `init_db()`
create_all ile, inventory kolonu alter_safe satırıyla gelir.  Bu dosya alembic
geçmişi ve temiz kurulum içindir; init_db'nin zaten kurduğu DB'de
`alembic stamp head` kullan.

Revision ID: e3f5a7c9b1d2
Revises: d8f0b2c4e6a9
Create Date: 2026-10-09 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e3f5a7c9b1d2'
down_revision: Union[str, Sequence[str], None] = 'd8f0b2c4e6a9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _id():
    return sa.Column('id', sa.Integer(), primary_key=True)


def _user(name, nullable=False):
    return sa.Column(name, sa.Integer(), sa.ForeignKey('users.id'), nullable=nullable)


def _now(name='created_at'):
    return sa.Column(name, sa.DateTime(), nullable=False)


def upgrade() -> None:
    op.create_table(
        'outsourcing_partners', _id(),
        sa.Column('domain', sa.String(20), nullable=False),
        sa.Column('name', sa.String(150), nullable=False),
        sa.Column('contact', sa.Text(), nullable=True),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        _user('created_by'), _now(),
        sa.UniqueConstraint('domain', 'name', name='uq_outsourcing_partner_name'))
    op.create_index('ix_outsourcing_partners_domain', 'outsourcing_partners', ['domain'])

    op.create_table(
        'outsourcing_material_codes', _id(),
        sa.Column('domain', sa.String(20), nullable=False),
        sa.Column('partner_id', sa.Integer(), sa.ForeignKey('outsourcing_partners.id'), nullable=False),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id'), nullable=False),
        sa.Column('code', sa.String(40), nullable=False),
        sa.Column('specification', sa.Text(), nullable=False),
        sa.Column('spec_hash', sa.String(64), nullable=False),
        sa.Column('safety_instructions', sa.Text(), nullable=False),
        sa.Column('unit', sa.String(20), nullable=False),
        sa.Column('kind', sa.String(12), nullable=False),
        _user('verified_by', nullable=True),
        sa.Column('verified_at', sa.DateTime(), nullable=True),
        _user('created_by'), _now(),
        sa.UniqueConstraint('partner_id', 'code', name='uq_outsourcing_partner_code'),
        sa.UniqueConstraint('partner_id', 'item_id', 'spec_hash', name='uq_outsourcing_material_identity'))
    op.create_index('ix_outsourcing_material_codes_domain', 'outsourcing_material_codes', ['domain'])

    op.create_table(
        'outsourcing_jobs', _id(),
        sa.Column('domain', sa.String(20), nullable=False),
        sa.Column('partner_id', sa.Integer(), sa.ForeignKey('outsourcing_partners.id'), nullable=False),
        sa.Column('recipe_id', sa.Integer(), sa.ForeignKey('recipes.id'), nullable=True),
        sa.Column('target_item_id', sa.Integer(), sa.ForeignKey('items.id'), nullable=False),
        sa.Column('quantity', sa.Float(), nullable=False),
        sa.Column('unit', sa.String(20), nullable=False),
        sa.Column('label_language', sa.String(8), nullable=False),
        sa.Column('external_product_name', sa.String(150), nullable=False),
        sa.Column('external_notes', sa.Text(), nullable=False),
        _user('technical_user_id'), _user('owner_user_id'), _user('manager_user_id'),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('frozen_at', sa.DateTime(), nullable=True),
        sa.Column('closed_at', sa.DateTime(), nullable=True),
        sa.Column('cancel_reason', sa.Text(), nullable=True),
        _user('created_by'), _now(),
        sa.CheckConstraint('quantity > 0', name='ck_outsourcing_job_qty'),
        sa.CheckConstraint('technical_user_id <> owner_user_id AND technical_user_id <> manager_user_id '
                           'AND owner_user_id <> manager_user_id', name='ck_outsourcing_distinct_approvers'))
    op.create_index('ix_outsourcing_jobs_domain', 'outsourcing_jobs', ['domain'])
    op.create_index('ix_outsourcing_jobs_status', 'outsourcing_jobs', ['status'])

    op.create_table(
        'outsourcing_packet_revisions', _id(),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('outsourcing_jobs.id'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('packet_hash', sa.String(64), nullable=False),
        sa.Column('snapshot', sa.Text(), nullable=False),
        _user('created_by'), _now(),
        sa.UniqueConstraint('job_id', 'revision', name='uq_outsourcing_packet_revision'))
    op.create_index('ix_outsourcing_packet_revisions_job_id', 'outsourcing_packet_revisions', ['job_id'])

    op.create_table(
        'outsourcing_approvals', _id(),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('outsourcing_jobs.id'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('packet_hash', sa.String(64), nullable=False),
        sa.Column('role', sa.String(16), nullable=False),
        _user('user_id'), _now('approved_at'),
        sa.UniqueConstraint('job_id', 'revision', 'role', name='uq_outsourcing_approval_role'),
        sa.UniqueConstraint('job_id', 'revision', 'user_id', name='uq_outsourcing_approval_user'))

    op.create_table(
        'outsourcing_containers', _id(),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('outsourcing_jobs.id'), nullable=False),
        sa.Column('material_code_id', sa.Integer(), sa.ForeignKey('outsourcing_material_codes.id'),
                  nullable=False),
        sa.Column('inventory_id', sa.Integer(), nullable=True),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id'), nullable=False),
        sa.Column('container_uid', sa.String(48), nullable=False, unique=True),
        sa.Column('external_lot', sa.String(48), nullable=False, unique=True),
        sa.Column('source_lot_number', sa.String(100), nullable=True),
        sa.Column('source_snapshot', sa.Text(), nullable=False),
        sa.Column('expiry_date', sa.String(20), nullable=True),
        sa.Column('quantity', sa.Float(), nullable=False),
        sa.Column('unit', sa.String(20), nullable=False),
        sa.Column('dispatched_quantity', sa.Float(), nullable=False),
        sa.Column('consumed_quantity', sa.Float(), nullable=False),
        sa.Column('waste_quantity', sa.Float(), nullable=False),
        sa.Column('returned_quantity', sa.Float(), nullable=False),
        sa.Column('is_active', sa.Boolean(), nullable=False),
        _user('created_by'), _now(),
        sa.CheckConstraint('quantity > 0', name='ck_outsourcing_container_qty'))
    op.create_index('ix_outsourcing_containers_job_id', 'outsourcing_containers', ['job_id'])
    op.create_foreign_key('fk_outsourcing_container_inventory', 'outsourcing_containers', 'inventory',
                          ['inventory_id'], ['id'])

    op.create_table(
        'outsourcing_operations', _id(),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('outsourcing_jobs.id'), nullable=False),
        sa.Column('idempotency_key', sa.String(100), nullable=False),
        sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('request_hash', sa.String(64), nullable=False),
        _user('actor_id'), _now(),
        sa.UniqueConstraint('job_id', 'idempotency_key', name='uq_outsourcing_operation_key'))

    op.create_table(
        'outsourcing_shipments', _id(),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('outsourcing_jobs.id'), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('packet_hash', sa.String(64), nullable=False),
        sa.Column('operation_id', sa.Integer(), sa.ForeignKey('outsourcing_operations.id'),
                  nullable=False, unique=True),
        _user('dispatched_by'), _now('dispatched_at'))
    op.create_index('ix_outsourcing_shipments_job_id', 'outsourcing_shipments', ['job_id'])

    op.create_table(
        'outsourcing_shipment_lines', _id(),
        sa.Column('shipment_id', sa.Integer(), sa.ForeignKey('outsourcing_shipments.id'), nullable=False),
        sa.Column('container_id', sa.Integer(), sa.ForeignKey('outsourcing_containers.id'),
                  nullable=False, unique=True),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=False),
        sa.Column('quantity', sa.Float(), nullable=False))
    op.create_index('ix_outsourcing_shipment_lines_shipment_id', 'outsourcing_shipment_lines', ['shipment_id'])

    op.create_table(
        'outsourcing_movements', _id(),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('outsourcing_jobs.id'), nullable=False),
        sa.Column('operation_id', sa.Integer(), sa.ForeignKey('outsourcing_operations.id'), nullable=False),
        sa.Column('container_id', sa.Integer(), sa.ForeignKey('outsourcing_containers.id'), nullable=False),
        sa.Column('kind', sa.String(20), nullable=False),
        sa.Column('quantity', sa.Float(), nullable=False),
        sa.Column('unit', sa.String(20), nullable=False),
        sa.Column('reason', sa.Text(), nullable=True),
        _now(),
        sa.CheckConstraint('quantity > 0', name='ck_outsourcing_movement_qty'))
    op.create_index('ix_outsourcing_movements_job_id', 'outsourcing_movements', ['job_id'])

    op.create_table(
        'outsourcing_receipts', _id(),
        sa.Column('job_id', sa.Integer(), sa.ForeignKey('outsourcing_jobs.id'), nullable=False),
        sa.Column('operation_id', sa.Integer(), sa.ForeignKey('outsourcing_operations.id'), nullable=False),
        sa.Column('container_id', sa.Integer(), sa.ForeignKey('outsourcing_containers.id'), nullable=True),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id'), nullable=False),
        sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('quantity', sa.Float(), nullable=False),
        sa.Column('unit', sa.String(20), nullable=False),
        sa.Column('external_lot', sa.String(100), nullable=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('input_transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'),
                  nullable=True, unique=True),
        sa.Column('qc_by', sa.String(100), nullable=True),
        sa.Column('qc_at', sa.DateTime(), nullable=True),
        _now(),
        sa.CheckConstraint('quantity > 0', name='ck_outsourcing_receipt_qty'))
    op.create_index('ix_outsourcing_receipts_job_id', 'outsourcing_receipts', ['job_id'])

    op.add_column('inventory', sa.Column('outsourcing_receipt_id', sa.Integer(), nullable=True, unique=True))
    op.create_foreign_key('fk_inventory_outsourcing_receipt', 'inventory', 'outsourcing_receipts',
                          ['outsourcing_receipt_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_inventory_outsourcing_receipt', 'inventory', type_='foreignkey')
    op.drop_column('inventory', 'outsourcing_receipt_id')
    op.drop_constraint('fk_outsourcing_container_inventory', 'outsourcing_containers', type_='foreignkey')
    for table in ('outsourcing_receipts', 'outsourcing_movements', 'outsourcing_shipment_lines',
                  'outsourcing_shipments', 'outsourcing_operations', 'outsourcing_containers',
                  'outsourcing_approvals', 'outsourcing_packet_revisions', 'outsourcing_jobs',
                  'outsourcing_material_codes', 'outsourcing_partners'):
        op.drop_table(table)
