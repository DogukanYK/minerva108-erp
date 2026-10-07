"""Birleştirilen tedarikçi kartının kazananı (suppliers.merged_into_id)

Tedarikçi birleştirmede (core/suppliers.merge_suppliers) kaybeden kart pasif
kalır; adı kazananın TAKMA ADI olarak yaşar: fiyat listesinin eski yazımı
içe aktarmada kazanana eşlenir (core/supplier_prices.import_prices) ve firma
satın alma planında / Raporlar panelinde "pasif tedarikçi" sayılmaz
(core/purchase_pricing.FirmActivity).  Kart yeniden etkinleştirilince
temizlenir.  Mevcut veri için backfill YOK — birleştirme bu sürümle gelir.

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — kolon prod'a `init_db()` alter_safe
satırıyla gelir.  Bu dosya alembic geçmişi ve temiz kurulum içindir; kolonu
init_db zaten eklemiş bir DB'de `alembic stamp head` kullan.

Revision ID: c4e6a8b0d2f5
Revises: f9d1b3c5e7a2
Create Date: 2026-10-08 01:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c4e6a8b0d2f5'
down_revision: Union[str, Sequence[str], None] = 'f9d1b3c5e7a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('suppliers', sa.Column('merged_into_id', sa.Integer(),
                                         sa.ForeignKey('suppliers.id'), nullable=True))


def downgrade() -> None:
    op.drop_column('suppliers', 'merged_into_id')
