"""Minerva Asistan — assistant_documents (Belge Oku, 13.10.2026)

Kullanıcının yüklediği fatura / proforma / irsaliye / CoA belgesinin yapay
zekâ çıkarımı (`core/assistant/documents.py`).  Dosyanın kendisi SAKLANMAZ —
yalnız sha256 (aynı belge yeniden yüklenince çıkarım yeniden kullanılır).

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — tablo prod'a `init_db()` create_all
ile gelir.  Bu dosya alembic geçmişi ve temiz kurulum içindir; init_db'nin
zaten kurduğu DB'de `alembic stamp head` kullan.

Kimlik rastgele: aynı gün main'e giren Trendyol migration'ı ilk seçilen
desenli kimliği (d5f7a9c1e3b6) almıştı; bu dosya onun ÜSTÜNE zincirlenir.

Revision ID: b917b245c7c5
Revises: d5f7a9c1e3b6
Create Date: 2026-10-13 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b917b245c7c5'
down_revision: Union[str, Sequence[str], None] = 'd5f7a9c1e3b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'assistant_documents',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('domain', sa.String(20), nullable=False),
        sa.Column('file_name', sa.String(255), nullable=False),
        sa.Column('media_type', sa.String(40), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('pages', sa.Integer(), nullable=True),
        sa.Column('doc_type', sa.String(20), nullable=True),
        sa.Column('extraction_json', sa.Text(), nullable=False),
        sa.Column('model', sa.String(60), nullable=True),
        sa.Column('cost_usd', sa.Float(), nullable=False, server_default=sa.text('0')),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_assistant_documents_user_created', 'assistant_documents', ['user_id', 'created_at'])
    op.create_index('ix_assistant_documents_user_sha', 'assistant_documents', ['user_id', 'sha256'])


def downgrade() -> None:
    op.drop_index('ix_assistant_documents_user_sha', table_name='assistant_documents')
    op.drop_index('ix_assistant_documents_user_created', table_name='assistant_documents')
    op.drop_table('assistant_documents')
