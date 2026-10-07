"""
Tedarikçi fiyat listesi — Işık Hanım'ın "Stok Son Durum" Excel'ini sisteme alıp
satın alma raporunu otomatik dolduran çekirdek mantık.

Akış:
  • parse_stok_son_durum(data)  → Excel'i (onun sütun düzeniyle) satırlara çevirir
  • import_prices(db, rows, …)  → malzeme/tedarikçi eşler, SupplierPrice'a upsert eder
                                  (yalnız Excel kaynaklı satırları değiştirir; elle
                                  girilen `manual` satır korunur)
  • price_unit_ok / serialize_price → elle fiyat uçları (routers/reports.py)
  • prices_for_items(db, …)     → rapor için malzeme başına (ucuzdan) tedarikçi listesi
  • price_per_purchase_unit(…)  → fiyatı malzemenin alım birimine (kg / l / adet) çevirir

Fiyat temeli (2026-10): Stok Son Durum listesi **USD / kg**. Eskiden para birimi
kolonu model varsayılanıyla 'TRY' yazılıyor, birim hiç tutulmuyordu — panel her
fiyatı "₺" gösteriyordu. Artık her satır `currency` + `price_unit` (kg|l|adet) +
`source` / `source_label` / `quoted_at` taşır; adet birimli malzemenin fiyatı
DAİMA adet başınadır (`default_price_unit`). Eski satırlar `database.
_backfill_supplier_price_units()` ile bir kez USD/kg(adet) olarak işaretlendi.

Beklenen Excel düzeni (Işık Hanım'ın dosyası):
  A Malzeme · B Kategori · C Birim · D Toplam Gereken · E Mevcut Stok · F ALINACAK
  G Alınabilecek Miktar(1) · H TEDARİKÇİ-1 · I Birim Fiyat(1)
  J TEDARİKÇİ-2 · K Alınabilecek Miktar(2) · L Birim Fiyat(2)
  M TEDARİKÇİ-3 · N Alınabilecek Miktar(3) · O Birim Fiyat(3)
(Tedarikçi-1'in paketi adından ÖNCE, 2 ve 3'ün paketi adından SONRA — onun düzeni.)
"""
from datetime import date
from io import BytesIO
from typing import List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from database import Item, Supplier, SupplierPrice

# 0-indeksli sütun konumları — Işık Hanım'ın STOK SON DURUM düzeni
COL_MATERIAL = 0
_SUPPLIER_BLOCKS = [
    {"name": 7,  "package": 6,  "price": 8},   # Tedarikçi-1: G paket, H ad, I fiyat
    {"name": 9,  "package": 10, "price": 11},  # Tedarikçi-2: J ad, K paket, L fiyat
    {"name": 12, "package": 13, "price": 14},  # Tedarikçi-3: M ad, N paket, O fiyat
]

# Fiyat temeli sözlüğü — form/uç doğrulaması ve rapor aynı listeyi kullanır
CURRENCIES = ("USD", "EUR", "TRY")
PRICE_UNITS = ("kg", "l", "adet")
# İçe aktarmada seçilebilen LİSTE birimi yalnız ağırlık/hacim temelidir: adet
# birimli malzeme zaten otomatik 'adet' alır, 'adet' seçeneği ise listedeki her
# g/kg/ml/lt kalemine 'adet' yazıp `price_per_purchase_unit`'te hepsini
# "uyuşmuyor"a düşürürdü (satın alma planında hammadde fiyatı kalmazdı).
LIST_PRICE_UNITS = ("kg", "l")
SOURCE_STOK_SON_DURUM = "stok_son_durum"
# Lab'ın elle girdiği (ya da düzenlediği) satır — POST/PUT /api/supplier-prices.
# İçe aktarma bu satırları SİLMEZ ve aynı firmanın teklifini üstüne yazmaz.
SOURCE_MANUAL = "manual"
SOURCE_LABEL_MAX = 120

# Malzeme birimi aileleri (Item.unit: g/kg/ml/lt/adet/kutu/rulo …).  Ağırlık ve
# hacim dışındaki her birim SAYILAN birimdir → fiyatı adet başına.
_MASS_UNITS = {"g", "gr", "kg"}
_VOLUME_UNITS = {"ml", "l", "lt"}
LITRE_KG_NOTE = "1 l ≈ 1 kg kabulüyle"

# Türkçe harf katlama (eşleştirme için) — ç/ğ/ı/İ/ö/ş/ü → ascii
_TR_FOLD = str.maketrans({
    "ç": "c", "Ç": "c", "ğ": "g", "Ğ": "g", "ı": "i", "İ": "i",
    "ö": "o", "Ö": "o", "ş": "s", "Ş": "s", "ü": "u", "Ü": "u", "I": "i",
})


def clean_name(v) -> Optional[str]:
    """Görüntü adı: trim + iç boşlukları teke indir.  Boşsa None."""
    if v is None:
        return None
    s = " ".join(str(v).split())
    return s or None


def normalize(v) -> str:
    """Eşleştirme anahtarı: Türkçe-katlanmış, küçük harf, boşluk-normalize."""
    s = clean_name(v)
    if not s:
        return ""
    return " ".join(s.translate(_TR_FOLD).lower().split())


def _num(v) -> Optional[float]:
    """Hücreyi sayıya çevir; Türkçe ondalık virgülünü destekler.  Yoksa None."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip().replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def parse_stok_son_durum(data: bytes) -> List[dict]:
    """Yüklenen .xlsx baytlarını satırlara çevirir.

    Döner: [{ "material": str, "suppliers": [{name, package, price}, …] }, …]
    Başlık satırı (1. satır) atlanır; malzeme adı boş satırlar atlanır.
    """
    wb = openpyxl_load(data)
    ws = wb[wb.sheetnames[0]]
    rows: List[dict] = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r:
            continue
        material = clean_name(r[COL_MATERIAL] if len(r) > COL_MATERIAL else None)
        if not material:
            continue
        suppliers = []
        for blk in _SUPPLIER_BLOCKS:
            name = clean_name(r[blk["name"]] if len(r) > blk["name"] else None)
            if not name:
                continue
            suppliers.append({
                "name": name,
                "package": _num(r[blk["package"]] if len(r) > blk["package"] else None),
                "price": _num(r[blk["price"]] if len(r) > blk["price"] else None),
            })
        rows.append({"material": material, "suppliers": suppliers})
    return rows


def openpyxl_load(data: bytes):
    import openpyxl
    return openpyxl.load_workbook(BytesIO(data), data_only=True, read_only=True)


def _unit_family(item_unit) -> str:
    """'mass' | 'volume' | 'count' — boş/bilinmeyen birim sayılan birimdir."""
    u = (item_unit or "").strip().lower()
    if u in _MASS_UNITS:
        return "mass"
    if u in _VOLUME_UNITS:
        return "volume"
    return "count"


def price_unit_ok(item_unit, price_unit) -> bool:
    """Elle girilen fiyat biriminin malzemeye uygunluğu (birim ailesi).

    Sayılan (adet/kutu/rulo…) malzeme → yalnız 'adet'; kütle/hacim malzemesi
    → 'kg' ya da 'l' (çapraz kg↔l `price_per_purchase_unit`'te "1 l ≈ 1 kg"
    notuyla kullanılır).  Adet ↔ kg/l hiçbir zaman çevrilemez — öyle bir satır
    satın alma planında "fiyat yok" olurdu."""
    pu = (price_unit or "").strip().lower()
    if _unit_family(item_unit) == "count":
        return pu == "adet"
    return pu in LIST_PRICE_UNITS


def default_price_unit(item_unit, list_unit: str = "kg") -> str:
    """Bir malzemenin fiyat satırına yazılacak `price_unit`.

    Adet (ve kutu/rulo gibi sayılan) birimli malzemede fiyat DAİMA adet
    başınadır; ağırlık/hacim malzemesinde listenin birimi (`list_unit`,
    Stok Son Durum için 'kg') geçerlidir.  İçe aktarma ve tek seferlik geri
    doldurma bu tek kuralı paylaşır."""
    return "adet" if _unit_family(item_unit) == "count" else list_unit


def import_prices(db: Session, rows: List[dict], domain: str, *,
                  currency: str = "USD", price_unit: str = "kg",
                  source_label: Optional[str] = None,
                  quoted_at: Optional[date] = None) -> dict:
    """Satırları SupplierPrice'a yazar (malzeme başına ESKİ Excel satırlarını değiştirir).

    Malzeme `Item.name` ile, tedarikçi AKTİF `Supplier.name` ile (Türkçe-
    katlanmış; aynı anahtarda en eski kart) eşlenir — pasife alınmış mükerrer
    kart (TATLİDİLİMLER / TATLIDİLİMLER) fiyatı üstüne çekmesin. Eşleşmeyen
    tedarikçi serbest metin olarak saklanır (supplier_id NULL).
    Silme YALNIZ `source` stok_son_durum ya da boş (eski) satırlarda; lab'ın
    elle girdiği `manual` satır korunur (`kept_manual`) ve aynı firmanın Excel
    teklifi o malzemeye YAZILMAZ (`skipped_manual`) — aynı firma iki kez
    görünmesin, elle girilen değer ezilmesin.
    Her satır `currency` + `price_unit` + kaynak bilgisiyle yazılır; adet
    birimli malzemeler `price_unit`'ten bağımsız olarak 'adet' alır.
    `price_unit` listenin ağırlık/hacim temelidir → yalnız `LIST_PRICE_UNITS`
    (kg | l); 'adet' ValueError.
    Döner: özet (kaç malzeme güncellendi, kaç fiyat satırı, eşleşmeyenler).
    """
    currency = (currency or "").strip().upper()
    price_unit = (price_unit or "").strip().lower()
    if currency not in CURRENCIES:
        raise ValueError(f"Geçersiz para birimi: {currency!r}")
    if price_unit not in LIST_PRICE_UNITS:
        raise ValueError(f"Geçersiz liste fiyat birimi: {price_unit!r} (kg ya da l)")
    source_label = clean_name(source_label)
    if source_label:
        source_label = source_label[:SOURCE_LABEL_MAX]

    item_by_norm = {}
    for it in db.query(Item).filter(Item.domain == domain, Item.is_active == True).all():
        item_by_norm.setdefault(normalize(it.name), it)
    sup_by_norm = {}
    for sp in (db.query(Supplier)
               .filter(Supplier.domain == domain, Supplier.is_active == True)  # noqa: E712
               .order_by(Supplier.id).all()):
        sup_by_norm.setdefault(normalize(sp.name), sp)

    items_updated = 0
    prices_inserted = 0
    kept_manual = 0
    skipped_manual = 0
    unmatched_materials: List[str] = []
    unmatched_suppliers = set()

    for row in rows:
        it = item_by_norm.get(normalize(row["material"]))
        if not it:
            unmatched_materials.append(row["material"])
            continue
        # Replace semantics: bu malzemenin eski EXCEL satırlarını sil (elle
        # girilenler kalır).  Kaynağı boş satır içe aktarma öncesi döneme ait.
        db.query(SupplierPrice).filter(
            SupplierPrice.item_id == it.id, SupplierPrice.domain == domain,
            or_(SupplierPrice.source == SOURCE_STOK_SON_DURUM, SupplierPrice.source.is_(None)),
        ).delete(synchronize_session=False)
        manual_keys = set()
        for m in (db.query(SupplierPrice)
                  .filter(SupplierPrice.item_id == it.id, SupplierPrice.domain == domain).all()):
            kept_manual += 1
            if m.supplier_id is not None:
                manual_keys.add(("id", m.supplier_id))
            if normalize(m.supplier_name):
                manual_keys.add(("name", normalize(m.supplier_name)))
        for s in row["suppliers"]:
            sup = sup_by_norm.get(normalize(s["name"]))
            if (sup is not None and ("id", sup.id) in manual_keys) or \
                    ("name", normalize(s["name"])) in manual_keys:
                skipped_manual += 1
                continue
            if not sup:
                unmatched_suppliers.add(s["name"])
            db.add(SupplierPrice(
                item_id=it.id,
                supplier_id=(sup.id if sup else None),
                supplier_name=s["name"],
                package_size=s["package"],
                unit_price=s["price"],
                currency=currency,
                price_unit=default_price_unit(it.unit, price_unit),
                source=SOURCE_STOK_SON_DURUM,
                source_label=source_label,
                quoted_at=quoted_at,
                domain=domain,
            ))
            prices_inserted += 1
        items_updated += 1

    db.commit()
    return {
        "items_updated": items_updated,
        "prices_inserted": prices_inserted,
        "unmatched_materials": unmatched_materials,
        "unmatched_suppliers": sorted(unmatched_suppliers),
        "matched_suppliers": prices_inserted - len(unmatched_suppliers),
        "kept_manual": kept_manual,
        "skipped_manual": skipped_manual,
        "currency": currency,
        "price_unit": price_unit,
        "source_label": source_label,
        "quoted_at": quoted_at.isoformat() if quoted_at else None,
    }


def prices_for_items(db: Session, item_ids: List[int], domain: str, limit: int = 3) -> dict:
    """Rapor için: { item_id: [ {supplier_name, package_size, unit_price,
    currency, price_unit, source_label, quoted_at}, … ] }.

    Her malzeme için fiyatı OLAN tedarikçiler ucuzdan pahalıya; fiyatı olmayanlar
    (None) sona. En çok `limit` tedarikçi.  Bir firmanın elle girilmiş
    (`manual`) satırı varsa aynı firmanın Excel satırı listeye girmez —
    yoksa aynı firma iki sütun kaplar ve 3. tedarikçi kesilirdi.
    """
    if not item_ids:
        return {}
    out: dict = {}
    q = (db.query(SupplierPrice)
         .filter(SupplierPrice.item_id.in_(item_ids), SupplierPrice.domain == domain)
         .all())

    def _firm_keys(sp):
        keys = set()
        if sp.supplier_id is not None:
            keys.add(("id", sp.supplier_id))
        if normalize(sp.supplier_name):
            keys.add(("name", normalize(sp.supplier_name)))
        return keys

    manual_firms: dict = {}
    for sp in q:
        if sp.source == SOURCE_MANUAL:
            manual_firms.setdefault(sp.item_id, set()).update(_firm_keys(sp))
    for sp in q:
        if sp.source != SOURCE_MANUAL and _firm_keys(sp) & manual_firms.get(sp.item_id, set()):
            continue
        out.setdefault(sp.item_id, []).append({
            "supplier_name": sp.supplier_name or (sp.supplier.name if sp.supplier else "—"),
            "package_size": sp.package_size,
            "unit_price": sp.unit_price,
            "currency": sp.currency,
            "price_unit": sp.price_unit,
            "source_label": sp.source_label,
            "quoted_at": sp.quoted_at,
        })
    for iid, lst in out.items():
        lst.sort(key=lambda x: (x["unit_price"] is None, x["unit_price"] if x["unit_price"] is not None else 0.0))
        out[iid] = lst[:limit]
    return out


def _offer_get(offer, key):
    """Teklif dict'i (prices_for_items) ya da SupplierPrice nesnesi."""
    if isinstance(offer, dict):
        return offer.get(key)
    return getattr(offer, key, None)


def price_per_purchase_unit(offer, item_unit) -> Tuple[Optional[float], str, Optional[str]]:
    """Teklif fiyatını malzemenin ALIM birimine çevirir → (fiyat, alım birimi, not|None).

    Alım birimi: g/kg malzeme → 'kg', ml/l malzeme → 'l', diğerleri → 'adet'.
      • kg fiyatı ml/l malzemeye (ya da l fiyatı g/kg malzemeye) uygulanırsa
        fiyat aynen kullanılır, not "1 l ≈ 1 kg kabulüyle" eklenir.
      • adet ↔ kg/l çevrilemez: fiyat None döner + not (yanlış tutar
        üretmektense "fiyat yok" görünmesi güvenli).
      • `price_unit` boş eski satır → `default_price_unit` kuralı.
    Para birimine dokunmaz (çeviri `core.fx.convert`).
    """
    family = _unit_family(item_unit)
    purchase_unit = {"mass": "kg", "volume": "l"}.get(family, "adet")
    price = _offer_get(offer, "unit_price")
    basis = (_offer_get(offer, "price_unit") or "").strip().lower() or default_price_unit(item_unit)
    if price is None:
        return None, purchase_unit, None
    price = float(price)
    if basis == purchase_unit:
        return price, purchase_unit, None
    if {basis, purchase_unit} == {"kg", "l"}:
        return price, purchase_unit, LITRE_KG_NOTE
    return None, purchase_unit, (
        f"Fiyat birimi ({basis}) malzemenin alım birimiyle ({purchase_unit}) "
        f"uyuşmuyor — fiyat kullanılmadı")


def serialize_price(sp, item=None, supplier_status: Optional[str] = None) -> dict:
    """Fiyat satırının uç yanıtı — GET /api/supplier-prices (grup içi satır),
    GET /api/suppliers/{id}/prices ve POST/PUT yanıtı aynı şekli paylaşır.
    `item` verilirse malzeme adı/birimi eklenir; tarih alanları ISO (quoted_at)
    ve TR saati (updated_at)."""
    from database import to_tr
    out = {
        "id": sp.id,
        "item_id": sp.item_id,
        "supplier_id": sp.supplier_id,
        "supplier_name": sp.supplier_name or (sp.supplier.name if sp.supplier else "—"),
        "package_size": sp.package_size,
        "unit_price": sp.unit_price,
        "currency": sp.currency,
        "price_unit": sp.price_unit,
        "source": sp.source,
        "source_label": sp.source_label,
        "quoted_at": sp.quoted_at.isoformat() if sp.quoted_at else None,
        "note": sp.note,
        "created_by": sp.created_by,
        "updated_by": sp.updated_by,
        "updated_at": to_tr(sp.updated_at).strftime("%d.%m.%Y %H:%M") if sp.updated_at else "",
        "matched": sp.supplier_id is not None,
        "supplier_status": supplier_status,
    }
    if item is not None:
        out["material"] = item.name
        out["unit"] = item.unit or ""
        out["category"] = item.category or ""
    return out
