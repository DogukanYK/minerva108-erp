# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Trendyol modülü uçları — Faz 1, YALNIZ OKUMA (core/trendyol.py).

  GET    /api/trendyol/status                durum + akışlar + sayaçlar        view
  GET    /api/trendyol/orders                paket listesi (TR gün aralığı)    view
  GET    /api/trendyol/orders/{order_no}     sipariş detayı (+finans/kargo: profit)  view
  GET    /api/trendyol/catalog               eşleştirme + Trendyol/IMS stok    view
  GET    /api/trendyol/items?q=              eşleme için kart arama (iki panel) manage
  PUT    /api/trendyol/mappings              elle barkod eşlemesi (set dahil)  manage
  DELETE /api/trendyol/mappings?barcode=     eşlemeyi kaldır                   manage
  POST   /api/trendyol/test                  tek okuma çağrısıyla bağlantı testi manage
  POST   /api/trendyol/sync                  arka planda senkron (202; çalışıyorsa 409) manage
  PUT    /api/trendyol/config                otomatik senkron aç/kapat         manage
  GET    /api/trendyol/calibration           finans ham toplamları             profit (+finans rolü)
  GET    /api/trendyol/probe?kind=           maskeli ham örnek                 yalnız SuperAdmin

Cross-cutting (CRM/Drive/Shopify gibi): `active_domain` cookie'si BİLEREK
yok sayılır — tek satıcı hesabı iki panelin ürününü birlikte satar; panel
süzgeci eşleşen kartın domain'inden türetilir (`panel` parametresi).
Maliyet/komisyon alanları yalnız `core.trendyol.can_profit` ile döner (alan
hiç gelmez — 0 olarak da gelmez).  Distributor tüm uçlarda 403.
"""
import logging
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import trendyol as T
from core.audit import log_admin_event
from core.limiter import limiter
from core.permissions import _has_permission, require_internal_user
from core.supplier_prices import normalize as _fold
from database import Item, TrendyolBarcodeMap, User, get_db

logger = logging.getLogger("minerva108.trendyol")

router = APIRouter(prefix="/api/trendyol", tags=["trendyol"])

_PANELS = ("all", "cosmetics", "supplement", "mixed", "unmatched")
_BARCODE_RE = re.compile(r"^[0-9A-Za-z._\-]{1,64}$")


def _perm(action: str):
    return require_internal_user(("trendyol", action))


def _user(db: Session, payload: dict) -> Optional[User]:
    return db.query(User).filter(User.id == int(payload.get("sub", 0))).first()


def _actor_name(payload: dict) -> str:
    return (payload.get("full_name") or payload.get("username") or "—")[:100]


def _not_configured():
    return JSONResponse(status_code=400, content={
        "detail": "Trendyol bağlantısı yapılandırılmamış — sunucu .env'ine şu değişkenler eklenmeli: "
                  + ", ".join(T.config_problems()),
        "missing": T.config_problems()})


class SyncBody(BaseModel):
    kind: str = Field("all", pattern="^(orders|finance|catalog|all)$")
    backfill_days: Optional[int] = Field(None, ge=1, le=T.FIN_BACKFILL_MAX)


class ConfigBody(BaseModel):
    enabled: bool


class MapComponent(BaseModel):
    item_id: int = Field(..., gt=0)
    qty: float = Field(1.0, gt=0, le=100)


class MappingBody(BaseModel):
    barcode: str = Field(..., min_length=1, max_length=64)
    components: List[MapComponent] = Field(..., min_length=1, max_length=10)
    note: Optional[str] = Field(None, max_length=255)


# ─── Okuma ───────────────────────────────────────────────────────────────────

@router.get("/status")
def trendyol_status(db: Session = Depends(get_db), current_user: dict = Depends(_perm("view"))):
    user = _user(db, current_user)
    out = T.status_summary(db)
    out["can_profit"] = T.can_profit(user)
    out["can_manage"] = bool(user and _has_permission(user, "trendyol", "manage"))
    out["is_superadmin"] = bool(user and user.role == "SuperAdmin")
    return out


@router.get("/orders")
def trendyol_orders(start: Optional[str] = Query(None, max_length=10),
                    end: Optional[str] = Query(None, max_length=10),
                    status: Optional[str] = Query(None, max_length=30),
                    panel: str = Query("all", max_length=12),
                    q: Optional[str] = Query(None, max_length=100),
                    page: int = Query(0, ge=0, le=10_000),
                    size: int = Query(50, ge=1, le=200),
                    db: Session = Depends(get_db), current_user: dict = Depends(_perm("view"))):
    if panel not in _PANELS:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz panel süzgeci."})
    show_cost = T.can_profit(_user(db, current_user))
    return T.orders_view(db, start=start, end=end, status=status or None, panel=panel,
                         q=(q or "").strip() or None, page=page, size=size, show_cost=show_cost)


@router.get("/orders/{order_number}")
def trendyol_order_detail(order_number: str, db: Session = Depends(get_db),
                          current_user: dict = Depends(_perm("view"))):
    if len(order_number) > 40:
        return JSONResponse(status_code=404, content={"detail": "Sipariş bulunamadı."})
    out = T.order_detail(db, order_number, show_cost=T.can_profit(_user(db, current_user)))
    if out is None:
        return JSONResponse(status_code=404, content={"detail": "Sipariş bulunamadı."})
    return out


@router.get("/catalog")
def trendyol_catalog(panel: str = Query("all", max_length=12),
                     q: Optional[str] = Query(None, max_length=100),
                     problems: bool = False,
                     db: Session = Depends(get_db), _: dict = Depends(_perm("view"))):
    if panel not in _PANELS:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz panel süzgeci."})
    return T.catalog_view(db, panel=panel, q=(q or "").strip() or None, problems_only=problems)


@router.get("/items")
def trendyol_item_search(q: str = Query("", max_length=100), db: Session = Depends(get_db),
                         _: dict = Depends(_perm("manage"))):
    """Eşleme penceresi için: iki paneldeki AKTİF bitmiş ürünler (ad/barkod)."""
    fq = _fold(q)
    rows = (db.query(Item).filter(Item.category == T.FINISHED, Item.is_active == True)   # noqa: E712
            .order_by(Item.name).all())
    out = []
    for it in rows:
        if fq and fq not in _fold(it.name) and fq not in _fold(it.name_tr) and fq not in (it.barcode or ""):
            continue
        out.append({"id": it.id, "name": it.name, "barcode": it.barcode, "domain": it.domain,
                    "domain_label": T.PANEL_LABELS.get(it.domain, it.domain),
                    "current_stock": float(it.current_stock or 0), "unit": it.unit or "adet"})
        if len(out) >= 30:
            break
    return {"items": out}


# ─── Elle eşleme ─────────────────────────────────────────────────────────────

@router.put("/mappings")
def trendyol_set_mapping(body: MappingBody, request: Request, db: Session = Depends(get_db),
                         current_user: dict = Depends(_perm("manage"))):
    """Barkodun eşlemesini TAMAMEN değiştirir (tek kart ya da set bileşenleri)."""
    barcode = body.barcode.strip()
    if not _BARCODE_RE.match(barcode):
        return JSONResponse(status_code=400, content={"detail": "Barkod yalnız harf, rakam ve . _ - içerebilir."})
    ids = [c.item_id for c in body.components]
    if len(set(ids)) != len(ids):
        return JSONResponse(status_code=400, content={"detail": "Aynı kart iki kez eklenemez."})
    items = {it.id: it for it in db.query(Item).filter(Item.id.in_(ids)).all()}
    for iid in ids:
        it = items.get(iid)
        if it is None or not it.is_active or it.category != T.FINISHED:
            return JSONResponse(status_code=400, content={
                "detail": f"Kart #{iid} eşlenemez — yalnız aktif \"Bitmiş Ürün\" kartları seçilebilir."})
    old = [{"item_id": m.item_id, "qty": m.qty} for m in
           db.query(TrendyolBarcodeMap).filter(TrendyolBarcodeMap.barcode == barcode).all()]
    db.query(TrendyolBarcodeMap).filter(TrendyolBarcodeMap.barcode == barcode) \
      .delete(synchronize_session=False)
    note = (body.note or "").strip() or None
    for c in body.components:
        db.add(TrendyolBarcodeMap(barcode=barcode, item_id=c.item_id, qty=float(c.qty), note=note,
                                  created_by=_actor_name(current_user)))
    db.commit()
    log_admin_event(db, request, actor=current_user, action="trendyol.mapping",
                    target_type="trendyol_barcode", target_name=barcode,
                    details={"old": old, "new": [{"item_id": c.item_id, "qty": c.qty} for c in body.components],
                             "note": note})
    r = T.Resolver(db).resolve(barcode)
    return {"message": "Eşleme kaydedildi.", "barcode": barcode,
            "resolution": T.public_resolution(r, show_cost=False)}


@router.delete("/mappings")
def trendyol_delete_mapping(request: Request, barcode: str = Query(..., min_length=1, max_length=64),
                            db: Session = Depends(get_db), current_user: dict = Depends(_perm("manage"))):
    barcode = barcode.strip()
    rows = db.query(TrendyolBarcodeMap).filter(TrendyolBarcodeMap.barcode == barcode).all()
    if not rows:
        return JSONResponse(status_code=404, content={"detail": "Bu barkod için elle eşleme yok."})
    old = [{"item_id": m.item_id, "qty": m.qty} for m in rows]
    for m in rows:
        db.delete(m)
    db.commit()
    log_admin_event(db, request, actor=current_user, action="trendyol.mapping_delete",
                    target_type="trendyol_barcode", target_name=barcode, details={"old": old})
    r = T.Resolver(db).resolve(barcode)
    return {"message": "Elle eşleme kaldırıldı.", "barcode": barcode,
            "resolution": T.public_resolution(r, show_cost=False)}


# ─── Bağlantı / senkron ──────────────────────────────────────────────────────

@router.post("/test")
@limiter.limit("10/minute")
def trendyol_test(request: Request, db: Session = Depends(get_db),
                  current_user: dict = Depends(_perm("manage"))):
    if not T.is_configured():
        return _not_configured()
    api = None
    try:
        api = T.make_api()
        res = T.connection_test(api)
    except T.TrendyolError as e:
        log_admin_event(db, request, actor=current_user, action="trendyol.test",
                        target_type="integration", target_name="trendyol", details={"ok": False, "error": str(e)})
        return JSONResponse(status_code=502, content={"detail": str(e)})
    finally:
        if api is not None:
            api.close()
    log_admin_event(db, request, actor=current_user, action="trendyol.test",
                    target_type="integration", target_name="trendyol", details=res)
    return {"message": "Bağlantı çalışıyor.", **res}


@router.post("/sync", status_code=202)
@limiter.limit("10/minute")
def trendyol_sync(request: Request, body: SyncBody,
                  db: Session = Depends(get_db), current_user: dict = Depends(_perm("manage"))):
    """Senkronu istekten bağımsız bir daemon iş parçacığında başlatır
    (`T.start_in_background` — BackgroundTasks deploy'daki yeniden başlatmayı
    bekletirdi); ilerleme /status'tan izlenir.  Aynı anda tek çalıştırma
    (zamanlayıcı dahil) — çalışıyorsa 409."""
    if not T.is_configured():
        return _not_configured()
    if T.is_running():
        return JSONResponse(status_code=409, content={"detail": "Bir Trendyol senkronu zaten çalışıyor.",
                                                      "progress": T.progress_snapshot()})
    log_admin_event(db, request, actor=current_user, action="trendyol.sync",
                    target_type="integration", target_name="trendyol",
                    details={"kind": body.kind, "backfill_days": body.backfill_days})
    T.start_in_background(body.kind, backfill_days=body.backfill_days, actor=_actor_name(current_user))
    return {"message": "Senkron başlatıldı.", "kind": body.kind, "backfill_days": body.backfill_days}


@router.put("/config")
def trendyol_config(body: ConfigBody, request: Request, db: Session = Depends(get_db),
                    current_user: dict = Depends(_perm("manage"))):
    T.set_enabled(db, body.enabled)
    log_admin_event(db, request, actor=current_user, action="trendyol.config",
                    target_type="integration", target_name="trendyol", details={"enabled": body.enabled})
    return {"message": "Kaydedildi.", "enabled": T.get_enabled(db)}


# ─── Kalibrasyon ─────────────────────────────────────────────────────────────

@router.get("/calibration")
def trendyol_calibration(start: Optional[str] = Query(None, max_length=10),
                         end: Optional[str] = Query(None, max_length=10),
                         db: Session = Depends(get_db), current_user: dict = Depends(_perm("profit"))):
    if not T.can_profit(_user(db, current_user)):
        return JSONResponse(status_code=403, content={"detail": "Finans verisi yalnız finans rollerine açık."})
    return T.calibration_view(db, start=start, end=end)


@router.get("/probe")
@limiter.limit("10/minute")
def trendyol_probe(request: Request, kind: str = Query(..., max_length=20), db: Session = Depends(get_db),
                   current_user: dict = Depends(_perm("manage"))):
    """İlk 3 kaydın kişisel alanları maskeli ham hâli — alan adlarını ve zaman
    damgasını panelle karşılaştırmak için.  Yalnız SuperAdmin."""
    user = _user(db, current_user)
    if not user or user.role != "SuperAdmin":
        return JSONResponse(status_code=403, content={"detail": "Yalnız SuperAdmin."})
    if kind not in T.PROBE_KINDS:
        return JSONResponse(status_code=400, content={"detail": "Geçersiz tür.", "kinds": list(T.PROBE_KINDS)})
    if not T.is_configured():
        return _not_configured()
    api = None
    try:
        api = T.make_api()
        return T.probe(api, kind)
    except T.TrendyolError as e:
        return JSONResponse(status_code=502, content={"detail": str(e)})
    finally:
        if api is not None:
            api.close()
