"""PDKS — personelin izin/rapor bildirimi (yönetici onaylı) + belge no

pdks_leave_requests: hastalanan personel evden "rapor aldım" der (tür, tarih
aralığı, gerekçe, e-rapor no); yönetici onaylayınca gerçek LeaveRecord yazılır.
Onaysız hiçbir puantaj etkisi yoktur.
pdks_leaves.document_no: e-rapor / istirahat belgesi numarası — muhasebecinin
SGK listesiyle eşleştirdiği anahtar.

DİKKAT: deploy.sh alembic ÇALIŞTIRMAZ — tablo create_all ile, document_no
kolonu ise init_db() alter_safe satırıyla prod'a ulaşır.  Bu migration
alembic geçmişi + temiz kurulum içindir.

Revision ID: e1a3c5b7d9f2
Revises: c7e9b1d3f5a2
Create Date: 2026-08-12 11:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e1a3c5b7d9f2'
down_revision: Union[str, Sequence[str], None] = 'c7e9b1d3f5a2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('pdks_leaves', sa.Column('document_no', sa.String(60), nullable=True))
    op.create_table(
        'pdks_leave_requests',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('employee_id',   sa.Integer(), sa.ForeignKey('pdks_employees.id'), nullable=False),
        sa.Column('leave_type',    sa.String(20), nullable=False),
        sa.Column('start_date',    sa.Date(), nullable=False),
        sa.Column('end_date',      sa.Date(), nullable=False),
        sa.Column('note',          sa.String(300), nullable=False),
        sa.Column('document_no',   sa.String(60), nullable=True),
        sa.Column('status',        sa.String(12), nullable=False, server_default='pending'),
        sa.Column('created_at',    sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column('decided_by',    sa.String(100), nullable=True),
        sa.Column('decided_at',    sa.DateTime(), nullable=True),
        sa.Column('decision_note', sa.String(300), nullable=True),
        sa.Column('leave_id',      sa.Integer(), sa.ForeignKey('pdks_leaves.id'), nullable=True),
    )
    op.create_index('ix_pdks_leave_requests_employee_id', 'pdks_leave_requests', ['employee_id'])
    op.create_index('ix_pdks_leave_requests_start_date', 'pdks_leave_requests', ['start_date'])
    op.create_index('ix_pdks_leave_requests_status', 'pdks_leave_requests', ['status'])


def downgrade() -> None:
    op.drop_index('ix_pdks_leave_requests_status', table_name='pdks_leave_requests')
    op.drop_index('ix_pdks_leave_requests_start_date', table_name='pdks_leave_requests')
    op.drop_index('ix_pdks_leave_requests_employee_id', table_name='pdks_leave_requests')
    op.drop_table('pdks_leave_requests')
    op.drop_column('pdks_leaves', 'document_no')
