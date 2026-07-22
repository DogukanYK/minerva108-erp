"""Numune Analiz Formu (FR.KK.01) — sample_analyses

AR-GE/KK formülasyon deneme analiz belgesi: bulk adı, üretim tarihi, lot,
özellik tablosu (JSON string), analiz sonucu, SONUÇ metni, imza adları,
opsiyonel reçete bağı (ad snapshot'lı). Items 'numune' sekmesindeki tedarikçi
numune lotlarıyla (Inventory.is_sample) ilgisiz.
init_db() create_all bu tabloyu açılışta kurar; migration alembic geçmişi +
temiz kurulum içindir.

Revision ID: e9c1a3b5d7f0
Revises: d8f2a4c6e0b1
Create Date: 2026-07-21 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e9c1a3b5d7f0'
down_revision: Union[str, Sequence[str], None] = 'd8f2a4c6e0b1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'sample_analyses',
        sa.Column('id',                sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('document_no',       sa.String(40), nullable=True),
        sa.Column('bulk_name',         sa.String(200), nullable=False),
        sa.Column('production_date',   sa.Date(), nullable=True),
        sa.Column('lot_number',        sa.String(100), nullable=True),
        sa.Column('recipe_id',         sa.Integer(), sa.ForeignKey('recipes.id'), nullable=True),
        sa.Column('recipe_name',       sa.String(200), nullable=True),
        sa.Column('formulation_notes', sa.Text(), nullable=True),
        sa.Column('properties',        sa.Text(), nullable=False, server_default='[]'),
        sa.Column('analyst_name',      sa.String(100), nullable=True),
        sa.Column('result',            sa.String(20), nullable=True),
        sa.Column('result_text',       sa.Text(), nullable=True),
        sa.Column('notes',             sa.Text(), nullable=True),
        sa.Column('approved_by',       sa.String(100), nullable=True),
        sa.Column('qa_representative', sa.String(100), nullable=True),
        sa.Column('form_code',         sa.String(80), nullable=True),
        sa.Column('created_by',        sa.String(100), nullable=True),
        sa.Column('domain',            sa.String(20), nullable=False, server_default='cosmetics'),
        sa.Column('is_active',         sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at',        sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_sample_analyses_document_no', 'sample_analyses', ['document_no'], unique=True)
    op.create_index('ix_sample_analyses_lot_number', 'sample_analyses', ['lot_number'])
    op.create_index('ix_sample_analyses_domain', 'sample_analyses', ['domain'])


def downgrade() -> None:
    op.drop_index('ix_sample_analyses_domain', table_name='sample_analyses')
    op.drop_index('ix_sample_analyses_lot_number', table_name='sample_analyses')
    op.drop_index('ix_sample_analyses_document_no', table_name='sample_analyses')
    op.drop_table('sample_analyses')
