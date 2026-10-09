"""Proforma şablonu: belge başına banka seçimi + şartlar, RUB IBAN

Üç proforma (teklif, teslimat PRF-, B2B sipariş) tek şablonla basılır ve belge
başına 1–3 banka profili seçer (`bank_profile_ids`, JSON liste) + düzenlenebilir
şartlar (`proforma_terms`, JSON: transportation / shipment / delivery_type /
loading_days [/ payment]).  Banka profili RUB IBAN taşıyabilir (ör. Rusya
ödemeleri için Emlak Bank); `is_default` ülke kuralı yokken önceden seçili gelen
bankaları işaretler (şablon: Kuveyt Türk + Vakıfbank — `_backfill_bank_defaults`,
sentinel `backfill.bank_defaults.v1`).

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — kolonlar prod'a `init_db()`
alter_safe satırlarıyla gelir.  Bu dosya alembic geçmişi ve temiz kurulum içindir.

Revision ID: a6c8e0b2d4f7
Revises: f4a6c8e0b2d3
Create Date: 2026-10-09 23:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a6c8e0b2d4f7'
down_revision: Union[str, Sequence[str], None] = 'f4a6c8e0b2d3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_DOCS = ('b2b_orders', 'quotations', 'deliveries')


def upgrade() -> None:
    op.add_column('bank_profiles', sa.Column('iban_rub', sa.String(length=40), nullable=True))
    op.add_column('bank_profiles', sa.Column('is_default', sa.Boolean(), nullable=False,
                                             server_default=sa.false()))
    for table in _DOCS:
        op.add_column(table, sa.Column('bank_profile_ids', sa.Text(), nullable=True))
        op.add_column(table, sa.Column('proforma_terms', sa.Text(), nullable=True))


def downgrade() -> None:
    for table in reversed(_DOCS):
        op.drop_column(table, 'proforma_terms')
        op.drop_column(table, 'bank_profile_ids')
    op.drop_column('bank_profiles', 'is_default')
    op.drop_column('bank_profiles', 'iban_rub')
