# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
"Aynı malzeme" grupları router'ı — /api/material-groups.

Grup, farklı tedarikçi kartlarını BİRLEŞTİRMEDEN bağlar (stearil alkol:
Tatlıdilimler / Yiğitoğlu / Veser kartları).  Satın alma planı alternatif
tedarikçiyi, numune çevirme hedef kartı bu bağdan bulur.  İş kuralları ve
öneri motoru core/material_groups.py'de.

KURALLAR
  • Kart en fazla TEK grupta — başka gruptaki kart `move: true` olmadan
    eklenmez (409 `in_other_group`).
  • Aktif üyesi 2'nin altına düşen grup kendiliğinden dağılır.
  • Ad AKTİF gruplar arasında panel içinde TR-katlanmış tekil (409).
  • Bitmiş Ürün gruplanmaz; hammadde ile ambalaj aynı grupta olmaz.
  • Birim ailesi farklıysa (126 "g" ↔ 599 "adet") engellenmez, `warning` döner.
  • Okuma items.view, yazma items.edit; her sorgu aktif panelle sınırlı.
"""
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import material_groups as mg
from core.audit import log_admin_event
from core.domain import active_domain
from core.permissions import require_permission
from core.stock_lots import lot_kind
from database import Item, MaterialGroup, get_db

router = APIRouter(prefix="/api/material-groups", tags=["material-groups"])


# ─── Gövdeler ────────────────────────────────────────────────────────────────

class GroupCreateBody(BaseModel):
    name: str = Field(..., min_length=1, max_length=150)
    item_ids: List[int] = Field(..., min_length=2, max_length=100)
    note: Optional[str] = Field(None, max_length=2000)
    # Kartlardan biri başka gruptaysa oradan alınsın mı (yoksa 409)
    move: bool = False


class GroupUpdateBody(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=150)
    note: Optional[str] = Field(None, max_length=2000)


class MemberBody(BaseModel):
    item_id: int
    move: bool = False


class DismissBody(BaseModel):
    item_ids: List[int] = Field(..., min_length=2, max_length=100)


# ─── Yardımcılar ─────────────────────────────────────────────────────────────

def _actor(user: dict) -> str:
    return ((user or {}).get("full_name") or (user or {}).get("username") or "sistem")[:100]


def _err(status: int, detail: str, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"detail": detail, **extra})


def _group(db: Session, group_id: int, domain: str, *, lock: bool = False):
    q = db.query(MaterialGroup).filter(MaterialGroup.id == group_id,
                                       MaterialGroup.domain == domain,
                                       MaterialGroup.is_active == True)   # noqa: E712
    if lock:
        q = q.with_for_update()
    return q.first()


def _card_problem(it: Optional[Item], domain: str, item_id: int) -> Optional[JSONResponse]:
    if it is None or (it.domain or "cosmetics") != domain:
        return _err(404, f"Kart bulunamadı (id {item_id}).")
    if not it.is_active:
        return _err(400, f"«{it.name}» kartı pasif — gruba eklenemez.")
    if lot_kind(it.category) == "finished":
        return _err(400, f"«{it.name}» bitmiş ürün — aynı malzeme grubu hammadde ve "
                         f"ambalaj kartları içindir.")
    return None


def _kind_problem(cards: List[Item]) -> Optional[JSONResponse]:
    if len({lot_kind(c.category) for c in cards}) > 1:
        return _err(400, "Hammadde ve ambalaj kartları aynı grupta olamaz.")
    return None


def _other_group(db: Session, it: Item, group_id: Optional[int]) -> Optional[MaterialGroup]:
    """Kartın (bu grup dışındaki) AKTİF grubu."""
    if not it.material_group_id or it.material_group_id == group_id:
        return None
    return (db.query(MaterialGroup)
            .filter(MaterialGroup.id == it.material_group_id,
                    MaterialGroup.is_active == True).first())             # noqa: E712


def _in_other_group(it: Item, other: MaterialGroup) -> JSONResponse:
    return _err(409, f"«{it.name}» zaten «{other.name}» grubunda — bir kart tek grupta olur. "
                     f"Taşımak için onaylayın.",
                code="in_other_group", item_id=it.id,
                group={"id": other.id, "name": other.name})


def _name_conflict(existing: MaterialGroup) -> JSONResponse:
    return _err(409, f"Bu adla bir grup zaten var: «{existing.name}».",
                code="name_conflict", existing={"id": existing.id, "name": existing.name})


def _with_meta(payload: Optional[dict], **extra) -> dict:
    out = dict(payload or {})
    out.update(extra)
    if payload:
        out["warning"] = mg.unit_warning(m["unit"] for m in payload["members"] if m["is_active"])
    else:
        out.setdefault("warning", None)
    return out


# ─── Okuma ───────────────────────────────────────────────────────────────────

@router.get("")
def list_groups(
    q: Optional[str] = Query(None, max_length=100),
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("items", "view")),
    domain: str = Depends(active_domain),
):
    """Paneldeki aktif gruplar + üyeleri (`?q=` grup/kart/tedarikçi araması)."""
    groups = mg.groups_payload(db, domain, q)
    return {"groups": groups, "count": len(groups)}


@router.get("/suggestions")
def list_suggestions(
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("items", "view")),
    domain: str = Depends(active_domain),
):
    """"Aynı malzeme olabilir" önerileri (core.material_groups.suggest)."""
    out = mg.load_suggestions(db, domain)
    return {"suggestions": out, "count": len(out)}


@router.post("/suggestions/dismiss")
def dismiss_suggestion(
    data: DismissBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("items", "edit")),
    domain: str = Depends(active_domain),
):
    """Lab "bunlar farklı malzeme" dedi — kartların bütün çiftleri bir daha
    önerilmez (AppSetting `material_groups.dismissed.<domain>`)."""
    ids = sorted(set(data.item_ids))
    if len(ids) < 2:
        return _err(400, "En az iki farklı kart gerekli.")
    found = {i for (i,) in db.query(Item.id).filter(Item.id.in_(ids), Item.domain == domain).all()}
    missing = [i for i in ids if i not in found]
    if missing:
        return _err(404, f"Kart bulunamadı (id {missing[0]}).")
    added = mg.add_dismissed(db, domain, ids)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="material_group.dismiss",
                    target_type="item", target_id=ids[0],
                    details={"item_ids": ids, "yeni_cift": added})
    return {"message": "Öneri kapatıldı — bu kartlar bir daha birlikte önerilmeyecek.",
            "item_ids": ids, "added_pairs": added}


@router.get("/{group_id}")
def get_group(
    group_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("items", "view")),
    domain: str = Depends(active_domain),
):
    grp = _group(db, group_id, domain)
    if grp is None:
        return _err(404, "Grup bulunamadı.")
    return _with_meta(mg.group_payload(db, grp.id))


# ─── Yazma ───────────────────────────────────────────────────────────────────

@router.post("", status_code=201)
def create_group(
    data: GroupCreateBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("items", "edit")),
    domain: str = Depends(active_domain),
):
    name = data.name.strip()
    if not name:
        return _err(400, "Grup adı boş olamaz.")
    ids = list(dict.fromkeys(data.item_ids))
    if len(ids) < 2:
        return _err(400, "Grup için en az iki farklı kart seçin.")
    clash = mg.find_name_conflict(db, domain, name)
    if clash:
        return _name_conflict(clash)
    cards = {it.id: it for it in db.query(Item).filter(Item.id.in_(ids)).with_for_update().all()}
    for i in ids:
        bad = _card_problem(cards.get(i), domain, i)
        if bad:
            return bad
    ordered = [cards[i] for i in ids]
    bad = _kind_problem(ordered)
    if bad:
        return bad
    olds = {}
    for it in ordered:
        other = _other_group(db, it, None)
        if other is not None:
            if not data.move:
                return _in_other_group(it, other)
            olds[other.id] = other.name

    actor = _actor(current_user)
    grp = MaterialGroup(name=name, note=(data.note or "").strip() or None, domain=domain,
                        source="manual", created_by=actor)
    db.add(grp)
    db.flush()
    for it in ordered:
        it.material_group_id = grp.id
    dissolved = [gid for gid in olds if mg.prune(db, gid)]
    db.commit()
    log_admin_event(db, request, actor=current_user, action="material_group.create",
                    target_type="material_group", target_id=grp.id, target_name=grp.name,
                    details={"item_ids": ids, "tasinan_gruplar": sorted(olds),
                             "dagilan_gruplar": dissolved})
    return _with_meta(mg.group_payload(db, grp.id),
                      message=f"«{grp.name}» grubu {len(ids)} kartla kuruldu.",
                      dissolved_groups=dissolved)


@router.put("/{group_id}")
def update_group(
    group_id: int,
    data: GroupUpdateBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("items", "edit")),
    domain: str = Depends(active_domain),
):
    grp = _group(db, group_id, domain, lock=True)
    if grp is None:
        return _err(404, "Grup bulunamadı.")
    old_name = grp.name
    changes = {}
    if data.name is not None:
        name = data.name.strip()
        if not name:
            return _err(400, "Grup adı boş olamaz.")
        if name != grp.name:
            clash = mg.find_name_conflict(db, domain, name, exclude_id=grp.id)
            if clash:
                return _name_conflict(clash)
            changes["ad"] = [grp.name, name]
            grp.name = name
    if data.note is not None:
        note = data.note.strip() or None
        if note != grp.note:
            changes["not"] = True
            grp.note = note
    db.commit()
    if changes:
        log_admin_event(db, request, actor=current_user, action="material_group.rename",
                        target_type="material_group", target_id=grp.id, target_name=old_name,
                        details=changes)
    return _with_meta(mg.group_payload(db, grp.id), message="Grup güncellendi.")


@router.post("/{group_id}/members")
def add_member(
    group_id: int,
    data: MemberBody,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("items", "edit")),
    domain: str = Depends(active_domain),
):
    grp = _group(db, group_id, domain, lock=True)
    if grp is None:
        return _err(404, "Grup bulunamadı.")
    it = db.query(Item).filter(Item.id == data.item_id).with_for_update().first()
    bad = _card_problem(it, domain, data.item_id)
    if bad:
        return bad
    if it.material_group_id == grp.id:
        return _with_meta(mg.group_payload(db, grp.id),
                          message=f"«{it.name}» zaten bu grupta.", dissolved_groups=[])
    members = (db.query(Item).filter(Item.material_group_id == grp.id,
                                     Item.is_active == True).all())    # noqa: E712
    bad = _kind_problem(members + [it])
    if bad:
        return bad
    other = _other_group(db, it, grp.id)
    if other is not None and not data.move:
        return _in_other_group(it, other)

    it.material_group_id = grp.id
    dissolved = [other.id] if other is not None and mg.prune(db, other.id) else []
    db.commit()
    log_admin_event(db, request, actor=current_user, action="material_group.add",
                    target_type="material_group", target_id=grp.id, target_name=grp.name,
                    details={"item_id": it.id, "item": it.name,
                             "onceki_grup": other.id if other is not None else None,
                             "dagilan_gruplar": dissolved})
    return _with_meta(mg.group_payload(db, grp.id),
                      message=f"«{it.name}» «{grp.name}» grubuna eklendi.",
                      dissolved_groups=dissolved)


@router.delete("/{group_id}/members/{item_id}")
def remove_member(
    group_id: int,
    item_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("items", "edit")),
    domain: str = Depends(active_domain),
):
    grp = _group(db, group_id, domain, lock=True)
    if grp is None:
        return _err(404, "Grup bulunamadı.")
    it = (db.query(Item).filter(Item.id == item_id, Item.material_group_id == grp.id)
          .with_for_update().first())
    if it is None:
        return _err(404, "Kart bu grupta değil.")
    it.material_group_id = None
    dissolved = mg.prune(db, grp.id)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="material_group.remove",
                    target_type="material_group", target_id=grp.id, target_name=grp.name,
                    details={"item_id": it.id, "item": it.name, "dagildi": dissolved})
    if dissolved:
        log_admin_event(db, request, actor=current_user, action="material_group.dissolve",
                        target_type="material_group", target_id=grp.id, target_name=grp.name,
                        details={"sebep": "aktif üye 2'nin altına düştü"})
        return _with_meta(None, id=grp.id, name=grp.name, dissolved=True,
                          message=f"«{it.name}» çıkarıldı; grupta tek kart kaldığı için "
                                  f"«{grp.name}» grubu dağıtıldı.")
    return _with_meta(mg.group_payload(db, grp.id), dissolved=False,
                      message=f"«{it.name}» gruptan çıkarıldı.")


@router.delete("/{group_id}")
def dissolve_group(
    group_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("items", "edit")),
    domain: str = Depends(active_domain),
):
    grp = _group(db, group_id, domain, lock=True)
    if grp is None:
        return _err(404, "Grup bulunamadı.")
    ids = mg.dissolve(db, grp)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="material_group.dissolve",
                    target_type="material_group", target_id=grp.id, target_name=grp.name,
                    details={"item_ids": ids})
    return {"message": f"«{grp.name}» grubu dağıtıldı — kartlar yerinde, yalnız bağ kalktı.",
            "id": grp.id, "item_ids": ids, "dissolved": True}
