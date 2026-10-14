"""Trendyol satış artırma — Liste Doktoru, buybox, müşteri soruları

Spec: docs/superpowers/specs/2026-10-13-trendyol-satis-artirma-design.md.
  * `trendyol_products` + içerik (açıklama, özellikler, görsel sayısı,
    kategori, teslimat süresi, içerik özeti) ve buybox kolonları.
  * `trendyol_category_attrs` — kategori özellik listesi (+ tembel değerler).
  * `trendyol_product_notes` — ekibin ürün bilgi notu.
  * `trendyol_listing_suggestions` — Claude liste önerileri (girdi özeti ile
    önbellek; maliyet ayrıca assistant_usage'a).
  * `trendyol_score_history` — günlük liste puanı.
  * `trendyol_questions` — müşteri soruları + taslak (müşteri adı/id YOK).

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — kolonlar prod'a `init_db()`
alter_safe, tablolar create_all ile gelir.  Bu dosya geçmiş ve temiz kurulum
içindir; init_db'nin kurduğu DB'de `alembic stamp head`.

Revision ID: d5f7a9c1e3b6
Revises: c3e5a7b9d1f2
Create Date: 2026-10-13 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd5f7a9c1e3b6'
down_revision: Union[str, Sequence[str], None] = 'c3e5a7b9d1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_PRODUCT_COLUMNS = (
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('attributes_json', sa.Text(), nullable=True),
    sa.Column('image_count', sa.Integer(), nullable=True),
    sa.Column('category_id', sa.BigInteger(), nullable=True),
    sa.Column('delivery_duration', sa.Integer(), nullable=True),
    sa.Column('rush_delivery', sa.Boolean(), nullable=True),
    sa.Column('content_hash', sa.String(64), nullable=True),
    sa.Column('buybox_order', sa.Integer(), nullable=True),
    sa.Column('buybox_price', sa.Float(), nullable=True),
    sa.Column('buybox_second_price', sa.Float(), nullable=True),
    sa.Column('buybox_third_price', sa.Float(), nullable=True),
    sa.Column('has_multiple_seller', sa.Boolean(), nullable=True),
    sa.Column('buybox_checked_at', sa.DateTime(), nullable=True),
    sa.Column('buybox_alerted_at', sa.DateTime(), nullable=True),
)


def upgrade() -> None:
    for col in _PRODUCT_COLUMNS:
        op.add_column('trendyol_products', col.copy())

    op.create_table(
        'trendyol_category_attrs',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('category_id', sa.BigInteger(), nullable=False),
        sa.Column('category_name', sa.String(150), nullable=True),
        sa.Column('attribute_id', sa.BigInteger(), nullable=False),
        sa.Column('name', sa.String(150), nullable=True),
        sa.Column('required', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('slicer', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('varianter', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('allow_custom', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('allow_multiple', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('values_json', sa.Text(), nullable=True),
        sa.Column('values_fetched_at', sa.DateTime(), nullable=True),
        sa.Column('fetched_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('category_id', 'attribute_id', name='uq_trendyol_category_attrs_cat_attr'),
    )
    op.create_index('ix_trendyol_category_attrs_category_id', 'trendyol_category_attrs', ['category_id'])

    op.create_table(
        'trendyol_product_notes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('barcode', sa.String(64), nullable=False, unique=True),
        sa.Column('note', sa.Text(), nullable=False, server_default=''),
        sa.Column('updated_by', sa.String(100), nullable=True),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
    )

    op.create_table(
        'trendyol_listing_suggestions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('barcode', sa.String(64), nullable=False),
        sa.Column('input_hash', sa.String(64), nullable=False),
        sa.Column('prompt_version', sa.String(20), nullable=False),
        sa.Column('model', sa.String(60), nullable=False),
        sa.Column('payload_json', sa.Text(), nullable=False),
        sa.Column('flags_json', sa.Text(), nullable=True),
        sa.Column('flagged', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('cost_usd', sa.Float(), nullable=False, server_default='0'),
        sa.Column('created_by', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_trendyol_listing_suggestions_input_hash', 'trendyol_listing_suggestions', ['input_hash'])
    op.create_index('ix_trendyol_suggestions_bc_created', 'trendyol_listing_suggestions', ['barcode', 'created_at'])

    op.create_table(
        'trendyol_score_history',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('barcode', sa.String(64), nullable=False),
        sa.Column('day', sa.Date(), nullable=False),
        sa.Column('score', sa.Integer(), nullable=False),
        sa.Column('breakdown_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.UniqueConstraint('barcode', 'day', name='uq_trendyol_score_history_bc_day'),
    )
    op.create_index('ix_trendyol_score_history_barcode', 'trendyol_score_history', ['barcode'])

    op.create_table(
        'trendyol_questions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('question_id', sa.BigInteger(), nullable=False, unique=True),
        sa.Column('product_main_id', sa.String(100), nullable=True),
        sa.Column('barcode', sa.String(64), nullable=True),
        sa.Column('product_name', sa.String(300), nullable=True),
        sa.Column('text', sa.Text(), nullable=True),
        sa.Column('status', sa.String(30), nullable=True),
        sa.Column('asked_at', sa.DateTime(), nullable=True),
        sa.Column('answer_text', sa.Text(), nullable=True),
        sa.Column('answered_at', sa.DateTime(), nullable=True),
        sa.Column('rejected_reason', sa.String(255), nullable=True),
        sa.Column('web_url', sa.String(500), nullable=True),
        sa.Column('draft_text', sa.Text(), nullable=True),
        sa.Column('draft_flags_json', sa.Text(), nullable=True),
        sa.Column('draft_at', sa.DateTime(), nullable=True),
        sa.Column('draft_by', sa.String(100), nullable=True),
        sa.Column('notified_at', sa.DateTime(), nullable=True),
        sa.Column('first_seen_at', sa.DateTime(), nullable=True),
        sa.Column('synced_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_trendyol_questions_product_main_id', 'trendyol_questions', ['product_main_id'])
    op.create_index('ix_trendyol_questions_status', 'trendyol_questions', ['status'])
    op.create_index('ix_trendyol_questions_asked_at', 'trendyol_questions', ['asked_at'])


def downgrade() -> None:
    for ix in ('asked_at', 'status', 'product_main_id'):
        op.drop_index(f'ix_trendyol_questions_{ix}', table_name='trendyol_questions')
    op.drop_table('trendyol_questions')
    op.drop_index('ix_trendyol_score_history_barcode', table_name='trendyol_score_history')
    op.drop_table('trendyol_score_history')
    op.drop_index('ix_trendyol_suggestions_bc_created', table_name='trendyol_listing_suggestions')
    op.drop_index('ix_trendyol_listing_suggestions_input_hash', table_name='trendyol_listing_suggestions')
    op.drop_table('trendyol_listing_suggestions')
    op.drop_table('trendyol_product_notes')
    op.drop_index('ix_trendyol_category_attrs_category_id', table_name='trendyol_category_attrs')
    op.drop_table('trendyol_category_attrs')
    for col in reversed(_PRODUCT_COLUMNS):
        op.drop_column('trendyol_products', col.name)
