"""Tüm şablonların Sistem bölümüne 'Shopify Senkron' nav linkini ekler (one-off, idempotent).

Kalıp: Sistem bloğundaki href="/admin" (Yönetim Paneli) linkinin ALTINA aynı girintiyle.
/admin linki çoğu şablonda tek satır ama index.html'de çok satırlı → re.S ile eşle.
Zaten href="/shopify-sync" içeren şablonlar atlanır (shopify_sync.html elle yazıldı).
Sistem bloğu olmayan şablonlar (reports/items…) linki almaz — doğru davranış (admin.view yok).
"""
import re
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "templates"
LINK = '<a href="/shopify-sync" class="nav-link-item"><i class="bi bi-bag-check nav-icon"></i>Shopify Senkron</a>'
PAT = re.compile(r'([ \t]*)<a href="/admin"[^>]*>.*?Yönetim Paneli</a>', re.S)

changed, skipped = [], []
for f in sorted(TPL.glob("*.html")):
    text = f.read_text(encoding="utf-8")
    if 'href="/shopify-sync"' in text:
        skipped.append(f.name)
        continue
    m = PAT.search(text)
    if not m:
        continue                      # Sistem bloğu (admin.view) olmayan şablon
    indent = m.group(1)
    new = text[:m.end()] + "\n" + indent + LINK + text[m.end():]
    f.write_text(new, encoding="utf-8")
    changed.append(f.name)

print("Eklendi :", ", ".join(changed) or "—")
print("Atlandı :", ", ".join(skipped) or "—")
