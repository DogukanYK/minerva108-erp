"""Satın Alma Planı — purchase_plans (kaydedilmiş senaryolar)

`/satin-alma` sayfasında kurulan planın (ürünler × adet + seçenekler)
adlandırılmış kaydı.  `config` = PlanRequest JSON'u ({"version": 1, …}).
Domain-kapsamlı; ad AKTİF kayıtlarda panel içinde tekil — kısmi tekil
indeks (`is_active` koşullu) bunu DB seviyesinde zorlar, yumuşak silinen
senaryonun adı yeniden kullanılabilir.  Uçlar routers/purchase_plan.py.
init_db() create_all bu tabloyu açılışta kurar (eklenen kolon yok,
alter_safe'e dokunmaya gerek yok); migration alembic geçmişi + temiz kurulum
içindir (deploy.sh alembic ÇALIŞTIRMIYOR).

Revision ID: b7d9f1a3c5e8
Revises: a3c5e7f9b1d4
Create Date: 2026-10-05 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7d9f1a3c5e8'
down_revision: Union[str, Sequence[str], None] = 'a3c5e7f9b1d4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'purchase_plans',
        sa.Column('id',            sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('name',          sa.String(150), nullable=False),
        sa.Column('config',        sa.Text(), nullable=False),
        sa.Column('domain',        sa.String(20), nullable=False, server_default='cosmetics'),
        sa.Column('created_by_id', sa.Integer(),
                  sa.ForeignKey('users.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_by',    sa.String(80), nullable=True),
        sa.Column('updated_by',    sa.String(80), nullable=True),
        sa.Column('created_at',    sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column('updated_at',    sa.DateTime(), nullable=True, server_default=sa.func.now()),
        sa.Column('last_run_at',   sa.DateTime(), nullable=True),
        sa.Column('is_active',     sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.create_index('ix_purchase_plans_id', 'purchase_plans', ['id'])
    op.create_index('ix_purchase_plans_domain', 'purchase_plans', ['domain'])
    op.create_index(
        'uq_purchase_plans_domain_name_active', 'purchase_plans', ['domain', 'name'],
        unique=True, postgresql_where=sa.text('is_active'),
    )


def downgrade() -> None:
    op.drop_index('uq_purchase_plans_domain_name_active', table_name='purchase_plans')
    op.drop_index('ix_purchase_plans_domain', table_name='purchase_plans')
    op.drop_index('ix_purchase_plans_id', table_name='purchase_plans')
    op.drop_table('purchase_plans')
