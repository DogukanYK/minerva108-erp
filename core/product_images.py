# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Amazon ürün görselleri — dosya adı sözleşmesi + güvenli yol çözümü + CSV.

Amazon flat-file'ında `main_image_url` ve `other_image_url1..8` alanları var;
Amazon bu adreslere KENDİ sunucusundan istek atıp görseli indiriyor.  Bu yüzden
görseller `/public/product-images/<ad>` altında **kimlik doğrulaması olmadan**
servis edilir (bkz. routers/product_images.py).

Dosya adı TEK KAYNAKTIR — SKU ve görsel sırası addan okunur, DB'de eşleme
tutulmaz.  Sistemde `Item.sku` alanı boş (Amazon SKU'ları IMS'te tanımlı değil),
bu yüzden adı otorite kabul etmek hem doğru hem de yeni ürün eklemeyi
"dosyayı yükle, bitti"ye indirger.

    <SKU>.MAIN.jpg     → main_image_url
    <SKU>.PT01.jpg     → other_image_url1
    …
    <SKU>.PT08.jpg     → other_image_url8

GÜVENLİK — burası public bir yolun girdi doğrulayıcısıdır:
  • Ad, `FILENAME_RE` beyaz listesine UYMAK ZORUNDA (harf/rakam/nokta/tire/alt
    çizgi).  Bu, `..`, `/`, `\\`, NUL ve URL-encoded varyantlarını baştan eler.
  • `safe_path()` ayrıca resolve edip kök dizin içinde kaldığını doğrular —
    symlink veya normalizasyon sürprizlerine karşı ikinci kat.
  • Yalnız JPEG kabul edilir; yüklemede sihirli bayt kontrolü yapılır
    (uzantı yalanı işe yaramaz).
"""
import csv
import io
import os
import re
from pathlib import Path
from typing import List, Optional, Tuple

# Amazon: 1 ana + 8 yardımcı görsel
SLOTS = ("MAIN",) + tuple(f"PT{i:02d}" for i in range(1, 9))
MAX_UPLOAD_BYTES = 15 * 1024 * 1024          # 1600x1600 JPEG ~300-600 KB; bol pay
JPEG_MAGIC = b"\xff\xd8\xff"

# SKU: harf/rakam/tire/alt çizgi, 1-80 karakter.  Nokta SKU'da YASAK — ad
# ayrıştırması noktaya dayanıyor (SKU.SLOT.jpg).
FILENAME_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9_-]{0,79})\.(MAIN|PT0[1-8])\.jpg$")

# Türkçe harf → ASCII (dosya adı Amazon tarafında sorun çıkarmasın)
_TR = str.maketrans({
    "ç": "c", "Ç": "C", "ğ": "g", "Ğ": "G", "ı": "i", "İ": "I",
    "ö": "o", "Ö": "O", "ş": "s", "Ş": "S", "ü": "u", "Ü": "U",
})


def images_dir() -> Path:
    """Görsellerin durduğu dizin.  Env ile taşınabilir; yoksa oluşturulur."""
    d = Path(os.getenv("PRODUCT_IMAGES_DIR", "product_images")).resolve()
    d.mkdir(parents=True, exist_ok=True)
    return d


def normalize_name(original: str) -> str:
    """Yüklenen dosya adını sözleşmeye yaklaştır (SKU'yu BOZMADAN).

    Yapılanlar: dizin bileşenlerini at · Türkçe harfleri ASCII'ye çevir ·
    boşluk ve geçersiz karakterleri tireye indir · `.jpeg`→`.jpg` ·
    slot'u büyük harfe çevir (`main`→`MAIN`, `pt1`/`pt01`→`PT01`).
    Sonuç yine `FILENAME_RE` ile DOĞRULANMALIDIR — bu fonksiyon sadece
    yaygın yazım farklarını düzeltir, güvenlik kontrolü değildir.
    """
    name = os.path.basename((original or "").strip()).translate(_TR)
    name = re.sub(r"\.jpeg$", ".jpg", name, flags=re.I)
    parts = name.split(".")
    if len(parts) < 3:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    ext = parts[-1].lower()
    slot = parts[-2].upper()
    sku = ".".join(parts[:-2])
    m = re.fullmatch(r"PT-?(\d{1,2})", slot)
    if m:
        slot = f"PT{int(m.group(1)):02d}"
    sku = re.sub(r"[^A-Za-z0-9_-]+", "-", sku).strip("-")
    return f"{sku}.{slot}.{ext}"


def parse_name(name: str) -> Optional[Tuple[str, str]]:
    """'MIN-DAYCRM-50.MAIN.jpg' → ('MIN-DAYCRM-50', 'MAIN').  Uymuyorsa None."""
    m = FILENAME_RE.fullmatch(name or "")
    return (m.group(1), m.group(2)) if m else None


def is_valid_name(name: str) -> bool:
    return FILENAME_RE.fullmatch(name or "") is not None


def safe_path(name: str) -> Optional[Path]:
    """Ad → tam yol.  Beyaz listeye uymuyorsa ya da kök dışına çıkıyorsa None.

    İki kat koruma: (1) regex — `..`/ayraç/NUL zaten geçemez, (2) resolve
    sonrası kök kontrolü — symlink veya beklenmedik normalizasyon yakalanır.
    """
    if not is_valid_name(name):
        return None
    root = images_dir()
    p = (root / name).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    return p


def is_jpeg(head: bytes) -> bool:
    """Sihirli bayt kontrolü — uzantı yalanını eler."""
    return bool(head) and head.startswith(JPEG_MAGIC)


# JPEG'de atılacak metadata segmentleri:
#   APP1 (EXIF/XMP — GPS, cihaz seri no, yazılım, gömülü orijinal küçük resim)
#   APP2..APP15 (ICC dışı üretici blokları), COM (yorum)
# APP0 (JFIF) KORUNUR — bazı eski çözücüler onu bekler.
# ICC profili APP2'dedir; renk doğruluğu için o da korunur.
_STRIP_MARKERS = {0xE1} | set(range(0xE3, 0xF0)) | {0xFE}
_KEEP_APP2_ICC = b"ICC_PROFILE\x00"


def strip_jpeg_metadata(data: bytes) -> bytes:
    """EXIF/XMP/yorum segmentlerini at — görüntü verisine DOKUNMADAN.

    Ürün fotoğrafları telefonla çekildiğinde JPEG içinde GPS koordinatı (lab
    adresi), cihaz seri numarası ve rötuş öncesi gömülü küçük resim kalır.  Bu
    dosyalar kimlik doğrulaması olmayan bir adresten yayınlandığı için metadata
    da herkese açık olur.

    Yeniden kodlama YAPILMAZ (kalite kaybı ve Pillow bağımlılığı olmaz);
    yalnız segment başlıkları gezilip istenmeyenler atlanır.  Ayrıca
    EOI (görüntü sonu) işaretinden SONRAKİ veri kesilir — JPEG'in kuyruğuna
    iliştirilmiş polyglot/ek yük böylece taşınmaz.

    Ayrıştırma başarısız olursa (bozuk/alışılmadık yapı) veri AYNEN döner —
    görseli bozmaktansa metadata'yı taşımak yeğdir; çağıran zaten sihirli bayt
    kontrolü yapmıştır.
    """
    if not data.startswith(JPEG_MAGIC):
        return data
    out = bytearray(data[:2])                     # SOI
    i = 2
    n = len(data)
    try:
        while i < n - 1:
            if data[i] != 0xFF:
                return data                        # hizalama bozuk → dokunma
            marker = data[i + 1]
            if marker == 0xD8:                     # gömülü SOI (beklenmez)
                return data
            if marker == 0xDA:                     # SOS — bundan sonrası taranmış veri
                end = data.rfind(b"\xff\xd9")      # EOI
                out += data[i:end + 2] if end > i else data[i:]
                return bytes(out)
            if 0xD0 <= marker <= 0xD9:             # bağımsız işaretler (uzunluksuz)
                out += data[i:i + 2]
                i += 2
                continue
            seg_len = int.from_bytes(data[i + 2:i + 4], "big")
            if seg_len < 2 or i + 2 + seg_len > n:
                return data                        # bozuk/taşan uzunluk → dokunma
            seg = data[i:i + 2 + seg_len]
            keep = marker not in _STRIP_MARKERS
            if marker == 0xE2 and not seg[4:].startswith(_KEEP_APP2_ICC):
                keep = False                       # ICC dışı APP2 → at
            if keep:
                out += seg
            i += 2 + seg_len
    except (IndexError, ValueError):
        return data
    # Buraya düşmek = SOS (görüntü verisi) hiç görülmedi → dosya alışılmadık.
    # Biriktirdiğimiz `out` görüntü taşımıyor; orijinali döndürmek tek güvenli
    # davranış (metadata taşımak, görseli boşaltmaktan iyidir).
    return data


def list_images() -> List[dict]:
    """Dizindeki geçerli görseller — SKU/slot çözümlenmiş, ada göre sıralı."""
    root = images_dir()
    out = []
    for f in sorted(root.iterdir()):
        if not f.is_file():
            continue
        parsed = parse_name(f.name)
        if not parsed:
            continue                      # sözleşmeye uymayan dosyayı listeleme
        sku, slot = parsed
        st = f.stat()
        out.append({"name": f.name, "sku": sku, "slot": slot,
                    "size": st.st_size, "mtime": st.st_mtime})
    return out


def group_by_sku(images: List[dict]) -> dict:
    """{sku: {slot: ad}} — CSV ve UI gruplaması için."""
    g: dict = {}
    for im in images:
        g.setdefault(im["sku"], {})[im["slot"]] = im["name"]
    return g


def public_url(base_url: str, name: str) -> str:
    return f"{base_url.rstrip('/')}/public/product-images/{name}"


def build_csv(base_url: str, images: List[dict]) -> str:
    """Amazon flat-file yükleme CSV'si.

    Başlık: sku,main_image_url,other_image_url1..8.  Bir SKU'nun eksik slotu
    boş bırakılır (Amazon boş alanı yok sayar).  Satırlar SKU'ya göre sıralı.
    """
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(["sku", "main_image_url"] + [f"other_image_url{i}" for i in range(1, 9)])
    for sku, slots in sorted(group_by_sku(images).items()):
        row = [sku]
        row.append(public_url(base_url, slots["MAIN"]) if "MAIN" in slots else "")
        for i in range(1, 9):
            key = f"PT{i:02d}"
            row.append(public_url(base_url, slots[key]) if key in slots else "")
        w.writerow(row)
    return buf.getvalue()
