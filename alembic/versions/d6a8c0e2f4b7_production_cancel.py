"""Üretim iptali — tüketim dökümü tablosu + iptal damgası

`production_consumptions` tablosu: üretim başlatılırken yazılan HER
Transaction'ın satırı (hangi karttan / lottan / ne kadar, hangi Output ya da
Input ile; iptalde hangi telafi Adjustment'ı ile).  Eski üretimler iptal
anında defterden yeniden kurulup `source='ledger'` olarak yazılır
(core/production_cancel.py).  `production_history`'ye iptal damgası:
`cancelled_at` (+ indeks `ix_prodhist_cancelled`), `cancelled_by`,
`cancel_reason`, `lot_released`.

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — prod şeması `init_db()` alter_safe
bloğuyla evrilir; aynı kolonlar orada da eklenir, tablo create_all ile gelir.
Override'lı kullanıcılara `production.cancel` yetkisinin yazılması
`_backfill_perm_production_cancel()` (AppSetting sentinel
'backfill.perm.production_cancel.v1') ile bir kez yapılır — burada veri
yazılmaz.  Bu dosya alembic geçmişi ve temiz kurulum içindir; kolonları
init_db zaten eklemiş bir DB'de `alembic stamp head` kullan.

Revision ID: d6a8c0e2f4b7
Revises: c9e1a3b5d7f2
Create Date: 2026-10-07 15:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd6a8c0e2f4b7'
down_revision: Union[str, Sequence[str], None] = 'c9e1a3b5d7f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'production_consumptions',
        sa.Column('id',             sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('production_id',  sa.Integer(), sa.ForeignKey('production_history.id'), nullable=False),
        sa.Column('kind',           sa.String(12), nullable=False),
        sa.Column('recipe_item_id', sa.Integer(),
                  sa.ForeignKey('items.id', ondelete='SET NULL'), nullable=True),
        sa.Column('item_id',        sa.Integer(), sa.ForeignKey('items.id'), nullable=False),
        sa.Column('inventory_id',   sa.Integer(),
                  sa.ForeignKey('inventory.id', ondelete='SET NULL'), nullable=True),
        sa.Column('lot_number',     sa.String(100), nullable=True),
        sa.Column('supplier_id',    sa.Integer(),
                  sa.ForeignKey('suppliers.id', ondelete='SET NULL'), nullable=True),
        sa.Column('supplier_name',  sa.String(150), nullable=True),
        sa.Column('quantity',       sa.Float(), nullable=False),
        sa.Column('factor',         sa.Float(), nullable=False, server_default='1.0'),
        sa.Column('unit',           sa.String(20), nullable=True),
        sa.Column('phase',          sa.String(8), nullable=True),
        sa.Column('transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'), nullable=True),
        sa.Column('cancel_transaction_id', sa.Integer(), sa.ForeignKey('transactions.id'),
                  nullable=True),
        sa.Column('source',         sa.String(10), nullable=False, server_default='live'),
        sa.Column('domain',         sa.String(20), nullable=False, server_default='cosmetics'),
        sa.Column('created_at',     sa.DateTime(), nullable=True, server_default=sa.func.now()),
    )
    op.create_index('ix_production_consumptions_id', 'production_consumptions', ['id'])
    op.create_index('ix_production_consumptions_production_id', 'production_consumptions',
                    ['production_id'])
    op.create_index('ix_production_consumptions_transaction_id', 'production_consumptions',
                    ['transaction_id'])
    op.create_index('ix_production_consumptions_domain', 'production_consumptions', ['domain'])

    op.add_column('production_history', sa.Column('cancelled_at', sa.DateTime(), nullable=True))
    op.add_column('production_history', sa.Column('cancelled_by', sa.String(100), nullable=True))
    op.add_column('production_history', sa.Column('cancel_reason', sa.Text(), nullable=True))
    op.add_column('production_history', sa.Column('lot_released', sa.Boolean(), nullable=False,
                                                  server_default=sa.false()))
    op.create_index('ix_prodhist_cancelled', 'production_history', ['cancelled_at'])


def downgrade() -> None:
    op.drop_index('ix_prodhist_cancelled', table_name='production_history')
    op.drop_column('production_history', 'lot_released')
    op.drop_column('production_history', 'cancel_reason')
    op.drop_column('production_history', 'cancelled_by')
    op.drop_column('production_history', 'cancelled_at')
    op.drop_index('ix_production_consumptions_domain', table_name='production_consumptions')
    op.drop_index('ix_production_consumptions_transaction_id', table_name='production_consumptions')
    op.drop_index('ix_production_consumptions_production_id', table_name='production_consumptions')
    op.drop_index('ix_production_consumptions_id', table_name='production_consumptions')
    op.drop_table('production_consumptions')
