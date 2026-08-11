"""Ürün yorumları — davet + yorum tabloları, sesli not destekli

İki yeni tablo + shopify_orders'a bir kolon:
  ① `review_invites` — tek kullanımlık davet linki (hediye ürün /
     doğrulanmış alışveriş).  token unique+indexed; şifre yok, token'ın
     kendisi sır.
  ② `product_reviews` — yorumun kendisi.  IMS gerçeğin kaynağıdır; onay
     sonrası Shopify'a (metaobject + Files) itilir.  `is_published`,
     `publish_step`'ten AYRI bir bayrak — rollback adım bilgisini geri
     sarabilir ama etkiyle aynı commit'te yazılan bayrağı geri alamaz
     (ShopifyOrder.stock_applied ile birebir aynı gerekçe).
  ③ `shopify_orders.invites_created` — doğrulanmış-alıcı daveti üretildi mi
     idempotency bayrağı (webhook redelivery aynı siparişe iki davet
     üretmesin).

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — prod şeması `init_db()`
alter_safe bloğuyla evrilir, tablolar `create_all` ile gelir.  Bu dosya
alembic geçmişi ve temiz kurulum içindir; kolonlar ayrıca alter_safe'e
de yazıldı (CLAUDE.md iki-yer kuralı) ve model sınıfları database.py'de
tanımlı.

Revision ID: b8d2f4a6c9e1
Revises: e4a6c8b0d2f5
Create Date: 2026-08-11 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b8d2f4a6c9e1'
down_revision: Union[str, Sequence[str], None] = 'e4a6c8b0d2f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'review_invites',
        sa.Column('id',                  sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('token',                sa.String(64), nullable=False),
        sa.Column('kind',                 sa.String(20), nullable=False),
        sa.Column('store_key',            sa.String(20), nullable=False),
        sa.Column('shopify_product_id',   sa.BigInteger(), nullable=False),
        sa.Column('product_title',        sa.String(255), nullable=True),
        sa.Column('product_handle',       sa.String(255), nullable=True),
        sa.Column('shopify_order_id',     sa.BigInteger(), nullable=True),
        sa.Column('order_number',         sa.String(40), nullable=True),
        sa.Column('recipient_email',      sa.String(150), nullable=True),
        sa.Column('recipient_name',       sa.String(150), nullable=True),
        sa.Column('locale',               sa.String(10), nullable=False, server_default='tr'),
        sa.Column('expires_at',           sa.DateTime(), nullable=False),
        sa.Column('used_at',              sa.DateTime(), nullable=True),
        sa.Column('created_by',           sa.String(100), nullable=True),
        sa.Column('created_at',           sa.DateTime(), server_default=sa.func.now()),
    )
    op.create_index('ix_review_invites_token', 'review_invites', ['token'], unique=True)
    op.create_index('ix_review_invites_store_key', 'review_invites', ['store_key'])

    op.create_table(
        'product_reviews',
        sa.Column('id',                    sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('invite_id',              sa.Integer(), nullable=False),
        sa.Column('store_key',              sa.String(20), nullable=False),
        sa.Column('shopify_product_id',     sa.BigInteger(), nullable=False),
        sa.Column('product_title',          sa.String(255), nullable=True),
        sa.Column('source',                 sa.String(20), nullable=False),
        sa.Column('author_name',            sa.String(60), nullable=False),
        sa.Column('author_email',           sa.String(150), nullable=True),
        sa.Column('rating',                 sa.Integer(), nullable=False),
        sa.Column('body',                   sa.Text(), nullable=True),
        sa.Column('locale',                 sa.String(10), nullable=False, server_default='tr'),
        sa.Column('audio_stored_name',      sa.String(80), nullable=True),
        sa.Column('audio_mime',             sa.String(60), nullable=True),
        sa.Column('audio_bytes',            sa.Integer(), nullable=True),
        sa.Column('audio_duration_s',       sa.Integer(), nullable=True),
        sa.Column('transcript',             sa.Text(), nullable=True),
        sa.Column('consent_voice',          sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('consent_text_version',   sa.String(20), nullable=True),
        sa.Column('consent_ip',             sa.String(45), nullable=True),
        sa.Column('consent_at',             sa.DateTime(), nullable=True),
        sa.Column('status',                 sa.String(24), nullable=False, server_default='pending'),
        sa.Column('publish_step',           sa.String(30), nullable=True),
        sa.Column('is_published',           sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('moderation_note',        sa.Text(), nullable=True),
        sa.Column('moderated_by',           sa.String(100), nullable=True),
        sa.Column('moderated_at',           sa.DateTime(), nullable=True),
        sa.Column('shopify_metaobject_id',  sa.String(80), nullable=True),
        sa.Column('shopify_file_id',        sa.String(80), nullable=True),
        sa.Column('shopify_file_url',       sa.String(500), nullable=True),
        sa.Column('publish_error',          sa.String(500), nullable=True),
        sa.Column('publish_attempts',       sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at',             sa.DateTime(), server_default=sa.func.now()),
        sa.Column('updated_at',             sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['invite_id'], ['review_invites.id']),
    )
    op.create_index('ix_product_reviews_invite_id', 'product_reviews', ['invite_id'])
    op.create_index('ix_product_reviews_store_key', 'product_reviews', ['store_key'])
    op.create_index('ix_product_reviews_shopify_product_id', 'product_reviews', ['shopify_product_id'])
    op.create_index('ix_product_reviews_status', 'product_reviews', ['status'])

    op.add_column('shopify_orders',
                  sa.Column('invites_created', sa.Boolean(), nullable=False,
                            server_default=sa.false()))


def downgrade() -> None:
    op.drop_column('shopify_orders', 'invites_created')
    op.drop_index('ix_product_reviews_status', table_name='product_reviews')
    op.drop_index('ix_product_reviews_shopify_product_id', table_name='product_reviews')
    op.drop_index('ix_product_reviews_store_key', table_name='product_reviews')
    op.drop_index('ix_product_reviews_invite_id', table_name='product_reviews')
    op.drop_table('product_reviews')
    op.drop_index('ix_review_invites_store_key', table_name='review_invites')
    op.drop_index('ix_review_invites_token', table_name='review_invites')
    op.drop_table('review_invites')
