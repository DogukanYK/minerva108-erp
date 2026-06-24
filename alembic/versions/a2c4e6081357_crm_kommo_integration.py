"""CRM — Kommo entegrasyonu (tek yön ayna)

crm_company/crm_contact/crm_deal'e kommo_id (upsert anahtarı) + source kolonları;
crm_integration_state tablosu (delta senkron imleci + son çalıştırma özeti).
init_db() alter_safe + create_all bunları açılışta da uygular; bu migration
alembic geçmişi + temiz kurulum içindir.

Revision ID: a2c4e6081357
Revises: f7b1c3d5e9a2
Create Date: 2026-06-24 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a2c4e6081357'
down_revision: Union[str, Sequence[str], None] = 'f7b1c3d5e9a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('crm_company', sa.Column('source', sa.String(50), nullable=True))
    op.add_column('crm_company', sa.Column('kommo_id', sa.BigInteger(), nullable=True))
    op.add_column('crm_contact', sa.Column('kommo_id', sa.BigInteger(), nullable=True))
    op.add_column('crm_deal',    sa.Column('source', sa.String(50), nullable=True))
    op.add_column('crm_deal',    sa.Column('kommo_id', sa.BigInteger(), nullable=True))
    op.create_index('ix_crm_company_kommo', 'crm_company', ['kommo_id'])
    op.create_index('ix_crm_contact_kommo', 'crm_contact', ['kommo_id'])
    op.create_index('ix_crm_deal_kommo',    'crm_deal',    ['kommo_id'])

    op.create_table(
        'crm_integration_state',
        sa.Column('id',             sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('provider',       sa.String(40), nullable=False),
        sa.Column('last_sync_at',   sa.DateTime(), nullable=True),
        sa.Column('cursor',         sa.BigInteger(), nullable=True),
        sa.Column('last_status',    sa.String(255), nullable=True),
        sa.Column('last_run_at',    sa.DateTime(), nullable=True),
        sa.Column('imported_total', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('updated_at',     sa.DateTime(), nullable=True, server_default=sa.func.now()),
    )
    op.create_index('ix_crm_integration_provider', 'crm_integration_state', ['provider'], unique=True)


def downgrade() -> None:
    op.drop_table('crm_integration_state')
    op.drop_index('ix_crm_deal_kommo', table_name='crm_deal')
    op.drop_index('ix_crm_contact_kommo', table_name='crm_contact')
    op.drop_index('ix_crm_company_kommo', table_name='crm_company')
    op.drop_column('crm_deal', 'kommo_id')
    op.drop_column('crm_deal', 'source')
    op.drop_column('crm_contact', 'kommo_id')
    op.drop_column('crm_company', 'kommo_id')
    op.drop_column('crm_company', 'source')
