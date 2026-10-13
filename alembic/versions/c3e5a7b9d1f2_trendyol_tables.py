"""Trendyol pazaryeri — trendyol_* tabloları (Faz 1, yalnız okuma)

Tek satıcı hesabından YALNIZ OKUMA senkronu (core/trendyol.py):
  * `trendyol_packages` / `trendyol_lines` — sipariş paketleri ve satırları
    (paket = shipmentPackageId; satır tutarları birim + toplam).  Müşteri adı,
    adresi, telefonu, vergi no SAKLANMAZ (KVKK — parser beyaz listesi).
  * `trendyol_fin_rows` — cari hesap ekstresi (settlements + otherfinancials);
    `type_param` istenen enum, `type_label` Trendyol'un Türkçe etiketi.
  * `trendyol_cargo_items` — kargo faturası kalemleri (sipariş no başına).
  * `trendyol_products` — onaylı katalog + Trendyol stok/fiyat.
  * `trendyol_barcode_map` — elle barkod → kart eşlemesi (set = çok satır).
  * `trendyol_sync_state` — akış başına durum / watermark / bildirim sayacı.
Yetki kategorisi `trendyol.*`'ın override'lı kullanıcılara dağıtımı
`database._backfill_perm_trendyol` (sentinel `backfill.perm.trendyol.v1`) ile
init_db'de yapılır — bu dosyada veri yok.

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — tablolar prod'a `init_db()`
create_all ile gelir.  Bu dosya alembic geçmişi ve temiz kurulum içindir;
init_db'nin zaten kurduğu DB'de `alembic stamp head` kullan.

Revision ID: c3e5a7b9d1f2
Revises: b4d6f8a0c2e4
Create Date: 2026-10-13 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c3e5a7b9d1f2'
down_revision: Union[str, Sequence[str], None] = 'b4d6f8a0c2e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'trendyol_packages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('package_id', sa.BigInteger(), nullable=False, unique=True),
        sa.Column('order_number', sa.String(40), nullable=False),
        sa.Column('status', sa.String(30), nullable=True),
        sa.Column('order_date', sa.DateTime(), nullable=True),
        sa.Column('last_modified_at', sa.DateTime(), nullable=True),
        sa.Column('last_modified_ms', sa.BigInteger(), nullable=True),
        sa.Column('gross_amount', sa.Float(), nullable=True),
        sa.Column('seller_discount', sa.Float(), nullable=True),
        sa.Column('ty_discount', sa.Float(), nullable=True),
        sa.Column('total_price', sa.Float(), nullable=True),
        sa.Column('currency', sa.String(8), nullable=True),
        sa.Column('cargo_provider', sa.String(80), nullable=True),
        sa.Column('cargo_tracking', sa.String(64), nullable=True),
        sa.Column('origin_package_ids', sa.String(255), nullable=True),
        sa.Column('cancelled_by', sa.String(40), nullable=True),
        sa.Column('cancel_reason', sa.String(255), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(), nullable=True),
        sa.Column('synced_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_trendyol_packages_order_number', 'trendyol_packages', ['order_number'])
    op.create_index('ix_trendyol_packages_status', 'trendyol_packages', ['status'])
    op.create_index('ix_trendyol_packages_order_date', 'trendyol_packages', ['order_date'])
    op.create_index('ix_trendyol_packages_cargo_tracking', 'trendyol_packages', ['cargo_tracking'])

    op.create_table(
        'trendyol_lines',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('package_row_id', sa.Integer(),
                  sa.ForeignKey('trendyol_packages.id', ondelete='CASCADE'), nullable=False),
        sa.Column('line_id', sa.BigInteger(), nullable=False),
        sa.Column('barcode', sa.String(64), nullable=True),
        sa.Column('stock_code', sa.String(100), nullable=True),
        sa.Column('product_name', sa.String(300), nullable=True),
        sa.Column('content_id', sa.BigInteger(), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('unit_gross', sa.Float(), nullable=True),
        sa.Column('unit_price', sa.Float(), nullable=True),
        sa.Column('gross_total', sa.Float(), nullable=True),
        sa.Column('seller_discount_total', sa.Float(), nullable=True),
        sa.Column('ty_discount_total', sa.Float(), nullable=True),
        sa.Column('net_total', sa.Float(), nullable=True),
        sa.Column('vat_rate', sa.Float(), nullable=True),
        sa.Column('commission_rate', sa.Float(), nullable=True),
        sa.Column('line_status', sa.String(30), nullable=True),
        sa.Column('currency', sa.String(8), nullable=True),
        sa.UniqueConstraint('package_row_id', 'line_id', name='uq_trendyol_lines_pkg_line'),
    )
    op.create_index('ix_trendyol_lines_package_row_id', 'trendyol_lines', ['package_row_id'])
    op.create_index('ix_trendyol_lines_barcode', 'trendyol_lines', ['barcode'])

    op.create_table(
        'trendyol_fin_rows',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('source', sa.String(12), nullable=False),
        sa.Column('row_id', sa.String(64), nullable=False),
        sa.Column('type_param', sa.String(40), nullable=False),
        sa.Column('sub_type', sa.String(40), nullable=True),
        sa.Column('type_label', sa.String(80), nullable=True),
        sa.Column('description', sa.String(255), nullable=True),
        sa.Column('transaction_date', sa.DateTime(), nullable=True),
        sa.Column('order_number', sa.String(40), nullable=True),
        sa.Column('package_id', sa.BigInteger(), nullable=True),
        sa.Column('barcode', sa.String(64), nullable=True),
        sa.Column('debt', sa.Float(), nullable=True),
        sa.Column('credit', sa.Float(), nullable=True),
        sa.Column('commission_rate', sa.Float(), nullable=True),
        sa.Column('commission_amount', sa.Float(), nullable=True),
        sa.Column('seller_revenue', sa.Float(), nullable=True),
        sa.Column('receipt_id', sa.BigInteger(), nullable=True),
        sa.Column('payment_order_id', sa.BigInteger(), nullable=True),
        sa.Column('payment_date', sa.DateTime(), nullable=True),
        sa.Column('order_date', sa.DateTime(), nullable=True),
        sa.Column('commission_invoice_serial', sa.String(64), nullable=True),
        sa.Column('detail_fetched_at', sa.DateTime(), nullable=True),
        sa.Column('synced_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('source', 'row_id', name='uq_trendyol_fin_rows_source_row'),
    )
    op.create_index('ix_trendyol_fin_rows_type_param', 'trendyol_fin_rows', ['type_param'])
    op.create_index('ix_trendyol_fin_rows_transaction_date', 'trendyol_fin_rows', ['transaction_date'])
    op.create_index('ix_trendyol_fin_rows_order_number', 'trendyol_fin_rows', ['order_number'])
    op.create_index('ix_trendyol_fin_rows_package_id', 'trendyol_fin_rows', ['package_id'])
    op.create_index('ix_trendyol_fin_rows_payment_order_id', 'trendyol_fin_rows', ['payment_order_id'])

    op.create_table(
        'trendyol_cargo_items',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('invoice_serial', sa.String(64), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('parcel_unique_id', sa.String(40), nullable=True),
        sa.Column('order_number', sa.String(40), nullable=True),
        sa.Column('kind', sa.String(60), nullable=True),
        sa.Column('is_return', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('amount', sa.Float(), nullable=True),
        sa.Column('desi', sa.Float(), nullable=True),
        sa.Column('synced_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('invoice_serial', 'seq', name='uq_trendyol_cargo_items_serial_seq'),
    )
    op.create_index('ix_trendyol_cargo_items_invoice_serial', 'trendyol_cargo_items', ['invoice_serial'])
    op.create_index('ix_trendyol_cargo_items_parcel_unique_id', 'trendyol_cargo_items', ['parcel_unique_id'])
    op.create_index('ix_trendyol_cargo_items_order_number', 'trendyol_cargo_items', ['order_number'])

    op.create_table(
        'trendyol_products',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('barcode', sa.String(64), nullable=False, unique=True),
        sa.Column('content_id', sa.BigInteger(), nullable=True),
        sa.Column('product_main_id', sa.String(100), nullable=True),
        sa.Column('stock_code', sa.String(100), nullable=True),
        sa.Column('title', sa.String(300), nullable=True),
        sa.Column('brand', sa.String(100), nullable=True),
        sa.Column('category', sa.String(150), nullable=True),
        sa.Column('sale_price', sa.Float(), nullable=True),
        sa.Column('list_price', sa.Float(), nullable=True),
        sa.Column('quantity', sa.Integer(), nullable=True),
        sa.Column('vat_rate', sa.Float(), nullable=True),
        sa.Column('on_sale', sa.Boolean(), nullable=True),
        sa.Column('archived', sa.Boolean(), nullable=True),
        sa.Column('locked', sa.Boolean(), nullable=True),
        sa.Column('blacklisted', sa.Boolean(), nullable=True),
        sa.Column('product_url', sa.String(500), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(), nullable=True),
        sa.Column('last_seen_at', sa.DateTime(), nullable=True),
        sa.Column('synced_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_trendyol_products_last_seen_at', 'trendyol_products', ['last_seen_at'])

    op.create_table(
        'trendyol_barcode_map',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('barcode', sa.String(64), nullable=False),
        sa.Column('item_id', sa.Integer(), sa.ForeignKey('items.id', ondelete='CASCADE'), nullable=False),
        sa.Column('qty', sa.Float(), nullable=False),
        sa.Column('note', sa.String(255), nullable=True),
        sa.Column('created_by', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('barcode', 'item_id', name='uq_trendyol_barcode_map_bc_item'),
    )
    op.create_index('ix_trendyol_barcode_map_barcode', 'trendyol_barcode_map', ['barcode'])
    op.create_index('ix_trendyol_barcode_map_item_id', 'trendyol_barcode_map', ['item_id'])

    op.create_table(
        'trendyol_sync_state',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('stream', sa.String(20), nullable=False, unique=True),
        sa.Column('watermark', sa.DateTime(), nullable=True),
        sa.Column('last_run_at', sa.DateTime(), nullable=True),
        sa.Column('last_ok_at', sa.DateTime(), nullable=True),
        sa.Column('last_status', sa.String(255), nullable=True),
        sa.Column('last_error', sa.String(500), nullable=True),
        sa.Column('consecutive_failures', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_notified_at', sa.DateTime(), nullable=True),
        sa.Column('meta', sa.Text(), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )


def downgrade() -> None:
    op.drop_table('trendyol_sync_state')
    op.drop_index('ix_trendyol_barcode_map_item_id', table_name='trendyol_barcode_map')
    op.drop_index('ix_trendyol_barcode_map_barcode', table_name='trendyol_barcode_map')
    op.drop_table('trendyol_barcode_map')
    op.drop_index('ix_trendyol_products_last_seen_at', table_name='trendyol_products')
    op.drop_table('trendyol_products')
    op.drop_index('ix_trendyol_cargo_items_order_number', table_name='trendyol_cargo_items')
    op.drop_index('ix_trendyol_cargo_items_parcel_unique_id', table_name='trendyol_cargo_items')
    op.drop_index('ix_trendyol_cargo_items_invoice_serial', table_name='trendyol_cargo_items')
    op.drop_table('trendyol_cargo_items')
    for ix in ('payment_order_id', 'package_id', 'order_number', 'transaction_date', 'type_param'):
        op.drop_index(f'ix_trendyol_fin_rows_{ix}', table_name='trendyol_fin_rows')
    op.drop_table('trendyol_fin_rows')
    op.drop_index('ix_trendyol_lines_barcode', table_name='trendyol_lines')
    op.drop_index('ix_trendyol_lines_package_row_id', table_name='trendyol_lines')
    op.drop_table('trendyol_lines')
    for ix in ('cargo_tracking', 'order_date', 'status', 'order_number'):
        op.drop_index(f'ix_trendyol_packages_{ix}', table_name='trendyol_packages')
    op.drop_table('trendyol_packages')
