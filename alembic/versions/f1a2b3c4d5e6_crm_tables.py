"""CRM — müşteri ilişkileri tabloları (cross-cutting, domain'siz)

crm_company, crm_contact, crm_stage, crm_deal, crm_activity, crm_task.
init_db() create_all bu tabloları açılışta kurar + varsayılan pipeline
aşamalarını seed'ler; bu migration alembic geçmişi + temiz kurulum içindir.
CRM tek birleşik platform — Kozmetik/Supplement domain ayrımına tabi DEĞİL.

Revision ID: f1a2b3c4d5e6
Revises: e5b3c9a17d42
Create Date: 2026-06-22 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, Sequence[str], None] = 'e5b3c9a17d42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'crm_company',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name',          sa.String(200), nullable=False),
        sa.Column('sector',        sa.String(100), nullable=True),
        sa.Column('website',       sa.String(200), nullable=True),
        sa.Column('phone',         sa.String(50),  nullable=True),
        sa.Column('email',         sa.String(150), nullable=True),
        sa.Column('address',       sa.Text(),      nullable=True),
        sa.Column('city',          sa.String(100), nullable=True),
        sa.Column('country',       sa.String(100), nullable=True),
        sa.Column('tax_office',    sa.String(120), nullable=True),
        sa.Column('tax_no',        sa.String(50),  nullable=True),
        sa.Column('notes',         sa.Text(),      nullable=True),
        sa.Column('owner_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('owner_name',    sa.String(100), nullable=True),
        sa.Column('is_active',     sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at',    sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column('created_by',    sa.String(100), nullable=True),
    )
    op.create_index('ix_crm_company_name',  'crm_company', ['name'])
    op.create_index('ix_crm_company_owner', 'crm_company', ['owner_user_id'])

    op.create_table(
        'crm_contact',
        sa.Column('id',              sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('company_id',      sa.Integer(), sa.ForeignKey('crm_company.id', ondelete='SET NULL'), nullable=True),
        sa.Column('full_name',       sa.String(150), nullable=False),
        sa.Column('title',           sa.String(100), nullable=True),
        sa.Column('phone',           sa.String(50),  nullable=True),
        sa.Column('mobile',          sa.String(50),  nullable=True),
        sa.Column('email',           sa.String(150), nullable=True),
        sa.Column('whatsapp_number', sa.String(50),  nullable=True),
        sa.Column('source',          sa.String(50),  nullable=True),
        sa.Column('notes',           sa.Text(),      nullable=True),
        sa.Column('owner_user_id',   sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('owner_name',      sa.String(100), nullable=True),
        sa.Column('is_active',       sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at',      sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column('created_by',      sa.String(100), nullable=True),
    )
    op.create_index('ix_crm_contact_company', 'crm_contact', ['company_id'])
    op.create_index('ix_crm_contact_name',    'crm_contact', ['full_name'])

    op.create_table(
        'crm_stage',
        sa.Column('id',         sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name',       sa.String(80), nullable=False),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_won',     sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('is_lost',    sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('is_active',  sa.Boolean(), nullable=False, server_default=sa.true()),
    )

    op.create_table(
        'crm_deal',
        sa.Column('id',                sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('title',             sa.String(200), nullable=False),
        sa.Column('company_id',        sa.Integer(), sa.ForeignKey('crm_company.id', ondelete='SET NULL'), nullable=True),
        sa.Column('contact_id',        sa.Integer(), sa.ForeignKey('crm_contact.id', ondelete='SET NULL'), nullable=True),
        sa.Column('stage_id',          sa.Integer(), sa.ForeignKey('crm_stage.id',   ondelete='SET NULL'), nullable=True),
        sa.Column('value',             sa.Float(), server_default='0'),
        sa.Column('currency',          sa.String(3), nullable=False, server_default='TRY'),
        sa.Column('probability',       sa.Integer(), server_default='0'),
        sa.Column('expected_close_at', sa.DateTime(), nullable=True),
        sa.Column('status',            sa.String(20), nullable=False, server_default='open'),
        sa.Column('lost_reason',       sa.Text(), nullable=True),
        sa.Column('owner_user_id',     sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('owner_name',        sa.String(100), nullable=True),
        sa.Column('quotation_id',      sa.Integer(), sa.ForeignKey('quotations.id', ondelete='SET NULL'), nullable=True),
        sa.Column('sort_order',        sa.Integer(), server_default='0'),
        sa.Column('created_at',        sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column('created_by',        sa.String(100), nullable=True),
        sa.Column('won_at',            sa.DateTime(), nullable=True),
        sa.Column('closed_at',         sa.DateTime(), nullable=True),
    )
    op.create_index('ix_crm_deal_stage',   'crm_deal', ['stage_id'])
    op.create_index('ix_crm_deal_status',  'crm_deal', ['status'])
    op.create_index('ix_crm_deal_company', 'crm_deal', ['company_id'])

    op.create_table(
        'crm_activity',
        sa.Column('id',             sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('company_id',     sa.Integer(), sa.ForeignKey('crm_company.id', ondelete='CASCADE'), nullable=True),
        sa.Column('contact_id',     sa.Integer(), sa.ForeignKey('crm_contact.id', ondelete='CASCADE'), nullable=True),
        sa.Column('deal_id',        sa.Integer(), sa.ForeignKey('crm_deal.id',    ondelete='CASCADE'), nullable=True),
        sa.Column('type',           sa.String(20), nullable=False, server_default='note'),
        sa.Column('subject',        sa.String(200), nullable=True),
        sa.Column('body',           sa.Text(), nullable=True),
        sa.Column('author_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('author_name',    sa.String(100), nullable=True),
        sa.Column('is_pinned',      sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at',     sa.DateTime(), nullable=True, server_default=sa.func.now()),
    )
    op.create_index('ix_crm_activity_company', 'crm_activity', ['company_id'])
    op.create_index('ix_crm_activity_contact', 'crm_activity', ['contact_id'])
    op.create_index('ix_crm_activity_deal',    'crm_activity', ['deal_id'])
    op.create_index('ix_crm_activity_created',  'crm_activity', ['created_at'])

    op.create_table(
        'crm_task',
        sa.Column('id',                  sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('title',               sa.String(200), nullable=False),
        sa.Column('notes',               sa.Text(), nullable=True),
        sa.Column('due_at',              sa.DateTime(), nullable=True),
        sa.Column('status',              sa.String(20), nullable=False, server_default='open'),
        sa.Column('company_id',          sa.Integer(), sa.ForeignKey('crm_company.id', ondelete='SET NULL'), nullable=True),
        sa.Column('contact_id',          sa.Integer(), sa.ForeignKey('crm_contact.id', ondelete='SET NULL'), nullable=True),
        sa.Column('deal_id',             sa.Integer(), sa.ForeignKey('crm_deal.id',    ondelete='SET NULL'), nullable=True),
        sa.Column('assigned_to_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('assigned_to_name',    sa.String(100), nullable=True),
        sa.Column('created_at',          sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column('created_by',          sa.String(100), nullable=True),
        sa.Column('completed_at',        sa.DateTime(), nullable=True),
        sa.Column('reminder_sent',       sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index('ix_crm_task_due',      'crm_task', ['due_at'])
    op.create_index('ix_crm_task_status',   'crm_task', ['status'])
    op.create_index('ix_crm_task_assignee', 'crm_task', ['assigned_to_user_id'])


def downgrade() -> None:
    op.drop_table('crm_task')
    op.drop_table('crm_activity')
    op.drop_table('crm_deal')
    op.drop_table('crm_stage')
    op.drop_table('crm_contact')
    op.drop_table('crm_company')
