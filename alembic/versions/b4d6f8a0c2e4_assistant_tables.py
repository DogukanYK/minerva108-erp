"""Minerva Asistan — assistant_conversations / assistant_messages / assistant_usage

IMS içi, yalnız okuma yapan Claude tabanlı soru-cevap asistanının
(core/assistant) kalıcı katmanı:
  * `assistant_conversations` — sohbet başlığı: sahibi, panel (`domain`),
    model, dondurulmuş sistem istemi hash'i, rotasyon zinciri (`parent_id`),
    tur / son istem token sayaçları.  Model/istem/panel değişince ya da eşik
    aşılınca eski sohbet DOKUNULMADAN yeni sohbet açılır.
  * `assistant_messages` — API'ye gönderilen/alınan içerik bloklarının BİREBİR
    JSON'u (`kind`: question | context | assistant | tool_result; thinking
    imzaları dahil).  Geçmiş yalnız eklenir, budanmaz.
  * `assistant_usage` — çağrı başına token/önbellek sayaçları ve dondurulmuş
    maliyet; aylık tavan, kişi başı günlük tavan ve /admin kullanım raporu
    buradan hesaplanır.  Sohbet/mesaj silinse de satır kalır (SET NULL).
Yetki kategorisi `assistant.use`'un override'lı kullanıcılara dağıtımı
`database._backfill_perm_assistant` (sentinel `backfill.perm.assistant.v1`)
ile init_db'de yapılır — bu dosyada veri yok.

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — tablolar prod'a `init_db()`
create_all ile gelir.  Bu dosya alembic geçmişi ve temiz kurulum içindir;
init_db'nin zaten kurduğu DB'de `alembic stamp head` kullan.
Head notu: önceki oturum notlarında "3 head" geçer; `a2c4e6081357` ve
`b8e2d4f6a1c3` dalları `b3d5f7092468` merge revizyonuyla kapanmış olduğundan
bu dosya yazılırken (12.10.2026) `alembic heads` TEK head (`a6c8e0b2d4f7`)
veriyordu; bu revizyon ona zincirlenir ve yeni tek head olur.

Revision ID: b4d6f8a0c2e4
Revises: a6c8e0b2d4f7
Create Date: 2026-10-12 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b4d6f8a0c2e4'
down_revision: Union[str, Sequence[str], None] = 'a6c8e0b2d4f7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'assistant_conversations',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('domain', sa.String(20), nullable=False, server_default='cosmetics'),
        sa.Column('model', sa.String(60), nullable=False),
        sa.Column('prompt_hash', sa.String(16), nullable=False),
        sa.Column('title', sa.String(120), nullable=True),
        sa.Column('parent_id', sa.Integer(),
                  sa.ForeignKey('assistant_conversations.id', ondelete='SET NULL'), nullable=True),
        sa.Column('turns', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('last_prompt_tokens', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('is_active', sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_assistant_conversations_user_id', 'assistant_conversations', ['user_id'])
    op.create_index('ix_assistant_conv_user_updated', 'assistant_conversations', ['user_id', 'updated_at'])

    op.create_table(
        'assistant_messages',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('conversation_id', sa.Integer(),
                  sa.ForeignKey('assistant_conversations.id', ondelete='CASCADE'), nullable=False),
        sa.Column('role', sa.String(12), nullable=False),      # user | assistant | system
        sa.Column('kind', sa.String(16), nullable=False),      # question | context | assistant | tool_result
        sa.Column('content_json', sa.Text(), nullable=False),  # API içerik blokları, birebir
        sa.Column('display_text', sa.Text(), nullable=True),
        sa.Column('tools_json', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_assistant_messages_conversation_id', 'assistant_messages', ['conversation_id'])

    op.create_table(
        'assistant_usage',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('conversation_id', sa.Integer(),
                  sa.ForeignKey('assistant_conversations.id', ondelete='SET NULL'), nullable=True),
        sa.Column('message_id', sa.Integer(),
                  sa.ForeignKey('assistant_messages.id', ondelete='SET NULL'), nullable=True),
        sa.Column('domain', sa.String(20), nullable=False),
        sa.Column('model', sa.String(60), nullable=False),
        sa.Column('round_no', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('input_tokens', sa.Integer(), nullable=True),
        sa.Column('cache_creation_input_tokens', sa.Integer(), nullable=True),
        sa.Column('cache_creation_1h_tokens', sa.Integer(), nullable=True),
        sa.Column('cache_read_input_tokens', sa.Integer(), nullable=True),
        sa.Column('output_tokens', sa.Integer(), nullable=True),
        sa.Column('cost_usd', sa.Float(), nullable=False, server_default='0'),
        sa.Column('tool_calls_json', sa.Text(), nullable=True),
        sa.Column('latency_ms', sa.Integer(), nullable=True),
        sa.Column('stop_reason', sa.String(30), nullable=True),
        sa.Column('request_id', sa.String(80), nullable=True),
        sa.Column('question_preview', sa.String(160), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
    )
    op.create_index('ix_assistant_usage_created', 'assistant_usage', ['created_at'])
    op.create_index('ix_assistant_usage_user_created', 'assistant_usage', ['user_id', 'created_at'])


def downgrade() -> None:
    op.drop_index('ix_assistant_usage_user_created', table_name='assistant_usage')
    op.drop_index('ix_assistant_usage_created', table_name='assistant_usage')
    op.drop_table('assistant_usage')
    op.drop_index('ix_assistant_messages_conversation_id', table_name='assistant_messages')
    op.drop_table('assistant_messages')
    op.drop_index('ix_assistant_conv_user_updated', table_name='assistant_conversations')
    op.drop_index('ix_assistant_conversations_user_id', table_name='assistant_conversations')
    op.drop_table('assistant_conversations')
