"""recipe phase + production_notes — üretim föyü alanları

Lab üretim föyü (Excel) FAZ sütunu + YAPILIŞI metni içeriyor.  Sistemin bu
föyü üretebilmesi için reçetede iki yeni alan:

  • recipe_ingredients.phase  — malzemenin fazı (A / B / D / E ...).  Kısa
    string; boş olabilir.
  • recipes.production_notes  — üretimin hazırlanış / yapılış metni.  Reçete
    başına tek, üretimlerde değişmez.

İkisi de nullable — mevcut reçeteler bozulmaz, lab zamanla doldurur.

Revision ID: a78f6d0c7e6c
Revises: d579c1629598
Create Date: 2026-05-15 14:03:34.554603
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a78f6d0c7e6c'
down_revision: Union[str, Sequence[str], None] = 'd579c1629598'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('recipe_ingredients', sa.Column('phase', sa.String(8), nullable=True))
    op.add_column('recipes', sa.Column('production_notes', sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column('recipes', 'production_notes')
    op.drop_column('recipe_ingredients', 'phase')
