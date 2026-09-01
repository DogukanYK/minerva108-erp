"""pdks_employees.end_date — işten ayrılış tarihi

Ayrılan personelin kartı pasifleştirilse bile aylık puantaj ayın kalanını
DEVAMSIZ yazıyordu (compute_month yalnız programa bakıyor, istihdam penceresine
değil).  Ağustos 2026'da iki ayrılış oldu ve son ay bordrosu yanlış çıktı.
Bu kolon istihdam penceresinin üst sınırı; core/pdks.compute_day bu tarihten
sonrasını 'ayrildi' statüsüyle toplam DIŞI bırakır.

Prod'a `init_db()` alter_safe satırıyla ulaşır (deploy.sh alembic çalıştırmaz);
bu dosya alembic geçmişi + temiz kurulum içindir — CLAUDE.md iki-yer kuralı.

Revision ID: a4c6e8b2d1f5
Revises: f2c4e6a8b1d3
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'a4c6e8b2d1f5'
down_revision: Union[str, Sequence[str], None] = 'f2c4e6a8b1d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('pdks_employees', sa.Column('end_date', sa.Date(), nullable=True))


def downgrade() -> None:
    op.drop_column('pdks_employees', 'end_date')
