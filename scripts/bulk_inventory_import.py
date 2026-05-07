"""
Bulk Inventory Importer — Phase 10
====================================

Lab'ın elindeki 7 Excel dosyasını sisteme aktaran tek-seferlik script.

Karar matrisi (lab tarafından onaylandı, 2026-05-06):

  Hammadde  → Sayfa10 (Nisan snapshot) authoritative
              "4.276" yazımı = 4276g (Türkçe binlik ayracı)
              Tarih kolonu yok, birim isimden tespit (ml ise mevcut DB'den
              koru, yoksa default 'g')

  Ambalaj   → Sayfa3 Nisan kolonu authoritative (en güncel)
              pkg_type otomatik tespit: ŞİŞE/KAVANOZ/POMPA/KAPAK
              Tip boş kalanlar genel "Ambalaj" altında görünür

  Etiket    → 4 ayrı dosya, son sayfadaki sayılar
              Serenida: yan + üst etiket × TR + ENG (4'e kadar SKU)
              Evanira:  TR + ENG (yan etiket sayılır, 2 SKU)
              Red Clover ve Altın Seri: tek sayı = TR+ENG TOPLAMI
                (tek "Etiket" SKU, dilsiz)
              Altın Seri'de "X ÜST + Y YAN" varsa ikiye böl

  Bitmiş    → Showroom Sayım dosyası 3 sayfa
   Ürün       Lacivert Seri = "RED CLOVER" geçenler
              Altın Seri    = diğer Minerva-108
              Serenida ve Evanira ailesi ayrı
              Sayım = Adjustment (audit'e geçer)

  Eşleştirme: isim normalizasyonu (büyük harf, çift boşluk temizle, trim);
              aynı isim varsa stok düzeltme (Adjustment), yoksa yeni item.
              Aynı dosyada bir ürün iki kez geçerse miktar toplanır.

Kullanım:
  python scripts/bulk_inventory_import.py             # dry-run (default)
  python scripts/bulk_inventory_import.py --commit    # gerçek yazım

Dry-run özet bir JSON dökümü yazdırır; --commit eklemeden DB'ye dokunmaz.
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Optional

import openpyxl

# Script projeyi parent'tan import etmeli
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from database import SessionLocal, Item, Inventory, Transaction, Supplier   # noqa: E402

# ─── Konfigürasyon ────────────────────────────────────────────────────────

DOWNLOADS = Path("/Users/dogukan/Downloads")

FILES = {
    "hammadde":          DOWNLOADS / "HAMMADDE STOK TAKİP LİSTESİ.xlsx",
    "ambalaj":           DOWNLOADS / "AMBALAJ STOK LİSTESİ (08.01.2026).xlsx",
    "showroom":          DOWNLOADS / "SHOWROOM SAYIM GÜNCEL.xlsx",
    "etiket_red_clover": DOWNLOADS / "MİNERVA RED CLOVER ETİKET SAYISI.xlsx",
    "etiket_altin":      DOWNLOADS / "MİNERVA 108 ALTIN SERİ ETİKET SAYISI 28.01.2026 (2).xlsx",
    "etiket_serenida":   DOWNLOADS / "YENİ GELEN ETİKET SAYISI  SERENİDA 14.01.2026 (2).xlsx",
    "etiket_evanira":    DOWNLOADS / "YENİ GELEN ETİKET SAYISI EVANİRA (2).xlsx",
}

ACTOR = "Toplu Excel İçe Aktarımı"     # Transaction.performed_by


# ─── Yardımcılar ──────────────────────────────────────────────────────────

def norm(s) -> str:
    """Eşleştirme için normalize: büyük harf, fazla boşluk temizle, trim."""
    if s is None:
        return ""
    out = str(s).strip().upper()
    out = re.sub(r"\s+", " ", out)
    return out


def parse_qty_clean(raw) -> float:
    """
    Sayfa10 (Nisan snapshot) gibi temizlenmiş sayılar için.
    'PEMBE KAOLİN KİL' satırında '4276' gibi tam sayı geliyor.
    Bazen '1138', bazen 0 veya boş.  Öneki/ekiyle gelirse ('1 KG', '162ML')
    de yakalayalım — ama Sayfa10'da pek olmaz.
    """
    if raw is None:
        return 0.0
    s = str(raw).strip().upper()
    if not s or s in ("—", "-", "NAN"):
        return 0.0
    # Birim ekini at
    m = re.match(r"([0-9.,]+)\s*(KG|GR|G|ML)?$", s)
    if not m:
        # Yedek: noktalı sayı
        s2 = s.replace(",", ".")
        try:
            return float(s2)
        except ValueError:
            return 0.0
    num_str, unit = m.group(1), m.group(2) or ""
    num_str = num_str.replace(",", ".")
    # Türkçe binlik: "4.276" gibi 3 ondalık basamak → binlik ayracı say
    if re.match(r"^\d+\.\d{3}$", num_str):
        num_str = num_str.replace(".", "")
    try:
        val = float(num_str)
    except ValueError:
        return 0.0
    if unit == "KG":
        val *= 1000
    return val


def detect_pkg_type(name: str) -> Optional[str]:
    """Ambalaj kalemi adından alt-tipi tespit et."""
    n = name.upper()
    # Sıralama önemli: 'POMPA' içinde 'POM' var; 'KAVANOZ' belirgin
    if "ETİKET" in n or "ETIKET" in n:
        return "etiket"
    if "POMPA" in n:
        return "pompa"
    if "KAVANOZ" in n:
        return "kavanoz"
    if "ŞİŞE" in n or "SISE" in n:
        return "şişe"
    if "KAPAK" in n:
        return "kapak"
    return None


def parse_int_loose(raw) -> int:
    """Çetrefil hücreleri tek sayıya indirgemek için. '20' → 20, '129' → 129,
       '20+21+9' → 50, '50YAN' → 50, 'YAN 70' → 70, '' → 0."""
    if raw is None:
        return 0
    s = str(raw).strip()
    if not s:
        return 0
    # Tüm rakamları topla (toplama formülü gibi davransın)
    # 'X+Y+Z' formatını yakalamak için + ile böl, her parçayı parse et
    if "+" in s:
        total = 0
        for part in re.split(r"[+\-]", s):
            nums = re.findall(r"\d+", part)
            if nums:
                total += int(nums[0])
        return total
    nums = re.findall(r"\d+", s)
    if not nums:
        return 0
    return int(nums[0])


# ─── 1. HAMMADDE Parser (Sayfa10 = Nisan snapshot) ────────────────────────

def parse_hammadde():
    """
    Sayfa10 başlığı: '27 NİSAN AYI 2026'
    Row 1: ['27 NİSAN AYI 2026']
    Row 2: ['HAMMADDE İSİM', 'GR', 'MARKASI']
    Row 3+: data

    Fakat Sayfa10'da birim bilgisi yok (sadece sayı). Birim için fallback:
    Sayfa1-9, 11'de aynı isimle eşleşeni bul, ondan birimi al; yoksa 'g'.
    """
    fp = FILES["hammadde"]
    wb = openpyxl.load_workbook(fp, data_only=True, read_only=True)

    # Önce Sayfa1-9, 11'den birim/marka indeksi kur
    unit_index = {}     # {norm_name: 'g' / 'ml'}
    brand_index = {}    # {norm_name: brand_str}
    for sheet_name in wb.sheetnames:
        if sheet_name in ("Sayfa7", "Sayfa10"):
            continue
        ws = wb[sheet_name]
        for row in ws.iter_rows(min_row=3, values_only=True):
            if len(row) < 3:
                continue
            name_cell, qty_raw, brand_cell = row[1], row[2], (row[3] if len(row) > 3 else None)
            if not name_cell:
                continue
            name = norm(name_cell)
            if "DOLAP" in name or "KONTROL TARİH" in name or "HAMMADDE İSİM" in name:
                continue
            # Birim tespiti — qty ekinden
            s = str(qty_raw or "").upper()
            if "ML" in s:
                unit_index.setdefault(name, "ml")
            elif "KG" in s or "GR" in s or "G" in s:
                unit_index.setdefault(name, "g")
            if brand_cell and str(brand_cell).strip():
                brand_index.setdefault(name, str(brand_cell).strip())

    # Şimdi Sayfa10'dan kanonik veriyi oku
    rows = []
    ws = wb["Sayfa10"]
    for row in ws.iter_rows(min_row=3, values_only=True):
        if not row or not row[0]:
            continue
        name_raw = row[0]
        qty_raw  = row[1] if len(row) > 1 else None
        brand    = (row[2] if len(row) > 2 else None) or ""
        n = norm(name_raw)
        if not n or "HAMMADDE İSİM" in n:
            continue
        qty = parse_qty_clean(qty_raw)
        unit = unit_index.get(n, "g")
        rows.append({
            "name":     str(name_raw).strip(),
            "norm":     n,
            "qty":      qty,
            "unit":     unit,
            "brand":    str(brand).strip() if brand else (brand_index.get(n) or ""),
            "source":   f"{fp.name} :: Sayfa10",
        })

    wb.close()

    # Aynı isim iki kez → topla
    return _merge_by_norm(rows)


# ─── 2. AMBALAJ Parser (Sayfa3 Nisan kolonu) ──────────────────────────────

def parse_ambalaj():
    """
    Sayfa3 schema:
      Row 1: ['', 'OCAK', 'ŞUBAT', 'MART', 'NİSAN']
      Row 2: ['AMBALAJ ADI:', 'ADET', ...]
      Row 3+: data — col[0]=ad, col[4]=NİSAN
    """
    fp = FILES["ambalaj"]
    wb = openpyxl.load_workbook(fp, data_only=True, read_only=True)
    ws = wb["Sayfa3"]

    rows = []
    for row in ws.iter_rows(min_row=3, values_only=True):
        if not row or not row[0]:
            continue
        name_raw = str(row[0]).strip()
        if not name_raw or "AMBALAJ" in name_raw.upper() and "ADET" in (str(row[1] or "").upper()):
            continue   # header satırı
        # NİSAN kolonu = index 4
        nisan_raw = row[4] if len(row) > 4 else None
        try:
            qty = float(nisan_raw) if nisan_raw not in (None, "") else 0
        except (ValueError, TypeError):
            qty = parse_int_loose(nisan_raw)
        rows.append({
            "name":     name_raw,
            "norm":     norm(name_raw),
            "qty":      qty,
            "unit":     "adet",
            "pkg_type": detect_pkg_type(name_raw),
            "source":   f"{fp.name} :: Sayfa3 (NİSAN)",
        })

    wb.close()
    return _merge_by_norm(rows)


# ─── 3. ETİKET Parserlar ──────────────────────────────────────────────────

def parse_etiket_serenida():
    """
    Serenida son sayfa (Sayfa3) schema:
      Row 1: ['SERENİDA']
      Row 2: ['ÜRÜN İSMİ', 'ML', 'İNGİLİZCE SAYI', 'TÜRKÇE SAYI', 'üst etiket ing', 'üst trk']
      Row 3+:  ad, ml, eng_yan, trk_yan, eng_üst, trk_üst

    Çıktı: Her ürün+ml için 2 veya 4 SKU:
      şişe/tüp tipi (sadece yan): 2 SKU (TR yan, ENG yan)
      kavanoz tipi (üst+yan):    4 SKU (TR yan, ENG yan, TR üst, ENG üst)

    Hangi tip olduğunu üst etiket dolu mu boş mu ile anlarız.
    """
    fp = FILES["etiket_serenida"]
    wb = openpyxl.load_workbook(fp, data_only=True, read_only=True)
    ws = wb["Sayfa3"]

    rows = []
    for row in ws.iter_rows(min_row=3, values_only=True):
        if not row or not row[0]:
            continue
        name_raw = str(row[0]).strip()
        if not name_raw:
            continue
        ml      = str(row[1] or "").strip() if len(row) > 1 else ""
        eng_yan = parse_int_loose(row[2] if len(row) > 2 else None)
        trk_yan = parse_int_loose(row[3] if len(row) > 3 else None)
        eng_ust = parse_int_loose(row[4] if len(row) > 4 else None)
        trk_ust = parse_int_loose(row[5] if len(row) > 5 else None)

        prefix = name_raw + (f" {ml}ml" if ml else "")
        # Yan etiketler her ürün için var (ister şişe ister kavanoz)
        if eng_yan or trk_yan:
            rows.append(_etiket_row(f"{prefix} Etiket Yan (ENG)", eng_yan, fp.name, "Sayfa3"))
            rows.append(_etiket_row(f"{prefix} Etiket Yan (TR)",  trk_yan, fp.name, "Sayfa3"))
        # Üst etiket sadece kavanoz tipinde dolu — boş ise atla
        if eng_ust or trk_ust:
            rows.append(_etiket_row(f"{prefix} Etiket Üst (ENG)", eng_ust, fp.name, "Sayfa3"))
            rows.append(_etiket_row(f"{prefix} Etiket Üst (TR)",  trk_ust, fp.name, "Sayfa3"))
    wb.close()
    return _merge_by_norm(rows)


def parse_etiket_evanira():
    """
    Evanira son sayfa (Sayfa3) schema:
      Row 2: ['ÜRÜN İSİM', 'ML', 'ETİKET SAYI İNG', 'ETİKET SAYI TRK']
      Row 3+: ad, ml, eng, trk
    Tek tür etiket (yan ya da düz, ayrım yok); 2 SKU per ürün.
    """
    fp = FILES["etiket_evanira"]
    wb = openpyxl.load_workbook(fp, data_only=True, read_only=True)
    ws = wb["Sayfa3"]

    rows = []
    for row in ws.iter_rows(min_row=3, values_only=True):
        if not row or not row[0]:
            continue
        name_raw = str(row[0]).strip()
        if not name_raw:
            continue
        ml  = str(row[1] or "").strip() if len(row) > 1 else ""
        eng = parse_int_loose(row[2] if len(row) > 2 else None)
        trk = parse_int_loose(row[3] if len(row) > 3 else None)
        prefix = name_raw + (f" {ml}ml" if ml else "")
        if eng:
            rows.append(_etiket_row(f"{prefix} Etiket (ENG)", eng, fp.name, "Sayfa3"))
        if trk:
            rows.append(_etiket_row(f"{prefix} Etiket (TR)",  trk, fp.name, "Sayfa3"))
    wb.close()
    return _merge_by_norm(rows)


def parse_etiket_redclover():
    """
    Red Clover tek sayfa schema:
      Row 1: başlık
      Row 2+: ad, ml, etiket_sayısı (TR+ENG TOPLAM)
    Tek SKU per ürün, dilsiz.
    """
    fp = FILES["etiket_red_clover"]
    wb = openpyxl.load_workbook(fp, data_only=True, read_only=True)
    ws = wb["Sayfa1"]

    rows = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        if not row or not row[0]:
            continue
        name_raw = str(row[0]).strip()
        if not name_raw or "ETİKET SAYISI" in name_raw.upper():
            continue
        ml    = str(row[1] or "").strip() if len(row) > 1 else ""
        total = parse_int_loose(row[2] if len(row) > 2 else None)
        if total <= 0:
            continue
        full = name_raw + (f" {ml}" if ml and ml.upper() not in name_raw.upper() else "")
        rows.append(_etiket_row(f"{full.strip()} Etiket", total, fp.name, "Sayfa1"))
    wb.close()
    return _merge_by_norm(rows)


def parse_etiket_altin():
    """
    Altın Seri tek sayfa schema:
      Row 1: başlık
      Row 2: 'ALTIN SERİ'
      Row 3+: ad, ml, etiket_sayı (text)

    "etiket_sayı" hücresi:
      - Düz sayı (örn '40')     → tek SKU
      - 'X ÜST ETİKET+Y YAN ETİKET' → 2 SKU (üst, yan)
    Dilsiz (TR+ENG toplam).
    """
    fp = FILES["etiket_altin"]
    wb = openpyxl.load_workbook(fp, data_only=True, read_only=True)
    ws = wb["Sayfa1"]

    rows = []
    for row in ws.iter_rows(min_row=3, values_only=True):
        if not row or not row[0]:
            continue
        name_raw = str(row[0]).strip()
        if not name_raw or "ALTIN SERİ" in name_raw.upper() or "ETİKET" in name_raw.upper() and len(name_raw) < 25:
            continue
        ml      = str(row[1] or "").strip() if len(row) > 1 else ""
        cell    = str(row[2] or "").strip() if len(row) > 2 else ""
        if not cell:
            continue

        prefix = name_raw + (f" {ml}ml" if ml else "")
        upper = cell.upper()

        if "ÜST" in upper and "YAN" in upper:
            # "80ÜST + 38 YAN" gibi — sayıları çıkar
            ust_match = re.search(r"(\d+)\s*ÜST", upper)
            yan_match = re.search(r"(\d+)\s*YAN", upper)
            ust = int(ust_match.group(1)) if ust_match else 0
            yan = int(yan_match.group(1)) if yan_match else 0
            if ust > 0:
                rows.append(_etiket_row(f"{prefix} Etiket Üst", ust, fp.name, "Sayfa1"))
            if yan > 0:
                rows.append(_etiket_row(f"{prefix} Etiket Yan", yan, fp.name, "Sayfa1"))
        else:
            total = parse_int_loose(cell)
            if total > 0:
                rows.append(_etiket_row(f"{prefix} Etiket", total, fp.name, "Sayfa1"))
    wb.close()
    return _merge_by_norm(rows)


def _etiket_row(name, qty, file_name, sheet):
    return {
        "name":   name,
        "norm":   norm(name),
        "qty":    qty,
        "unit":   "adet",
        "source": f"{file_name} :: {sheet}",
    }


# ─── 4. SHOWROOM Parser ────────────────────────────────────────────────────

def parse_showroom():
    """
    3 sayfa: SERENIDA, LACİVERT ve ALTIN SERİ, EVANİRA
    Her satır: [ÜRÜN İSİM, ML, KALAN]
    "RED CLOVER" geçenler Lacivert; diğer Minerva-108 → Altın.
    """
    fp = FILES["showroom"]
    wb = openpyxl.load_workbook(fp, data_only=True, read_only=True)
    rows = []
    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        for row in ws.iter_rows(min_row=3, values_only=True):
            if not row or not row[0]:
                continue
            name_raw = str(row[0]).strip()
            if not name_raw or "ÜRÜN İSİM" in name_raw.upper() or "ÜRÜN İSMİ" in name_raw.upper():
                continue
            ml = str(row[1] or "").strip() if len(row) > 1 else ""
            try:
                qty = float(row[2]) if len(row) > 2 and row[2] not in (None, "") else 0.0
            except (ValueError, TypeError):
                qty = parse_int_loose(row[2] if len(row) > 2 else None)
            full_name = name_raw + (f" {ml}ml" if ml else "")
            # Aile etiketi
            if "RED CLOVER" in name_raw.upper():
                family = "Lacivert (Red Clover)"
            elif "MİNERVA-108" in name_raw.upper() or "MINERVA-108" in name_raw.upper():
                family = "Altın Seri"
            elif "EVANİRA" in name_raw.upper() or "EVANIRA" in name_raw.upper():
                family = "Evanira"
            elif "SERENIDA" in name_raw.upper() or "SERENİDA" in name_raw.upper():
                family = "Serenida"
            else:
                family = sheet_name.strip()
            rows.append({
                "name":   full_name.strip(),
                "norm":   norm(full_name),
                "qty":    qty,
                "unit":   "adet",
                "family": family,
                "source": f"{fp.name} :: {sheet_name.strip()}",
            })
    wb.close()
    return _merge_by_norm(rows)


# ─── Ortak yardımcı: aynı norm-isim → topla ──────────────────────────────

def _merge_by_norm(rows):
    by_norm = {}
    for r in rows:
        key = r["norm"]
        if not key:
            continue
        if key in by_norm:
            by_norm[key]["qty"] += r["qty"]
            # Source notunu birleştir
            if r["source"] not in by_norm[key]["source"]:
                by_norm[key]["source"] += f" + {r['source']}"
        else:
            by_norm[key] = dict(r)
    return list(by_norm.values())


# ─── DB Eşleştirme + Plan Üretimi ─────────────────────────────────────────

def build_plan(db):
    """Tüm parserları çalıştır, mevcut DB ile eşleştir, plan üret."""
    existing = {norm(i.name): i for i in db.query(Item).filter(Item.is_active == True).all()}
    existing_suppliers = {norm(s.name): s for s in db.query(Supplier).filter(Supplier.is_active == True).all()}

    def classify(rows, category, *, set_pkg_type=False):
        creates = []
        adjusts = []
        for r in rows:
            existing_item = existing.get(r["norm"])
            if existing_item:
                old_qty = float(existing_item.current_stock or 0)
                new_qty = float(r["qty"])
                if abs(new_qty - old_qty) > 1e-9:
                    adjusts.append({
                        **r,
                        "item_id":  existing_item.id,
                        "old_qty":  old_qty,
                        "new_qty":  new_qty,
                        "delta":    round(new_qty - old_qty, 6),
                        "category": category,
                    })
                # Stok aynıysa sessizce atla
            else:
                rec = {**r, "category": category}
                if set_pkg_type and "pkg_type" not in rec:
                    rec["pkg_type"] = detect_pkg_type(r["name"])
                creates.append(rec)
        return creates, adjusts

    plan = {
        "hammadde": {"creates": [], "adjusts": []},
        "ambalaj":  {"creates": [], "adjusts": []},
        "etiket":   {"creates": [], "adjusts": []},
        "bitmis":   {"creates": [], "adjusts": []},
        "suppliers_to_create": [],
    }

    # 1. Hammadde
    rows = parse_hammadde()
    plan["hammadde"]["creates"], plan["hammadde"]["adjusts"] = classify(rows, "Hammadde")
    # Marka → Supplier oluşturma planı
    for r in rows:
        b = r.get("brand", "")
        if b and norm(b) not in existing_suppliers:
            if b not in plan["suppliers_to_create"]:
                plan["suppliers_to_create"].append(b)

    # 2. Ambalaj
    rows = parse_ambalaj()
    plan["ambalaj"]["creates"], plan["ambalaj"]["adjusts"] = classify(rows, "Ambalaj", set_pkg_type=True)

    # 3. Etiket (4 dosya birleşik)
    et_rows = (
        parse_etiket_serenida()
        + parse_etiket_evanira()
        + parse_etiket_redclover()
        + parse_etiket_altin()
    )
    et_rows = _merge_by_norm(et_rows)
    # Etiketler de Ambalaj kategorisinde — sadece pkg_type=etiket ile ayrılır
    et_creates, et_adjusts = classify(et_rows, "Ambalaj")
    for r in et_creates:
        r["pkg_type"] = "etiket"
    plan["etiket"]["creates"], plan["etiket"]["adjusts"] = et_creates, et_adjusts

    # 4. Bitmiş Ürün (showroom)
    rows = parse_showroom()
    plan["bitmis"]["creates"], plan["bitmis"]["adjusts"] = classify(rows, "Bitmiş Ürün")

    return plan


# ─── Dry-Run Yazıcı ───────────────────────────────────────────────────────

def print_plan(plan):
    def section(title, data):
        c, a = data["creates"], data["adjusts"]
        print(f"\n{'─'*72}")
        print(f"  {title}")
        print(f"{'─'*72}")
        print(f"   Yeni eklenecek: {len(c)}")
        for r in c[:6]:
            extra = f" [pkg_type={r.get('pkg_type')}]" if r.get("pkg_type") else ""
            print(f"     + {r['name']!r:55s}  {r['qty']:>8.2f} {r['unit']}{extra}")
        if len(c) > 6:
            print(f"     … +{len(c)-6} satır daha")
        print(f"   Stok düzeltmesi yapılacak: {len(a)}")
        for r in a[:6]:
            sign = "+" if r["delta"] > 0 else ""
            print(f"     ⚙ {r['name']!r:55s}  {r['old_qty']:>8.2f} → {r['new_qty']:>8.2f}  (Δ {sign}{r['delta']:.2f})")
        if len(a) > 6:
            print(f"     … +{len(a)-6} satır daha")

    print()
    print("="*72)
    print("  TOPLU İÇE AKTARIM PLANI — DRY RUN (DB'ye yazılmadı)")
    print("="*72)
    section("1. HAMMADDE",         plan["hammadde"])
    section("2. AMBALAJ",          plan["ambalaj"])
    section("3. ETİKET",           plan["etiket"])
    section("4. BİTMİŞ ÜRÜN",      plan["bitmis"])
    print()
    print(f"{'─'*72}")
    print(f"  YENİ TEDARİKÇİ: {len(plan['suppliers_to_create'])}")
    for s in plan["suppliers_to_create"][:8]:
        print(f"     + {s}")
    if len(plan["suppliers_to_create"]) > 8:
        print(f"     … +{len(plan['suppliers_to_create'])-8} tedarikçi daha")
    print()

    # Toplam özet
    total_creates = sum(len(plan[k]["creates"]) for k in ("hammadde", "ambalaj", "etiket", "bitmis"))
    total_adjusts = sum(len(plan[k]["adjusts"]) for k in ("hammadde", "ambalaj", "etiket", "bitmis"))
    print("="*72)
    print(f"   ÖZET:  +{total_creates} yeni item   ⚙ {total_adjusts} stok düzeltmesi   "
          f"+{len(plan['suppliers_to_create'])} tedarikçi")
    print("="*72)
    print()


# ─── Commit ───────────────────────────────────────────────────────────────

def apply_plan(db, plan):
    """Planı transactionally uygula."""
    suppliers_created = 0
    items_created     = 0
    adjustments_logged = 0

    # 1. Önce tedarikçiler — hammadde createleri sırasında supplier_id atayabilelim
    supplier_lookup = {norm(s.name): s for s in db.query(Supplier).filter(Supplier.is_active == True).all()}
    for s_name in plan["suppliers_to_create"]:
        if norm(s_name) not in supplier_lookup:
            sup = Supplier(name=s_name, is_active=True)
            db.add(sup)
            db.flush()
            supplier_lookup[norm(s_name)] = sup
            suppliers_created += 1

    def add_input_tx(item, qty, src):
        """Yeni eklenen item için stok girişi audit kaydı."""
        if qty > 0:
            db.add(Transaction(
                item_id=item.id,
                transaction_type="Input",
                quantity=qty,
                notes=f"Bulk Import — {src}",
                performed_by=ACTOR,
            ))

    def add_adjust_tx(item_id, old_q, new_q, src, item_unit):
        """Mevcut item için Adjustment audit kaydı."""
        delta = round(new_q - old_q, 6)
        sign = "+" if delta > 0 else ""
        db.add(Transaction(
            item_id=item_id,
            transaction_type="Adjustment",
            quantity=delta,
            notes=(
                f"Bulk Import — Eski: {old_q} {item_unit or ''} → "
                f"Yeni: {new_q} {item_unit or ''} (Δ {sign}{delta}) | "
                f"Sebep: {src}"
            ),
            performed_by=ACTOR,
        ))

    # 2. Yeni item'lar (her kategori için)
    for key in ("hammadde", "ambalaj", "etiket", "bitmis"):
        for r in plan[key]["creates"]:
            sup_id = None
            if r.get("brand"):
                s = supplier_lookup.get(norm(r["brand"]))
                if s:
                    sup_id = s.id
            item = Item(
                name=r["name"],
                category=r["category"],
                unit=r["unit"],
                current_stock=round(float(r["qty"]), 6),
                pkg_type=r.get("pkg_type"),
                supplier_id=sup_id,
                is_active=True,
            )
            db.add(item)
            db.flush()
            add_input_tx(item, float(r["qty"]), r["source"])
            items_created += 1

    # 3. Stok düzeltmeleri
    for key in ("hammadde", "ambalaj", "etiket", "bitmis"):
        for r in plan[key]["adjusts"]:
            item = db.query(Item).filter(Item.id == r["item_id"]).first()
            if not item:
                continue
            item.current_stock = round(float(r["new_qty"]), 6)
            add_adjust_tx(item.id, r["old_qty"], r["new_qty"], r["source"], item.unit)
            adjustments_logged += 1

    db.commit()
    return {
        "suppliers_created": suppliers_created,
        "items_created":     items_created,
        "adjustments_logged": adjustments_logged,
    }


# ─── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Toplu envanter aktarımı")
    parser.add_argument("--commit", action="store_true",
                        help="DB'ye gerçekten yaz (default: dry-run)")
    parser.add_argument("--json", action="store_true",
                        help="Plan'ı JSON olarak da yaz (debug)")
    args = parser.parse_args()

    # Dosya kontrolleri
    missing = [k for k, p in FILES.items() if not p.exists()]
    if missing:
        print(f"⚠  Eksik dosyalar: {missing}", file=sys.stderr)
        sys.exit(1)

    db = SessionLocal()
    try:
        plan = build_plan(db)

        if args.json:
            # JSON-serializable hale getir
            print(json.dumps(plan, indent=2, ensure_ascii=False, default=str))
            return

        print_plan(plan)

        if args.commit:
            print("⏵ Commit ediliyor…")
            result = apply_plan(db, plan)
            print(f"\n  ✓ {result['items_created']} yeni item")
            print(f"  ✓ {result['adjustments_logged']} stok düzeltmesi")
            print(f"  ✓ {result['suppliers_created']} yeni tedarikçi")
            print()
        else:
            print("ℹ  DRY-RUN modu. Gerçekten yazmak için: --commit ekle.\n")

    finally:
        db.close()


if __name__ == "__main__":
    main()
