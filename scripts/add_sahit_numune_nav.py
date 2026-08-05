"""Tüm şablonların sidebar'ına 'Şahit Numune' nav linkini ekler (one-off, idempotent).

Kalıp: href="/numune-analiz" nav satırının ALTINA aynı girintiyle Şahit Numune linki.
Zaten href="/sahit-numune" içeren şablonlar atlanır (sahit_numune.html elle yazıldı).
Yeni bir şablon eklendiğinde tekrar çalıştırılabilir.
"""
import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "templates"
LINK = '<a href="/sahit-numune" class="nav-link-item"><i class="bi bi-archive nav-icon"></i>Şahit Numune</a>'
PAT = re.compile(r'^([ \t]*)<a href="/numune-analiz" class="nav-link-item[^"]*"[^\n]*</a>[ \t]*$', re.M)

changed, skipped = [], []
for f in sorted(TPL.glob("*.html")):
    text = f.read_text(encoding="utf-8")
    if 'href="/sahit-numune"' in text:
        skipped.append(f.name)
        continue
    m = PAT.search(text)
    if not m:
        continue                      # sidebar'ında Numune Analizi linki olmayan şablon
    indent = m.group(1)
    new = text[:m.end()] + "\n" + indent + LINK + text[m.end():]
    f.write_text(new, encoding="utf-8")
    changed.append(f.name)

print("Eklendi :", ", ".join(changed) or "—")
print("Atlandı :", ", ".join(skipped) or "—")
