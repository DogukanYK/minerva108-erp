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

from fastapi import APIRouter, Depends, UploadFile, File, Form, Request, Query
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
from starlette.background import BackgroundTask
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional, List

from database import (get_db, DriveFile, DriveFolder, DriveCollection,
                      DriveCollectionFile, DriveCollectionFolder)
from core.auth import get_current_user
from core import drive as D

router = APIRouter(prefix="/api/drive", tags=["drive"])
share_router = APIRouter(tags=["drive-public"])

_COOKIE_SECURE = os.getenv("COOKIE_SECURE", "true").lower() not in ("0", "false", "no")


# ─── Şemalar ─────────────────────────────────────────────────────────────────

class CollectionCreate(BaseModel):
    name:         str = Field(..., min_length=1, max_length=150)
    file_ids:     List[int] = Field(default_factory=list, max_length=2000)
    folder_ids:   List[int] = Field(default_factory=list, max_length=500)   # klasör paylaşımı
    password:     Optional[str] = None          # boş/None = şifresiz
    expires_days: Optional[int] = Field(None, ge=0, le=3650)   # 0/None = süresiz
    custom_slug:  Optional[str] = Field(None, max_length=80)   # boş = rastgele kod


class CollectionUpdate(BaseModel):
    name:         Optional[str] = Field(None, max_length=150)
    file_ids:     Optional[List[int]] = Field(None, max_length=2000)
    folder_ids:   Optional[List[int]] = Field(None, max_length=500)
    password:     Optional[str] = None          # ""=şifre kaldır, "xx"=değiştir, None=dokunma
    clear_password: bool = False
    expires_days: Optional[int] = Field(None, ge=0, le=3650)
    custom_slug:  Optional[str] = Field(None, max_length=80)


class FolderCreate(BaseModel):
    name:      str = Field(..., min_length=1, max_length=255)
    parent_id: Optional[int] = None          # None = kök


class FolderRename(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class FileRename(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)


class MoveRequest(BaseModel):
    file_ids:         List[int] = Field(default_factory=list, max_length=2000)
    folder_ids:       List[int] = Field(default_factory=list, max_length=500)
    target_folder_id: Optional[int] = None   # None = kök


# ─── Yönetim (login) ─────────────────────────────────────────────────────────

@router.post("/upload", status_code=201)
async def upload_file(
    file: UploadFile = File(...),
    rel_path: Optional[str] = Form(None),
    folder_id: Optional[int] = Form(None),
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
    # Hedef klasör (yoksa kök); geçersiz folder_id → köke düş.
    target = folder_id
    if target is not None and not db.query(DriveFolder.id).filter(DriveFolder.id == target).first():
        target = None
    # Klasör yüklemesinde tarayıcı göreli yol gönderir → alt klasör ağacını
    # hedef ALTINDA kur, dosyayı en derin klasöre koy, adı basename yap.
    name = file.filename or "dosya"
    if rel_path:
        parts = [p for p in D.clean_rel_path(rel_path).split("/") if p]
        if parts:
            name = parts[-1]
            for seg in parts[:-1]:
                target = D.get_or_create_folder(db, seg, target, actor)
    rec = DriveFile(original_name=name[:255], stored_name=stored, folder_id=target,
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
    nf = db.query(func.count(DriveCollectionFolder.id)).filter(
        DriveCollectionFolder.collection_id == c.id).scalar() or 0
    from database import to_tr
    return {
        "id": c.id, "name": c.name, "token": c.share_token,
        "share_path": f"/s/{c.share_token}",
        "file_count": int(n),
        "folder_count": int(nf),
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
    # token: özel link (slug) verilmişse onu, yoksa tahmin edilemez rastgele
    if data.custom_slug and data.custom_slug.strip():
        slug = D.slugify(data.custom_slug)
        if len(slug) < 3:
            return JSONResponse(status_code=400, content={"detail": "Özel link en az 3 geçerli karakter (harf/rakam) içermeli."})
        if db.query(DriveCollection.id).filter(DriveCollection.share_token == slug).first():
            return JSONResponse(status_code=400, content={"detail": f"'{slug}' zaten kullanımda — başka bir ad deneyin."})
        token = slug
    else:
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
    # klasörleri bağla (paylaşım tüm alt ağacı kapsar)
    vfold = {fid for (fid,) in db.query(DriveFolder.id).filter(DriveFolder.id.in_(data.folder_ids or [])).all()}
    for i, fid in enumerate(data.folder_ids or []):
        if fid in vfold:
            db.add(DriveCollectionFolder(collection_id=c.id, folder_id=fid, sort_order=i))
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
    if data.custom_slug is not None and data.custom_slug.strip():
        slug = D.slugify(data.custom_slug)
        if len(slug) < 3:
            return JSONResponse(status_code=400, content={"detail": "Özel link en az 3 geçerli karakter içermeli."})
        if db.query(DriveCollection.id).filter(DriveCollection.share_token == slug,
                                               DriveCollection.id != cid).first():
            return JSONResponse(status_code=400, content={"detail": f"'{slug}' zaten kullanımda."})
        c.share_token = slug
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
    if data.folder_ids is not None:
        db.query(DriveCollectionFolder).filter(DriveCollectionFolder.collection_id == cid).delete()
        vfold = {fid for (fid,) in db.query(DriveFolder.id).filter(DriveFolder.id.in_(data.folder_ids)).all()}
        for i, fid in enumerate(data.folder_ids):
            if fid in vfold:
                db.add(DriveCollectionFolder(collection_id=cid, folder_id=fid, sort_order=i))
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


# ─── Klasör ağacı (browse / organize) ────────────────────────────────────────

def _file_dict(r, counts, to_tr) -> dict:
    return {
        "id": r.id, "name": r.original_name,
        "size_human": D.humanize(r.size_bytes),
        "content_type": r.content_type or "",
        "uploaded_by": r.uploaded_by or "—",
        "in_collections": int(counts.get(r.id, 0)),
        "folder_id": r.folder_id,
        "created_at": to_tr(r.created_at).strftime("%d.%m.%Y %H:%M") if r.created_at else "",
    }


@router.get("/list")
def list_folder(folder_id: Optional[int] = Query(None),
                db: Session = Depends(get_db), _: dict = Depends(get_current_user)):
    """Bir klasörün içeriği — breadcrumb + alt klasörler + dosyalar (Drive ana görünümü)."""
    from sqlalchemy import func
    from database import to_tr
    if folder_id is not None and not db.query(DriveFolder.id).filter(DriveFolder.id == folder_id).first():
        return JSONResponse(status_code=404, content={"detail": "Klasör bulunamadı."})
    folders = (db.query(DriveFolder)
               .filter(DriveFolder.parent_id.is_(None) if folder_id is None
                       else DriveFolder.parent_id == folder_id)
               .order_by(DriveFolder.name).all())
    files = (db.query(DriveFile)
             .filter(DriveFile.folder_id.is_(None) if folder_id is None
                     else DriveFile.folder_id == folder_id)
             .order_by(DriveFile.original_name).all())
    counts = dict(db.query(DriveCollectionFile.file_id, func.count(DriveCollectionFile.id))
                  .group_by(DriveCollectionFile.file_id).all())
    # alt klasör başına öğe sayısı (dosya + alt klasör)
    child_counts = {}
    if folders:
        fids = [f.id for f in folders]
        for fid, c in (db.query(DriveFile.folder_id, func.count(DriveFile.id))
                       .filter(DriveFile.folder_id.in_(fids)).group_by(DriveFile.folder_id).all()):
            child_counts[fid] = child_counts.get(fid, 0) + c
        for pid, c in (db.query(DriveFolder.parent_id, func.count(DriveFolder.id))
                       .filter(DriveFolder.parent_id.in_(fids)).group_by(DriveFolder.parent_id).all()):
            child_counts[pid] = child_counts.get(pid, 0) + c
    return {
        "folder_id": folder_id,
        "breadcrumb": D.folder_path(db, folder_id),
        "folders": [{"id": f.id, "name": f.name, "item_count": int(child_counts.get(f.id, 0))}
                    for f in folders],
        "files": [_file_dict(r, counts, to_tr) for r in files],
    }


@router.get("/tree")
def folder_tree(db: Session = Depends(get_db), _: dict = Depends(get_current_user)):
    """Tüm klasör ağacı — sol panel + 'Taşı' klasör seçici için."""
    rows = db.query(DriveFolder).order_by(DriveFolder.name).all()
    return {"folders": [{"id": f.id, "name": f.name, "parent_id": f.parent_id} for f in rows]}


@router.post("/folders", status_code=201)
def create_folder(data: FolderCreate, db: Session = Depends(get_db),
                  current_user: dict = Depends(get_current_user)):
    actor = current_user.get("full_name") or current_user.get("username") or "—"
    if data.parent_id is not None and not db.query(DriveFolder.id).filter(DriveFolder.id == data.parent_id).first():
        return JSONResponse(status_code=404, content={"detail": "Üst klasör bulunamadı."})
    name = D.sanitize_folder_name(data.name)
    if not name:
        return JSONResponse(status_code=400, content={"detail": "Geçerli bir klasör adı girin."})
    fid = D.get_or_create_folder(db, name, data.parent_id, actor)
    db.commit()
    return {"id": fid, "name": name, "parent_id": data.parent_id}


@router.put("/folders/{fid}")
def rename_folder(fid: int, data: FolderRename, db: Session = Depends(get_db),
                  _: dict = Depends(get_current_user)):
    f = db.query(DriveFolder).filter(DriveFolder.id == fid).first()
    if not f:
        return JSONResponse(status_code=404, content={"detail": "Klasör bulunamadı."})
    name = D.sanitize_folder_name(data.name)
    if not name:
        return JSONResponse(status_code=400, content={"detail": "Geçerli bir ad girin."})
    f.name = name
    db.commit()
    return {"id": f.id, "name": f.name}


@router.delete("/folders/{fid}")
def delete_folder(fid: int, db: Session = Depends(get_db), _: dict = Depends(get_current_user)):
    """Klasörü ÖZYİNELEMELİ sil — alt klasörler + dosyalar (disk dahil) + link bağları."""
    f = db.query(DriveFolder).filter(DriveFolder.id == fid).first()
    if not f:
        return JSONResponse(status_code=404, content={"detail": "Klasör bulunamadı."})
    ids = [fid] + D.folder_descendants(db, fid)
    files = db.query(DriveFile).filter(DriveFile.folder_id.in_(ids)).all()
    for x in files:
        D.delete_stored(x.stored_name)
        db.delete(x)   # drive_collection_file CASCADE ile temizlenir
    db.query(DriveFolder).filter(DriveFolder.id.in_(ids)).delete(synchronize_session=False)
    db.commit()
    return {"message": "Klasör silindi.", "deleted_files": len(files), "deleted_folders": len(ids)}


@router.put("/files/{file_id}")
def rename_file(file_id: int, data: FileRename, db: Session = Depends(get_db),
                _: dict = Depends(get_current_user)):
    f = db.query(DriveFile).filter(DriveFile.id == file_id).first()
    if not f:
        return JSONResponse(status_code=404, content={"detail": "Dosya bulunamadı."})
    name = D.sanitize_folder_name(data.name)   # slash/kontrol temizler (tek parça ad)
    if name:
        f.original_name = name[:255]
        db.commit()
    return {"id": f.id, "name": f.original_name}


@router.get("/dl/{file_id}")
def download_own_file(file_id: int, db: Session = Depends(get_db),
                      _: dict = Depends(get_current_user)):
    """Oturum açmış kullanıcı bir Drive dosyasını indirir (yönetim görünümü)."""
    f = db.query(DriveFile).filter(DriveFile.id == file_id).first()
    if not f:
        return JSONResponse(status_code=404, content={"detail": "Dosya bulunamadı."})
    try:
        path = D.stored_path(f.stored_name)
    except ValueError:
        return JSONResponse(status_code=404, content={"detail": "Dosya bulunamadı."})
    if not path.is_file():
        return JSONResponse(status_code=404, content={"detail": "Dosya diskte yok."})
    return FileResponse(str(path), media_type="application/octet-stream",
                        filename=D.safe_download_name(f.original_name))


# ─── Önizleme (Quick Look) ───────────────────────────────────────────────────
# İndirmeden önizleme.  GÜVENLİK: /dl bilerek octet-stream+attachment veriyor
# (XSS guard).  Burada SADECE beyaz-listedeki güvenli türler inline sunulur:
#   • foto/PDF  → /preview/raw  (nosniff + inline; tarayıcı kendi render eder)
#   • xlsx/csv  → sunucuda parse → HTML değil, JSON satır (ham dosya gitmez)
#   • metin/kod → sunucuda okunur → düz metin (istemci textContent ile basar)
# HTML/SVG asla belge olarak sunulmaz; SVG istemcide <img> ile gösterilir.

_PREVIEW_MAX_RAW   = 50 * 1024 * 1024   # foto/PDF inline tavanı (50 MB)
_PREVIEW_MAX_TEXT  = 200 * 1024         # metin önizleme tavanı (200 KB)
_PREVIEW_MAX_ROWS  = 100                # xlsx/csv satır tavanı
_PREVIEW_MAX_COLS  = 30                 # xlsx/csv sütun tavanı

_IMAGE_EXT = {"png", "jpg", "jpeg", "gif", "webp", "bmp", "svg"}
_IMAGE_MIME = {
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "gif": "image/gif",
    "webp": "image/webp", "bmp": "image/bmp", "svg": "image/svg+xml",
}
_SHEET_EXT = {"xlsx", "xlsm", "csv"}
_TEXT_EXT  = {"txt", "md", "markdown", "log", "json", "csv", "xml", "yaml", "yml",
              "ini", "cfg", "conf", "py", "js", "ts", "css", "html", "htm", "sql", "sh", "tsv"}


def _ext_of(name: str) -> str:
    return (name or "").rsplit(".", 1)[-1].lower() if "." in (name or "") else ""


def _preview_mode(f) -> str:
    """Dosyanın önizleme kipi: image | pdf | table | text | none."""
    ext = _ext_of(f.original_name)
    if ext in _IMAGE_EXT:
        return "image"
    if ext == "pdf" or (f.content_type or "") == "application/pdf":
        return "pdf"
    if ext in _SHEET_EXT and ext != "csv":   # csv hem tablo hem metin olabilir → tablo tercih
        return "table"
    if ext == "csv":
        return "table"
    if ext in _TEXT_EXT:
        return "text"
    return "none"


def _read_xlsx_rows(path):
    import openpyxl
    wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    ws = wb[wb.sheetnames[0]]
    rows, truncated = [], False
    for i, r in enumerate(ws.iter_rows(values_only=True)):
        if i >= _PREVIEW_MAX_ROWS:
            truncated = True
            break
        cells = ["" if c is None else str(c) for c in r[:_PREVIEW_MAX_COLS]]
        if len(r) > _PREVIEW_MAX_COLS:
            truncated = True
        rows.append(cells)
    wb.close()
    return rows, truncated


def _read_csv_rows(path):
    import csv
    rows, truncated = [], False
    with open(path, "r", encoding="utf-8", errors="replace", newline="") as fh:
        for i, row in enumerate(csv.reader(fh)):
            if i >= _PREVIEW_MAX_ROWS:
                truncated = True
                break
            if len(row) > _PREVIEW_MAX_COLS:
                truncated = True
            rows.append([str(c) for c in row[:_PREVIEW_MAX_COLS]])
    return rows, truncated


@router.get("/files/{file_id}/preview/meta")
def preview_meta(file_id: int, db: Session = Depends(get_db),
                 _: dict = Depends(get_current_user)):
    """Önizleme meta verisi: kip + (metin/tablo için) içerik gömülü."""
    f = db.query(DriveFile).filter(DriveFile.id == file_id).first()
    if not f:
        return JSONResponse(status_code=404, content={"detail": "Dosya bulunamadı."})
    base = {"id": f.id, "name": f.original_name, "size_human": D.humanize(f.size_bytes),
            "size_bytes": int(f.size_bytes or 0), "content_type": f.content_type or "",
            "download_url": f"/api/drive/dl/{f.id}"}
    mode = _preview_mode(f)
    try:
        path = D.stored_path(f.stored_name)
    except ValueError:
        return JSONResponse(status_code=404, content={"detail": "Dosya bulunamadı."})
    if not path.is_file():
        return JSONResponse(status_code=404, content={"detail": "Dosya diskte yok."})

    if mode in ("image", "pdf"):
        if int(f.size_bytes or 0) > _PREVIEW_MAX_RAW:
            return {**base, "mode": "none", "reason": "Dosya önizleme için çok büyük."}
        return {**base, "mode": mode, "raw_url": f"/api/drive/files/{f.id}/preview/raw"}

    if mode == "table":
        try:
            ext = _ext_of(f.original_name)
            rows, truncated = _read_csv_rows(path) if ext == "csv" else _read_xlsx_rows(path)
            return {**base, "mode": "table", "rows": rows, "truncated": truncated}
        except Exception:
            return {**base, "mode": "none", "reason": "Tablo okunamadı."}

    if mode == "text":
        try:
            data = path.read_bytes()[:_PREVIEW_MAX_TEXT + 1]
            truncated = len(data) > _PREVIEW_MAX_TEXT
            text = data[:_PREVIEW_MAX_TEXT].decode("utf-8", errors="replace")
            return {**base, "mode": "text", "content": text, "truncated": truncated}
        except Exception:
            return {**base, "mode": "none", "reason": "Metin okunamadı."}

    return {**base, "mode": "none", "reason": "Bu dosya türü için önizleme yok."}


@router.get("/files/{file_id}/preview/raw")
def preview_raw(file_id: int, db: Session = Depends(get_db),
                _: dict = Depends(get_current_user)):
    """Foto/PDF için GÜVENLİ inline bayt akışı (beyaz liste).  Diğer türler 415."""
    f = db.query(DriveFile).filter(DriveFile.id == file_id).first()
    if not f:
        return JSONResponse(status_code=404, content={"detail": "Dosya bulunamadı."})
    mode = _preview_mode(f)
    if mode not in ("image", "pdf"):
        return JSONResponse(status_code=415, content={"detail": "Bu tür inline önizlenemez."})
    if int(f.size_bytes or 0) > _PREVIEW_MAX_RAW:
        return JSONResponse(status_code=413, content={"detail": "Dosya çok büyük."})
    try:
        path = D.stored_path(f.stored_name)
    except ValueError:
        return JSONResponse(status_code=404, content={"detail": "Dosya bulunamadı."})
    if not path.is_file():
        return JSONResponse(status_code=404, content={"detail": "Dosya diskte yok."})
    # nosniff: tarayıcı içeriği başka türmüş gibi yorumlamasın.  inline: indirme değil görüntüle.
    if mode == "pdf":
        media = "application/pdf"
        # PDF tarayıcının yerel görüntüleyicisinde açılır; 'sandbox' viewer'ı bozabilir → koymuyoruz.
        csp = "default-src 'none'; object-src 'self'; img-src 'self'; style-src 'unsafe-inline'"
    else:
        media = _IMAGE_MIME.get(_ext_of(f.original_name), "application/octet-stream")
        # Foto <img> ile gösterilir; doğrudan gezinilirse (ör. kötücül SVG) 'sandbox' aktif içeriği keser.
        csp = "default-src 'none'; img-src 'self'; style-src 'unsafe-inline'; sandbox"
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Disposition": f'inline; filename="{D.safe_download_name(f.original_name)}"',
        "Content-Security-Policy": csp,
    }
    return FileResponse(str(path), media_type=media, headers=headers)


@router.post("/move")
def move_items(data: MoveRequest, db: Session = Depends(get_db),
               _: dict = Depends(get_current_user)):
    """Dosya ve/veya klasörleri hedef klasöre taşı (target_folder_id None = kök)."""
    target = data.target_folder_id
    if target is not None and not db.query(DriveFolder.id).filter(DriveFolder.id == target).first():
        return JSONResponse(status_code=404, content={"detail": "Hedef klasör bulunamadı."})
    # Döngü koruması: bir klasörü kendi içine / alt ağacına taşıma
    for fid in (data.folder_ids or []):
        if D.is_descendant(db, target, fid):
            return JSONResponse(status_code=400, content={"detail": "Bir klasörü kendi içine taşıyamazsınız."})
    moved_f = 0
    for fid in (data.folder_ids or []):
        f = db.query(DriveFolder).filter(DriveFolder.id == fid).first()
        if f and f.parent_id != target:
            f.parent_id = target
            moved_f += 1
    moved_x = 0
    if data.file_ids:
        moved_x = (db.query(DriveFile).filter(DriveFile.id.in_(data.file_ids))
                   .update({DriveFile.folder_id: target}, synchronize_session=False))
    db.commit()
    return {"moved_folders": moved_f, "moved_files": int(moved_x or 0)}


@router.get("/search")
def search_drive(q: str = Query("", max_length=120),
                 db: Session = Depends(get_db), _: dict = Depends(get_current_user)):
    """Ağaç genelinde dosya + klasör arama — sonuçlar yol bilgisiyle."""
    term = (q or "").strip()
    if len(term) < 2:
        return {"folders": [], "files": []}
    like = f"%{term}%"

    def _path_str(folder_id):
        return " / ".join(p["name"] for p in D.folder_path(db, folder_id)) or "Kök"

    folders = (db.query(DriveFolder).filter(DriveFolder.name.ilike(like))
               .order_by(DriveFolder.name).limit(100).all())
    files = (db.query(DriveFile).filter(DriveFile.original_name.ilike(like))
             .order_by(DriveFile.original_name).limit(200).all())
    return {
        "folders": [{"id": f.id, "name": f.name, "parent_id": f.parent_id,
                     "path": _path_str(f.parent_id)} for f in folders],
        "files": [{"id": r.id, "name": r.original_name, "size_human": D.humanize(r.size_bytes),
                   "folder_id": r.folder_id, "path": _path_str(r.folder_id)} for r in files],
    }


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
    # dosya bu paylaşımda mı? (doğrudan ya da bağlı klasör alt ağacında)
    if not D.file_in_share(db, c, file_id):
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


def _gate_share(c, token, request):
    """Paylaşım kapısı: geçerliyse None, aksi halde engel JSONResponse döner."""
    if not c:
        return JSONResponse(status_code=404, content={"detail": "Link bulunamadı."})
    if D.is_expired(c):
        return JSONResponse(status_code=410, content={"detail": "Bu paylaşımın süresi doldu."})
    if c.password_hash:
        sig = request.cookies.get(D.unlock_cookie_name(token), "")
        if not D.verify_unlock(token, sig):
            return JSONResponse(status_code=403, content={"detail": "Şifre gerekli."})
    return None


@share_router.get("/s/{token}/browse")
def browse_share(token: str, request: Request, folder: Optional[int] = Query(None),
                 db: Session = Depends(get_db)):
    """Paylaşım içeriğini gezilebilir döndürür (kök = bağlı klasörler + loose dosyalar)."""
    c = _load_open_collection(db, token)
    gate = _gate_share(c, token, request)
    if gate is not None:
        return gate
    from sqlalchemy import func
    if folder is None:
        link_fids = [fid for (fid,) in db.query(DriveCollectionFolder.folder_id)
                     .filter(DriveCollectionFolder.collection_id == c.id).all()]
        folders = (db.query(DriveFolder).filter(DriveFolder.id.in_(link_fids))
                   .order_by(DriveFolder.name).all()) if link_fids else []
        direct = [fid for (fid,) in db.query(DriveCollectionFile.file_id)
                  .filter(DriveCollectionFile.collection_id == c.id).all()]
        files = (db.query(DriveFile).filter(DriveFile.id.in_(direct))
                 .order_by(DriveFile.original_name).all()) if direct else []
        breadcrumb = []
    else:
        if not D.folder_in_share(db, c, folder):
            return JSONResponse(status_code=404, content={"detail": "Klasör bulunamadı."})
        folders = (db.query(DriveFolder).filter(DriveFolder.parent_id == folder)
                   .order_by(DriveFolder.name).all())
        files = (db.query(DriveFile).filter(DriveFile.folder_id == folder)
                 .order_by(DriveFile.original_name).all())
        breadcrumb = D.share_breadcrumb(db, c, folder)
    fids = [f.id for f in folders]
    child = {}
    if fids:
        for fid, n in (db.query(DriveFile.folder_id, func.count(DriveFile.id))
                       .filter(DriveFile.folder_id.in_(fids)).group_by(DriveFile.folder_id).all()):
            child[fid] = child.get(fid, 0) + n
        for pid, n in (db.query(DriveFolder.parent_id, func.count(DriveFolder.id))
                       .filter(DriveFolder.parent_id.in_(fids)).group_by(DriveFolder.parent_id).all()):
            child[pid] = child.get(pid, 0) + n
    return {
        "name": c.name, "folder_id": folder, "breadcrumb": breadcrumb,
        "folders": [{"id": f.id, "name": f.name, "item_count": int(child.get(f.id, 0))} for f in folders],
        "files": [{"id": r.id, "name": r.original_name, "size_human": D.humanize(r.size_bytes)} for r in files],
    }


@share_router.get("/s/{token}/zip")
def zip_share(token: str, request: Request, folder: Optional[int] = Query(None),
              db: Session = Depends(get_db)):
    """Paylaşımın (ya da bir alt klasörün) tüm dosyalarını yapı-koruyan ZIP indir."""
    c = _load_open_collection(db, token)
    gate = _gate_share(c, token, request)
    if gate is not None:
        return gate
    if folder is not None and not D.folder_in_share(db, c, folder):
        return JSONResponse(status_code=404, content={"detail": "Klasör bulunamadı."})
    try:
        path, name = D.build_zip(db, c, folder)
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "ZIP oluşturulamadı."})

    def _cleanup():
        try:
            os.unlink(path)
        except OSError:
            pass

    return FileResponse(path, media_type="application/zip", filename=name,
                        background=BackgroundTask(_cleanup))
