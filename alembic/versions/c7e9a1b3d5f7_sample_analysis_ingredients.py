"""Numune Analizi — bileşen satırları + çalışma türü

sample_analysis_ingredients: FR.KK.01 formunda hangi hammadde, nereden (numune
lotu / stok / henüz gelmedi), ne kadar; consumed_qty fiilen düşülmüş miktar.
sample_analyses.mode: 'existing' (mevcut reçete üzerinde) | 'new' (yeni reçete).
init_db() create_all tabloyu açılışta kurar, mode kolonu alter_safe ile eklenir;
migration alembic geçmişi + temiz kurulum içindir (deploy.sh alembic çalıştırmaz).

Revision ID: c7e9a1b3d5f7
Revises: b5d7f9a1c3e5
Create Date: 2026-09-09 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c7e9a1b3d5f7'
down_revision: Union[str, Sequence[str], None] = 'b5d7f9a1c3e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sample_analyses',
                  sa.Column('mode', sa.String(10), nullable=False, server_default='new'))
    op.execute("UPDATE sample_analyses SET mode='existing' WHERE recipe_id IS NOT NULL")
    op.create_table(
        'sample_analysis_ingredients',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('analysis_id',   sa.Integer(), sa.ForeignKey('sample_analyses.id'), nullable=False),
        sa.Column('item_id',       sa.Integer(), sa.ForeignKey('items.id'), nullable=False),
        sa.Column('item_name',     sa.String(150), nullable=False),
        sa.Column('unit',          sa.String(20), nullable=True),
        sa.Column('source',        sa.String(10), nullable=False, server_default='pending'),
        sa.Column('inventory_id',  sa.Integer(), sa.ForeignKey('inventory.id', ondelete='SET NULL'), nullable=True),
        sa.Column('lot_number',    sa.String(100), nullable=True),
        sa.Column('supplier_name', sa.String(150), nullable=True),
        sa.Column('quantity',      sa.Float(), nullable=False, server_default='0'),
        sa.Column('consumed_qty',  sa.Float(), nullable=False, server_default='0'),
        sa.Column('note',          sa.String(300), nullable=True),
        sa.Column('position',      sa.Integer(), nullable=False, server_default='0'),
    )
    op.create_index('ix_sample_analysis_ingredients_analysis_id', 'sample_analysis_ingredients', ['analysis_id'])
    op.create_index('ix_sample_analysis_ingredients_item_id', 'sample_analysis_ingredients', ['item_id'])
    op.create_index('ix_sample_analysis_ingredients_inventory_id', 'sample_analysis_ingredients', ['inventory_id'])


def downgrade() -> None:
    op.drop_index('ix_sample_analysis_ingredients_inventory_id', table_name='sample_analysis_ingredients')
    op.drop_index('ix_sample_analysis_ingredients_item_id', table_name='sample_analysis_ingredients')
    op.drop_index('ix_sample_analysis_ingredients_analysis_id', table_name='sample_analysis_ingredients')
    op.drop_table('sample_analysis_ingredients')
    op.drop_column('sample_analyses', 'mode')
