"""Şahit numune dolabı — retention_samples + retention_sample_movements

Üretimde şahit numuneye ayrılan adetler `Inventory`'ye `-S` lotu olarak
yazılmaya DEVAM eder (stok kaynağı orası, Item.current_stock içinde sayılırlar).
Bu tablolar onların üstüne bir YÖNETİM katmanı ekler: hangi dolapta (marka),
hangi rafta/gözde, ne zamana kadar saklanacak, kim ne zaman çıkardı.
`inventory_id` UNIQUE — bir `-S` lotunun tek yönetim kaydı olur; backfill
script'inin idempotency anahtarı da budur.

Ek kolonlar: `items.lot_seq` (ürün bazlı lot sayacı — MNR006 önerisi) ve
`production_history.witness_quantity` (kaç adet dolaba ayrıldı; bugüne kadar
yalnız Transaction not metnindeydi).

init_db() create_all bu tabloları açılışta kurar; migration alembic geçmişi +
temiz kurulum içindir.  Kolonlar ayrıca init_db() alter_safe bloğunda da var
(CLAUDE.md iki-yer kuralı).

Revision ID: c5e7a9b1d3f8
Revises: a4c8e2f6b9d1
Create Date: 2026-08-05 15:20:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c5e7a9b1d3f8'
down_revision: Union[str, Sequence[str], None] = 'a4c8e2f6b9d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'retention_samples',
        sa.Column('id',                    sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('inventory_id',          sa.Integer(), nullable=True),
        sa.Column('item_id',               sa.Integer(), nullable=False),
        sa.Column('item_name',             sa.String(150), nullable=True),
        sa.Column('lot_number',            sa.String(100), nullable=False),
        sa.Column('production_history_id', sa.Integer(), nullable=True),
        sa.Column('brand',                 sa.String(60), nullable=False),
        sa.Column('shelf',                 sa.String(20), nullable=True),
        sa.Column('slot',                  sa.String(20), nullable=True),
        sa.Column('quantity',              sa.Float(), nullable=False, server_default='0'),
        sa.Column('initial_quantity',      sa.Float(), nullable=False, server_default='0'),
        sa.Column('unit',                  sa.String(20), nullable=True),
        sa.Column('produced_at',           sa.DateTime(), nullable=True),
        sa.Column('retention_until',       sa.Date(), nullable=True),
        sa.Column('status',                sa.String(20), nullable=False, server_default='stored'),
        sa.Column('qc_status',             sa.String(20), nullable=True),
        sa.Column('source',                sa.String(20), nullable=False, server_default='production'),
        sa.Column('placed_by',             sa.String(80), nullable=True),
        sa.Column('note',                  sa.Text(), nullable=True),
        sa.Column('domain',                sa.String(20), nullable=False, server_default='cosmetics'),
        sa.Column('created_at',            sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at',            sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['inventory_id'], ['inventory.id']),
        sa.ForeignKeyConstraint(['item_id'], ['items.id']),
        sa.ForeignKeyConstraint(['production_history_id'], ['production_history.id']),
    )
    op.create_index('ix_retention_samples_inventory_id', 'retention_samples',
                    ['inventory_id'], unique=True)
    op.create_index('ix_retention_samples_item_id', 'retention_samples', ['item_id'])
    op.create_index('ix_retention_samples_lot_number', 'retention_samples', ['lot_number'])
    op.create_index('ix_retention_samples_brand', 'retention_samples', ['brand'])
    op.create_index('ix_retention_samples_retention_until', 'retention_samples', ['retention_until'])
    op.create_index('ix_retention_samples_domain', 'retention_samples', ['domain'])

    op.create_table(
        'retention_sample_movements',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('sample_id',     sa.Integer(), nullable=False),
        sa.Column('movement_type', sa.String(20), nullable=False),
        sa.Column('quantity',      sa.Float(), nullable=False, server_default='0'),
        sa.Column('reason',        sa.String(40), nullable=True),
        sa.Column('note',          sa.Text(), nullable=True),
        sa.Column('performed_by',  sa.String(80), nullable=True),
        sa.Column('created_at',    sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['sample_id'], ['retention_samples.id']),
    )
    op.create_index('ix_retention_sample_movements_sample_id',
                    'retention_sample_movements', ['sample_id'])
    op.create_index('ix_retention_sample_movements_created_at',
                    'retention_sample_movements', ['created_at'])

    op.add_column('items', sa.Column('lot_seq', sa.Integer(), nullable=False,
                                     server_default='0'))
    op.add_column('production_history', sa.Column('witness_quantity', sa.Float(),
                                                  nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('production_history', 'witness_quantity')
    op.drop_column('items', 'lot_seq')
    op.drop_index('ix_retention_sample_movements_created_at', table_name='retention_sample_movements')
    op.drop_index('ix_retention_sample_movements_sample_id', table_name='retention_sample_movements')
    op.drop_table('retention_sample_movements')
    for ix in ('domain', 'retention_until', 'brand', 'lot_number', 'item_id', 'inventory_id'):
        op.drop_index(f'ix_retention_samples_{ix}', table_name='retention_samples')
    op.drop_table('retention_samples')
