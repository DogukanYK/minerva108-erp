"""İmzalı creator'lar için iş birliği kaydı + sözleşme dosyası — 10.09.2026.

NEDEN
─────
`seed_creators_20260910.py` creator kartlarını açtı ama iki şey eksik kaldı:

  1. **İş birliği (`influencer_collab`)** — Kanban'ın kartı ve gönderimin
     bağlanacağı kayıt.  `store_key` zorunlu olduğu için hangi markanın
     ürününün kime gideceği kararlaşana kadar açılmamıştı.  Karar: dördü de
     **Minerva 108**.
  2. **Sözleşme PDF'i (`influencer_file` kind='agreement')** — ıslak/dijital
     imzalı nüshalar WhatsApp'ta duruyordu; belge sistemde olmadan iş birliği
     denetlenebilir değil.

NE YAPAR
────────
✔ Creator başına TEK aktif iş birliği açar (`INF-2026-NNNNN`), sözleşmeden
  gelen kullanım haklarını `usage_rights`'a, içerik teslim planını (§4)
  `deliverables_json`'a yazar.
✔ `guideline_ack_*` alanlarını imzalı sözleşmeye dayandırır: reklam ilişkisinin
  açıklanması yükümlülüğü (§6) sözleşmede kabul edilmiştir, panelde ayrıca
  onay kutusu beklemeye gerek yok.  Sürüm etiketi belgeyi işaret eder.
✔ Sözleşme PDF'ini `DRIVE_DIR`'e rastgele adla kopyalar (`save_media` kalıbı,
  0600) ve `influencer_file` satırı yazar.
✘ **GÖNDERİM (shipment) AÇMAZ** — ürün seçimi panelden yapılacak (patron
  kararı).  Stok bu script'te HİÇ hareket etmez.

AŞAMA SEÇİMİ
────────────
Adresi olan creator `kabul_edildi` ile başlar (gönderim panelden açılınca
motor kendisi `urun_hazirlaniyor`a taşır).  Adresi olmayan `adres_bekleniyor`
ile başlar — panoda ne beklendiği görünsün.

İDEMPOTENT
──────────
Creator'ın aktif iş birliği varsa yeni açılmaz; `agreement` dosyası varsa
tekrar yüklenmez.  İki kez çalıştırmak zarar vermez.

KULLANIM (prod'da)
──────────────────
    venv/bin/python scripts/seed_collabs_files_20260910.py            # kuru
    venv/bin/python scripts/seed_collabs_files_20260910.py --commit   # yaz

Sözleşme PDF'leri `--pdf-dir` ile verilir (varsayılan `_sozlesmeler/`).
"""
import secrets
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.drive import DRIVE_DIR                                  # noqa: E402
from core.influencer import next_collab_code                      # noqa: E402
from database import (InfluencerActivity, InfluencerAddress,      # noqa: E402
                      InfluencerCollab, InfluencerCollabStageLog,
                      InfluencerCreator, InfluencerFile, SessionLocal, User)

COMMIT = "--commit" in sys.argv
ACTOR = "sistem (iş birliği aktarımı 10.09.2026)"
OWNER_USERNAME = "dogukan"
STORE_KEY = "minerva"          # patron kararı 10.09.2026: dördü de Minerva 108
MARKET = "TR"

_pdf_dir = "_sozlesmeler"
if "--pdf-dir" in sys.argv:
    _pdf_dir = sys.argv[sys.argv.index("--pdf-dir") + 1]
PDF_DIR = Path(__file__).resolve().parent.parent / _pdf_dir

#: Sözleşme §4 — asgari içerik teslim planı (her creator için aynı).
DELIVERABLES = [
    {"platform": "instagram", "type": "unboxing", "qty": 1,
     "note": "Teslimden sonraki 7 gün — story / reel / tiktok"},
    {"platform": "instagram", "type": "rutin", "qty": 1,
     "note": "14-30 gün — gerçek kullanım deneyimi"},
]

COLLABS = [
    {"email": "merven517@gmail.com", "model": "barter",
     "usage_rights": "Organik + ücretli reklam 12 ay (09.09.2027)",
     "signed_on": date(2026, 9, 9), "pdf": "merve-koman.pdf",
     "notes": "Minerva 108 · BARTER ONLY — affiliate'e katılmadı (EK-3: Hayır), "
              "komisyon/link yok, satış atfı yapılamaz."},
    {"email": "elvanyagci@icloud.com", "model": "karma",
     "usage_rights": "Organik + ücretli reklam 6 ay (09.03.2027)",
     "signed_on": date(2026, 9, 9), "pdf": "elvan-yagci.pdf",
     "notes": "Minerva 108 · barter + affiliate (%10 / 30 gün / aylık / 1.000 TL)."},
    {"email": "gulsahcolak95@outlook.com", "model": "karma",
     "usage_rights": "Organik + ücretli reklam 6 ay (09.03.2027)",
     "signed_on": date(2026, 9, 9), "pdf": "gulsah-ozguler.pdf",
     "notes": "Minerva 108 · barter + affiliate (%10 / 30 gün / aylık / 1.000 TL)."},
    {"email": "kckomur@yahoo.com", "model": "karma",
     "usage_rights": "Organik + ücretli reklam 12 ay (08.09.2027)",
     "signed_on": date(2026, 9, 8), "pdf": "omur-kucuk.pdf",
     "notes": "Minerva 108 · barter + affiliate (%10 / 30 gün / aylık / 1.000 TL). "
              "90 günlük ambassador değerlendirmesi sözü verildi — 08.12.2026."},
]


def _next_code(db) -> str:
    year = datetime.utcnow().year
    prefix = f"INF-{year}-"
    seq = (db.query(InfluencerCollab)
           .filter(InfluencerCollab.code.like(prefix + "%")).count())
    while True:
        seq += 1
        code = next_collab_code(seq, year)
        if not db.query(InfluencerCollab.id).filter(InfluencerCollab.code == code).first():
            return code


def main() -> int:
    db = SessionLocal()
    collabs = files = skipped_c = skipped_f = 0
    warnings = []
    try:
        owner = db.query(User).filter(User.username == OWNER_USERNAME).first()
        print(f"Sorumlu: {owner.full_name if owner else '—'}   "
              f"Mağaza: {STORE_KEY}   PDF dizini: {PDF_DIR}\n")

        for rec in COLLABS:
            c = (db.query(InfluencerCreator)
                 .filter(InfluencerCreator.email == rec["email"]).first())
            if not c:
                print(f"✖ {rec['email']} — creator YOK, atlandı "
                      f"(önce seed_creators_20260910.py çalıştır)")
                warnings.append(f"{rec['email']} creator kaydı bulunamadı.")
                continue

            print(f"■ {c.full_name}  (creator id {c.id})")

            # ── İş birliği ──────────────────────────────────────────────
            existing = (db.query(InfluencerCollab)
                        .filter(InfluencerCollab.creator_id == c.id,
                                InfluencerCollab.is_active == True)      # noqa: E712
                        .first())
            if existing:
                print(f"    · iş birliği zaten var: {existing.code} "
                      f"({existing.stage}) — atlandı")
                skipped_c += 1
            else:
                has_addr = db.query(InfluencerAddress.id).filter(
                    InfluencerAddress.creator_id == c.id).first()
                stage = "kabul_edildi" if has_addr else "adres_bekleniyor"
                code = _next_code(db) if COMMIT else "INF-2026-?????"
                print(f"    + iş birliği {code} · {rec['model']:6} · {stage}")
                print(f"      haklar: {rec['usage_rights']}")
                if COMMIT:
                    now = datetime.utcnow()
                    ack = datetime.combine(rec["signed_on"], datetime.min.time())
                    col = InfluencerCollab(
                        code=code, creator_id=c.id, store_key=STORE_KEY, market=MARKET,
                        model=rec["model"],
                        tier_key_at_start=c.tier_override_key or c.tier_key,
                        stage=stage, stage_changed_at=now, offered_at=now, accepted_at=ack,
                        usage_rights=rec["usage_rights"], exclusivity_days=0,
                        guideline_ack_at=ack,
                        # String(20) — sözleşmenin §6 yükümlülüğü + imza tarihi.
                        # Uzun etiket sığmıyor; ayrıntı `notes` ve aktivitede.
                        guideline_version=f"sözleşme {rec['signed_on']:%d.%m.%Y}",
                        deliverables_json=__import__("json").dumps(
                            DELIVERABLES, ensure_ascii=False),
                        owner_user_id=owner.id if owner else None,
                        owner_name=owner.full_name if owner else None,
                        notes=rec["notes"], created_by=ACTOR,
                    )
                    db.add(col)
                    db.flush()
                    db.add(InfluencerCollabStageLog(
                        collab_id=col.id, from_stage=None, to_stage=stage, actor=ACTOR,
                        reason="İmzalı sözleşmeden aktarıldı"))
                    db.add(InfluencerActivity(
                        creator_id=c.id, collab_id=col.id, type="system",
                        subject="İş birliği açıldı", author_name=ACTOR,
                        body=f"{code} · Minerva 108 · {rec['notes']}"))
                collabs += 1

            # ── Sözleşme dosyası ────────────────────────────────────────
            has_file = (db.query(InfluencerFile.id)
                        .filter(InfluencerFile.entity == "creator",
                                InfluencerFile.entity_id == c.id,
                                InfluencerFile.kind == "agreement").first())
            if has_file:
                print("    · sözleşme dosyası zaten yüklü — atlandı")
                skipped_f += 1
                print()
                continue
            src = PDF_DIR / rec["pdf"]
            if not src.exists():
                print(f"    ✖ PDF bulunamadı: {src} — atlandı")
                warnings.append(f"{c.full_name}: {src} yok, sözleşme yüklenmedi.")
                print()
                continue
            head = src.open("rb").read(5)
            if head != b"%PDF-":
                print(f"    ✖ {src.name} PDF değil (sihirli bayt) — atlandı")
                warnings.append(f"{c.full_name}: {src.name} geçerli PDF değil.")
                print()
                continue
            size = src.stat().st_size
            print(f"    + sözleşme: {rec['pdf']}  ({size/1024/1024:.1f} MB)")
            if COMMIT:
                stored = secrets.token_hex(16) + ".pdf"
                dst = DRIVE_DIR / stored
                shutil.copyfile(src, dst)
                try:
                    dst.chmod(0o600)
                except OSError:
                    pass
                db.add(InfluencerFile(
                    entity="creator", entity_id=c.id, kind="agreement",
                    original_name=rec["pdf"], stored_name=stored,
                    size_bytes=size, content_type="application/pdf",
                    uploaded_by=ACTOR))
            files += 1
            print()

        if COMMIT:
            db.commit()
    except Exception as exc:                                   # pragma: no cover
        db.rollback()
        print(f"\n✖ HATA — hiçbir şey yazılmadı: {exc}")
        return 1
    finally:
        db.close()

    print("═" * 76)
    print(f"İş birliği: {collabs} yeni · {skipped_c} atlandı   |   "
          f"Sözleşme: {files} yeni · {skipped_f} atlandı")
    if warnings:
        print("\n⚠ UYARILAR:")
        for w in warnings:
            print(f"   · {w}")
    print("\nSIRADAKİ: gönderimler panelden — /influencer → Gönderimler.")
    print("Stok bu script'te HAREKET ETMEDİ; kargolamada düşer.")
    print("\n✓ YAZILDI." if COMMIT else "\nKURU ÇALIŞTIRMA — yazmak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
