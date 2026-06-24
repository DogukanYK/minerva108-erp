"""CRM — crm_activity.external_id (Kommo mesaj olayları dedup)

WhatsApp/sohbet mesaj aktivitelerini Kommo olaylarından çekerken tekrar senkronda
çift kayıt olmaması için harici kaynak referansı (örn. 'kommo_evt_<id>').

Revision ID: b3d5f7092468
Revises: a2c4e6081357, b8e2d4f6a1c3
Create Date: 2026-06-24 13:00:00.000000

NOT: Bu bir MERGE migration'ı — iki paralel dalı birleştirir:
  • a2c4e6081357 (CRM Kommo entegrasyonu)
  • b8e2d4f6a1c3 (Teslimat özelliği)
İkisi de f7b1c3d5e9a2'den dallandığı için iki head oluşmuştu; bu revizyon tek
head'e indirir.  upgrade() yalnızca crm_activity.external_id ekler (bağımsız).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b3d5f7092468'
down_revision: Union[str, Sequence[str], None] = ('a2c4e6081357', 'b8e2d4f6a1c3')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('crm_activity', sa.Column('external_id', sa.String(80), nullable=True))
    op.create_index('ix_crm_activity_extid', 'crm_activity', ['external_id'])


def downgrade() -> None:
    op.drop_index('ix_crm_activity_extid', table_name='crm_activity')
    op.drop_column('crm_activity', 'external_id')
