"""stock_snapshot table — otomatik aylık stok dondurma

Her ayın 1'inde bir önceki ayın stok durumu bu tabloya kalıcı olarak yazılır
(bkz. core/snapshots.py + core/scheduler.py).  Aylık stok raporu, geçmiş bir
ay için önce bu tabloya bakar — varsa dondurulmuş kesin değeri kullanır,
yoksa transaction rekonstrüksiyonuna düşer.

Neden gerekli: rekonstrüksiyon transaction tarihçesinin el değmemiş olmasına
bağlıdır.  Bir undo / veri temizliği transaction silerse geçmiş ay raporu
kayar.  Snapshot, ayın stok fotoğrafını çekip dondurarak bunu önler.

Şema notları:
  • item_id nullable + ON DELETE SET NULL — ürün sonradan silinse bile
    snapshot satırı item_name/category denormalize alanlarıyla yaşar.
  • (year, month, item_id) pratikte tekildir; capture fonksiyonu o ay/yıl
    için önce siler sonra yeniden yazar (idempotent), bu yüzden DB-level
    unique constraint zorunlu değil — yine de sorgu hızı için index var.

Revision ID: 8954ef5ca750
Revises: a78f6d0c7e6c
Create Date: 2026-05-22 11:46:24.986134
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '8954ef5ca750'
down_revision: Union[str, Sequence[str], None] = 'a78f6d0c7e6c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'stock_snapshot',
        sa.Column('id',          sa.Integer(),  primary_key=True, autoincrement=True),
        sa.Column('year',        sa.Integer(),  nullable=False),
        sa.Column('month',       sa.Integer(),  nullable=False),
        sa.Column('item_id',     sa.Integer(),  sa.ForeignKey('items.id', ondelete='SET NULL'), nullable=True),
        sa.Column('item_name',   sa.String(150), nullable=True),
        sa.Column('category',    sa.String(50),  nullable=True),
        sa.Column('pkg_type',    sa.String(20),  nullable=True),
        sa.Column('unit',        sa.String(20),  nullable=True),
        sa.Column('stock',       sa.Float(),     nullable=False, server_default='0'),
        sa.Column('captured_at', sa.DateTime(),  nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_stock_snapshot_year_month', 'stock_snapshot', ['year', 'month'])
    op.create_index('ix_stock_snapshot_item',       'stock_snapshot', ['item_id'])


def downgrade() -> None:
    op.drop_index('ix_stock_snapshot_item',       table_name='stock_snapshot')
    op.drop_index('ix_stock_snapshot_year_month', table_name='stock_snapshot')
    op.drop_table('stock_snapshot')
