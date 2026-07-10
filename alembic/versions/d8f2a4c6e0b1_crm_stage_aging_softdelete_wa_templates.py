"""CRM yükseltme — aşama yaşı + fırsat soft-delete + WhatsApp şablonları

crm_deal.stage_changed_at: fırsatın mevcut aşamaya giriş anı (Kanban yaş rozeti
"aşamada N gün" bunu okur; backfill = created_at).  crm_deal.is_active: fırsat
silme artık arşivleme (firma/kişi ile tutarlı soft-delete).  crm_wa_template:
tıkla-konuş için paylaşımlı WhatsApp mesaj şablonları ({ad} yer tutuculu).
init_db() create_all/alter_safe kurar; migration alembic geçmişi içindir.

Revision ID: d8f2a4c6e0b1
Revises: c4f6a8b2d1e5
Create Date: 2026-07-10 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd8f2a4c6e0b1'
down_revision: Union[str, Sequence[str], None] = 'c4f6a8b2d1e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('crm_deal', sa.Column('stage_changed_at', sa.DateTime(), nullable=True))
    op.execute("UPDATE crm_deal SET stage_changed_at = created_at WHERE stage_changed_at IS NULL")
    op.add_column('crm_deal', sa.Column('is_active', sa.Boolean(), nullable=False,
                                        server_default=sa.true()))

    op.create_table(
        'crm_wa_template',
        sa.Column('id',         sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name',       sa.String(100), nullable=False),
        sa.Column('body',       sa.String(1000), nullable=False),
        sa.Column('created_by', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('crm_wa_template')
    op.drop_column('crm_deal', 'is_active')
    op.drop_column('crm_deal', 'stage_changed_at')
