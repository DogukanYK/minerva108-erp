# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
IMS → Shopify tek yön stok push (3 mağaza, marka başına).

IMS tek doğruluk kaynağı (Item.current_stock); Shopify vitrin. Marka ürün adının
ilk kelimesinden türetilir (core.production_sim.brand_of) → hangi mağaza. Eşleşme
BARKOD (Item.barcode) üzerinden; barkodu ≥2 aktif üründe olan "belirsiz" sayılır ve
tahmin edilmeden atlanıp raporlanır.

Kommo entegrasyonunun aynası (ters yön: pull değil push). Senkron httpx.Client,
config runtime `os.getenv(...,"")` ile okunur → env yoksa modül import edilebilir
kalır ve `is_configured()` False döner (no-op). Global buffer/enabled AppSetting'te
(`shopify.buffer`/`shopify.enabled`); durum ShopifySyncState tablosunda (mağaza/satır).

Shopify Admin GraphQL: POST /admin/api/{ver}/graphql.json, X-Shopify-Access-Token.
GraphQL 200 dönse de body["errors"] / userErrors kontrol edilir.
Faz 2 (Shopify siparişi → IMS stok düşümü webhook'u) henüz YOK — routers/shopify.py stub.
"""
import json
import logging
import math
import os
import time
from datetime import datetime
from typing import List, Optional, Tuple

import httpx
from sqlalchemy.orm import Session

from database import AppSetting, Item, ShopifySyncState, to_tr
from core.production_sim import brand_of

logger = logging.getLogger("minerva108.shopify")

_BRANDS = ("Minerva", "Evanira", "Serenida")   # kanonik marka adları (görüntü)
_RATE_SLEEP = 0.3          # GraphQL çağrıları arası (Shopify maliyet-bazlı limiter)
_VARIANT_PAGE = 250        # productVariants sayfa boyutu
_PUSH_CHUNK = 100          # inventorySetQuantities başına quantity (≤250; 100 güvenli)


class ShopifyError(RuntimeError):
    """GraphQL/transport düzeyinde beklenmeyen hata."""


# ─── Türkçe-katlanmış marka eşleştirme ───────────────────────────────────────

def _fold(s: str) -> str:
    return (s or "").strip().translate({ord("İ"): "i", ord("I"): "ı"}).casefold()


_BRAND_BY_FOLD = {_fold(b): b for b in _BRANDS}


def canonical_brand(name: str) -> Optional[str]:
    """Ürün adının ilk kelimesinden kanonik marka; hedef marka değilse None."""
    return _BRAND_BY_FOLD.get(_fold(brand_of(name)))


def store_key(brand: str) -> str:
    return _fold(brand).replace("ı", "i")   # 'minerva'/'evanira'/'serenida'


# ─── Yapılandırma (runtime — import-time DEĞİL) ───────────────────────────────

def _api_version() -> str:
    return (os.getenv("SHOPIFY_API_VERSION", "") or "2025-01").strip()


def _env(brand: str, suffix: str) -> str:
    return (os.getenv(f"SHOPIFY_{brand.upper()}_{suffix}", "") or "").strip()


def store_config(brand: str) -> Optional[dict]:
    """{'store','client_id','client_secret','location'} — hepsi doluysa; yoksa None.

    Yeni Shopify Dev Dashboard'da statik Admin API token'ı YOK; uygulama mağazaya
    kurulduktan sonra Client ID + Client Secret ile client_credentials akışından
    24 saatlik token alınır (bkz. _get_access_token)."""
    store = _env(brand, "STORE")
    client_id = _env(brand, "CLIENT_ID")
    client_secret = _env(brand, "CLIENT_SECRET")
    location = _env(brand, "LOCATION")
    if not (store and client_id and client_secret and location):
        return None
    return {"store": store, "client_id": client_id, "client_secret": client_secret,
            "location": location, "brand": brand}


def is_configured(brand: str) -> bool:
    return store_config(brand) is not None


def any_configured() -> bool:
    return any(is_configured(b) for b in _BRANDS)


# ─── Global ayarlar (AppSetting KV) ──────────────────────────────────────────

def _get_setting(db: Session, key: str) -> Optional[str]:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    return row.value if row else None


def _set_setting(db: Session, key: str, value: str) -> None:
    row = db.query(AppSetting).filter(AppSetting.key == key).first()
    if row:
        row.value = value
    else:
        db.add(AppSetting(key=key, value=value))


def get_buffer(db: Session) -> int:
    try:
        return max(0, int(float(_get_setting(db, "shopify.buffer") or 0)))
    except (TypeError, ValueError):
        return 0


def get_enabled(db: Session) -> bool:
    return (_get_setting(db, "shopify.enabled") or "true").lower() != "false"


def set_config(db: Session, *, buffer: Optional[int] = None, enabled: Optional[bool] = None,
               store_key_: Optional[str] = None, store_enabled: Optional[bool] = None) -> None:
    if buffer is not None:
        _set_setting(db, "shopify.buffer", str(max(0, int(buffer))))
    if enabled is not None:
        _set_setting(db, "shopify.enabled", "true" if enabled else "false")
    if store_key_ and store_enabled is not None:
        st = _state(db, store_key_)
        st.enabled = bool(store_enabled)
    db.commit()


# ─── Durum tablosu ────────────────────────────────────────────────────────────

def _state(db: Session, sk: str, brand: Optional[str] = None) -> ShopifySyncState:
    st = db.query(ShopifySyncState).filter(ShopifySyncState.store_key == sk).first()
    if not st:
        st = ShopifySyncState(store_key=sk, brand=brand, enabled=True)
        db.add(st)
        db.flush()
    if brand and not st.brand:
        st.brand = brand
    return st


# ─── OAuth token (client_credentials) — Dev Dashboard uygulaması ─────────────
# Yeni akış: statik token yok. Uygulama mağazaya KURULDUKTAN sonra Client ID +
# Client Secret ile POST /admin/oauth/access_token → 24 saatlik access_token.
# Süresi dolana dek modül-içi cache'lenir (5 dk emniyet payı).

_TOKEN_CACHE: dict = {}   # store → (access_token, expiry_epoch)


def _get_access_token(cfg: dict) -> str:
    store = cfg["store"]
    cached = _TOKEN_CACHE.get(store)
    if cached and cached[1] > time.time() + 300:
        return cached[0]
    r = httpx.post(
        f"https://{store}/admin/oauth/access_token",
        data={"grant_type": "client_credentials",
              "client_id": cfg["client_id"],
              "client_secret": cfg["client_secret"]},
        headers={"Accept": "application/json"},
        timeout=20.0,
    )
    r.raise_for_status()
    body = r.json()
    token = body.get("access_token")
    if not token:
        raise ShopifyError(f"access_token alınamadı: {str(body)[:200]}")
    _TOKEN_CACHE[store] = (token, time.time() + int(body.get("expires_in", 86399)))
    return token


# ─── GraphQL istemci ──────────────────────────────────────────────────────────

def _client(cfg: dict) -> httpx.Client:
    token = _get_access_token(cfg)
    return httpx.Client(
        base_url=f"https://{cfg['store']}/admin/api/{_api_version()}",
        headers={"X-Shopify-Access-Token": token, "Content-Type": "application/json"},
        timeout=20.0,
    )


def _graphql(client: httpx.Client, query: str, variables: dict) -> dict:
    r = client.post("/graphql.json", json={"query": query, "variables": variables})
    r.raise_for_status()                       # transport hatası
    body = r.json()
    if body.get("errors"):                     # GraphQL 200 dönse de hata olabilir
        raise ShopifyError(str(body["errors"])[:400])
    time.sleep(_RATE_SLEEP)
    return body.get("data") or {}


_Q_VARIANTS = """
query($cursor: String) {
  productVariants(first: 250, after: $cursor) {
    pageInfo { hasNextPage endCursor }
    nodes {
      id
      barcode
      sku
      inventoryItem { id }
      product { id status }
    }
  }
}
"""

_M_SET = """
mutation($input: InventorySetQuantitiesInput!) {
  inventorySetQuantities(input: $input) {
    inventoryAdjustmentGroup { createdAt reason }
    userErrors { field message code }
  }
}
"""


def fetch_variants(brand: str) -> List[dict]:
    """Mağazadaki tüm varyantlar → [{barcode, sku, inventory_item_id, variant_id,
    product_status}]. Barkodsuzlar da döner (raporlama için); eşlemede atlanır."""
    cfg = store_config(brand)
    if not cfg:
        return []
    out: List[dict] = []
    with _client(cfg) as client:
        cursor = None
        while True:
            data = _graphql(client, _Q_VARIANTS, {"cursor": cursor})
            conn = data.get("productVariants") or {}
            for n in conn.get("nodes") or []:
                inv = (n.get("inventoryItem") or {}).get("id")
                prod = n.get("product") or {}
                out.append({
                    "barcode": (n.get("barcode") or "").strip(),
                    "sku": (n.get("sku") or "").strip(),
                    "inventory_item_id": inv,
                    "variant_id": n.get("id"),
                    "product_status": prod.get("status"),
                })
            page = conn.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                break
            cursor = page.get("endCursor")
    return out


def push_inventory(brand: str, pairs: List[Tuple[str, int]]) -> dict:
    """[(inventory_item_id, qty)] → inventorySetQuantities (100'lük chunk).
    Döner {'pushed': n, 'userErrors': [...]}. userErrors raise etmez, toplar."""
    cfg = store_config(brand)
    if not cfg or not pairs:
        return {"pushed": 0, "userErrors": []}
    loc = f"gid://shopify/Location/{cfg['location']}"
    pushed, errors = 0, []
    with _client(cfg) as client:
        for i in range(0, len(pairs), _PUSH_CHUNK):
            chunk = pairs[i:i + _PUSH_CHUNK]
            variables = {"input": {
                "name": "available",
                "reason": "correction",
                "ignoreCompareQuantity": True,
                "quantities": [
                    {"inventoryItemId": iid, "locationId": loc, "quantity": qty}
                    for iid, qty in chunk
                ],
            }}
            data = _graphql(client, _M_SET, variables)
            res = data.get("inventorySetQuantities") or {}
            ue = res.get("userErrors") or []
            if ue:
                errors.extend(ue)
            else:
                pushed += len(chunk)
    return {"pushed": pushed, "userErrors": errors}


# ─── Orkestratör ──────────────────────────────────────────────────────────────

def _ims_items_by_brand(db: Session, brand: str) -> List[Item]:
    """Aktif paneldeki (cosmetics) bitmiş, barkodlu, bu markaya ait ürünler."""
    rows = (db.query(Item)
            .filter(Item.is_active == True,             # noqa: E712
                    Item.domain == "cosmetics",
                    Item.category == "Bitmiş Ürün",
                    Item.barcode.isnot(None))
            .all())
    return [it for it in rows if (it.barcode or "").strip()
            and canonical_brand(it.name) == brand]


def _sync_one(db: Session, brand: str, buffer: int, dry_run: bool) -> dict:
    sk = store_key(brand)
    cfg = store_config(brand)
    entry = {"store": sk, "brand": brand, "configured": cfg is not None,
             "matched": 0, "pushed": 0, "ambiguous": [], "unmatched_ims": [],
             "unmatched_shop": [], "userErrors": [], "error": None}
    if not cfg:
        entry["skipped"] = True
        return entry

    # Shopify varyantları: barkod → inventory_item_id (aktif+draft hedef olabilir)
    variants = fetch_variants(brand)
    shop_map, shop_active = {}, set()
    for v in variants:
        bc = v["barcode"]
        if not bc or not v["inventory_item_id"]:
            continue
        shop_map.setdefault(bc, v["inventory_item_id"])
        if v.get("product_status") == "ACTIVE":
            shop_active.add(bc)

    # IMS ürünleri: barkoda göre grupla; ≥2 aktif ürün → belirsiz (atla)
    ims = _ims_items_by_brand(db, brand)
    by_bc: dict = {}
    for it in ims:
        by_bc.setdefault((it.barcode or "").strip(), []).append(it)

    pairs: List[Tuple[str, int]] = []
    matched = 0
    for bc, items in by_bc.items():
        if len(items) > 1:
            entry["ambiguous"].append({"barcode": bc, "ids": [i.id for i in items]})
            continue
        it = items[0]
        if bc not in shop_map:
            entry["unmatched_ims"].append({"barcode": bc, "id": it.id, "name": it.name})
            continue
        qty = max(0, int(math.floor(float(it.current_stock or 0))) - buffer)
        pairs.append((shop_map[bc], qty))
        matched += 1
    entry["matched"] = matched

    # Shopify'da AKTİF ama IMS'te yok (draft'lar gürültü sayılmaz)
    ims_bcs = set(by_bc.keys())
    for bc in shop_active:
        if bc not in ims_bcs:
            entry["unmatched_shop"].append(bc)

    if not dry_run and pairs:
        res = push_inventory(brand, pairs)
        entry["pushed"] = res["pushed"]
        entry["userErrors"] = res["userErrors"]
    return entry


def run_shopify_sync(db: Session, brands: Optional[List[str]] = None,
                     dry_run: bool = False) -> dict:
    """Marka başına stok push. brands=None → yapılandırılmış hepsi. dry_run=True →
    ne Shopify'a yazar ne state (yalnız ne push edileceğini + eşleşmeyeni raporlar)."""
    targets = brands or list(_BRANDS)
    buffer = get_buffer(db)
    per_store = []
    for brand in targets:
        if brand not in _BRANDS:
            continue
        try:
            entry = _sync_one(db, brand, buffer, dry_run)
        except Exception as exc:                       # bir mağaza patlarsa diğerleri sürsün
            logger.exception("shopify sync failed for %s", brand)
            entry = {"store": store_key(brand), "brand": brand,
                     "configured": is_configured(brand), "matched": 0, "pushed": 0,
                     "ambiguous": [], "unmatched_ims": [], "unmatched_shop": [],
                     "userErrors": [], "error": str(exc)[:255]}
        if not dry_run and entry.get("configured"):
            st = _state(db, entry["store"], brand)
            st.last_run_at = datetime.utcnow()
            st.matched_count = entry["matched"]
            st.pushed_count = entry["pushed"]
            st.unmatched = json.dumps({
                "shopify_only": entry["unmatched_shop"],
                "ims_only": entry["unmatched_ims"],
                "ambiguous": entry["ambiguous"],
            }, ensure_ascii=False)
            if entry["error"]:
                st.last_status = f"HATA: {entry['error']}"
            else:
                st.last_sync_at = datetime.utcnow()
                st.last_status = (f"{entry['pushed']} push · {entry['matched']} eşleşme"
                                  f" · {len(entry['unmatched_shop'])} Shopify-tek"
                                  f" · {len(entry['unmatched_ims'])} IMS-tek")
            db.commit()
        per_store.append(entry)
    return {"per_store": per_store}


def status_summary(db: Session) -> dict:
    """UI/test için: mağaza durumları + global ayarlar."""
    states = {s.store_key: s for s in db.query(ShopifySyncState).all()}
    stores = []
    for brand in _BRANDS:
        sk = store_key(brand)
        st = states.get(sk)
        try:
            unmatched = json.loads(st.unmatched) if (st and st.unmatched) else None
        except (TypeError, ValueError):
            unmatched = None
        stores.append({
            "store_key": sk,
            "brand": brand,
            "configured": is_configured(brand),
            "enabled": (st.enabled if st else True),
            "last_sync_at": to_tr(st.last_sync_at).strftime("%d.%m.%Y %H:%M") if (st and st.last_sync_at) else None,
            "last_run_at": to_tr(st.last_run_at).strftime("%d.%m.%Y %H:%M") if (st and st.last_run_at) else None,
            "last_status": (st.last_status if st else None),
            "matched_count": (st.matched_count if st else 0),
            "pushed_count": (st.pushed_count if st else 0),
            "unmatched": unmatched,
        })
    return {
        "stores": stores,
        "any_configured": any_configured(),
        "enabled": get_enabled(db),
        "buffer": get_buffer(db),
        "api_version": _api_version(),
    }


# ─── Faz 2: orders/paid webhook → stok düşümü + Paraşüt fatura ───────────────

def brand_for_store_key(sk: str) -> Optional[str]:
    """'minerva' → 'Minerva' (bilinmeyen → None)."""
    for b in _BRANDS:
        if store_key(b) == sk:
            return b
    return None


def webhook_secret(brand: str) -> str:
    """Webhook HMAC anahtarı: SHOPIFY_<MARKA>_WEBHOOK_SECRET, yoksa client_secret
    (dev-dashboard app'lerinde client_secret webhook'ları imzalar)."""
    explicit = _env(brand, "WEBHOOK_SECRET")
    if explicit:
        return explicit
    cfg = store_config(brand)
    return cfg["client_secret"] if cfg else ""


#: variant_id → barkod önbelleği (mağaza başına). Sipariş webhook'u barkod
#  göndermediği için stok eşleşmesinde kullanılır; 10 dk sonra tazelenir.
_VARIANT_BC_CACHE: dict = {}      # store_key → (expiry_epoch, {variant_id: barcode})
_VARIANT_BC_TTL = 600


def _variant_barcode(store_key_: str, variant_id) -> Optional[str]:
    """Shopify variant_id'den barkod. Mağaza haritası önbelleklenir; hata → None."""
    try:
        vid = str(variant_id).split("/")[-1]
        cached = _VARIANT_BC_CACHE.get(store_key_)
        if not cached or cached[0] < time.time():
            brand = brand_for_store_key(store_key_)
            if not brand:
                return None
            mapping = {}
            for v in fetch_variants(brand):
                key = str(v.get("variant_id") or "").split("/")[-1]
                if key and v.get("barcode"):
                    mapping[key] = v["barcode"]
            cached = (time.time() + _VARIANT_BC_TTL, mapping)
            _VARIANT_BC_CACHE[store_key_] = cached
        return cached[1].get(vid)
    except Exception:
        logger.exception("variant_id → barkod çözümlenemedi (%s)", store_key_)
        return None


#: "Turkey" gibi ülke ADI gelen (country_code boş) siparişler için eşleme.
_TR_NAMES = {"turkey", "türkiye", "turkiye", "republic of türkiye", "republic of turkey"}


def _country_code(payload: dict, billing: dict) -> str:
    """Sipariş ülkesi — çok kaynaklı, dayanıklı tespit.

    Shopify bazı akışlarda (draft order → complete, bazı ödeme yöntemleri)
    billing_address'i doldurur ama country/country_code'u BOŞ bırakır — yaşandı:
    gerçek bir TR siparişi 'ihracat' sanılıp faturasız kalıyordu.
    Sıra: billing → shipping → müşterinin varsayılan adresi; kod yoksa ülke ADINDAN
    çıkarım. Hiçbiri yoksa "" döner → akış onu ihracat sayıp MANUEL'e düşürür
    (güvenli taraf: resmi belge asla tahminle kesilmez).
    """
    cust = ((payload.get("customer") or {}).get("default_address") or {})
    sources = [billing or {}, payload.get("shipping_address") or {}, cust]
    for src in sources:                       # 1) ISO kodu
        cc = ((src.get("country_code") or "")).strip().upper()
        if cc:
            return cc
    for src in sources:                       # 2) ülke adı → TR
        nm = ((src.get("country") or "")).strip().lower()
        if nm in _TR_NAMES:
            return "TR"
        if nm:
            return nm[:8].upper()             # bilinmeyen ülke adı → TR değil
    return ""


def _order_summary(store_key_: str, payload: dict) -> dict:
    """Shopify orders/paid payload'ından gerekli alt küme (retry/fatura için)."""
    billing = payload.get("billing_address") or payload.get("shipping_address") or {}
    customer = payload.get("customer") or {}
    name = " ".join(x for x in [billing.get("first_name") or customer.get("first_name"),
                                billing.get("last_name") or customer.get("last_name")] if x).strip()
    lines = []
    for li in payload.get("line_items") or []:
        qty = float(li.get("quantity") or 0)
        price = float(li.get("price") or 0)               # birim, KDV DAHİL (TR mağaza)
        vat = None
        for tl in li.get("tax_lines") or []:
            try:
                vat = round(float(tl.get("rate")) * 100, 2)
                break
            except (TypeError, ValueError):
                pass
        disc = 0.0
        for da in li.get("discount_allocations") or []:
            try:
                disc += float(da.get("amount") or 0)
            except (TypeError, ValueError):
                pass
        lines.append({"barcode": (li.get("barcode") or "").strip() or None,
                      "sku": (li.get("sku") or "").strip() or None,
                      "variant_id": li.get("variant_id"),   # barkod gelmezse eşleşme anahtarı
                      "title": li.get("title") or "",
                      "quantity": qty, "unit_price_incl": price,
                      "vat_rate": vat, "discount_amount": round(disc, 4)})
    shipping = 0.0
    for sl in payload.get("shipping_lines") or []:
        try:
            shipping += float(sl.get("price") or 0)
        except (TypeError, ValueError):
            pass
    company = (billing.get("company") or "").strip()
    return {
        "store_key": store_key_,
        "store_label": brand_for_store_key(store_key_) or store_key_,
        "shopify_order_id": int(payload.get("id") or 0),
        # Shopify 'name' zaten '#1001' biçiminde → baştaki '#' tekilleştirilir
        "order_number": str(payload.get("name") or payload.get("order_number") or "").lstrip("#"),
        "total": float(payload.get("total_price") or 0),
        "currency": payload.get("currency") or "TRY",
        "country": _country_code(payload, billing),
        "customer_email": (payload.get("email") or customer.get("email") or "").strip(),
        "customer_name": name or company or "Shopify Müşterisi",
        "vkn_tckn": "",                                  # Shopify standart alanı yok — B2C varsayımı
        "address": ", ".join(x for x in [billing.get("address1"), billing.get("address2")] if x),
        "city": billing.get("city") or billing.get("province") or "",
        "district": billing.get("city") or "",
        "phone": billing.get("phone") or payload.get("phone") or "",
        "order_date": (payload.get("created_at") or "")[:10],
        "shipping_amount": round(shipping, 4),
        "lines": lines,
    }


def _decrement_stock(db: Session, order: dict) -> Tuple[int, list]:
    """Kalemleri barkod→(sku fallback)→Item eşle, b2b kalıbıyla düş.

    Belirsiz (≥2 aktif eşleşme) / eşleşmeyen kalem ATLANIR + rapor edilir (fatura
    yine kesilir — ürün satıldı). Negatife DÜŞEBİLİR (gerçek: satış oldu; 5-dk
    push Shopify'a zaten max(0,floor) yazar). Döner: (düşülen kalem, sorunlar[]).
    """
    from database import Transaction
    issues, plan = [], []
    for ln in order["lines"]:
        qty = float(ln["quantity"] or 0)
        if qty <= 0:
            continue
        item = None
        bc = (ln.get("barcode") or "").strip()
        if bc:
            matches = (db.query(Item)
                       .filter(Item.barcode == bc, Item.is_active == True,   # noqa: E712
                               Item.domain == "cosmetics",
                               Item.category == "Bitmiş Ürün")
                       .with_for_update().all())
            if len(matches) > 1:
                issues.append(f"belirsiz barkod {bc} (ids {[m.id for m in matches]}) — kalem atlandı")
                continue
            item = matches[0] if matches else None
        if item is None and (ln.get("sku") or "").strip():
            item = (db.query(Item)
                    .filter(Item.sku == ln["sku"].strip(), Item.is_active == True,  # noqa: E712
                            Item.domain == "cosmetics", Item.category == "Bitmiş Ürün")
                    .with_for_update().first())
        if item is None and ln.get("variant_id"):
            # Shopify sipariş webhook'u line_item'da BARKOD GÖNDERMEZ (sadece sku +
            # variant_id) — yaşandı: barkod-eşleşmeli stok hiç düşmüyordu. Mağazadan
            # variant_id → barkod haritasını (bir kez) çekip barkodla eşleştir.
            bc2 = (_variant_barcode(order["store_key"], ln["variant_id"]) or "").strip()
            if bc2:
                matches = (db.query(Item)
                           .filter(Item.barcode == bc2, Item.is_active == True,   # noqa: E712
                                   Item.domain == "cosmetics",
                                   Item.category == "Bitmiş Ürün")
                           .with_for_update().all())
                if len(matches) > 1:
                    issues.append(f"belirsiz barkod {bc2} (ids {[m.id for m in matches]}) — kalem atlandı")
                    continue
                item = matches[0] if matches else None
        if item is None:
            issues.append(f"IMS'te eşleşmedi: {ln.get('barcode') or ln.get('sku') or ln.get('title')} — kalem atlandı")
            continue
        plan.append((item, qty))

    for item, qty in plan:
        item.current_stock = round((item.current_stock or 0) - qty, 6)
        db.add(Transaction(
            item_id=item.id,
            transaction_type="Output",
            quantity=qty,
            notes=f"Shopify #{order['order_number']} · {order['store_label']}",
            performed_by="Shopify",
        ))
    return len(plan), issues


def handle_order_paid(store_key_: str, payload: dict) -> Optional[dict]:
    """orders/paid webhook işleyicisi — BackgroundTask'ta koşar, kendi session'ı.

    Adım bazlı durum makinesi: received → stock_done → invoiced → paid → legalized.
    STOK TEK KEZ düşer; Paraşüt hatasında status=failed + step, retry job kaldığı
    ADIMDAN devam eder. Uçlar: skipped_export (TR dışı), failed.
    """
    import json as _json

    from sqlalchemy.exc import IntegrityError

    from database import SessionLocal, ShopifyOrder
    from core import parasut as P
    from core.audit import log_admin_event

    db = SessionLocal()
    try:
        order = _order_summary(store_key_, payload)
        if not order["shopify_order_id"]:
            logger.warning("shopify webhook: order id yok, atlandı")
            return None

        # ── 1) Dedup (unique store_key+order_id) ──
        row = ShopifyOrder(store_key=store_key_,
                           shopify_order_id=order["shopify_order_id"],
                           order_number=order["order_number"],
                           status="received", total=order["total"],
                           currency=order["currency"], country=order["country"],
                           customer_email=order["customer_email"],
                           customer_name=order["customer_name"],
                           lines_json=_json.dumps(
                               {"lines": order["lines"],
                                "shipping_amount": order["shipping_amount"],
                                "order_date": order["order_date"],
                                "address": order["address"], "city": order["city"],
                                "phone": order["phone"]}, ensure_ascii=False))
        db.add(row)
        try:
            db.flush()
        except IntegrityError:
            db.rollback()
            row = (db.query(ShopifyOrder)
                   .filter(ShopifyOrder.store_key == store_key_,
                           ShopifyOrder.shopify_order_id == order["shopify_order_id"])
                   .with_for_update().first())
            if not row or row.status not in ("failed",):
                logger.info("shopify webhook dedup: %s #%s (%s) — atlandı",
                            store_key_, order["order_number"],
                            row.status if row else "?")
                return {"dedup": True}
        row.attempts = (row.attempts or 0) + 1
        db.commit()

        return _process_order(db, row, order)
    except Exception:
        logger.exception("handle_order_paid failed (%s)", store_key_)
        return None
    finally:
        db.close()


def _process_order(db: Session, row, order: dict) -> dict:
    """Durum makinesini kaldığı yerden yürütür (webhook + retry ortak yolu)."""
    from core import parasut as P
    from core.audit import log_admin_event
    from core.notifications import (notify_export_order_manual, notify_low_stock,
                                    notify_parasut_failure)

    lows = []
    try:
        # ── 2) Ülke ──
        if row.status == "received":
            if (order.get("country") or "") != "TR":
                row.status = "skipped_export"
                db.commit()
                try:
                    notify_export_order_manual(order["order_number"], order.get("country") or "?")
                except Exception:
                    logger.exception("export notify failed")
                return {"status": row.status}

            # ── 3) Stok (yalnız bir kez) ──
            row.step = "stock"
            n, issues = _decrement_stock(db, order)
            if issues:
                row.last_error = ("; ".join(issues))[:500]
            row.status = "stock_done"
            row.stock_applied = True          # stokla BİRLİKTE commit — retry'ın tek doğrusu
            db.commit()
            for it_name, cur, mn, unit in _low_stock_after(db, order):
                lows.append((it_name, cur, mn, unit))
            for args in lows:
                try:
                    notify_low_stock(*args)
                except Exception:
                    logger.exception("low stock notify failed")
            if issues:
                try:
                    notify_parasut_failure(order["order_number"],
                                           "Stok eşleşme uyarısı: " + "; ".join(issues)[:200])
                except Exception:
                    pass

        # ── 4) Paraşüt ──
        if P.is_configured() and _parasut_enabled(db):
            if row.status == "stock_done":
                row.step = "contact"
                order["vkn_tckn"] = row.vkn_tckn or order.get("vkn_tckn") or ""
                cid = P.find_or_create_contact(order)
                row.parasut_contact_id = cid
                row.step = "invoice"
                inv = P.create_invoice(order, order["lines"], cid)
                row.parasut_invoice_id = inv
                row.status = "invoiced"
                db.commit()
            if row.status == "invoiced":
                row.step = "payment"
                P.add_payment(row.parasut_invoice_id, order["total"],
                              date=order.get("order_date") or None,
                              description=f"iyzico · Shopify #{order['order_number']}",
                              store_key=row.store_key)   # marka kendi iyzico hesabına
                row.status = "paid"
                db.commit()
            if row.status == "paid":
                row.step = "legalize"
                res = P.legalize(order, row.parasut_invoice_id)
                if res["mode"] == "official":
                    row.parasut_doc_type = res["doc_type"]
                    row.trackable_job_id = res["job_id"]
                    if res["job_status"] == "done":
                        row.status = "legalized"
                # draft modda paid'de kalır (bilinçli)
                db.commit()

        log_admin_event(db, None, actor=None, action="shopify.order_paid",
                        target_type="shopify_order", target_id=row.id,
                        target_name=order["order_number"],
                        details={"store": row.store_key, "total": order["total"],
                                 "status": row.status})
        return {"status": row.status}
    except Exception as exc:
        db.rollback()
        row.status = "failed"
        row.last_error = str(exc)[:500]
        db.commit()
        logger.exception("order processing failed: %s", order.get("order_number"))
        try:
            notify_parasut_failure(order["order_number"], str(exc)[:200])
        except Exception:
            pass
        return {"status": "failed", "error": str(exc)[:200]}


def _low_stock_after(db: Session, order: dict):
    """Commit sonrası düşen kalemlerin kritik stok kontrolü (primitif döner)."""
    out = []
    bcs = [ln.get("barcode") for ln in order["lines"] if ln.get("barcode")]
    if not bcs:
        return out
    rows = (db.query(Item)
            .filter(Item.barcode.in_(bcs), Item.is_active == True,   # noqa: E712
                    Item.domain == "cosmetics").all())
    for it in rows:
        if (it.min_stock_level or 0) > 0 and (it.current_stock or 0) <= it.min_stock_level:
            out.append((it.name, float(it.current_stock or 0),
                        float(it.min_stock_level), it.unit or ""))
    return out


def _parasut_enabled(db: Session) -> bool:
    return (_get_setting(db, "parasut.enabled") or "true").lower() != "false"


def retry_order(db: Session, row) -> dict:
    """failed siparişi kaldığı adımdan yeniden dener (stok TEKRAR DÜŞMEZ)."""
    import json as _json
    try:
        saved = _json.loads(row.lines_json or "{}")
    except (TypeError, ValueError):
        saved = {}
    order = {
        "store_key": row.store_key,
        "store_label": brand_for_store_key(row.store_key) or row.store_key,
        "shopify_order_id": row.shopify_order_id,
        "order_number": row.order_number or "",
        "total": row.total or 0.0, "currency": row.currency or "TRY",
        "country": row.country or "", "customer_email": row.customer_email or "",
        "customer_name": row.customer_name or "", "vkn_tckn": row.vkn_tckn or "",
        "address": saved.get("address") or "", "city": saved.get("city") or "",
        "district": saved.get("city") or "", "phone": saved.get("phone") or "",
        "order_date": saved.get("order_date") or "",
        "shipping_amount": saved.get("shipping_amount") or 0.0,
        "lines": saved.get("lines") or [],
    }
    # failed → kaldığı ADIMA geri sar. Stok için TEK doğru kaynak `stock_applied`
    # bayrağıdır (step, hata sonrası rollback'te geri sarabilir — yaşandı/testli).
    if row.status == "failed":
        if row.parasut_doc_type:
            row.status = "paid"               # fatura+tahsilat var, resmileştirme kaldı
        elif row.parasut_invoice_id:
            row.status = "invoiced"           # fatura var, tahsilat kaldı
        elif row.stock_applied:
            row.status = "stock_done"         # stok DÜŞTÜ — asla tekrar düşürme
        else:
            row.status = "received"           # stok hiç düşmedi
    # Her deneme sayılır → 'attempts < 5' limiti tüm durumlar için işler
    # (yarım kalmış stock_done/invoiced kayıtları da sonsuza dek denenmez).
    row.attempts = (row.attempts or 0) + 1
    db.commit()
    return _process_order(db, row, order)
