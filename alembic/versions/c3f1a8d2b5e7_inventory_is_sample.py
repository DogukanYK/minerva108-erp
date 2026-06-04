"""inventory.is_sample — numune lotu işareti (Faz 2)

Var olan bir hammaddenin alternatif tedarikçiden numune olarak gelen
partileri Inventory.is_sample=True ile işaretlenir.  Stoğa girer, üretimde
kullanılabilir; stok sayfası ayrı rozetle gösterir, üretimde hangi
tedarikçinin/lotun tüketileceği seçilebilir.

init_db() alter_safe bloğu da bu kolonu her açılışta idempotent ekler;
migration alembic geçmişi tutarlılığı + temiz kurulum içindir.

Revision ID: c3f1a8d2b5e7
Revises: b2e1c7f4a9d3
Create Date: 2026-06-04 09:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3f1a8d2b5e7'
down_revision: Union[str, Sequence[str], None] = 'b2e1c7f4a9d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'inventory',
        sa.Column('is_sample', sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column('inventory', 'is_sample')
