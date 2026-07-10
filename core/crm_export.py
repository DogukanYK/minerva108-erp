# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
CRM içe/dışa aktarma — Excel (xlsx) dışa aktar + Excel/CSV içe aktar.

Dışa aktarma: serialize edilmiş kayıt dict'lerinden openpyxl ile xlsx üretir
(monthly_report/qc_report deseni — openpyxl zaten bağımlılık).
İçe aktarma: başlık satırını esnek eşler (TR/EN eş-anlamlılar), alan dict'leri
döndürür; router upsert/oluşturmayı yapar.  Kayıtlar source='import' damgalanır.
"""
import io
import csv
import re
from typing import List, Dict, Tuple

from openpyxl import Workbook, load_workbook


# ─── Dışa aktarma sütunları (başlık, serialized-alan) ───────────────────────

EXPORT_COLS: Dict[str, List[Tuple[str, str]]] = {
    "companies": [
        ("Firma", "name"), ("Sektör", "sector"), ("Telefon", "phone"),
        ("E-posta", "email"), ("Şehir", "city"), ("Ülke", "country"),
        ("Vergi Dairesi", "tax_office"), ("Vergi No", "tax_no"), ("Adres", "address"),
        ("Sorumlu", "owner_name"), ("Kaynak", "source_label"),
        ("Kişi Sayısı", "contact_count"), ("Açık Fırsat", "open_deal_count"),
        ("Oluşturulma", "created_at"), ("Not", "notes"),
    ],
    "contacts": [
        ("Ad Soyad", "full_name"), ("Unvan", "title"), ("Firma", "company_name"),
        ("Telefon", "phone"), ("Mobil", "mobile"), ("WhatsApp", "whatsapp_number"),
        ("E-posta", "email"), ("Kaynak", "source_label"), ("Sorumlu", "owner_name"),
        ("Oluşturulma", "created_at"), ("Not", "notes"),
    ],
    "deals": [
        ("Başlık", "title"), ("Firma", "company_name"), ("Kişi", "contact_name"),
        ("Aşama", "stage_name"), ("Değer", "value"), ("Para Birimi", "currency"),
        ("Olasılık %", "probability"), ("Durum", "status"), ("Kaynak", "source_label"),
        ("Sorumlu", "owner_name"), ("Beklenen Kapanış", "expected_close_label"),
        ("Oluşturulma", "created_at"),
    ],
}

# ─── İçe aktarma alan eş-anlamlıları (alan → kabul edilen başlıklar) ─────────

def _fold(s: str) -> str:
    s = (s or "").strip().lower()
    tr = {"ç": "c", "ğ": "g", "ı": "i", "i̇": "i", "ö": "o", "ş": "s", "ü": "u"}
    s = "".join(tr.get(ch, ch) for ch in s)
    return re.sub(r"[^a-z0-9]+", "", s)


# Dışarıya açık ad — import upsert + mükerrer kontrolü ad eşleştirmesinde kullanır.
fold = _fold


IMPORT_FIELDS: Dict[str, List[Tuple[str, List[str]]]] = {
    "companies": [
        ("name", ["firma", "firmaadi", "firmaadı", "ad", "unvan", "company", "companyname", "name"]),
        ("sector", ["sektor", "sektör", "sector", "industry"]),
        ("phone", ["telefon", "tel", "phone", "gsm"]),
        ("email", ["eposta", "e-posta", "email", "mail"]),
        ("city", ["sehir", "şehir", "city", "il"]),
        ("country", ["ulke", "ülke", "country"]),
        ("tax_office", ["vergidairesi", "vergidaire", "taxoffice"]),
        ("tax_no", ["vergino", "vkn", "taxno", "taxnumber"]),
        ("address", ["adres", "address"]),
        ("notes", ["not", "notlar", "note", "notes", "aciklama", "açıklama"]),
    ],
    "contacts": [
        ("full_name", ["adsoyad", "ad", "isim", "kisi", "kişi", "name", "fullname", "contact"]),
        ("company", ["firma", "company", "companyname", "firmaadi"]),
        ("title", ["unvan", "title", "pozisyon", "position"]),
        ("phone", ["telefon", "tel", "phone"]),
        ("mobile", ["mobil", "cep", "gsm", "mobile", "cellphone"]),
        ("whatsapp_number", ["whatsapp", "whatsappno", "wa"]),
        ("email", ["eposta", "e-posta", "email", "mail"]),
        ("notes", ["not", "notlar", "note", "notes", "aciklama", "açıklama"]),
    ],
}


# ─── Dışa aktarma ────────────────────────────────────────────────────────────

def export_workbook(entity: str, rows: List[dict]) -> bytes:
    cols = EXPORT_COLS.get(entity)
    if not cols:
        raise ValueError("Geçersiz varlık")
    wb = Workbook()
    ws = wb.active
    ws.title = entity[:31]
    ws.append([h for h, _ in cols])
    for r in rows:
        ws.append(["" if r.get(f) is None else r.get(f) for _, f in cols])
    # başlık kalın + sütun genişliği
    for ci, (h, _) in enumerate(cols, start=1):
        ws.cell(row=1, column=ci).font = ws.cell(row=1, column=ci).font.copy(bold=True)
        ws.column_dimensions[ws.cell(row=1, column=ci).column_letter].width = max(12, min(40, len(h) + 6))
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


def template_workbook(entity: str) -> bytes:
    """İçe aktarma için boş şablon — yalnızca başlıklar (kullanıcı doldurur)."""
    fields = IMPORT_FIELDS.get(entity)
    if not fields:
        raise ValueError("Geçersiz varlık")
    wb = Workbook()
    ws = wb.active
    ws.title = entity[:31]
    # ilk eş-anlamlıyı başlık olarak kullan (en okunaklı TR)
    headers = {"name": "Firma", "sector": "Sektör", "phone": "Telefon", "email": "E-posta",
               "city": "Şehir", "country": "Ülke", "tax_office": "Vergi Dairesi",
               "tax_no": "Vergi No", "address": "Adres", "notes": "Not",
               "full_name": "Ad Soyad", "company": "Firma", "title": "Unvan",
               "mobile": "Mobil", "whatsapp_number": "WhatsApp"}
    ws.append([headers.get(f, f) for f, _ in fields])
    for ci in range(1, len(fields) + 1):
        ws.cell(row=1, column=ci).font = ws.cell(row=1, column=ci).font.copy(bold=True)
        ws.column_dimensions[ws.cell(row=1, column=ci).column_letter].width = 18
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


# ─── İçe aktarma (parse) ─────────────────────────────────────────────────────

def _header_map(headers: List[str], entity: str) -> Dict[int, str]:
    """Sütun indeksi → alan adı eşlemesi (esnek başlık eşleştirme)."""
    syn = {}
    for field, names in IMPORT_FIELDS[entity]:
        for n in names:
            syn[_fold(n)] = field
    out = {}
    for i, h in enumerate(headers):
        f = syn.get(_fold(h))
        if f and f not in out.values():
            out[i] = f
    return out


def parse_import(file_bytes: bytes, filename: str, entity: str) -> List[dict]:
    """Dosyayı (xlsx/csv) parse edip alan dict'leri döndür.  Boş satırları atlar."""
    if entity not in IMPORT_FIELDS:
        raise ValueError("Geçersiz varlık")
    name = (filename or "").lower()
    rows: List[List[str]] = []
    if name.endswith(".csv"):
        text = file_bytes.decode("utf-8-sig", errors="replace")
        # ; veya , ayraç otomatik
        sample = text[:2000]
        delim = ";" if sample.count(";") > sample.count(",") else ","
        for r in csv.reader(io.StringIO(text), delimiter=delim):
            rows.append(r)
    else:
        wb = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
        ws = wb.active
        for r in ws.iter_rows(values_only=True):
            rows.append(["" if c is None else str(c) for c in r])
    if not rows:
        return []
    headers = [str(c or "") for c in rows[0]]
    cmap = _header_map(headers, entity)
    if not cmap:
        raise ValueError("Başlık satırı tanınamadı — şablonu kullanın.")
    records = []
    for r in rows[1:]:
        rec = {}
        for idx, field in cmap.items():
            val = (str(r[idx]).strip() if idx < len(r) and r[idx] is not None else "")
            if val:
                rec[field] = val
        # zorunlu ad alanı dolu mu?
        key = "name" if entity == "companies" else "full_name"
        if rec.get(key):
            records.append(rec)
    return records
