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

Satış artırma (spec 2026-10-13-trendyol-satis-artirma-design.md):
  GET    /api/trendyol/listing               Liste Doktoru tablo + özet + AI harcaması  view
  GET    /api/trendyol/listing/estimate      toplu üretim tahmini              manage
  GET    /api/trendyol/listing/{barcode}     içerik, kural dökümü, geçmiş, not, öneri  view
  PUT    /api/trendyol/listing/{barcode}/note   bilgi notu (audit)             manage
  POST   /api/trendyol/listing/{barcode}/suggest   öneri işi (202; bütçe)      manage
  POST   /api/trendyol/listing/suggest-batch       puanı en düşük N (202)      manage
  GET    /api/trendyol/questions             müşteri soruları                  view
  POST   /api/trendyol/questions/{id}/draft  cevap taslağı işi (202; bütçe)    manage
Yapay zekâ işleri arka planda koşar (core.trendyol_ai — tek çağrı 30–90 sn);
arayüz `job` alanını yoklar.  IMS Trendyol'a YAZMAZ: öneri/taslak kopyalanır.

Cross-cutting (CRM/Drive/Shopify gibi): `active_domain` cookie'si BİLEREK
yok sayılır — tek satıcı hesabı iki panelin ürününü birlikte satar; panel
süzgeci eşleşen kartın domain'inden türetilir (`panel` parametresi).
Maliyet/komisyon alanları yalnız `core.trendyol.can_profit` ile döner (alan
hiç gelmez — 0 olarak da gelmez).  Distributor tüm uçlarda 403.
"""
import asyncio
import logging
import re
from typing import List, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from core import trendyol as T
from core import trendyol_ai as AI
from core import trendyol_boost as B
from core.audit import log_admin_event
from core.limiter import limiter
from core.permissions import _has_permission, require_internal_user
from core.supplier_prices import normalize as _fold
from database import (Item, SessionLocal, TrendyolBarcodeMap, TrendyolProduct, TrendyolProductNote,
                      TrendyolQuestion, User, get_db)

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
    kind: str = Field("all", pattern="^(orders|finance|catalog|questions|all)$")
    backfill_days: Optional[int] = Field(None, ge=1, le=T.FIN_BACKFILL_MAX)


class ConfigBody(BaseModel):
    enabled: bool


class NoteBody(BaseModel):
    note: str = Field("", max_length=B.NOTE_MAX)


class SuggestBody(BaseModel):
    force: bool = False


class BatchBody(BaseModel):
    limit: int = Field(10, ge=1, le=500)


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


# ─── Satış artırma: Liste Doktoru ────────────────────────────────────────────

def _uid(payload: dict) -> int:
    return int(payload.get("sub", 0) or 0)


def _ai_error(e: "AI.AIError") -> JSONResponse:
    return JSONResponse(status_code=e.status, content={"detail": e.detail, "code": e.code})


def _bad_barcode(barcode: str) -> bool:
    return not _BARCODE_RE.match(barcode or "")


@router.get("/listing")
def trendyol_listing(db: Session = Depends(get_db), current_user: dict = Depends(_perm("view"))):
    user = _user(db, current_user)
    out = B.listing_view(db)
    jobs = AI.jobs_with_prefix("liste:")
    for r in out["rows"]:
        r["job"] = jobs.get(r["barcode"])
    out["ai"] = AI.spend_summary(db, _uid(current_user))
    out["batch"] = AI.batch_snapshot()
    out["can_manage"] = bool(user and _has_permission(user, "trendyol", "manage"))
    return out


@router.get("/listing/estimate")
def trendyol_listing_estimate(limit: int = Query(10, ge=1, le=500), db: Session = Depends(get_db),
                              current_user: dict = Depends(_perm("manage"))):
    """Toplu üretim öncesi tahmin: güncel önerisi olmayan, puanı en düşük
    `limit` ürün × ürün başına ortalama maliyet; kalan tavanla karşılaştırma."""
    barcodes = AI.batch_candidates(db, limit)
    spend = AI.spend_summary(db, _uid(current_user))
    per = spend["est_per_listing_usd"]
    total = round(per * len(barcodes), 2)
    rooms = [x for x in (spend["month_remaining_usd"], spend["day_remaining_usd"]) if x is not None]
    room = min(rooms) if rooms else None
    return {"count": len(barcodes), "per_item_usd": per, "total_usd": total, "room_usd": room,
            "fits": room is None or total <= room, "configured": spend["configured"],
            "model_label": spend["model_label"], "running": AI.batch_snapshot()["running"]}


@router.get("/listing/{barcode}")
def trendyol_listing_detail(barcode: str, db: Session = Depends(get_db),
                            _: dict = Depends(_perm("view"))):
    if _bad_barcode(barcode):
        return JSONResponse(status_code=404, content={"detail": "Ürün bulunamadı."})
    out = B.listing_detail(db, barcode)
    if out is None:
        return JSONResponse(status_code=404, content={"detail": "Ürün Trendyol kataloğunda yok."})
    out["job"] = AI.job_view(AI.listing_key(barcode))
    return out


@router.put("/listing/{barcode}/note")
def trendyol_listing_note(barcode: str, body: NoteBody, request: Request, db: Session = Depends(get_db),
                          current_user: dict = Depends(_perm("manage"))):
    """Ürün bilgi notu (öne çıkan içerikler, cilt tipi, kullanım…) — yapay
    zekâya giden tek ekip girdisi.  Reçete/oran/maliyet YAZILMAMALI."""
    if _bad_barcode(barcode) or db.query(TrendyolProduct.id).filter(TrendyolProduct.barcode == barcode).first() is None:
        return JSONResponse(status_code=404, content={"detail": "Ürün Trendyol kataloğunda yok."})
    prev = db.query(TrendyolProductNote).filter(TrendyolProductNote.barcode == barcode).first()
    old = (prev.note or "") if prev else ""
    row = B.save_note(db, barcode, body.note, _actor_name(current_user))
    log_admin_event(db, request, actor=current_user, action="trendyol.note",
                    target_type="trendyol_product", target_name=barcode,
                    details={"old": old[:500], "new": (row.note or "")[:500]})
    return {"message": "Bilgi notu kaydedildi.",
            "note": {"text": row.note, "updated_by": row.updated_by, "updated_at": B._fmt(row.updated_at)}}


def _listing_preflight(barcode: str, user_id: int) -> None:
    db = SessionLocal()
    try:
        if db.query(TrendyolProduct.id).filter(TrendyolProduct.barcode == barcode).first() is None:
            raise AI.AIError(404, "not_found", "Ürün Trendyol kataloğunda yok.")
        denied = AI.budget_denied(db, user_id)
        if denied is not None:
            raise AI.AIError(denied.status, denied.code, denied.detail)
    finally:
        db.close()


@router.post("/listing/suggest-batch", status_code=202)
@limiter.limit("5/minute")
async def trendyol_listing_batch(request: Request, body: BatchBody,
                                 current_user: dict = Depends(_perm("manage"))):
    """Puanı en düşük N ürün (güncel önerisi olanlar atlanır) için sırayla
    öneri üretir; tavan dolarsa kalan ürünlerde durur.  Aynı anda tek toplu iş."""
    if AI.batch_snapshot()["running"]:
        return JSONResponse(status_code=409, content={"detail": "Toplu öneri üretimi zaten çalışıyor.",
                                                      "batch": AI.batch_snapshot()})
    uid = _uid(current_user)

    def prepare():
        db = SessionLocal()
        try:
            denied = AI.budget_denied(db, uid)
            if denied is not None:
                raise AI.AIError(denied.status, denied.code, denied.detail)
            barcodes = AI.batch_candidates(db, body.limit)
            if barcodes:
                log_admin_event(db, request, actor=current_user, action="trendyol.ai_batch",
                                target_type="integration", target_name="trendyol",
                                details={"limit": body.limit, "count": len(barcodes)})
            return barcodes
        finally:
            db.close()

    try:
        barcodes = await asyncio.to_thread(prepare)
    except AI.AIError as e:
        return _ai_error(e)
    if not barcodes:
        return JSONResponse(status_code=200, content={"message": "Güncel önerisi olmayan ürün kalmadı.",
                                                      "count": 0, "batch": AI.batch_snapshot()})
    if not AI.start_batch(barcodes, user_id=uid, actor=_actor_name(current_user)):
        return JSONResponse(status_code=409, content={"detail": "Toplu öneri üretimi zaten çalışıyor.",
                                                      "batch": AI.batch_snapshot()})
    return {"message": f"{len(barcodes)} ürün için öneri üretimi başladı.", "count": len(barcodes),
            "batch": AI.batch_snapshot()}


@router.post("/listing/{barcode}/suggest", status_code=202)
@limiter.limit("20/minute")
async def trendyol_listing_suggest(request: Request, barcode: str, body: SuggestBody,
                                   current_user: dict = Depends(_perm("manage"))):
    """Öneri işini arka planda başlatır (202); sonuç `GET /listing/{barcode}`
    `job` + `suggestion` alanlarından izlenir.  Aynı girdiyle kayıtlı öneri
    varsa ücretsiz döner (force=false)."""
    if _bad_barcode(barcode):
        return JSONResponse(status_code=404, content={"detail": "Ürün bulunamadı."})
    uid = _uid(current_user)
    try:
        await asyncio.to_thread(_listing_preflight, barcode, uid)
        AI.spawn_listing(barcode, user_id=uid, actor=_actor_name(current_user), force=body.force)
    except AI.AIError as e:
        return _ai_error(e)
    return {"message": "Öneri hazırlanıyor…", "job": AI.job_view(AI.listing_key(barcode))}


# ─── Satış artırma: müşteri soruları ─────────────────────────────────────────

@router.get("/questions")
def trendyol_questions(status: Optional[str] = Query(None, max_length=30), db: Session = Depends(get_db),
                       current_user: dict = Depends(_perm("view"))):
    user = _user(db, current_user)
    out = B.questions_view(db, status=status or None)
    jobs = AI.jobs_with_prefix("soru:")
    for q in out["questions"]:
        q["job"] = jobs.get(str(q["question_id"]))
    out["can_manage"] = bool(user and _has_permission(user, "trendyol", "manage"))
    return out


def _question_preflight(question_id: int, user_id: int) -> None:
    db = SessionLocal()
    try:
        q = db.query(TrendyolQuestion).filter(TrendyolQuestion.question_id == question_id).first()
        if q is None:
            raise AI.AIError(404, "not_found", "Soru bulunamadı.")
        if q.status != T.WAITING:
            raise AI.AIError(400, "not_waiting", "Bu soru cevap beklemiyor; taslak yalnız bekleyen sorular için.")
        denied = AI.budget_denied(db, user_id)
        if denied is not None:
            raise AI.AIError(denied.status, denied.code, denied.detail)
    finally:
        db.close()


@router.post("/questions/{question_id}/draft", status_code=202)
@limiter.limit("20/minute")
async def trendyol_question_draft(request: Request, question_id: int,
                                  current_user: dict = Depends(_perm("manage"))):
    """Cevap taslağı işini arka planda başlatır (202).  Taslak OTOMATİK
    GÖNDERİLMEZ — ekip kopyalayıp Trendyol'da cevaplar."""
    if question_id <= 0:
        return JSONResponse(status_code=404, content={"detail": "Soru bulunamadı."})
    uid = _uid(current_user)
    try:
        await asyncio.to_thread(_question_preflight, question_id, uid)
        AI.spawn_answer(question_id, user_id=uid, actor=_actor_name(current_user))
    except AI.AIError as e:
        return _ai_error(e)
    return {"message": "Taslak hazırlanıyor…", "job": AI.job_view(AI.question_key(question_id))}
