"""İlk üç imzalı creator'ı influencer modülüne aktar — 10.09.2026.

NEDEN
─────
The Minerva 108 Society'nin ilk imzalı sözleşmeleri WhatsApp/Kommo üzerinden
geldi.  Sözleşmeler tek tek denetlendi (09.09.2026); üçü de eksiksiz.  Bu
script onları `influencer_creator` + `influencer_account` + `influencer_address`
kayıtlarına çevirir ki panel, kanban, gönderim ve UpPromote eşlemesi IMS
üzerinden yürüsün — Kommo sohbetinde kaybolmasın.

NE YAPAR / NE YAPMAZ
────────────────────
✔ Creator kartı + platform hesapları + varsayılan gönderim adresi açar.
✔ Sözleşme özetini `notes` alanına ve bir `influencer_activity` satırına yazar
  (paid media izin süresi ve BİTİŞ TARİHİ dahil — reklam kreatifinde kullanım
  bu tarihte durur).
✘ **T.C. kimlik numarası YAZMAZ.**  Model bilinçli olarak bu alanı tutmuyor
  (KVKK veri minimizasyonu, `InfluencerCreator` docstring'i); numaralar yalnız
  ıslak sözleşmede kalır.
✘ `influencer_collab` AÇMAZ — iş birliği kaydı `store_key` (minerva /
  serenida / evanira) ister; hangi markanın ürünü kime gidecek henüz
  kararlaşmadı.  Karar verilince panelden ya da ayrı bir turda açılır.
✘ `influencer_affiliate` AÇMAZ — UpPromote kaydı henüz yapılmadı; `sca_ref`
  ve affiliate linki oradan gelecek.  Eşleme o zaman kurulur.
✘ Takipçi sayısı GİRMEZ (elde yok) → `tier_key` boş kalır.  Sayılar panelden
  girilince kademe kendiliğinden hesaplanır.

İDEMPOTENT
──────────
Eşleşme anahtarı normalize e-postadır.  Kayıt varsa creator ATLANIR; hesap ya
da adres eksikse yalnız o eklenir.  İki kez çalıştırmak zarar vermez.

KULLANIM (prod'da)
──────────────────
    venv/bin/python scripts/seed_creators_20260910.py            # kuru
    venv/bin/python scripts/seed_creators_20260910.py --commit   # yaz
"""
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.drive import slugify                                   # noqa: E402
from database import (InfluencerAccount, InfluencerActivity,      # noqa: E402
                      InfluencerAddress, InfluencerCreator, SessionLocal, User)

COMMIT = "--commit" in sys.argv
ACTOR = "sistem (creator aktarımı 10.09.2026)"
OWNER_USERNAME = "dogukan"          # program sorumlusu (plan kararı)

# ── İmzalı sözleşmelerden çıkarılan kayıtlar ────────────────────────────────
# `notes` sözleşmenin operasyonel özeti: neyin izni var, ne zamana kadar.
CREATORS = [
    {
        "full_name": "Merve Koman",
        "email": "merven517@gmail.com",
        "phone": "05435444792",
        "city": "Gaziantep",
        "accepted_model": "barter",          # affiliate'e KATILMADI (EK-3: Hayır)
        "relationship_stage": "basvurdu",
        "accounts": [("instagram", "dijitalannemerve", True)],
        "address": {
            "recipient_name": "Merve Koman",
            "line1": "Mavikent Mah. 135003 Sok. Günsev 9 Sitesi H Blok Daire: 13",
            "district": "Şahinbey",
            "city": "Gaziantep",
            "phone": "05435444792",
        },
        "notes": (
            "Sözleşme: 09.09.2026 (2. nüsha — 1. nüshada organik onay kutusu boştu "
            "ve imza tarihi 2021 yazılmıştı, ikisi de düzeltildi).\n"
            "Model: YALNIZ BARTER. EK-3'te 'Affiliate programına katılım = HAYIR' "
            "işaretli, EK-1 boş → komisyon/link YOK. Satış atfı yapılamaz.\n"
            "Organik içerik kullanımı: EVET.\n"
            "Ücretli reklam (paid media): EVET — süre 12 ay, BİTİŞ 09.09.2027.\n"
            "Kimlik unsurları: ad-soyad, kullanıcı adı, profil fotoğrafı, "
            "görüntü/yüz, ses — 5/5 onaylı.\n"
            "KVKK: aydınlatma + açık rıza + ticari elektronik ileti — 3/3 onaylı.\n"
            "AÇIK KONU: markalar için yapay zekâ içerik üreticiliği teklif etti "
            "(09.09.2026), ayrı bir iş kalemi olarak değerlendirilecek.\n"
            "Adres WhatsApp üzerinden alındı (claim-link ile doğrulanmadı)."
        ),
    },
    {
        "full_name": "Elvan Yağcı",
        "email": "elvanyagci@icloud.com",
        "phone": "05075825698",
        "city": "Zonguldak",
        "accepted_model": "karma",           # barter + affiliate link
        "relationship_stage": "affiliate",
        "accounts": [("instagram", "elvanyagci", True), ("tiktok", "elvanyagci", False)],
        "address": {
            "recipient_name": "Elvan Yağcı",
            "line1": "Uzunmehmet Mah. Obakent Sitesi D Blok Çavdarlı Sokak No: 52/5 Kat: 2",
            "district": "Ereğli",
            "city": "Zonguldak",
            "phone": "05075825698",
        },
        "notes": (
            "Sözleşme: 09.09.2026 — eksiksiz.\n"
            "Model: barter + affiliate. EK-1 standart koşullarla doldurulmuş ve "
            "paraflanmış: %10 komisyon / 30 gün attribution / aylık ödeme / "
            "1.000 TL minimum eşik.\n"
            "Organik içerik kullanımı: EVET.\n"
            "Ücretli reklam (paid media): EVET — süre 6 ay, BİTİŞ 09.03.2027.\n"
            "Kimlik unsurları: 5/5 onaylı. KVKK: 3/3 onaylı.\n"
            "SIRADA: UpPromote kaydı IMS'teki e-postayla açılacak, dönen "
            "affiliate linki ve sca_ref buraya eşlenecek.\n"
            "Adres WhatsApp üzerinden alındı (claim-link ile doğrulanmadı)."
        ),
    },
    {
        "full_name": "Gülşah Özgüler",
        "email": "gulsahcolak95@outlook.com",
        "phone": "05342458441",
        "city": "İstanbul",
        "accepted_model": "karma",
        "relationship_stage": "affiliate",
        "accounts": [("instagram", "gulsahozgulerr", True), ("tiktok", "gulsahozgulerr", False)],
        "address": {
            "recipient_name": "Gülşah Özgüler",
            "line1": "Büyükşehir Mah. Ondokuz Mayıs Cad. B Blok 28 Kat Altı No: 26",
            "district": "Beylikdüzü",
            "city": "İstanbul",
            "phone": "05342458441",
        },
        "notes": (
            "Sözleşme: 09.09.2026 (2. nüsha — 1. nüshada organik onay kutusu boştu, "
            "düzeltildi). Islak imzalı tarama.\n"
            "Model: barter + affiliate. EK-1: %10 komisyon / 30 gün attribution / "
            "aylık ödeme / 1.000 TL minimum eşik.\n"
            "Organik içerik kullanımı: EVET.\n"
            "Ücretli reklam (paid media): EVET — süre 6 ay, BİTİŞ 09.03.2027.\n"
            "Kimlik unsurları: 5/5 onaylı. KVKK: 3/3 onaylı.\n"
            "NOT: ilk yazışmada 60 gün attribution anlaşıldığını yazmıştı; "
            "sözleşmede 30 gün olarak teyit edildi.\n"
            "SIRADA: UpPromote kaydı + sca_ref eşlemesi.\n"
            "Adres WhatsApp üzerinden alındı (claim-link ile doğrulanmadı)."
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


def main() -> int:
    db = SessionLocal()
    created = accounts_added = addresses_added = skipped = 0
    warnings = []
    try:
        owner = db.query(User).filter(User.username == OWNER_USERNAME).first()
        if owner:
            print(f"Sorumlu: {owner.full_name} (id {owner.id})\n")
        else:
            warnings.append(f"'{OWNER_USERNAME}' kullanıcısı bulunamadı — "
                            f"creator'lar sorumlusuz açılacak.")
            print(f"⚠ '{OWNER_USERNAME}' bulunamadı — sorumlu boş bırakılacak.\n")

        for rec in CREATORS:
            email = rec["email"].strip().lower()
            c = (db.query(InfluencerCreator)
                 .filter(InfluencerCreator.email == email).first())

            if c:
                print(f"· {rec['full_name']:20} zaten kayıtlı (id {c.id}) — creator atlandı")
                skipped += 1
            else:
                print(f"+ {rec['full_name']:20} {email:32} "
                      f"{rec['accepted_model']:6} · {rec['relationship_stage']}")
                if COMMIT:
                    c = InfluencerCreator(
                        slug=_unique_slug(db, rec["full_name"]),
                        full_name=rec["full_name"], email=email, phone=rec["phone"],
                        country="TR", city=rec["city"], language="tr",
                        accepted_model=rec["accepted_model"],
                        relationship_stage=rec["relationship_stage"],
                        owner_user_id=owner.id if owner else None,
                        owner_name=owner.full_name if owner else None,
                        source="manual", notes=rec["notes"], created_by=ACTOR,
                    )
                    db.add(c)
                    db.flush()
                    db.add(InfluencerActivity(
                        creator_id=c.id, type="system",
                        subject="Sözleşme imzalandı ve sisteme aktarıldı",
                        body=rec["notes"], author_name=ACTOR))
                created += 1

            # ── Hesaplar (creator yoksa kuru çalıştırmada c=None olabilir) ──
            for platform, handle, primary in rec["accounts"]:
                dup = (db.query(InfluencerAccount)
                       .filter(InfluencerAccount.platform == platform,
                               InfluencerAccount.handle == handle).first())
                if dup:
                    owner_ok = c is not None and dup.creator_id == c.id
                    print(f"    · {platform:9} @{handle:20} zaten kayıtlı"
                          + ("" if owner_ok else "  ⚠ BAŞKA CREATOR'DA"))
                    if not owner_ok and c is not None:
                        warnings.append(
                            f"@{handle} ({platform}) başka bir creator'a bağlı "
                            f"(creator_id {dup.creator_id}) — elle bakılmalı.")
                    continue
                print(f"    + {platform:9} @{handle:20}"
                      + ("  [birincil]" if primary else ""))
                if COMMIT and c is not None:
                    db.add(InfluencerAccount(
                        creator_id=c.id, platform=platform, handle=handle,
                        is_primary=primary, metrics_source="manual"))
                accounts_added += 1

            # ── Varsayılan adres ────────────────────────────────────────────
            a = rec["address"]
            has_addr = (c is not None and db.query(InfluencerAddress.id)
                        .filter(InfluencerAddress.creator_id == c.id).first())
            if has_addr:
                print("    · adres zaten kayıtlı — atlandı")
            else:
                print(f"    + adres: {a['line1'][:46]}… {a['district']}/{a['city']}")
                if COMMIT and c is not None:
                    db.add(InfluencerAddress(
                        creator_id=c.id, label="ev", is_default=True, country="TR",
                        **a))
                addresses_added += 1
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
    print(f"Creator: {created} yeni · {skipped} atlandı   |   "
          f"Hesap: {accounts_added}   |   Adres: {addresses_added}")
    if warnings:
        print("\n⚠ UYARILAR:")
        for w in warnings:
            print(f"   · {w}")
    print("\nSIRADAKİ ADIMLAR (bu script kapsamı DIŞINDA):")
    print("   1. Takipçi sayılarını panelden gir → kademe (tier) hesaplansın.")
    print("   2. Hangi markanın ürünü kime gidecek? → iş birliği (collab) aç.")
    print("   3. Elvan + Gülşah için UpPromote kaydı → sca_ref eşlemesi.")
    print("   4. Sözleşme PDF'lerini creator dosyalarına yükle (kind='agreement').")

    print("\n✓ YAZILDI." if COMMIT else "\nKURU ÇALIŞTIRMA — yazmak için: --commit")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
