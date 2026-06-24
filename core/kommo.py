# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Kommo CRM → Minerva CRM tek yön ayna (salt-okunur ingestion).

Kommo'daki firma/kişi/fırsat verileri bizim CRM'e akıtılır.  İki yol:
  • Toplu/delta çekme (pull) — GET /api/v4/{contacts,companies,leads}, Bearer
    long-lived token, sayfalama (limit max 250), ~7 req/s rate-limit.
  • Webhook (push) — Kommo olay POST'u; entity API'den taze çekilip upsert edilir.

Upsert anahtarı `kommo_id` → idempotent (tekrar import çift kayıt yaratmaz).
Senkron httpx.Client kullanılır (uygulamanın sync DB/route desenine uyumlu).
Env yoksa modül import edilebilir kalır; `is_configured()` False döner (no-op).
"""
import os
import re
import time
import logging
from datetime import datetime
from typing import Optional, Iterable

import httpx
from sqlalchemy.orm import Session

from database import CrmCompany, CrmContact, CrmDeal, CrmStage, CrmIntegrationState

logger = logging.getLogger("minerva108.kommo")

# Kommo standart sistem status_id'leri (her pipeline'da sabit)
_KOMMO_WON = 142
_KOMMO_LOST = 143
_PAGE_LIMIT = 250
_RATE_SLEEP = 0.2          # ~5 req/s — Kommo limiti 7/s'in altında güvenli


# ─── Yapılandırma ────────────────────────────────────────────────────────────

def _subdomain() -> str:
    raw = (os.getenv("KOMMO_SUBDOMAIN", "") or "").strip().lower()
    raw = raw.replace("https://", "").replace("http://", "")
    return raw.split(".")[0] if raw else ""


def _token() -> str:
    return (os.getenv("KOMMO_TOKEN", "") or "").strip()


def webhook_secret() -> str:
    return (os.getenv("KOMMO_WEBHOOK_SECRET", "") or "").strip()


def is_configured() -> bool:
    return bool(_subdomain() and _token())


def _base_url() -> str:
    return f"https://{_subdomain()}.kommo.com/api/v4"


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=_base_url(),
        headers={"Authorization": f"Bearer {_token()}", "Content-Type": "application/json"},
        timeout=20.0,
    )


# ─── Düşük seviye çekme (sayfalı, rate-limited) ──────────────────────────────

def _get_page(client: httpx.Client, path: str, params: dict) -> dict:
    r = client.get(path, params=params)
    if r.status_code == 204:           # boş koleksiyon
        return {}
    r.raise_for_status()
    time.sleep(_RATE_SLEEP)
    return r.json()


def _iter_entities(client: httpx.Client, path: str, key: str,
                   extra_params: Optional[dict] = None) -> Iterable[dict]:
    """path (örn. '/contacts') altındaki tüm entity'leri sayfalayarak ver."""
    page = 1
    while True:
        params = {"page": page, "limit": _PAGE_LIMIT}
        if extra_params:
            params.update(extra_params)
        data = _get_page(client, path, params)
        items = ((data.get("_embedded") or {}).get(key)) or []
        if not items:
            break
        for it in items:
            yield it
        if not ((data.get("_links") or {}).get("next")):
            break
        page += 1


# ─── Özel alan çıkarımı ──────────────────────────────────────────────────────

def _cf(entity: dict, code: str) -> Optional[str]:
    """custom_fields_values içinden field_code'a göre ilk değeri çek."""
    for f in (entity.get("custom_fields_values") or []):
        if (f.get("field_code") or "").upper() == code.upper():
            vals = f.get("values") or []
            if vals:
                return str(vals[0].get("value") or "").strip() or None
    return None


# Meta (Facebook/Instagram) reklam lead'leri Kommo'da 'fb…'/'ig…' etiketiyle gelir.
_META_TAG_RE = re.compile(r"^(fb|ig)", re.IGNORECASE)


def _source_from_tags(entity: dict) -> str:
    """Entity etiketlerine bakıp kaynağı belirle: 'meta' (fb/ig etiketi) yoksa 'kommo'."""
    for t in ((entity.get("_embedded") or {}).get("tags") or []):
        if _META_TAG_RE.match((t.get("name") or "").strip()):
            return "meta"
    return "kommo"


# ─── Aşama çözümü (Kommo status → CrmStage) ──────────────────────────────────

def _load_status_names(client: httpx.Client) -> dict:
    """status_id → status adı (tüm pipeline'lardan).  Hata olursa boş döner."""
    names = {}
    try:
        data = _get_page(client, "/leads/pipelines", {})
        for p in ((data.get("_embedded") or {}).get("pipelines") or []):
            for s in ((p.get("_embedded") or {}).get("statuses") or []):
                if s.get("id") is not None:
                    names[int(s["id"])] = s.get("name") or ""
    except Exception:
        logger.warning("Kommo pipeline statüleri okunamadı", exc_info=True)
    return names


def _resolve_stage(db: Session, status_id: Optional[int], status_name: str):
    """(stage_id, deal_status) döndür.  Kazanıldı/kaybedildi sistem ID'lerini ve
    ada göre eşlemeyi yapar; ad eşleşmezse aşamayı oluşturur."""
    if status_id == _KOMMO_WON:
        st = db.query(CrmStage).filter(CrmStage.is_won == True).order_by(CrmStage.sort_order).first()  # noqa: E712
        return (st.id if st else None), "won"
    if status_id == _KOMMO_LOST:
        st = db.query(CrmStage).filter(CrmStage.is_lost == True).order_by(CrmStage.sort_order).first()  # noqa: E712
        return (st.id if st else None), "lost"
    name = (status_name or "").strip()
    if name:
        st = db.query(CrmStage).filter(CrmStage.name.ilike(name)).first()
        if not st:
            maxo = db.query(CrmStage).count()
            st = CrmStage(name=name[:80], sort_order=maxo, is_won=False, is_lost=False)
            db.add(st); db.flush()
        return st.id, "open"
    first = db.query(CrmStage).filter(CrmStage.is_active == True).order_by(CrmStage.sort_order).first()  # noqa: E712
    return (first.id if first else None), "open"


# ─── Upsert (kommo_id anahtarı ile idempotent) ───────────────────────────────

def upsert_company(db: Session, kco: dict) -> CrmCompany:
    kid = int(kco["id"])
    c = db.query(CrmCompany).filter(CrmCompany.kommo_id == kid).first()
    if not c:
        c = CrmCompany(kommo_id=kid, source="kommo", created_by="Kommo")
        db.add(c)
    c.name = (kco.get("name") or f"Kommo #{kid}")[:200]
    c.phone = c.phone or _cf(kco, "PHONE")
    c.email = c.email or _cf(kco, "EMAIL")
    c.source = _source_from_tags(kco)
    db.flush()
    return c


def upsert_contact(db: Session, kc: dict, company_map: Optional[dict] = None) -> CrmContact:
    kid = int(kc["id"])
    c = db.query(CrmContact).filter(CrmContact.kommo_id == kid).first()
    if not c:
        c = CrmContact(kommo_id=kid, source="kommo", created_by="Kommo")
        db.add(c)
    c.full_name = (kc.get("name") or f"Kommo #{kid}")[:150]
    c.phone = _cf(kc, "PHONE") or c.phone
    c.email = _cf(kc, "EMAIL") or c.email
    c.whatsapp_number = c.whatsapp_number or _cf(kc, "PHONE")
    c.source = _source_from_tags(kc)
    # firma bağı — kommo company id → bizim company id
    emb = (kc.get("_embedded") or {}).get("companies") or []
    if emb:
        kco_id = int(emb[0].get("id"))
        link = (company_map or {}).get(kco_id)
        if link is None:
            row = db.query(CrmCompany.id).filter(CrmCompany.kommo_id == kco_id).first()
            link = row[0] if row else None
        if link:
            c.company_id = link
    db.flush()
    return c


def upsert_lead(db: Session, kl: dict, status_names: dict) -> CrmDeal:
    kid = int(kl["id"])
    d = db.query(CrmDeal).filter(CrmDeal.kommo_id == kid).first()
    if not d:
        d = CrmDeal(kommo_id=kid, source="kommo", created_by="Kommo", currency="TRY")
        db.add(d)
    d.title = (kl.get("name") or f"Kommo Fırsat #{kid}")[:200]
    d.value = float(kl.get("price") or 0)
    sid = kl.get("status_id")
    sid = int(sid) if sid is not None else None
    d.stage_id, d.status = _resolve_stage(db, sid, status_names.get(sid, ""))
    if d.status == "won" and not d.won_at:
        d.won_at = datetime.utcnow(); d.closed_at = datetime.utcnow()
    elif d.status == "lost" and not d.closed_at:
        d.closed_at = datetime.utcnow()
    # firma/kişi bağı
    emb = kl.get("_embedded") or {}
    cos = emb.get("companies") or []
    if cos:
        row = db.query(CrmCompany.id).filter(CrmCompany.kommo_id == int(cos[0]["id"])).first()
        if row:
            d.company_id = row[0]
    cts = emb.get("contacts") or []
    if cts:
        row = db.query(CrmContact.id).filter(CrmContact.kommo_id == int(cts[0]["id"])).first()
        if row:
            d.contact_id = row[0]
    d.source = _source_from_tags(kl)
    db.flush()
    return d


# ─── İçe aktarma / delta senkron ─────────────────────────────────────────────

def _state(db: Session) -> CrmIntegrationState:
    s = db.query(CrmIntegrationState).filter(CrmIntegrationState.provider == "kommo").first()
    if not s:
        s = CrmIntegrationState(provider="kommo", imported_total=0)
        db.add(s); db.flush()
    return s


def run_sync(db: Session, since_epoch: Optional[int] = None) -> dict:
    """Kommo'dan companies→contacts→leads çekip upsert eder.  since_epoch verilirse
    yalnızca o andan beri güncellenenler (delta).  Sayım döndürür."""
    if not is_configured():
        raise RuntimeError("Kommo yapılandırılmamış (KOMMO_SUBDOMAIN / KOMMO_TOKEN).")
    extra = {}
    if since_epoch:
        extra = {"filter[updated_at][from]": int(since_epoch)}
    counts = {"companies": 0, "contacts": 0, "leads": 0}
    started = datetime.utcnow()
    with _client() as client:
        status_names = _load_status_names(client)
        for kco in _iter_entities(client, "/companies", "companies", extra):
            upsert_company(db, kco); counts["companies"] += 1
        db.commit()
        for kc in _iter_entities(client, "/contacts", "contacts",
                                 {**extra, "with": "companies"} if extra else {"with": "companies"}):
            upsert_contact(db, kc); counts["contacts"] += 1
        db.commit()
        lead_params = {**extra, "with": "contacts"} if extra else {"with": "contacts"}
        for kl in _iter_entities(client, "/leads", "leads", lead_params):
            upsert_lead(db, kl, status_names); counts["leads"] += 1
        db.commit()

    st = _state(db)
    st.last_sync_at = started
    st.last_run_at = datetime.utcnow()
    st.cursor = int(started.timestamp())
    st.imported_total = (st.imported_total or 0) + sum(counts.values())
    st.last_status = f"OK · firma {counts['companies']} · kişi {counts['contacts']} · fırsat {counts['leads']}"
    db.commit()
    logger.info("Kommo sync tamam: %s", counts)
    return counts


# ─── Webhook ─────────────────────────────────────────────────────────────────

def _entity_ids_from_webhook(form: dict) -> dict:
    """Kommo form-POST (leads[add][0][id]=..) → {'leads': {ids}, 'contacts': {ids},
    'companies': {ids}, 'deleted': {...}}.  Sadece add/update/status ID'lerini toplar."""
    out = {"leads": set(), "contacts": set(), "companies": set(),
           "del_leads": set(), "del_contacts": set(), "del_companies": set()}
    ent_map = {"leads": "leads", "contacts": "contacts", "companies": "companies"}
    import re
    for key, val in form.items():
        m = re.match(r"(leads|contacts|companies)\[(\w+)\]\[\d+\]\[id\]$", key)
        if not m:
            continue
        ent, action = m.group(1), m.group(2)
        try:
            eid = int(val)
        except (TypeError, ValueError):
            continue
        if action == "delete":
            out["del_" + ent_map[ent]].add(eid)
        else:                          # add / update / status / responsible …
            out[ent_map[ent]].add(eid)
    return out


def handle_webhook(form: dict) -> dict:
    """Webhook payload'ını işle — etkilenen entity'leri API'den taze çekip upsert et.
    Kendi DB session'ını açar (scheduler/webhook deseni).  Kommo configure değilse no-op."""
    from database import SessionLocal
    ids = _entity_ids_from_webhook(form)
    result = {"companies": 0, "contacts": 0, "leads": 0, "deactivated": 0}
    if not is_configured():
        logger.warning("Kommo webhook geldi ama yapılandırma yok — atlandı")
        return result
    db = SessionLocal()
    try:
        with _client() as client:
            status_names = _load_status_names(client) if ids["leads"] else {}
            for cid in ids["companies"]:
                try:
                    upsert_company(db, _get_page(client, f"/companies/{cid}", {})); result["companies"] += 1
                except Exception:
                    logger.warning("Kommo company %s çekilemedi", cid, exc_info=True)
            db.commit()
            for cid in ids["contacts"]:
                try:
                    upsert_contact(db, _get_page(client, f"/contacts/{cid}", {"with": "companies"})); result["contacts"] += 1
                except Exception:
                    logger.warning("Kommo contact %s çekilemedi", cid, exc_info=True)
            db.commit()
            for lid in ids["leads"]:
                try:
                    upsert_lead(db, _get_page(client, f"/leads/{lid}", {"with": "contacts"}), status_names); result["leads"] += 1
                except Exception:
                    logger.warning("Kommo lead %s çekilemedi", lid, exc_info=True)
            db.commit()
        # Silmeler — tek yön aynada soft-deactivate (is_active=False)
        for model, kset in ((CrmCompany, ids["del_companies"]),
                            (CrmContact, ids["del_contacts"])):
            for kid in kset:
                row = db.query(model).filter(model.kommo_id == kid).first()
                if row and getattr(row, "is_active", True):
                    row.is_active = False; result["deactivated"] += 1
        for kid in ids["del_leads"]:
            d = db.query(CrmDeal).filter(CrmDeal.kommo_id == kid).first()
            if d and d.status == "open":
                d.status = "lost"; d.lost_reason = "Kommo'da silindi"; d.closed_at = datetime.utcnow()
                result["deactivated"] += 1
        db.commit()
    except Exception:
        logger.exception("Kommo webhook işleme hatası")
        db.rollback()
    finally:
        db.close()
    return result


def status_summary(db: Session) -> dict:
    """UI için entegrasyon durumu."""
    st = db.query(CrmIntegrationState).filter(CrmIntegrationState.provider == "kommo").first()
    from core.crm import fmt_dt
    return {
        "configured": is_configured(),
        "subdomain": _subdomain() or None,
        "webhook_secret_set": bool(webhook_secret()),
        "last_sync_at": fmt_dt(st.last_sync_at) if st else None,
        "last_run_at": fmt_dt(st.last_run_at) if st else None,
        "last_status": st.last_status if st else None,
        "imported_total": (st.imported_total if st else 0) or 0,
    }
