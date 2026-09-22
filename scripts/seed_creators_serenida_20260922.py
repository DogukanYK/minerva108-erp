"""İkinci grup imzalı creator — Serenida hattı, 22.09.2026.

NEDEN
─────
Eylül ortasında altı sözleşme daha geldi.  Hepsi **v3 şablonuyla** imzalandı:
EK-1 matbu (%10 / 30 gün / Aylık / 1.000 TL) ve yeni 7. madde (İçerik Ön Onayı)
belgede duruyor — ilk turdaki "creator kendi komisyonunu yazıyor" sorunu kapandı.
Patron kararı: bu grup **Serenida** ürünleriyle çalışacak.

NE YAPAR
────────
✔ Creator kartı + platform hesapları + sözleşme özeti notu.
✔ Sözleşme PDF'ini `DRIVE_DIR`'e kopyalar, `influencer_file kind='agreement'`.
✔ Sözleşmesi eksiksiz olanlara `influencer_collab` açar (store_key='serenida').
✘ **T.C. kimlik numarası YAZMAZ** (KVKK — model bu alanı tutmuyor).
✘ ADRES YOK — altısının da teslimat adresi henüz alınmadı, iş birlikleri
  `adres_bekleniyor` ile başlar.
✘ Sözleşmesi kusurlu olana İŞ BİRLİĞİ AÇMAZ (`collab: False`) — kayıt yine
  açılır ki belge sistemde dursun, ama gönderim yoluna sokulmaz.

DENETİM BULGULARI (22.09.2026, 18 bağımsız okuma + çapraz doğrulama)
────────────────────────────────────────────────────────────────────
Her creator'ın `notes` alanı kendi kusurunu taşır.  Özet:
  · Melisa   — C'de profil fotoğrafı İŞARETSİZ → fotoğrafı kullanılamaz
  · Melike   — temiz (YouTube satırına handle yerine kanal adı yazmış)
  · Elif     — izin süresi HİÇ işaretlenmemiş; C'de 3/5; barter-only
  · Ece      — KUSURLU: A bölümü boş, B'deki tik iki kutu arasında,
               C'de tek çizik; iş birliği açılmadı
  · Cemilenaz— tarihi 2027 yazmış (2026 olmalı); gerisi temiz
  · Ayşegül  — "Diğer" işaretli ama süre yazılmamış; barter-only

İDEMPOTENT
──────────
Eşleşme anahtarı normalize e-posta.  Creator/hesap/dosya/iş birliği tek tek
kontrol edilir; ikinci çalıştırma hiçbir şeyi çoğaltmaz.

KULLANIM (prod'da)
──────────────────
    venv/bin/python scripts/seed_creators_serenida_20260922.py            # kuru
    venv/bin/python scripts/seed_creators_serenida_20260922.py --commit   # yaz
"""
import json
import secrets
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.drive import DRIVE_DIR, slugify                        # noqa: E402
from core.influencer import next_collab_code                     # noqa: E402
from database import (InfluencerAccount, InfluencerActivity,      # noqa: E402
                      InfluencerCollab, InfluencerCollabStageLog,
                      InfluencerCreator, InfluencerFile, SessionLocal, User)

COMMIT = "--commit" in sys.argv
ACTOR = "sistem (creator aktarımı 22.09.2026)"
OWNER_USERNAME = "dogukan"
STORE_KEY = "serenida"          # patron kararı 22.09.2026
MARKET = "TR"
PDF_DIR = Path(__file__).resolve().parent.parent / "_sozlesmeler"

#: Sözleşme §4 — asgari içerik teslim planı (v3'te de aynı).
DELIVERABLES = [
    {"platform": "instagram", "type": "unboxing", "qty": 1,
     "note": "Teslimden sonraki 7 gün — story / reel / tiktok"},
    {"platform": "instagram", "type": "rutin", "qty": 1,
     "note": "14-30 gün — gerçek kullanım deneyimi"},
]

CREATORS = [
    {
        "full_name": "Melisa İnci Trak",
        "email": "melisaincii377@gmail.com",
        "phone": "05536232353",
        "accounts": [("instagram", "kisacameli", True)],
        "model": "karma", "stage": "affiliate",
        "signed_on": date(2026, 9, 16), "collab": True,
        "usage_rights": "Organik + ücretli reklam 3 ay (16.12.2026)",
        "pdf": "melisa-inci-trak.pdf",
        "notes": (
            "Sözleşme: 16.09.2026 (v3 şablonu — EK-1 matbu, 7. madde İçerik Ön Onayı var).\n"
            "Model: barter + affiliate — EK-3'te affiliate katılımı EVET.\n"
            "Organik içerik kullanımı: EVET.\n"
            "Ücretli reklam: EVET — süre 3 AY, BİTİŞ 16.12.2026 (kısa; takvime al).\n"
            "⚠ KİMLİK UNSURU KISITI: C bölümünde **profil fotoğrafı İŞARETSİZ**. "
            "Ad-soyad, kullanıcı adı, görüntü/yüz ve ses onaylı (4/5). "
            "Profil fotoğrafı içerikte/reklamda KULLANILAMAZ.\n"
            "EK-3'te aynı kalem toplu 'Evet' işaretli — çelişki; esas olan "
            "8-C'deki tek tek seçimdir, kısıt öyle uygulanmalı.\n"
            "Sözleşmedeki 'Ek içerik' satırına elle 1 adet / 3 gün yazmış.\n"
            "ADRES ALINMADI."
        ),
    },
    {
        "full_name": "Melike Üstel",
        "email": "melikeustel37@gmail.com",
        "phone": "05536273737",
        "accounts": [("instagram", "melikeustel", True), ("tiktok", "mrsdeathl", False)],
        "model": "karma", "stage": "affiliate",
        "signed_on": date(2026, 9, 17), "collab": True,
        "usage_rights": "Organik + ücretli reklam 12 ay (17.09.2027)",
        "pdf": "melike-ustel.pdf",
        "notes": (
            "Sözleşme: 17.09.2026 (v3) — eksiksiz.\n"
            "Model: barter + affiliate (EK-3 affiliate EVET).\n"
            "Organik: EVET. Ücretli reklam: EVET — 12 ay, BİTİŞ 17.09.2027.\n"
            "Kimlik unsurları 5/5, KVKK 3/3 onaylı.\n"
            "NOT: YouTube satırına kanal handle'ı yerine görünen ad ('Melike Üstel') "
            "yazmış — YouTube metriği çekilecekse kanal adresi ayrıca istenmeli.\n"
            "Program alanına elle 'The Minevra 108 Society' yazmış (yazım hatası, "
            "alan zaten matbu dolu — hükme etkisi yok).\n"
            "ADRES ALINMADI."
        ),
    },
    {
        "full_name": "Elif Çubukcu",
        "email": "elifervancubukcu@hotmail.com",
        "phone": "05070564483",
        "accounts": [("instagram", "elifin.gunluguu", True)],
        "model": "barter", "stage": "basvurdu",
        "signed_on": date(2026, 9, 15), "collab": True,
        "usage_rights": "Organik + ücretli reklam — SÜRE TANIMSIZ",
        "pdf": "elif-cubukcu.pdf",
        "notes": (
            "Sözleşme: 15.09.2026 (v3).\n"
            "Model: YALNIZ BARTER — EK-3'te affiliate katılımı HAYIR. Komisyon/link "
            "yok, satış atfı yapılamaz.\n"
            "Organik: EVET.\n"
            "⚠ ÜCRETLİ REKLAM SÜRESİ TANIMSIZ: 3 ay / 6 ay / 12 ay / Diğer "
            "seçeneklerinin HİÇBİRİ işaretlenmemiş. Süre teyit edilmeden içeriği "
            "reklam kreatifinde KULLANMA.\n"
            "⚠ KİMLİK UNSURU KISITI: C'de 5 kalemden yalnız 3'ü işaretli — "
            "sosyal medya kullanıcı adı, görüntü/yüz, ses. **Ad-soyad ve profil "
            "fotoğrafı İŞARETSİZ**, kullanılamaz.\n"
            "ADRES ALINMADI."
        ),
    },
    {
        "full_name": "Ece Duran Sertel",
        "email": "eceylearen@gmail.com",
        "phone": "05301412370",          # sözleşmede baştaki 0 eksik yazılmış
        "accounts": [("instagram", "eceylearen", True)],
        "model": "karma", "stage": "basvurdu",
        "signed_on": date(2026, 9, 17), "collab": False,   # KUSURLU — iş birliği açılmaz
        "usage_rights": None,
        "pdf": "ece-duran-sertel.pdf",
        "notes": (
            "⛔ SÖZLEŞME KUSURLU — YENİDEN İMZALANMALI. İş birliği bilerek AÇILMADI.\n"
            "Denetim (22.09.2026, çapraz doğrulandı):\n"
            "  1) 'A. Organik İçerik Kullanım Onayı' — EVET/HAYIR İKİ KUTU DA BOŞ. "
            "Organik yeniden paylaşım hakkı dayanaksız.\n"
            "  2) B bölümündeki tek tik EVET ile HAYIR kutuları ARASINDA duruyor, "
            "hangisi seçildiği okunmuyor; buna rağmen izin süresi 12 ay işaretli. "
            "'Yalnızca organik' seçildiyse 12 ay çelişkili.\n"
            "  3) C bölümünde beş kalemin üstünden geçen TEK BİR ÇİZİK var — beş "
            "ayrı işaret değil; hangi kimlik unsuruna izin verildiği belirsiz.\n"
            "  4) Instagram/TikTok/YouTube satırı BOŞ.\n"
            "  5) İmza, kendi hücresinin dışına, Marka Yetkilisi satırlarının "
            "üstüne taşmış.\n"
            "Adını 'Ece sertel' yazmış; telefonu 10 hane (başta 0 yok), "
            "05301412370 olarak düzeltildi.\n"
            "NOT: bu kişi hunide zaten 'Ece' olarak vardı (telefon eşleşiyor); "
            "ona Serenida HA serum + yüz kremi düşünülmüştü.\n"
            "ADRES ALINMADI."
        ),
    },
    {
        "full_name": "Cemilenaz Acerkol",
        "email": "ajanskare87@gmail.com",
        "phone": "05380722855",
        "accounts": [("instagram", "naz.acerkolll", True)],
        "model": "karma", "stage": "affiliate",
        "signed_on": date(2026, 9, 16), "collab": True,
        "usage_rights": "Organik + ücretli reklam 12 ay (16.09.2027)",
        "pdf": "cemilenaz-acerkol.pdf",
        "notes": (
            "Sözleşme: 16.09.2026 (v3) — onay alanları eksiksiz.\n"
            "Model: barter + affiliate (EK-3 affiliate EVET).\n"
            "Organik: EVET. Ücretli reklam: EVET — 12 ay.\n"
            "Kimlik unsurları 5/5, KVKK 3/3, EK-3 5 satır Evet.\n"
            "⚠ TARİH HATASI: hem 1. sayfaya hem imza bölümüne **16/09/2027** "
            "yazmış (son hane net bir 7). Doğrusu 2026. Önemli: ücretli reklam "
            "süresi ve içeriğin 90 gün yayında kalma şartı bu tarihten sayılıyor. "
            "Düzelttirilmeli — sistemde 16.09.2026 kabul edildi.\n"
            "E-posta sözleşmede büyük harfle başlıyor (Ajanskare87@...), "
            "normalize edildi. 2. sayfada soyadı küçük harfle yazılmış.\n"
            "ADRES ALINMADI."
        ),
    },
    {
        "full_name": "Ayşegül Örnek",
        "email": "aysegulornek09@gmail.com",
        "phone": "05355702218",
        "accounts": [("instagram", "aycegulornek", True)],
        "model": "barter", "stage": "basvurdu",
        "signed_on": date(2026, 9, 15), "collab": True,
        "usage_rights": "Organik + ücretli reklam — SÜRE TANIMSIZ",
        "pdf": "aysegul-ornek.pdf",
        "notes": (
            "Sözleşme: 15.09.2026 (v3).\n"
            "Model: YALNIZ BARTER — EK-3'te affiliate katılımı HAYIR.\n"
            "Organik: EVET. Kimlik unsurları 5/5.\n"
            "⚠ ÜCRETLİ REKLAM SÜRESİ TANIMSIZ: 'Diğer' kutusu işaretlenmiş ama "
            "yanındaki çizgiye süre YAZILMAMIŞ; 3/6/12 ay da işaretsiz. Süre "
            "teyit edilmeden içeriği reklam kreatifinde KULLANMA.\n"
            "⚠ KULLANICI ADI ŞÜPHELİ: sözleşmede 'aycegulornek' (c ile) yazıyor "
            "ama e-postası 'aysegulornek09@' (s ile) ve adı Ayşegül. Gerçek "
            "Instagram hesabı teyit edilmeli — yanlışsa affiliate linki de "
            "yanlış kurulur.\n"
            "EK-2'de ticari elektronik ileti kutusundaki iz kutunun içine tam "
            "girmiyor; opsiyonel olduğu için engel değil, ama e-posta "
            "pazarlamasına eklemeden önce teyit iste.\n"
            "ADRES ALINMADI."
        ),
    },
]


def _unique_slug(db, base: str) -> str:
    base = (slugify(base) or "creator")[:48]
    slug, n = base, 1
    while db.query(InfluencerCreator.id).filter(InfluencerCreator.slug == slug).first():
        n += 1
        slug = f"{base}-{n}"
    return slug


def _next_code(db) -> str:
    year = datetime.utcnow().year
    prefix = f"INF-{year}-"
    seq = db.query(InfluencerCollab).filter(InfluencerCollab.code.like(prefix + "%")).count()
    while True:
        seq += 1
        code = next_collab_code(seq, year)
        if not db.query(InfluencerCollab.id).filter(InfluencerCollab.code == code).first():
            return code


def main() -> int:
    db = SessionLocal()
    yeni = hesap = dosya = isbirligi = atlanan = 0
    uyarilar = []
    try:
        owner = db.query(User).filter(User.username == OWNER_USERNAME).first()
        print(f"Sorumlu: {owner.full_name if owner else '—'} · Mağaza: {STORE_KEY} "
              f"· PDF: {PDF_DIR}\n")

        for rec in CREATORS:
            email = rec["email"].strip().lower()
            c = db.query(InfluencerCreator).filter(InfluencerCreator.email == email).first()
            print(f"■ {rec['full_name']}")

            # ── Creator ─────────────────────────────────────────────────
            if c:
                print(f"    · zaten kayıtlı (id {c.id}) — atlandı")
                atlanan += 1
            else:
                print(f"    + {email:34} {rec['model']:6} · {rec['stage']}")
                if COMMIT:
                    c = InfluencerCreator(
                        slug=_unique_slug(db, rec["full_name"]),
                        full_name=rec["full_name"], email=email, phone=rec["phone"],
                        country="TR", language="tr",
                        accepted_model=rec["model"], relationship_stage=rec["stage"],
                        owner_user_id=owner.id if owner else None,
                        owner_name=owner.full_name if owner else None,
                        source="manual", notes=rec["notes"], created_by=ACTOR)
                    db.add(c)
                    db.flush()
                    db.add(InfluencerActivity(
                        creator_id=c.id, type="system", author_name=ACTOR,
                        subject="Sözleşme imzalandı ve sisteme aktarıldı",
                        body=rec["notes"]))
                yeni += 1

            # ── Hesaplar ────────────────────────────────────────────────
            for platform, handle, birincil in rec["accounts"]:
                dup = (db.query(InfluencerAccount)
                       .filter(InfluencerAccount.platform == platform,
                               InfluencerAccount.handle == handle).first())
                if dup:
                    ayni = c is not None and dup.creator_id == c.id
                    print(f"    · {platform:9} @{handle:20} zaten kayıtlı"
                          + ("" if ayni else "  ⚠ BAŞKA CREATOR'DA"))
                    if not ayni and c is not None:
                        uyarilar.append(f"@{handle} ({platform}) başka creator'a bağlı "
                                        f"(id {dup.creator_id}).")
                    continue
                print(f"    + {platform:9} @{handle:20}" + ("  [birincil]" if birincil else ""))
                if COMMIT and c is not None:
                    db.add(InfluencerAccount(creator_id=c.id, platform=platform,
                                             handle=handle, is_primary=birincil,
                                             metrics_source="manual"))
                hesap += 1

            # ── Sözleşme dosyası ────────────────────────────────────────
            var = (c is not None and db.query(InfluencerFile.id)
                   .filter(InfluencerFile.entity == "creator",
                           InfluencerFile.entity_id == c.id,
                           InfluencerFile.kind == "agreement").first())
            if var:
                print("    · sözleşme dosyası zaten yüklü")
            else:
                src = PDF_DIR / rec["pdf"]
                if not src.exists():
                    print(f"    ✖ PDF yok: {src.name}")
                    uyarilar.append(f"{rec['full_name']}: {src.name} bulunamadı.")
                elif src.open("rb").read(5) != b"%PDF-":
                    print(f"    ✖ {src.name} geçerli PDF değil")
                    uyarilar.append(f"{rec['full_name']}: {src.name} PDF değil.")
                else:
                    boyut = src.stat().st_size
                    print(f"    + sözleşme: {rec['pdf']} ({boyut/1048576:.1f} MB)")
                    if COMMIT and c is not None:
                        stored = secrets.token_hex(16) + ".pdf"
                        shutil.copyfile(src, DRIVE_DIR / stored)
                        try:
                            (DRIVE_DIR / stored).chmod(0o600)
                        except OSError:
                            pass
                        db.add(InfluencerFile(
                            entity="creator", entity_id=c.id, kind="agreement",
                            original_name=rec["pdf"], stored_name=stored,
                            size_bytes=boyut, content_type="application/pdf",
                            uploaded_by=ACTOR))
                    dosya += 1

            # ── İş birliği ──────────────────────────────────────────────
            if not rec["collab"]:
                print("    ⛔ iş birliği AÇILMADI — sözleşme kusurlu, yeniden imza bekliyor")
                print()
                continue
            mevcut = (c is not None and db.query(InfluencerCollab)
                      .filter(InfluencerCollab.creator_id == c.id,
                              InfluencerCollab.is_active == True).first())    # noqa: E712
            if mevcut:
                print(f"    · iş birliği zaten var: {mevcut.code}")
            else:
                kod = _next_code(db) if COMMIT else "INF-2026-?????"
                print(f"    + iş birliği {kod} · {STORE_KEY} · adres_bekleniyor")
                print(f"      haklar: {rec['usage_rights']}")
                if COMMIT and c is not None:
                    now = datetime.utcnow()
                    ack = datetime.combine(rec["signed_on"], datetime.min.time())
                    col = InfluencerCollab(
                        code=kod, creator_id=c.id, store_key=STORE_KEY, market=MARKET,
                        model=rec["model"], tier_key_at_start=c.tier_key,
                        stage="adres_bekleniyor", stage_changed_at=now,
                        offered_at=now, accepted_at=ack,
                        usage_rights=(rec["usage_rights"] or "")[:60] or None,
                        exclusivity_days=0, guideline_ack_at=ack,
                        guideline_version=f"sözleşme {rec['signed_on']:%d.%m.%Y}",
                        deliverables_json=json.dumps(DELIVERABLES, ensure_ascii=False),
                        owner_user_id=owner.id if owner else None,
                        owner_name=owner.full_name if owner else None,
                        notes=f"Serenida · {rec['usage_rights']}", created_by=ACTOR)
                    db.add(col)
                    db.flush()
                    db.add(InfluencerCollabStageLog(
                        collab_id=col.id, from_stage=None, to_stage="adres_bekleniyor",
                        actor=ACTOR, reason="İmzalı sözleşmeden aktarıldı"))
                isbirligi += 1
            print()

        if COMMIT:
            db.commit()
    except Exception as exc:                                   # pragma: no cover
        db.rollback()
        print(f"\n✖ HATA — geri alındı: {exc}")
        import traceback
        traceback.print_exc()
        return 1
    finally:
        db.close()

    print("═" * 76)
    print(f"Creator: {yeni} yeni · {atlanan} atlandı  |  Hesap: {hesap}  |  "
          f"Sözleşme: {dosya}  |  İş birliği: {isbirligi}")
    if uyarilar:
        print("\n⚠ UYARILAR:")
        for u in uyarilar:
            print(f"   · {u}")
    print("\nSIRADAKİ:")
    print("   1. Altısının da TESLİMAT ADRESİ alınmalı (hepsi adres_bekleniyor).")
    print("   2. Ece Duran Sertel — sözleşme yeniden imzalanmalı, iş birliği yok.")
    print("   3. Elif + Ayşegül — ücretli reklam süresi teyit edilmeli.")
    print("   4. Cemilenaz — sözleşme tarihi 2027 yazılmış, düzelttirilmeli.")
    print("   5. Ayşegül — Instagram kullanıcı adı teyidi (aycegulornek / aysegulornek).")
    print("\n✓ YAZILDI." if COMMIT else "\nKURU ÇALIŞTIRMA — yazmak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
