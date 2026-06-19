"""Minerva Drive — dosya paylaşım tabloları

drive_file, drive_collection, drive_collection_file.  init_db() create_all bu
tabloları açılışta kurar; migration alembic geçmişi + temiz kurulum içindir.
Dosya İÇERİĞİ DB'de değil; diskte (drive_files/) tutulur.

Revision ID: e5b3c9a17d42
Revises: d4a2e9c1f6b8
Create Date: 2026-06-19 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e5b3c9a17d42'
down_revision: Union[str, Sequence[str], None] = 'd4a2e9c1f6b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'drive_file',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('original_name', sa.String(255), nullable=False),
        sa.Column('stored_name',   sa.String(80),  nullable=False, unique=True),
        sa.Column('size_bytes',    sa.Integer(), nullable=False, server_default='0'),
        sa.Column('content_type',  sa.String(120), nullable=True),
        sa.Column('uploaded_by',   sa.String(100), nullable=True),
        sa.Column('created_at',    sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        'drive_collection',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name',          sa.String(150), nullable=False),
        sa.Column('share_token',   sa.String(64), nullable=False),
        sa.Column('password_hash', sa.String(255), nullable=True),
        sa.Column('expires_at',    sa.DateTime(), nullable=True),
        sa.Column('created_by',    sa.String(100), nullable=True),
        sa.Column('created_at',    sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_drive_collection_share_token', 'drive_collection', ['share_token'], unique=True)
    op.create_table(
        'drive_collection_file',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('collection_id', sa.Integer(), sa.ForeignKey('drive_collection.id', ondelete='CASCADE'), nullable=False),
        sa.Column('file_id',       sa.Integer(), sa.ForeignKey('drive_file.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sort_order',    sa.Integer(), server_default='0'),
    )
    op.create_index('ix_drive_cf_collection', 'drive_collection_file', ['collection_id'])
    op.create_index('ix_drive_cf_file',       'drive_collection_file', ['file_id'])


def downgrade() -> None:
    op.drop_table('drive_collection_file')
    op.drop_index('ix_drive_collection_share_token', table_name='drive_collection')
    op.drop_table('drive_collection')
    op.drop_table('drive_file')
