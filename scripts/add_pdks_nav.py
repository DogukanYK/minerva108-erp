"""Tüm şablonların sidebar'ına 'PDKS' nav linkini ekler (one-off, idempotent).

Kalıp: href="/numune-analiz" nav satırının ALTINA aynı girintiyle PDKS linki.
Zaten href="/pdks" içeren şablonlar atlanır (pdks.html elle yazıldı).
"""
import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "templates"
LINK = '<a href="/pdks" class="nav-link-item"><i class="bi bi-fingerprint nav-icon"></i>PDKS</a>'
PAT = re.compile(r'^([ \t]*)<a href="/numune-analiz" class="nav-link-item[^"]*"[^\n]*</a>[ \t]*$', re.M)

changed, skipped = [], []
for f in sorted(TPL.glob("*.html")):
    text = f.read_text(encoding="utf-8")
    if 'href="/pdks"' in text:
        skipped.append(f.name)
        continue
    m = PAT.search(text)
    if not m:
        continue                      # sidebar'ında Numune Analizi linki olmayan şablon (login, share…)
    indent = m.group(1)
    new = text[:m.end()] + "\n" + indent + LINK + text[m.end():]
    f.write_text(new, encoding="utf-8")
    changed.append(f.name)

print("Eklendi :", ", ".join(changed) or "—")
print("Atlandı :", ", ".join(skipped) or "—")
