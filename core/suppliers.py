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
  • Malzeme bazlı tercih (`MaterialSupplierPref`, P1b): `serialize_pref`,
    `effective_prefs` (kart kapsamı grup kapsamını ezer), kart birleştirme
    (`move_item_prefs`) ve grup dağılması (`prefs_to_items`) kancaları.
  • Tedarikçi birleştirme (`merge_preview` / `merge_suppliers`) — mükerrer
    firma kartları (TATLİDİLİMLER / TATLIDİLİMLER …): bütün bağlar
    kazanana taşınır, kaybeden PASİF kalır (iz), hiçbir kayıt silinmez
    (çakışan tercih hariç — kazananınki esas).  Kaybedenin adı kazananın
    takma adıdır (`merged_into_id`, `live_card_id`).
Uçlar: routers/inventory.py (liste/ekle/düzenle/pasife al) ve
routers/suppliers.py (durum + fiyatlar + tercihler + birleştirme).
"""
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Tuple

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


# ─── Malzeme bazlı tercih (material_supplier_prefs) ─────────────────────────

PREFERENCES = ("preferred", "avoid")
PREFERENCE_LABELS = {"preferred": "Tercih edilen", "avoid": "Bu malzemede alma"}
RANK_MAX = 99
PREF_NOTE_MAX = 1000


def serialize_pref(p, suppliers: Optional[dict] = None, items: Optional[dict] = None,
                   groups: Optional[dict] = None) -> dict:
    """`/api/material-prefs` satırı.  `suppliers` / `items` / `groups`: id →
    nesne sözlükleri (yoksa ad boş döner)."""
    sup = (suppliers or {}).get(p.supplier_id)
    st = (normalize_status(sup.purchase_status) or "normal") if sup is not None else "normal"
    it = (items or {}).get(p.item_id) if p.item_id else None
    grp = (groups or {}).get(p.material_group_id) if p.material_group_id else None
    return {
        "id": p.id,
        "scope": "group" if p.material_group_id else "item",
        "material_group_id": p.material_group_id,
        "group_name": grp.name if grp is not None else None,
        "item_id": p.item_id,
        "item_name": it.name if it is not None else None,
        "supplier_id": p.supplier_id,
        "supplier_name": sup.name if sup is not None else None,
        "supplier_active": bool(sup.is_active) if sup is not None and sup.is_active is not None else True,
        "supplier_status": st,
        "supplier_status_label": STATUS_LABELS[st],
        "preference": p.preference,
        "preference_label": PREFERENCE_LABELS.get(p.preference, p.preference),
        "rank": int(p.rank or 1),
        "note": p.note,
        "created_by": p.created_by,
        "created_at": _dt_text(p.created_at),
        "updated_at": _dt_text(p.updated_at),
    }


def effective_prefs(rows: Iterable) -> Dict[int, Tuple[str, int]]:
    """Bir kartın geçerli tercihleri → {supplier_id: (preference, rank)}.
    Kart kapsamlı satır aynı tedarikçinin grup satırını EZER (daha özel karar).
    `rows`: MaterialSupplierPref nesneleri ya da (supplier_id, preference,
    rank, is_item_scope) dörtlüleri."""
    group: Dict[int, Tuple[str, int]] = {}
    item: Dict[int, Tuple[str, int]] = {}
    for r in rows or ():
        if isinstance(r, tuple):
            sid, pref, rank, own = r
        else:
            sid, pref, rank, own = r.supplier_id, r.preference, r.rank, r.item_id is not None
        if pref not in PREFERENCES:
            continue
        (item if own else group)[int(sid)] = (pref, int(rank or 1))
    out = dict(group)
    out.update(item)
    return out


def move_item_prefs(db, loser_id: int, survivor_id: int) -> Tuple[int, int]:
    """core/item_merge.merge_items kancası — kaybeden kartın KART kapsamlı
    tercihleri kazanana taşınır; kazananın aynı tedarikçi için kendi tercihi
    varsa o esas, kaybedeninki düşer.  Dönüş (taşınan, düşen).  Commit
    ÇAĞIRANA aittir."""
    from database import MaterialSupplierPref as P
    rows = db.query(P).filter(P.item_id == loser_id).order_by(P.id).all()
    if not rows:
        return 0, 0
    have = {sid for (sid,) in db.query(P.supplier_id).filter(P.item_id == survivor_id).all()}
    moved = dropped = 0
    for p in rows:
        if p.supplier_id in have:
            db.delete(p)
            dropped += 1
        else:
            p.item_id = survivor_id
            p.updated_at = datetime.utcnow()
            have.add(p.supplier_id)
            moved += 1
    db.flush()
    return moved, dropped


def prefs_to_items(db, group_id: int, items: Iterable, heir_group_id: Optional[int] = None) -> int:
    """core/material_groups.dissolve kancası — dağılan grubun tercihleri
    kaybolmasın:
      • `heir_group_id` (kartlar "taşı" ile yeni gruba geçtiği için dağılan
        grup — routers/material_groups create_group / add_member) → aynı
        tercihler o gruba grup kapsamlı yazılır; yeni grubun aynı tedarikçi
        için kendi tercihi varsa o esas;
      • grupta kalan AKTİF kart(lar)a kart kapsamlı tercih olarak (kartın aynı
        tedarikçi için kendi tercihi varsa o esas).
    Grubun kendi satırları SİLİNMEZ — grup pasif kalır (iz), okuyucular yalnız
    aktif grubu okur.  Dönüş: yazılan satır sayısı.  Commit ÇAĞIRANA aittir."""
    from database import MaterialSupplierPref as P
    rows = db.query(P).filter(P.material_group_id == group_id, P.item_id.is_(None)).order_by(P.id).all()
    if not rows:
        return 0
    n = 0
    if heir_group_id and heir_group_id != group_id:
        heir_has = {sid for (sid,) in db.query(P.supplier_id)
                    .filter(P.material_group_id == heir_group_id, P.item_id.is_(None)).all()}
        for p in rows:
            if p.supplier_id in heir_has:
                continue
            db.add(P(domain=p.domain, material_group_id=heir_group_id, supplier_id=p.supplier_id,
                     preference=p.preference, rank=p.rank, note=p.note,
                     created_by=p.created_by, created_at=p.created_at))
            heir_has.add(p.supplier_id)
            n += 1
    live = [it for it in items if it is not None and it.is_active]
    have: Dict[int, set] = {}
    if live:
        for iid, sid in (db.query(P.item_id, P.supplier_id)
                         .filter(P.item_id.in_([it.id for it in live])).all()):
            have.setdefault(iid, set()).add(sid)
    for p in rows:
        for it in live:
            if p.supplier_id in have.get(it.id, set()):
                continue
            db.add(P(domain=p.domain, item_id=it.id, supplier_id=p.supplier_id,
                     preference=p.preference, rank=p.rank, note=p.note,
                     created_by=p.created_by, created_at=p.created_at))
            have.setdefault(it.id, set()).add(p.supplier_id)
            n += 1
    db.flush()
    return n


# ─── Tedarikçi birleştirme ──────────────────────────────────────────────────

CONTACT_FIELDS = ("contact_person", "phone", "email", "address", "notes")
CONTACT_FIELD_LABELS = {"contact_person": "yetkili", "phone": "telefon", "email": "e-posta",
                        "address": "adres", "notes": "not"}


class SupplierMergeError(Exception):
    """Kullanıcıya gösterilebilir birleştirme engeli (status: HTTP kodu)."""

    def __init__(self, detail: str, status: int = 400, code: Optional[str] = None):
        super().__init__(detail)
        self.detail, self.status, self.code = detail, status, code


def _pref_target(p) -> tuple:
    return ("g", p.material_group_id) if p.material_group_id else ("i", p.item_id)


def live_card_id(cards: dict, sid) -> Optional[int]:
    """Kartın YAŞAYAN karşılığı: aktifse kendisi; birleştirilmişse
    (`merged_into_id`) zincirin sonundaki aktif kazanan; yoksa None (firma
    gerçekten pasif).  `cards`: {id: (is_active, merged_into_id)}.  Döngüye
    karşı korumalı.  Ortak kural: `supplier_prices.import_prices` (eski
    yazım → kazanan) ve `purchase_pricing.FirmActivity` (pasif firma)."""
    seen = set()
    while sid in cards and sid not in seen:
        seen.add(sid)
        active, into = cards[sid]
        if active:
            return sid
        sid = into
    return None


def _merge_pair(db, loser_id: int, survivor_id: int, domain: str, *, lock: bool = False):
    """Birleştirme ön koşulları: farklı, ikisi de bu panelde ve AKTİF.  Kilit
    id sırasıyla (iki eşzamanlı ters birleştirme kilitlenmesin)."""
    from database import Supplier
    if loser_id == survivor_id:
        raise SupplierMergeError("Tedarikçi kendisiyle birleştirilemez.", 400, "same")
    q = db.query(Supplier).filter(Supplier.id.in_((loser_id, survivor_id)), Supplier.domain == domain)
    if lock:
        q = q.order_by(Supplier.id).with_for_update()
    rows = {s.id: s for s in q.all()}
    loser, survivor = rows.get(loser_id), rows.get(survivor_id)
    if loser is None or survivor is None:
        raise SupplierMergeError("Tedarikçi bulunamadı.", 404, "not_found")
    for s in (loser, survivor):
        if s.is_active is False:
            raise SupplierMergeError(f"«{s.name}» pasif — önce etkinleştirin.", 409, "inactive")
    return loser, survivor


def _merge_counts(db, loser, survivor) -> dict:
    from database import (Inventory, Item, MaterialSupplierPref as P, ProductionConsumption,
                          StockOrderFlag, SupplierPrice)
    lid, sid = loser.id, survivor.id
    sp_survivor = {(i, (u or "")) for (i, u) in db.query(SupplierPrice.item_id, SupplierPrice.price_unit)
                   .filter(SupplierPrice.supplier_id == sid).all()}
    sp_loser = db.query(SupplierPrice.item_id, SupplierPrice.price_unit).filter(
        SupplierPrice.supplier_id == lid).all()
    pref_survivor = {_pref_target(p) for p in db.query(P).filter(P.supplier_id == sid).all()}
    pref_loser = db.query(P).filter(P.supplier_id == lid).all()
    return {
        "items": db.query(Item.id).filter(Item.supplier_id == lid).count(),
        "items_active": db.query(Item.id).filter(Item.supplier_id == lid,
                                                 Item.is_active == True).count(),   # noqa: E712
        "lots": db.query(Inventory.id).filter(Inventory.supplier_id == lid).count(),
        "prices": len(sp_loser),
        "price_overlaps": sum(1 for (i, u) in sp_loser if (i, u or "") in sp_survivor),
        "order_flags": db.query(StockOrderFlag.id).filter(StockOrderFlag.supplier_id == lid).count(),
        "consumptions": db.query(ProductionConsumption.id).filter(
            ProductionConsumption.supplier_id == lid).count(),
        "prefs": len(pref_loser),
        "prefs_dropped": sum(1 for p in pref_loser if _pref_target(p) in pref_survivor),
    }


def _copy_fields(loser, survivor) -> List[str]:
    return [f for f in CONTACT_FIELDS
            if not (getattr(survivor, f) or "").strip() and (getattr(loser, f) or "").strip()]


def _status_warning(loser, survivor) -> Optional[str]:
    ls = normalize_status(loser.purchase_status) or "normal"
    ss = normalize_status(survivor.purchase_status) or "normal"
    if ls == ss:
        return None
    return (f"«{loser.name}» «{STATUS_LABELS[ls]}», «{survivor.name}» «{STATUS_LABELS[ss]}» işaretli — "
            f"birleşince «{survivor.name}» kendi durumunu ({STATUS_LABELS[ss]}) korur; gerekirse "
            f"durumu sonra değiştirin.")


def merge_preview(db, loser_id: int, survivor_id: int, domain: str) -> dict:
    """GET …/merge-preview — taşınacak kayıt sayıları + kopyalanacak boş
    iletişim alanları + durum uyarısı.  Veri DEĞİŞMEZ."""
    loser, survivor = _merge_pair(db, loser_id, survivor_id, domain)
    copy = _copy_fields(loser, survivor)
    return {"loser": serialize_supplier(loser), "survivor": serialize_supplier(survivor),
            "counts": _merge_counts(db, loser, survivor),
            "copy_fields": copy, "copy_fields_text": [CONTACT_FIELD_LABELS[f] for f in copy],
            "status_warning": _status_warning(loser, survivor)}


def merge_suppliers(db, loser_id: int, survivor_id: int, domain: str, actor: str) -> dict:
    """Kaybeden tedarikçi kartını kazanana birleştir.  Commit ÇAĞIRANA aittir.

    Taşınanlar (supplier_id): ürün kartları, lotlar, fiyat satırları
    (`supplier_name` metni KALIR — listede firmanın o günkü adı), sipariş
    işaretleri, üretim tüketim dökümü (`supplier_name` kalır), malzeme
    tercihleri (aynı grup/kart için kazananın tercihi varsa kaybedeninki
    düşer).  Numune Analizi bileşen satırlarının ANLIK adları DOKUNULMAZ.
    Kazananın boş iletişim alanları kaybedenden doldurulur.  Kaybeden
    `is_active=False` + not satırı ("… kartına birleştirildi"); kazanan kendi
    satın alma durumunu korur (farklıysa `warnings`).  Kaybedene
    `merged_into_id` = kazanan yazılır: adı kazananın takma adı olur
    (fiyat listesinin eski yazımı kazanana eşlenir, firma "pasif" sayılmaz)."""
    from database import (Inventory, Item, MaterialSupplierPref as P, ProductionConsumption,
                          StockOrderFlag, Supplier, SupplierPrice)
    loser, survivor = _merge_pair(db, loser_id, survivor_id, domain, lock=True)
    counts = _merge_counts(db, loser, survivor)
    lid, sid = loser.id, survivor.id
    for model in (Item, Inventory, SupplierPrice, StockOrderFlag, ProductionConsumption):
        (db.query(model).filter(model.supplier_id == lid)
         .update({model.supplier_id: sid}, synchronize_session=False))
    have = {_pref_target(p) for p in db.query(P).filter(P.supplier_id == sid).all()}
    for p in db.query(P).filter(P.supplier_id == lid).order_by(P.id).all():
        if _pref_target(p) in have:
            db.delete(p)
        else:
            p.supplier_id = sid
            p.updated_at = datetime.utcnow()
            have.add(_pref_target(p))
    copied = {}
    for f in _copy_fields(loser, survivor):
        copied[f] = getattr(loser, f)
        setattr(survivor, f, getattr(loser, f))
    warning = _status_warning(loser, survivor)
    stamp = to_tr(datetime.utcnow()).strftime("%d.%m.%Y")
    line = f"«{survivor.name}» (#{survivor.id}) kartına birleştirildi — {stamp}, {actor or '—'}"
    loser.notes = (f"{loser.notes.rstrip()}\n{line}" if (loser.notes or "").strip() else line)
    loser.is_active = False
    # Kaybedenin adı kazananın takma adı (`live_card_id`); daha önce
    # kaybedene birleştirilmiş kartlar da artık doğrudan kazanana bağlanır.
    loser.merged_into_id = sid
    (db.query(Supplier).filter(Supplier.merged_into_id == lid)
     .update({Supplier.merged_into_id: sid}, synchronize_session=False))
    db.flush()
    return {"loser_id": lid, "loser_name": loser.name, "survivor_id": sid,
            "survivor_name": survivor.name, "counts": counts, "copied_fields": sorted(copied),
            "warnings": [warning] if warning else []}
