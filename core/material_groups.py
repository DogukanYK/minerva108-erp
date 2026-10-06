# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
"Aynı malzeme" grupları — farklı tedarikçi kartlarını bağlar, BİRLEŞTİRMEZ.

NEDEN VAR (06.10.2026)
──────────────────────
Lab aynı malzemenin her tedarikçisini AYRI kart tutuyor (24.08 sonrası 18
kopya kümesinde "farklı ürünler, ayrı kalsın" kararı).  Stearil alkol üç
kartta: SETİL STEARİL ALKOL / Tatlıdilimler, CETYL STEARYL ALCOHOL /
Yiğitoğlu, CETEARYL ALCOHOL / Veser.  Sistem bunların aynı malzeme olduğunu
bilmiyordu — numune yanlış tedarikçinin kartına çevrildi (Naturalya jojobası
KRK GIDA kartına), satın alma planı alternatif tedarikçiyi göstermedi.

Grup stok ya da defter BİRLEŞTİRMEZ (o iş core/item_merge'ün); yalnız "bu
kartlar aynı malzeme" bilgisini taşır.  Kart en fazla tek grupta
(`Item.material_group_id`).  Ad AKTİF kayıtlarda panel içinde TR-katlanmış
tekil (`core.supplier_prices.normalize`).

Bu modül şimdilik numune çevirmenin ihtiyacı kadardır (`join`,
`group_payload`); grup CRUD'u ve öneri motoru ayrı pakette genişler.
"""
from typing import Optional

from sqlalchemy.orm import Session

from core.supplier_prices import normalize as _tr_fold
from database import Item, MaterialGroup, Supplier


def find_name_conflict(db: Session, domain: str, name: str,
                       exclude_id: Optional[int] = None) -> Optional[MaterialGroup]:
    """Aynı panelde TR-katlanmış adı eşleşen AKTİF grup (yoksa None)."""
    key = _tr_fold(name)
    if not key:
        return None
    q = db.query(MaterialGroup).filter(MaterialGroup.domain == domain,
                                       MaterialGroup.is_active == True)   # noqa: E712
    if exclude_id:
        q = q.filter(MaterialGroup.id != exclude_id)
    for g in q.all():
        if _tr_fold(g.name) == key:
            return g
    return None


def unique_name(db: Session, domain: str, base: str) -> str:
    """`base` boşsa kullanılır; doluysa "base (2)", "base (3)" … — tohumlama ve
    otomatik açılan gruplar ad çakışmasında durmasın diye."""
    base = (base or "").strip()[:140] or "Aynı malzeme"
    if not find_name_conflict(db, domain, base):
        return base
    n = 2
    while find_name_conflict(db, domain, f"{base} ({n})"):
        n += 1
    return f"{base} ({n})"


def join(db: Session, a: Item, b: Item, *, actor: str) -> Optional[MaterialGroup]:
    """`b` kartını `a`'nın grubuna kat; `a` grupsuzsa ikisi için grup aç.

    • `a`'nın grubu varsa `b` ona eklenir.
    • yoksa `MaterialGroup(name=a.name, source='convert')` açılır, ikisi konur.
    • `b` zaten BAŞKA bir gruptaysa ya da kartlar farklı paneldeyse dokunulmaz
      → None (bir kart tek gruptadır; sessizce taşımak lab'ın kurduğu düzeni
      bozardı).
    Commit ÇAĞIRANA aittir (flush eder).
    """
    if a is None or b is None or a.id == b.id:
        return None
    if (a.domain or "cosmetics") != (b.domain or "cosmetics"):
        return None
    if a.material_group_id:
        grp = db.query(MaterialGroup).filter(MaterialGroup.id == a.material_group_id).first()
        if grp is not None and grp.is_active:
            if b.material_group_id and b.material_group_id != grp.id:
                return None
            b.material_group_id = grp.id
            return grp
    if b.material_group_id:
        return None
    dom = a.domain or "cosmetics"
    grp = MaterialGroup(name=unique_name(db, dom, a.name), domain=dom,
                        source="convert", created_by=(actor or "")[:100] or None)
    db.add(grp)
    db.flush()
    a.material_group_id = grp.id
    b.material_group_id = grp.id
    return grp


def group_payload(db: Session, group_id: Optional[int]) -> Optional[dict]:
    """Grup + üyeleri: ``{id, name, members: [{item_id, name, unit,
    supplier_name, stock, is_active}]}`` — grup yok/pasifse None.

    Pasif kartlar da listelenir (`is_active` ile işaretli) — numunesi pasif
    karta bağlı kalmış lotlar arayüzde gizlenmesin."""
    if not group_id:
        return None
    grp = db.query(MaterialGroup).filter(MaterialGroup.id == group_id).first()
    if grp is None or not grp.is_active:
        return None
    rows = (db.query(Item, Supplier.name)
            .outerjoin(Supplier, Supplier.id == Item.supplier_id)
            .filter(Item.material_group_id == grp.id)
            .order_by(Item.is_active.desc(), Item.name, Item.id).all())
    return {
        "id": grp.id,
        "name": grp.name,
        "members": [{
            "item_id": it.id,
            "name": it.name,
            "unit": it.unit or "",
            "supplier_name": sname,
            "stock": round(float(it.current_stock or 0.0), 4),
            "is_active": bool(it.is_active),
        } for it, sname in rows],
    }
