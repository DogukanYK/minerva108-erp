"""PDKS — imzanın hangi yolla doğrulandığını kaydet (verify_method)

pdks_events.verify_method: 'qr' (kiosk QR) | 'code' (kiosk sayısal kod) |
'static' (basılı afiş) | 'static_fallback' (rotating moddayken afiş kabul
edildi) | 'off' (doğrulama kapalıydı).

11.08.2026'da kimsenin imza atamadığı kesinti, hangi yolun kullanıldığı
kayıtlı olmadığı için adli inceleme gerektirdi; bu kolon bir dahakine tek
bakışta cevap verir.

DİKKAT: deploy.sh alembic ÇALIŞTIRMAZ — bu kolon prod'a database.py
init_db() içindeki alter_safe satırıyla ulaşır.  Bu migration alembic
geçmişi + temiz kurulum içindir.

Revision ID: c7e9b1d3f5a2
Revises: b8d2f4a6c9e1
Create Date: 2026-08-12 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7e9b1d3f5a2'
down_revision: Union[str, Sequence[str], None] = 'b8d2f4a6c9e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('pdks_events', sa.Column('verify_method', sa.String(16), nullable=True))


def downgrade() -> None:
    op.drop_column('pdks_events', 'verify_method')
