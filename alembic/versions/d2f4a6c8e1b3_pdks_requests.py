"""PDKS — personelin unuttuğu giriş/çıkış bildirimi (yönetici onaylı)

pdks_requests: personel "çıkış yapmayı unuttum" der, saat + gerekçe bildirir;
yönetici onaylayınca gerçek AttendanceEvent (source='request') yazılır.
Onaysız hiçbir puantaj etkisi yoktur — doğrulama (ofis ağı + konum + QR)
zayıflamaz, yalnız düzeltme akışı personelden başlatılabilir olur.
init_db() create_all bu tabloyu açılışta kurar; migration alembic geçmişi +
temiz kurulum içindir.

Revision ID: d2f4a6c8e1b3
Revises: c5e7a9b1d3f8
Create Date: 2026-08-06 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd2f4a6c8e1b3'
down_revision: Union[str, Sequence[str], None] = 'c5e7a9b1d3f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'pdks_requests',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('employee_id',   sa.Integer(), sa.ForeignKey('pdks_employees.id'), nullable=False),
        sa.Column('event_type',    sa.String(10), nullable=False),
        sa.Column('ts_utc',        sa.DateTime(), nullable=False),
        sa.Column('work_date',     sa.Date(), nullable=False),
        sa.Column('note',          sa.String(300), nullable=False),
        sa.Column('status',        sa.String(12), nullable=False, server_default='pending'),
        sa.Column('created_at',    sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('decided_by',    sa.String(100), nullable=True),
        sa.Column('decided_at',    sa.DateTime(), nullable=True),
        sa.Column('decision_note', sa.String(300), nullable=True),
        sa.Column('event_id',      sa.Integer(), sa.ForeignKey('pdks_events.id'), nullable=True),
    )
    op.create_index('ix_pdks_requests_employee_id', 'pdks_requests', ['employee_id'])
    op.create_index('ix_pdks_requests_work_date', 'pdks_requests', ['work_date'])
    op.create_index('ix_pdks_requests_status', 'pdks_requests', ['status'])


def downgrade() -> None:
    op.drop_index('ix_pdks_requests_status', table_name='pdks_requests')
    op.drop_index('ix_pdks_requests_work_date', table_name='pdks_requests')
    op.drop_index('ix_pdks_requests_employee_id', table_name='pdks_requests')
    op.drop_table('pdks_requests')
