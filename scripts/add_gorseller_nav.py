"""Tüm şablonların sidebar'ına 'Ürün Görselleri' nav linkini ekler (idempotent).

Kalıp: href="/sahit-numune" nav satırının ALTINA aynı girintiyle.
"""
import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "templates"
LINK = '<a href="/urun-gorselleri" class="nav-link-item"><i class="bi bi-images nav-icon"></i>Ürün Görselleri</a>'
PAT = re.compile(r'^([ \t]*)<a href="/sahit-numune" class="nav-link-item[^"]*"[^\n]*</a>[ \t]*$', re.M)

changed, skipped = [], []
for f in sorted(TPL.glob("*.html")):
    text = f.read_text(encoding="utf-8")
    if 'href="/urun-gorselleri"' in text:
        skipped.append(f.name); continue
    m = PAT.search(text)
    if not m:
        continue
    new = text[:m.end()] + "\n" + m.group(1) + LINK + text[m.end():]
    f.write_text(new, encoding="utf-8")
    changed.append(f.name)

print("Eklendi :", ", ".join(changed) or "—")
print("Atlandı :", ", ".join(skipped) or "—")
