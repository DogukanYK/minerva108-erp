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
    """{'store','token','location'} — üçü de doluysa; değilse None (o mağaza kapalı)."""
    store = _env(brand, "STORE")
    token = _env(brand, "TOKEN")
    location = _env(brand, "LOCATION")
    if not (store and token and location):
        return None
    return {"store": store, "token": token, "location": location, "brand": brand}


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


# ─── GraphQL istemci ──────────────────────────────────────────────────────────

def _client(store: str, token: str) -> httpx.Client:
    return httpx.Client(
        base_url=f"https://{store}/admin/api/{_api_version()}",
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
    with _client(cfg["store"], cfg["token"]) as client:
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
    with _client(cfg["store"], cfg["token"]) as client:
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
