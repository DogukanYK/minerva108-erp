"""undo_log: per-user 50-deep undoable action ring buffer

Lab kullanıcısı yanlışlıkla stoğu düzeltir / ürün düzenler — Ctrl+Z (veya
toolbar'daki ↶) ile son işlemi geri alabilsin diye gerekli persistent log.

Kapsam (V1):
  * stock_adjust       — Item.current_stock + ilgili Transaction
  * item_edit          — Item row'unun tüm kolonları
  * inventory_receive  — Inventory lot oluşturma + bağlı Transaction

Şema notları:
  * payload JSONB: action tipine göre değişir; uygulayıcı (core/undo.py)
    "switch on action_type" mantığıyla parse eder.  Şema değişikliği gerekmez.
  * undone_at NULL → henüz geri alınmamış (undoable).
  * Trim: per-user 50 en yeni entry tutulur (eskileri DELETE — core/undo.py).

Revision ID: 8f7d494b619c
Revises: a7af35288275
Create Date: 2026-05-13 11:45:41.909209
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision: str = '8f7d494b619c'
down_revision: Union[str, Sequence[str], None] = 'a7af35288275'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'undo_log',
        sa.Column('id',           sa.Integer(),   primary_key=True, autoincrement=True),
        sa.Column('user_id',      sa.Integer(),   sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('action_type',  sa.String(40),  nullable=False),
        sa.Column('target_table', sa.String(40),  nullable=False),
        sa.Column('target_id',    sa.Integer(),   nullable=True),
        sa.Column('payload',      JSONB(),        nullable=False),
        sa.Column('description',  sa.String(255), nullable=False),
        sa.Column('created_at',   sa.DateTime(),  nullable=False, server_default=sa.func.now()),
        sa.Column('undone_at',    sa.DateTime(),  nullable=True),
    )
    op.create_index('ix_undo_log_user_undone',  'undo_log', ['user_id', 'undone_at'])
    op.create_index('ix_undo_log_user_created', 'undo_log', ['user_id', sa.text('created_at DESC')])
    op.create_index('ix_undo_log_action_type',  'undo_log', ['action_type'])


def downgrade() -> None:
    op.drop_index('ix_undo_log_action_type',  table_name='undo_log')
    op.drop_index('ix_undo_log_user_created', table_name='undo_log')
    op.drop_index('ix_undo_log_user_undone',  table_name='undo_log')
    op.drop_table('undo_log')
