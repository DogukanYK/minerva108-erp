"""app_setting — genel anahtar-değer ayarlar (özelleştirilebilir rol etiketleri)

SuperAdmin'in rollerin görünen adlarını (Süper Yönetici, Yönetici…) değiştirebilmesi
için. init_db() create_all bunu açılışta kurar; migration alembic geçmişi + temiz
kurulum içindir.

Revision ID: e1f3a7c9d2b5
Revises: d7c3f1a9e8b4
Create Date: 2026-06-26 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e1f3a7c9d2b5'
down_revision: Union[str, Sequence[str], None] = 'd7c3f1a9e8b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'app_setting',
        sa.Column('key',        sa.String(80), primary_key=True),
        sa.Column('value',      sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    op.drop_table('app_setting')
