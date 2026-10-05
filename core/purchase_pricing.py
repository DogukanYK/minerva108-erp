# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Satın Alma Planı — fiyat + tedarikçi katmanı.

`core/purchase_plan.compute()` çıktısını (malzeme satırları) fiyatlandırır ve
"kimlerle görüşeceğiz" rehberini kurar.  Rusya listesinin
`fiyat/fiyatli_liste.py` betiğinin sistemleştirilmiş hâli; Paraşüt faturası
fiyatları SONRAKİ AŞAMA (burada `invoice` grubu yalnız ayrılmış bir addır).

  • load_price_inputs(db, item_ids, domain) → PriceInputs   (TEK DB okuyucu)
  • attach(result, pin, rates, currency=…)  → result         (SAF, yerinde)
  • sections(result)                         → bölüm listesi (UI/PDF/Excel'in
    TEK numaralandırma kaynağı; boş bölüm atlanır)

Kurallar:
  • Teklifler birleşmiş kartların HEPSİNDEN toplanır (aynı malzemenin fiyatı
    ikinci kartta duruyorsa — ALEOVERA vakası — kaybolmasın).
  • Her teklif `supplier_prices.price_per_purchase_unit` ile malzemenin alım
    birimine (kg / l / adet) normalize edilir, `core.fx.convert` ile rapor
    para birimine çevrilir; fiyatlılar ucuzdan pahalıya, fiyatsızlar sonda.
  • Tutar = round_half_up(alınacak × birim fiyat) — Excel ROUND ile aynı
    (Python `round` bankacı yuvarlaması yapar, .5'te Excel'den sapardı).
  • İlişkiler: stok kartında yazan (`Item.supplier_id`), son alım (numune ve
    numuneden çevrilen lotlar HARİÇ), numune gönderen.  AppSetting
    `purchase_plan.skip_suppliers` listesi (BİLİNMEYEN / MİNERVA / NUMUNE
    GÖNDERİM) ilişki sayılmaz.
"""
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional, Set, Tuple

from core.purchase_plan import LotRec, SAMPLE_CONVERTED_MARK, alnum_fold, clean_text, tr_num
from core.supplier_prices import LITRE_KG_NOTE, price_per_purchase_unit

CFG_SKIP_SUPPLIERS = "purchase_plan.skip_suppliers"
DEFAULT_SKIP_SUPPLIERS = ("BİLİNMEYEN", "MİNERVA", "NUMUNE GÖNDERİM")

CURRENCY_SYMBOL = {"USD": "$", "EUR": "€", "TRY": "₺"}
CURRENCY_DATIVE = {"USD": "dolara", "EUR": "avroya", "TRY": "TL'ye"}   # "… fiyatlar dolara çevrildi"
UNIT_TEXT = {"kg": "kg", "l": "litre", "adet": "adet"}

# supplier_key: ilk ANLAMLI kelime ≥6 harfse firma anahtarı odur (uzun ticari
# unvanlar: "NATURALYA DOĞAL ÜRÜNLER SAN. TİC. LTD." = "NATURALYA").  Şehir
# ve jenerik kelimeler anlamlı sayılmaz — "İSTANBUL KİMYA" ile "İSTANBUL
# AMBALAJ" aynı firma olmasın.
_SUP_STOP = {
    "ISTANBUL", "ANKARA", "IZMIR", "BURSA", "KOCAELI", "ANTALYA", "TURKIYE", "TURKEY",
    "KIMYA", "KIMYEVI", "KIMYEVIMADDE", "AMBALAJ", "PLASTIK", "KOZMETIK", "TICARET",
    "SANAYI", "LIMITED", "ANONIM", "SIRKETI", "GLOBAL", "INTERNATIONAL", "ULUSLARARASI",
    "NATURAL", "DOGAL", "ORGANIK", "TEKNIK", "URUNLERI", "GROUP", "GRUP", "HOLDING",
}
_SUP_SPLIT = re.compile(r"[\s\-/.,&()+]+")

SECTION_TITLES = {
    "raw_priced": "Hammadde — fiyat listesinde olanlar",
    "raw_unpriced": "Hammadde — fiyatı olmayanlar, teklif alınacak",
    "pkg_priced": "Ambalaj — fiyatlı",
    "pkg_unpriced": "Ambalaj — fiyatı olmayanlar",
    "labels": "Etiket",
    "new_items": "Yeni etiket / koli / palet — teklif alınacak",
    "suppliers": "Kimlerle görüşeceğiz — tedarikçi bazında",
    "candidates": "Fiyatı olmayanlar için teklif istenebilecek firmalar",
    "checklist": "Tedarikçi bilgi eksikleri",
    "products": "Ürünler, hedef adetler ve kapasite",
    "sufficient": "Yeterli olanlar",
    "notes": "Notlar ve kaynaklar",
}


# ─── Girdi kayıtları ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OfferRec:
    item_id: int
    supplier_name: Optional[str] = None
    supplier_id: Optional[int] = None
    unit_price: Optional[float] = None
    package_size: Optional[float] = None
    currency: str = "USD"
    price_unit: Optional[str] = None
    source: Optional[str] = None
    source_label: Optional[str] = None
    quoted_at: Optional[date] = None


@dataclass(frozen=True)
class SupplierRec:
    id: int
    name: str
    contact_person: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    is_active: bool = True


@dataclass
class PriceInputs:
    offers: Dict[int, List[OfferRec]] = field(default_factory=dict)
    suppliers: Dict[int, SupplierRec] = field(default_factory=dict)
    card_supplier: Dict[int, int] = field(default_factory=dict)     # item_id → supplier_id
    lots: Dict[int, List[LotRec]] = field(default_factory=dict)
    converted_lots: Set[Tuple[int, str]] = field(default_factory=set)
    skip_suppliers: Tuple[str, ...] = DEFAULT_SKIP_SUPPLIERS


# ─── Yardımcılar ────────────────────────────────────────────────────────────

def round_half_up(x) -> Optional[int]:
    """Excel ROUND(x, 0) ile aynı: .5 daima yukarı (Decimal(repr(x)) — float'ın
    kısa ondalık yazımı üzerinden, 2.675 gibi ikili temsil sürprizi olmadan)."""
    if x is None:
        return None
    return int(Decimal(repr(float(x))).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def _sup_tokens(name) -> Tuple[List[str], List[str]]:
    s = clean_text(name)
    if not s:
        return [], []
    toks = [t for t in (alnum_fold(x) for x in _SUP_SPLIT.split(s)) if t]
    sig = [t for t in toks if t not in _SUP_STOP and not t.isdigit()]
    return toks, sig


def supplier_key(name) -> str:
    """Tedarikçi eşleştirme anahtarı (tek ad üzerinden, saf).

    Harf+rakam katlaması (ULUDAĞ HERBAL = ULUDAG HERBAL); jenerik kelimeler
    (KİMYA, KİMYEVİ, şehir adları…) atılır (SURYA KİMYA = SURYA).  Uzun ticari
    unvanlarda ilk ANLAMLI kelime ≥6 harfse anahtar o kelimedir
    (NATURALYA… / NILKIM… / UMAYCHEM, BAŞAK ORGANİK → UMAYCHEM).
    Raporun bütün adları elde varsa `SupplierIndex` kullanılır (bitişik yazım:
    HAMMADDESEPETİ = HAMMADDE SEPETİ).
    """
    toks, sig = _sup_tokens(name)
    if not toks:
        return ""
    if sig and len(sig[0]) >= 6:
        return sig[0]
    return "".join(sig) or "".join(toks)


class SupplierIndex:
    """Bir rapordaki bütün tedarikçi adlarını (IMS kartları, fiyat listesi,
    lotlar) tek anahtar uzayında birleştirir.  İki ad aynı firmadır, eğer
    (a) jenerik kelimeleri atılmış tam katlamaları eşitse (bitişik/ayrı yazım:
    "HAMMADDESEPETİ" kartı = listedeki "HAMMADDE SEPETİ") ya da (b) ilk anlamlı
    kelimeleri ≥6 harf ve eşitse (uzun unvanlar).  Önek eşleşmesi BİLEREK yok:
    "HAMMADDELER.COM" ayrı bir firmadır.  Küme temsilcisi = üyelerin en kısa
    `supplier_key`'i."""

    def __init__(self, names=()):
        parent: Dict[str, str] = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a, b):
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[max(ra, rb)] = min(ra, rb)

        keys: Dict[str, str] = {}
        for n in names:
            nodes = self._nodes(n)
            if not nodes:
                continue
            for x in nodes[1:]:
                union(nodes[0], x)
            find(nodes[0])
            keys[nodes[0]] = supplier_key(n)
        rep: Dict[str, str] = {}
        for node, k in keys.items():
            r = find(node)
            if r not in rep or (len(k), k) < (len(rep[r]), rep[r]):
                rep[r] = k
        self._parent, self._find, self._rep = parent, find, rep

    @staticmethod
    def _nodes(name) -> List[str]:
        toks, sig = _sup_tokens(name)
        if not toks:
            return []
        out = ["F:" + ("".join(sig) or "".join(toks))]
        if sig and len(sig[0]) >= 6:
            out.append("S:" + sig[0])
        return out

    def key(self, name) -> str:
        for node in self._nodes(name):
            if node in self._parent:
                return self._rep.get(self._find(node), supplier_key(name))
        return supplier_key(name)


def money(v, currency: str = "USD") -> str:
    """Tam para birimi: '7.088 $'."""
    if v is None:
        return "—"
    return f"{tr_num(v)} {CURRENCY_SYMBOL.get(currency, currency)}"


def price_text(v) -> str:
    """Birim fiyat: 1'in altındaysa 4 hane (0,1193), değilse 2 hane."""
    return tr_num(v, 4) if v < 1 else tr_num(v, 2)


def unpriced_offer_text(o: dict) -> str:
    """Fiyatı kullanılamayan teklifin satırı (PDF + Excel aynı metin).

    Teklifin notu varsa NEDEN odur — birim uyuşmazlığı ("fiyat birimi (kg)
    … uyuşmuyor") ya da kur yok; yalnız gerçekten boş fiyatta "fiyat
    yazılmamış".  Aksi halde listede fiyatı OLAN teklif satın almaya
    "fiyat yok" diye gider (adet kartı + kg fiyatı vakası)."""
    why = (o.get("note") or "").strip() or "fiyat yazılmamış"
    if why[:1].isupper() and why[1:2].islower():       # "Fiyat birimi" → "fiyat birimi"; "USD …" kalır
        why = {"I": "ı", "İ": "i"}.get(why[0], why[0].lower()) + why[1:]
    return f"{o.get('name') or '—'} — fiyat listesinde, {why}"


def _phone(p) -> str:
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d.startswith("0"):
        return f"{d[:4]} {d[4:7]} {d[7:9]} {d[9:]}"
    return (p or "").strip()


def _d(v) -> Optional[date]:
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date()
    return v


def _dmy(v) -> str:
    v = _d(v)
    return v.strftime("%d.%m.%Y") if v else ""


# ─── DB yükleyici ───────────────────────────────────────────────────────────

def load_price_inputs(db, item_ids_incl_members, domain: str) -> PriceInputs:
    """Fiyat + ilişki girdileri — hepsi aktif panele kapsanır.

    `item_ids_incl_members`: plandaki malzemelerin BÜTÜN üye kartları
    (`member_ids`), teklifler bunların birleşimidir.
    """
    from database import AppSetting, Inventory, Item, Supplier, SupplierPrice, Transaction

    ids = sorted({int(i) for i in (item_ids_incl_members or []) if i is not None})
    suppliers = {s.id: SupplierRec(id=s.id, name=s.name or "", contact_person=s.contact_person,
                                   phone=s.phone, email=s.email, address=s.address,
                                   is_active=bool(s.is_active) if s.is_active is not None else True)
                 for s in db.query(Supplier).filter(Supplier.domain == domain).all()}
    pin = PriceInputs(suppliers=suppliers)
    row = db.query(AppSetting).filter(AppSetting.key == CFG_SKIP_SUPPLIERS).first()
    if row and row.value is not None:
        pin.skip_suppliers = tuple(x.strip() for x in row.value.split(",") if x.strip())
    if not ids:
        return pin
    for sp in (db.query(SupplierPrice)
               .filter(SupplierPrice.item_id.in_(ids), SupplierPrice.domain == domain)
               .order_by(SupplierPrice.id.asc()).all()):
        name = sp.supplier_name or (suppliers[sp.supplier_id].name if sp.supplier_id in suppliers else None)
        pin.offers.setdefault(sp.item_id, []).append(OfferRec(
            item_id=sp.item_id, supplier_name=name, supplier_id=sp.supplier_id,
            unit_price=sp.unit_price, package_size=sp.package_size,
            currency=(sp.currency or "USD"), price_unit=sp.price_unit, source=sp.source,
            source_label=sp.source_label, quoted_at=sp.quoted_at))
    for iid, sid in (db.query(Item.id, Item.supplier_id)
                     .filter(Item.id.in_(ids), Item.domain == domain,
                             Item.supplier_id.isnot(None)).all()):
        pin.card_supplier[iid] = sid
    for (iid, lot_no, sid, is_s, cat, qty) in (
            db.query(Inventory.item_id, Inventory.lot_number, Inventory.supplier_id,
                     Inventory.is_sample, Inventory.created_at, Inventory.quantity)
            .filter(Inventory.item_id.in_(ids), Inventory.domain == domain).all()):
        pin.lots.setdefault(iid, []).append(LotRec(
            item_id=iid, lot_number=lot_no or "", supplier_id=sid,
            supplier_name=suppliers[sid].name if sid in suppliers else None,
            is_sample=bool(is_s), created_at=cat, quantity=float(qty or 0.0)))
    pin.converted_lots = {(iid, lot or "") for (iid, lot) in
                          db.query(Transaction.item_id, Transaction.lot_number)
                          .filter(Transaction.item_id.in_(ids),
                                  Transaction.notes.like(f"%{SAMPLE_CONVERTED_MARK}%")).all()}
    return pin


# ─── Fiyatlandırma ──────────────────────────────────────────────────────────

def _offer_name(o: OfferRec, pin: PriceInputs) -> str:
    if clean_text(o.supplier_name):
        return clean_text(o.supplier_name)
    if o.supplier_id in pin.suppliers:
        return clean_text(pin.suppliers[o.supplier_id].name) or "—"
    return "—"


def _lot_name(l: LotRec, pin: PriceInputs) -> Optional[str]:
    if l.supplier_id in pin.suppliers:
        return clean_text(pin.suppliers[l.supplier_id].name)
    return clean_text(l.supplier_name)


def _relations(member_ids: List[int], pin: PriceInputs, skip: Set[str], ix: "SupplierIndex") -> dict:
    """Malzemenin tedarikçi ilişkileri (bütün üye kartlar üzerinden)."""
    card, seen = [], set()
    for i in member_ids:
        sid = pin.card_supplier.get(i)
        if sid is None or sid not in pin.suppliers:
            continue
        nm = clean_text(pin.suppliers[sid].name)
        k = ix.key(nm)
        if not k or k in skip or k in seen:
            continue
        seen.add(k)
        card.append({"name": nm, "key": k, "supplier_id": sid})
    last, samples, sseen = None, [], set()
    for i in member_ids:
        for l in pin.lots.get(i, []):
            nm = _lot_name(l, pin)
            k = ix.key(nm)
            if not k or k in skip:
                continue
            if l.is_sample or (l.item_id, l.lot_number or "") in pin.converted_lots:
                sk = (k, _d(l.created_at))
                if sk not in sseen:
                    sseen.add(sk)
                    samples.append({"name": nm, "key": k, "date": _dmy(l.created_at)})
                continue
            at = l.created_at or datetime.min
            if last is None or at > last["_at"]:
                last = {"name": nm, "key": k, "date": _dmy(l.created_at), "_at": at}
    if last:
        last.pop("_at")
    samples.sort(key=lambda s: (s["name"], s["date"]))
    return {"card": card, "last": last, "samples": samples}


def _price_row(m: dict, pin: PriceInputs, rates, currency: str, round_to_package: bool,
               fx_used: Set[str], sources: dict, flags: dict, skip: Set[str],
               ix: "SupplierIndex") -> None:
    unit = m["unit"]
    offers, seen = [], set()
    for mid in m.get("member_ids") or []:
        for o in pin.offers.get(mid, []):
            name = _offer_name(o, pin)
            price = o.unit_price if (o.unit_price is not None and o.unit_price > 0) else None
            conv, pu, note = price_per_purchase_unit(
                {"unit_price": price, "price_unit": o.price_unit}, unit)
            ocur = (o.currency or "USD").upper()
            if conv is not None and ocur != currency:
                if rates is None:
                    note = (note + " · " if note else "") + f"{ocur} fiyat çevrilemedi (kur yok)"
                    conv = None
                else:
                    from core.fx import convert
                    conv = convert(conv, ocur, currency, rates)
                    fx_used.add(ocur)
            k = ix.key(name)
            dkey = (k, round(conv, 6) if conv is not None else None, o.package_size)
            if dkey in seen:
                continue
            seen.add(dkey)
            if note == LITRE_KG_NOTE and conv is not None:
                flags["litre_kg"] = True
            if conv is not None:
                skey = (o.source or "", o.source_label or "", o.quoted_at, ocur, o.price_unit or "")
                sources[skey] = sources.get(skey, 0) + 1
            offers.append({"name": name, "supplier_key": k, "supplier_id": o.supplier_id,
                           "item_id": o.item_id, "price": conv, "price_unit": pu,
                           "orig_price": o.unit_price, "orig_currency": ocur,
                           "orig_price_unit": o.price_unit, "package": o.package_size,
                           "currency": currency, "note": note, "source": o.source,
                           "source_label": o.source_label,
                           "quoted_at": o.quoted_at.isoformat() if o.quoted_at else None})
    offers.sort(key=lambda x: (x["price"] is None, x["price"] if x["price"] is not None else 0.0,
                               x["name"]))
    best = offers[0] if offers and offers[0]["price"] is not None else None
    buy_num = float(m["display"]["buy_num"] or 0.0)
    m["offers"] = offers
    m["group"] = "list" if best else "none"
    m["price"] = best["price"] if best else None
    m["price_unit"] = (best["price_unit"] if best else
                       {"kg": "kg", "l": "l"}.get(m["display"].get("buy_unit"), "adet"))
    m["supplier"] = best["name"] if best else None
    m["supplier_key"] = best["supplier_key"] if best else None
    m["price_source"] = (best["source_label"] or ("Fiyat listesi" if best["source"] else "Fiyat kaydı")) if best else None
    m["amount"] = (round_half_up(buy_num * best["price"])
                   if best and m["status"] == "to_buy" else None)
    m["pkg_buy"] = m["pkg_amount"] = None
    if round_to_package and best and best.get("package") and best["package"] > 0 and m["status"] == "to_buy":
        pkg = float(best["package"])
        m["pkg_buy"] = round(math.ceil(buy_num / pkg - 1e-9) * pkg, 6)
        m["pkg_amount"] = round_half_up(m["pkg_buy"] * best["price"])
    m["relations"] = _relations(m.get("member_ids") or [], pin, skip, ix)


def attach(result: dict, pin: PriceInputs, rates, *, currency: str = "USD",
           round_to_package: bool = False) -> dict:
    """Fiyat + tedarikçi alanlarını `result`'a yazar (yerinde) ve döndürür.

    `rates` (`core.fx.Rates`) yalnız para birimi farklı teklif varsa gerekir;
    None iken çevrilemeyen teklif fiyatsız sayılır (notla).
    """
    currency = (currency or "USD").upper()
    ix = supplier_index(pin)
    skip = {ix.key(s) for s in (pin.skip_suppliers or ())}
    skip.discard("")
    fx_used: Set[str] = set()
    sources: dict = {}
    flags = {"litre_kg": False}
    for m in result.get("materials", []):
        _price_row(m, pin, rates, currency, round_to_package, fx_used, sources, flags, skip, ix)
    for m in result.get("held", []):
        _price_row(m, pin, rates, currency, round_to_package, fx_used, sources, flags, skip, ix)

    mats = [m for m in result.get("materials", []) if m["status"] == "to_buy"]
    totals = {k: sum(m["amount"] or 0 for m in mats if m["kind"] == k) for k in ("raw", "packaging", "label")}
    totals["all"] = sum(totals.values())
    counts = {}
    for k in ("raw", "packaging", "label"):
        counts[f"{k}_priced"] = sum(1 for m in mats if m["kind"] == k and m["group"] == "list")
        counts[f"{k}_unpriced"] = sum(1 for m in mats if m["kind"] == k and m["group"] != "list")
    fx = None
    if fx_used:
        fx = {"used": True, "currencies": sorted(fx_used),
              "source": getattr(rates, "source", None),
              "as_of": rates.as_of.isoformat() if getattr(rates, "as_of", None) else None,
              "stale": bool(getattr(rates, "stale", False)),
              "warning": getattr(rates, "warning", None),
              "try_per": {c: rates.rate(c) for c in sorted(fx_used | {currency})}}
    result["pricing"] = {
        "currency": currency, "symbol": CURRENCY_SYMBOL.get(currency, currency),
        "fx": fx, "litre_kg_used": flags["litre_kg"], "round_to_package": bool(round_to_package),
        "sources": [{"source": s or None, "source_label": lbl or None,
                     "quoted_at": q.isoformat() if q else None, "currency": c,
                     "price_unit": pu or None, "offers": n}
                    for (s, lbl, q, c, pu), n in sorted(sources.items(), key=lambda kv: -kv[1])],
        "totals": totals, "counts": counts,
        "held_what_if": sum(m.get("amount") or 0 for m in result.get("held", [])),
    }
    result["suppliers"] = supplier_directory(result, pin, skip, ix)
    return result


def supplier_index(pin: PriceInputs) -> SupplierIndex:
    """Rapordaki bütün tedarikçi adlarından ortak anahtar uzayı."""
    names = [s.name for s in pin.suppliers.values()]
    names += [o.supplier_name for lst in pin.offers.values() for o in lst if o.supplier_name]
    names += [l.supplier_name for lst in pin.lots.values() for l in lst if l.supplier_name]
    names += list(pin.skip_suppliers or ())
    return SupplierIndex(names)


# ─── Tedarikçi rehberi ──────────────────────────────────────────────────────

def _contacts(pin: PriceInputs, ix: SupplierIndex) -> Dict[str, dict]:
    """IMS tedarikçi kartlarından supplier_key başına birleşik iletişim
    (ilk dolu değer kazanır; kart adları kopya-kart kontrolü için tutulur)."""
    out: Dict[str, dict] = {}
    for sid in sorted(pin.suppliers):
        s = pin.suppliers[sid]
        k = ix.key(s.name)
        if not k:
            continue
        c = out.setdefault(k, {"name": clean_text(s.name), "person": "", "phone": "", "email": "",
                               "address": "", "cards": [], "card_ids": []})
        c["cards"].append(clean_text(s.name))
        c["card_ids"].append(sid)
        for f_, v in (("person", s.contact_person), ("phone", _phone(s.phone)),
                      ("email", s.email), ("address", s.address)):
            if v and str(v).strip() and not c[f_]:
                c[f_] = " ".join(str(v).split())
    return out


def contact_line(c: Optional[dict]) -> Optional[str]:
    if not c:
        return None
    parts = [x for x in (c.get("person"), c.get("phone"), c.get("email"), c.get("address")) if x]
    return " · ".join(parts) if parts else None


def supplier_directory(result: dict, pin: PriceInputs, skip: Optional[Set[str]] = None,
                       ix: Optional[SupplierIndex] = None) -> dict:
    """Seçilen (en ucuz) tedarikçi blokları + fiyatsızlar için aday firmalar +
    hiçbir firmayla ilişkisi olmayanlar + kontrol listesi."""
    ix = ix or supplier_index(pin)
    contacts = _contacts(pin, ix)
    mats = [m for m in result.get("materials", []) if m["status"] == "to_buy"]
    names: Dict[str, str] = {}

    def disp(k, fallback):
        if k in contacts and contacts[k]["name"]:
            return contacts[k]["name"]
        return names.setdefault(k, fallback)

    chosen: Dict[str, dict] = {}
    for m in mats:
        if m.get("group") != "list" or not m.get("supplier_key"):
            continue
        k = m["supplier_key"]
        b = chosen.setdefault(k, {"key": k, "name": disp(k, m["supplier"]), "items": [], "total": 0})
        b["items"].append({"key": m["key"], "name": m["name"], "buy_text": m["display"]["buy_text"],
                           "price": m["price"], "price_unit": m["price_unit"], "amount": m["amount"]})
        b["total"] += m["amount"] or 0
    for b in chosen.values():
        b["items"].sort(key=lambda x: -(x["amount"] or 0))
        b["count"] = len(b["items"])
        c = contacts.get(b["key"])
        b["contact"] = c
        b["contact_line"] = contact_line(c)
    chosen_list = sorted(chosen.values(), key=lambda b: (-b["total"], b["name"]))

    cand: Dict[str, dict] = {}
    unrelated: List[dict] = []
    for m in mats:
        if m.get("group") == "list":
            continue
        rel = m.get("relations") or {"card": [], "last": None, "samples": []}
        ks: List[Tuple[str, str, str]] = []
        for c in rel["card"]:
            ks.append((c["key"], c["name"], "stok kartında yazan"))
        if rel["last"]:
            ks.append((rel["last"]["key"], rel["last"]["name"], f"son alım {rel['last']['date']}"))
        for s in rel["samples"]:
            ks.append((s["key"], s["name"], f"numune {s['date']}"))
        for o in m.get("offers") or []:
            if o["supplier_key"] and o["supplier_key"] not in (skip or set()):
                ks.append((o["supplier_key"], o["name"], "fiyat listesinde (fiyatsız)"))
        entry = {"key": m["key"], "name": m["name"], "buy_text": m["display"]["buy_text"]}
        seen: Dict[str, dict] = {}
        for k, nm, why in ks:
            if not k:
                continue
            if k in seen:
                if why not in seen[k]["reasons"]:
                    seen[k]["reasons"].append(why)
                continue
            b = cand.setdefault(k, {"key": k, "name": disp(k, nm), "items": []})
            it = dict(entry, reasons=[why])
            b["items"].append(it)
            seen[k] = it
        if not seen:
            unrelated.append(entry)
    for b in cand.values():
        c = contacts.get(b["key"])
        b["contact"] = c
        b["contact_line"] = contact_line(c)
        b["count"] = len(b["items"])
    cand_list = sorted(cand.values(), key=lambda b: (-b["count"], b["name"]))

    used = set(chosen) | set(cand)
    missing = sorted((k for k in used if not (contacts.get(k) or {}).get("phone")
                      and not (contacts.get(k) or {}).get("email")),
                     key=lambda k: disp(k, k))
    no_card = sorted((k for k in used if k not in contacts), key=lambda k: disp(k, k))
    dups = sorted(((k, contacts[k]["cards"]) for k in used
                   if k in contacts and len(contacts[k]["cards"]) > 1), key=lambda x: disp(x[0], x[0]))
    checklist = {
        "missing_contact": [{"key": k, "name": disp(k, k)} for k in missing],
        "no_card": [{"key": k, "name": disp(k, k)} for k in no_card],
        "duplicate_cards": [{"key": k, "name": disp(k, k), "cards": cs} for k, cs in dups],
    }
    return {"chosen": chosen_list, "candidates": cand_list, "unrelated": unrelated,
            "checklist": checklist}


# ─── Bölümler ───────────────────────────────────────────────────────────────

def _fmt_pct(v) -> str:
    return tr_num(v, 0 if float(v).is_integer() else 1)


def notes_lines(result: dict) -> List[str]:
    """12. bölüm — kaynaklar, kabuller, hariç/bekletilenler, kur, kullanıcı notları."""
    meta = result.get("meta", {})
    opts = meta.get("options", {})
    pr = result.get("pricing") or {}
    cur = pr.get("currency") or opts.get("currency") or "USD"
    out: List[str] = []
    srcs = pr.get("sources") or []
    if srcs:
        for s in srcs:
            lbl = s.get("source_label") or ("Stok Son Durum fiyat listesi"
                                            if s.get("source") == "stok_son_durum" else "Fiyat kaydı")
            when = f" ({_dmy(date.fromisoformat(s['quoted_at']))})" if s.get("quoted_at") else ""
            unit = UNIT_TEXT.get(s.get("price_unit") or "", s.get("price_unit") or "birim")
            out.append(f"Fiyat kaynağı: {lbl}{when}; birim fiyatlar "
                       f"{CURRENCY_SYMBOL.get(s.get('currency'), s.get('currency'))}/{unit}. "
                       "Fiyatlar güncelliğini yitirmiş olabilir; sipariş öncesi teyit edilmeli.")
    elif pr:
        out.append("Sistemde bu kalemler için fiyat kaydı yok; tüm kalemler için teklif alınacak.")
    if pr.get("litre_kg_used"):
        out.append("Sıvı malzemelerde 1 litre ≈ 1 kg kabul edildi (yaklaşık).")
    fx = pr.get("fx")
    if fx and fx.get("used"):
        rates = ", ".join(f"1 {c} = {tr_num(v, 4)} ₺" for c, v in (fx.get("try_per") or {}).items() if c != "TRY")
        src = "TCMB döviz satış" if fx.get("source") == "TCMB" else (fx.get("source") or "kur")
        when = f" ({_dmy(date.fromisoformat(fx['as_of']))})" if fx.get("as_of") else ""
        out.append(f"Farklı para birimindeki fiyatlar {CURRENCY_DATIVE.get(cur, cur)} çevrildi: {rates} — {src}{when}."
                   + (f" {fx['warning']}" if fx.get("warning") else ""))
    fires = (result.get("summary") or {}).get("fires") or []
    fire = ("Hammaddeye üretim firesi reçeteden eklendi"
            + (f" (%{'/%'.join(_fmt_pct(f) for f in fires)})" if fires else "")
            + "; ambalaj ve etikete fire payı eklenmedi.")
    w = opts.get("extra_waste_pct") or {}
    extra = [f"{lbl} %{_fmt_pct(w[k])}" for k, lbl in (("raw", "hammadde"), ("packaging", "ambalaj"),
                                                       ("label", "etiket")) if w.get(k)]
    if extra:
        fire += " Ek alım firesi: " + ", ".join(extra) + "."
    out.append(fire)
    out.append("Alınacak miktarlar güvenli yuvarlandı: gereken yukarı, elimizde aşağı."
               if opts.get("safe_rounding", True) else "Miktarlar en yakın değere yuvarlandı.")
    if pr:
        out.append("Tutar = alınacak × en ucuz birim fiyat. "
                   + ("Ambalaj katına yuvarlanmış miktar ve tutarı ayrıca gösterildi."
                      if pr.get("round_to_package") else
                      "Firmalar tam ambalaj sattığı için gerçek alım biraz daha fazla olabilir."))
    if opts.get("subtract_open_orders"):
        out.append("Açık siparişler (sipariş verildi işareti) yolda sayılıp alınacaktan düşüldü.")
    out.append("Numune lotları stoğa dahil değildir; eksi stoklu kartlar 0 sayıldı.")
    for e in result.get("excluded") or []:
        out.append(f"{e['name']} {e['need_text']} listede yok ({e['reason']}).")
    for h in result.get("held") or []:
        txt = f"{h['name']} ({h['display']['buy_text']}) "
        txt += (f"bekletiliyor: {h['reason'].rstrip('. ')}" if h.get("reason") else "teyit bekliyor")
        txt += "; listeye ve toplama girmedi."
        if h.get("amount") is not None and h.get("supplier"):
            txt += (f" Alınırsa en ucuz teklifle ({h['supplier']}, {price_text(h['price'])} "
                    f"{CURRENCY_SYMBOL.get(cur, cur)}/{UNIT_TEXT.get(h['price_unit'], h['price_unit'])}) "
                    f"yaklaşık {money(h['amount'], cur)} tutar.")
        out.append(txt)
    out.append(f"Stoklar {meta.get('stock_as_of_tr', '')} sistem kaydıdır.")
    out += [clean_text(n) for n in (opts.get("notes") or []) if clean_text(n)]
    return out


def sections(result: dict) -> List[dict]:
    """Bölüm sırası + numarası — UI, PDF ve Excel'in TEK kaynağı.

    Döner: [{key, no, title, subtitle, rows, total, count, summary}, …]
    Boş bölüm atlanır; numaralar atlamadan sonra verilir.
    """
    meta = result.get("meta", {})
    opts = meta.get("options", {})
    pr = result.get("pricing") or {}
    cur = pr.get("currency") or opts.get("currency") or "USD"
    mats = result.get("materials") or []
    to_buy = [m for m in mats if m["status"] == "to_buy"]

    def grp(m):
        return m.get("group") or "none"

    def by_amount(rows):
        return sorted(rows, key=lambda m: (-(m.get("amount") or 0), alnum_fold(m["name"])))

    def by_buy(rows):
        return sorted(rows, key=lambda m: (-float(m["display"]["buy_num"] or 0), alnum_fold(m["name"])))

    def tot(rows):
        return sum(m.get("amount") or 0 for m in rows)

    out: List[dict] = []

    def add(key, rows, *, subtitle=None, total=None, summary=None, title=None):
        if not rows:
            return
        out.append({"key": key, "no": len(out) + 1, "title": title or SECTION_TITLES[key],
                    "subtitle": subtitle, "rows": rows, "total": total, "count": len(rows),
                    "summary": summary})

    raw_p = by_amount([m for m in to_buy if m["kind"] == "raw" and grp(m) == "list"])
    raw_u = by_buy([m for m in to_buy if m["kind"] == "raw" and grp(m) != "list"])
    pkg_p = by_amount([m for m in to_buy if m["kind"] == "packaging" and grp(m) == "list"])
    pkg_u = by_buy([m for m in to_buy if m["kind"] == "packaging" and grp(m) != "list"])
    lbl = sorted([m for m in to_buy if m["kind"] == "label"],
                 key=lambda m: (grp(m) != "list", -(m.get("amount") or 0),
                                -float(m["display"]["buy_num"] or 0), alnum_fold(m["name"])))
    add("raw_priced", raw_p, total=tot(raw_p),
        summary=f"{len(raw_p)} kalem, toplam {money(tot(raw_p), cur)}",
        subtitle="Kalın yazılan tedarikçi en ucuz olanı; tutar onunla hesaplandı. "
                 "Gri satırlar diğer teklifler ve sistemdeki kayıtlar.")
    add("raw_unpriced", raw_u, summary=f"{len(raw_u)} kalem",
        subtitle="Satırda, sistemde bu malzeme için kayıtlı firma (stok kartı, son alım, numune) yazıyor.")
    add("pkg_priced", pkg_p, total=tot(pkg_p),
        summary=f"{len(pkg_p)} kalem, toplam {money(tot(pkg_p), cur)}")
    add("pkg_unpriced", pkg_u, summary=f"{len(pkg_u)} kalem")
    add("labels", lbl, total=tot(lbl) or None, summary=f"{len(lbl)} kalem")

    new_rows = ([dict(l, type="label") for l in result.get("labels_new") or []]
                + [dict(ml, type="manual") for ml in result.get("manual_lines") or []])
    add("new_items", new_rows, summary=f"{len(new_rows)} kalem",
        subtitle="Bu kalemlerin sistemde fiyatı ve stok kaydı yok; teklif alınacak.")

    sup = result.get("suppliers") or {}
    chosen = sup.get("chosen") or []
    add("suppliers", chosen, total=sum(b["total"] for b in chosen),
        summary=f"{len(chosen)} firma, toplam {money(sum(b['total'] for b in chosen), cur)}",
        subtitle="Her malzeme en ucuz teklifi veren firmanın altında.")
    cands = list(sup.get("candidates") or [])
    if sup.get("unrelated"):
        cands.append({"key": "__none__", "name": "Sistemde hiçbir firmayla ilişkisi yok",
                      "items": [dict(u, reasons=[]) for u in sup["unrelated"]],
                      "count": len(sup["unrelated"]), "contact": None,
                      "contact_line": "Yeni tedarikçi bulunmalı (fiyat listesindeki firmalara da sorulabilir).",
                      "none": True})
    add("candidates", cands, summary=f"{len(cands)} grup",
        subtitle="Bu firmalar sistemde o malzemeyle ilişkili görünüyor (stok kartı, son alım, numune). "
                 "Teklif istenmeli; bir malzeme birden fazla firmanın altında olabilir.")

    chk = sup.get("checklist") or {}
    chk_rows = []
    if chk.get("missing_contact"):
        chk_rows.append({"code": "missing_contact", "suppliers": chk["missing_contact"],
                         "text": "Telefonu ve e-postası sistemde olmayanlar: "
                                 + ", ".join(s["name"] for s in chk["missing_contact"]) + "."})
    if chk.get("no_card"):
        chk_rows.append({"code": "no_card", "suppliers": chk["no_card"],
                         "text": "Sistemde tedarikçi kartı hiç olmayanlar (yeni kart açılmalı): "
                                 + ", ".join(s["name"] for s in chk["no_card"]) + "."})
    if chk.get("duplicate_cards"):
        chk_rows.append({"code": "duplicate_cards", "suppliers": chk["duplicate_cards"],
                         "text": "Aynı firmanın birden fazla kartı var (bilgiyi tek karta girin): "
                                 + "; ".join(f"{s['name']}: " + " / ".join(f"“{c}”" for c in s["cards"])
                                             for s in chk["duplicate_cards"]) + "."})
    owner = clean_text(opts.get("checklist_owner"))
    add("checklist", chk_rows, title=SECTION_TITLES["checklist"] + (f" ({owner} için)" if owner else ""),
        subtitle="Bu listedeki firmaların bilgileri sisteme girilmeli.")

    prods = result.get("products") or []
    add("products", prods, summary=f"{len(prods)} ürün, toplam {tr_num(sum(p['qty'] for p in prods))} adet")
    if opts.get("stock_mode", "net") == "net":
        suff = sorted([m for m in mats if m["status"] == "yeterli"], key=lambda m: alnum_fold(m["name"]))
        add("sufficient", suff, summary=f"{len(suff)} kalem",
            subtitle="Elimizdeki stok yetiyor; alım gerekmez.")
    add("notes", notes_lines(result))
    return out
