"""Tedarikçi satın alma durumu + elle fiyatın kişisi

`suppliers`: `purchase_status` (normal | preferred | phase_out — lab işaretler,
core/suppliers.STATUSES), `status_reason` (phase_out'ta zorunlu), `status_by`,
`status_at`.  `supplier_prices`: `created_by` / `updated_by` — lab'ın elle
girdiği (`source='manual'`) ya da düzenlediği satırın kişisi; içe aktarılan
satırda boş kalır.

DİKKAT: `deploy.sh` alembic ÇALIŞTIRMAZ — prod şeması `init_db()` alter_safe
bloğuyla evrilir; aynı kolonlar orada da eklenir.  Override'lı kullanıcılara
yeni `suppliers.*` yetkilerinin yazılması `_backfill_perm_suppliers()`
(AppSetting sentinel 'backfill.perm.suppliers.v1') ile bir kez yapılır — burada
veri yazılmaz.  Bu dosya alembic geçmişi ve temiz kurulum içindir; kolonları
init_db zaten eklemiş bir DB'de `alembic stamp head` kullan.

Revision ID: e7b9d1f3a5c8
Revises: d6a8c0e2f4b7
Create Date: 2026-10-07 20:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e7b9d1f3a5c8'
down_revision: Union[str, Sequence[str], None] = 'd6a8c0e2f4b7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('suppliers', sa.Column('purchase_status', sa.String(16), nullable=False,
                                         server_default='normal'))
    op.add_column('suppliers', sa.Column('status_reason', sa.Text(), nullable=True))
    op.add_column('suppliers', sa.Column('status_by', sa.String(100), nullable=True))
    op.add_column('suppliers', sa.Column('status_at', sa.DateTime(), nullable=True))
    op.add_column('supplier_prices', sa.Column('created_by', sa.String(100), nullable=True))
    op.add_column('supplier_prices', sa.Column('updated_by', sa.String(100), nullable=True))


def downgrade() -> None:
    op.drop_column('supplier_prices', 'updated_by')
    op.drop_column('supplier_prices', 'created_by')
    op.drop_column('suppliers', 'status_at')
    op.drop_column('suppliers', 'status_by')
    op.drop_column('suppliers', 'status_reason')
    op.drop_column('suppliers', 'purchase_status')
