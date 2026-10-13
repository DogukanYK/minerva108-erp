# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Üst çubuk araması — "önce sistemde ara, sonra sor" (13.10.2026 kullanıcı kararı).

YAPAY ZEKÂ ÇAĞRISI YOK, maliyetsiz: yazdıkça (`GET /api/assistant/search`)
asistanın kendi araç işlevleri `tools.run_tool` üzerinden çağrılır — izin ve
aktif panel denetimi asistanla BİREBİR (`find_item`, `find_recipe`,
`find_supplier`) — + lot no araması (`inventory.view`, panel) + izinle
süzülmüş sabit sayfa listesi.  Her grup ≤ `GROUP_MAX`; en altta istemci daima
"Minerva'ya sor" satırını ekler.

Sayfa listesi `api_main` sayfa route'larının izin kapılarıyla aynıdır
(`test_assistant_search.py` route'lardaki `_user_can` çağrılarıyla karşılaştırır):
kullanıcının açamayacağı sayfa önerilmez.  Tedarikçi satırında iletişim alanı
(telefon/e-posta) GÖSTERİLMEZ — arama sonucu yalnız gezinme içindir.
"""
from dataclasses import dataclass
from typing import Callable, List, Optional, Tuple
from urllib.parse import quote

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.assistant.tools import ToolContext, run_tool
from core.lots import normalize_lot
from core.supplier_prices import normalize
from database import Inventory, Item

GROUP_MAX = 5
Q_MIN = 2
Q_MAX = 80
LOT_MIN = 3


@dataclass(frozen=True)
class Page:
    label: str
    href: str
    icon: str                                   # Bootstrap Icons sınıfı (bi-…)
    perm: Optional[Tuple[str, str]] = None      # None = her iç kullanıcı
    keywords: Tuple[str, ...] = ()
    superadmin: bool = False


#: Sol menüyle aynı ad + ikon; izin = sayfa route'unun `_user_can` kapısı.
PAGES: Tuple[Page, ...] = (
    Page("Dashboard", "/", "bi-grid-1x2-fill", None, ("ana sayfa", "pano", "özet")),
    Page("Ürünler", "/items", "bi-box-seam", ("items", "view"), ("kart", "malzeme", "hammadde", "ambalaj", "sku", "barkod")),
    Page("Tedarikçiler", "/suppliers", "bi-truck", ("items", "view"), ("firma", "tedarik", "fiyat")),
    Page("Mal Kabul", "/receiving", "bi-box-arrow-in-down", ("inventory", "view"), ("giriş", "fatura", "irsaliye", "kabul")),
    Page("Reçeteler", "/recipes", "bi-journal-text", ("recipes", "view"), ("formül", "reçete")),
    Page("Üretim", "/production", "bi-gear", ("production", "view"), ("parti", "üretim")),
    Page("Fason Üretim", "/outsourcing", "bi-box-seam", ("outsourcing", "view"), ("fason", "dış üretim")),
    Page("B2B Siparişleri", "/b2b-siparisler", "bi-clipboard2-check", ("b2b_orders", "view"), ("sipariş", "müşteri siparişi")),
    Page("Kalite Kontrol", "/qc", "bi-patch-check", ("qc", "view"), ("qc", "karantina", "onay")),
    Page("Stoklar", "/stocks", "bi-boxes", ("inventory", "view"), ("stok", "depo", "konum")),
    Page("Teslimat", "/delivery", "bi-box-arrow-up", ("inventory", "view"), ("kargo", "sevk", "gönderi", "proforma")),
    Page("İadeler", "/returns", "bi-arrow-return-left", ("inventory", "view"), ("iade",)),
    Page("Numune Analizi", "/numune-analiz", "bi-clipboard2-pulse", ("qc", "view"), ("numune", "analiz", "deneme")),
    Page("Şahit Numune", "/sahit-numune", "bi-archive", ("retention", "view"), ("şahit", "dolap", "numune")),
    Page("Ürün Görselleri", "/urun-gorselleri", "bi-images", ("items", "import"), ("görsel", "fotoğraf", "amazon", "sds")),
    Page("PDKS", "/pdks", "bi-fingerprint", ("pdks", "view_own"), ("puantaj", "mesai", "izin", "giriş çıkış", "devam")),
    Page("Yorumlar", "/yorumlar", "bi-chat-square-text", ("reviews", "view"), ("yorum", "değerlendirme")),
    Page("Creator Programı", "/influencer", "bi-stars", ("influencer", "view"), ("influencer", "creator", "kampanya")),
    Page("İşlem Defteri", "/ledger", "bi-journal-bookmark", ("reports", "view"), ("defter", "hareket", "işlem")),
    Page("Raporlar", "/reports", "bi-bar-chart-line", ("reports", "view"), ("rapor", "analiz", "tüketim")),
    Page("Satın Alma", "/satin-alma", "bi-cart-check", ("reports", "view"), ("satın alma", "alım", "sipariş planı")),
    Page("Trendyol", "/trendyol", "bi-shop-window", ("trendyol", "view"), ("pazaryeri",)),
    Page("İzlenebilirlik", "/traceability", "bi-diagram-3", ("inventory", "view"), ("lot", "skt", "izlenebilirlik", "iz")),
    Page("Dosya Paylaşım", "/drive", "bi-folder-fill", None, ("drive", "dosya", "belge", "sertifika")),
    Page("CRM", "/crm", "bi-people-fill", ("crm", "view"), ("müşteri", "firma", "görüşme")),
    Page("Distribütörler", "/distributors", "bi-shop", ("distributors", "view"), ("bayi", "distribütör")),
    Page("B2B Teklifler", "/quotations", "bi-file-earmark-text", ("b2b", "view"), ("teklif", "proforma", "fiyat teklifi")),
    Page("Yönetim Paneli", "/admin", "bi-shield-lock", ("admin", "view"), ("yönetim", "kullanıcı", "yetki", "ayar")),
    Page("Shopify Senkron", "/shopify-sync", "bi-bag-check", ("admin", "view"), ("shopify", "mağaza")),
    Page("Sistem Durumu", "/system", "bi-activity", None, ("sistem", "sağlık"), superadmin=True),
    Page("Asistan", "/asistan", "bi-chat-left-text", None, ("asistan", "soru", "geçmiş")),
    Page("Belge Oku", "/asistan/belge", "bi-file-earmark-text", None, ("belge", "fatura", "coa", "proforma oku")),
    Page("Metin Yaz", "/asistan/yaz", "bi-pencil-square", None, ("metin", "çeviri", "açıklama", "e-posta")),
)


def _tokens(text: str) -> List[str]:
    return [t for t in normalize(text).split() if len(t) >= 2]


def page_score(page: Page, q_norm: str, q_tokens: List[str]) -> int:
    """SAF: 0 = eşleşmedi.  Ad öneki 3 › ad/anahtar kelime içinde geçme 2 ›
    sorudaki bir kelime anahtar kelimeyle başlıyor ("kargolanacak" → kargo) 1."""
    label = normalize(page.label)
    if q_norm and label.startswith(q_norm):
        return 3
    words = [label] + [normalize(k) for k in page.keywords]
    if q_norm and any(q_norm in w for w in words):
        return 2
    keys = [w for w in words if len(w) >= 3]
    if any(t.startswith(k) or (len(t) >= 3 and k.startswith(t)) for t in q_tokens for k in keys):
        return 1
    return 0


def visible_pages(ctx: ToolContext) -> List[Page]:
    return [p for p in PAGES
            if (not p.superadmin or ctx.role == "SuperAdmin")
            and (p.perm is None or ctx.can(*p.perm))]


def match_pages(ctx: ToolContext, q: str) -> List[dict]:
    q_norm, q_tokens = normalize(q), _tokens(q)
    scored = [(page_score(p, q_norm, q_tokens), i, p) for i, p in enumerate(visible_pages(ctx))]
    scored = [t for t in scored if t[0] > 0]
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [{"title": p.label, "sub": p.href, "href": p.href, "icon": p.icon}
            for _, _, p in scored[:GROUP_MAX]]


def tr_num(v) -> str:
    """12500.5 → '12.500,5' (TR biçimi; en çok 3 ondalık, sondaki sıfırlar atılır)."""
    if v is None:
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    s = f"{f:,.3f}".rstrip("0").rstrip(".")
    return s.replace(",", "\x00").replace(".", ",").replace("\x00", ".")


def _tool_rows(ctx: ToolContext, name: str, args: dict, key: str,
               build: Callable[[dict], dict]) -> List[dict]:
    res = run_tool(ctx, name, args)
    if res.is_error or not res.payload:
        return []
    return [build(r) for r in (res.payload.get(key) or [])[:GROUP_MAX]]


def _item_row(r: dict) -> dict:
    unit = r.get("unit") or ""
    return {"title": r["name"], "sub": " · ".join(x for x in (r.get("category"), r.get("sku")) if x),
            "meta": f"{tr_num(r.get('stock'))} {unit}".strip(),
            "href": f"/items?q={quote(r['name'], safe='')}&id={int(r['id'])}", "icon": "bi-box-seam"}


def _recipe_row(r: dict) -> dict:
    sub = r.get("target_item") or ""
    return {"title": r["name"], "sub": sub if sub and sub != r["name"] else "",
            "meta": f"{r.get('ingredients_count') or 0} bileşen",
            "href": f"/recipes?q={quote(r['name'], safe='')}", "icon": "bi-journal-text"}


def _supplier_row(r: dict) -> dict:
    status = r.get("status") or "normal"
    return {"title": r["name"], "sub": "" if status == "normal" else (r.get("status_label") or ""),
            "meta": "" if r.get("is_active", True) else "pasif",
            "href": f"/suppliers?q={quote(r['name'], safe='')}", "icon": "bi-truck"}


def lot_rows(ctx: ToolContext, db: Session, q: str) -> List[dict]:
    """Lot no araması (lot no'lar rakam taşır): ≥3 karakter + rakam + boşluksuz.
    Önek eşleşmesi önce; ≥4 karakterde içinde geçen de.  Lot ürün bazlıdır →
    (lot, ürün) başına tek satır; panel süzgeci var (find_lot gibi)."""
    raw = (q or "").strip()
    if not ctx.can("inventory", "view") or any(ch.isspace() for ch in raw):
        return []
    lot = normalize_lot(raw)
    if len(lot) < LOT_MIN or not any(ch.isdigit() for ch in lot):
        return []
    esc = lot.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    pattern = (f"%{esc}%" if len(lot) >= 4 else f"{esc}%")
    rows = (db.query(Inventory.lot_number, Item.id, Item.name, Item.unit,
                     func.sum(Inventory.quantity), func.max(Inventory.id))
            .join(Item, Item.id == Inventory.item_id)
            .filter(Inventory.domain == ctx.domain, Inventory.lot_number.isnot(None),
                    func.upper(Inventory.lot_number).like(pattern, escape="\\"))
            .group_by(Inventory.lot_number, Item.id, Item.name, Item.unit)
            .order_by(func.max(Inventory.id).desc())
            .limit(40).all())
    rows.sort(key=lambda r: (not (r[0] or "").upper().startswith(lot), -(r[5] or 0)))
    return [{"title": ln, "sub": name, "meta": f"{tr_num(qty)} {unit or ''}".strip(),
             "href": f"/traceability?lot={quote(ln, safe='')}&item_id={int(iid)}", "icon": "bi-upc-scan"}
            for ln, iid, name, unit, qty, _ in rows[:GROUP_MAX]]


def search(ctx: ToolContext, db: Session, q: str) -> dict:
    """Gruplar sırayla: Ürünler · Lotlar · Reçeteler · Tedarikçiler · Sayfalar.
    Boş grup gönderilmez.  `q` < Q_MIN → boş liste."""
    q = (q or "").strip()[:Q_MAX]
    if len(q) < Q_MIN:
        return {"q": q, "groups": []}
    groups = [
        ("items", "Ürünler", _tool_rows(ctx, "find_item", {"q": q, "category": None}, "items", _item_row)),
        ("lots", "Lotlar", lot_rows(ctx, db, q)),
        ("recipes", "Reçeteler", _tool_rows(ctx, "find_recipe", {"q": q}, "recipes", _recipe_row)),
        ("suppliers", "Tedarikçiler", _tool_rows(ctx, "find_supplier", {"q": q}, "suppliers", _supplier_row)),
        ("pages", "Sayfalar", match_pages(ctx, q)),
    ]
    return {"q": q, "groups": [{"key": k, "label": label, "rows": rows} for k, label, rows in groups if rows]}
