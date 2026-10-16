"""Tüm şablonların sidebar'ına 'Malzeme Fiyatları' nav linkini ekler (one-off, idempotent).

Kalıp: "Satın Alma" (`/satin-alma`) nav linkinin ALTINA aynı girintiyle,
`reports.view` yetkisine kapılı tek satır (sayfanın kendi kapısıyla aynı —
`/fiyatlar`, api_main.fiyatlar_page).  Satın Alma linki her şablonda
`scripts/add_satin_alma_nav.py`'nin yazdığı tek satırdır; çok satırlı yazım da
yakalanır.  Malzeme Fiyatları NAV linki zaten olan şablonlar atlanır
(fiyatlar.html elle yazıldı; Raporlar'daki sayfa içi "Malzeme Fiyatları →"
bağlantısı nav linki sayılmaz).  Satın Alma nav linki olmayan şablon
(login, share, crm, asistan…) değişmez.  Yeni bir şablon eklendiğinde
tekrar çalıştırılabilir (önce add_satin_alma_nav.py).

    .venv/bin/python scripts/add_fiyatlar_nav.py
"""
import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "templates"
LINK = ("{% if can('reports','view') %}<a href=\"/fiyatlar\" class=\"nav-link-item\">"
        "<i class=\"bi bi-tags nav-icon\"></i>Malzeme Fiyatları</a>{% endif %}")
NAV_MARK = re.compile(r'<a href="/fiyatlar"\s+class="nav-link-item')
# Satın Alma nav satırının tamamı: (varsa) {% if %} kapısı + <a …nav-link-item…>…</a> + (varsa) {% endif %}
PAT = re.compile(r'^([ \t]*)(?:\{%\s*if[^%]*%\}\s*)?<a href="/satin-alma"\s+class="nav-link-item[^"]*"[^>]*>'
                 r'.*?</a>(?:\s*\{%\s*endif\s*%\})?[ \t]*$', re.M | re.S)


def main() -> None:
    changed, skipped = [], []
    for f in sorted(TPL.glob("*.html")):
        text = f.read_text(encoding="utf-8")
        if NAV_MARK.search(text):
            skipped.append(f.name)
            continue
        m = PAT.search(text)
        if not m:
            continue                  # sidebar'ında Satın Alma linki olmayan şablon
        indent = m.group(1)
        new = text[:m.end()] + "\n" + indent + LINK + text[m.end():]
        f.write_text(new, encoding="utf-8")
        changed.append(f.name)
    print("Eklendi :", ", ".join(changed) or "—")
    print("Atlandı :", ", ".join(skipped) or "—")


if __name__ == "__main__":
    main()
