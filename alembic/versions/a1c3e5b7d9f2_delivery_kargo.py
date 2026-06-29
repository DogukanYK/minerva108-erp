"""Teslimat: Kargo / Sevkiyat alanları (ertelenmiş stok — takip no ile)

deliveries: tracking_no, carrier, shipped_at, shipped_by.
Kargo modu mevcut 'status' kolonunu yeniden kullanır ('preparing'/'shipped'/'canceled');
yeni kolon gerekmez. init_db() alter_safe bunları açılışta da ekler.

Revision ID: a1c3e5b7d9f2
Revises: f2d4b6e8a1c7
Create Date: 2026-06-27 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a1c3e5b7d9f2'
down_revision: Union[str, Sequence[str], None] = 'f2d4b6e8a1c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('deliveries', sa.Column('tracking_no', sa.String(100), nullable=True))
    op.add_column('deliveries', sa.Column('carrier', sa.String(80), nullable=True))
    op.add_column('deliveries', sa.Column('shipped_at', sa.DateTime(), nullable=True))
    op.add_column('deliveries', sa.Column('shipped_by', sa.String(80), nullable=True))


def downgrade() -> None:
    op.drop_column('deliveries', 'shipped_by')
    op.drop_column('deliveries', 'shipped_at')
    op.drop_column('deliveries', 'carrier')
    op.drop_column('deliveries', 'tracking_no')
