# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Undo router — Ctrl+Z / toolbar buton'unun arkasındaki endpoint'ler.

GET  /api/undo/peek   → en yeni undoable entry (button enable/disable + tooltip)
POST /api/undo        → entry'i geri al, başarı/hata mesajı dön
"""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from database import get_db
from core.auth import get_current_user
from core.undo import (
    apply_undo, peek_latest_undoable,
    UndoConflict, UndoTargetGone, UndoUnsupported,
)


router = APIRouter(prefix="/api/undo", tags=["undo"])


@router.get("/peek")
def peek_undo(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    Bu user'ın en yeni undoable entry'sinin özetini dön.  Frontend bu
    cevaba göre button'u enable/disable eder ve tooltip yazar.
    """
    uid = int(current_user.get("sub", 0))
    if not uid:
        return {"available": False}

    entry = peek_latest_undoable(db, uid)
    if not entry:
        return {"available": False}
    return {
        "available":   True,
        "id":          entry.id,
        "action_type": entry.action_type,
        "description": entry.description,
        "created_at":  entry.created_at.isoformat() + "Z",
    }


@router.post("", status_code=200)
def do_undo(
    db: Session = Depends(get_db),
    current_user: dict = Depends(get_current_user),
):
    """
    En yeni undoable entry'i geri al.  Çakışma varsa 409, hedef kaybolduysa
    410, desteklenmeyen tip için 400.
    """
    uid = int(current_user.get("sub", 0))
    if not uid:
        return JSONResponse(status_code=401, content={"detail": "Oturum geçersiz."})

    entry = peek_latest_undoable(db, uid)
    if not entry:
        return JSONResponse(status_code=404, content={"detail": "Geri alınacak bir hareket yok."})

    try:
        msg = apply_undo(db, entry)
        db.commit()
        return {"message": msg, "action_type": entry.action_type}
    except UndoConflict as e:
        db.rollback()
        return JSONResponse(status_code=409, content={"detail": str(e)})
    except UndoTargetGone as e:
        db.rollback()
        return JSONResponse(status_code=410, content={"detail": str(e)})
    except UndoUnsupported as e:
        db.rollback()
        return JSONResponse(status_code=400, content={"detail": str(e)})
    except Exception as e:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": f"Geri alma hatası: {e}"})
