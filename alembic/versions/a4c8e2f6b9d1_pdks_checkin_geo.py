"""PDKS — üçlü doğrulama (ofis ağı + konum + canlı QR) için geo kolonları

pdks_events'e geo_lat/geo_lon/geo_accuracy_m eklenir. Self check-in/out
doğrulaması açıkken (AppSetting pdks.checkin.enforce) tarayıcı konumu bu
kolonlara yazılır; manuel olaylarda ve doğrulama kapalıyken NULL kalır.
init_db() alter_safe bloğu bu kolonları idempotent ekler; migration alembic
geçmişi + temiz kurulum içindir.

Revision ID: a4c8e2f6b9d1
Revises: f3a5c7e9b2d4
Create Date: 2026-08-03 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4c8e2f6b9d1'
down_revision: Union[str, Sequence[str], None] = 'f3a5c7e9b2d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('pdks_events', sa.Column('geo_lat', sa.Float(), nullable=True))
    op.add_column('pdks_events', sa.Column('geo_lon', sa.Float(), nullable=True))
    op.add_column('pdks_events', sa.Column('geo_accuracy_m', sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column('pdks_events', 'geo_accuracy_m')
    op.drop_column('pdks_events', 'geo_lon')
    op.drop_column('pdks_events', 'geo_lat')
