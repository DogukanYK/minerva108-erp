# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
"Malzeme Fiyatları" sayfası (`/fiyatlar`) — rapor almadan fiyatlara bakış
(Işık Hanım, 16.10.2026).  Uç: `GET /api/supplier-prices/overview`
(routers/reports.py, `reports.view` + aktif panel).

  • load_supplier_inputs(db, item_ids, domain, rows) → PriceInputs
        Satın alma planının tedarikçi / tercih / teklif girdileri — yalnız
        durum bayrakları için gerekenler (lot, giriş, sipariş, analiz YOK).
        `core.purchase_pricing.load_price_inputs` ile aynı sorgular.
  • build_overview(db, domain, currency, q, category, has_price, source, rates_fn)
        → {currency, fx, blocks: [{group, cards: [{…, rows: […]}]}], summary, …}

Kurallar (satın alma planıyla TEK kaynak — sayfa ve plan aynı şeyi söyler):
  • Kartlar: aktif, panel içi; bitmiş ürün HARİÇ.  Pasif kartın fiyatı görünmez.
  • Elle (`manual`) fiyat aynı firmanın Excel / lab satırını gizler
    (`supplier_prices.firm_keys`, kart başına) — gizlenen satır sayısı kartta.
  • Firma ve malzeme düzeyi bayraklar planın kendi kuralıyla:
    `_status_ctx` / `_row_prefs` / `_offer_status` → normal | preferred |
    phase_out | avoid | skip | inactive (+ "tercih 2. sıra" / "bitirilecek,
    alma" metni `offer_tags`).
  • Net fiyat: `net_unit_price` (KDV dahil → net) → `price_per_purchase_unit`
    (kartın alım birimi kg / l / adet) → `fx.convert` (seçilen para birimi).
    Kur YALNIZ seçilenden farklı para birimli satır varsa çekilir; alınamazsa
    o satırın neti "kur yok".
  • Karşılaştırma: aktif (pasif firma değil) fiyatlı satırlar aynı fiyat
    biriminde VE (aynı para biriminde ya da kurla çevrilmiş) ise
    `comparable`; en ucuz satır (`cheapest`) yalnız o zaman ve en az iki
    satırda işaretlenir.  `plan_pick`: planın bu kartta seçeceği teklif
    (uygun → tercih kademesi → en ucuz; eşdeğer kart teklifleri hariç).
  • Plan ikizleri: planın OTOMATİK birleştirdiği kartlar (aynı katlanmış ad +
    birim, kept kararı / grup kapsamıyorsa — `purchase_plan.merge_map`,
    `plan_merge_twins`) tek satırdır; `plan_pick` küme başına BİR kez, bütün
    üyelerin satırları arasından seçilir (tercihler de kümenin).  Fiyatsız
    ikizi fiyatlı olan kart "fiyatı olmayanlar"a girmez (`twin_priced`).
  • Grup: "aynı malzeme" grubunun en az 2 GÖRÜNÜR kartı tek blokta; arama
    grubun adına ya da bir üyesine uyarsa grubun bütün kartları gelir.
  • Fiyatsız kartlar (`has_price=unpriced|all`): aktif hammadde, ambalaj ve
    etiket kartları — eksik fiyat buradan girilir (plan "Fiyat gir →"
    bağlantısı `/fiyatlar?q=<ad>`).  "Diğer" kovası (Kimyasal / Yardımcı /
    boş kategori — plan bunları hammadde sayar) arama (`q`) ya da
    `category=other` ile gelir; bitmiş ürün hiç gelmez.
  • TR sayı biçimi burada yazılır (`*_text`); JS yeniden biçimlemez.
"""
from datetime import date
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.group_stock import BUCKET_LABELS, classify
from core.purchase_plan import tr_num
from core.purchase_pricing import (CFG_SKIP_SUPPLIERS, CURRENCY_SYMBOL, UNIT_TEXT, OfferRec, PriceInputs,
                                   SupplierRec, _offer_status, _row_prefs, _status_ctx, offer_tags, price_text,
                                   supplier_index)
from core.supplier_prices import (CURRENCIES, LITRE_KG_NOTE, SOURCE_MANUAL, SOURCE_PARASUT, SOURCE_STOK_SON_DURUM,
                                  firm_keys, net_unit_price, normalize, price_per_purchase_unit, serialize_price,
                                  vat_label)

# Süzgeç sözlükleri — uç doğrulaması (routers/reports.py) ve sayfa aynı listeyi kullanır
CATEGORY_FILTERS = ("all", "hammadde", "ambalaj", "etiket", "other")
HAS_PRICE_FILTERS = ("all", "priced", "unpriced")
SOURCE_FILTERS = ("all", "manual", "stok_son_durum", "lab", "parasut")
SOURCE_FILTER_LABELS = {"manual": "Elle girilen", "stok_son_durum": "Stok Son Durum listesi",
                        "lab": "Lab fiyat notları / belge", "parasut": "Paraşüt alış faturası"}
HAS_PRICE_LABELS = {"all": "Hepsi", "priced": "Fiyatı olanlar", "unpriced": "Fiyatı olmayanlar"}
# Fiyatsız listede gösterilen kovalar (bitmiş ürün ve "diğer" hariç)
UNPRICED_BUCKETS = ("hammadde", "ambalaj", "etiket")
_STATUS_KIND = {"preferred": "preferred", "phase_out": "blocked", "avoid": "blocked", "skip": "blocked",
                "inactive": "inactive", "stale": "blocked"}


def _source_bucket(source) -> str:
    """Fiyat satırının kaynak süzgeci: manual | stok_son_durum (boş eski satır
    dahil — içe aktarma da onları Excel sayar) | parasut (onaylanan Paraşüt
    alış faturası satırı) | lab (lab_notu / proforma / fatura / siparis ve
    diğerleri)."""
    s = (source or "").strip()
    if s == SOURCE_MANUAL:
        return "manual"
    if s in ("", SOURCE_STOK_SON_DURUM):
        return "stok_son_durum"
    if s == SOURCE_PARASUT:
        return "parasut"
    return "lab"


def _qty(v) -> str:
    """Ambalaj boyu: 25 · 0,5 · 1.000 (TR biçimi)."""
    v = float(v)
    return tr_num(v) if v.is_integer() else tr_num(v, 3).rstrip("0").rstrip(",")


def _purchase_unit(item_unit) -> str:
    return price_per_purchase_unit({"unit_price": None}, item_unit)[1]


def package_text(sp, item_unit=None) -> Optional[str]:
    """"25 kg" / "5 litre" / "1.000 adet" — ambalaj FİYAT BİRİMİNDE yazılır
    (canlıdaki bütün satırlar kg/l; kartın g/ml birimi değil).  Boş → None."""
    pkg = sp.package_size
    if pkg is None or float(pkg) <= 0:
        return None
    pu = (sp.price_unit or "").strip().lower()
    if not pu:
        pu = "adet" if _purchase_unit(item_unit) == "adet" else "kg"
    return f"{_qty(pkg)} {UNIT_TEXT.get(pu, pu)}"


def _unit_price_text(v, cur: str, unit) -> str:
    sym = CURRENCY_SYMBOL.get(cur, cur)
    u = (unit or "").strip().lower()
    return f"{price_text(float(v))} {sym}" + (f"/{UNIT_TEXT.get(u, u)}" if u else "")


def _dmy(d) -> str:
    return d.strftime("%d.%m.%Y") if d else ""


# ─── Yükleyici ──────────────────────────────────────────────────────────────

def load_supplier_inputs(db: Session, item_ids, domain: str, rows=()) -> PriceInputs:
    """Durum bayrakları için `PriceInputs`: bütün panel tedarikçileri
    (`purchase_status`, birleştirme zinciri), atlanacaklar listesi
    (AppSetting `purchase_plan.skip_suppliers`), `item_ids` kartlarının
    malzeme tercihleri (kartın kendi satırları + aktif grubunun satırları,
    `effective_prefs`) ve `rows` (SupplierPrice) teklifleri.
    `load_price_inputs`'un ilgili bölümüyle birebir aynı kurallar; lot /
    giriş / sipariş / analiz okunmaz (sayfa ilişki dökümü göstermez)."""
    from core.suppliers import effective_prefs, normalize_status
    from database import AppSetting, Item, MaterialGroup, MaterialSupplierPref, Supplier

    suppliers = {s.id: SupplierRec(id=s.id, name=s.name or "", contact_person=s.contact_person,
                                   phone=s.phone, email=s.email, address=s.address,
                                   is_active=bool(s.is_active) if s.is_active is not None else True,
                                   status=normalize_status(s.purchase_status) or "normal",
                                   status_reason=s.status_reason, merged_into_id=s.merged_into_id)
                 for s in db.query(Supplier).filter(Supplier.domain == domain).all()}
    pin = PriceInputs(suppliers=suppliers)
    st = db.query(AppSetting).filter(AppSetting.key == CFG_SKIP_SUPPLIERS).first()
    if st and st.value is not None:
        pin.skip_suppliers = tuple(x.strip() for x in st.value.split(",") if x.strip())
    for sp in rows or ():
        name = sp.supplier_name or (suppliers[sp.supplier_id].name if sp.supplier_id in suppliers else None)
        pin.offers.setdefault(sp.item_id, []).append(OfferRec(
            item_id=sp.item_id, supplier_name=name, supplier_id=sp.supplier_id,
            unit_price=sp.unit_price, package_size=sp.package_size,
            currency=(sp.currency or "USD"), price_unit=sp.price_unit, source=sp.source,
            source_label=sp.source_label, quoted_at=sp.quoted_at,
            vat_included=sp.vat_included, vat_rate=sp.vat_rate))
    ids = sorted({int(i) for i in (item_ids or []) if i is not None})
    if not ids:
        return pin
    group_of = {iid: gid for iid, gid in (
        db.query(Item.id, Item.material_group_id)
        .join(MaterialGroup, MaterialGroup.id == Item.material_group_id)
        .filter(Item.id.in_(ids), Item.domain == domain, MaterialGroup.is_active == True)   # noqa: E712
        .all())}
    cond = MaterialSupplierPref.item_id.in_(ids)
    if group_of:
        cond = or_(cond, MaterialSupplierPref.material_group_id.in_(sorted(set(group_of.values()))))
    pref_rows: Dict[int, list] = {}
    for p in db.query(MaterialSupplierPref).filter(MaterialSupplierPref.domain == domain, cond).all():
        rec = (p.supplier_id, p.preference, p.rank, p.item_id is not None)
        targets = [p.item_id] if p.item_id else [i for i, g in group_of.items() if g == p.material_group_id]
        for iid in targets:
            pref_rows.setdefault(iid, []).append(rec)
    for iid, prs in pref_rows.items():
        eff = effective_prefs(prs)
        if eff:
            pin.prefs[iid] = sorted(((sid, pr, rk) for sid, (pr, rk) in eff.items()),
                                    key=lambda x: (x[1] != "preferred", x[2], x[0]))
    return pin


def plan_merge_twins(db: Session, domain: str, items: Dict[int, object]) -> Dict[int, Tuple[int, ...]]:
    """Satın Alma Planının varsayılan (`merge_duplicates=True`) OTOMATİK
    birleştirmesi: {kart id: (aynı plan satırındaki diğer kart id'leri)}.
    Kural kopyalanmaz — `purchase_plan.merge_map` aynı girdilerle çağrılır:
    `items` (aktif, panel içi kartlar), lab'ın 'kept' kopya kararları ve
    aktif "aynı malzeme" grupları (ikisi de birleştirmeyi engeller)."""
    from core.consumption import item_rec
    from core.purchase_plan import DecisionRec, PlanInputs, _json_int_list, merge_map
    from core.purchase_plan_models import PlanOptionsIn
    from database import DuplicateItemDecision, MaterialGroup

    recs = {iid: item_rec(it) for iid, it in items.items()}
    decisions = []
    for d in db.query(DuplicateItemDecision).filter(DuplicateItemDecision.status == "kept").all():
        ids = frozenset(i for i in (_json_int_list(d.item_ids) or []) if i in recs)
        if len(ids) >= 2:
            decisions.append(DecisionRec(item_ids=ids, status="kept"))
    active_groups = {g for (g,) in db.query(MaterialGroup.id).filter(
        MaterialGroup.domain == domain, MaterialGroup.is_active == True).all()}   # noqa: E712
    mgroup_of = {iid: it.material_group_id for iid, it in items.items() if it.material_group_id in active_groups}
    _root, members = merge_map(PlanInputs(items=recs, recipes={}, decisions=decisions, mgroup_of=mgroup_of,
                                          domain=domain), PlanOptionsIn())
    return {i: tuple(x for x in ids if x != i) for ids in members.values() for i in ids}


# ─── Kur ────────────────────────────────────────────────────────────────────

def _default_rates():
    from core.fx import today_rates
    return today_rates()


def fx_view(rates, needed: bool) -> dict:
    """Kur kutusu: "kur: TCMB 16.10" · önceki kur · varsayılan kur · "kur yok"."""
    if not needed:
        return {"needed": False, "available": True, "text": None, "warning": None, "rates_text": None}
    if rates is None:
        return {"needed": True, "available": False, "text": "kur yok",
                "warning": "Döviz kuru alınamadı — farklı para birimli fiyatlar çevrilemedi.",
                "rates_text": None}
    src = getattr(rates, "source", None) or ""
    day = getattr(rates, "as_of", None)
    if day is None and getattr(rates, "fetched_at", None) is not None:
        from database import to_tr
        day = to_tr(rates.fetched_at).date()
    if src == "fallback":
        text = "kur: varsayılan (TCMB'ye ulaşılamadı)"
    else:
        text = "kur: " + (src or "TCMB") + (f" {day:%d.%m}" if day else "")
        if getattr(rates, "stale", False):
            text += " (önceki kur)"
    parts = []
    for c in ("USD", "EUR"):
        try:
            parts.append(f"1 {CURRENCY_SYMBOL[c]} = {tr_num(rates.rate(c), 4)} ₺")
        except (ValueError, TypeError, KeyError):
            continue
    return {"needed": True, "available": True, "text": text, "warning": getattr(rates, "warning", None),
            "source": src or None, "as_of": day.isoformat() if isinstance(day, date) else None,
            "stale": bool(getattr(rates, "stale", False)), "rates_text": " · ".join(parts) or None}


# ─── Görünüm ────────────────────────────────────────────────────────────────

def _fold_hit(key: str, *texts) -> bool:
    return any(key in normalize(t) for t in texts if t)


def _validate(currency, category, has_price, source) -> Tuple[str, str, str, str]:
    cur = (currency or "USD").strip().upper()
    if cur not in CURRENCIES:
        raise ValueError("Para birimi USD, EUR ya da TRY olmalı.")
    category = (category or "all").strip().lower()
    if category not in CATEGORY_FILTERS:
        raise ValueError("Geçersiz kategori.")
    has_price = (has_price or "all").strip().lower()
    if has_price not in HAS_PRICE_FILTERS:
        raise ValueError("Geçersiz fiyat durumu.")
    source = (source or "all").strip().lower()
    if source not in SOURCE_FILTERS:
        raise ValueError("Geçersiz kaynak.")
    return cur, category, has_price, source


def build_overview(db: Session, domain: str, currency: str = "USD", q: Optional[str] = None,
                   category: str = "all", has_price: str = "all", source: str = "all",
                   rates_fn: Optional[Callable] = None) -> dict:
    """Malzeme Fiyatları görünümü (modül belgesindeki kurallar).  Geçersiz
    süzgeç → ValueError (uç 400 döner).  `rates_fn`: argümansız kur
    fonksiyonu (varsayılan `core.fx.today_rates`) — yalnız gerekirse BİR KEZ
    çağrılır; istisna → kur yok."""
    from database import Item, MaterialGroup, Supplier, SupplierPrice

    cur, category, has_price, source = _validate(currency, category, has_price, source)
    key = normalize(q)[:100] if q else ""

    # ── Kartlar (aktif, panel içi, bitmiş ürün hariç) ──
    sup_names = dict(db.query(Supplier.id, Supplier.name).filter(Supplier.domain == domain).all())
    sup_status = dict(db.query(Supplier.id, Supplier.purchase_status).filter(Supplier.domain == domain).all())
    items: Dict[int, object] = {}
    bucket_of: Dict[int, str] = {}
    for it in db.query(Item).filter(Item.domain == domain, Item.is_active == True).all():   # noqa: E712
        b = classify(it.category, it.pkg_type)
        if b == "finished":
            continue
        items[it.id] = it
        bucket_of[it.id] = b
    groups = {g.id: g.name for g in db.query(MaterialGroup)
              .filter(MaterialGroup.domain == domain, MaterialGroup.is_active == True).all()}   # noqa: E712

    # ── Fiyat satırları + elle-fiyat çakışması (kart başına, firm_keys) ──
    by_item: Dict[int, list] = {}
    for sp in (db.query(SupplierPrice).filter(SupplierPrice.domain == domain)
               .order_by(SupplierPrice.id.asc()).all()):
        if sp.item_id in items:
            by_item.setdefault(sp.item_id, []).append(sp)
    kept: Dict[int, list] = {}
    hidden: Dict[int, int] = {}
    for iid, lst in by_item.items():
        manual = set()
        for sp in lst:
            if sp.source == SOURCE_MANUAL:
                manual |= firm_keys(sp.supplier_id, sp.supplier_name)
        for sp in lst:
            if sp.source != SOURCE_MANUAL and firm_keys(sp.supplier_id, sp.supplier_name) & manual:
                hidden[iid] = hidden.get(iid, 0) + 1
                continue
            kept.setdefault(iid, []).append(sp)

    def priced(iid) -> bool:
        return any(sp.unit_price is not None and sp.unit_price > 0 for sp in kept.get(iid, []))

    # Planın otomatik birleştirdiği ikiz kartlar (tek plan satırı)
    twins = plan_merge_twins(db, domain, items)

    def twin_priced(iid) -> bool:
        return not priced(iid) and any(priced(t) for t in twins.get(iid, ()))

    def disp_name(sp) -> str:
        return sp.supplier_name or sup_names.get(sp.supplier_id) or "—"

    def card_hit(iid) -> bool:
        it = items[iid]
        return _fold_hit(key, it.name, it.name_tr, it.sku, sup_names.get(it.supplier_id),
                         *[disp_name(sp) for sp in kept.get(iid, [])])

    gid_of = {iid: it.material_group_id for iid, it in items.items() if it.material_group_id in groups}
    hit_groups = set()
    if key:
        for iid, gid in gid_of.items():
            if _fold_hit(key, groups[gid]) or card_hit(iid):
                hit_groups.add(gid)

    # ── Süzgeçler ──
    visible: List[int] = []
    for iid in items:
        b = bucket_of[iid]
        if category != "all" and b != category:
            continue
        if priced(iid):
            if has_price == "unpriced":
                continue
            if source != "all" and not any(_source_bucket(sp.source) == source for sp in kept.get(iid, [])):
                continue
        else:
            # Fiyatsız kart: hammadde / ambalaj / etiket ("diğer" yalnız arama ya
            # da "diğer" süzgeciyle — plan onları hammadde sayıp bağlantı verir);
            # kaynak süzgecinde yok; ikizi fiyatlıysa planda fiyatı var
            if has_price == "priced" or source != "all":
                continue
            if b not in UNPRICED_BUCKETS and not (key or category == "other"):
                continue
            if has_price == "unpriced" and twin_priced(iid):
                continue
        if key and not (card_hit(iid) or gid_of.get(iid) in hit_groups):
            continue
        visible.append(iid)

    # Görünür kartlar + plan ikizleri (seçim kümenin bütün satırlarından)
    loaded = list(dict.fromkeys(visible + [t for iid in visible for t in twins.get(iid, ())]))
    vis_rows = [sp for iid in visible for sp in kept.get(iid, [])]
    all_rows = [sp for iid in loaded for sp in kept.get(iid, [])]
    pin = load_supplier_inputs(db, loaded, domain, all_rows)
    ix = supplier_index(pin)
    sc = _status_ctx(pin, ix, True)
    skip = {ix.key(s) for s in (pin.skip_suppliers or ())} - {""}

    # Kur: yalnız seçilenden farklı para birimli FİYATLI satır varsa, bir kez
    # (kur kutusu görünür satırlara göre; ikiz satırı da çevrilsin diye çekilir)
    def needs_fx(rows_) -> bool:
        return any((sp.currency or "USD").upper() != cur for sp in rows_
                   if sp.unit_price is not None and sp.unit_price > 0)
    need_fx = needs_fx(vis_rows)
    rates = None
    if need_fx or needs_fx(all_rows):
        try:
            rates = (rates_fn or _default_rates)()
        except Exception:
            rates = None
    from core.fx import convert

    def cluster_of(iid) -> List[int]:
        return sorted({iid, *twins.get(iid, ())})

    def card_rows(iid, rp) -> List[dict]:
        it = items[iid]
        rows = []
        for sp in kept.get(iid, []):
            name = disp_name(sp)
            # `source` / `quoted_at` planın eski fatura korumasına gider (parasut + 12 ay)
            o = {"supplier_key": ix.key(name), "supplier_id": sp.supplier_id, "name": name,
                 "source": sp.source, "quoted_at": sp.quoted_at}
            _offer_status(o, rp, sc, skip, pin)
            tags, blocked = offer_tags(o, vat=False)
            net = net_unit_price(sp)
            price = net if (net is not None and net > 0) else None
            conv, pu, note = price_per_purchase_unit({"unit_price": price, "price_unit": sp.price_unit}, it.unit)
            raw = conv                                    # alım birimine çevrilmiş, kur çevrilmemiş net
            ocur = (sp.currency or "USD").upper()
            if conv is not None and ocur != cur:
                if rates is None:
                    note = (note + " · " if note else "") + f"{ocur} fiyat çevrilemedi (kur yok)"
                    conv = None
                else:
                    try:
                        conv = convert(conv, ocur, cur, rates)
                    except (ValueError, TypeError):
                        note = (note + " · " if note else "") + f"{ocur} fiyat çevrilemedi"
                        conv = None
            row = serialize_price(sp, it, supplier_status=sup_status.get(sp.supplier_id),
                                  supplier_inactive=o["status"] == "inactive")
            row.update({
                "status": o["status"], "status_kind": _STATUS_KIND.get(o["status"], "normal"),
                "eligible": o["eligible"], "status_tags": tags, "status_blocked": blocked,
                "status_reason": (sc.reasons.get(o["supplier_key"]) if o["status"] == "phase_out" else None),
                "source_filter": _source_bucket(sp.source),
                "net": conv, "net_unit": pu,
                "net_text": _unit_price_text(conv, cur, pu) if conv is not None else None,
                "net_note": note if note != LITRE_KG_NOTE else None,
                "litre_kg": note == LITRE_KG_NOTE,
                "orig_text": (_unit_price_text(sp.unit_price, ocur, sp.price_unit)
                              if sp.unit_price is not None else None),
                "vat_label": vat_label(sp),
                "package_text": package_text(sp, it.unit),
                "date_text": _dmy(sp.quoted_at),
                "cheapest": False, "plan_pick": False,
                "_tier": o["tier"], "_raw": raw, "_cur": ocur,
                "_pu": (sp.price_unit or "").strip().lower(),
            })
            rows.append(row)
        return rows

    # Satırlar: kart başına; durum / tercih kümenin (planın birleşik satırı)
    rows_of: Dict[int, List[dict]] = {}
    prefs_of: Dict[int, dict] = {}
    for iid in loaded:
        rp = _row_prefs({"member_ids": cluster_of(iid)}, pin, ix)
        prefs_of[iid] = rp
        rows_of[iid] = card_rows(iid, rp)
    # Planın seçeceği teklif: küme başına BİR kez, bütün üyelerin satırlarından
    done = set()
    for iid in visible:
        cl = tuple(cluster_of(iid))
        if cl in done:
            continue
        done.add(cl)
        pickable = [r for m in cl for r in rows_of.get(m, []) if r["eligible"] and r["net"] is not None]
        if pickable:
            min(pickable, key=lambda r: (r["_tier"], r["net"], normalize(r["supplier_name"])))["plan_pick"] = True

    cards: Dict[int, dict] = {}
    n_rows = 0
    for iid in visible:
        it = items[iid]
        rp = prefs_of[iid]
        pu_card = _purchase_unit(it.unit)
        rows = rows_of[iid]
        # Karşılaştırma: pasif olmayan, alım birimine çevrilebilen fiyatlı satırlar
        # Pasif firma ve eski fatura karşılaştırmaya / "en ucuz"a girmez (plan da seçmez)
        live = [r for r in rows if r["_raw"] is not None and r["status"] not in ("inactive", "stale")]
        units = {r["_pu"] for r in live if r["_pu"]}
        curs = {r["_cur"] for r in live}
        compare_note = None
        if len(units) > 1:
            comparable = False
            compare_note = "Farklı fiyat birimi (" + " / ".join(UNIT_TEXT.get(u, u) for u in sorted(units)) + \
                           ") — karşılaştırılamaz"
        elif len(curs) > 1 and rates is None:
            comparable = False
            compare_note = "Farklı para birimi, kur yok — karşılaştırılamaz"
        else:
            comparable = True

        def cmp_key(r):
            return r["net"] if r["net"] is not None else r["_raw"]
        if comparable and len(live) >= 2:
            best = min(live, key=lambda r: (cmp_key(r), normalize(r["supplier_name"])))
            best["cheapest"] = True
        rows.sort(key=lambda r: (r["status"] == "inactive", not r["eligible"], r["_raw"] is None,
                                 cmp_key(r) if (comparable and r["_raw"] is not None) else 0.0,
                                 normalize(r["supplier_name"])))
        for r in rows:
            for k in ("_tier", "_raw", "_cur", "_pu"):
                r.pop(k, None)
        # Kaynak süzgeci satırları daraltır; karşılaştırma bütün satırlarla yapıldı
        n_off = 0
        if source != "all":
            shown = [r for r in rows if r["source_filter"] == source]
            n_off, rows = len(rows) - len(shown), shown
        n_rows += len(rows)
        gid = gid_of.get(iid)
        nh = hidden.get(iid, 0)
        pref_bits = []
        if rp["preferred"]:
            pref_bits.append("Tercih: " + ", ".join(
                p["name"] + (f" ({p['rank']}.)" if len(rp["preferred"]) > 1 else "") for p in rp["preferred"]))
        if rp["avoid"]:
            pref_bits.append("Bu malzemede alma: " + ", ".join(p["name"] for p in rp["avoid"]))
        tw = [t for t in cluster_of(iid) if t != iid]
        tw_names = [items[t].name for t in tw]
        cards[iid] = {
            "item_id": iid, "name": it.name, "name_tr": it.name_tr or "", "sku": it.sku or "",
            "unit": it.unit or "", "category": it.category or "", "bucket": bucket_of[iid],
            "bucket_label": BUCKET_LABELS.get(bucket_of[iid], ""),
            "card_supplier": sup_names.get(it.supplier_id) if it.supplier_id else None,
            "purchase_unit": pu_card, "priced": priced(iid),
            "group_id": gid, "group_name": groups.get(gid) if gid else None,
            "rows": rows, "hidden_rows": nh,
            "hidden_text": (f"Aynı firmanın {nh} içe aktarılmış satırı elle girilen fiyatla gizlendi."
                            if nh else None),
            "filtered_rows": n_off,
            "filtered_text": (f"Kaynak süzgecinin dışında {n_off} fiyat satırı daha var "
                              f"(en ucuz karşılaştırması onlarla birlikte yapıldı)." if n_off else None),
            "comparable": comparable, "compare_note": compare_note,
            "pref_text": " · ".join(pref_bits) or None,
            "merged_with": [{"item_id": t, "name": items[t].name} for t in tw],
            "twin_priced": twin_priced(iid),
            "merge_text": ("Satın Alma Planı bu kartı " + ", ".join(tw_names) + " ile tek satırda birleştirir; "
                           "seçilen teklif kartların bütün fiyatları arasından çıkar." if tw else None),
        }

    # ── Bloklar: grup (≥2 görünür kart) → kartlar; diğerleri tek kart ──
    members: Dict[int, List[int]] = {}
    for iid in visible:
        if gid_of.get(iid):
            members.setdefault(gid_of[iid], []).append(iid)
    blocks = []
    used = set()
    for gid, ids in members.items():
        if len(ids) < 2:
            continue
        ids.sort(key=lambda i: (normalize(items[i].name), i))
        blocks.append({"group": {"id": gid, "name": groups[gid]}, "cards": [cards[i] for i in ids],
                       "_sort": normalize(groups[gid])})
        used.update(ids)
    for iid in visible:
        if iid not in used:
            blocks.append({"group": None, "cards": [cards[iid]], "_sort": normalize(items[iid].name)})
    blocks.sort(key=lambda b: (b["_sort"], b["cards"][0]["item_id"]))
    for b in blocks:
        b.pop("_sort")

    n_priced = sum(1 for c in cards.values() if c["priced"])
    n_unpriced = sum(1 for c in cards.values() if not c["priced"] and not c["twin_priced"])
    n_groups = sum(1 for b in blocks if b["group"])
    n_hidden = sum(c["hidden_rows"] for c in cards.values())
    bits = [f"{tr_num(len(cards))} malzeme", f"{tr_num(n_rows)} fiyat satırı"]
    if n_unpriced:
        bits.append(f"{tr_num(n_unpriced)} fiyatsız")
    if n_groups:
        bits.append(f"{tr_num(n_groups)} aynı malzeme grubu")
    return {
        "currency": cur, "symbol": CURRENCY_SYMBOL.get(cur, cur),
        "fx": fx_view(rates, need_fx),
        "blocks": blocks,
        "summary": {"cards": len(cards), "priced_cards": n_priced, "unpriced_cards": n_unpriced,
                    "rows": n_rows, "hidden_rows": n_hidden, "groups": n_groups},
        "summary_text": " · ".join(bits),
        "filters": {"q": q or "", "category": category, "has_price": has_price, "source": source},
        "options": {
            "currencies": list(CURRENCIES),
            "categories": [{"key": k, "label": "Hepsi" if k == "all" else BUCKET_LABELS[k]}
                           for k in CATEGORY_FILTERS],
            "has_price": [{"key": k, "label": HAS_PRICE_LABELS[k]} for k in HAS_PRICE_FILTERS],
            "sources": [{"key": "all", "label": "Hepsi"}] + [{"key": k, "label": SOURCE_FILTER_LABELS[k]}
                                                              for k in SOURCE_FILTERS if k != "all"],
        },
    }
