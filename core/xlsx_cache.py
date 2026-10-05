# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
xlsx formül hücrelerine HESAPLANMIŞ değeri gömer.

openpyxl formülü yazar ama sonucunu yazamaz (`<f>…</f><v />`).  Excel açılışta
yeniden hesaplar; fakat formül hesaplamayan önizlemeler (Mail/Quick Look,
WhatsApp, Drive önizlemesi, `openpyxl data_only=True` ile okuyan betikler)
hücreyi BOŞ ya da 0 gösterir — "Tutar" sütunu boş bir alım listesi demek.
Rusya betiklerindeki (`fiyat/fiyatli_liste.py`, `Proforma/proforma_bicim.py`)
zip yönteminin genel hâli:

    inject_cached_values(xlsx_bytes, {"Alım listesi": {"K5": 7088, "K6": ""}})

  • Sayfa adı → `xl/workbook.xml` (+ `_rels/workbook.xml.rels`) üzerinden
    `xl/worksheets/sheetN.xml` dosyasına çözülür (sayfa sırasına güvenilmez).
  • Formül hücresindeki boş `<v />` / `<v></v>` (ya da hiç olmayan `<v>`)
    `<v>değer</v>` olur.  Metin sonuç `t="str"`, mantıksal `t="b"`; boş metin
    sonucu (`=IF(J5="","",…)`) `t="str"` + `<v></v>` — Excel'in kendi yazdığı
    biçim.
  • Önbellekte olup sayfada formül hücresi olarak BULUNAMAYAN referans
    `ValueError` atar: sessizce eksik kalan önbellek, formülle tutmayan bir
    toplamı gizlerdi (referans betiğin `assert n == 1` kuralı).
"""
import io
import posixpath
import re
import zipfile
from typing import Dict, Optional
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

_NS_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_NS_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_NS_PKG = "http://schemas.openxmlformats.org/package/2006/relationships"

# Kendini kapatan (<c r="A1" s="2"/>) hücreler eşleşmez: `(?<!/)>`.
_CELL_RE = re.compile(r"<c\b([^>]*?)(?<!/)>(.*?)</c>", re.S)
_REF_RE = re.compile(r'\br="([A-Z]+[0-9]+)"')
_T_ATTR_RE = re.compile(r'\s+t="[^"]*"')
_V_RE = re.compile(r"<v\s*/>|<v>[^<]*</v>|<v\s[^>]*/>|<v\s[^>]*>[^<]*</v>")


def _sheet_paths(z: zipfile.ZipFile) -> Dict[str, str]:
    """Sayfa adı → zip içindeki worksheet yolu."""
    wb = ET.fromstring(z.read("xl/workbook.xml"))
    rels = ET.fromstring(z.read("xl/_rels/workbook.xml.rels"))
    target = {}
    for r in rels.findall(f"{{{_NS_PKG}}}Relationship"):
        t = r.get("Target") or ""
        # Hedef mutlak ('/xl/worksheets/sheet1.xml') ya da xl/'ye göre göreli olabilir.
        path = t.lstrip("/") if t.startswith("/") else posixpath.normpath(posixpath.join("xl", t))
        target[r.get("Id")] = path
    out = {}
    sheets = wb.find(f"{{{_NS_MAIN}}}sheets")
    for s in (sheets if sheets is not None else []):
        rid = s.get(f"{{{_NS_REL}}}id")
        if rid in target:
            out[s.get("name")] = target[rid]
    return out


def _v_payload(val):
    """(t niteliği | None, <v> içeriği)."""
    if isinstance(val, bool):
        return "b", "1" if val else "0"
    if val is None or val == "":
        return "str", ""
    if isinstance(val, int):
        return None, str(val)
    if isinstance(val, float):
        if val != val or val in (float("inf"), float("-inf")):
            raise ValueError("xlsx önbelleğine NaN/sonsuz yazılamaz")
        return None, repr(val)
    return "str", escape(str(val))


def _inject_sheet(xml: str, cells: Dict[str, object], sheet: str) -> str:
    todo = {k.upper(): v for k, v in cells.items()}
    done = set()

    def repl(m):
        attrs, inner = m.group(1), m.group(2)
        rm = _REF_RE.search(attrs)
        if not rm or rm.group(1) not in todo or "<f" not in inner:
            return m.group(0)
        ref = rm.group(1)
        t, payload = _v_payload(todo[ref])
        attrs = _T_ATTR_RE.sub("", attrs)
        if t:
            attrs += f' t="{t}"'
        v = f"<v>{payload}</v>"
        inner = _V_RE.sub("", inner).rstrip() + v
        done.add(ref)
        return f"<c{attrs}>{inner}</c>"

    out = _CELL_RE.sub(repl, xml)
    missing = sorted(set(todo) - done)
    if missing:
        raise ValueError(f"'{sheet}' sayfasında formül hücresi bulunamadı: {', '.join(missing[:10])}")
    return out


def inject_cached_values(xlsx_bytes: bytes, cache: Dict[str, Dict[str, object]]) -> bytes:
    """`cache` = {sayfa adı: {hücre: değer}} → değerleri gömülmüş yeni xlsx baytları."""
    cache = {k: v for k, v in (cache or {}).items() if v}
    if not cache:
        return xlsx_bytes
    src = zipfile.ZipFile(io.BytesIO(xlsx_bytes))
    paths = _sheet_paths(src)
    by_path: Dict[str, str] = {}
    for sheet in cache:
        p: Optional[str] = paths.get(sheet)
        if not p:
            raise ValueError(f"xlsx'te '{sheet}' adlı sayfa yok")
        by_path[p] = sheet
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as dst:
        for info in src.infolist():
            data = src.read(info.filename)
            if info.filename in by_path:
                sheet = by_path[info.filename]
                data = _inject_sheet(data.decode("utf-8"), cache[sheet], sheet).encode("utf-8")
            dst.writestr(info, data)
    return buf.getvalue()
