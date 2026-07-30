"""PDKS — Personel Devam Takip Sistemi tabloları

pdks_employees (personel, opsiyonel users bağı) · pdks_schedules (versiyonlu
haftalık program) · pdks_events (giriş/çıkış olayları, TR-yerel work_date) ·
pdks_leaves (izin aralıkları) · pdks_holidays (ortak resmi tatiller).
Cross-cutting modül — domain kolonu YOK (CRM/Drive emsali).
init_db() create_all bu tabloları açılışta kurar; migration alembic geçmişi +
temiz kurulum içindir.

Revision ID: f3a5c7e9b2d4
Revises: b7c9d1e3f5a7
Create Date: 2026-07-30 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3a5c7e9b2d4'
down_revision: Union[str, Sequence[str], None] = 'b7c9d1e3f5a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'pdks_employees',
        sa.Column('id',         sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('user_id',    sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('full_name',  sa.String(150), nullable=False),
        sa.Column('title',      sa.String(100), nullable=True),
        sa.Column('start_date', sa.Date(), nullable=True),
        sa.Column('notes',      sa.Text(), nullable=True),
        sa.Column('is_active',  sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_pdks_employees_user_id', 'pdks_employees', ['user_id'], unique=True)

    op.create_table(
        'pdks_schedules',
        sa.Column('id',                  sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('employee_id',         sa.Integer(), sa.ForeignKey('pdks_employees.id'), nullable=False),
        sa.Column('effective_from',      sa.Date(), nullable=False),
        sa.Column('weekly_template',     sa.Text(), nullable=False),
        sa.Column('lunch_break_minutes', sa.Integer(), nullable=False, server_default='60'),
        sa.Column('created_by',          sa.String(100), nullable=True),
        sa.Column('created_at',          sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('employee_id', 'effective_from', name='uq_pdks_schedule_emp_from'),
    )
    op.create_index('ix_pdks_schedules_employee_id', 'pdks_schedules', ['employee_id'])

    op.create_table(
        'pdks_events',
        sa.Column('id',                 sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('employee_id',        sa.Integer(), sa.ForeignKey('pdks_employees.id'), nullable=False),
        sa.Column('event_type',         sa.String(10), nullable=False),
        sa.Column('ts_utc',             sa.DateTime(), nullable=False),
        sa.Column('work_date',          sa.Date(), nullable=False),
        sa.Column('source',             sa.String(20), nullable=False, server_default='self'),
        sa.Column('created_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
        sa.Column('ip_address',         sa.String(64), nullable=True),
        sa.Column('corrected_by',       sa.String(100), nullable=True),
        sa.Column('corrected_at',       sa.DateTime(), nullable=True),
        sa.Column('correction_note',    sa.String(300), nullable=True),
        sa.Column('is_active',          sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at',         sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_pdks_events_employee_id', 'pdks_events', ['employee_id'])
    op.create_index('ix_pdks_events_work_date', 'pdks_events', ['work_date'])
    op.create_index('ix_pdks_events_emp_date', 'pdks_events', ['employee_id', 'work_date'])

    op.create_table(
        'pdks_leaves',
        sa.Column('id',          sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('employee_id', sa.Integer(), sa.ForeignKey('pdks_employees.id'), nullable=False),
        sa.Column('leave_type',  sa.String(20), nullable=False),
        sa.Column('start_date',  sa.Date(), nullable=False),
        sa.Column('end_date',    sa.Date(), nullable=False),
        sa.Column('note',        sa.String(300), nullable=True),
        sa.Column('created_by',  sa.String(100), nullable=True),
        sa.Column('is_active',   sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at',  sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_pdks_leaves_employee_id', 'pdks_leaves', ['employee_id'])

    op.create_table(
        'pdks_holidays',
        sa.Column('id',           sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('holiday_date', sa.Date(), nullable=False),
        sa.Column('name',         sa.String(150), nullable=False),
        sa.Column('is_half_day',  sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('created_by',   sa.String(100), nullable=True),
        sa.Column('is_active',    sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column('created_at',   sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.create_index('ix_pdks_holidays_date', 'pdks_holidays', ['holiday_date'])


def downgrade() -> None:
    op.drop_index('ix_pdks_holidays_date', table_name='pdks_holidays')
    op.drop_table('pdks_holidays')
    op.drop_index('ix_pdks_leaves_employee_id', table_name='pdks_leaves')
    op.drop_table('pdks_leaves')
    op.drop_index('ix_pdks_events_emp_date', table_name='pdks_events')
    op.drop_index('ix_pdks_events_work_date', table_name='pdks_events')
    op.drop_index('ix_pdks_events_employee_id', table_name='pdks_events')
    op.drop_table('pdks_events')
    op.drop_index('ix_pdks_schedules_employee_id', table_name='pdks_schedules')
    op.drop_table('pdks_schedules')
    op.drop_index('ix_pdks_employees_user_id', table_name='pdks_employees')
    op.drop_table('pdks_employees')
