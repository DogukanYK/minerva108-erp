"""
Amazon ürün görselleri — public servis + yükleme testleri.

Amazon'un flat-file yüklemesi görselleri KENDİ sunucusundan indirir; bu yüzden
public ucun sözleşmesi katı:
  • 200 + Content-Type: image/jpeg · yönlendirme YOK · çerez/auth YOK
  • oturum açık olmadan da erişilebilir
Ve public olduğu için saldırı yüzeyi test edilir: path traversal, uzantı
yalanı, beyaz liste dışı adlar.
"""
import io
import os

import pytest
from fastapi.testclient import TestClient

from core.product_images import (build_csv, images_dir, is_valid_name,
                                 normalize_name, parse_name, safe_path)

_HDR = {"Origin": "http://testserver"}
_API = "/api/product-images"
_PUB = "/public/product-images"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64          # geçerli JPEG sihirli baytı
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


@pytest.fixture(autouse=True)
def _tmp_images_dir(tmp_path, monkeypatch):
    """Her test kendi dizininde çalışsın — repo'daki gerçek görsellere dokunma."""
    monkeypatch.setenv("PRODUCT_IMAGES_DIR", str(tmp_path / "product_images"))
    yield


def _up(client, name, data=JPEG):
    return client.post(f"{_API}/upload", headers=_HDR,
                       files=[("files", (name, io.BytesIO(data), "image/jpeg"))])


# ─── Saf birim: ad sözleşmesi ───────────────────────────────────────────────

def test_filename_contract():
    assert is_valid_name("MIN-DAYCRM-50.MAIN.jpg")
    assert is_valid_name("SKU_1.PT08.jpg")
    assert parse_name("MIN-DAYCRM-50.MAIN.jpg") == ("MIN-DAYCRM-50", "MAIN")
    assert parse_name("A.PT01.jpg") == ("A", "PT01")
    # Geçersizler
    for bad in ("MIN.MAIN.png", "MIN.MAIN.JPG", "MIN.PT09.jpg", "MIN.PT0.jpg",
                "MIN.MAIN.jpeg", "MIN MAIN.jpg", "MIN.MAIN.jpg.exe",
                ".MAIN.jpg", "MIN..MAIN.jpg", "TÜRKÇE.MAIN.jpg"):
        assert not is_valid_name(bad), bad


def test_normalize_name_fixes_common_variants():
    assert normalize_name("min-daycrm-50.main.jpg") == "min-daycrm-50.MAIN.jpg"
    assert normalize_name("MIN-DAYCRM-50.pt1.jpeg") == "MIN-DAYCRM-50.PT01.jpg"
    assert normalize_name("MIN-DAYCRM-50.PT-3.JPG") == "MIN-DAYCRM-50.PT03.jpg"
    # Türkçe karakter ve boşluk temizlenir
    assert normalize_name("ÜRÜN 50.MAIN.jpg") == "URUN-50.MAIN.jpg"
    # Dizin bileşeni atılır (traversal denemesi ada dönüşmez)
    assert normalize_name("../../etc/passwd.MAIN.jpg") == "passwd.MAIN.jpg"


def test_safe_path_blocks_traversal():
    assert safe_path("MIN.MAIN.jpg") is not None
    for bad in ("../secret.jpg", "..%2Fsecret.jpg", "/etc/passwd",
                "MIN.MAIN.jpg/../../x", "a/b.MAIN.jpg", "MIN.MAIN.jpg\x00.png"):
        assert safe_path(bad) is None, bad


def test_build_csv_shape():
    imgs = [{"name": "A.MAIN.jpg", "sku": "A", "slot": "MAIN", "size": 1, "mtime": 0},
            {"name": "A.PT01.jpg", "sku": "A", "slot": "PT01", "size": 1, "mtime": 0},
            {"name": "B.PT02.jpg", "sku": "B", "slot": "PT02", "size": 1, "mtime": 0}]
    csv_text = build_csv("https://x.tld", imgs)
    lines = csv_text.strip().split("\n")
    assert lines[0] == "sku,main_image_url," + ",".join(
        f"other_image_url{i}" for i in range(1, 9))
    assert lines[1].startswith("A,https://x.tld/public/product-images/A.MAIN.jpg,"
                               "https://x.tld/public/product-images/A.PT01.jpg,")
    # B'nin MAIN'i yok → boş; PT02 ikinci other sütununda
    b = lines[2].split(",")
    assert b[0] == "B" and b[1] == "" and b[2] == "" and b[3].endswith("B.PT02.jpg")


# ─── PUBLIC uç — Amazon'un gördüğü sözleşme ─────────────────────────────────

def test_public_serves_image_without_auth(client: TestClient, authed_client):
    """Yükle (auth'lu) → oturumsuz istemciyle indir (auth YOK)."""
    assert _up(authed_client, "MIN-DAYCRM-50.MAIN.jpg").status_code == 200
    anon = TestClient(client.app)                     # çerezsiz, taze istemci
    r = anon.get(f"{_PUB}/MIN-DAYCRM-50.MAIN.jpg")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(b"\xff\xd8\xff")
    # Amazon şartı: yönlendirme olmamalı, çerez set edilmemeli
    assert not r.history
    assert "set-cookie" not in {k.lower() for k in r.headers}


def test_public_head_matches_get_headers(client: TestClient, authed_client):
    """curl -I ile doğrulama — 200 + image/jpeg.

    HEAD ve GET AYNI handler'dan gelmeli (RFC 9110 §9.3.2): elle yazılmış ayrı
    bir HEAD ucu ETag/Last-Modified/Accept-Ranges'i kaçırıyordu.
    """
    _up(authed_client, "MIN-DAYCRM-50.MAIN.jpg")
    anon = TestClient(client.app)
    h = anon.head(f"{_PUB}/MIN-DAYCRM-50.MAIN.jpg")
    g = anon.get(f"{_PUB}/MIN-DAYCRM-50.MAIN.jpg")
    assert h.status_code == g.status_code == 200
    assert h.headers["content-type"] == "image/jpeg"
    for k in ("content-type", "content-length", "cache-control", "etag", "last-modified"):
        assert h.headers.get(k) == g.headers.get(k), f"HEAD/GET başlığı ayrışıyor: {k}"
    assert h.content == b""                       # gövdesiz


def test_cache_control_is_not_immutable(client: TestClient, authed_client):
    """Aynı ada yeni görsel yüklenebiliyor → `immutable` YANLIŞ olur.

    immutable ile aracı cache'ler bir yıl boyunca yeniden sormaz ve düzeltilmiş
    görsel yayılmazdı.
    """
    _up(authed_client, "A.MAIN.jpg")
    cc = TestClient(client.app).get(f"{_PUB}/A.MAIN.jpg").headers["cache-control"]
    assert "immutable" not in cc
    assert "must-revalidate" in cc


def test_public_unknown_and_malicious_names(client: TestClient):
    anon = TestClient(client.app)
    assert anon.get(f"{_PUB}/YOK.MAIN.jpg").status_code == 404
    # Beyaz liste dışı / traversal → 404, asla 200 ya da dosya sızıntısı
    for bad in ("../../../etc/passwd", "..%2f..%2fetc%2fpasswd",
                "MIN.MAIN.png", "MIN.MAIN.jpg.exe", "x.MAIN.JPG"):
        r = anon.get(f"{_PUB}/{bad}")
        assert r.status_code in (404, 400), (bad, r.status_code)
        assert b"root:" not in r.content


def test_public_is_read_only(client: TestClient, authed_client):
    """Public yol yalnız GET/HEAD — yazma fiilleri kabul edilmez."""
    _up(authed_client, "A.MAIN.jpg")
    anon = TestClient(client.app)
    for verb in ("post", "put", "delete"):
        r = getattr(anon, verb)(f"{_PUB}/A.MAIN.jpg", headers=_HDR)
        assert r.status_code in (403, 405), (verb, r.status_code)


def test_public_endpoint_is_exempt_from_rate_limit(client: TestClient, authed_client):
    """Amazon 60 ürün × 9 görsel çekiyor — global 300/dk sınırına TAKILMAMALI.

    Ölçüldü: aynı koşulda `/login` 320 istekte 429 yiyor (limit gerçekten
    uygulanıyor), bu uç yemiyor.  `@limiter.exempt` kaldırılırsa bu test kırılır.
    """
    _up(authed_client, "LOAD.MAIN.jpg")
    anon = TestClient(client.app)
    codes = set()
    for _ in range(310):                       # default_limits = 300/minute
        codes.add(anon.get(f"{_PUB}/LOAD.MAIN.jpg").status_code)
    assert codes == {200}, f"rate limit'e takıldı: {codes}"


def test_robots_allows_public_path(client: TestClient):
    r = client.get("/robots.txt")
    assert r.status_code == 200
    assert "Allow: /public/" in r.text


# ─── Yükleme — auth + doğrulama ─────────────────────────────────────────────

def test_upload_requires_permission(client: TestClient, labtech_client):
    """LabTech'in items.import yetkisi yok."""
    r = _up(labtech_client, "A.MAIN.jpg")
    assert r.status_code == 403
    anon = TestClient(client.app)
    assert _up(anon, "A.MAIN.jpg").status_code in (401, 403)


def _seg(marker: int, payload: bytes) -> bytes:
    """Geçerli bir JPEG segmenti kur — uzunluk alanı KENDİSİ dahil sayılır."""
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


def test_upload_strips_exif_and_trailing_payload(authed_client, client: TestClient):
    """EXIF/GPS public adresten okunmamalı; JPEG kuyruğundaki ek yük taşınmamalı."""
    jpeg = (b"\xff\xd8"                                        # SOI
            + _seg(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")   # APP0 → korunur
            + _seg(0xE1, b"Exif\x00\x00GPSGIZLIKONUM")          # APP1 → atılır
            + _seg(0xFE, b"gizli yorum")                        # COM  → atılır
            + b"\xff\xda\x00\x08\x01\x01\x00\x00?\x00"      # SOS
            + b"\x11" * 32 + b"\xff\xd9"                        # taranmış veri + EOI
            + b"<script>alert(1)</script>")                     # kuyruk yükü → kesilir
    r = _up(authed_client, "EXIF.MAIN.jpg", data=jpeg)
    assert r.status_code == 200 and r.json()["saved"], r.text
    body = TestClient(client.app).get(f"{_PUB}/EXIF.MAIN.jpg").content
    assert b"GPSGIZLIKONUM" not in body          # EXIF sıyrıldı
    assert b"gizli yorum" not in body            # yorum sıyrıldı
    assert b"<script>" not in body               # EOI sonrası kuyruk kesildi
    assert body.startswith(b"\xff\xd8\xff") and body.endswith(b"\xff\xd9")
    assert b"JFIF" in body                       # APP0 korundu (çözücü uyumu)
    assert b"\x11" * 32 in body                  # görüntü verisi bozulmadı


def test_strip_metadata_leaves_unparseable_file_intact():
    """Bozuk/alışılmadık JPEG'de veri AYNEN korunur — görseli bozmaktansa geç."""
    from core.product_images import strip_jpeg_metadata
    weird = b"\xff\xd8\xff\xe1\x00\x02"          # uzunluk alanı tutarsız
    assert strip_jpeg_metadata(weird) == weird
    assert strip_jpeg_metadata(b"not-a-jpeg") == b"not-a-jpeg"


def test_upload_rejects_non_jpeg_and_bad_names(authed_client):
    r = _up(authed_client, "A.MAIN.jpg", data=PNG)          # uzantı yalanı
    assert r.status_code == 200
    assert r.json()["saved"] == [] and "JPEG değil" in r.json()["rejected"][0]["reason"]

    r2 = _up(authed_client, "A.PT09.jpg")                   # geçersiz slot
    assert r2.json()["saved"] == [] and r2.json()["rejected"]


def test_upload_overwrites_keeping_url(authed_client):
    """Aynı ad tekrar yüklenince görsel değişir, adres AYNI kalır."""
    a = _up(authed_client, "A.MAIN.jpg").json()["saved"][0]
    b = _up(authed_client, "A.MAIN.jpg", data=JPEG + b"x").json()["saved"][0]
    assert a["url"] == b["url"] and b["replaced"] is True


def test_list_and_csv_export(authed_client):
    _up(authed_client, "MIN-DAYCRM-50.MAIN.jpg")
    _up(authed_client, "MIN-DAYCRM-50.PT01.jpg")
    _up(authed_client, "MIN-NIGHT-50.PT01.jpg")             # MAIN'i eksik
    d = authed_client.get(_API).json()
    assert d["count"] == 3 and d["sku_count"] == 2
    assert d["incomplete"] == ["MIN-NIGHT-50"]
    assert d["images"][0]["url"].endswith("/public/product-images/MIN-DAYCRM-50.MAIN.jpg")

    r = authed_client.get(f"{_API}/export.csv")
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    body = r.content.decode("utf-8-sig")
    assert "sku,main_image_url,other_image_url1" in body
    assert "MIN-DAYCRM-50.MAIN.jpg" in body


def test_delete(authed_client):
    _up(authed_client, "A.MAIN.jpg")
    assert authed_client.delete(f"{_API}/A.MAIN.jpg", headers=_HDR).status_code == 200
    assert authed_client.get(_API).json()["count"] == 0
    assert authed_client.delete(f"{_API}/A.MAIN.jpg", headers=_HDR).status_code == 404
    # Traversal ile silme denemesi
    assert authed_client.delete(f"{_API}/../../etc/passwd", headers=_HDR).status_code in (404, 400)


def test_page_renders(authed_client):
    r = authed_client.get("/urun-gorselleri")
    assert r.status_code == 200
    assert "Ürün Görselleri" in r.text
    assert r.text.count("<script") == r.text.count("</script>")


# ─── Walmart SDS PDF desteği ────────────────────────────────────────────────
#
# Walmart WFS, isChemical=Yes olan her ürün için public bir Güvenlik Bilgi
# Formu (SDS) PDF adresi ister: /public/product-images/<SKU>.SDS.pdf
# Aynı public uç hem Amazon JPEG'ini hem Walmart PDF'ini yayınlar.

PDF = b"%PDF-1.7\n1 0 obj<</Type/Catalog>>endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n"


def _up_pdf(client, name, data=PDF):
    return client.post(f"{_API}/upload", headers=_HDR,
                       files=[("files", (name, io.BytesIO(data), "application/pdf"))])


def test_sds_name_contract():
    """Slot ile uzantı ÇİFT olarak doğrulanır — çapraz eşleşme reddedilir."""
    from core.product_images import content_type_for, ext_of, is_sds
    assert is_valid_name("SER-SHMP-OILY-200.SDS.pdf")
    assert parse_name("SER-SHMP-OILY-200.SDS.pdf") == ("SER-SHMP-OILY-200", "SDS")
    assert ext_of("SER-SHMP-OILY-200.SDS.pdf") == "pdf"
    assert content_type_for("SER-SHMP-OILY-200.SDS.pdf") == "application/pdf"
    assert content_type_for("A.MAIN.jpg") == "image/jpeg"
    assert is_sds("A.SDS.pdf") and not is_sds("A.MAIN.jpg")
    # Çapraz eşleşme YASAK — public uç Content-Type'ı uzantıdan seçiyor
    for bad in ("A.MAIN.pdf", "A.SDS.jpg", "A.SDS.PDF", "A.SDS.pdf.exe",
                "../gizli.SDS.pdf", ".SDS.pdf"):
        assert not is_valid_name(bad), bad


def test_sds_upload_and_public_serve(authed_client):
    """SDS yüklenir, public uçtan application/pdf olarak BOZULMADAN döner."""
    r = _up_pdf(authed_client, "SER-SHMP-OILY-200.SDS.pdf")
    assert r.status_code == 200, r.text
    body = r.json()
    assert not body["rejected"], body["rejected"]
    assert body["saved"][0]["slot"] == "SDS"

    # PDF baytları AYNEN korunmalı — strip_jpeg_metadata PDF'e uygulanmamalı
    assert (images_dir() / "SER-SHMP-OILY-200.SDS.pdf").read_bytes() == PDF

    g = authed_client.get(f"{_PUB}/SER-SHMP-OILY-200.SDS.pdf")
    assert g.status_code == 200
    assert g.headers["content-type"].startswith("application/pdf")
    assert g.content == PDF
    # Güvenlik başlıkları — nginx location'ında da tekrarlanır (iki-yer kuralı)
    assert g.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in g.headers["content-security-policy"]
    assert g.headers["access-control-allow-origin"] == "*"


def test_sds_rejects_non_pdf_content(authed_client):
    """Adı .SDS.pdf ama içeriği JPEG olan dosya reddedilir."""
    r = _up_pdf(authed_client, "SAHTE.SDS.pdf", data=JPEG)
    assert r.status_code == 200
    assert not r.json()["saved"]
    assert "PDF değil" in r.json()["rejected"][0]["reason"]
    assert not (images_dir() / "SAHTE.SDS.pdf").exists()


def test_jpeg_slot_still_rejects_pdf_content(authed_client):
    """Gerileme kilidi: görsel slotuna PDF yüklenemez."""
    r = _up(authed_client, "MIN-X-50.MAIN.jpg", data=PDF)
    assert not r.json()["saved"]
    assert "JPEG değil" in r.json()["rejected"][0]["reason"]


def test_sds_excluded_from_amazon_csv(authed_client):
    """SDS, Amazon görsel CSV'sinin sütunlarına SIZMAMALI."""
    assert _up(authed_client, "MIN-X-50.MAIN.jpg").json()["saved"]
    assert _up_pdf(authed_client, "MIN-X-50.SDS.pdf").json()["saved"]
    csv_text = authed_client.get(f"{_API}/export.csv").text
    assert "MIN-X-50.MAIN.jpg" in csv_text
    assert ".SDS.pdf" not in csv_text            # asıl kilit


def test_sds_only_sku_absent_from_csv(authed_client):
    """Görseli olmayıp yalnız SDS'i olan SKU, Amazon CSV'sine BOŞ SATIR olarak
    girmemeli — flat-file'da görselsiz satır işe yaramaz."""
    assert _up_pdf(authed_client, "ONLY-SDS-2.SDS.pdf").json()["saved"]
    assert _up(authed_client, "HAS-IMG-1.MAIN.jpg").json()["saved"]
    csv_text = authed_client.get(f"{_API}/export.csv").text
    assert "HAS-IMG-1" in csv_text
    assert "ONLY-SDS-2" not in csv_text


def test_sds_only_sku_not_flagged_incomplete(authed_client):
    """Yalnız SDS'i olan SKU 'ana görseli eksik' listesine düşmemeli."""
    assert _up_pdf(authed_client, "ONLY-SDS-1.SDS.pdf").json()["saved"]
    d = authed_client.get(_API).json()
    assert d["sds_count"] == 1
    assert "ONLY-SDS-1" not in d["incomplete"]
