"""Tüm şablonların sidebar'ına 'Satın Alma' nav linkini ekler (one-off, idempotent).

Kalıp: href="/reports" (Raporlar) nav linkinin ALTINA aynı girintiyle,
`reports.view` yetkisine kapılı tek satır (sayfanın kendi kapısıyla aynı).
Raporlar linki bazı şablonlarda tek satır, bazılarında (index/items/qc/
suppliers) çok satırlı — ikisi de yakalanır; eklenen satır önceki nav
script'leri gibi tek satırdır.  Satın Alma NAV linki zaten olan şablonlar
atlanır (satin_alma.html elle yazıldı; Raporlar'daki sayfa içi bağlantı
sayılmaz).  Yeni bir şablon eklendiğinde tekrar
çalıştırılabilir.

    .venv/bin/python scripts/add_satin_alma_nav.py
"""
import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "templates"
LINK = ("{% if can('reports','view') %}<a href=\"/satin-alma\" class=\"nav-link-item\">"
        "<i class=\"bi bi-cart-check nav-icon\"></i>Satın Alma</a>{% endif %}")
NAV_MARK = re.compile(r'<a href="/satin-alma"\s+class="nav-link-item')
# Raporlar linkinin tamamı (çok satırlı olabilir): açılıştan İLK </a>'ya kadar
PAT = re.compile(r'^([ \t]*)<a href="/reports"\s+class="nav-link-item[^"]*"[^>]*>.*?</a>[ \t]*$', re.M | re.S)

changed, skipped = [], []
for f in sorted(TPL.glob("*.html")):
    text = f.read_text(encoding="utf-8")
    # Nav linkinin kendisine bak — sayfa içi bağlantı (Raporlar'daki "Gelişmiş:
    # Satın Alma Planı →") şablonu atlatmasın.
    if NAV_MARK.search(text):
        skipped.append(f.name)
        continue
    m = PAT.search(text)
    if not m:
        continue                      # sidebar'ında Raporlar linki olmayan şablon (login, share, crm…)
    indent = m.group(1)
    new = text[:m.end()] + "\n" + indent + LINK + text[m.end():]
    f.write_text(new, encoding="utf-8")
    changed.append(f.name)

print("Eklendi :", ", ".join(changed) or "—")
print("Atlandı :", ", ".join(skipped) or "—")
