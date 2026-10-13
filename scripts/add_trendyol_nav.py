"""Add permission-gated "Trendyol" link to internal sidebar templates.

İdempotent.  Link, "Satın Alma" (/satin-alma) bağlantısının hemen altına —
o yoksa Raporlar (/reports) bağlantısının altına — aynı girintiyle eklenir ve
`trendyol.view` ile sarılır (sayfanın kendi kapısıyla aynı).  Zaten olan
şablona dokunulmaz; trendyol.html kendi (active) linkini taşır.  Sidebar'ı
olmayan şablonlar ve kendine özgü düzeni olan CRM atlanır.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK = ("{% if can('trendyol','view') %}<a href=\"/trendyol\" class=\"nav-link-item\">"
        "<i class=\"bi bi-shop-window nav-icon\"></i>Trendyol</a>{% endif %}")
OPT = r"(?:\{%[^%]*%\})?"
ANCHORS = [
    re.compile(r"^([ \t]*)" + OPT + r'<a href="/satin-alma"\s*class="nav-link-item[^"]*"[^>]*>.*?</a>'
               + OPT + r"[ \t]*$", re.M),
    re.compile(r"^([ \t]*)" + OPT + r'<a href="/reports"\s*class="nav-link-item[^"]*"[^>]*>.*?</a>'
               + OPT + r"[ \t]*$", re.M),
]
SKIP = {"trendyol.html", "crm.html"}


def place(text):
    """(yeni metin, değişti mi)."""
    if 'href="/trendyol"' in text:
        return text, False
    for anchor in ANCHORS:
        match = anchor.search(text)
        if match:
            new = text[:match.end()] + "\n" + match.group(1) + LINK + text[match.end():]
            return new, True
    return text, False


def main():
    changed, missed = [], []
    for path in sorted((ROOT / "templates").glob("*.html")):
        text = path.read_text(encoding="utf-8")
        if 'class="sidebar' not in text or path.name in SKIP:
            continue
        new, did = place(text)
        if did:
            path.write_text(new, encoding="utf-8")
            changed.append(path.name)
        elif 'href="/trendyol"' not in text:
            missed.append(path.name)
    print("Trendyol bağlantısı eklendi:", ", ".join(changed) or "yok")
    if missed:
        print("Bağlantı yeri bulunamadı (elle bakın):", ", ".join(missed))


if __name__ == "__main__":
    main()
