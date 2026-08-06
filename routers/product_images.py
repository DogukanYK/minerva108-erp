# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Amazon ürün görselleri — yükleme (auth'lu) + public servis (auth'suz).

İki ayrı router:
  • `router`        → `/api/product-images/*`  — liste/yükle/sil/CSV, `items.import`
  • `public_router` → `/public/product-images/{ad}` — **KİMLİK DOĞRULAMASI YOK**

Public uç neden auth'suz: Amazon flat-file'daki `main_image_url` /
`other_image_url1..8` adreslerine Amazon KENDİ sunucusundan istek atıp görseli
indirir.  Çerez, imzalı URL, süreli token ya da yönlendirme olursa indirme
başarısız olur ve ürün listelemesi reddedilir.  Bu yüzden uç:
  – 200 + `Content-Type: image/jpeg` döner (yönlendirme YOK)
  – oturum/çerez aramaz, referer kontrolü yapmaz
  – rate limit'ten MUAFtır (`@limiter.exempt`) — Amazon toplu indirirken
    global 300/dk sınırına takılmasın
  – ölçülü cache taşır: `max-age=3600, must-revalidate` (`immutable` DEĞİL —
    aynı adın üzerine yeni görsel yüklenebiliyor, bkz. upload ucu)

Saldırı yüzeyi dar tutuldu: yalnız GET/HEAD, yalnız `core/product_images.
FILENAME_RE` beyaz listesine uyan adlar, yalnız JPEG, dizin dışına çıkış
`safe_path()` ile iki kat engelli.  Yüklenen dosyadan EXIF/GPS/yorum
segmentleri sıyrılır (`strip_jpeg_metadata`) — public adresten konum bilgisi
sızmasın.  Dosya listeleme ucu public DEĞİL.
"""
import os
from typing import List, Optional

from fastapi import APIRouter, Depends, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from sqlalchemy.orm import Session

from database import get_db
from core.audit import log_admin_event
from core.limiter import limiter
from core.permissions import require_permission
from core.product_images import (MAX_UPLOAD_BYTES, build_csv, images_dir,
                                 is_jpeg, is_valid_name, list_images,
                                 normalize_name, parse_name, public_url,
                                 safe_path, strip_jpeg_metadata)

router = APIRouter(prefix="/api/product-images", tags=["product-images"])
public_router = APIRouter(prefix="/public", tags=["product-images-public"])

_PUBLIC_PATH = "/public/product-images"


def _base_url(request: Request) -> str:
    """CSV'ye yazılacak kök adres.

    Önce `PUBLIC_BASE_URL` env (prod'da https://ims.minerva108.com) — proxy
    arkasında scheme yanlış türetilmesin diye açık ayar tercih edilir.
    Yoksa isteğin kendi kökünden türetilir, https'e zorlanır (Amazon http
    kabul etmiyor).
    """
    env = (os.getenv("PUBLIC_BASE_URL") or "").strip().rstrip("/")
    if env:
        return env
    base = str(request.base_url).rstrip("/")
    if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://"):]
    return base


# ─── PUBLIC — kimlik doğrulaması YOK ─────────────────────────────────────────

# GET ve HEAD TEK handler — ayrı bir HEAD ucu yazılmamalı.  Starlette'in
# FileResponse'u HEAD'i kendisi doğru işler (gövdesiz, aynı başlıklar);
# elle yazılan ikinci temsil ETag/Last-Modified/Accept-Ranges'i kaçırıyor
# ve zamanla GET'ten sapıyordu (RFC 9110 §9.3.2).
@public_router.api_route("/product-images/{filename}", methods=["GET", "HEAD"])
@limiter.exempt                      # Amazon toplu indirirken 300/dk'ya takılmasın
def serve_product_image(filename: str, request: Request):
    """Ürün görselini doğrudan döndür — 200 + image/jpeg, yönlendirme yok."""
    path = safe_path(filename)       # beyaz liste + dizin-dışı koruması
    if path is None or not path.is_file():
        return PlainTextResponse("Not Found", status_code=404)
    return FileResponse(
        path,
        media_type="image/jpeg",
        headers={
            # `immutable` KULLANILMAZ: yükleme ucu aynı adın üzerine yazabiliyor
            # (görseli düzeltip tekrar yüklemek kasıtlı bir akış), immutable
            # ise aracı cache'lere "bir daha sorma" der ve eski görsel takılı
            # kalırdı.  1 saat + revalidate: Amazon'un tek seferlik indirmesi
            # için fazlasıyla yeterli, düzeltme de aynı gün yayılır.
            # FileResponse ayrıca ETag/Last-Modified ekler → 304 ucuz.
            "Cache-Control": "public, max-age=3600, must-revalidate",
            # Hotlink koruması YOK, referer kontrolü YOK (Amazon şartı).
            "Access-Control-Allow-Origin": "*",
        },
    )


# ─── Yönetim — auth arkasında ────────────────────────────────────────────────

@router.get("")
def list_product_images(
    request: Request,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("items", "import")),
):
    imgs = list_images()
    base = _base_url(request)
    skus = {}
    for im in imgs:
        skus.setdefault(im["sku"], []).append(im["slot"])
    return {
        "base_url": base,
        "public_path": _PUBLIC_PATH,
        "count": len(imgs),
        "sku_count": len(skus),
        "images": [{**im, "url": public_url(base, im["name"])} for im in imgs],
        "incomplete": sorted(s for s, sl in skus.items() if "MAIN" not in sl),
    }


@router.post("/upload")
def upload_product_images(
    request: Request,
    files: List[UploadFile] = File(...),
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("items", "import")),
):
    """Çoklu görsel yükleme.  Dosya adı sözleşmeye uymalı: <SKU>.MAIN.jpg

    Aynı ad tekrar yüklenirse ÜZERİNE yazılır — URL kalıcı kalsın diye
    (Amazon aynı adresi tekrar çeker, yeni görseli görür).
    """
    root = images_dir()
    saved, rejected = [], []
    for uf in files:
        original = uf.filename or ""
        name = normalize_name(original)
        if not is_valid_name(name):
            rejected.append({"file": original, "reason":
                             "Ad biçimi geçersiz — <SKU>.MAIN.jpg veya <SKU>.PT01.jpg olmalı "
                             "(SKU'da yalnız harf/rakam/tire/alt çizgi)."})
            continue
        data = uf.file.read(MAX_UPLOAD_BYTES + 1)
        if len(data) > MAX_UPLOAD_BYTES:
            rejected.append({"file": original,
                             "reason": f"Dosya çok büyük (>{MAX_UPLOAD_BYTES // (1024*1024)} MB)."})
            continue
        if not is_jpeg(data):
            rejected.append({"file": original,
                             "reason": "JPEG değil (uzantı .jpg olsa da içerik JPEG olmalı)."})
            continue
        path = safe_path(name)
        if path is None:
            rejected.append({"file": original, "reason": "Ad reddedildi."})
            continue
        replaced = path.is_file()
        # EXIF/GPS/yorum segmentlerini at — dosya public bir adresten
        # yayınlanacak, telefon fotoğrafındaki konum bilgisi sızmasın.
        clean = strip_jpeg_metadata(data)
        with open(path, "wb") as fh:
            fh.write(clean)
        sku, slot = parse_name(name)
        saved.append({"name": name, "sku": sku, "slot": slot,
                      "size": len(clean), "replaced": replaced,
                      "url": public_url(_base_url(request), name)})

    if saved:
        log_admin_event(db, request, actor=current_user, action="product_images.upload",
                        target_type="product_image", target_id=None,
                        target_name=f"{len(saved)} görsel",
                        details={"saved": [s["name"] for s in saved][:50],
                                 "rejected": len(rejected)})
    return {"saved": saved, "rejected": rejected,
            "message": f"{len(saved)} görsel yüklendi"
                       + (f", {len(rejected)} reddedildi" if rejected else "")}


@router.delete("/{filename}")
def delete_product_image(
    filename: str,
    request: Request,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("items", "import")),
):
    path = safe_path(filename)
    if path is None or not path.is_file():
        return JSONResponse(status_code=404, content={"detail": "Görsel bulunamadı."})
    path.unlink()
    log_admin_event(db, request, actor=current_user, action="product_images.delete",
                    target_type="product_image", target_id=None, target_name=filename)
    return {"message": f"{filename} silindi."}


@router.get("/export.csv")
def export_product_images_csv(
    request: Request,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("items", "import")),
):
    """Amazon flat-file için: sku,main_image_url,other_image_url1..8"""
    csv_text = build_csv(_base_url(request), list_images())
    return Response(
        content=csv_text.encode("utf-8-sig"),      # Excel TR için BOM
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="amazon_product_images.csv"'},
    )
