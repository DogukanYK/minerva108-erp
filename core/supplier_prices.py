"""
Tedarikçi fiyat listesi — Işık Hanım'ın "Stok Son Durum" Excel'ini sisteme alıp
satın alma raporunu otomatik dolduran çekirdek mantık.

Akış:
  • parse_stok_son_durum(data)  → Excel'i (onun sütun düzeniyle) satırlara çevirir
  • import_prices(db, rows, …)  → malzeme/tedarikçi eşler, SupplierPrice'a upsert eder
  • prices_for_items(db, …)     → rapor için malzeme başına (ucuzdan) tedarikçi listesi

Beklenen Excel düzeni (Işık Hanım'ın dosyası):
  A Malzeme · B Kategori · C Birim · D Toplam Gereken · E Mevcut Stok · F ALINACAK
  G Alınabilecek Miktar(1) · H TEDARİKÇİ-1 · I Birim Fiyat(1)
  J TEDARİKÇİ-2 · K Alınabilecek Miktar(2) · L Birim Fiyat(2)
  M TEDARİKÇİ-3 · N Alınabilecek Miktar(3) · O Birim Fiyat(3)
(Tedarikçi-1'in paketi adından ÖNCE, 2 ve 3'ün paketi adından SONRA — onun düzeni.)
"""
from io import BytesIO
from typing import List, Optional

from sqlalchemy.orm import Session

from database import Item, Supplier, SupplierPrice

# 0-indeksli sütun konumları — Işık Hanım'ın STOK SON DURUM düzeni
COL_MATERIAL = 0
_SUPPLIER_BLOCKS = [
    {"name": 7,  "package": 6,  "price": 8},   # Tedarikçi-1: G paket, H ad, I fiyat
    {"name": 9,  "package": 10, "price": 11},  # Tedarikçi-2: J ad, K paket, L fiyat
    {"name": 12, "package": 13, "price": 14},  # Tedarikçi-3: M ad, N paket, O fiyat
]

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


def import_prices(db: Session, rows: List[dict], domain: str) -> dict:
    """Satırları SupplierPrice'a yazar (malzeme başına ESKİ satırları değiştirir).

    Malzeme `Item.name` ile, tedarikçi `Supplier.name` ile (Türkçe-katlanmış)
    eşlenir. Eşleşmeyen tedarikçi serbest metin olarak saklanır (supplier_id NULL).
    Döner: özet (kaç malzeme güncellendi, kaç fiyat satırı, eşleşmeyenler).
    """
    item_by_norm = {}
    for it in db.query(Item).filter(Item.domain == domain, Item.is_active == True).all():
        item_by_norm.setdefault(normalize(it.name), it)
    sup_by_norm = {}
    for sp in db.query(Supplier).filter(Supplier.domain == domain).all():
        sup_by_norm.setdefault(normalize(sp.name), sp)

    items_updated = 0
    prices_inserted = 0
    unmatched_materials: List[str] = []
    unmatched_suppliers = set()

    for row in rows:
        it = item_by_norm.get(normalize(row["material"]))
        if not it:
            unmatched_materials.append(row["material"])
            continue
        # Replace semantics: bu malzemenin eski fiyat satırlarını sil
        db.query(SupplierPrice).filter(
            SupplierPrice.item_id == it.id, SupplierPrice.domain == domain
        ).delete(synchronize_session=False)
        for s in row["suppliers"]:
            sup = sup_by_norm.get(normalize(s["name"]))
            if not sup:
                unmatched_suppliers.add(s["name"])
            db.add(SupplierPrice(
                item_id=it.id,
                supplier_id=(sup.id if sup else None),
                supplier_name=s["name"],
                package_size=s["package"],
                unit_price=s["price"],
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
    }


def prices_for_items(db: Session, item_ids: List[int], domain: str, limit: int = 3) -> dict:
    """Rapor için: { item_id: [ {supplier_name, package_size, unit_price}, … ] }.

    Her malzeme için fiyatı OLAN tedarikçiler ucuzdan pahalıya; fiyatı olmayanlar
    (None) sona. En çok `limit` tedarikçi.
    """
    if not item_ids:
        return {}
    out: dict = {}
    q = (db.query(SupplierPrice)
         .filter(SupplierPrice.item_id.in_(item_ids), SupplierPrice.domain == domain)
         .all())
    for sp in q:
        out.setdefault(sp.item_id, []).append({
            "supplier_name": sp.supplier_name or (sp.supplier.name if sp.supplier else "—"),
            "package_size": sp.package_size,
            "unit_price": sp.unit_price,
        })
    for iid, lst in out.items():
        lst.sort(key=lambda x: (x["unit_price"] is None, x["unit_price"] if x["unit_price"] is not None else 0.0))
        out[iid] = lst[:limit]
    return out
