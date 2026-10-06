"""Aynı malzeme grupları + lot taşıma izi + numune çevirme damgası

`material_groups` tablosu: aynı malzemenin farklı tedarikçi kartlarını
BİRLEŞTİRMEDEN bağlar (lab düzeni: her tedarikçi ayrı kart).
`items.material_group_id` (FK + indeks) kartın grubu — kart en fazla tek
grupta.  `inventory.moved_from_item_id` lotun ilk geldiği kart (lot başka
karta taşındıysa; core/stock_lots.move_lot), `inventory.sample_converted_at`
numunenin stoğa çevrildiği an ("yalnız lotu bağla" Transaction yazmadığı için
tek iz).

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — prod şeması `init_db()` alter_safe
bloğuyla evrilir; aynı kolonlar orada da eklenir, tablo create_all ile gelir.
Lab'ın "ayrı kalsın" kararlarından grup tohumlama
`_backfill_material_groups_from_kept()` (AppSetting sentinel
'backfill.material_groups_from_kept.v1') ile bir kez yapılır — burada veri
yazılmaz.  Bu dosya alembic geçmişi ve temiz kurulum içindir; kolonları
init_db zaten eklemiş bir DB'de `alembic stamp head` kullan.

Revision ID: c9e1a3b5d7f2
Revises: b7d9f1a3c5e8
Create Date: 2026-10-06 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9e1a3b5d7f2'
down_revision: Union[str, Sequence[str], None] = 'b7d9f1a3c5e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'material_groups',
        sa.Column('id',         sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name',       sa.String(150), nullable=False),
        sa.Column('note',       sa.Text(), nullable=True),
        sa.Column('domain',     sa.String(20), nullable=False, server_default='cosmetics'),
        sa.Column('is_active',  sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('source',     sa.String(20), nullable=False, server_default='manual'),
        sa.Column('source_ref', sa.Integer(), nullable=True),
        sa.Column('created_by', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column('updated_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
    )
    op.create_index('ix_material_groups_id', 'material_groups', ['id'])
    op.create_index('ix_material_groups_domain', 'material_groups', ['domain'])

    op.add_column('items', sa.Column('material_group_id', sa.Integer(),
                                     sa.ForeignKey('material_groups.id'), nullable=True))
    op.create_index('ix_items_material_group_id', 'items', ['material_group_id'])

    op.add_column('inventory', sa.Column('moved_from_item_id', sa.Integer(),
                                         sa.ForeignKey('items.id'), nullable=True))
    op.add_column('inventory', sa.Column('sample_converted_at', sa.DateTime(), nullable=True))


def downgrade() -> None:
    op.drop_column('inventory', 'sample_converted_at')
    op.drop_column('inventory', 'moved_from_item_id')
    op.drop_index('ix_items_material_group_id', table_name='items')
    op.drop_column('items', 'material_group_id')
    op.drop_index('ix_material_groups_domain', table_name='material_groups')
    op.drop_index('ix_material_groups_id', table_name='material_groups')
    op.drop_table('material_groups')
