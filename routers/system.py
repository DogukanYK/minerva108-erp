# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
System health router — sistemin canlılık + genel sağlık durumu.

İki endpoint:
  • GET /health            — KİMLİKSİZ, hafif liveness probe.  Dış izleme
                             servisleri (UptimeRobot, Pingdom vb.) için.
                             DB'ye SELECT 1 atar; ok → 200, hata → 503.
                             Hiçbir hassas bilgi sızdırmaz.
  • GET /api/system/health — SuperAdmin'e özel ZENGİN sağlık raporu:
                             uygulama uptime, DB gecikme + boyut, disk,
                             zamanlayıcı işleri, son yedek, aylık snapshot,
                             sistem saati.  /system sayfası bunu kullanır.

Her kontrol bir "status" taşır: ok / warn / down.  Genel durum, en kötü
alt-kontrolün durumudur.
"""
import platform
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse, Response, FileResponse
from sqlalchemy import text
from sqlalchemy.orm import Session

from database import get_db, to_tr, tr_now, StockSnapshot
from core.auth import require_role

router = APIRouter(tags=["system"])

# Uygulama başlangıç anı — bu modül import edildiğinde (≈ app start) yakalanır.
STARTED_AT = datetime.utcnow()

APP_VERSION   = "2.0.0"
_PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Zamanlayıcı iş id'leri → insan-okur Türkçe etiket.
_JOB_LABELS = {
    "daily_expiry_scan":      "Günlük SKT taraması",
    "monthly_stock_snapshot": "Aylık stok snapshot",
}

# status önceliği — genel durumu hesaplarken "en kötü" kazanır.
_STATUS_RANK = {"ok": 0, "warn": 1, "down": 2}


# ─── Yardımcılar ────────────────────────────────────────────────────────────

def _humanize_bytes(n) -> str:
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024.0 or unit == "TB":
            return f"{int(n)} B" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} TB"


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    d, rem = divmod(s, 86400)
    h, rem = divmod(rem, 3600)
    m, _   = divmod(rem, 60)
    if d:
        return f"{d} gün {h} saat"
    if h:
        return f"{h} saat {m} dk"
    if m:
        return f"{m} dk"
    return f"{s} sn"


def _git_revision():
    """Mevcut git commit'ini kısa SHA olarak döner.  systemd PATH'i kısıtlı
    olduğu için (sadece venv/bin) git'i mutlak yoldan da dener.  Bulamazsa None."""
    for git_bin in ("/usr/bin/git", "git"):
        try:
            r = subprocess.run(
                [git_bin, "rev-parse", "--short", "HEAD"],
                cwd=str(_PROJECT_ROOT), capture_output=True, text=True, timeout=3,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
        except Exception:
            continue
    return None


def _worst(statuses) -> str:
    rank = max((_STATUS_RANK.get(s, 0) for s in statuses), default=0)
    return {0: "ok", 1: "warn", 2: "down"}[rank]


# ─── /health — hafif, kimliksiz liveness probe ──────────────────────────────

@router.get("/health", include_in_schema=False)
def health(db: Session = Depends(get_db)):
    """
    Minimal canlılık kontrolü — dış uptime izleyiciler için.
    DB'ye SELECT 1 atar; ulaşılabiliyorsa 200, yoksa 503.  Auth gerektirmez,
    hassas bilgi döndürmez.
    """
    try:
        db.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "error"})


# ─── /api/system/health — SuperAdmin'e özel zengin rapor ────────────────────

@router.get("/api/system/health")
def system_health(
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(["SuperAdmin"])),
):
    """Sistemin genel sağlık durumu — /system sayfası bunu çağırır."""
    checks: dict[str, dict] = {}

    # ── 1) Uygulama ──────────────────────────────────────────────────────
    uptime_s = (datetime.utcnow() - STARTED_AT).total_seconds()
    git_rev  = _git_revision()
    checks["app"] = {
        "label":   "Uygulama",
        "icon":    "bi-hdd-stack",
        "status":  "ok",
        "detail":  f"{_fmt_duration(uptime_s)} kesintisiz çalışıyor",
        "rows": [
            ("Sürüm",     f"v{APP_VERSION}" + (f" · {git_rev}" if git_rev else "")),
            ("Python",    platform.python_version()),
            ("Başlangıç", to_tr(STARTED_AT).strftime("%d.%m.%Y %H:%M")),
        ],
    }

    # ── 2) Veritabanı (PostgreSQL) ───────────────────────────────────────
    try:
        t0 = time.perf_counter()
        db.execute(text("SELECT 1"))
        latency = (time.perf_counter() - t0) * 1000.0
        size_b  = db.execute(
            text("SELECT pg_database_size(current_database())")
        ).scalar() or 0
        checks["database"] = {
            "label":  "Veritabanı (PostgreSQL)",
            "icon":   "bi-database",
            "status": "ok" if latency < 200 else "warn",
            "detail": f"{latency:.1f} ms yanıt · {_humanize_bytes(size_b)} veri",
            "rows": [
                ("Yanıt süresi", f"{latency:.1f} ms"),
                ("Boyut",        _humanize_bytes(size_b)),
            ],
        }
    except Exception as e:
        checks["database"] = {
            "label": "Veritabanı (PostgreSQL)", "icon": "bi-database",
            "status": "down", "detail": f"Bağlantı hatası: {type(e).__name__}",
            "rows": [],
        }

    # ── 3) Disk alanı ────────────────────────────────────────────────────
    try:
        u = shutil.disk_usage(str(_PROJECT_ROOT))
        used_pct = (u.used / u.total * 100.0) if u.total else 0.0
        checks["disk"] = {
            "label":  "Disk Alanı",
            "icon":   "bi-hdd",
            "status": "ok" if used_pct < 85 else ("warn" if used_pct < 95 else "down"),
            "detail": f"%{used_pct:.1f} dolu · {_humanize_bytes(u.free)} boş",
            "rows": [
                ("Kullanım", f"%{used_pct:.1f}"),
                ("Boş alan", _humanize_bytes(u.free)),
                ("Toplam",   _humanize_bytes(u.total)),
            ],
        }
    except Exception as e:
        checks["disk"] = {
            "label": "Disk Alanı", "icon": "bi-hdd",
            "status": "warn", "detail": f"Ölçülemedi: {type(e).__name__}", "rows": [],
        }

    # ── 4) Zamanlayıcı (APScheduler) ─────────────────────────────────────
    try:
        from core.scheduler import scheduler
        running = bool(scheduler.running)
        jobs = []
        for j in scheduler.get_jobs():
            nr = getattr(j, "next_run_time", None)
            # next_run_time tz-aware; sunucu UTC çalıştığı için naive kısmı UTC
            # kabul edip to_tr ile TR'ye çeviriyoruz.
            nr_txt = (
                to_tr(nr.replace(tzinfo=None)).strftime("%d.%m.%Y %H:%M")
                if nr else "—"
            )
            jobs.append({
                "label":    _JOB_LABELS.get(j.id, j.id),
                "next_run": nr_txt,
            })
        checks["scheduler"] = {
            "label":  "Zamanlayıcı (otomatik işler)",
            "icon":   "bi-clock-history",
            "status": "ok" if running else "warn",
            "detail": (f"{len(jobs)} iş kayıtlı, çalışıyor"
                       if running else "Zamanlayıcı çalışmıyor"),
            "rows": [(jb["label"], f"Sonraki: {jb['next_run']}") for jb in jobs],
        }
    except Exception as e:
        checks["scheduler"] = {
            "label": "Zamanlayıcı (otomatik işler)", "icon": "bi-clock-history",
            "status": "warn", "detail": f"Okunamadı: {type(e).__name__}", "rows": [],
        }

    # ── 5) Yedekler ──────────────────────────────────────────────────────
    try:
        from routers.backup import _list_backups
        backups = _list_backups()
        if backups:
            newest = backups[0]
            age_h  = (time.time() - newest["timestamp"]) / 3600.0
            total  = sum(b["size_bytes"] for b in backups)
            checks["backups"] = {
                "label":  "Yedekler",
                "icon":   "bi-shield-check",
                "status": "ok" if age_h <= 26 else ("warn" if age_h <= 50 else "down"),
                "detail": f"{len(backups)} yedek · son {age_h:.1f} saat önce",
                "rows": [
                    ("Son yedek",   newest["created_at"]),
                    ("Yedek sayısı", str(len(backups))),
                    ("Toplam boyut", _humanize_bytes(total)),
                ],
            }
        else:
            checks["backups"] = {
                "label": "Yedekler", "icon": "bi-shield-check",
                "status": "warn", "detail": "Hiç yedek bulunamadı", "rows": [],
            }
    except Exception as e:
        checks["backups"] = {
            "label": "Yedekler", "icon": "bi-shield-check",
            "status": "warn", "detail": f"Okunamadı: {type(e).__name__}", "rows": [],
        }

    # ── 6) Aylık stok snapshot ───────────────────────────────────────────
    try:
        last = (
            db.query(StockSnapshot.year, StockSnapshot.month)
            .order_by(StockSnapshot.year.desc(), StockSnapshot.month.desc())
            .first()
        )
        months = db.query(StockSnapshot.year, StockSnapshot.month).distinct().all()
        if last:
            checks["snapshots"] = {
                "label":  "Aylık Stok Snapshot",
                "icon":   "bi-camera",
                "status": "ok",
                "detail": f"{len(months)} ay donduruldu · son {last[1]:02d}.{last[0]}",
                "rows": [
                    ("Son donmuş ay", f"{last[1]:02d}.{last[0]}"),
                    ("Toplam ay",     str(len(months))),
                ],
            }
        else:
            checks["snapshots"] = {
                "label": "Aylık Stok Snapshot", "icon": "bi-camera",
                "status": "warn",
                "detail": "Henüz snapshot yok (ilk ay sonunda oluşur)", "rows": [],
            }
    except Exception as e:
        checks["snapshots"] = {
            "label": "Aylık Stok Snapshot", "icon": "bi-camera",
            "status": "warn", "detail": f"Okunamadı: {type(e).__name__}", "rows": [],
        }

    # ── 7) Sistem saati ──────────────────────────────────────────────────
    utc = datetime.utcnow()
    tr  = tr_now()
    offset_h = round((tr - utc).total_seconds() / 3600.0)
    checks["time"] = {
        "label":  "Sistem Saati",
        "icon":   "bi-clock",
        "status": "ok" if offset_h == 3 else "warn",
        "detail": f"TR: {tr.strftime('%d.%m.%Y %H:%M:%S')}",
        "rows": [
            ("Türkiye (UTC+3)", tr.strftime("%d.%m.%Y %H:%M:%S")),
            ("Sunucu (UTC)",    utc.strftime("%d.%m.%Y %H:%M:%S")),
        ],
    }

    overall = _worst(c["status"] for c in checks.values())
    return {
        "status":       overall,
        "generated_at": tr_now().strftime("%d.%m.%Y %H:%M:%S"),
        "checks":       checks,
    }


# ─── Aylık detaylı sistem raporu — SuperAdmin'e özel ────────────────────────

# Path-traversal'a karşı sıkı dosya adı kalıbı: minerva_rapor_2026-04.pdf
_REPORT_NAME_RE = re.compile(r"^minerva_rapor_\d{4}-\d{2}\.(pdf|xlsx)$")


@router.get("/api/system/report")
def generate_system_report(
    year: int,
    month: int,
    format: str = "pdf",
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(["SuperAdmin"])),
):
    """Seçilen ay için baştan aşağı detaylı sistem raporunu anında üretip indirir."""
    fmt = (format or "pdf").lower()
    if fmt not in ("pdf", "excel", "xlsx"):
        return JSONResponse(status_code=400,
                            content={"detail": "Geçersiz format (pdf veya excel)."})
    if not (2020 <= year <= 2100) or not (1 <= month <= 12):
        return JSONResponse(status_code=400, content={"detail": "Geçersiz yıl/ay."})

    from core.monthly_report import (
        gather_report_data, render_pdf, render_excel, report_filename)
    try:
        data = gather_report_data(db, year, month)
        if fmt == "pdf":
            content, media, ext = render_pdf(data), "application/pdf", "pdf"
        else:
            content = render_excel(data)
            media = ("application/vnd.openxmlformats-officedocument"
                     ".spreadsheetml.sheet")
            ext = "xlsx"
    except Exception:
        return JSONResponse(status_code=500,
                            content={"detail": "Rapor üretilemedi."})

    fname = report_filename(year, month, ext)
    return Response(
        content=content, media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@router.get("/api/system/reports")
def list_system_reports(_: dict = Depends(require_role(["SuperAdmin"]))):
    """Otomatik üretilip saklanmış aylık raporları listeler (en yeni önce)."""
    from core.monthly_report import SYSTEM_REPORT_DIR
    rows = []
    for f in sorted(SYSTEM_REPORT_DIR.glob("minerva_rapor_*"),
                    key=lambda p: p.name, reverse=True):
        try:
            st = f.stat()
        except OSError:
            continue
        rows.append({
            "filename":   f.name,
            "size":       _humanize_bytes(st.st_size),
            "created_at": to_tr(datetime.utcfromtimestamp(st.st_mtime))
                          .strftime("%d.%m.%Y %H:%M"),
        })
    return {"reports": rows}


@router.get("/api/system/reports/{filename}")
def download_system_report(
    filename: str,
    _: dict = Depends(require_role(["SuperAdmin"])),
):
    """Saklanmış bir aylık raporu indirir.  Dosya adı path-traversal'a karşı sıkı."""
    from core.monthly_report import SYSTEM_REPORT_DIR
    if not _REPORT_NAME_RE.match(filename or ""):
        return JSONResponse(status_code=400, content={"detail": "Geçersiz dosya adı."})
    path = SYSTEM_REPORT_DIR / filename
    if not path.is_file():
        return JSONResponse(status_code=404, content={"detail": "Rapor bulunamadı."})
    return FileResponse(str(path), filename=filename,
                        media_type="application/octet-stream")
