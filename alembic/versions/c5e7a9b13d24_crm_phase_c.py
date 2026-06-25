"""CRM Faz C — kaydedilmiş görünüm, etiket, özel alan, dosya eki tabloları

crm_saved_view, crm_tag, crm_entity_tag, crm_field_def, crm_field_value,
crm_attachment.  init_db() create_all bunları açılışta kurar; bu migration
alembic geçmişi + temiz kurulum içindir.

Revision ID: c5e7a9b13d24
Revises: b3d5f7092468
Create Date: 2026-06-25 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c5e7a9b13d24'
down_revision: Union[str, Sequence[str], None] = 'b3d5f7092468'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'crm_saved_view',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('entity', sa.String(20), nullable=False),
        sa.Column('name', sa.String(80), nullable=False),
        sa.Column('criteria', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
    )
    op.create_index('ix_crm_saved_view_user', 'crm_saved_view', ['user_id'])

    op.create_table(
        'crm_tag',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name', sa.String(60), nullable=False),
        sa.Column('color', sa.String(20), nullable=True),
    )
    op.create_index('ix_crm_tag_name', 'crm_tag', ['name'], unique=True)

    op.create_table(
        'crm_entity_tag',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('entity', sa.String(20), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('tag_id', sa.Integer(), sa.ForeignKey('crm_tag.id', ondelete='CASCADE'), nullable=False),
    )
    op.create_index('ix_crm_entity_tag_ent', 'crm_entity_tag', ['entity', 'entity_id'])
    op.create_index('ix_crm_entity_tag_tag', 'crm_entity_tag', ['tag_id'])

    op.create_table(
        'crm_field_def',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('entity', sa.String(20), nullable=False),
        sa.Column('key', sa.String(40), nullable=False),
        sa.Column('label', sa.String(80), nullable=False),
        sa.Column('field_type', sa.String(20), nullable=False, server_default='text'),
        sa.Column('options', sa.Text(), nullable=True),
        sa.Column('sort_order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index('ix_crm_field_def_ent', 'crm_field_def', ['entity'])

    op.create_table(
        'crm_field_value',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('field_id', sa.Integer(), sa.ForeignKey('crm_field_def.id', ondelete='CASCADE'), nullable=False),
        sa.Column('entity', sa.String(20), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('value', sa.Text(), nullable=True),
    )
    op.create_index('ix_crm_field_value_field', 'crm_field_value', ['field_id'])
    op.create_index('ix_crm_field_value_ent', 'crm_field_value', ['entity', 'entity_id'])

    op.create_table(
        'crm_attachment',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('entity', sa.String(20), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('original_name', sa.String(255), nullable=False),
        sa.Column('stored_name', sa.String(80), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('content_type', sa.String(120), nullable=True),
        sa.Column('uploaded_by', sa.String(100), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True, server_default=sa.func.now()),
    )
    op.create_index('ix_crm_attachment_ent', 'crm_attachment', ['entity', 'entity_id'])


def downgrade() -> None:
    op.drop_table('crm_attachment')
    op.drop_table('crm_field_value')
    op.drop_table('crm_field_def')
    op.drop_table('crm_entity_tag')
    op.drop_table('crm_tag')
    op.drop_table('crm_saved_view')
