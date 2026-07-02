"""Tüm şablonların sidebar'ına 'İadeler' nav linkini ekler (one-off, idempotent).

Kalıp: href="/delivery" nav satırının ALTINA aynı girintiyle İadeler linki.
Zaten href="/returns" içeren şablonlar atlanır (returns.html elle yazıldı).
"""
import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "templates"
LINK = '<a href="/returns" class="nav-link-item"><i class="bi bi-arrow-return-left nav-icon"></i>İadeler</a>'
PAT = re.compile(r'^([ \t]*)<a href="/delivery" class="nav-link-item[^"]*"[^\n]*</a>[ \t]*$', re.M)

changed, skipped = [], []
for f in sorted(TPL.glob("*.html")):
    text = f.read_text(encoding="utf-8")
    if 'href="/returns"' in text:
        skipped.append(f.name)
        continue
    m = PAT.search(text)
    if not m:
        continue                      # sidebar'ında Teslimat linki olmayan şablon (login, share…)
    indent = m.group(1)
    new = text[:m.end()] + "\n" + indent + LINK + text[m.end():]
    f.write_text(new, encoding="utf-8")
    changed.append(f.name)

print("Eklendi :", ", ".join(changed) or "—")
print("Atlandı :", ", ".join(skipped) or "—")
