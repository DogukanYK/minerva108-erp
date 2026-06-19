# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Minerva Drive — yönetim (login'li) + public paylaşım uçları.

  router        (/api/drive/*)  — yükle, dosya/koleksiyon CRUD; oturum gerekir.
  share_router  (/s/*)          — public: şifre kilidi açma + dosya indirme.
                                  Paylaşım SAYFASI api_main'de (GET /s/{token}).
"""
import os
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, UploadFile, File, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional, List

from database import get_db, DriveFile, DriveCollection, DriveCollectionFile
from core.auth import get_current_user
from core import drive as D

router = APIRouter(prefix="/api/drive", tags=["drive"])
share_router = APIRouter(tags=["drive-public"])

_COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() not in ("0", "false", "no")


# ─── Şemalar ─────────────────────────────────────────────────────────────────

class CollectionCreate(BaseModel):
    name:         str = Field(..., min_length=1, max_length=150)
    file_ids:     List[int] = Field(default_factory=list, max_length=500)
    password:     Optional[str] = None          # boş/None = şifresiz
    expires_days: Optional[int] = Field(None, ge=0, le=3650)   # 0/None = süresiz


class CollectionUpdate(BaseModel):
    name:         Optional[str] = Field(None, max_length=150)
    file_ids:     Optional[List[int]] = Field(None, max_length=500)
    password:     Optional[str] = None          # ""=şifre kaldır, "xx"=değiştir, None=dokunma
    clear_password: bool = False
    expires_days: Optional[int] = Field(None, ge=0, le=3650)


# ─── Yönetim (login) ─────────────────────────────────────────────────────────

@router.post("/upload", status_code=201)
async def upload_file(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    actor = current_user.get("full_name") or current_user.get("username") or "—"
    try:
        stored, size, ctype = await D.save_upload(file)
    except ValueError as e:
        return JSONResponse(status_code=413, content={"detail": str(e)})
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "Yükleme sırasında hata oluştu."})
    rec = DriveFile(original_name=(file.filename or "dosya")[:255], stored_name=stored,
                    size_bytes=size, content_type=ctype, uploaded_by=actor)
    db.add(rec); db.commit(); db.refresh(rec)
    return {"id": rec.id, "name": rec.original_name, "size_human": D.humanize(size)}


@router.get("/files")
def list_files(db: Session = Depends(get_db), _: dict = Depends(get_current_user)):
    rows = db.query(DriveFile).order_by(DriveFile.id.desc()).all()
    # her dosya kaç koleksiyonda?
    from sqlalchemy import func
    counts = dict(
        db.query(DriveCollectionFile.file_id, func.count(DriveCollectionFile.id))
        .group_by(DriveCollectionFile.file_id).all()
    )
    from database import to_tr
    return [{
        "id": r.id, "name": r.original_name,
        "size_human": D.humanize(r.size_bytes),
        "content_type": r.content_type or "",
        "uploaded_by": r.uploaded_by or "—",
        "in_collections": int(counts.get(r.id, 0)),
        "created_at": to_tr(r.created_at).strftime("%d.%m.%Y %H:%M") if r.created_at else "",
    } for r in rows]


@router.delete("/files/{file_id}")
def delete_file(file_id: int, db: Session = Depends(get_db), _: dict = Depends(get_current_user)):
    f = db.query(DriveFile).filter(DriveFile.id == file_id).first()
    if not f:
        return JSONResponse(status_code=404, content={"detail": "Dosya bulunamadı."})
    stored = f.stored_name
    db.delete(f)          # drive_collection_file CASCADE ile temizlenir
    db.commit()
    D.delete_stored(stored)
    return {"message": "Dosya silindi."}


def _serialize_collection(db: Session, c: DriveCollection) -> dict:
    from sqlalchemy import func
    n = db.query(func.count(DriveCollectionFile.id)).filter(
        DriveCollectionFile.collection_id == c.id).scalar() or 0
    from database import to_tr
    return {
        "id": c.id, "name": c.name, "token": c.share_token,
        "share_path": f"/s/{c.share_token}",
        "file_count": int(n),
        "protected": bool(c.password_hash),
        "expires_at": to_tr(c.expires_at).strftime("%d.%m.%Y %H:%M") if c.expires_at else None,
        "expired": D.is_expired(c),
        "created_at": to_tr(c.created_at).strftime("%d.%m.%Y %H:%M") if c.created_at else "",
    }


@router.get("/collections")
def list_collections(db: Session = Depends(get_db), _: dict = Depends(get_current_user)):
    rows = db.query(DriveCollection).order_by(DriveCollection.id.desc()).all()
    return [_serialize_collection(db, c) for c in rows]


@router.post("/collections", status_code=201)
def create_collection(
    data: CollectionCreate,
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    actor = current_user.get("full_name") or current_user.get("username") or "—"
    # benzersiz token
    token = D.new_token()
    while db.query(DriveCollection.id).filter(DriveCollection.share_token == token).first():
        token = D.new_token()
    exp = (datetime.utcnow() + timedelta(days=data.expires_days)
           if data.expires_days else None)
    pw = D.hash_password(data.password) if (data.password and data.password.strip()) else None
    c = DriveCollection(name=data.name.strip(), share_token=token, password_hash=pw,
                        expires_at=exp, created_by=actor)
    db.add(c); db.flush()
    # dosyaları bağla (yalnızca var olanlar)
    valid = {fid for (fid,) in db.query(DriveFile.id).filter(DriveFile.id.in_(data.file_ids or [])).all()}
    for i, fid in enumerate(data.file_ids or []):
        if fid in valid:
            db.add(DriveCollectionFile(collection_id=c.id, file_id=fid, sort_order=i))
    db.commit(); db.refresh(c)
    return _serialize_collection(db, c)


@router.put("/collections/{cid}")
def update_collection(
    cid: int, data: CollectionUpdate,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    c = db.query(DriveCollection).filter(DriveCollection.id == cid).first()
    if not c:
        return JSONResponse(status_code=404, content={"detail": "Link bulunamadı."})
    if data.name is not None and data.name.strip():
        c.name = data.name.strip()
    if data.clear_password:
        c.password_hash = None
    elif data.password and data.password.strip():
        c.password_hash = D.hash_password(data.password)
    if data.expires_days is not None:
        c.expires_at = (datetime.utcnow() + timedelta(days=data.expires_days)
                        if data.expires_days > 0 else None)
    if data.file_ids is not None:
        db.query(DriveCollectionFile).filter(DriveCollectionFile.collection_id == cid).delete()
        valid = {fid for (fid,) in db.query(DriveFile.id).filter(DriveFile.id.in_(data.file_ids)).all()}
        for i, fid in enumerate(data.file_ids):
            if fid in valid:
                db.add(DriveCollectionFile(collection_id=cid, file_id=fid, sort_order=i))
    db.commit(); db.refresh(c)
    return _serialize_collection(db, c)


@router.delete("/collections/{cid}")
def delete_collection(cid: int, db: Session = Depends(get_db), _: dict = Depends(get_current_user)):
    c = db.query(DriveCollection).filter(DriveCollection.id == cid).first()
    if not c:
        return JSONResponse(status_code=404, content={"detail": "Link bulunamadı."})
    db.delete(c)   # drive_collection_file CASCADE; dosyalar (DriveFile) DURUR
    db.commit()
    return {"message": "Link silindi."}


# ─── Public paylaşım (auth YOK) ──────────────────────────────────────────────

def _load_open_collection(db: Session, token: str):
    """token → koleksiyon; yoksa None.  (expiry/şifre kontrolü çağrıda yapılır)"""
    return db.query(DriveCollection).filter(DriveCollection.share_token == token).first()


@share_router.post("/s/{token}/unlock")
def unlock_share(token: str, request: Request, password: str = Form(""),
                 db: Session = Depends(get_db)):
    c = _load_open_collection(db, token)
    if not c or D.is_expired(c):
        return RedirectResponse(url=f"/s/{token}", status_code=303)
    if not c.password_hash or not D.verify_password(password, c.password_hash):
        return RedirectResponse(url=f"/s/{token}?e=1", status_code=303)
    resp = RedirectResponse(url=f"/s/{token}", status_code=303)
    resp.set_cookie(D.unlock_cookie_name(token), D.sign_unlock(token),
                    max_age=60 * 60 * 12, httponly=True, samesite="lax",
                    secure=_COOKIE_SECURE, path=f"/s/{token}")
    return resp


@share_router.get("/s/{token}/f/{file_id}")
def download_share_file(token: str, file_id: int, request: Request,
                        db: Session = Depends(get_db)):
    c = _load_open_collection(db, token)
    if not c:
        return JSONResponse(status_code=404, content={"detail": "Link bulunamadı."})
    if D.is_expired(c):
        return JSONResponse(status_code=410, content={"detail": "Bu paylaşımın süresi doldu."})
    if c.password_hash:
        sig = request.cookies.get(D.unlock_cookie_name(token), "")
        if not D.verify_unlock(token, sig):
            return JSONResponse(status_code=403, content={"detail": "Şifre gerekli."})
    # dosya bu koleksiyonda mı?
    link = (db.query(DriveCollectionFile)
            .filter(DriveCollectionFile.collection_id == c.id,
                    DriveCollectionFile.file_id == file_id).first())
    if not link:
        return JSONResponse(status_code=404, content={"detail": "Dosya bulunamadı."})
    f = db.query(DriveFile).filter(DriveFile.id == file_id).first()
    if not f:
        return JSONResponse(status_code=404, content={"detail": "Dosya bulunamadı."})
    try:
        path = D.stored_path(f.stored_name)
    except ValueError:
        return JSONResponse(status_code=404, content={"detail": "Dosya bulunamadı."})
    if not path.is_file():
        return JSONResponse(status_code=404, content={"detail": "Dosya diskte yok."})
    # octet-stream + attachment → tarayıcıda çalıştırma/inline render yok (XSS guard)
    return FileResponse(str(path), media_type="application/octet-stream",
                        filename=D.safe_download_name(f.original_name))
