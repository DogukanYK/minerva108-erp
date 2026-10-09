"""Add permission-gated "B2B Siparişleri" link to internal sidebar templates.

İdempotent.  Link, teknik kullanıcının da gördüğü bölüme — Fason Üretim
bağlantısının, o yoksa Üretim (/production, tek ya da çok satırlı)
bağlantısının hemen altına eklenir.  B2B Teklif bağlantısı `b2b.view`
bloğunun İÇİNDE durduğu için oraya eklemek Songül gibi fiyat görmeyen
kullanıcıdan linki saklardı — eski bir koşudan kalan öyle satırlar önce
kaldırılır, sonra doğru yere eklenir.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LINK = ("{% if can('b2b_orders','view') %}<a href=\"/b2b-siparisler\" class=\"nav-link-item\">"
        "<i class=\"bi bi-clipboard2-check nav-icon\"></i>B2B Siparişleri</a>{% endif %}")
OPT = r"(?:\{%[^%]*%\})?"
ANCHORS = [
    re.compile(r"^([ \t]*)" + OPT + r'<a href="/outsourcing"\s*class="nav-link-item[^"]*"[^>]*>.*?</a>'
               + OPT + r"[ \t]*$", re.M | re.S),
    re.compile(r"^([ \t]*)" + OPT + r'<a href="/production"\s*class="nav-link-item[^"]*"[^>]*>.*?</a>'
               + OPT + r"[ \t]*$", re.M | re.S),
]


def place(text):
    """(yeni metin, değişti mi) — link doğru yerde değilse taşı."""
    lines = text.split("\n")
    kept = [ln for ln in lines if ln.strip() != LINK]
    base = "\n".join(kept)
    for anchor in ANCHORS:
        match = anchor.search(base)
        if match:
            new = base[:match.end()] + "\n" + match.group(1) + LINK + base[match.end():]
            return new, new != text
    return text, False


def main():
    changed = []
    for path in sorted((ROOT / "templates").glob("*.html")):
        text = path.read_text()
        if 'class="sidebar' not in text or path.name == "b2b_siparisler.html":
            continue
        new, did = place(text)
        if did:
            path.write_text(new)
            changed.append(path.name)
    print("B2B Siparişleri bağlantısı eklendi/taşındı:", ", ".join(changed) or "yok")


if __name__ == "__main__":
    main()
