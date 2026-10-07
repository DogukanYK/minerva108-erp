"""Malzeme bazlı tedarikçi tercihi (material_supplier_prefs)

Lab'ın malzeme başına "önce X'ten al" (preferred, rank) / "bu malzemede
Y'den alma" (avoid) kararı.  Kapsam TAM OLARAK biri: "aynı malzeme" grubu
(`material_group_id`) ya da tek kart (`item_id`) — CHECK kısıtı.  Kısmi tekil
indeksler: (grup, tedarikçi) ve (kart, tedarikçi).  Satın alma planı ve
"bitirilecek" stok hesabı okur (core/purchase_pricing, core/purchase_plan).

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — prod'da tablo `init_db()` içindeki
`create_all` ile gelir (yeni tablo; kolon eklemesi yok).  Bu dosya alembic
geçmişi ve temiz kurulum içindir; tabloyu create_all zaten açmış bir DB'de
`alembic stamp head` kullan.

Revision ID: f9d1b3c5e7a2
Revises: e7b9d1f3a5c8
Create Date: 2026-10-07 22:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f9d1b3c5e7a2'
down_revision: Union[str, Sequence[str], None] = 'e7b9d1f3a5c8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'material_supplier_prefs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('domain', sa.String(20), nullable=False, server_default='cosmetics'),
        sa.Column('material_group_id', sa.Integer(), sa.ForeignKey('material_groups.id'), nullable=True),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id'), nullable=True),
        sa.Column('supplier_id', sa.Integer(), sa.ForeignKey('suppliers.id'), nullable=False),
        sa.Column('preference', sa.String(10), nullable=False, server_default='preferred'),
        sa.Column('rank', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_by', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.CheckConstraint("(material_group_id IS NULL) <> (item_id IS NULL)",
                           name='ck_material_supplier_prefs_scope'),
    )
    op.create_index('ix_material_supplier_prefs_id', 'material_supplier_prefs', ['id'])
    op.create_index('ix_material_supplier_prefs_domain', 'material_supplier_prefs', ['domain'])
    op.create_index('ix_material_supplier_prefs_material_group_id', 'material_supplier_prefs',
                    ['material_group_id'])
    op.create_index('ix_material_supplier_prefs_item_id', 'material_supplier_prefs', ['item_id'])
    op.create_index('ix_material_supplier_prefs_supplier_id', 'material_supplier_prefs', ['supplier_id'])
    op.create_index('uq_material_supplier_prefs_group', 'material_supplier_prefs',
                    ['material_group_id', 'supplier_id'], unique=True,
                    postgresql_where=sa.text('item_id IS NULL'))
    op.create_index('uq_material_supplier_prefs_item', 'material_supplier_prefs',
                    ['item_id', 'supplier_id'], unique=True,
                    postgresql_where=sa.text('material_group_id IS NULL'))


def downgrade() -> None:
    op.drop_index('uq_material_supplier_prefs_item', table_name='material_supplier_prefs')
    op.drop_index('uq_material_supplier_prefs_group', table_name='material_supplier_prefs')
    op.drop_index('ix_material_supplier_prefs_supplier_id', table_name='material_supplier_prefs')
    op.drop_index('ix_material_supplier_prefs_item_id', table_name='material_supplier_prefs')
    op.drop_index('ix_material_supplier_prefs_material_group_id', table_name='material_supplier_prefs')
    op.drop_index('ix_material_supplier_prefs_domain', table_name='material_supplier_prefs')
    op.drop_index('ix_material_supplier_prefs_id', table_name='material_supplier_prefs')
    op.drop_table('material_supplier_prefs')
