"""Teslimat: çok-bacaklı kargo takibi (deliveries.tracking_legs JSON)

Yurtdışı gönderiler birden çok kargo bacağından geçer (yerel TR → global → varış yereli).
tracking_legs = JSON liste [{"label","carrier","tracking_no"}]. init_db alter_safe de ekler.

Revision ID: b2d4f6a8c1e3
Revises: a1c3e5b7d9f2
Create Date: 2026-06-29 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2d4f6a8c1e3'
down_revision: Union[str, Sequence[str], None] = 'a1c3e5b7d9f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('deliveries', sa.Column('tracking_legs', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('deliveries', 'tracking_legs')
