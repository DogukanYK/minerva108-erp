"""duplicate_item_decisions — kopya hammadde kartı karar kayıtları

24.08.2026 numune olayı taramasında bulunan 21 kopya kümesi için lab kararı
popup'ının veri tablosu.  Prod'a create_all ile gelir (yeni TABLO — CLAUDE.md
iki-yer kuralı gereği migration yalnız alembic geçmişi + temiz kurulum için).

Revision ID: f2c4e6a8b1d3
Revises: e1a3c5b7d9f2
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f2c4e6a8b1d3'
down_revision: Union[str, Sequence[str], None] = 'e1a3c5b7d9f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'duplicate_item_decisions',
        sa.Column('id', sa.Integer(), primary_key=True, index=True),
        sa.Column('cluster_key', sa.String(80), nullable=False),
        sa.Column('title', sa.String(150), nullable=False),
        sa.Column('item_ids', sa.Text(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False, server_default='pending'),
        sa.Column('target_item_id', sa.Integer(), sa.ForeignKey('items.id'), nullable=True),
        sa.Column('decided_by', sa.String(80), nullable=True),
        sa.Column('decided_at', sa.DateTime(), nullable=True),
        sa.Column('result_note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_duplicate_item_decisions_cluster_key',
                    'duplicate_item_decisions', ['cluster_key'], unique=True)
    op.create_index('ix_duplicate_item_decisions_status',
                    'duplicate_item_decisions', ['status'])


def downgrade() -> None:
    op.drop_table('duplicate_item_decisions')
