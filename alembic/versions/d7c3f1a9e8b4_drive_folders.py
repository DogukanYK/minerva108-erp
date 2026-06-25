"""Drive klasör ağacı — drive_folder + drive_file.folder_id

Minerva Drive'ı düz listeden hiyerarşik klasör (tree) sistemine yükseltir.
init_db() create_all + alter_safe bunları açılışta kurar; migration alembic
geçmişi + temiz kurulum içindir.  init_db ayrıca eski klasör-yüklemelerini
(adında '/' olanları) gerçek klasör ağacına çeviren idempotent bir backfill
çalıştırır.

Revision ID: d7c3f1a9e8b4
Revises: c5e7a9b13d24
Create Date: 2026-06-24 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd7c3f1a9e8b4'
down_revision: Union[str, Sequence[str], None] = 'c5e7a9b13d24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'drive_folder',
        sa.Column('id',         sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name',       sa.String(255), nullable=False),
        sa.Column('parent_id',  sa.Integer(),
                  sa.ForeignKey('drive_folder.id', ondelete='CASCADE'), nullable=True),
        sa.Column('created_by', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_drive_folder_parent', 'drive_folder', ['parent_id'])
    op.add_column('drive_file', sa.Column(
        'folder_id', sa.Integer(),
        sa.ForeignKey('drive_folder.id', ondelete='SET NULL'), nullable=True))
    op.create_index('ix_drive_file_folder', 'drive_file', ['folder_id'])
    # Klasör paylaşımı (Faz 2) — link'e klasör bağla
    op.create_table(
        'drive_collection_folder',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('collection_id', sa.Integer(),
                  sa.ForeignKey('drive_collection.id', ondelete='CASCADE'), nullable=False),
        sa.Column('folder_id',     sa.Integer(),
                  sa.ForeignKey('drive_folder.id', ondelete='CASCADE'), nullable=False),
        sa.Column('sort_order',    sa.Integer(), server_default='0'),
    )
    op.create_index('ix_drive_cf2_collection', 'drive_collection_folder', ['collection_id'])
    op.create_index('ix_drive_cf2_folder',     'drive_collection_folder', ['folder_id'])


def downgrade() -> None:
    op.drop_index('ix_drive_cf2_folder',     table_name='drive_collection_folder')
    op.drop_index('ix_drive_cf2_collection', table_name='drive_collection_folder')
    op.drop_table('drive_collection_folder')
    op.drop_index('ix_drive_file_folder', table_name='drive_file')
    op.drop_column('drive_file', 'folder_id')
    op.drop_index('ix_drive_folder_parent', table_name='drive_folder')
    op.drop_table('drive_folder')
