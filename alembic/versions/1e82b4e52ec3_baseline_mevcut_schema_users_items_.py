"""baseline: mevcut schema (noop)

Bu, projenin Alembic'e geçtiği andaki "her şey nasılsa öyle kabul et"
baseline'ıdır.  Mevcut tablolar/indeksler/kolonlar `database.py`'deki
SQLAlchemy modellerinden ve `init_db()`'deki idempotent ALTER listesinden
elle oluşturulmuştu.  Alembic bu durumu görmüş gibi davranır:

  $ alembic stamp head    # ← mevcut DB'leri (dev + prod) bu revision'da işle

Buradan SONRAKİ migration'lar normal `alembic revision --autogenerate`
ile üretilir; sadece DIFF içerirler.

upgrade() ve downgrade() boş — autogenerate bazı diff'ler tespit etti
(eski packaging_cost/ingredient_cost kolonları, elle yaratılmış indeksler)
ama bunlar mevcut durumla uyumlu; otomatik silmek istemiyoruz.

Gelecekteki migration'lar:
  $ alembic revision --autogenerate -m "açıklayıcı_isim"
  $ alembic upgrade head

Revision ID: 1e82b4e52ec3
Revises:
Create Date: 2026-05-11 06:26:54.050704

"""
from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = '1e82b4e52ec3'
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Baseline — mevcut şema zaten DB'de var.  Hiçbir işlem yapma."""
    pass


def downgrade() -> None:
    """Baseline'ın altına geri dönüş yok (sıfırdan başlamak demek)."""
    pass
