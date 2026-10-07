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

  • load_price_inputs(db, item_ids, domain, extra_item_ids=(), offer_extra_ids=())
                                             → PriceInputs (TEK DB okuyucu)
  • attach(result, pin, rates, currency=…)  → result         (SAF, yerinde)
  • sections(result)                         → bölüm listesi (UI/PDF/Excel'in
    TEK numaralandırma kaynağı; boş bölüm atlanır)

Kurallar:
  • Teklifler birleşmiş kartların HEPSİNDEN toplanır (aynı malzemenin fiyatı
    ikinci kartta duruyorsa — ALEOVERA vakası — kaybolmasın).
  • Her teklif `supplier_prices.price_per_purchase_unit` ile malzemenin alım
    birimine (kg / l / adet) normalize edilir, `core.fx.convert` ile rapor
    para birimine çevrilir.
  • Teklif seçimi (07.10.2026, `respect_supplier_status`, varsayılan açık):
    uygun olmayanlar SEÇİLMEZ — firma "bitirilecek" (`Supplier.
    purchase_status`, firma anahtarı düzeyinde), bu malzemede "alma"
    (`material_supplier_prefs`), atlanacaklar listesi.  Uygunlar sırayla:
    malzeme tercihi (rank) → "tercih edilen" firma → normal; aynı kademede
    ucuzdan pahalıya, fiyatsızlar sonda.  Seçilen = ilk uygun FİYATLI teklif.
    Uygun olmayanlar gri etiketle listede kalır.  Durum/tercih yoksa seçim
    eskisiyle (en ucuz önce) birebir aynıdır; seçenek kapalıysa da öyle.
  • PASİF firma (yumuşak silinmiş — `FirmActivity`: firma anahtarında HİÇ
    yaşayan kart yok; mükerrer kartın pasif ikizi ve birleştirilmiş kartın
    adı sayılmaz, aktif karta bağlı teklif asla pasif değildir) seçenekten
    BAĞIMSIZ olarak seçilmez: gri "pasif tedarikçi" teklifi, aday firma da
    olmaz, iletişim/kopya-kart kontrolünde aktif kart esastır.  Raporlar
    paneli / Excel (`supplier_prices.inactive_firm_check`) aynı sınıfı
    kullanır.
  • Aynı kartta bir firmanın elle girilmiş (`manual`) fiyatı varsa o firmanın
    Excel teklifi sayılmaz (`supplier_prices.firm_keys` — Üretim Stok Analizi
    `prices_for_items` ile aynı kural).
  • Kural açıkken "aynı malzeme" grubundaki diğer kartların teklifleri de
    yüklenir (`offer_extra_ids`) — "eşdeğer kart «X»" etiketiyle
    (`via_item_id` / `via_name`).  Yalnız kendisi tercih kademesindeyse
    (malzeme tercihi / "tercih edilen" firma) ya da satırın kendi teklifleri
    durum / "alma" / atlanacaklar yüzünden elendiyse yarışır; yoksa gri bilgi
    (`status="equivalent"`) olarak yazılır.  Kural kapalıyken yüklenmez.
  • Tutar = round_half_up(alınacak × birim fiyat) — Excel ROUND ile aynı
    (Python `round` bankacı yuvarlaması yapar, .5'te Excel'den sapardı).
  • İlişkiler: stok kartında yazan (`Item.supplier_id`), son alım (numune ve
    numuneden çevrilen lotlar HARİÇ), numune gönderen.  AppSetting
    `purchase_plan.skip_suppliers` listesi (BİLİNMEYEN / MİNERVA / NUMUNE
    GÖNDERİM) ilişki sayılmaz.
  • Firma dökümü (`relations.firms`) bizim kayıtlarımızdan: stok kartı, alım
    (`Mal kabul` Input'u), stok aktarımı (IMP-/XLS-/Excel), numune (lot +
    analiz sonucu), sipariş işareti (`StockOrderFlag`, açık + kapalı), fiyat
    listesi.  Lot sınıflaması (`_RelIndex.lot_class`): numune → numune;
    kendi kartında (kart, lot) `Mal kabul` Input'u → ALIM (aynı lot no'lu
    çevrilmiş numune birleşse bile); kendi kartında çevirme Input'u →
    çevrilmiş numune; ilk geldiği kartta (`moved_from_item_id`) `Mal kabul`
    → ALIM; ilk karttaki çevirme Input'u / `sample_converted_at` → çevrilmiş
    numune; aktarım; kalan → `created_at` tarihli eski alım.  "Lot taşındı"
    Adjustment'ları alım sayılmaz (Input değil).
  • "Aynı malzeme" grubu (`material_group`, motor) → `relations.
    alternatives`: gruptaki diğer kartlar, tedarikçileri, stok/numune, son
    alım.  Fiyatsız kalemlerin aday firmalarına "eşdeğer kart" sebebiyle
    girer; ihtiyacı DEĞİŞTİRMEZ.  `relation_texts(m)` önizleme / PDF / Excel
    metnini tek kaynaktan üretir.
"""
import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional, Set, Tuple

from core.purchase_plan import LotRec, SAMPLE_CONVERTED_MARK, _amount_near, alnum_fold, clean_text, tr_num
from core.supplier_prices import LITRE_KG_NOTE, SOURCE_MANUAL, firm_keys, price_per_purchase_unit

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
    status: str = "normal"                     # normal | preferred | phase_out (core/suppliers)
    status_reason: Optional[str] = None
    merged_into_id: Optional[int] = None       # birleştirmenin kazananı (adı takma ad)


@dataclass(frozen=True)
class ReceiptRec:
    """Stok girişi (Input Transaction) — `classify_input` türüyle."""
    item_id: int
    lot: str = ""
    qty: float = 0.0
    at: Optional[datetime] = None
    kind: str = "other"           # purchase | converted | import | return | production | move | other


@dataclass(frozen=True)
class OrderRec:
    """`StockOrderFlag` — "sipariş verildi" işareti (açık ve kapalı)."""
    item_id: int
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    quantity: Optional[float] = None
    unit: Optional[str] = None
    ordered_at: Optional[datetime] = None
    expected_date: Optional[date] = None
    closed_at: Optional[datetime] = None
    closed_reason: Optional[str] = None        # received | manual


@dataclass(frozen=True)
class AnalysisRec:
    """Numune Analiz Formu (FR.KK.01) bileşen satırı ⨝ aktif analiz."""
    item_id: int
    inventory_id: Optional[int] = None
    lot: str = ""
    result: Optional[str] = None               # uygun | uygun_degil | None = beklemede
    document_no: Optional[str] = None
    at: Optional[datetime] = None


@dataclass(frozen=True)
class LotMetaRec:
    """Lotun izi: ilk geldiği kart (taşındıysa) + numuneden çevrildiği an."""
    moved_from_item_id: Optional[int] = None
    sample_converted_at: Optional[datetime] = None


@dataclass
class PriceInputs:
    offers: Dict[int, List[OfferRec]] = field(default_factory=dict)
    suppliers: Dict[int, SupplierRec] = field(default_factory=dict)
    card_supplier: Dict[int, int] = field(default_factory=dict)     # item_id → supplier_id
    lots: Dict[int, List[LotRec]] = field(default_factory=dict)
    converted_lots: Set[Tuple[int, str]] = field(default_factory=set)
    skip_suppliers: Tuple[str, ...] = DEFAULT_SKIP_SUPPLIERS
    receipts: Dict[int, List[ReceiptRec]] = field(default_factory=dict)    # item_id → Input'lar
    orders: Dict[int, List[OrderRec]] = field(default_factory=dict)        # item_id → işaretler
    analyses: Dict[int, List[AnalysisRec]] = field(default_factory=dict)   # item_id → analiz satırları
    lot_meta: Dict[int, LotMetaRec] = field(default_factory=dict)          # inventory id → iz
    # Malzeme bazlı tedarikçi tercihi: kart → [(supplier_id, preferred|avoid, rank)]
    # (grup tercihleri kartlara açılmış; kartın kendi satırı grubunkini ezer)
    prefs: Dict[int, List[Tuple[int, str, int]]] = field(default_factory=dict)


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
    tags, blocked = offer_tags(o)                      # tercih / eşdeğer kart / bitirilecek
    return _with_tags(f"{o.get('name') or '—'} — fiyat listesinde, {why}", tags, blocked)


def _phone(p) -> str:
    d = re.sub(r"\D", "", p or "")
    if len(d) == 11 and d.startswith("0"):
        return f"{d[:4]} {d[4:7]} {d[7:9]} {d[9:]}"
    return (p or "").strip()


def _dmy(v) -> str:
    """dd.mm.yyyy — datetime UTC saklanır, TR gününe çevrilir (23:30 UTC
    ertesi gündür); düz `date` değerleri (teklif/beklenen tarih) kaydırılmaz."""
    if v is None:
        return ""
    if isinstance(v, datetime):
        from database import to_tr
        v = to_tr(v).date()
    return v.strftime("%d.%m.%Y")


def _dated(label: str, d) -> str:
    return f"{label} {d}" if d else label


def classify_input(note, lot=None) -> str:
    """Input Transaction'ının türü — not ve lot no'sundan:
    "Mal kabul" → purchase, "Numune stoğa çevrildi" → converted, IMP-/XLS- lot
    ya da Excel/Import notu → import, "İade" → return, "Üretim çıktısı" →
    production, "Lot taşındı" → move (alım SAYILMAZ), diğer → other."""
    from core.stock_lots import LOT_MOVE_MARK
    n = " ".join(str(note or "").split())
    lt = str(lot or "").strip().upper()
    if n.startswith("Mal kabul"):
        return "purchase"
    if SAMPLE_CONVERTED_MARK in n:
        return "converted"
    if LOT_MOVE_MARK in n:
        return "move"
    if lt.startswith(("IMP-", "XLS-")) or re.search(r"excel|import|içe aktar", n, re.I):
        return "import"
    if n.startswith("İade"):
        return "return"
    if n.startswith("Üretim çıktısı"):
        return "production"
    return "other"


# ─── DB yükleyici ───────────────────────────────────────────────────────────

def load_price_inputs(db, item_ids_incl_members, domain: str, extra_item_ids=(),
                      offer_extra_ids=()) -> PriceInputs:
    """Fiyat + ilişki girdileri — hepsi aktif panele kapsanır.

    `item_ids_incl_members`: plandaki malzemelerin BÜTÜN üye kartları
    (`member_ids`), teklifler bunların birleşimidir.  `extra_item_ids`:
    "aynı malzeme" grubundaki diğer kartlar — ilişki için (kart tedarikçisi,
    lotlar, girişler, siparişler, analizler).  `offer_extra_ids`: teklifleri
    DE yüklenecek ek kartlar (satın alma planı grubun alternatif kartlarını
    verir — "eşdeğer kart" teklifi); ilişki için de okunur.  Taşınmış lotların
    ilk geldiği kartların (`moved_from_item_id`) Input'ları da okunur: alım
    geçmişi orada.  Tedarikçi durumu (`purchase_status`) ve malzeme
    tercihleri (`material_supplier_prefs`, grup → kart açılmış) da yüklenir.
    """
    from sqlalchemy import or_

    from core.suppliers import effective_prefs, normalize_status
    from database import (AppSetting, Inventory, Item, MaterialGroup, MaterialSupplierPref, SampleAnalysis,
                          SampleAnalysisIngredient, StockOrderFlag, Supplier, SupplierPrice, Transaction)

    offer_extra = {int(i) for i in (offer_extra_ids or []) if i is not None}
    ids = sorted({int(i) for i in (item_ids_incl_members or []) if i is not None})
    offer_ids = sorted(set(ids) | offer_extra)
    rel_ids = sorted(set(offer_ids) | {int(i) for i in (extra_item_ids or []) if i is not None})
    suppliers = {s.id: SupplierRec(id=s.id, name=s.name or "", contact_person=s.contact_person,
                                   phone=s.phone, email=s.email, address=s.address,
                                   is_active=bool(s.is_active) if s.is_active is not None else True,
                                   status=normalize_status(s.purchase_status) or "normal",
                                   status_reason=s.status_reason, merged_into_id=s.merged_into_id)
                 for s in db.query(Supplier).filter(Supplier.domain == domain).all()}
    pin = PriceInputs(suppliers=suppliers)
    row = db.query(AppSetting).filter(AppSetting.key == CFG_SKIP_SUPPLIERS).first()
    if row and row.value is not None:
        pin.skip_suppliers = tuple(x.strip() for x in row.value.split(",") if x.strip())
    if not rel_ids:
        return pin
    # ── Malzeme tercihleri: kartın kendi satırları + aktif grubunun satırları ──
    group_of = {iid: gid for iid, gid in (
        db.query(Item.id, Item.material_group_id)
        .join(MaterialGroup, MaterialGroup.id == Item.material_group_id)
        .filter(Item.id.in_(rel_ids), Item.domain == domain, MaterialGroup.is_active == True)   # noqa: E712
        .all())}
    pref_rows: Dict[int, list] = {}
    pq = db.query(MaterialSupplierPref).filter(MaterialSupplierPref.domain == domain)
    cond = MaterialSupplierPref.item_id.in_(rel_ids)
    if group_of:
        cond = or_(cond, MaterialSupplierPref.material_group_id.in_(sorted(set(group_of.values()))))
    for p in pq.filter(cond).all():
        rec = (p.supplier_id, p.preference, p.rank, p.item_id is not None)
        targets = [p.item_id] if p.item_id else [i for i, g in group_of.items() if g == p.material_group_id]
        for iid in targets:
            pref_rows.setdefault(iid, []).append(rec)
    for iid, rows in pref_rows.items():
        eff = effective_prefs(rows)
        if eff:
            pin.prefs[iid] = sorted(((sid, pr, rk) for sid, (pr, rk) in eff.items()),
                                    key=lambda x: (x[1] != "preferred", x[2], x[0]))
    if offer_ids:
        for sp in (db.query(SupplierPrice)
                   .filter(SupplierPrice.item_id.in_(offer_ids), SupplierPrice.domain == domain)
                   .order_by(SupplierPrice.id.asc()).all()):
            name = sp.supplier_name or (suppliers[sp.supplier_id].name if sp.supplier_id in suppliers else None)
            pin.offers.setdefault(sp.item_id, []).append(OfferRec(
                item_id=sp.item_id, supplier_name=name, supplier_id=sp.supplier_id,
                unit_price=sp.unit_price, package_size=sp.package_size,
                currency=(sp.currency or "USD"), price_unit=sp.price_unit, source=sp.source,
                source_label=sp.source_label, quoted_at=sp.quoted_at))
    for iid, sid in (db.query(Item.id, Item.supplier_id)
                     .filter(Item.id.in_(rel_ids), Item.domain == domain,
                             Item.supplier_id.isnot(None)).all()):
        pin.card_supplier[iid] = sid
    moved_from: Set[int] = set()
    for (inv_id, iid, lot_no, sid, is_s, cat, qty, mfrom, conv_at) in (
            db.query(Inventory.id, Inventory.item_id, Inventory.lot_number, Inventory.supplier_id,
                     Inventory.is_sample, Inventory.created_at, Inventory.quantity,
                     Inventory.moved_from_item_id, Inventory.sample_converted_at)
            .filter(Inventory.item_id.in_(rel_ids), Inventory.domain == domain).all()):
        pin.lots.setdefault(iid, []).append(LotRec(
            item_id=iid, lot_number=lot_no or "", supplier_id=sid,
            supplier_name=suppliers[sid].name if sid in suppliers else None,
            is_sample=bool(is_s), created_at=cat, quantity=float(qty or 0.0), inventory_id=inv_id))
        if mfrom or conv_at:
            pin.lot_meta[inv_id] = LotMetaRec(moved_from_item_id=mfrom, sample_converted_at=conv_at)
        if mfrom:
            moved_from.add(mfrom)
    rx_ids = sorted(set(rel_ids) | moved_from)
    for (iid, lot_no, qty, ts, note) in (
            db.query(Transaction.item_id, Transaction.lot_number, Transaction.quantity,
                     Transaction.timestamp, Transaction.notes)
            .join(Item, Item.id == Transaction.item_id)
            .filter(Transaction.item_id.in_(rx_ids), Item.domain == domain,
                    Transaction.transaction_type == "Input").all()):
        pin.receipts.setdefault(iid, []).append(ReceiptRec(
            item_id=iid, lot=lot_no or "", qty=float(qty or 0.0), at=ts,
            kind=classify_input(note, lot_no)))
    pin.converted_lots = {(r.item_id, r.lot) for lst in pin.receipts.values() for r in lst
                          if r.kind == "converted"}
    for f, sname in (db.query(StockOrderFlag, Supplier.name)
                     .outerjoin(Supplier, Supplier.id == StockOrderFlag.supplier_id)
                     .filter(StockOrderFlag.item_id.in_(rel_ids), StockOrderFlag.domain == domain)
                     .order_by(StockOrderFlag.ordered_at.desc(), StockOrderFlag.id.desc()).all()):
        pin.orders.setdefault(f.item_id, []).append(OrderRec(
            item_id=f.item_id, supplier_id=f.supplier_id,
            supplier_name=suppliers[f.supplier_id].name if f.supplier_id in suppliers else sname,
            quantity=f.quantity, unit=f.unit, ordered_at=f.ordered_at, expected_date=f.expected_date,
            closed_at=f.closed_at, closed_reason=f.closed_reason))
    inv_ids = [l.inventory_id for lst in pin.lots.values() for l in lst if l.inventory_id is not None]
    cond = SampleAnalysisIngredient.item_id.in_(rx_ids)
    if inv_ids:
        cond = or_(cond, SampleAnalysisIngredient.inventory_id.in_(inv_ids))
    for (iid, inv_id, lot_no, res, doc, at) in (
            db.query(SampleAnalysisIngredient.item_id, SampleAnalysisIngredient.inventory_id,
                     SampleAnalysisIngredient.lot_number, SampleAnalysis.result,
                     SampleAnalysis.document_no, SampleAnalysis.created_at)
            .join(SampleAnalysis, SampleAnalysis.id == SampleAnalysisIngredient.analysis_id)
            .filter(cond, SampleAnalysis.is_active == True,                   # noqa: E712
                    SampleAnalysis.domain == domain).all()):
        pin.analyses.setdefault(iid, []).append(AnalysisRec(
            item_id=iid, inventory_id=inv_id, lot=lot_no or "", result=res, document_no=doc, at=at))
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


# İlişki türleri — firma dökümünde bu sırayla
REL_TYPES = ("card", "purchase", "import", "sample", "order", "offer")
REL_TYPE_TEXT = {"card": "stok kartı", "purchase": "alım", "import": "stok aktarımı", "sample": "numune",
                 "order": "sipariş", "offer": "fiyat listesi", "equivalent": "eşdeğer kart"}
_FIRM_RANK = {"card": 0, "purchase": 1, "order": 2, "offer": 3, "sample": 4, "import": 5}
ORDER_STATUS_TEXT = {"open": "açık", "received": "teslim alındı", "manual": "kapatıldı"}
ANALYSIS_TEXT = {"uygun": "uygun", "uygun_degil": "uygun değil"}
REL_LINES_MAX = 3            # PDF/önizleme: sipariş ve eşdeğer kart satırı en çok 3 + "+N"
STATUS_TEXT = {"normal": "normal", "preferred": "tercih edilen", "phase_out": "bitirilecek"}
# Fiyat kaynağı metni (Excel "Fiyat kaynağı" sütunu + Notlar dipnotu ORTAK) —
# etiket (`source_label`) yoksa kaynağa göre: elle girilen fiyat liste fiyatı
# gibi görünmesin.
SOURCE_TEXT = {"stok_son_durum": "Stok Son Durum fiyat listesi", "manual": "Elle girilen fiyat"}


def source_text(source, label=None) -> str:
    return label or SOURCE_TEXT.get(source or "", "Fiyat kaydı")


def _qty_text(q, unit) -> str:
    return _amount_near(q, unit) if q is not None and q > 1e-9 else ""


def _new_firm(k: str, name, supplier_id) -> dict:
    return {"key": k, "name": name, "supplier_id": supplier_id, "types": set(),
            "purchases": {"count": 0, "last_date": "", "last_qty_text": ""},
            "imports": {"count": 0, "last_date": ""}, "samples": [], "orders": [], "offers": [],
            "_p_at": None, "_i_at": None, "_seen": set()}


def _firm(firms: Dict[str, dict], k: str, name, supplier_id) -> dict:
    f = firms.get(k)
    if f is None:
        f = firms[k] = _new_firm(k, name, supplier_id)
    elif f["supplier_id"] is None and supplier_id is not None:
        f["supplier_id"] = supplier_id
    return f


class _RelIndex:
    """attach() başına bir kez kurulan arama tabloları: (kart, lot) → Input'lar,
    lot → analiz satırları."""

    def __init__(self, pin: PriceInputs, skip: Set[str], ix: SupplierIndex):
        self.pin, self.skip, self.ix = pin, skip, ix
        self.rx: Dict[Tuple[int, str], List[ReceiptRec]] = {}
        for lst in pin.receipts.values():
            for r in lst:
                self.rx.setdefault((r.item_id, r.lot or ""), []).append(r)
        self.an_inv: Dict[int, List[AnalysisRec]] = {}
        self.an_lot: Dict[Tuple[int, str], List[AnalysisRec]] = {}
        for lst in pin.analyses.values():
            for a in lst:
                if a.inventory_id is not None:
                    self.an_inv.setdefault(a.inventory_id, []).append(a)
                if a.lot:
                    self.an_lot.setdefault((a.item_id, a.lot), []).append(a)

    def key(self, name) -> str:
        k = self.ix.key(name) if name else ""
        return "" if (not k or k in self.skip) else k

    def lot_class(self, l: LotRec) -> dict:
        """Lotun türü + tarihi + miktarı (modül docstring'indeki sınıflama).

        Kanıt sırası — lotun KENDİ kartı, ilk geldiği karttan
        (`moved_from_item_id`) önce gelir:
          1. kendi kartta "Mal kabul" → ALIM (aynı lot no'ya numune çevrilip
             birleşmiş olsa da);
          2. kendi kartta çevirme Input'u → ÇEVRİLMİŞ NUMUNE;
          3. ilk kartta "Mal kabul" → ALIM (tam taşınmış birleşik lot);
          4. ilk kartta çevirme Input'u ya da `sample_converted_at` → ÇEVRİLMİŞ NUMUNE.
        İlk kart (kart, lot no) üzerinden eşleşir; lab "MİNERVA"/"NUMUNE" gibi
        genel lot no'ları kullandığı için oradaki İLGİSİZ bir alım, hedef karta
        çevrilmiş numuneyi alıma çevirmesin diye 2 → 3'ten önce."""
        meta = self.pin.lot_meta.get(l.inventory_id) if l.inventory_id is not None else None
        lot = l.lot_number or ""
        origin = (meta.moved_from_item_id if meta and meta.moved_from_item_id else None) or l.item_id
        own = list(self.rx.get((l.item_id, lot), ()))
        orig = list(self.rx.get((origin, lot), ())) if origin != l.item_id else []
        recs = own + orig
        conv = [r for r in recs if r.kind == "converted"]
        out = {"origin": origin, "lot": lot, "conv": conv}
        if l.is_sample:
            return dict(out, kind="sample", at=l.created_at, qty=l.quantity)

        def purchase(ps):
            at = max((r.at for r in ps if r.at), default=None) or l.created_at
            return dict(out, kind="purchase", at=at, qty=sum(r.qty for r in ps))

        def converted(cs):
            return dict(out, conv=cs, kind="converted", at=l.created_at,
                        qty=sum(r.qty for r in cs) if cs else l.quantity)

        own_p = [r for r in own if r.kind == "purchase"]
        orig_p = [r for r in orig if r.kind == "purchase"]
        if own_p:
            return purchase(own_p + orig_p)
        own_c = [r for r in own if r.kind == "converted"]
        if own_c:
            return converted(own_c)
        if orig_p:
            return purchase(orig_p)
        if conv or (meta and meta.sample_converted_at) or (l.item_id, lot) in self.pin.converted_lots:
            return converted(conv)
        imp = [r for r in recs if r.kind == "import"]
        if imp or lot.upper().startswith(("IMP-", "XLS-")):
            at = max((r.at for r in imp if r.at), default=None) or l.created_at
            return dict(out, kind="import", at=at, qty=sum(r.qty for r in imp) or None)
        if any(r.kind == "production" for r in recs):
            return dict(out, kind="production", at=l.created_at, qty=None)
        other = [r for r in recs if r.kind == "other"]
        return dict(out, kind="old", at=l.created_at, qty=sum(r.qty for r in other) if other else None)

    def analysis(self, l: LotRec) -> Optional[dict]:
        cands = list(self.an_inv.get(l.inventory_id, ())) if l.inventory_id is not None else []
        if not cands and l.lot_number:
            cands = [a for a in self.an_lot.get((l.item_id, l.lot_number), ())
                     if a.inventory_id is None or a.inventory_id == l.inventory_id]
        if not cands:
            return None
        a = max(cands, key=lambda x: (x.at or datetime.min, x.document_no or ""))
        return {"result": a.result, "result_text": ANALYSIS_TEXT.get(a.result or "", "beklemede"),
                "doc_no": a.document_no}

    def lot_name(self, l: LotRec) -> Optional[str]:
        return _lot_name(l, self.pin)

    def summary(self, item_ids: List[int], unit) -> dict:
        """Kart(lar)ın tedarikçi özeti: firms {key: firma}, eski `last`/`samples`."""
        pin = self.pin
        firms: Dict[str, dict] = {}

        def firm(k, nm, sid):
            return _firm(firms, k, nm, sid)

        card = []
        for i in item_ids:
            sid = pin.card_supplier.get(i)
            if sid is None or sid not in pin.suppliers:
                continue
            nm = clean_text(pin.suppliers[sid].name)
            k = self.key(nm)
            if not k:
                continue
            firm(k, nm, sid)["types"].add("card")
            if all(c["key"] != k for c in card):
                card.append({"name": nm, "key": k, "supplier_id": sid})
        last, samples, sseen = None, [], set()
        for i in item_ids:
            for l in pin.lots.get(i, []):
                nm = self.lot_name(l)
                k = self.key(nm)
                if not k:
                    continue
                c = self.lot_class(l)
                f = firm(k, nm, l.supplier_id)
                if c["kind"] in ("sample", "converted"):
                    f["types"].add("sample")
                    f["samples"].append({"lot": c["lot"], "date": _dmy(l.created_at),
                                         "qty_text": _qty_text(c["qty"], unit),
                                         "converted": c["kind"] == "converted", "analysis": self.analysis(l),
                                         "_at": l.created_at or datetime.min})
                    sk = (k, _dmy(l.created_at))
                    if sk not in sseen:
                        sseen.add(sk)
                        samples.append({"name": nm, "key": k, "date": _dmy(l.created_at)})
                    continue
                if c["kind"] == "import":
                    f["types"].add("import")
                    f["imports"]["count"] += 1
                    if f["_i_at"] is None or (c["at"] or datetime.min) > f["_i_at"]:
                        f["_i_at"] = c["at"] or datetime.min
                        f["imports"]["last_date"] = _dmy(c["at"])
                    continue
                if c["kind"] == "production":
                    continue
                # alım (Mal kabul) ya da Input'u bilinmeyen eski alım
                pkey = (c["origin"], c["lot"])
                if c["kind"] == "purchase" and pkey in f["_seen"]:
                    continue                      # kısmi taşımanın iki parçası tek alımdır
                f["_seen"].add(pkey)
                f["types"].add("purchase")
                f["purchases"]["count"] += 1
                at = c["at"] or datetime.min
                if f["_p_at"] is None or at > f["_p_at"]:
                    f["_p_at"] = at
                    f["purchases"]["last_date"] = _dmy(c["at"])
                    f["purchases"]["last_qty_text"] = _qty_text(c["qty"], unit)
                if last is None or at > last["_at"]:
                    last = {"name": nm, "key": k, "date": _dmy(c["at"]), "_at": at}
                # Alım lotuna aynı lot no'lu numune çevrilip birleşmişse numune izi de kalsın
                for r in c["conv"]:
                    f["types"].add("sample")
                    f["samples"].append({"lot": c["lot"], "date": _dmy(r.at), "qty_text": _qty_text(r.qty, unit),
                                         "converted": True, "analysis": self.analysis(l),
                                         "_at": r.at or datetime.min})
        if last:
            last.pop("_at")
        samples.sort(key=lambda s: (s["name"], s["date"]))
        for f in firms.values():
            f["samples"].sort(key=lambda x: x["_at"], reverse=True)       # en yeni önce
            for x in f["samples"]:
                x.pop("_at")
        return {"firms": firms, "card": card, "last": last, "samples": samples}


def _order_view(o: OrderRec, unit, ri: _RelIndex) -> dict:
    nm = clean_text(o.supplier_name)
    status = "open" if o.closed_at is None else ("received" if o.closed_reason == "received" else "manual")
    return {"name": nm, "key": ri.key(nm) or None, "supplier_id": o.supplier_id,
            "date": _dmy(o.ordered_at), "qty_text": _qty_text(o.quantity, o.unit or unit),
            "status": status, "expected": _dmy(o.expected_date)}


def _relations(m: dict, ri: _RelIndex) -> dict:
    """Malzemenin tedarikçi ilişkileri (bütün üye kartlar üzerinden).

    Eski anahtarlar (`card`, `last`, `samples`) aynı şekilde kalır; ek olarak
    `firms[]` (firma başına tür dökümü), `orders[]` (sipariş işaretleri,
    firmasızlar dahil), `alternatives[]` ("aynı malzeme" grubundaki diğer
    kartlar) ve `group`."""
    pin = ri.pin
    member_ids = m.get("member_ids") or []
    unit = m.get("unit")
    sm = ri.summary(member_ids, unit)
    firms = sm["firms"]
    orders = []
    for i in member_ids:
        for o in pin.orders.get(i, []):
            ov = _order_view(o, unit, ri)
            orders.append(dict(ov, _at=o.ordered_at or datetime.min))
            if ov["key"]:
                f = _firm(firms, ov["key"], ov["name"], o.supplier_id)
                f["types"].add("order")
                f["orders"].append({k: ov[k] for k in ("date", "qty_text", "status", "expected")})
    orders.sort(key=lambda x: x["_at"], reverse=True)                     # en yeni önce
    for o in orders:
        o.pop("_at")
    for o in m.get("offers") or []:
        k = o.get("supplier_key") or ""
        if not k or k in ri.skip or o.get("status") == "equivalent":   # eşdeğer kartın bilgi teklifi
            continue
        f = _firm(firms, k, o["name"], o.get("supplier_id"))
        f["types"].add("offer")
        f["offers"].append({"price": o.get("price"), "price_unit": o.get("price_unit"),
                            "currency": o.get("currency"), "package": o.get("package"),
                            "note": o.get("note"), "quoted_at": o.get("quoted_at"),
                            "source_label": o.get("source_label"), "via_name": o.get("via_name"),
                            "status": o.get("status"), "eligible": o.get("eligible", True)})
    firm_list = []
    for f in firms.values():
        for tmp in ("_p_at", "_i_at", "_seen"):
            f.pop(tmp, None)
        f["types"] = [t for t in REL_TYPES if t in f["types"]]
        firm_list.append(f)
    firm_list.sort(key=lambda f: (min(_FIRM_RANK[t] for t in f["types"]) if f["types"] else 9,
                                  alnum_fold(f["name"])))

    mg = m.get("material_group") or None
    alternatives = []
    for a in (mg or {}).get("alts") or []:
        aid = a["item_id"]
        sid = pin.card_supplier.get(aid)
        snm = clean_text(pin.suppliers[sid].name) if sid in pin.suppliers else None
        sk = ri.key(snm)
        asm = ri.summary([aid], a.get("unit"))
        sq = sum(l.quantity for l in pin.lots.get(aid, []) if l.is_sample and l.quantity > 0)
        stock = float(a.get("stock") or 0.0)
        alternatives.append({
            "item_id": aid, "name": a.get("name") or "—", "unit": a.get("unit") or "",
            "unit_mismatch": bool(a.get("unit_mismatch")),
            "supplier": snm if sk else None, "supplier_key": sk or None, "supplier_id": sid if sk else None,
            "stock": stock, "stock_text": _amount_near(stock, a.get("unit")) if stock > 0 else "yok",
            "sample_text": f"{_amount_near(sq, a.get('unit'))} numune" if sq > 0 else "",
            "last": asm["last"], "samples": asm["samples"], "in_plan": bool(a.get("in_plan"))})
    return {"card": sm["card"], "last": sm["last"], "samples": sm["samples"],
            "firms": firm_list, "orders": orders, "alternatives": alternatives,
            "group": {"id": mg["id"], "name": mg["name"]} if mg else None}


# Teklif durumu — seçime etkisi ve etiketi.  Uygun olmayanlar SEÇİLMEZ.
OFFER_BLOCKED = ("skip", "phase_out", "avoid", "inactive")
OFFER_BLOCKED_TEXT = {"phase_out": "bitirilecek, alma", "avoid": "bu malzemede alma",
                      "skip": "atlanacaklar listesinde", "inactive": "pasif tedarikçi"}


@dataclass
class _StatusCtx:
    """attach() başına: firma anahtarı → durum (mükerrer kartlar tek duruma
    indirgenmiş, `core.suppliers.status_by_key`) + çelişkiler + seçim kuralı
    açık mı (`respect_supplier_status`)."""
    respect: bool = True
    by_key: Dict[str, str] = field(default_factory=dict)
    conflicts: List[dict] = field(default_factory=list)
    reasons: Dict[str, str] = field(default_factory=dict)       # phase_out anahtarı → sebep
    # Pasif firma kuralı (seçenekten bağımsız); None → yalnız kartın kendisi
    firms: Optional["FirmActivity"] = None


def _status_ctx(pin: PriceInputs, ix: "SupplierIndex", respect: bool) -> _StatusCtx:
    from core.suppliers import status_by_key
    by_key, conflicts = status_by_key(list(pin.suppliers.values()), ix)
    reasons: Dict[str, str] = {}
    for sid in sorted(pin.suppliers):
        sr = pin.suppliers[sid]
        k = ix.key(sr.name)
        if k and sr.status == "phase_out" and sr.status_reason and sr.is_active and k not in reasons:
            reasons[k] = " ".join(sr.status_reason.split())
    return _StatusCtx(respect=respect, by_key=by_key, conflicts=conflicts, reasons=reasons,
                      firms=firm_activity(pin, ix))


class FirmActivity:
    """Firma PASİF mi (yumuşak silinmiş) — satın alma planı (`_offer_status`,
    aday firmalar) ile Raporlar paneli / Excel'in
    (`supplier_prices.inactive_firm_check`) TEK kuralı: iki rapor aynı fiyat
    satırına farklı sonuç vermesin.

    Ölçüt KART değil FİRMA (`SupplierIndex` anahtarı):
      • Kart YAŞIYOR: aktif, ya da birleştirilmiş (`merged_into_id`) ve
        zincirin sonunda aktif bir kazanan var (`core.suppliers.live_card_id`)
        — birleşen kartın adı kazananın takma adıdır, firma silinmedi.
      • Satır AKTİF: metin adının ya da bağlı kartın adının anahtarı yaşayan
        bir kartınkiyle eşleşiyorsa — mükerrer kartın pasif ikizi
        (TATLİDİLİMLER pasif / TATLIDİLİMLER aktif) ve birleştirmede
        kazanana taşınıp eski adı metinde kalan satır (KRK GIDA(HAYAT) →
        KRK GIDA) dahil.
      • Değilse: bağlı kart ölü → pasif; kartsız serbest metin yalnız adı
        ölü bir kartla eşleşirse pasif.
    `cards`: {supplier_id: (ad, is_active, merged_into_id)}."""

    def __init__(self, cards: Dict[int, Tuple[str, bool, Optional[int]]], ix: "SupplierIndex"):
        from core.suppliers import live_card_id
        self._cards, self._ix = cards, ix
        chain = {sid: (a, m) for sid, (_, a, m) in cards.items()}
        self._alive = {sid: live_card_id(chain, sid) is not None for sid in cards}
        keys = {sid: ix.key(n) for sid, (n, _, _) in cards.items()}
        self.active: Set[str] = {keys[s] for s, a in self._alive.items() if a} - {""}
        self.inactive: Set[str] = ({keys[s] for s, a in self._alive.items() if not a}
                                   - self.active - {""})

    def is_inactive(self, supplier_id, supplier_name) -> bool:
        names = [supplier_name]
        if supplier_id in self._cards:
            names.append(self._cards[supplier_id][0])
        keys = {self._ix.key(n) for n in names if n} - {""}
        if keys & self.active:
            return False
        if supplier_id in self._cards:
            return not self._alive[supplier_id]
        return bool(keys & self.inactive)


def firm_activity(pin: PriceInputs, ix: "SupplierIndex") -> FirmActivity:
    return FirmActivity({sid: (s.name or "", s.is_active is not False, s.merged_into_id)
                         for sid, s in pin.suppliers.items()}, ix)


def _row_prefs(m: dict, pin: PriceInputs, ix: "SupplierIndex") -> dict:
    """Satırın malzeme tercihleri (bütün üye kartlar): "alma" her şeyi yener,
    tercih sırası en küçük rank.  → {"preferred": [{supplier_id, key, name,
    rank}], "avoid": [...]} (supplier_id + anahtar ile eşleşir)."""
    pref: Dict[int, int] = {}
    avoid: Set[int] = set()
    for mid in m.get("member_ids") or []:
        for sid, p, rank in pin.prefs.get(mid, []):
            if p == "avoid":
                avoid.add(sid)
            elif p == "preferred":
                pref[sid] = min(int(rank or 1), pref.get(sid, 10 ** 6))

    def rec(sid, rank=None):
        sr = pin.suppliers.get(sid)
        nm = clean_text(sr.name) if sr else None
        d = {"supplier_id": sid, "key": ix.key(nm) if nm else "", "name": nm or f"Tedarikçi #{sid}"}
        if rank is not None:
            d["rank"] = rank
        return d
    return {"preferred": sorted((rec(sid, r) for sid, r in pref.items() if sid not in avoid),
                                key=lambda x: (x["rank"], alnum_fold(x["name"]))),
            "avoid": sorted((rec(sid) for sid in avoid), key=lambda x: alnum_fold(x["name"]))}


def _offer_status(o: dict, rp: dict, sc: _StatusCtx, skip: Set[str], pin: PriceInputs) -> None:
    """Teklife `status` / `eligible` / `pref` / `pref_rank` / `tier` yazar (yerinde).
    Pasif firma (`FirmActivity` — Raporlar paneliyle aynı kural; aktif karta
    bağlı teklif metin adı ne olursa olsun pasif DEĞİLDİR) seçim kuralı
    kapalıyken de elenir — silinmiş firma "tercih" değil, yok."""
    k, sid = o.get("supplier_key") or "", o.get("supplier_id")
    o.update(status="normal", eligible=True, pref=None, pref_rank=None, tier=(2, 0))
    dead = (sc.firms.is_inactive(sid, o.get("name")) if sc.firms is not None
            else (sid in pin.suppliers and pin.suppliers[sid].is_active is False))
    if dead:
        o.update(status="inactive", eligible=False, tier=(9, 0))
        return
    if not sc.respect:
        return

    def hit(lst):
        return next((x for x in lst if (sid is not None and x["supplier_id"] == sid) or (k and x["key"] == k)),
                    None)
    sup_st = sc.by_key.get(k) if k else None
    if sup_st is None and sid in pin.suppliers:
        sup_st = pin.suppliers[sid].status
    if k and k in skip:
        o.update(status="skip", eligible=False, tier=(9, 0))
    elif sup_st == "phase_out":
        o.update(status="phase_out", eligible=False, tier=(9, 0))
    elif hit(rp["avoid"]):
        o.update(status="avoid", eligible=False, tier=(9, 0))
    else:
        p = hit(rp["preferred"])
        if p is not None:
            o.update(status="preferred", pref="material", pref_rank=p["rank"], tier=(0, p["rank"]))
        elif sup_st == "preferred":
            o.update(status="preferred", pref="supplier", tier=(1, 0))


def offer_tags(o: dict, best: Optional[dict] = None) -> Tuple[List[str], Optional[str]]:
    """Teklif satırının ekleri → (parantez içi etiketler, engel metni).
    Etiketler: "tercih" (malzeme tercihi; 2+ sırada "tercih 2. sıra"),
    "tercih edilen firma", "eşdeğer kart «X»", seçilenden farklı uygun fiyatlı
    teklifte "daha pahalı" / "daha ucuz".  Engel: "bitirilecek, alma" /
    "bu malzemede alma" / "atlanacaklar listesinde" (gri, seçilmez).
    Eşdeğer kartın bilgi teklifi (`status="equivalent"`) engelsiz gridir —
    "daha ucuz" / "daha pahalı" kıyası onda da yazılır."""
    tags: List[str] = []
    if o.get("pref") == "material":
        r = o.get("pref_rank") or 1
        tags.append("tercih" if r <= 1 else f"tercih {r}. sıra")
    elif o.get("pref") == "supplier":
        tags.append("tercih edilen firma")
    if o.get("via_name"):
        tags.append(f"eşdeğer kart «{o['via_name']}»")
    blocked = OFFER_BLOCKED_TEXT.get(o.get("status")) if o.get("eligible") is False else None
    if (best is not None and o is not best and not blocked and o.get("price") is not None
            and best.get("price") is not None):
        tags.append("daha ucuz" if o["price"] < best["price"] - 1e-9 else "daha pahalı")
    return tags, blocked


def offer_text(o: dict, cur: str) -> str:
    """'BEFCHEM — 8,00 $/kg · 25 kg'lık ambalaj (2,50 €/kg)' — fiyatlı teklif
    gövdesi (etiketsiz); önizleme / PDF / Excel ortak."""
    from core.purchase_plan_pdf import PKG_SUFFIX, _qty
    sym = CURRENCY_SYMBOL.get(cur, cur)
    unit = UNIT_TEXT.get(o.get("price_unit"), o.get("price_unit") or "")
    t = f"{o['name']} — {price_text(o['price'])} {sym}/{unit}"
    if o.get("package"):
        pu = o.get("orig_price_unit") or o.get("price_unit") or "kg"
        t += f" · {_qty(o['package'])} {PKG_SUFFIX.get(pu, pu)} ambalaj"
    if o.get("orig_currency") and o["orig_currency"] != cur and o.get("orig_price") is not None:
        osym = CURRENCY_SYMBOL.get(o["orig_currency"], o["orig_currency"])
        ou = UNIT_TEXT.get(o.get("orig_price_unit"), o.get("orig_price_unit") or unit)
        t += f" ({price_text(o['orig_price'])} {osym}/{ou})"
    return t


def _with_tags(t: str, tags: List[str], blocked: Optional[str]) -> str:
    if tags:
        t += " (" + ", ".join(tags) + ")"
    if blocked:
        t += f" — {blocked}"
    return t


def offer_lines(m: dict, cur: str) -> List[Tuple[str, str]]:
    """Tedarikçi hücresinin teklif kısmı → [(metin, stil)] — stil 'b' kalın
    (seçilen) | 'g' gri | 'r' kırmızı.  Önizleme (`_sup_lines`) ve PDF
    (`_sup_cell`) aynı listeyi basar: seçilen kalın; diğer fiyatlı teklifler
    gri ("daha pahalı" / "daha ucuz", "tercih", "eşdeğer kart «X»"); uygun
    olmayanlar gri + " — bitirilecek, alma" …; fiyat seçilemediyse kırmızı
    "Fiyat yok — teklif alınacak"; fiyatı kullanılamayanlar gri sonda."""
    out: List[Tuple[str, str]] = []
    offers = m.get("offers") or []
    priced = [o for o in offers if o.get("price") is not None]
    best = next((o for o in priced if o.get("eligible", True)), None) if m.get("group") == "list" else None
    if best is None:
        out.append(("Fiyat yok — teklif alınacak", "r"))
    for o in priced:
        tags, blocked = offer_tags(o, best)
        out.append((_with_tags(offer_text(o, cur), tags, blocked), "b" if o is best else "g"))
    for o in offers:
        if o.get("price") is None:
            out.append((unpriced_offer_text(o), "g"))
    return out


def _price_row(m: dict, pin: PriceInputs, rates, currency: str, round_to_package: bool,
               fx_used: Set[str], sources: dict, flags: dict, skip: Set[str],
               ix: "SupplierIndex", ri: Optional[_RelIndex] = None,
               sc: Optional[_StatusCtx] = None) -> None:
    unit = m["unit"]
    sc = sc or _StatusCtx(respect=False, firms=firm_activity(pin, ix))
    offers, seen = [], set()
    # Satırın kendi (birleşik) kartları önce, sonra (seçim kuralı açıkken)
    # "aynı malzeme" grubunun diğer kartları — "eşdeğer kart" teklifi (aynı
    # firma + fiyat + ambalaj ikinci kez yazılmaz; kendi kartındaki kalır).
    # Birim ailesi farklı eşdeğer kartın (126 "g" ↔ 599 "adet") teklifi bu
    # satıra uygulanamaz → alınmaz.  Kural kapalıyken eşdeğer teklif HİÇ
    # yoktur (eski davranış).
    cards = [(mid, None) for mid in m.get("member_ids") or []]
    if sc.respect:
        cards += [(a["item_id"], a.get("name")) for a in (m.get("material_group") or {}).get("alts") or []
                  if not a.get("unit_mismatch")]
    for mid, via in cards:
        card_offers = pin.offers.get(mid, [])
        # Elle girilen fiyat aynı firmanın Excel teklifinin yerine geçer
        # (kart başına; prices_for_items ile aynı `firm_keys` kuralı)
        manual: set = set()
        for o in card_offers:
            if o.source == SOURCE_MANUAL:
                manual |= firm_keys(o.supplier_id, o.supplier_name)
        for o in card_offers:
            if o.source != SOURCE_MANUAL and firm_keys(o.supplier_id, o.supplier_name) & manual:
                continue
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
            offers.append({"name": name, "supplier_key": k, "supplier_id": o.supplier_id,
                           "item_id": o.item_id, "price": conv, "price_unit": pu,
                           "orig_price": o.unit_price, "orig_currency": ocur,
                           "orig_price_unit": o.price_unit, "package": o.package_size,
                           "currency": currency, "note": note, "source": o.source,
                           "source_label": o.source_label,
                           "quoted_at": o.quoted_at.isoformat() if o.quoted_at else None,
                           "via_item_id": mid if via is not None else None, "via_name": via,
                           "_skey": ((o.source or "", o.source_label or "", o.quoted_at, ocur, o.price_unit or "")
                                     if conv is not None else None),
                           "_lkg": note == LITRE_KG_NOTE and conv is not None})
    rp = _row_prefs(m, pin, ix) if sc.respect else {"preferred": [], "avoid": []}
    for o in offers:
        _offer_status(o, rp, sc, skip, pin)
    # Eşdeğer kart teklifi YALNIZ şu hallerde seçilebilir: kendisi bir tercih
    # kademesinde (malzeme tercihi / "tercih edilen" firma) ya da satırın
    # kendi teklifleri durum / "alma" / atlanacaklar yüzünden elendi.  Öbür
    # hallerde gri bilgi ("equivalent") — durum yokken seçim, tutar, firma
    # dökümü ve fiyat kaynakları bugünküyle aynı kalır.
    own = [o for o in offers if o["via_item_id"] is None]
    own_out = (any(o["status"] in OFFER_BLOCKED for o in own)
               and not any(o["eligible"] and o["price"] is not None for o in own))
    for o in offers:
        if o["via_item_id"] is not None and o["eligible"] and o["tier"][0] >= 2 and not own_out:
            o.update(status="equivalent", eligible=False)
            flags["via_listed"] = True
        if o["status"] == "inactive":
            flags["inactive_listed"] = True
        elif o["status"] not in ("normal", "equivalent"):
            flags["status_used"] = True
        skey, lkg = o.pop("_skey"), o.pop("_lkg")
        if o["status"] == "equivalent":
            continue
        if lkg:
            flags["litre_kg"] = True
        if skey is not None:
            sources[skey] = sources.get(skey, 0) + 1
    # Uygun önce → kademe (malzeme tercihi rank → tercih edilen firma →
    # normal) → ucuzdan pahalıya (fiyatsız sonda) → ad.  Durum yoksa kendi
    # teklifler (uygun, normal) eski "en ucuz önce" sırasının aynısı; eşdeğer
    # kartın bilgi teklifleri onlardan sonra.
    offers.sort(key=lambda x: (not x["eligible"], x["tier"], x["price"] is None,
                               x["price"] if x["price"] is not None else 0.0, x["name"]))
    for o in offers:
        o.pop("tier")
    best = next((o for o in offers if o["eligible"] and o["price"] is not None), None)
    if best is not None and best.get("via_name"):
        flags["via_used"] = True
    buy_num = float(m["display"]["buy_num"] or 0.0)
    m["offers"] = offers
    m["supplier_prefs"] = rp
    m["supplier_status"] = best["status"] if best else None
    m["supplier_via"] = best.get("via_name") if best else None
    m["group"] = "list" if best else "none"
    m["price"] = best["price"] if best else None
    m["price_unit"] = (best["price_unit"] if best else
                       {"kg": "kg", "l": "l"}.get(m["display"].get("buy_unit"), "adet"))
    m["supplier"] = best["name"] if best else None
    m["supplier_key"] = best["supplier_key"] if best else None
    m["price_source"] = source_text(best["source"], best["source_label"]) if best else None
    m["amount"] = (round_half_up(buy_num * best["price"])
                   if best and m["status"] == "to_buy" else None)
    m["pkg_buy"] = m["pkg_amount"] = None
    if round_to_package and best and best.get("package") and best["package"] > 0 and m["status"] == "to_buy":
        pkg = float(best["package"])
        m["pkg_buy"] = round(math.ceil(buy_num / pkg - 1e-9) * pkg, 6)
        m["pkg_amount"] = round_half_up(m["pkg_buy"] * best["price"])
    m["relations"] = _relations(m, ri or _RelIndex(pin, skip, ix))


def attach(result: dict, pin: PriceInputs, rates, *, currency: str = "USD",
           round_to_package: bool = False, respect_status: Optional[bool] = None) -> dict:
    """Fiyat + tedarikçi alanlarını `result`'a yazar (yerinde) ve döndürür.

    `rates` (`core.fx.Rates`) yalnız para birimi farklı teklif varsa gerekir;
    None iken çevrilemeyen teklif fiyatsız sayılır (notla).
    `respect_status` None → `result.meta.options.respect_supplier_status`
    (yoksa açık).  Kapalıyken seçim yalnız en ucuz (eski davranış).
    """
    currency = (currency or "USD").upper()
    if respect_status is None:
        respect_status = bool(((result.get("meta") or {}).get("options") or {})
                              .get("respect_supplier_status", True))
    ix = supplier_index(pin)
    skip = {ix.key(s) for s in (pin.skip_suppliers or ())}
    skip.discard("")
    fx_used: Set[str] = set()
    sources: dict = {}
    flags = {"litre_kg": False, "status_used": False, "via_used": False, "via_listed": False,
             "inactive_listed": False}
    ri = _RelIndex(pin, skip, ix)
    sc = _status_ctx(pin, ix, respect_status)
    for m in result.get("materials", []):
        _price_row(m, pin, rates, currency, round_to_package, fx_used, sources, flags, skip, ix, ri, sc)
    for m in result.get("held", []):
        _price_row(m, pin, rates, currency, round_to_package, fx_used, sources, flags, skip, ix, ri, sc)

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
        # Seçimi tedarikçi durumu / malzeme tercihi etkiledi mi (metinler buna göre)
        "respect_status": bool(respect_status), "status_used": flags["status_used"],
        "via_used": flags["via_used"], "via_listed": flags["via_listed"],
        "inactive_listed": flags["inactive_listed"],
    }
    result["suppliers"] = supplier_directory(result, pin, skip, ix, sc)
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
    (ilk dolu değer kazanır; kart adları kopya-kart kontrolü için tutulur).

    Aktif kartlar ÖNCE gezilir; firmanın aktif kartı varsa pasif kartları
    (yumuşak silinmiş mükerrer) tamamen atlanır — ne adı, ne eski telefonu,
    ne kopya-kart sayımına girer.  Bütün kartları pasif firmada ilk pasif kart
    yedek olarak kalır (yoksa "kartı yok" diye yanlış alarm çıkardı).  Pasif
    kart yoksa sıra bugünküyle aynı (id sırası)."""
    out: Dict[str, dict] = {}
    for sid in sorted(pin.suppliers, key=lambda i: (pin.suppliers[i].is_active is False, i)):
        s = pin.suppliers[sid]
        k = ix.key(s.name)
        if not k or (s.is_active is False and k in out):
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
                       ix: Optional[SupplierIndex] = None, sc: Optional[_StatusCtx] = None) -> dict:
    """Seçilen tedarikçi blokları + fiyatsızlar için aday firmalar + hiçbir
    firmayla ilişkisi olmayanlar + kontrol listesi.

    Seçim kuralı açıkken (`sc.respect`): adaylara "bitirilecek" firmalar ve
    malzemenin "alma" listesindekiler GİRMEZ; malzeme tercihindeki firmalar
    (ilişkisi olmasa da) "tercih edilen yedek" sebebiyle EKLENİR.  Kontrol
    listesine `phase_out_firms` (bu planda karşılaşılan bitirilecek firmalar,
    sebepleriyle) ve `status_conflicts` (aynı firmanın kartları farklı
    işaretli) eklenir."""
    ix = ix or supplier_index(pin)
    sc = sc or _StatusCtx(respect=False, firms=firm_activity(pin, ix))
    po_keys = {k for k, st in sc.by_key.items() if st == "phase_out"} if sc.respect else set()
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
        row = {"key": m["key"], "name": m["name"], "buy_text": m["display"]["buy_text"],
               "price": m["price"], "price_unit": m["price_unit"], "amount": m["amount"]}
        best = next((o for o in m.get("offers") or [] if o.get("eligible", True) and o.get("price") is not None),
                    None)
        tags = offer_tags(best)[0] if best else []
        if tags:                                   # "tercih", "eşdeğer kart «X»"
            row["reasons"] = tags
        b["items"].append(row)
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
        for f in rel.get("firms") or []:
            if "purchase" in f.get("types", ()) and f["key"] != (rel["last"] or {}).get("key"):
                ks.append((f["key"], f["name"], _dated("alım", f["purchases"]["last_date"])))
            if "import" in f.get("types", ()):
                ks.append((f["key"], f["name"], _dated("stok aktarımı", f["imports"]["last_date"])))
        okeys: Set[str] = set()
        for o in rel.get("orders") or []:              # firma başına en yeni sipariş
            if o.get("key") and o["key"] not in okeys:
                okeys.add(o["key"])
                ks.append((o["key"], o["name"], _dated("sipariş", o.get("date"))))
        live_offer: Set[str] = set()
        for o in m.get("offers") or []:
            if (o["supplier_key"] and o["supplier_key"] not in (skip or set())
                    and o.get("status") not in ("equivalent", "inactive")):
                ks.append((o["supplier_key"], o["name"], "fiyat listesinde (fiyatsız)"))
                live_offer.add(o["supplier_key"])
        for a in rel.get("alternatives") or []:
            if a.get("supplier_key"):
                ks.append((a["supplier_key"], a["supplier"], f"eşdeğer kart «{a['name']}»"))
            if a.get("last"):
                ks.append((a["last"]["key"], a["last"]["name"], _dated("eşdeğer kartta son alım", a["last"]["date"])))
            for s in a.get("samples") or []:
                ks.append((s["key"], s["name"], _dated("eşdeğer kartta numune", s["date"])))
        rp = m.get("supplier_prefs") or {}
        for p in rp.get("preferred") or []:
            ks.append((p["key"], p["name"], "tercih edilen yedek" if (p.get("rank") or 1) <= 1
                       else f"tercih edilen yedek ({p['rank']}. sıra)"))
        # Pasif firma: teklifte kendi durumu (`inactive` — FirmActivity, aktif
        # karta bağlıysa asla), öbür ilişkilerde (kart / lot / sipariş adı)
        # anahtar; aynı anahtarda yaşayan teklif varsa firma yaşıyor.
        dead = (sc.firms.inactive if sc.firms is not None else set()) - live_offer
        blocked = po_keys | dead | {p["key"] for p in rp.get("avoid") or [] if p.get("key")}
        entry = {"key": m["key"], "name": m["name"], "buy_text": m["display"]["buy_text"]}
        seen: Dict[str, dict] = {}
        for k, nm, why in ks:
            if not k or k in blocked:
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
    # Firma başına ilişki türleri (Excel "Tedarikçiler" K sütunu) — bütün alınacaklar üzerinden
    rtypes: Dict[str, Set[str]] = {}
    for m in mats:
        rel = m.get("relations") or {}
        for f in rel.get("firms") or []:
            rtypes.setdefault(f["key"], set()).update(f.get("types") or ())
        for a in rel.get("alternatives") or []:
            for k in ([a.get("supplier_key")] + [(a.get("last") or {}).get("key")]
                      + [s["key"] for s in a.get("samples") or []]):
                if k:
                    rtypes.setdefault(k, set()).add("equivalent")
    for b in chosen_list + cand_list:
        b["types"] = [t for t in REL_TYPES + ("equivalent",) if t in rtypes.get(b["key"], ())]

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
    if sc.respect:
        # Bu planda karşılaşılan firmalar: teklifler + ilişkiler + eşdeğer kartlar
        seen_keys: Set[str] = set(used)
        for m in mats:
            rel = m.get("relations") or {}
            seen_keys.update(o["supplier_key"] for o in m.get("offers") or [] if o.get("supplier_key"))
            seen_keys.update(f["key"] for f in rel.get("firms") or [])
            seen_keys.update(a["supplier_key"] for a in rel.get("alternatives") or [] if a.get("supplier_key"))
        checklist["phase_out_firms"] = [{"key": k, "name": disp(k, k), "reason": sc.reasons.get(k)}
                                        for k in sorted(po_keys & seen_keys, key=lambda k: disp(k, k))]
        checklist["status_conflicts"] = [dict(c, name=disp(c["key"], c["key"])) for c in sc.conflicts
                                         if c["key"] in seen_keys]
    return {"chosen": chosen_list, "candidates": cand_list, "unrelated": unrelated,
            "checklist": checklist}


# ─── İlişki metinleri (önizleme / PDF / Excel ortak) ─────────────────────────

def _capped(lines: List[str], cap: Optional[int], noun: str) -> List[str]:
    if cap and len(lines) > cap:
        return lines[:cap] + [f"+{len(lines) - cap} {noun} daha"]
    return lines


def _sample_bits(s: dict) -> List[str]:
    """Numune kaydının kısa ekleri: miktar · analiz sonucu (belge no)."""
    out = [s["qty_text"]] if s.get("qty_text") else []
    an = s.get("analysis")
    if an:
        out.append(f"analiz {an['result_text']}" + (f" ({an['doc_no']})" if an.get("doc_no") else ""))
    return out


def _order_detail(o: dict) -> str:
    """'01.10.2026 · 25 kg · açık · beklenen 10.10.2026' (firmasız)."""
    bits = [x for x in (o.get("date"), o.get("qty_text"), ORDER_STATUS_TEXT.get(o.get("status"), "")) if x]
    if o.get("status") == "open" and o.get("expected"):
        bits.append(f"beklenen {o['expected']}")
    return " · ".join(bits)


def order_text(o: dict) -> str:
    """'KRK GIDA 01.10.2026 · 25 kg · açık · beklenen 10.10.2026'."""
    d = _order_detail(o)
    return (o.get("name") or "firma yazılmamış") + (f" {d}" if d else "")


def alt_text(a: dict) -> str:
    """'«CETYL STEARYL ALCOHOL» (YİĞİTOĞLU KİMYA, stok yok, birimi adet)'."""
    parts = [a.get("supplier") or "tedarikçi yazılmamış",
             f"stok {a['stock_text']}" if (a.get("stock") or 0) > 0 else "stok yok"]
    if a.get("unit_mismatch"):
        parts.append(f"birimi {a.get('unit') or '—'}")
    parts += _alt_history(a)
    return f"«{a.get('name') or '—'}» (" + ", ".join(parts) + ")"


def _alt_history(a: dict) -> List[str]:
    """Eşdeğer kartın son alımı + numuneleri (firma kartın tedarikçisiyse adı
    tekrarlanmaz) + elde duran numune miktarı."""
    def who(x, label):
        return label if x.get("key") == a.get("supplier_key") else f"{label} {x['name']}"
    out: List[str] = []
    last = a.get("last")
    if last:
        out.append(_dated(who(last, "son alım"), last.get("date")))
    for sm in a.get("samples") or []:
        out.append(_dated(who(sm, "numune"), sm.get("date")))
    if a.get("sample_text"):
        out.append(f"elde {a['sample_text']}")
    return out


def order_texts(m: dict) -> List[str]:
    return [order_text(o) for o in (m.get("relations") or {}).get("orders") or []]


def alt_texts(m: dict) -> List[str]:
    return [alt_text(a) for a in (m.get("relations") or {}).get("alternatives") or []]


def relation_line(m: dict) -> Optional[str]:
    """Eski ilişki satırı: 'Stok kartında yazan: X (son alım …) · Son alım: Y
    (…) · Numune gönderdi: Z (…, analiz uygun)'."""
    rel = m.get("relations") or {}
    extra: List[str] = []
    cards = rel.get("card") or []
    if cards:
        extra.append("Stok kartında yazan: " + " / ".join(x["name"] for x in cards))
    last = rel.get("last")
    if last:
        if any(x.get("key") == last.get("key") for x in cards):
            extra[0] += f" (son alım {last['date']})" if last.get("date") else ""
        else:
            extra.append(f"Son alım: {last['name']}" + (f" ({last['date']})" if last.get("date") else ""))
    firms = {f["key"]: f for f in rel.get("firms") or []}
    for s in rel.get("samples") or []:
        bits = [s["date"]] if s.get("date") else []
        an = next((x["analysis"] for x in (firms.get(s.get("key")) or {}).get("samples") or []
                   if x.get("analysis") and x.get("date") == s.get("date")), None)
        if an:
            bits.append(f"analiz {an['result_text']}")
        extra.append(f"Numune gönderdi: {s['name']}" + (f" ({', '.join(bits)})" if bits else ""))
    return " · ".join(extra) if extra else None


def relation_texts(m: dict, cap: Optional[int] = REL_LINES_MAX) -> List[str]:
    """Tedarikçi hücresinin ilişki satırları — önizleme (`_sup_lines`), PDF
    (`_sup_cell`) aynı listeyi basar: eski ilişki satırı, "Sipariş: …"
    satırları, "Aynı malzeme: «X» (FİRMA, stok …)" satırları (her biri en
    çok `cap` + "+N … daha")."""
    out: List[str] = []
    line = relation_line(m)
    if line:
        out.append(line)
    out += _capped(["Sipariş: " + t for t in order_texts(m)], cap, "sipariş")
    out += _capped(["Aynı malzeme: " + t for t in alt_texts(m)], cap, "kart")
    return out


def firm_lines(f: dict, currency: str = "USD") -> List[str]:
    """Firma dökümü satırları (önizleme "Tedarikçiler" açılır listesi)."""
    out: List[str] = []
    if "card" in f.get("types", ()):
        out.append("stok kartında yazan")
    p = f.get("purchases") or {}
    if p.get("count"):
        t = (f"{p['count']} alım, son {p['last_date']}" if p["count"] > 1
             else _dated("alım", p.get("last_date")))
        out.append(t + (f" ({p['last_qty_text']})" if p.get("last_qty_text") else ""))
    i = f.get("imports") or {}
    if i.get("count"):
        out.append(_dated("stok aktarımı", i.get("last_date")) + (f" ({i['count']} lot)" if i["count"] > 1 else ""))
    for s in f.get("samples") or []:
        t = _dated("numune (stoğa çevrildi)" if s.get("converted") else "numune", s.get("date"))
        if s.get("lot"):
            t += f" · lot {s['lot']}"
        out.append(" · ".join([t] + _sample_bits(s)))
    for o in f.get("orders") or []:
        out.append(_dated("sipariş", _order_detail(o)))
    sym = CURRENCY_SYMBOL.get(currency, currency)
    for o in f.get("offers") or []:
        if o.get("price") is not None:
            t = (f"fiyat listesi: {price_text(o['price'])} {sym}/"
                 f"{UNIT_TEXT.get(o.get('price_unit'), o.get('price_unit') or '')}")
        else:
            t = "fiyat listesinde (fiyatsız)"
        if o.get("via_name"):
            t += f" (eşdeğer kart «{o['via_name']}»)"
        if o.get("eligible") is False and o.get("status") in OFFER_BLOCKED_TEXT:
            t += f" — {OFFER_BLOCKED_TEXT[o['status']]}"
        out.append(t)
    return out


def alt_lines(a: dict, row_unit=None) -> List[str]:
    """Eşdeğer kart satırları (önizleme "Aynı malzeme — diğer kartlar")."""
    out = [f"stok {a['stock_text']}" if (a.get("stock") or 0) > 0 else "stok yok"]
    if a.get("in_plan"):
        out[0] += " (bu planda kendi satırında)"
    if a.get("unit_mismatch"):
        out.append(f"birimi {a.get('unit') or '—'}" + (f", bu satır {row_unit}" if row_unit else "")
                   + " — miktarlar doğrudan karşılaştırılamaz")
    return out + _alt_history(a)


def firms_view(m: dict, currency: str = "USD") -> Optional[dict]:
    """Önizlemenin "Tedarikçiler (N)" açılır listesi — sunucu biçimli.
    {title, count, firms:[{t, d:[…]}], alts_title, alts:[{t, d:[…], w}], group}
    (firma da eşdeğer kart da yoksa None)."""
    rel = m.get("relations") or {}
    firms = rel.get("firms") or []
    alts = rel.get("alternatives") or []
    if not firms and not alts:
        return None
    names: Dict[str, str] = {}
    for f in firms:
        names.setdefault(f["key"], f["name"])
    # Eşdeğer kartların firmaları da sayılır: kart tedarikçisi + kendi
    # lotlarından gelen son alım ve numune firmaları (alt satırlarda yazılanlar
    # — kartında tedarikçi yazmayan eşdeğer kart canlıda yaygın).
    for a in alts:
        if a.get("supplier_key"):
            names.setdefault(a["supplier_key"], a["supplier"])
        for x in ([a["last"]] if a.get("last") else []) + list(a.get("samples") or []):
            if x.get("key"):
                names.setdefault(x["key"], x["name"])
    nm = list(names.values())
    title = f"Tedarikçiler ({len(nm)})" + (": " + " · ".join(nm[:4]) + (" …" if len(nm) > 4 else "") if nm else "")
    grp = rel.get("group")
    return {"title": title, "count": len(nm),
            "firms": [{"t": f["name"], "d": firm_lines(f, currency)} for f in firms],
            "alts_title": ("Aynı malzeme — diğer kartlar" + (f" («{grp['name']}» grubu)" if grp else "")) if alts else None,
            "alts": [{"t": f"«{a['name']}»" + (f" — {a['supplier']}" if a.get("supplier") else ""),
                      "d": alt_lines(a, m.get("unit")), "w": bool(a.get("unit_mismatch"))} for a in alts],
            "group": grp}


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
            lbl = source_text(s.get("source"), s.get("source_label"))
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
        out.append(("Tutar = alınacak × seçilen teklifin birim fiyatı (önce malzeme tercihi ve “tercih "
                    "edilen” firma, sonra en ucuz; “bitirilecek” ve “bu malzemede alma” firmaların "
                    "teklifleri seçilmedi). " if pr.get("status_used") else
                    "Tutar = alınacak × en ucuz birim fiyat. ")
                   + ("Ambalaj katına yuvarlanmış miktar ve tutarı ayrıca gösterildi."
                      if pr.get("round_to_package") else
                      "Firmalar tam ambalaj sattığı için gerçek alım biraz daha fazla olabilir."))
        if pr.get("inactive_listed"):
            out.append("Pasife alınmış (silinmiş) tedarikçilerin teklifleri seçilmedi; listede gri "
                       "“pasif tedarikçi” olarak yazıldı.")
    if opts.get("subtract_open_orders"):
        out.append("Açık siparişler (sipariş verildi işareti) yolda sayılıp alınacaktan düşüldü.")
    out.append("Numune lotları stoğa dahil değildir; eksi stoklu kartlar 0 sayıldı.")
    rows_all = (result.get("materials") or []) + (result.get("held") or [])
    po_used = any((m.get("stock_phase_out") or 0) > 0 for m in rows_all)
    if any((m.get("material_group") or {}).get("alts") for m in rows_all):
        out.append("“Aynı malzeme” grubundaki diğer tedarikçi kartları (eşdeğer kartlar) satırlarda bilgi "
                   "olarak yazıldı; lab bunları ayrı ürün saydığı için stokları ihtiyaçtan düşülmedi"
                   + (" (bitirilecek tedarikçi kartları hariç)." if po_used else "."))
    if po_used:
        out.append("“Bitirilecek” (ya da bu malzemede alınmayacak) tedarikçinin aynı malzeme kartındaki stok "
                   "önce kullanılacağı için ihtiyaçtan düşüldü; satırda hangi karttan ne kadar olduğu yazıyor.")
    if pr.get("via_used") or pr.get("via_listed"):
        out.append("Aynı malzeme grubundaki diğer kartların fiyatları “eşdeğer kart «…»” etiketiyle yazıldı. "
                   "Eşdeğer kartın teklifi yalnız malzeme tercihi ya da “tercih edilen” firmaysa, ya da "
                   "malzemenin kendi teklifleri (bitirilecek / alma) elendiyse seçilir; öbürleri gri, "
                   "yalnız bilgi.")
    for e in result.get("excluded") or []:
        out.append(f"{e['name']} {e['need_text']} listede yok ({e['reason']}).")
    for h in result.get("held") or []:
        txt = f"{h['name']} ({h['display']['buy_text']}) "
        txt += (f"bekletiliyor: {h['reason'].rstrip('. ')}" if h.get("reason") else "teyit bekliyor")
        txt += "; listeye ve toplama girmedi."
        if h.get("amount") is not None and h.get("supplier"):
            txt += (f" Alınırsa {'seçilen' if pr.get('status_used') else 'en ucuz'} teklifle "
                    f"({h['supplier']}, {price_text(h['price'])} "
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
    picked = pr.get("status_used")
    add("raw_priced", raw_p, total=tot(raw_p),
        summary=f"{len(raw_p)} kalem, toplam {money(tot(raw_p), cur)}",
        subtitle=("Kalın yazılan tedarikçi seçilen teklif: önce malzeme tercihi ve “tercih edilen” firma, "
                  "sonra en ucuz; “bitirilecek” ve “bu malzemede alma” firmalar seçilmez. Tutar onunla "
                  "hesaplandı. Gri satırlar diğer teklifler ve sistemdeki kayıtlar." if picked else
                  "Kalın yazılan tedarikçi en ucuz olanı; tutar onunla hesaplandı. "
                  "Gri satırlar diğer teklifler ve sistemdeki kayıtlar."))
    add("raw_unpriced", raw_u, summary=f"{len(raw_u)} kalem",
        subtitle="Satırda, sistemde bu malzeme için kayıtlı firma (stok kartı, son alım, numune, sipariş) "
                 "ve aynı malzemenin diğer kartları yazıyor.")
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
        subtitle=("Her malzeme seçilen teklifi veren firmanın altında (tercih, sonra en ucuz)." if picked
                  else "Her malzeme en ucuz teklifi veren firmanın altında."))
    cands = list(sup.get("candidates") or [])
    if sup.get("unrelated"):
        cands.append({"key": "__none__", "name": "Sistemde hiçbir firmayla ilişkisi yok",
                      "items": [dict(u, reasons=[]) for u in sup["unrelated"]],
                      "count": len(sup["unrelated"]), "contact": None,
                      "contact_line": "Yeni tedarikçi bulunmalı (fiyat listesindeki firmalara da sorulabilir).",
                      "none": True})
    add("candidates", cands, summary=f"{len(cands)} grup",
        subtitle="Bu firmalar sistemde o malzemeyle ilişkili görünüyor (stok kartı, son alım, numune, "
                 "sipariş ya da aynı malzemenin başka tedarikçi kartı — eşdeğer kart). "
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
    if chk.get("phase_out_firms"):
        chk_rows.append({"code": "phase_out_firms", "suppliers": chk["phase_out_firms"],
                         "text": "“Bitirilecek — alma” işaretli firmalar (teklifleri seçilmedi, adaylara "
                                 "girmedi): " + ", ".join(s["name"] + (f" ({s['reason']})" if s.get("reason") else "")
                                                          for s in chk["phase_out_firms"]) + "."})
    if chk.get("status_conflicts"):
        chk_rows.append({"code": "status_conflicts", "suppliers": chk["status_conflicts"],
                         "text": "Aynı firmanın kartları farklı işaretlenmiş (durumu birleştirin ya da "
                                 "eşitleyin): " + "; ".join(
                                     f"{c['name']}: " + " / ".join(
                                         f"“{n}” {STATUS_TEXT.get(st, st)}"
                                         for st, ns in sorted(c["names_by_status"].items()) for n in ns)
                                     for c in chk["status_conflicts"]) + "."})
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
