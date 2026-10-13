"""Sol menüye "Yapay Zekâ" bölümünü ekler: Asistan · Belge Oku · Metin Yaz (13.10.2026).

İdempotent (şablonda `href="/asistan"` varsa atlanır).  Yalnız Minerva
Asistan'ı yükleyen (`/static/assistant.js`) ve sol menüsü olan şablonlara;
`templates/asistan.html` kendi bölümünü (etkin bağlantıyla) taşır.  Blok
`can('assistant','use')` kapılıdır — yetkisi kapatılan kullanıcı bölümü görmez.

Yer: "Sistem" bölüm başlığının hemen ÜSTÜ (Ana Menü ile Sistem arası); şablonda
"Sistem" başlığı yoksa menünün (`</nav>`) sonu.  `.nav-section-label` CSS'i
tanımlamayan şablonlarda başlık aynı görünümü satır içi stille alır.

Çalıştır:  .venv/bin/python scripts/add_ai_nav.py
"""
import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "templates"
SKIP = {"asistan.html"}
MARK = 'href="/asistan"'
LABEL_STYLE = "margin-top:1rem;"
LABEL_STYLE_FULL = ("margin-top:1rem;font-size:.65rem;font-weight:600;letter-spacing:.14em;"
                    "text-transform:uppercase;color:rgba(255,255,255,.3);padding:.5rem .75rem .4rem;")
LINKS = [
    ("/asistan", "bi-chat-left-text", "Asistan"),
    ("/asistan/belge", "bi-file-earmark-text", "Belge Oku"),
    ("/asistan/yaz", "bi-pencil-square", "Metin Yaz"),
]
SISTEM = re.compile(r'^([ \t]*)<div class="nav-section-label"[^>]*>Sistem</div>[ \t]*$', re.M)
JINJA_IF = re.compile(r'^[ \t]*\{%-?\s*if\b[^%]*%\}[ \t]*$')
NAV_LINK = re.compile(r'^([ \t]*)(?:\{%[^%]*%\})?<a href="[^"]*" class="nav-link-item', re.M)


def block(indent: str, styled: bool) -> str:
    style = LABEL_STYLE if styled else LABEL_STYLE_FULL
    lines = ["{% if can('assistant','use') %}"
             f'<div class="nav-section-label" style="{style}">Yapay Zekâ</div>']
    for href, icon, label in LINKS:
        lines.append(f'<a href="{href}" class="nav-link-item"><i class="bi {icon} nav-icon"></i>{label}</a>')
    lines[-1] += "{% endif %}"
    return "\n".join(indent + ln for ln in lines)


def place(text: str):
    """(yeni metin, değişti mi)."""
    if MARK in text or 'class="sidebar' not in text or "/static/assistant.js" not in text:
        return text, False
    styled = ".nav-section-label" in text
    m = SISTEM.search(text)
    if m:
        at = m.start()
        # "Sistem" bir yetki bloğunun içindeyse ({% if can('admin',…) %} tek
        # satırda, hemen üstte) bölüm o bloğun ÜSTÜNE gelir — yoksa yalnız
        # yöneticiler görürdü (crm.html).
        prev_start = text.rfind("\n", 0, max(at - 1, 0)) + 1
        if JINJA_IF.match(text[prev_start:at - 1] if at else ""):
            at = prev_start
        return text[:at] + block(m.group(1), styled) + "\n" + text[at:], True
    side = text.index('class="sidebar')
    end = text.find("</nav>", side)
    if end == -1:
        return text, False
    links = list(NAV_LINK.finditer(text, side, end))
    indent = links[-1].group(1) if links else "      "
    line_start = text.rfind("\n", 0, end) + 1
    return text[:line_start] + block(indent, styled) + "\n" + text[line_start:], True


def main() -> None:
    changed = []
    for path in sorted(TPL.glob("*.html")):
        if path.name in SKIP:
            continue
        text = path.read_text(encoding="utf-8")
        new, did = place(text)
        if did:
            path.write_text(new, encoding="utf-8")
            changed.append(path.name)
    print("Yapay Zekâ bölümü eklendi:", ", ".join(changed) or "yok")
    print("Toplam:", len(changed), "şablon")


if __name__ == "__main__":
    main()
