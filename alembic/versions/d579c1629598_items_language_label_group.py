"""items language + label_group — etiket dil ayrımı

Etiketlerin Türkçe + İngilizce iki fiziksel stoğu var.  Üretimde dil seçilince
doğru dildeki etiket stoktan düşmeli.  Bunun için iki kolon:

  • language     — 'TR' | 'EN' | NULL.  NULL = dilsiz (hammadde, kapak, pompa,
                    şişe, kavanoz, dil-nötr etiket).
  • label_group  — aynı mantıksal etiketin TR + EN üyelerini bağlayan anahtar.
                    Üretim, malzemenin diline bakar; seçili dilden farklıysa
                    aynı label_group'taki kardeş etikete iner.

Backfill (bu migration içinde):
  • category='Ambalaj' AND pkg_type='etiket' satırları taranır
  • İsimdeki '(ENG)'/'(EN)'/'İNG' → 'EN', '(TR)'/'(TUR)' → 'TR', yoksa NULL
  • label_group = isimden dil belirteçleri çıkarılıp normalize edilmiş hali
    → TR ve EN kardeşleri aynı normalize-isimde buluşur, aynı gruba düşer
  • Eki olmayan 37 etiket NULL dilde kalır — lab Ürünler sayfasından atar

Revision ID: d579c1629598
Revises: 8f7d494b619c
Create Date: 2026-05-15 11:24:38.557733
"""
import re
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd579c1629598'
down_revision: Union[str, Sequence[str], None] = '8f7d494b619c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Dil belirteçleri — isimden temizlenecek + dil tespiti için
_LANG_TOKEN_RE = re.compile(r'\(\s*(?:eng?|ing|tur|tr|t[üu]rk(?:[çc]e)?|english)\s*\)', re.IGNORECASE)


def _detect_language(name: str):
    """İsimden 'EN' / 'TR' / None döner."""
    up = (name or '').upper()
    if '(ENG)' in up or '(EN)' in up or 'İNG' in up or '(ING)' in up:
        return 'EN'
    if '(TR)' in up or '(TUR)' in up or 'TÜRK' in up:
        return 'TR'
    return None


def _group_key(name: str) -> str:
    """İsimden dil belirteçlerini at, normalize et → label_group anahtarı."""
    s = _LANG_TOKEN_RE.sub(' ', name or '')
    s = s.lower()
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def upgrade() -> None:
    # ── 1) Kolonları ekle ───────────────────────────────────────────────────
    op.add_column('items', sa.Column('language',    sa.String(8),   nullable=True))
    op.add_column('items', sa.Column('label_group', sa.String(255), nullable=True))
    op.create_index('ix_items_label_group', 'items', ['label_group'])

    # ── 2) Etiketleri backfill et ───────────────────────────────────────────
    conn = op.get_bind()
    rows = conn.execute(sa.text(
        "SELECT id, name FROM items "
        "WHERE category = 'Ambalaj' AND pkg_type = 'etiket'"
    )).fetchall()

    for row in rows:
        item_id, name = row[0], row[1]
        lang  = _detect_language(name)
        group = _group_key(name)
        conn.execute(
            sa.text("UPDATE items SET language = :lang, label_group = :grp WHERE id = :id"),
            {"lang": lang, "grp": group, "id": item_id},
        )


def downgrade() -> None:
    op.drop_index('ix_items_label_group', table_name='items')
    op.drop_column('items', 'label_group')
    op.drop_column('items', 'language')
