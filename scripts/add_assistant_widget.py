"""Tüm iç şablonlara Minerva Asistan widget'ını ekler (one-off, idempotent).

Kalıp: `/static/idle-watch.js` script satırının ALTINA aynı girintiyle
`/static/assistant.js` satırı.  idle-watch yalnız oturumlu iç sayfalarda
yüklü (login, /s/ paylaşım, yorum, başvuru ve kiosk sayfalarında yok) —
bu yüzden widget de tam o kümeye gider.  İstisna: `distributor_portal.html`
(bayi portalı; bayi `require_internal_user`'dan 403 alır, widget'ın orada işi
yok).  Zaten `/static/assistant.js` içeren şablonlar atlanır.

Çalıştır:  .venv/bin/python scripts/add_assistant_widget.py   (28 şablon beklenir)
Sonra:     static/sw.js CACHE_NAME +1 (eski önbellekteki sayfalar yeni script'i görsün).
"""
import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "templates"
LINE = '<script src="/static/assistant.js?v=4" defer></script>'
PAT = re.compile(r'^([ \t]*)<script src="/static/idle-watch\.js" defer></script>[ \t]*$', re.M)
SKIP = {"distributor_portal.html"}


def main() -> None:
    changed, skipped = [], []
    for f in sorted(TPL.glob("*.html")):
        if f.name in SKIP:
            skipped.append(f.name + " (bayi portalı)")
            continue
        text = f.read_text(encoding="utf-8")
        if "/static/assistant.js" in text:
            skipped.append(f.name)
            continue
        m = PAT.search(text)
        if not m:
            continue                      # idle-watch yüklemeyen şablon (login, share, kiosk…)
        indent = m.group(1)
        new = text[:m.end()] + "\n" + indent + LINE + text[m.end():]
        f.write_text(new, encoding="utf-8")
        changed.append(f.name)

    print("Eklendi :", ", ".join(changed) or "—")
    print("Atlandı :", ", ".join(skipped) or "—")
    print("Toplam  :", len(changed), "şablon")


if __name__ == "__main__":
    main()
