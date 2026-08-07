"""Şahit numune bilgi girişi — periyodik kontrol + teyit bayrağı + soft-delete

Üç parça:
  ① `retention_sample_checks` — periyodik kontrol kaydı (GMP gözlemi).
     Gözlemler `properties` JSON'unda: [{key,state,note}] — SampleAnalysis.
     properties kalıbı; etiket metni DB'ye yazılmaz, core/retention.CHECK_ITEMS
     tek kaynağından okunur.  APPEND-ONLY (düzenlenmez/silinmez).
  ② `retention_samples.needs_review` — sayımda SKT'si belirsiz kalan kayıtların
     yapısal bayrağı.  Eskiden not METNİNE "TEYİT BEKLİYOR" gömülüydü.
     Geriye dönük doldurma burada DEĞİL, `database._backfill_retention_needs_review()`
     içinde (AppSetting sentinel'li — init_db her açılışta koştuğu için
     koşulsuz UPDATE kullanıcının temizlediği bayrağı geri getirirdi).
  ③ `retention_samples.is_active` — soft-delete.  YANLIŞ GİRİLEN kaydı gizler;
     fiziksel imhadan (status='destroyed', stoktan düşer) ayrı kavramdır ve
     stoğa/Transaction defterine dokunmaz.

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — prod şeması `init_db()` alter_safe
bloğuyla evrilir, tablolar `create_all` ile gelir.  Bu dosya alembic geçmişi
ve temiz kurulum içindir; kolonlar ayrıca alter_safe'e de yazıldı
(CLAUDE.md iki-yer kuralı).

Revision ID: e4a6c8b0d2f5
Revises: d2f4a6c8e1b3
Create Date: 2026-08-06 12:40:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e4a6c8b0d2f5'
down_revision: Union[str, Sequence[str], None] = 'd2f4a6c8e1b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'retention_sample_checks',
        sa.Column('id',          sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('sample_id',   sa.Integer(), nullable=False),
        sa.Column('checked_on',  sa.Date(), nullable=False),
        sa.Column('properties',  sa.Text(), nullable=False, server_default='[]'),
        sa.Column('result',      sa.String(20), nullable=False),
        sa.Column('result_note', sa.Text(), nullable=True),
        sa.Column('checked_by',  sa.String(80), nullable=True),
        sa.Column('created_at',  sa.DateTime(), server_default=sa.func.now()),
        sa.ForeignKeyConstraint(['sample_id'], ['retention_samples.id']),
    )
    op.create_index('ix_retention_sample_checks_sample_id',
                    'retention_sample_checks', ['sample_id'])
    op.create_index('ix_retention_sample_checks_created_at',
                    'retention_sample_checks', ['created_at'])

    op.add_column('retention_samples',
                  sa.Column('needs_review', sa.Boolean(), nullable=False,
                            server_default=sa.false()))
    op.add_column('retention_samples',
                  sa.Column('is_active', sa.Boolean(), nullable=False,
                            server_default=sa.true()))


def downgrade() -> None:
    op.drop_column('retention_samples', 'is_active')
    op.drop_column('retention_samples', 'needs_review')
    op.drop_index('ix_retention_sample_checks_created_at',
                  table_name='retention_sample_checks')
    op.drop_index('ix_retention_sample_checks_sample_id',
                  table_name='retention_sample_checks')
    op.drop_table('retention_sample_checks')
