"""Influencer / Creator programı tabloları + crm_task.influencer_collab_id

influencer_tier (kademe merdiveni, seed) · influencer_benchmark (platform ×
kademe × metrik aralığı, seed) · influencer_creator (creator kartı) ·
influencer_account (platform hesabı + son metrikler) ·
influencer_metric_snapshot (büyüme serisi) · influencer_application (halka
açık başvuru) · influencer_address · influencer_campaign · influencer_collab
(iş birliği, statü makinesi) · influencer_collab_stage_log · influencer_token
(tek kullanımlık creator linki) · influencer_activity (zaman çizelgesi) ·
influencer_file (Drive diskindeki dosyalar) · influencer_shipment (gönderim —
STOK YOLU Delivery'de, delivery_id UNIQUE) · influencer_content ·
influencer_affiliate (UpPromote eşlemesi) · influencer_referral (UpPromote
referral siparişleri) · influencer_payout (ödeme belge kaydı).

Cross-cutting modül — domain kolonu YOK (CRM/Drive/PDKS emsali).
init_db() create_all bu tabloları açılışta kurar; crm_task'a eklenen
influencer_collab_id kolonu prod'a init_db() alter_safe satırıyla ulaşır
(deploy.sh alembic çalıştırmaz).  Bu dosya alembic geçmişi + temiz kurulum
içindir — CLAUDE.md iki-yer kuralı.  Kişisel veri notu: T.C. kimlik no ve
IBAN hiçbir tabloda TUTULMAZ.

Revision ID: b5d7f9a1c3e5
Revises: a4c6e8b2d1f5
Create Date: 2026-09-05 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b5d7f9a1c3e5'
down_revision: Union[str, Sequence[str], None] = 'a4c6e8b2d1f5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Matris ayarlar (seed init_db'de) ─────────────────────────────────
    op.create_table(
        'influencer_tier',
        sa.Column('id',                    sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('key',                   sa.String(20), nullable=False),
        sa.Column('label',                 sa.String(60), nullable=False),
        sa.Column('min_followers',         sa.Integer(), nullable=False, server_default='0'),
        sa.Column('max_followers',         sa.Integer(), nullable=True),
        sa.Column('commission_pct',        sa.Float(), nullable=False, server_default='10'),
        sa.Column('customer_discount_pct', sa.Float(), nullable=False, server_default='0'),
        sa.Column('max_gift_items',        sa.Integer(), nullable=False, server_default='1'),
        sa.Column('launch_access',         sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('code_allowed',          sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('sort_order',            sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active',             sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('updated_at',            sa.DateTime(), nullable=True),
        sa.Column('updated_by',            sa.String(100), nullable=True),
    )
    op.create_index('ix_influencer_tier_key', 'influencer_tier', ['key'], unique=True)

    op.create_table(
        'influencer_benchmark',
        sa.Column('id',         sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('platform',   sa.String(20), nullable=False),
        sa.Column('tier_key',   sa.String(20), nullable=False),
        sa.Column('metric',     sa.String(30), nullable=False),
        sa.Column('low',        sa.Float(), nullable=False, server_default='0'),
        sa.Column('high',       sa.Float(), nullable=False, server_default='0'),
        sa.Column('updated_at', sa.DateTime(), nullable=True),
        sa.Column('updated_by', sa.String(100), nullable=True),
        sa.UniqueConstraint('platform', 'tier_key', 'metric', name='uq_inf_benchmark_platform_tier_metric'),
    )

    # ── Creator + hesaplar ───────────────────────────────────────────────
    op.create_table(
        'influencer_creator',
        sa.Column('id',                 sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('slug',               sa.String(60), nullable=False),
        sa.Column('full_name',          sa.String(150), nullable=False),
        sa.Column('email',              sa.String(150), nullable=True),
        sa.Column('phone',              sa.String(50), nullable=True),
        sa.Column('country',            sa.String(60), nullable=True),
        sa.Column('city',               sa.String(100), nullable=True),
        sa.Column('language',           sa.String(10), nullable=True),
        sa.Column('birth_year',         sa.Integer(), nullable=True),
        sa.Column('categories_json',    sa.Text(), nullable=True),
        sa.Column('skin_type',          sa.String(40), nullable=True),
        sa.Column('clothing_size',      sa.String(20), nullable=True),
        sa.Column('allergies',          sa.Text(), nullable=True),
        sa.Column('accepted_model',     sa.String(20), nullable=True),
        sa.Column('tier_key',           sa.String(20), nullable=True),
        sa.Column('tier_override_key',  sa.String(20), nullable=True),
        sa.Column('relationship_stage', sa.String(20), nullable=False, server_default='havuz'),
        sa.Column('authenticity_score', sa.Float(), nullable=True),
        sa.Column('score_json',         sa.Text(), nullable=True),
        sa.Column('verification_level', sa.String(4), nullable=True),
        sa.Column('aqs',                sa.Float(), nullable=True),
        sa.Column('fake_pct',           sa.Float(), nullable=True),
        sa.Column('do_not_resend',      sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('rating',             sa.Integer(), nullable=True),
        sa.Column('owner_user_id',      sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('owner_name',         sa.String(100), nullable=True),
        sa.Column('source',             sa.String(50), nullable=True),
        sa.Column('notes',              sa.Text(), nullable=True),
        sa.Column('is_active',          sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at',         sa.DateTime(), nullable=True),
        sa.Column('created_by',         sa.String(100), nullable=True),
        sa.Column('updated_at',         sa.DateTime(), nullable=True),
    )
    op.create_index('ix_influencer_creator_slug', 'influencer_creator', ['slug'], unique=True)
    op.create_index('ix_influencer_creator_full_name', 'influencer_creator', ['full_name'])
    op.create_index('ix_influencer_creator_email', 'influencer_creator', ['email'])
    op.create_index('ix_influencer_creator_tier_key', 'influencer_creator', ['tier_key'])
    op.create_index('ix_influencer_creator_relationship_stage', 'influencer_creator', ['relationship_stage'])
    op.create_index('ix_influencer_creator_owner_user_id', 'influencer_creator', ['owner_user_id'])

    op.create_table(
        'influencer_account',
        sa.Column('id',                       sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('creator_id',               sa.Integer(), sa.ForeignKey('influencer_creator.id', ondelete='CASCADE'), nullable=False),
        sa.Column('platform',                 sa.String(20), nullable=False),
        sa.Column('handle',                   sa.String(120), nullable=False),
        sa.Column('external_id',              sa.String(80), nullable=True),
        sa.Column('followers',                sa.Integer(), nullable=True),
        sa.Column('following',                sa.Integer(), nullable=True),
        sa.Column('posts_count',              sa.Integer(), nullable=True),
        sa.Column('hidden_subscriber_count',  sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('avg_views',                sa.Float(), nullable=True),
        sa.Column('avg_likes',                sa.Float(), nullable=True),
        sa.Column('avg_comments',             sa.Float(), nullable=True),
        sa.Column('er_follower',              sa.Float(), nullable=True),
        sa.Column('er_view',                  sa.Float(), nullable=True),
        sa.Column('view_per_follower',        sa.Float(), nullable=True),
        sa.Column('views_cv',                 sa.Float(), nullable=True),
        sa.Column('audience_geo_json',        sa.Text(), nullable=True),
        sa.Column('audience_age_gender_json', sa.Text(), nullable=True),
        sa.Column('metrics_source',           sa.String(20), nullable=False, server_default='manual'),
        sa.Column('metrics_at',               sa.DateTime(), nullable=True),
        sa.Column('oauth_token_enc',          sa.Text(), nullable=True),
        sa.Column('is_primary',               sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at',               sa.DateTime(), nullable=True),
        sa.Column('updated_at',               sa.DateTime(), nullable=True),
        sa.UniqueConstraint('platform', 'handle', name='uq_inf_account_platform_handle'),
    )
    op.create_index('ix_influencer_account_creator_id', 'influencer_account', ['creator_id'])

    op.create_table(
        'influencer_metric_snapshot',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('account_id',    sa.Integer(), sa.ForeignKey('influencer_account.id', ondelete='CASCADE'), nullable=False),
        sa.Column('taken_at',      sa.DateTime(), nullable=False),
        sa.Column('source',        sa.String(20), nullable=False, server_default='manual'),
        sa.Column('followers',     sa.Integer(), nullable=True),
        sa.Column('posts_json',    sa.Text(), nullable=True),
        sa.Column('computed_json', sa.Text(), nullable=True),
        sa.Column('entered_by',    sa.String(100), nullable=True),
    )
    op.create_index('ix_influencer_metric_snapshot_account_id', 'influencer_metric_snapshot', ['account_id'])
    op.create_index('ix_influencer_metric_snapshot_taken_at', 'influencer_metric_snapshot', ['taken_at'])

    # ── Başvuru + adres ──────────────────────────────────────────────────
    op.create_table(
        'influencer_application',
        sa.Column('id',                   sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('store_key',            sa.String(20), nullable=False),
        sa.Column('full_name',            sa.String(150), nullable=False),
        sa.Column('email',                sa.String(150), nullable=False),
        sa.Column('phone',                sa.String(50), nullable=True),
        sa.Column('country',              sa.String(60), nullable=True),
        sa.Column('city',                 sa.String(100), nullable=True),
        sa.Column('language',             sa.String(10), nullable=True),
        sa.Column('birth_year',           sa.Integer(), nullable=True),
        sa.Column('accounts_json',        sa.Text(), nullable=True),
        sa.Column('form_json',            sa.Text(), nullable=True),
        sa.Column('status',               sa.String(12), nullable=False, server_default='pending'),
        sa.Column('reject_reason',        sa.Text(), nullable=True),
        sa.Column('reviewed_by',          sa.String(100), nullable=True),
        sa.Column('reviewed_at',          sa.DateTime(), nullable=True),
        sa.Column('creator_id',           sa.Integer(), sa.ForeignKey('influencer_creator.id', ondelete='SET NULL'), nullable=True),
        sa.Column('consent_text_version', sa.String(20), nullable=True),
        sa.Column('consent_at',           sa.DateTime(), nullable=True),
        sa.Column('consent_ip',           sa.String(64), nullable=True),
        sa.Column('honeypot_hit',         sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('user_agent',           sa.String(300), nullable=True),
        sa.Column('created_at',           sa.DateTime(), nullable=True),
    )
    op.create_index('ix_influencer_application_store_key', 'influencer_application', ['store_key'])
    op.create_index('ix_influencer_application_email', 'influencer_application', ['email'])
    op.create_index('ix_influencer_application_status', 'influencer_application', ['status'])
    op.create_index('ix_influencer_application_creator_id', 'influencer_application', ['creator_id'])
    op.create_index('ix_influencer_application_created_at', 'influencer_application', ['created_at'])

    op.create_table(
        'influencer_address',
        sa.Column('id',             sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('creator_id',     sa.Integer(), sa.ForeignKey('influencer_creator.id', ondelete='CASCADE'), nullable=False),
        sa.Column('label',          sa.String(60), nullable=True),
        sa.Column('recipient_name', sa.String(150), nullable=True),
        sa.Column('line1',          sa.String(200), nullable=True),
        sa.Column('line2',          sa.String(200), nullable=True),
        sa.Column('district',       sa.String(100), nullable=True),
        sa.Column('city',           sa.String(100), nullable=True),
        sa.Column('postal_code',    sa.String(20), nullable=True),
        sa.Column('country',        sa.String(60), nullable=True),
        sa.Column('phone',          sa.String(50), nullable=True),
        sa.Column('is_default',     sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('verified_at',    sa.DateTime(), nullable=True),
        sa.Column('verified_ip',    sa.String(64), nullable=True),
        sa.Column('created_at',     sa.DateTime(), nullable=True),
    )
    op.create_index('ix_influencer_address_creator_id', 'influencer_address', ['creator_id'])

    # ── Kampanya + iş birliği ────────────────────────────────────────────
    op.create_table(
        'influencer_campaign',
        sa.Column('id',                        sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name',                      sa.String(150), nullable=False),
        sa.Column('store_key',                 sa.String(20), nullable=False),
        sa.Column('market',                    sa.String(10), nullable=True),
        sa.Column('start_on',                  sa.Date(), nullable=True),
        sa.Column('end_on',                    sa.Date(), nullable=True),
        sa.Column('hashtags',                  sa.String(300), nullable=True),
        sa.Column('brief_text',                sa.Text(), nullable=True),
        sa.Column('claim_sheet_json',          sa.Text(), nullable=True),
        sa.Column('default_deliverables_json', sa.Text(), nullable=True),
        sa.Column('status',                    sa.String(20), nullable=False, server_default='taslak'),
        sa.Column('is_active',                 sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at',                sa.DateTime(), nullable=True),
        sa.Column('created_by',                sa.String(100), nullable=True),
        sa.Column('updated_at',                sa.DateTime(), nullable=True),
    )
    op.create_index('ix_influencer_campaign_store_key', 'influencer_campaign', ['store_key'])
    op.create_index('ix_influencer_campaign_status', 'influencer_campaign', ['status'])

    op.create_table(
        'influencer_collab',
        sa.Column('id',                      sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('code',                    sa.String(20), nullable=False),
        sa.Column('creator_id',              sa.Integer(), sa.ForeignKey('influencer_creator.id', ondelete='CASCADE'), nullable=False),
        sa.Column('campaign_id',             sa.Integer(), sa.ForeignKey('influencer_campaign.id', ondelete='SET NULL'), nullable=True),
        sa.Column('store_key',               sa.String(20), nullable=False),
        sa.Column('market',                  sa.String(10), nullable=True),
        sa.Column('model',                   sa.String(10), nullable=False, server_default='barter'),
        sa.Column('tier_key_at_start',       sa.String(20), nullable=True),
        sa.Column('stage',                   sa.String(30), nullable=False, server_default='teklif_gonderildi'),
        sa.Column('stage_changed_at',        sa.DateTime(), nullable=True),
        sa.Column('offered_at',              sa.DateTime(), nullable=True),
        sa.Column('accepted_at',             sa.DateTime(), nullable=True),
        sa.Column('content_due_on',          sa.Date(), nullable=True),
        sa.Column('usage_rights',            sa.String(60), nullable=True),
        sa.Column('exclusivity_days',        sa.Integer(), nullable=False, server_default='0'),
        sa.Column('guideline_ack_at',        sa.DateTime(), nullable=True),
        sa.Column('guideline_version',       sa.String(20), nullable=True),
        sa.Column('guideline_ack_ip',        sa.String(64), nullable=True),
        sa.Column('deliverables_json',       sa.Text(), nullable=True),
        sa.Column('cancel_reason',           sa.Text(), nullable=True),
        sa.Column('no_post_flagged_at',      sa.DateTime(), nullable=True),
        sa.Column('decision',                sa.String(20), nullable=True),
        sa.Column('decision_suggested_json', sa.Text(), nullable=True),
        sa.Column('owner_user_id',           sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('owner_name',              sa.String(100), nullable=True),
        sa.Column('notes',                   sa.Text(), nullable=True),
        sa.Column('is_active',               sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at',              sa.DateTime(), nullable=True),
        sa.Column('created_by',              sa.String(100), nullable=True),
        sa.Column('updated_at',              sa.DateTime(), nullable=True),
    )
    op.create_index('ix_influencer_collab_code', 'influencer_collab', ['code'], unique=True)
    op.create_index('ix_influencer_collab_creator_id', 'influencer_collab', ['creator_id'])
    op.create_index('ix_influencer_collab_campaign_id', 'influencer_collab', ['campaign_id'])
    op.create_index('ix_influencer_collab_store_key', 'influencer_collab', ['store_key'])
    op.create_index('ix_influencer_collab_stage', 'influencer_collab', ['stage'])
    op.create_index('ix_influencer_collab_owner_user_id', 'influencer_collab', ['owner_user_id'])

    op.create_table(
        'influencer_collab_stage_log',
        sa.Column('id',         sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('collab_id',  sa.Integer(), sa.ForeignKey('influencer_collab.id', ondelete='CASCADE'), nullable=False),
        sa.Column('from_stage', sa.String(30), nullable=True),
        sa.Column('to_stage',   sa.String(30), nullable=False),
        sa.Column('reason',     sa.Text(), nullable=True),
        sa.Column('actor',      sa.String(100), nullable=True),
        sa.Column('at',         sa.DateTime(), nullable=False),
    )
    op.create_index('ix_influencer_collab_stage_log_collab_id', 'influencer_collab_stage_log', ['collab_id'])
    op.create_index('ix_influencer_collab_stage_log_at', 'influencer_collab_stage_log', ['at'])

    op.create_table(
        'influencer_token',
        sa.Column('id',         sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('token',      sa.String(64), nullable=False),
        sa.Column('purpose',    sa.String(20), nullable=False),
        sa.Column('creator_id', sa.Integer(), sa.ForeignKey('influencer_creator.id', ondelete='CASCADE'), nullable=True),
        sa.Column('collab_id',  sa.Integer(), sa.ForeignKey('influencer_collab.id', ondelete='SET NULL'), nullable=True),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used_at',    sa.DateTime(), nullable=True),
        sa.Column('created_by', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
    )
    op.create_index('ix_influencer_token_token', 'influencer_token', ['token'], unique=True)
    op.create_index('ix_influencer_token_creator_id', 'influencer_token', ['creator_id'])
    op.create_index('ix_influencer_token_collab_id', 'influencer_token', ['collab_id'])

    op.create_table(
        'influencer_activity',
        sa.Column('id',             sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('creator_id',     sa.Integer(), sa.ForeignKey('influencer_creator.id', ondelete='CASCADE'), nullable=True),
        sa.Column('collab_id',      sa.Integer(), sa.ForeignKey('influencer_collab.id', ondelete='CASCADE'), nullable=True),
        sa.Column('type',           sa.String(20), nullable=False, server_default='note'),
        sa.Column('subject',        sa.String(200), nullable=True),
        sa.Column('body',           sa.Text(), nullable=True),
        sa.Column('author_user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('author_name',    sa.String(100), nullable=True),
        sa.Column('is_pinned',      sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_at',     sa.DateTime(), nullable=True),
    )
    op.create_index('ix_influencer_activity_creator_id', 'influencer_activity', ['creator_id'])
    op.create_index('ix_influencer_activity_collab_id', 'influencer_activity', ['collab_id'])
    op.create_index('ix_influencer_activity_author_user_id', 'influencer_activity', ['author_user_id'])
    op.create_index('ix_influencer_activity_created_at', 'influencer_activity', ['created_at'])

    # ── Dosya (content/payout FK verdiği için önce) ──────────────────────
    op.create_table(
        'influencer_file',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('entity',        sa.String(20), nullable=False),
        sa.Column('entity_id',     sa.Integer(), nullable=False),
        sa.Column('kind',          sa.String(20), nullable=False),
        sa.Column('original_name', sa.String(255), nullable=False),
        sa.Column('stored_name',   sa.String(80), nullable=False),
        sa.Column('size_bytes',    sa.Integer(), nullable=False, server_default='0'),
        sa.Column('content_type',  sa.String(120), nullable=True),
        sa.Column('uploaded_by',   sa.String(100), nullable=True),
        sa.Column('created_at',    sa.DateTime(), nullable=True),
    )
    op.create_index('ix_influencer_file_entity', 'influencer_file', ['entity'])
    op.create_index('ix_influencer_file_entity_id', 'influencer_file', ['entity_id'])

    # ── Gönderim (stok yolu Delivery'de) + içerik ────────────────────────
    op.create_table(
        'influencer_shipment',
        sa.Column('id',                sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('collab_id',         sa.Integer(), sa.ForeignKey('influencer_collab.id', ondelete='CASCADE'), nullable=False),
        sa.Column('delivery_id',       sa.Integer(), sa.ForeignKey('deliveries.id'), nullable=True),
        sa.Column('address_id',        sa.Integer(), sa.ForeignKey('influencer_address.id', ondelete='SET NULL'), nullable=True),
        sa.Column('cogs_total',        sa.Float(), nullable=False, server_default='0'),
        sa.Column('packaging_cost',    sa.Float(), nullable=False, server_default='0'),
        sa.Column('shipping_cost',     sa.Float(), nullable=False, server_default='0'),
        sa.Column('loaded_cost',       sa.Float(), nullable=False, server_default='0'),
        sa.Column('handwritten_note',  sa.Text(), nullable=True),
        sa.Column('shipped_at',        sa.DateTime(), nullable=True),
        sa.Column('delivered_at',      sa.DateTime(), nullable=True),
        sa.Column('reminder1_due',     sa.Date(), nullable=True),
        sa.Column('reminder2_due',     sa.Date(), nullable=True),
        sa.Column('reminder1_task_id', sa.Integer(), sa.ForeignKey('crm_task.id', ondelete='SET NULL'), nullable=True),
        sa.Column('reminder2_task_id', sa.Integer(), sa.ForeignKey('crm_task.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at',        sa.DateTime(), nullable=True),
        sa.Column('created_by',        sa.String(100), nullable=True),
    )
    op.create_index('ix_influencer_shipment_collab_id', 'influencer_shipment', ['collab_id'])
    op.create_index('ix_influencer_shipment_delivery_id', 'influencer_shipment', ['delivery_id'], unique=True)

    op.create_table(
        'influencer_content',
        sa.Column('id',               sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('collab_id',        sa.Integer(), sa.ForeignKey('influencer_collab.id', ondelete='CASCADE'), nullable=False),
        sa.Column('platform',         sa.String(20), nullable=True),
        sa.Column('type',             sa.String(20), nullable=True),
        sa.Column('url',              sa.String(500), nullable=True),
        sa.Column('status',           sa.String(20), nullable=False, server_default='taslak'),
        sa.Column('draft_file_id',    sa.Integer(), sa.ForeignKey('influencer_file.id', ondelete='SET NULL'), nullable=True),
        sa.Column('caption',          sa.Text(), nullable=True),
        sa.Column('review_note',      sa.Text(), nullable=True),
        sa.Column('approved_by',      sa.String(100), nullable=True),
        sa.Column('approved_at',      sa.DateTime(), nullable=True),
        sa.Column('published_at',     sa.DateTime(), nullable=True),
        sa.Column('metrics_d7_json',  sa.Text(), nullable=True),
        sa.Column('metrics_d30_json', sa.Text(), nullable=True),
        sa.Column('compliance_json',  sa.Text(), nullable=True),
        sa.Column('compliance_score', sa.Float(), nullable=True),
        sa.Column('created_at',       sa.DateTime(), nullable=True),
        sa.Column('created_by',       sa.String(100), nullable=True),
        sa.Column('updated_at',       sa.DateTime(), nullable=True),
    )
    op.create_index('ix_influencer_content_collab_id', 'influencer_content', ['collab_id'])
    op.create_index('ix_influencer_content_status', 'influencer_content', ['status'])

    # ── UpPromote eşlemesi + referral + ödeme ────────────────────────────
    op.create_table(
        'influencer_affiliate',
        sa.Column('id',                     sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('creator_id',             sa.Integer(), sa.ForeignKey('influencer_creator.id', ondelete='SET NULL'), nullable=True),
        sa.Column('store_key',              sa.String(20), nullable=False),
        sa.Column('uppromote_affiliate_id', sa.BigInteger(), nullable=True),
        sa.Column('affiliate_email',        sa.String(150), nullable=False),
        sa.Column('sca_ref',                sa.String(40), nullable=True),
        sa.Column('affiliate_link',         sa.String(500), nullable=True),
        sa.Column('coupon_code',            sa.String(60), nullable=True),
        sa.Column('program',                sa.String(60), nullable=True),
        sa.Column('commission_pct',         sa.Float(), nullable=False, server_default='10'),
        sa.Column('status',                 sa.String(12), nullable=False, server_default='bekliyor'),
        sa.Column('linked_at',              sa.DateTime(), nullable=True),
        sa.Column('synced_at',              sa.DateTime(), nullable=True),
        sa.Column('created_at',             sa.DateTime(), nullable=True),
        sa.UniqueConstraint('store_key', 'affiliate_email', name='uq_inf_affiliate_store_email'),
        sa.UniqueConstraint('store_key', 'sca_ref',         name='uq_inf_affiliate_store_sca_ref'),
    )
    op.create_index('ix_influencer_affiliate_creator_id', 'influencer_affiliate', ['creator_id'])
    op.create_index('ix_influencer_affiliate_store_key', 'influencer_affiliate', ['store_key'])
    op.create_index('ix_influencer_affiliate_status', 'influencer_affiliate', ['status'])

    op.create_table(
        'influencer_referral',
        sa.Column('id',                   sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('shopify_order_row_id', sa.Integer(), sa.ForeignKey('shopify_orders.id', ondelete='SET NULL'), nullable=True),
        sa.Column('store_key',            sa.String(20), nullable=False),
        sa.Column('shopify_order_id',     sa.BigInteger(), nullable=False),
        sa.Column('order_number',         sa.String(40), nullable=True),
        sa.Column('creator_id',           sa.Integer(), sa.ForeignKey('influencer_creator.id',   ondelete='SET NULL'), nullable=True),
        sa.Column('affiliate_id',         sa.Integer(), sa.ForeignKey('influencer_affiliate.id', ondelete='SET NULL'), nullable=True),
        sa.Column('collab_id',            sa.Integer(), sa.ForeignKey('influencer_collab.id',    ondelete='SET NULL'), nullable=True),
        sa.Column('source',               sa.String(20), nullable=False, server_default='uppromote_api'),
        sa.Column('order_total',          sa.Float(), nullable=False, server_default='0'),
        sa.Column('commission_amount',    sa.Float(), nullable=False, server_default='0'),
        sa.Column('commission_status',    sa.String(12), nullable=False, server_default='pending'),
        sa.Column('is_new_customer',      sa.Boolean(), nullable=True),
        sa.Column('refunded_total',       sa.Float(), nullable=False, server_default='0'),
        sa.Column('order_at',             sa.DateTime(), nullable=True),
        sa.Column('imported_at',          sa.DateTime(), nullable=True),
        sa.UniqueConstraint('store_key', 'shopify_order_id', name='uq_inf_referral_store_order'),
    )
    op.create_index('ix_influencer_referral_store_key', 'influencer_referral', ['store_key'])
    op.create_index('ix_influencer_referral_creator_id', 'influencer_referral', ['creator_id'])
    op.create_index('ix_influencer_referral_affiliate_id', 'influencer_referral', ['affiliate_id'])
    op.create_index('ix_influencer_referral_collab_id', 'influencer_referral', ['collab_id'])
    op.create_index('ix_influencer_referral_commission_status', 'influencer_referral', ['commission_status'])
    op.create_index('ix_influencer_referral_order_at', 'influencer_referral', ['order_at'])

    op.create_table(
        'influencer_payout',
        sa.Column('id',                   sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('creator_id',           sa.Integer(), sa.ForeignKey('influencer_creator.id', ondelete='CASCADE'), nullable=False),
        sa.Column('period',               sa.String(7), nullable=False),
        sa.Column('store_key',            sa.String(20), nullable=True),
        sa.Column('total',                sa.Float(), nullable=False, server_default='0'),
        sa.Column('currency',             sa.String(3), nullable=False, server_default='TRY'),
        sa.Column('status',               sa.String(20), nullable=False, server_default='uppromote_talep'),
        sa.Column('uppromote_payment_id', sa.String(60), nullable=True),
        sa.Column('tax_doc_type',         sa.String(20), nullable=True),
        sa.Column('tax_doc_no',           sa.String(60), nullable=True),
        sa.Column('withholding_pct',      sa.Float(), nullable=False, server_default='0'),
        sa.Column('bank_note',            sa.String(200), nullable=True),
        sa.Column('paid_at',              sa.DateTime(), nullable=True),
        sa.Column('paid_by',              sa.String(100), nullable=True),
        sa.Column('doc_file_id',          sa.Integer(), sa.ForeignKey('influencer_file.id', ondelete='SET NULL'), nullable=True),
        sa.Column('notes',                sa.Text(), nullable=True),
        sa.Column('created_at',           sa.DateTime(), nullable=True),
        sa.Column('created_by',           sa.String(100), nullable=True),
        sa.UniqueConstraint('creator_id', 'period', 'store_key', name='uq_inf_payout_creator_period_store'),
    )
    op.create_index('ix_influencer_payout_creator_id', 'influencer_payout', ['creator_id'])
    op.create_index('ix_influencer_payout_period', 'influencer_payout', ['period'])
    op.create_index('ix_influencer_payout_status', 'influencer_payout', ['status'])

    # ── Mevcut tabloya kolon: crm_task.influencer_collab_id ──────────────
    # Hatırlatma görevleri mevcut 08:00 CRM taramasından push'lanır; bu kolon
    # görevi iş birliğine bağlar.  (FK yok — bilinçli: crm_task create_all'da
    # influencer_collab'dan önce kurulur, döngü istemiyoruz.)
    op.add_column('crm_task', sa.Column('influencer_collab_id', sa.Integer(), nullable=True))
    op.create_index('ix_crm_task_inf_collab', 'crm_task', ['influencer_collab_id'])


def downgrade() -> None:
    op.drop_index('ix_crm_task_inf_collab', table_name='crm_task')
    op.drop_column('crm_task', 'influencer_collab_id')
    for t in (
        'influencer_payout', 'influencer_referral', 'influencer_affiliate',
        'influencer_content', 'influencer_shipment', 'influencer_file',
        'influencer_activity', 'influencer_token', 'influencer_collab_stage_log',
        'influencer_collab', 'influencer_campaign', 'influencer_address',
        'influencer_application', 'influencer_metric_snapshot', 'influencer_account',
        'influencer_creator', 'influencer_benchmark', 'influencer_tier',
    ):
        op.drop_table(t)
