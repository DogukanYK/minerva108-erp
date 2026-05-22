"""system_event table — uygulama açılış (restart) defteri

Uygulama her başladığında buraya bir 'app_start' satırı düşer
(bkz. database.log_system_event + api_main startup).  Aylık detaylı sistem
raporu, bir ay içindeki restart/deploy sayısını ve zamanlarını bu tablodan
çıkarır; her restart ~3-4 sn kesinti demek olduğu için tahmini downtime de
buradan hesaplanır.

Not: uygulama kendi *kapanışını* güvenilir yazamaz (süreç öldürülür), bu
yüzden yalnızca 'açılış' olayı tutulur.  init_db() create_all ile de bu
tabloyu oluşturur — migration alembic geçmişi tutarlılığı içindir.

Revision ID: b2e1c7f4a9d3
Revises: 8954ef5ca750
Create Date: 2026-05-22 16:10:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b2e1c7f4a9d3'
down_revision: Union[str, Sequence[str], None] = '8954ef5ca750'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'system_event',
        sa.Column('id',         sa.Integer(),   primary_key=True, autoincrement=True),
        sa.Column('event_type', sa.String(40),  nullable=False),
        sa.Column('detail',     sa.String(255), nullable=True),
        sa.Column('created_at', sa.DateTime(),  nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_system_event_event_type', 'system_event', ['event_type'])
    op.create_index('ix_system_event_created_at', 'system_event', ['created_at'])


def downgrade() -> None:
    op.drop_index('ix_system_event_created_at', table_name='system_event')
    op.drop_index('ix_system_event_event_type', table_name='system_event')
    op.drop_table('system_event')
