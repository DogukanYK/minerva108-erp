"""Tüm şablonlara /static/print.css linkini ekler (one-off, idempotent).

Kalıp: dark.css linkinin ALTINA aynı girintiyle (cascade sözleşmesi:
dark.css → print.css → sayfa-içi <style>). dark.css olmayan şablonlarda
(share.html, distributor_portal.html) </head> öncesine düşer.
"""
import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "templates"
LINK = '<link rel="stylesheet" href="/static/print.css" media="print" />'
DARK = re.compile(r'^([ \t]*)<link rel="stylesheet" href="/static/dark\.css"\s*/?>[ \t]*$', re.M)

changed, skipped = [], []
for f in sorted(TPL.glob("*.html")):
    text = f.read_text(encoding="utf-8")
    if "/static/print.css" in text:
        skipped.append(f.name)
        continue
    m = DARK.search(text)
    if m:
        new = text[:m.end()] + "\n" + m.group(1) + LINK + text[m.end():]
    else:
        i = text.find("</head>")
        if i == -1:
            continue
        new = text[:i] + "  " + LINK + "\n" + text[i:]
    f.write_text(new, encoding="utf-8")
    changed.append(f.name)

print("Eklendi :", ", ".join(changed) or "—")
print("Atlandı :", ", ".join(skipped) or "—")
