"""Tüm şablonların sidebar'ına 'Numune Analizi' nav linkini ekler (one-off, idempotent).

Kalıp: href="/returns" nav satırının ALTINA aynı girintiyle Numune Analizi linki.
Zaten href="/numune-analiz" içeren şablonlar atlanır (numune_analiz.html elle yazıldı).
"""
import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "templates"
LINK = '<a href="/numune-analiz" class="nav-link-item"><i class="bi bi-clipboard2-pulse nav-icon"></i>Numune Analizi</a>'
PAT = re.compile(r'^([ \t]*)<a href="/returns" class="nav-link-item[^"]*"[^\n]*</a>[ \t]*$', re.M)

changed, skipped = [], []
for f in sorted(TPL.glob("*.html")):
    text = f.read_text(encoding="utf-8")
    if 'href="/numune-analiz"' in text:
        skipped.append(f.name)
        continue
    m = PAT.search(text)
    if not m:
        continue                      # sidebar'ında İadeler linki olmayan şablon (login, share…)
    indent = m.group(1)
    new = text[:m.end()] + "\n" + indent + LINK + text[m.end():]
    f.write_text(new, encoding="utf-8")
    changed.append(f.name)

print("Eklendi :", ", ".join(changed) or "—")
print("Atlandı :", ", ".join(skipped) or "—")
