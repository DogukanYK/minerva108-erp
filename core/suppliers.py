# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Tedarikçi satın alma durumu — lab'ın firma bazlı tercihi.

NEDEN VAR (07.10.2026)
──────────────────────
Songül Hanım: "Üretimde önce küçük miktarlı satış yapan tedarikçilerden
elimizdeki hammaddeleri bitirmek, bu tedarikçilerin yerine alışveriş
yapabileceğimiz tedarikçileri eklemek istiyoruz."  Sistemde firmanın tercih /
"bitir" durumu yoktu.  Canlıda mal kabul kaydı neredeyse yok — "küçük satıcı"
veriden ÇIKARILAMAZ; durumu lab işaretler, sistem tahmin ETMEZ.

Durumlar (`Supplier.purchase_status`):
  • normal     — "Normal"
  • preferred  — "Tercih edilen"
  • phase_out  — "Bitirilecek — alma": elimizdekini bitir, yenisini başka
                 firmadan al.  Sebep zorunlu (PUT /api/suppliers/{id}/status).

İçerik:
  • `STATUSES` / `status_label` / `normalize_status`
  • `status_by_key` — aynı firmanın mükerrer kartları (`supplier_key`
    altında) tek duruma indirgenir: biri phase_out ise phase_out, yoksa
    preferred, yoksa normal; farklı durumlar `conflicts` listesine düşer
    (satın alma kontrol listesi — P1b).
  • `serialize_supplier` — liste + durum ucunun ortak yanıt şekli.
Uçlar: routers/inventory.py (liste/ekle/düzenle/pasife al) ve
routers/suppliers.py (durum + fiyatlar).
"""
from typing import Dict, List, Optional, Tuple

from database import to_tr

STATUSES = ("normal", "preferred", "phase_out")
STATUS_LABELS = {
    "normal": "Normal",
    "preferred": "Tercih edilen",
    "phase_out": "Bitirilecek — alma",
}
# status_by_key öncelik sırası — "bitir" kararı tercihten güçlüdür.
_RANK = {"phase_out": 2, "preferred": 1, "normal": 0}
REASON_MAX = 1000


def normalize_status(v) -> Optional[str]:
    """Girdi → STATUSES'tan biri; tanınmıyorsa None.  Boş/None → 'normal'."""
    s = (v or "normal").strip().lower() if isinstance(v, str) or v is None else None
    return s if s in STATUSES else None


def status_label(v) -> str:
    return STATUS_LABELS.get(normalize_status(v) or "normal", STATUS_LABELS["normal"])


def _get(obj, key, default=None):
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def status_by_key(suppliers, index) -> Tuple[Dict[str, str], List[dict]]:
    """Tedarikçi kartlarını `supplier_key` (ya da `SupplierIndex.key`) başına
    tek duruma indirger → (by_key, conflicts).

    `suppliers`: Supplier nesneleri ya da dict'ler (`name` +
    `purchase_status`/`status`).  `index`: `.key(name)` metodu olan nesne
    (SupplierIndex) ya da `name → key` çağrılabilir.  Pasif kartlar
    (`is_active` False) sayılmaz.
    conflicts: [{key, names_by_status: {status: [ad, …]}}] — aynı firmanın
    kartları farklı işaretlenmişse (ör. biri bitirilecek, biri tercih).
    """
    keyf = index.key if hasattr(index, "key") else index
    seen: Dict[str, Dict[str, List[str]]] = {}
    for s in suppliers or ():
        if _get(s, "is_active", True) is False:
            continue
        name = _get(s, "name") or ""
        k = keyf(name)
        if not k:
            continue
        st = normalize_status(_get(s, "purchase_status", None) or _get(s, "status", None)) or "normal"
        seen.setdefault(k, {}).setdefault(st, []).append(name)
    by_key: Dict[str, str] = {}
    conflicts: List[dict] = []
    for k, per in seen.items():
        by_key[k] = max(per, key=lambda st: _RANK[st])
        if len(per) > 1:
            conflicts.append({"key": k, "names_by_status": {st: sorted(v) for st, v in per.items()}})
    conflicts.sort(key=lambda c: c["key"])
    return by_key, conflicts


def _dt_text(dt) -> str:
    return to_tr(dt).strftime("%d.%m.%Y %H:%M") if dt else ""


def serialize_supplier(s) -> dict:
    """GET /api/suppliers satırı ve PUT .../status yanıtı (aynı şekil)."""
    st = normalize_status(s.purchase_status) or "normal"
    return {
        "id": s.id,
        "name": s.name,
        "contact_person": s.contact_person,
        "email": s.email,
        "phone": s.phone,
        "address": s.address,
        "notes": s.notes,
        "is_active": bool(s.is_active) if s.is_active is not None else True,
        "purchase_status": st,
        "status_label": STATUS_LABELS[st],
        "status_reason": s.status_reason,
        "status_by": s.status_by,
        "status_at": _dt_text(s.status_at),
        "created_at": to_tr(s.created_at).strftime("%d.%m.%Y") if s.created_at else "",
    }
