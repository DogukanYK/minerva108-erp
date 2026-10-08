"""
scripts/import_lab_price_notes_20261007.py — lab fiyat notlarının yüklenmesi.

Sentetik veri dosyası + küçük kart/tedarikçi kurulumu ile: malzeme eşleştirme
(TR katlama, name_tr, material_key, alım belgesinin "lab tablosu karşılığı"
ipucu, belirsiz aday, ayrı form) · tedarikçi takma adları (PHARMATEM,
DOLAIN/Doalin, ULUDĞ / ULUDAĞ AGRO, VESER KİMYA, GÜLER KİMYA (YEŞİL KİL),
TATLI DİLİMLER, HAMMADDE SEPETİ) · öncelik (en yeni belge > lab tablosu,
ezilen değer notta) · birim uyumsuzluğu · elle (manual) ve Stok Son Durum
satırlarına dokunmama · idempotent ikinci çalıştırma · --create-suppliers ·
KDV alanları · kontrol Excel'inin sayfaları · onaylı Excel'den tercih
(grup/kart kapsamı, rank sırası, mevcut tercihi ezmeme) ve bitirilecek
(yalnız E) · Excel satır kayması koruması · CLI kuru çalıştırma · gerçek
veri dosyasıyla duman testi.  İnceleme düzeltmeleri (08.10.2026): üstü
çizili / fiyat listesi kaydı yazılmaz ve yarışmaz · ad ↔ ipucu çelişkisi
belirsiz · alternatif tercih lab satırının kartına · yalnız E/H (X, ✓
hata; Excel listesi girişi reddeder) · işlenmemiş "Doğru değer / Doğru
IMS kartı" hücresi yazımı durdurur · yeni firma yalnız E ile açılır ·
tercih ↔ bitirilecek çelişkisi hata · Tercih önerisinde KDV etiketi ve
"Uludağ ucuz" uyarısı · 2 sarıda ucuz olan üstte.  Birden çok aday kart
(08.10.2026, prod kuru çalıştırması 41 belirsiz): (a) tedarikçinin kendi
kartı (grup içi dahil) · (b) grup ana kartı (reçete > stok, kg/l ×1000) ·
(c) tek reçete kartı = güven belirsiz (Excel'de görünür) · (d) belirsiz,
adaylar no·ad·tedarikçi·stok·reçete · malzeme takma adları (lab_belirlesin)
· belge ↔ aynı firmanın lab kaydı ipucu · toz kartı.
    MINERVA_TEST_DB=minerva_test2 .venv/bin/pytest tests/test_import_lab_price_notes.py -q
"""
import importlib.util
import json
from pathlib import Path

import pytest
from openpyxl import load_workbook
from sqlalchemy.orm import Session

from database import (AdminAuditLog, Item, MaterialGroup, MaterialSupplierPref, Recipe,
                      RecipeIngredient, Supplier, SupplierPrice)

_SPEC = importlib.util.spec_from_file_location(
    "_import_lab_notes",
    Path(__file__).resolve().parent.parent / "scripts" / "import_lab_price_notes_20261007.py")
lab = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(lab)


def rec(rid, malzeme, tedarikci, fiyat, *, tur="lab_tablo", tarih="2026-06-23", cur="EUR",
        unit="kg", pkg=25.0, kdv=None, oran=None, secili=False, guven="kesin", note="", sayfa=1,
        birim="g"):
    return {"id": rid, "kaynak_sayfa": sayfa, "tur": tur, "tarih": tarih, "malzeme": malzeme,
            "birim_kart": birim if tur == "lab_tablo" else None, "tedarikci": tedarikci,
            "fiyat": fiyat, "para_birimi": cur, "fiyat_birimi": unit, "ambalaj_kg": pkg,
            "kdv_dahil": kdv, "kdv_orani": oran, "secili": secili, "guven": guven, "not": note,
            "ihtiyac": None}


def data(*recs, **extra):
    return {"kaynak": "test", "kapsam": "test", "kurallar": [], "kayitlar": list(recs), **extra}


def _items(db, *specs):
    out = {}
    for spec in specs:
        name, unit = spec[0], spec[1]
        kw = spec[2] if len(spec) > 2 else {}
        it = Item(name=name, unit=unit, category=kw.pop("category", "Hammadde"), **kw)
        db.add(it)
        db.flush()
        out[name] = it
    return out


def _sups(db, *names):
    out = {}
    for n in names:
        s = Supplier(name=n)
        db.add(s)
        db.flush()
        out[n] = s
    return out


def _prices(db):
    db.expire_all()
    return db.query(SupplierPrice).order_by(SupplierPrice.id).all()


def _lab_prices(db):
    return [p for p in _prices(db) if (p.source_label or "").startswith(lab.LABEL_PREFIX)]


def _by_id(p):
    return {r["id"]: r for r in p["records"]}


# ─── Eşleştirme ─────────────────────────────────────────────────────────────

def test_material_match_tr_fold_name_tr_material_key_and_ambiguous(db_session: Session):
    its = _items(db_session, ("COCO BETAİNE%30", "g"), ("SHEA YAĞI", "g", {"name_tr": "KARİTE"}),
                 ("JOJOBA YAĞI", "g"), ("BADEM YAĞI", "g"), ("BADEM YAGI (NUMUNE)", "g"),
                 ("ÜRÜN X", "adet", {"category": "Bitmiş Ürün"}))
    _sups(db_session, "UMAYCHEM")
    db_session.commit()
    p = lab.plan(db_session, data(
        rec("A1-01-S1", "coco betaine%30", "UMAYCHEM", 1.0),            # TR katlama + küçük harf
        rec("A1-02-S1", "KARİTE", "UMAYCHEM", 2.0),                      # name_tr
        rec("A1-03-S1", "JOJOBA OIL", "UMAYCHEM", 3.0),                  # material_key (OIL = YAĞI)
        rec("A1-04-S1", "ALMOND OIL", "UMAYCHEM", 4.0),                  # 2 kart aynı anahtar → belirsiz
        rec("A1-05-S1", "ÜRÜN X", "UMAYCHEM", 5.0),                      # bitmiş ürün sayılmaz
        rec("A1-06-S1", "COCO BETAİNE %31", "UMAYCHEM", 6.0),            # bulunamaz → benzer öneri
    ))
    r = _by_id(p)
    assert r["A1-01-S1"]["item"]["id"] == its["COCO BETAİNE%30"].id and r["A1-01-S1"]["mat"]["how"] == "ad"
    assert r["A1-02-S1"]["item"]["id"] == its["SHEA YAĞI"].id
    assert r["A1-03-S1"]["item"]["id"] == its["JOJOBA YAĞI"].id
    assert r["A1-03-S1"]["mat"]["how"] == "material_key"
    assert r["A1-04-S1"]["status"] == "belirsiz"
    assert set(r["A1-04-S1"]["mat"]["candidates"]) == {its["BADEM YAĞI"].id, its["BADEM YAGI (NUMUNE)"].id}
    assert r["A1-05-S1"]["status"] == "eslesmedi"
    assert r["A1-06-S1"]["status"] == "eslesmedi"
    assert its["COCO BETAİNE%30"].id in r["A1-06-S1"]["mat"]["suggestions"]
    assert p["counts"]["malzeme_belirsiz"] == 1 and p["counts"]["malzeme_eslesmeyen"] == 2


def test_document_uses_lab_table_hint_and_strips_package_suffix(db_session: Session):
    its = _items(db_session, ("ASPİR YAĞI", "g"), ("SUSAM YAĞI", "g"), ("PAPATYA HİDRASOLÜ", "g"),
                 ("TATLI BADEM YAGI", "g"), ("BADEM YAĞI", "g"))
    _sups(db_session, "NATURALYA", "KRK GIDA", "ULUDAĞ HERBAL")
    db_session.commit()
    p = lab.plan(db_session, data(
        rec("A1-07-S1", "ASPİR YAĞI", "NATURALYA", 12.9),
        rec("A1-14-S1", "PAPATYA HİDRASOLÜ", "ULUDAĞ HERBAL", 4.36),
        rec("A1-26-S1", "TATLI BADEM YAGI", "NATURALYA", 12.9),
        rec("A1-41-S1", "BADEM YAĞI", "NATURALYA", 15.17),
        rec("C-s19-2", "SAFFLOWER OIL", "NATURALYA KİMYA", 15.0, tur="proforma", tarih="2026-07-01",
            cur="USD", note="aspir yağı; lab tablosu karşılığı: ASPİR YAĞI (A1-07)"),
        rec("C-s13-2", "SUSAM YAĞI (KG)", "KRK GIDA", 340.0, tur="proforma", tarih="2026-06-25",
            cur="TRY", pkg=5.0),
        rec("C-s9-3", "PAPATYA HİDROSOLÜ 1LT", "ULUDAĞ AGRO", 208.33, tur="fatura", tarih="2026-06-26",
            cur="TRY", unit="l", pkg=1.0,
            note="lab tablosu karşılığı: PAPATYA HİDRASOLÜ (A1-14, g birimli kart)"),
        # İki ipucu farklı karta → material_key ayırır (BADEM YAĞI (TATLI) = TATLI BADEM YAGI)
        rec("C-s13-1", "BADEM YAĞI (TATLI) 1 LT.", "KRK GIDA", 750.0, tur="proforma",
            tarih="2026-06-25", cur="TRY", unit="l", pkg=1.0,
            note="lab tablosu karşılığı: TATLI BADEM YAGI (A1-26) / BADEM YAĞI (A1-41)"),
    ))
    r = _by_id(p)
    # Belge, ipucundaki AYNI firmanın lab kaydının kartına gider (NATURALYA KİMYA = NATURALYA)
    assert r["C-s19-2"]["item"]["id"] == its["ASPİR YAĞI"].id
    assert r["C-s19-2"]["mat"]["how"] == f"{lab.HOW_HINT_SAME} A1-07-S1"
    assert r["C-s13-2"]["item"]["id"] == its["SUSAM YAĞI"].id and r["C-s13-2"]["mat"]["how"] == "ad"
    assert r["C-s9-3"]["item"]["id"] == its["PAPATYA HİDRASOLÜ"].id
    assert r["C-s13-1"]["item"]["id"] == its["TATLI BADEM YAGI"].id
    assert r["C-s13-1"]["mat"]["how"] == "ipucu + material_key"
    assert lab.clean_doc_material("XANTHAN GUM (25 KG) (MEIUHA)") == "XANTHAN GUM (MEIUHA)"
    assert lab.clean_doc_material("MİSK ADAÇAYI YAĞI LT") == "MİSK ADAÇAYI YAĞI"


def test_separate_form_records_never_auto_matched(db_session: Session):
    _items(db_session, ("ALEOVERA EKSTRAKTI", "ml"))
    _sups(db_session, "NATURALYA")
    db_session.commit()
    p = lab.plan(db_session, data(
        rec("A1-08-S2", "ALEOVERA EKSTRAKTI", "NATURALYA", 10.32, secili=True),
        rec("A1-08-S3", "ALEOVERA EKSTRAKTI", "Naturalya", 39.6, note="toz hali"),
        rec("C-s8-1", "ALOEVERA EXTRACT PE", "NATURALYA KİMYA", 43.0, tur="proforma", tarih="2026-07-16",
            cur="USD", pkg=1.0, note="lab tablosu karşılığı: ALEOVERA EKSTRAKTI (A1-08, sıvı kart)"),
    ))
    r = _by_id(p)
    assert r["A1-08-S3"]["status"] == "ayri_form" and r["C-s8-1"]["status"] == "ayri_form"
    # Sıvı kartın fiyatı tozun proformasıyla EZİLMEZ
    assert r["A1-08-S2"]["status"] == "yazilacak" and not r["A1-08-S2"]["overridden"]


def test_supplier_aliases_and_new_suppliers(db_session: Session):
    _items(db_session, ("X", "g"))
    s = _sups(db_session, "PHARMATERM", "DOALİNN", "ULUDAĞ HERBAL", "VESER KİMYEVİ", "GÜLER KİMYA",
              "TATLIDİLİMLER", "HAMMADDESEPETİ", "DOGASA", "YİĞİTOĞLU KİMYA")
    db_session.commit()
    names = ["PHARMATEM", "PHARMATERM İLAÇ", "Pharmaterm", "DOLAIN", "DOALIN", "Doalin", "ULUDĞ HERBAL",
             "ULUDAĞ AGRO", "VESER KİMYA", "GÜLER KİMYA (YEŞİL KİL)", "TATLI DİLİMLER", "HAMMADDE SEPETİ",
             "DOĞASA", "YİĞİTOĞLU", "BEFCHEM", "ATAMAN KİMYA", "MARKET"]
    p = lab.plan(db_session, data(*[rec(f"A1-{i:02d}-S1", "X", n, 1.0 + i) for i, n in enumerate(names, 1)]))
    got = {r["supplier"]: r["sup"] for r in p["records"]}
    want = {"PHARMATEM": "PHARMATERM", "PHARMATERM İLAÇ": "PHARMATERM", "Pharmaterm": "PHARMATERM",
            "DOLAIN": "DOALİNN", "DOALIN": "DOALİNN", "Doalin": "DOALİNN", "ULUDĞ HERBAL": "ULUDAĞ HERBAL",
            "ULUDAĞ AGRO": "ULUDAĞ HERBAL", "VESER KİMYA": "VESER KİMYEVİ",
            "GÜLER KİMYA (YEŞİL KİL)": "GÜLER KİMYA", "TATLI DİLİMLER": "TATLIDİLİMLER",
            "HAMMADDE SEPETİ": "HAMMADDESEPETİ", "DOĞASA": "DOGASA", "YİĞİTOĞLU": "YİĞİTOĞLU KİMYA"}
    for doc, card in want.items():
        assert got[doc]["kind"] == "id" and got[doc]["id"] == s[card].id, doc
    assert {n["name"] for n in p["new_suppliers"]} == {"BEFCHEM", "ATAMAN KİMYA", "MARKET"}
    for n in ("BEFCHEM", "ATAMAN KİMYA", "MARKET"):
        assert got[n]["kind"] == "new"


def test_inactive_duplicate_and_merged_card(db_session: Session):
    _items(db_session, ("X", "g"))
    s = _sups(db_session, "TATLİDİLİMLER", "SURYA KİMYA", "SURYA ESKİ")
    s["TATLİDİLİMLER"].is_active = False                       # pasif mükerrer — eşlenmez
    s["SURYA ESKİ"].is_active = False
    s["SURYA ESKİ"].merged_into_id = s["SURYA KİMYA"].id
    db_session.commit()
    p = lab.plan(db_session, data(rec("A1-01-S1", "X", "TATLI DİLİMLER", 1.0),
                                  rec("A1-02-S1", "X", "SURYA ESKİ", 2.0)))
    r = _by_id(p)
    # Yalnız pasif kartı olan firma: yeni kart AÇILMAZ, satır yazılmaz
    assert r["A1-01-S1"]["sup"]["kind"] == "inactive" and r["A1-01-S1"]["status"] == "pasif_tedarikci"
    assert p["new_suppliers"] == []
    assert r["A1-02-S1"]["sup"]["id"] == s["SURYA KİMYA"].id and r["A1-02-S1"]["status"] == "yazilacak"
    # Aktif ikiz varsa pasif mükerrer önemsiz: aktif karta bağlanır
    twin = Supplier(name="TATLIDİLİMLER")
    db_session.add(twin)
    db_session.commit()
    r = _by_id(lab.plan(db_session, data(rec("A1-01-S1", "X", "TATLI DİLİMLER", 1.0))))
    assert r["A1-01-S1"]["sup"]["id"] == twin.id
    p = lab.apply(db_session, data(rec("A1-01-S1", "X", "TATLI DİLİMLER", 1.0)), create_suppliers=True)
    assert p["created_suppliers"] == [] and p["written"] == 1


# ─── Öncelik, birim, elle fiyat ─────────────────────────────────────────────

def test_newest_document_wins_and_overridden_values_in_note(db_session: Session):
    its = _items(db_session, ("LAURYL GLUCOSİDE", "g"))
    _sups(db_session, "UMAYCHEM")
    db_session.commit()
    p = lab.apply(db_session, data(
        rec("A1-05-S1", "LAURYL GLUCOSİDE", "UMAYCHEM", 1.93, secili=True, pkg=220.0),
        rec("C-s12-1", "Lauryl Glucoside", "UMAYCHEM", 1.90, tur="proforma", tarih="2026-06-26",
            cur="USD", pkg=220.0, kdv=False, oran=20.0, sayfa=12),
        rec("C-s99-1", "Lauryl Glucoside", "UMAYCHEM", 2.10, tur="siparis", tarih="2026-07-10",
            cur="USD", pkg=25.0, kdv=False, oran=20.0, sayfa=15),
    ))
    r = _by_id(p)
    assert r["C-s99-1"]["status"] == "yazilacak"
    assert r["C-s12-1"]["status"] == "ezildi" and r["A1-05-S1"]["status"] == "ezildi"
    rows = _lab_prices(db_session)
    assert len(rows) == 1
    sp = rows[0]
    assert sp.item_id == its["LAURYL GLUCOSİDE"].id and sp.unit_price == 2.10 and sp.currency == "USD"
    assert sp.source == "siparis" and sp.source_label == f"{lab.LABEL_PREFIX} — s.15"
    assert str(sp.quoted_at) == "2026-07-10" and sp.package_size == 25.0
    assert sp.vat_included is False and sp.vat_rate == 20.0 and sp.created_by == lab.ACTOR
    assert "C-s12-1" in sp.note and "A1-05-S1" in sp.note and "Ezilen" in sp.note


def test_lab_rows_vat_and_selected_note(db_session: Session):
    _items(db_session, ("VANİLYA UÇUCU YAĞI", "ml"))
    _sups(db_session, "PHARMATERM", "ULUDAĞ HERBAL")
    db_session.commit()
    lab.apply(db_session, data(
        rec("A2-40-S1", "VANİLYA UÇUCU YAĞI", "ULUDAĞ HERBAL", 47.58, birim="ml"),
        rec("A2-40-S2", "VANİLYA UÇUCU YAĞI", "PHARMATERM İLAÇ", 82.56, kdv=True, oran=20.0,
            secili=True, birim="ml"),
    ))
    rows = {p.supplier_name: p for p in _lab_prices(db_session)}
    ph = rows["PHARMATERM"]
    assert ph.vat_included is True and ph.vat_rate == 20.0 and ph.source == "lab_notu"
    assert ph.price_unit == "kg" and ph.currency == "EUR" and "SARI" in ph.note
    assert rows["ULUDAĞ HERBAL"].vat_included is None and rows["ULUDAĞ HERBAL"].vat_rate is None


def test_unit_mismatch_listed_and_not_competing(db_session: Session):
    its = _items(db_session, ("POMPA", "adet"), ("ARGAN YAĞI", "g"))
    _sups(db_session, "NATURALYA")
    db_session.commit()
    p = lab.apply(db_session, data(
        rec("A1-01-S1", "POMPA", "NATURALYA", 1.0),                                   # kg ↔ adet
        rec("A1-12-S1", "ARGAN YAĞI", "NATURALYA", 25.8),
        rec("C-s1-1", "ARGAN YAĞI", "NATURALYA", 30.0, tur="proforma", tarih="2026-07-01",
            unit="adet", cur="USD"),                                                   # uyumsuz → yarışmaz
    ))
    r = _by_id(p)
    assert r["A1-01-S1"]["status"] == "uyumsuz" and r["C-s1-1"]["status"] == "uyumsuz"
    assert r["A1-12-S1"]["status"] == "yazilacak"
    rows = _lab_prices(db_session)
    assert [(x.item_id, x.unit_price) for x in rows] == [(its["ARGAN YAĞI"].id, 25.8)]


def test_manual_and_stok_son_durum_rows_untouched(db_session: Session):
    its = _items(db_session, ("ARGAN YAĞI", "g"), ("JOJOBA YAĞI", "g"))
    s = _sups(db_session, "NATURALYA", "SURYA KİMYA")
    man = SupplierPrice(item_id=its["ARGAN YAĞI"].id, supplier_id=s["NATURALYA"].id, supplier_name="NATURALYA",
                        unit_price=24.0, currency="EUR", price_unit="kg", source="manual",
                        created_by="Songül", domain="cosmetics")
    ssd = SupplierPrice(item_id=its["JOJOBA YAĞI"].id, supplier_id=None, supplier_name="NATURALYA",
                        unit_price=14.0, currency="USD", price_unit="kg", source="stok_son_durum",
                        source_label="Stok Son Durum — Eylül", domain="cosmetics")
    db_session.add_all([man, ssd])
    db_session.commit()
    snap = [(x.id, x.unit_price, x.source, x.source_label, x.note) for x in _prices(db_session)]
    d = data(rec("A1-12-S3", "ARGAN YAĞI", "NATURALYA", 25.8, secili=True),
             rec("A1-12-S2", "ARGAN YAĞI", "SURYA KİMYA", 26.0),
             rec("A1-17-S1", "JOJOBA YAĞI", "NATURALYA", 12.9))
    p = lab.apply(db_session, d)
    r = _by_id(p)
    assert r["A1-12-S3"]["status"] == "elle_var" and "24" in "; ".join(r["A1-12-S3"]["flags"])
    assert r["A1-17-S1"]["status"] == "yazilacak"
    assert any("Stok Son Durum" in f for f in r["A1-17-S1"]["flags"])
    after = {x.id: (x.id, x.unit_price, x.source, x.source_label, x.note) for x in _prices(db_session)}
    for row in snap:
        assert after[row[0]] == row                                    # elle + Stok Son Durum aynen
    labs = _lab_prices(db_session)
    assert {(x.item_id, x.supplier_name) for x in labs} == {(its["ARGAN YAĞI"].id, "SURYA KİMYA"),
                                                           (its["JOJOBA YAĞI"].id, "NATURALYA")}
    # İkinci çalıştırma: elle satır hâlâ kazanır, Stok Son Durum hâlâ yerinde
    lab.apply(db_session, d)
    after2 = {x.id: (x.id, x.unit_price, x.source, x.source_label, x.note) for x in _prices(db_session)}
    for row in snap:
        assert after2[row[0]] == row


def test_second_run_is_idempotent(db_session: Session):
    _items(db_session, ("GLİSERİN", "g"), ("SHEA BUTTER", "g"))
    _sups(db_session, "UMAYCHEM", "BEFCHEM")
    db_session.commit()
    d = data(rec("A1-09-S1", "GLİSERİN", "UMAYCHEM", 4.52, secili=True),
             rec("A1-18-S1", "SHEA BUTTER", "BEFCHEM", 8.0, secili=True))
    first = lab.apply(db_session, d)
    rows1 = [(x.item_id, x.supplier_id, x.unit_price, x.source, x.source_label, x.note, x.package_size)
             for x in _lab_prices(db_session)]
    second = lab.apply(db_session, d)
    rows2 = [(x.item_id, x.supplier_id, x.unit_price, x.source, x.source_label, x.note, x.package_size)
             for x in _lab_prices(db_session)]
    assert first["written"] == 2 and first["deleted"] == 0
    assert second["written"] == 2 and second["deleted"] == 2
    assert sorted(rows1) == sorted(rows2) and len(_prices(db_session)) == 2
    db_session.expire_all()
    audits = db_session.query(AdminAuditLog).filter(AdminAuditLog.action == lab.AUDIT_IMPORT).all()
    assert len(audits) == 2 and audits[0].actor_id is None
    det = json.loads(audits[1].details)
    assert det["actor"] == lab.ACTOR and det["written"] == 2 and det["deleted_previous"] == 2


def test_partial_import_replaces_only_selected_pair_and_keeps_full_plan_winner(db_session: Session):
    its = _items(db_session, ("X", "g"), ("Y", "g"))
    sups = _sups(db_session, "UMAYCHEM", "SURYA KİMYA")
    selected_pair = dict(item_id=its["X"].id, supplier_id=sups["UMAYCHEM"].id,
                         supplier_name="UMAYCHEM")
    base = dict(unit_price=9.0, currency="EUR", price_unit="kg", domain="cosmetics")
    old_selected = [SupplierPrice(**selected_pair, **base, source=source,
                                  source_label=lab.LABEL_PREFIX + " — eski")
                    for source in lab.LAB_SOURCES]
    preserved = [
        # Seçili kartın başka firması ve seçili firmanın başka kartı korunur.
        SupplierPrice(item_id=its["X"].id, supplier_id=sups["SURYA KİMYA"].id,
                      supplier_name="SURYA KİMYA", **base, source="fatura",
                      source_label=lab.LABEL_PREFIX + " — seçilmedi"),
        SupplierPrice(item_id=its["Y"].id, supplier_id=sups["UMAYCHEM"].id,
                      supplier_name="UMAYCHEM", **base, source="siparis",
                      source_label=lab.LABEL_PREFIX + " — seçilmedi"),
        SupplierPrice(item_id=its["Y"].id, supplier_id=sups["UMAYCHEM"].id,
                      supplier_name="UMAYCHEM", **base, source="manual", created_by="Songül"),
        SupplierPrice(**selected_pair, **base, source="stok_son_durum",
                      source_label=lab.LABEL_PREFIX + " — SSD"),
        SupplierPrice(**selected_pair, **base, source="lab_notu", source_label="Başka lab belgesi"),
        SupplierPrice(**selected_pair, unit_price=9.0, currency="EUR", price_unit="kg",
                      domain="supplement", source="lab_notu", source_label=lab.LABEL_PREFIX),
    ]
    db_session.add_all(old_selected + preserved)
    db_session.commit()
    old_ids = {p.id for p in old_selected}
    keep = [(p.id, p.item_id, p.supplier_id, p.source, p.domain, p.unit_price, p.source_label)
            for p in preserved]
    d = data(rec("A1-01-S1", "X", "UMAYCHEM", 1.0, secili=True),
             rec("C-s12-1", "X", "UMAYCHEM", 2.0, tur="proforma", tarih="2026-07-01"),
             rec("A1-01-S2", "X", "SURYA KİMYA", 3.0),
             rec("A1-02-S1", "Y", "UMAYCHEM", 4.0))
    selection = [{"id": "C-s12-1", "item_id": its["X"].id, "supplier_id": sups["UMAYCHEM"].id}]
    first = lab.apply(db_session, d, record_selection=selection)
    assert first["applied"] and first["written"] == 1 and first["deleted"] == len(lab.LAB_SOURCES)
    assert _by_id(first)["A1-01-S1"]["status"] == "ezildi"
    second = lab.apply(db_session, d, record_selection=selection)
    assert second["applied"] and second["written"] == 1 and second["deleted"] == 1
    got = _prices(db_session)
    assert old_ids.isdisjoint(p.id for p in got)
    for row in keep:
        assert row in [(p.id, p.item_id, p.supplier_id, p.source, p.domain, p.unit_price, p.source_label)
                       for p in got]
    imported = [p for p in got if p.id not in {row[0] for row in keep}]
    assert len(imported) == 1 and imported[0].source == "proforma" and imported[0].unit_price == 2.0
    assert "A1-01-S1" in imported[0].note and second["selected_records"] == ["C-s12-1"]
    audit = db_session.query(AdminAuditLog).filter(AdminAuditLog.action == lab.AUDIT_IMPORT).order_by(
        AdminAuditLog.id.desc()).first()
    assert json.loads(audit.details)["record_selection"] == selection


@pytest.mark.parametrize("invalid", ["empty", "unknown", "duplicate", "overridden", "manual", "new"])
def test_partial_import_invalid_selection_writes_nothing(db_session: Session, invalid):
    its = _items(db_session, ("X", "g"), ("Y", "g"))
    sups = _sups(db_session, "UMAYCHEM")
    db_session.add_all([
        SupplierPrice(item_id=its["X"].id, supplier_id=sups["UMAYCHEM"].id, supplier_name="UMAYCHEM",
                      unit_price=9.0, currency="EUR", price_unit="kg", source="lab_notu",
                      source_label=lab.LABEL_PREFIX, domain="cosmetics"),
        SupplierPrice(item_id=its["Y"].id, supplier_id=sups["UMAYCHEM"].id, supplier_name="UMAYCHEM",
                      unit_price=8.0, currency="EUR", price_unit="kg", source="manual", domain="cosmetics"),
    ])
    db_session.commit()
    d = data(rec("A1-01-S1", "X", "UMAYCHEM", 1.0),
             rec("C-s12-1", "X", "UMAYCHEM", 2.0, tur="proforma", tarih="2026-07-01"),
             rec("A1-02-S1", "Y", "UMAYCHEM", 3.0), rec("A1-03-S1", "X", "NOUVEAU", 4.0))
    good = {"id": "C-s12-1", "item_id": its["X"].id, "supplier_id": sups["UMAYCHEM"].id}
    bad_ids = {"unknown": "ABSENT", "overridden": "A1-01-S1", "manual": "A1-02-S1", "new": "A1-03-S1"}
    if invalid == "empty":
        selection = []
    elif invalid == "duplicate":
        selection = [good, dict(good)]
    else:
        selection = [good, {**good, "id": bad_ids[invalid]}]
    before = [(p.id, p.item_id, p.supplier_id, p.unit_price, p.source) for p in _prices(db_session)]
    p = lab.apply(db_session, d, record_selection=selection)
    assert not p["applied"] and p["errors"]
    assert [(x.id, x.item_id, x.supplier_id, x.unit_price, x.source) for x in _prices(db_session)] == before
    assert db_session.query(AdminAuditLog).count() == 0
    assert db_session.query(Supplier).count() == 1 and db_session.query(MaterialSupplierPref).count() == 0


@pytest.mark.parametrize("changed", ["item", "supplier"])
def test_partial_import_live_rematch_change_blocks_all_selected_prices(db_session: Session, changed):
    its = _items(db_session, ("X", "g"), ("Y", "g"))
    sups = _sups(db_session, "UMAYCHEM")
    db_session.commit()
    d = data(rec("A1-01-S1", "X", "UMAYCHEM", 1.0), rec("A1-02-S1", "Y", "UMAYCHEM", 2.0))
    p = lab.plan(db_session, d)
    selection = [{"id": r["id"], "item_id": r["item"]["id"], "supplier_id": r["sup"]["id"]}
                 for r in p["records"]]
    if changed == "item":
        its["Y"].name = "ESKİ Y"
        replacement = _items(db_session, ("Y", "g"))["Y"]
        assert replacement.id != selection[1]["item_id"]
    else:
        sups["UMAYCHEM"].is_active = False
        replacement = _sups(db_session, "UMAYCHEM")["UMAYCHEM"]
        assert replacement.id != selection[0]["supplier_id"]
    db_session.commit()
    result = lab.apply(db_session, d, record_selection=selection)
    assert not result["applied"] and any("eşlemesi değişti" in e for e in result["errors"])
    assert _prices(db_session) == [] and db_session.query(AdminAuditLog).count() == 0


@pytest.mark.parametrize("manifest", [["C-s12-1"], [{"id": "C-s12-1", "item_id": True, "supplier_id": 1}],
                                     [{"id": "C-s12-1", "item_id": 1, "supplier_id": 0}],
                                     [{"id": "C-s12-1", "item_id": 1, "supplier_id": 1, "approved": True}]])
def test_record_manifest_requires_exact_expected_identity_schema(tmp_path, manifest):
    path = tmp_path / "secim.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError):
        lab.load_record_selection(path)


def test_new_supplier_rows_skipped_without_flag_created_with_flag(db_session: Session, tmp_path):
    its = _items(db_session, ("D-PANTHENOL", "g"), ("GLİSERİN", "g"))
    _sups(db_session, "UMAYCHEM")
    db_session.commit()
    d = data(rec("A1-20-S1", "D-PANTHENOL", "ATAMAN KİMYA", 11.63, secili=True),
             rec("A1-09-S1", "GLİSERİN", "UMAYCHEM", 4.52),
             rec("A1-21-S1", "GLİSERİN", "MARKET", 0.29),
             rec("A1-22-S1", "YOK BÖYLE KART", "KİMYACINIZ", 5.46))
    p = lab.apply(db_session, d)
    assert _by_id(p)["A1-20-S1"]["status"] == "yeni_tedarikci"
    assert [x.item_id for x in _lab_prices(db_session)] == [its["GLİSERİN"].id]
    db_session.expire_all()
    assert db_session.query(Supplier).filter(Supplier.name == "ATAMAN KİMYA").count() == 0
    # Onaylı Excel'siz kart açılmaz
    p = lab.apply(db_session, d, create_suppliers=True)
    assert p["applied"] is False and any("onaylı Excel" in e for e in p["errors"])
    # 'Yeni tedarikçiler': yalnız E açılır; H (MARKET) açılmaz; yazılabilir
    # satırı olmayan (KİMYACINIZ — kartı yok) "—", açılmaz
    path = lab.build_xlsx(lab.plan(db_session, d), tmp_path / "onay.xlsx")
    ws = load_workbook(path)["Yeni tedarikçiler"]
    head = [c.value for c in ws[1]]
    new_rows = {r[head.index("Açılacak ad")]: r for r in ws.iter_rows(min_row=2, values_only=True)}
    assert new_rows["KİMYACINIZ"][head.index(lab.NEW_COL)] == "—"
    assert ws.data_validations.dataValidation[0].showErrorMessage
    ids = {r[head.index("Açılacak ad")]: r[head.index("Kayıt")] for r in new_rows.values()}
    _fill(path, "Yeni tedarikçiler", lab.NEW_COL, {ids["ATAMAN KİMYA"]: "E", ids["MARKET"]: "H"})
    appr = lab.read_approvals(path)
    assert not appr["errors"] and appr["new"][ids["ATAMAN KİMYA"]]["ok"] is True
    p = lab.apply(db_session, d, create_suppliers=True, approvals=appr)
    assert p["applied"]
    db_session.expire_all()
    assert db_session.query(Supplier).filter(Supplier.name.in_(("MARKET", "KİMYACINIZ"))).count() == 0
    at = db_session.query(Supplier).filter(Supplier.name == "ATAMAN KİMYA").one()
    assert at.domain == "cosmetics" and at.is_active and lab.SUPPLIER_NOTE in (at.notes or "")
    assert p["created_suppliers"] == [{"id": at.id, "name": "ATAMAN KİMYA"}]
    rows = {x.item_id: x for x in _lab_prices(db_session)}
    assert rows[its["D-PANTHENOL"].id].supplier_id == at.id
    # Kart artık var: üçüncü çalıştırma yeni kart açmaz, aynı sonucu yazar
    p = lab.apply(db_session, d, create_suppliers=True, approvals=appr)
    assert p["created_suppliers"] == [] and p["written"] == 2
    assert db_session.query(Supplier).filter(Supplier.name == "ATAMAN KİMYA").count() == 1


def test_bad_manual_override_raises(db_session: Session):
    _items(db_session, ("X", "g"))
    db_session.commit()
    with pytest.raises(ValueError):
        lab.plan(db_session, data(rec("A1-01-S1", "Y", "Z", 1.0), elle_eslesme={"kart": {"A1-01": 999999}}))


def test_manual_override_maps_row(db_session: Session):
    its = _items(db_session, ("NAR ÇEKİRDEĞİ YAĞI", "g"))
    _sups(db_session, "UMAYCHEM")
    db_session.commit()
    p = lab.plan(db_session, data(rec("A2-01-S2", "NAR ÇEKİDEĞİ YAGI", "UMAYCHEM", 18.57),
                                  elle_eslesme={"kart": {"A2-01": its["NAR ÇEKİRDEĞİ YAĞI"].id}}))
    r = p["records"][0]
    assert r["item"]["id"] == its["NAR ÇEKİRDEĞİ YAĞI"].id and r["mat"]["how"] == "elle eşleme"


# ─── Kontrol Excel'i + onay uygulaması ──────────────────────────────────────

def _pref_setup(db):
    its = _items(db, ("ALEOVERA EKSTRAKTI", "ml"), ("AKTİF KÖMÜR", "g"), ("GLİSERİN", "g"),
                 ("GLYCERIN", "g"), ("SQUALANE", "g"))
    grp = MaterialGroup(name="GLİSERİN", domain="cosmetics")
    db.add(grp)
    db.flush()
    its["GLİSERİN"].material_group_id = grp.id
    its["GLYCERIN"].material_group_id = grp.id
    s = _sups(db, "SURYA KİMYA", "NATURALYA", "TATLIDİLİMLER", "BEFCHEM", "UMAYCHEM", "HAMMADDESEPETİ",
              "ROSECE")
    # Mevcut tercih (elle): squalane'de UMAYCHEM 1. sıra — ezilmemeli
    db.add(MaterialSupplierPref(domain="cosmetics", item_id=its["SQUALANE"].id, supplier_id=s["UMAYCHEM"].id,
                                preference="preferred", rank=1, created_by="Songül"))
    db.commit()
    d = data(
        rec("A1-08-S1", "ALEOVERA EKSTRAKTI", "SURYA KİMYA", 16.34, secili=True, birim="ml"),
        rec("A1-08-S2", "ALEOVERA EKSTRAKTI", "NATURALYA", 10.32, secili=True, birim="ml"),
        rec("A1-09-S1", "GLİSERİN", "UMAYCHEM", 4.52, secili=True),
        rec("A2-13-S1", "AKTİF KÖMÜR", "TATLI DİLİMLER", 23.55, pkg=1.0),
        rec("A2-13-S2", "AKTİF KÖMÜR", "BEFCHEM", 38.0, pkg=20.0, secili=True),
        rec("A1-34-S1", "SQUALANE", "HAMMADDE SEPETİ", 120.37, pkg=1.0),
        rec("A1-34-S2", "SQUALANE", "BEFCHEM", 45.0, pkg=5.0, secili=True),
        rec("A1-31-S1", "SQUALANE", "ROSECE", 28.67, pkg=1.0),
        rec("A1-35-S1", "SQUALANE", "SURYA KİMYA", 124.0, pkg=1.0),
    )
    return its, s, grp, d


def _fill(path, sheet, col, values):
    wb = load_workbook(path)
    ws = wb[sheet]
    head = [c.value for c in ws[1]]
    ik, ic = head.index("Kayıt") + 1, head.index(col) + 1
    for r in range(2, ws.max_row + 1):
        rid = ws.cell(r, ik).value
        if rid in values:
            ws.cell(r, ic, values[rid])
    wb.save(path)


def test_xlsx_sheets_and_columns(db_session: Session, tmp_path):
    _its, _s, _grp, d = _pref_setup(db_session)
    p = lab.plan(db_session, d)
    path = lab.build_xlsx(p, tmp_path / "kontrol.xlsx")
    wb = load_workbook(path)
    assert wb.sheetnames == list(lab.SHEETS)
    fiy = wb["Fiyatlar"]
    assert fiy.max_row == 1 + len(d["kayitlar"]) and fiy.freeze_panes == "B2" and fiy.auto_filter.ref
    pref = wb["Tercih önerisi"]
    head = [c.value for c in pref[1]]
    assert lab.APPROVE_COL in head
    rows = [[c.value for c in r] for r in pref.iter_rows(min_row=2)]
    ids = [r[0] for r in rows]
    # sarılar (2 sarıda ucuz olan — NATURALYA 10,32 — üstte) + nottan
    # alternatif (A2-13-S1, aynı malzemenin sarısından sonra)
    assert ids == ["A1-08-S2", "A1-08-S1", "A1-09-S1", "A2-13-S2", "A2-13-S1", "A1-34-S2"]
    warn = {r[0]: r[head.index("Uyarı")] for r in rows}
    assert "2 sarı seçim" in warn["A1-08-S1"] and "tatlı dilimlerden" in warn["A2-13-S2"]
    assert "yalnız NATURALYA" in warn["A1-08-S1"]
    assert pref.data_validations.dataValidation[0].showErrorMessage
    assert all(r[head.index(lab.APPROVE_COL)] is None for r in rows)
    cur = {r[0]: r[head.index("Mevcut tercih")] for r in rows}
    assert "UMAYCHEM (tercih 1)" in cur["A1-34-S2"]
    small = wb["Küçük satıcı önerisi"]
    sh = [c.value for c in small[1]]
    srows = {r[sh.index("Öneri türü")]: r for r in small.iter_rows(min_row=2, values_only=True)}
    kinds = [r[sh.index("Öneri türü")] for r in small.iter_rows(min_row=2, values_only=True)]
    assert kinds.count("küçük satıcı") == 3 and "bilgi" in kinds           # Surya: karışık → bilgi
    assert srows["bilgi"][sh.index(lab.PHASE_COL)] == "—"
    for name in ("Eşleşmeyenler", "Belirsiz okumalar", "Yeni tedarikçiler", "Nasıl doldurulur"):
        assert wb[name].max_row >= 1


def test_apply_prefs_e_only_group_scope_rank_and_existing_kept(db_session: Session, tmp_path):
    its, s, grp, d = _pref_setup(db_session)
    path = lab.build_xlsx(lab.plan(db_session, d), tmp_path / "onay.xlsx")
    _fill(path, "Tercih önerisi", lab.APPROVE_COL,
          {"A1-08-S1": "E", "A1-08-S2": "e", "A1-09-S1": "Evet", "A2-13-S2": "H", "A2-13-S1": "E",
           "A1-34-S2": "E"})
    small_ids = {x["sup"]["name"]: x["id"] for x in lab.plan(db_session, d)["small"]}
    _fill(path, "Küçük satıcı önerisi", lab.PHASE_COL,
          {small_ids["HAMMADDESEPETİ"]: "E", small_ids["ROSECE"]: "H"})
    appr = lab.read_approvals(path)
    assert not appr["errors"]
    # Kuru: hiçbir şey yazılmaz
    pp = lab.plan_prefs(db_session, appr, d)
    assert pp["counts"] == {"tercih_yazilacak": 5, "tercih_atlanan": 0, "bitirilecek": 1}
    db_session.expire_all()
    assert db_session.query(MaterialSupplierPref).count() == 1
    pp = lab.apply_prefs(db_session, appr, d)
    assert pp["applied"]
    db_session.expire_all()
    prefs = {(p.item_id, p.material_group_id, p.supplier_id): p
             for p in db_session.query(MaterialSupplierPref).all()}
    alo = its["ALEOVERA EKSTRAKTI"].id
    assert prefs[(alo, None, s["NATURALYA"].id)].rank == 1             # Excel sırası = rank (ucuz üstte)
    assert prefs[(alo, None, s["SURYA KİMYA"].id)].rank == 2
    assert prefs[(None, grp.id, s["UMAYCHEM"].id)].rank == 1           # grubu olan kart → gruba
    assert prefs[(its["AKTİF KÖMÜR"].id, None, s["TATLIDİLİMLER"].id)].rank == 1
    assert (its["AKTİF KÖMÜR"].id, None, s["BEFCHEM"].id) not in prefs     # H
    sq = its["SQUALANE"].id
    assert prefs[(sq, None, s["UMAYCHEM"].id)].rank == 1 and prefs[(sq, None, s["UMAYCHEM"].id)].created_by == "Songül"
    assert prefs[(sq, None, s["BEFCHEM"].id)].rank == 2                # mevcut korunur, arkasına
    assert all(p.preference == "preferred" for p in prefs.values())
    sups = {x.name: x for x in db_session.query(Supplier).all()}
    assert sups["HAMMADDESEPETİ"].purchase_status == "phase_out"
    assert sups["HAMMADDESEPETİ"].status_reason == lab.PHASE_OUT_REASON
    assert sups["ROSECE"].purchase_status == "normal"                   # H → dokunulmaz
    assert sups["TATLIDİLİMLER"].purchase_status == "normal"            # boş → dokunulmaz
    acts = [a.action for a in db_session.query(AdminAuditLog).all()]
    assert acts.count("material_pref.create") == 5 and acts.count("supplier.status") == 1
    assert lab.AUDIT_PREFS in acts
    # İkinci uygulama: zaten var → atlanır, yeni satır yok
    pp = lab.apply_prefs(db_session, lab.read_approvals(path), d)
    assert pp["counts"]["tercih_yazilacak"] == 0 and pp["counts"]["bitirilecek"] == 0
    db_session.expire_all()
    assert db_session.query(MaterialSupplierPref).count() == 6


def test_apply_prefs_shifted_row_or_bad_value_writes_nothing(db_session: Session, tmp_path):
    _its, _s, _grp, d = _pref_setup(db_session)
    path = lab.build_xlsx(lab.plan(db_session, d), tmp_path / "onay.xlsx")
    _fill(path, "Tercih önerisi", lab.APPROVE_COL, {"A1-08-S1": "E", "A1-09-S1": "belki"})
    appr = lab.read_approvals(path)
    assert any("belki" in e for e in appr["errors"])
    pp = lab.apply_prefs(db_session, appr, d)
    assert pp["applied"] is False
    db_session.expire_all()
    assert db_session.query(MaterialSupplierPref).count() == 1
    # Satır kayması: malzeme metni plana uymuyor → hata
    wb = load_workbook(path)
    ws = wb["Tercih önerisi"]
    head = [c.value for c in ws[1]]
    ws.cell(2, head.index("Malzeme (not)") + 1, "BAŞKA MALZEME")
    ws.cell(3, head.index(lab.APPROVE_COL) + 1, "H")
    wb.save(path)
    pp = lab.apply_prefs(db_session, lab.read_approvals(path), d)
    assert pp["applied"] is False and any("kaymış" in e for e in pp["errors"])


def test_cli_dry_run_writes_nothing_and_xlsx(db_session: Session, tmp_path, capsys):
    _items(db_session, ("GLİSERİN", "g"))
    _sups(db_session, "UMAYCHEM")
    db_session.commit()
    f = tmp_path / "veri.json"
    f.write_text(json.dumps(data(rec("A1-09-S1", "GLİSERİN", "UMAYCHEM", 4.52, secili=True, guven="belirsiz"),
                                 rec("A1-20-S1", "GLİSERİN", "BEFCHEM", 5.0)), ensure_ascii=False),
                 encoding="utf-8")
    x = tmp_path / "k.xlsx"
    assert lab.main(["--data", str(f), "--xlsx", str(x)]) == 0
    out = capsys.readouterr().out
    assert "KURU ÇALIŞTIRMA" in out and "BEFCHEM" in out and x.exists()
    assert _prices(db_session) == []
    # Fiyat yazımı onaylı Excel ister
    assert lab.main(["--data", str(f), "--commit"]) == 2
    assert lab.main(["--data", str(f), "--create-suppliers"]) == 2
    assert _prices(db_session) == []
    capsys.readouterr()
    assert lab.main(["--data", str(f), "--onayli", str(x)]) == 0           # kuru, onaylı denetim
    assert "KURU" in capsys.readouterr().out and _prices(db_session) == []
    assert lab.main(["--data", str(f), "--onayli", str(x), "--commit"]) == 0
    assert "YAZILDI" in capsys.readouterr().out
    assert len(_lab_prices(db_session)) == 1
    # İşlenmemiş düzeltme → çıkış 2, yazılan satırlar değişmez
    _fill(x, "Belirsiz okumalar", lab.FIX_VALUE_COL, {"A1-09-S1": "4,25"})
    assert lab.main(["--data", str(f), "--onayli", str(x), "--commit"]) == 2
    assert "DURDURULDU" in capsys.readouterr().out
    assert [p.unit_price for p in _lab_prices(db_session)] == [4.52]
    assert lab.main(["--data", str(f), "--apply-prefs", str(tmp_path / "yok.xlsx")]) == 2


def test_cli_partial_import_requires_workbook_and_revalidates_selected_ids(db_session: Session, tmp_path, capsys):
    its = _items(db_session, ("X", "g"), ("Y", "g"))
    sups = _sups(db_session, "UMAYCHEM")
    db_session.commit()
    d = data(rec("A1-01-S1", "X", "UMAYCHEM", 1.0, guven="belirsiz"),
             rec("C-s12-1", "X", "UMAYCHEM", 2.0, tur="proforma", tarih="2026-07-01"),
             rec("A1-02-S1", "Y", "UMAYCHEM", 3.0))
    f, selection_file, workbook = tmp_path / "source.json", tmp_path / "selection.json", tmp_path / "review.xlsx"
    f.write_text(json.dumps(d), encoding="utf-8")
    selected = {"id": "C-s12-1", "item_id": its["X"].id, "supplier_id": sups["UMAYCHEM"].id}
    selection_file.write_text(json.dumps([selected]), encoding="utf-8")
    argv = ["--data", str(f), "--record-ids", str(selection_file)]
    assert lab.main(argv + ["--xlsx", str(workbook)]) == 0
    assert "YAZILACAK seçili" in capsys.readouterr().out and _prices(db_session) == []
    assert lab.main(argv + ["--commit"]) == 2
    assert "onaylı kontrol Excel'i" in capsys.readouterr().out
    assert lab.main(argv + ["--apply-prefs", str(workbook)]) == 2
    assert lab.main(argv + ["--onayli", str(workbook), "--create-suppliers"]) == 2
    assert _prices(db_session) == [] and db_session.query(AdminAuditLog).count() == 0
    # Manifest, tam kaynak içindeki işlenmemiş Excel düzeltmesini atlatamaz.
    _fill(workbook, "Belirsiz okumalar", lab.FIX_VALUE_COL, {"A1-01-S1": "7,15"})
    assert lab.main(argv + ["--onayli", str(workbook), "--commit"]) == 2
    assert _prices(db_session) == [] and db_session.query(AdminAuditLog).count() == 0
    _fill(workbook, "Belirsiz okumalar", lab.FIX_VALUE_COL, {"A1-01-S1": "—"})
    assert lab.main(argv + ["--onayli", str(workbook), "--commit"]) == 0
    assert [(p.item_id, p.unit_price, p.source) for p in _prices(db_session)] == [(its["X"].id, 2.0, "proforma")]
    # Daraltılmış veriyle eski lab satırını yeniden kazanan yapma: tam planda ezilmiş ID reddedilir.
    selection_file.write_text(json.dumps([{**selected, "id": "A1-01-S1"}]), encoding="utf-8")
    assert lab.main(argv + ["--onayli", str(workbook), "--commit"]) == 2
    assert "yazılacak kazanan değil" in capsys.readouterr().out
    assert [p.unit_price for p in _prices(db_session)] == [2.0]
    assert db_session.query(AdminAuditLog).count() == 1


def test_partial_workbook_cannot_run_full_import_or_another_selection(db_session: Session, tmp_path):
    its = _items(db_session, ("X", "g"), ("Y", "g"))
    sups = _sups(db_session, "UMAYCHEM")
    db_session.add_all([
        SupplierPrice(item_id=its[name].id, supplier_id=sups["UMAYCHEM"].id, supplier_name="UMAYCHEM",
                      unit_price=9.0, currency="EUR", price_unit="kg", source="lab_notu",
                      source_label=lab.LABEL_PREFIX, domain="cosmetics") for name in ("X", "Y")])
    db_session.commit()
    d = data(rec("A1-01-S1", "X", "UMAYCHEM", 1.0, secili=True), rec("A1-02-S1", "Y", "UMAYCHEM", 2.0))
    selection = [{"id": "A1-01-S1", "item_id": its["X"].id, "supplier_id": sups["UMAYCHEM"].id}]
    other = [{"id": "A1-02-S1", "item_id": its["Y"].id, "supplier_id": sups["UMAYCHEM"].id}]
    source = tmp_path / "source.json"
    source.write_text(json.dumps(d), encoding="utf-8")
    path = lab.build_xlsx(lab.plan(db_session, d), tmp_path / "partial.xlsx")
    wb = load_workbook(path)
    ws = wb.create_sheet("Aktarım seçimi")
    ws.append(["Kayıt", "item_id", "supplier_id"])
    ws.append([selection[0]["id"], selection[0]["item_id"], selection[0]["supplier_id"]])
    wb.save(path)
    _fill(path, "Tercih önerisi", lab.APPROVE_COL, {"A1-01-S1": "E"})
    approvals = lab.read_approvals(path)
    assert approvals["record_manifest"] == selection and approvals["record_manifest_errors"] == []
    before = [(p.id, p.item_id, p.unit_price) for p in _prices(db_session)]
    assert lab.main(["--data", str(source), "--onayli", str(path)]) == 2
    assert lab.main(["--data", str(source), "--onayli", str(path), "--commit"]) == 2
    no_flag = lab.apply(db_session, d, approvals=approvals)
    assert not no_flag["applied"] and any("--record-ids manifesti şart" in e for e in no_flag["errors"])
    mismatch = lab.apply(db_session, d, approvals=approvals, record_selection=other)
    assert not mismatch["applied"] and any("seçimi aynı değil" in e for e in mismatch["errors"])
    assert [(p.id, p.item_id, p.unit_price) for p in _prices(db_session)] == before
    assert db_session.query(AdminAuditLog).count() == 0 and db_session.query(MaterialSupplierPref).count() == 0
    matched = lab.apply(db_session, d, approvals=approvals, record_selection=selection)
    assert matched["applied"] and matched["written"] == 1 and matched["deleted"] == 1
    assert sorted((p.item_id, p.unit_price) for p in _prices(db_session)) == [(its["X"].id, 1.0), (its["Y"].id, 9.0)]
    # Tercih aktarımı fiyat metadata'sı için dış manifest istemez.
    prefs = lab.apply_prefs(db_session, approvals, d)
    assert prefs["applied"] and db_session.query(MaterialSupplierPref).count() == 1
    wb = load_workbook(path)
    wb["Aktarım seçimi"].cell(1, 2, "wrong_column")
    wb.save(path)
    broken = lab.read_approvals(path)
    assert broken["record_manifest_errors"] and not broken["errors"]
    before = [(p.id, p.item_id, p.unit_price) for p in _prices(db_session)]
    assert not lab.apply(db_session, d, approvals=broken, record_selection=selection)["applied"]
    assert [(p.id, p.item_id, p.unit_price) for p in _prices(db_session)] == before
    assert lab.apply_prefs(db_session, broken, d)["applied"]


# ─── İnceleme düzeltmeleri (08.10.2026) ─────────────────────────────────────

def test_struck_and_price_list_records_not_written_nor_competing(db_session: Session, tmp_path):
    """Üstü çizili teklif (CLOVE OIL proforması, Fındık Naturalya) ve s.11
    Doalin fiyat listesi yazılmaz; çizili belge 'belge > lab' önceliğiyle
    sarı lab satırını EZMEZ.  Doalin yine tercih önerisinde alternatiftir."""
    its = _items(db_session, ("KARANFİL TOMURCUĞU UÇUCU YAGI", "ml"), ("BERGAMOT UÇUCU YAĞI", "ml"),
                 ("FINDIK YAGI", "g"))
    _sups(db_session, "NATURALYA", "DOALİNN", "SURYA KİMYA")
    db_session.commit()
    d = data(
        rec("A2-36-S1", "KARANFİL TOMURCUĞU UÇUCU YAGI", "NATURALYA", 37.84, secili=True, birim="ml"),
        rec("C-s16-4", "CLOVE OIL", "NATURALYA KİMYA", 42.0, tur="proforma", tarih="2026-06-25", cur="USD",
            pkg=1.0, sayfa=16, note="satır üstü çizili; lab tablosu karşılığı: KARANFİL TOMURCUĞU UÇUCU YAGI (A2-36)"),
        rec("A2-16-S2", "FINDIK YAGI", "NATURALYA", 17.2),
        rec("A2-16-S3", "FINDIK YAGI", "SURYA KİMYA", 12.9, secili=True),
        rec("A2-28-S1", "BERGAMOT UÇUCU YAĞI", "NATURALYA", 36.12, secili=True, birim="ml"),
        rec("C-s11-1", "Bergamot Yağı", "Doalin", 175.0, tur="fiyat_listesi", tarih=None, cur="USD", unit="l",
            pkg=1.0, guven="belirsiz", sayfa=11, note="lab tablosu karşılığı: BERGAMOT UÇUCU YAĞI (A2-28)"),
    )
    d["kayitlar"][1]["cizili"] = True
    d["kayitlar"][2]["cizili"] = True
    p = lab.apply(db_session, d)
    r = _by_id(p)
    assert r["C-s16-4"]["status"] == "cizili" and r["A2-16-S2"]["status"] == "cizili"
    assert r["A2-36-S1"]["status"] == "yazilacak" and not r["A2-36-S1"]["overridden"]
    assert r["C-s11-1"]["status"] == "kapsam_disi" and p["counts"]["kapsam_disi"] == 1
    kar, ber, fin = (its["KARANFİL TOMURCUĞU UÇUCU YAGI"].id, its["BERGAMOT UÇUCU YAĞI"].id,
                     its["FINDIK YAGI"].id)
    rows = {(x.item_id, x.supplier_name): x for x in _lab_prices(db_session)}
    assert set(rows) == {(kar, "NATURALYA"), (fin, "SURYA KİMYA"), (ber, "NATURALYA")}
    assert rows[(kar, "NATURALYA")].unit_price == 37.84 and rows[(kar, "NATURALYA")].source == "lab_notu"
    alt = next(x for x in p["prefs"] if x["id"] == "C-s11-1")
    assert alt["kind"] == "nottan alternatif" and alt["item"]["id"] == ber and alt["sup"]["name"] == "DOALİNN"
    wb = load_workbook(lab.build_xlsx(p, tmp_path / "k.xlsx"))
    head = [c.value for c in wb["Fiyatlar"][1]]
    fiy = {row[0]: row for row in wb["Fiyatlar"].iter_rows(min_row=2, values_only=True)}
    assert "üstü çizili" in fiy["C-s16-4"][head.index("Yazılacak mı / neden")]
    assert "fiyat listesi" in fiy["C-s11-1"][head.index("Yazılacak mı / neden")]
    bel = wb["Belirsiz okumalar"]
    bh = [c.value for c in bel[1]]
    brow = next(x for x in bel.iter_rows(min_row=2, values_only=True) if x[0] == "C-s11-1")
    assert brow[bh.index(lab.FIX_VALUE_COL)] == "—"


def test_document_name_vs_hint_conflict_is_ambiguous_and_alt_scope_from_row(db_session: Session):
    """TR/EN mükerrer kart: belgenin adı BERGAMOT YAĞI kartına birebir uyuyor
    ama ipucu A2-28 (BERGAMOT UÇUCU YAĞI).  İpucu satırında AYNI firmanın
    kaydı varsa (Uludağ faturası ↔ A2-28-S2 Uludağ) belge o karta gider —
    (kart, firma) başına tek kayıt korunur, fatura lab satırını ezer.  Aynı
    firmanın kaydı yoksa (KRK) çelişki → belirsiz.  Nottan alternatif tercih
    lab satırının kartına bağlanır."""
    its = _items(db_session, ("BERGAMOT UÇUCU YAĞI", "ml"), ("BERGAMOT YAĞI", "ml"))
    _sups(db_session, "NATURALYA", "ULUDAĞ HERBAL", "DOALİNN", "KRK GIDA")
    db_session.commit()
    p = lab.plan(db_session, data(
        rec("A2-28-S1", "BERGAMOT UÇUCU YAĞI", "NATURALYA", 36.12, secili=True, birim="ml"),
        rec("A2-28-S2", "BERGAMOT UÇUCU YAĞI", "ULUDAĞ HERBAL", 44.99, birim="ml"),
        rec("C-s9-2", "BERGAMOT YAĞI 1LT", "ULUDAĞ AGRO", 11054.17, tur="fatura", tarih="2026-06-26", cur="TRY",
            unit="l", pkg=1.0, note="lab tablosu karşılığı: BERGAMOT UÇUCU YAĞI (A2-28)"),
        rec("C-s9-9", "BERGAMOT YAĞI 1LT", "KRK GIDA", 9000.0, tur="fatura", tarih="2026-06-26", cur="TRY",
            unit="l", pkg=1.0, note="lab tablosu karşılığı: BERGAMOT UÇUCU YAĞI (A2-28)"),
        rec("C-s11-1", "Bergamot Yağı", "Doalin", 175.0, tur="fiyat_listesi", tarih=None, cur="USD", unit="l",
            pkg=1.0, note="lab tablosu karşılığı: BERGAMOT UÇUCU YAĞI (A2-28)"),
    ))
    r = _by_id(p)
    uc, dup = its["BERGAMOT UÇUCU YAĞI"].id, its["BERGAMOT YAĞI"].id
    assert r["C-s9-2"]["item"]["id"] == uc and r["C-s9-2"]["mat"]["how"] == f"{lab.HOW_HINT_SAME} A2-28-S2"
    assert r["C-s9-2"]["status"] == "yazilacak" and "başka karta uyuyor" in "; ".join(r["C-s9-2"]["flags"])
    assert r["A2-28-S2"]["status"] == "ezildi" and r["A2-28-S2"]["flags"][-1] == "kazanan: C-s9-2"
    assert r["C-s9-9"]["status"] == "belirsiz" and "ipucu çelişiyor" in "; ".join(r["C-s9-9"]["flags"])
    assert set(r["C-s9-9"]["mat"]["candidates"]) == {uc, dup}
    alt = next(x for x in p["prefs"] if x["id"] == "C-s11-1")
    assert alt["item"]["id"] == uc and alt["scope"] == ("item", uc)
    # İpucu adın kartını gösteriyorsa (normal durum) birebir ad eşleşmesi aynen
    p = lab.plan(db_session, data(
        rec("A2-28-S1", "BERGAMOT YAĞI", "NATURALYA", 36.12, secili=True, birim="ml"),
        rec("C-s9-2", "BERGAMOT YAĞI 1LT", "ULUDAĞ AGRO", 11054.17, tur="fatura", tarih="2026-06-26", cur="TRY",
            unit="l", pkg=1.0, note="lab tablosu karşılığı: BERGAMOT YAĞI (A2-28)")))
    assert _by_id(p)["C-s9-2"]["item"]["id"] == dup and _by_id(p)["C-s9-2"]["mat"]["how"] == "ad"


def test_approval_cells_accept_only_e_or_h(db_session: Session, tmp_path):
    """'X' (Türkçede çoğu zaman hayır) ve '✓' gibi işaretler hata; Excel
    listesi de liste dışı girişi reddeder (showErrorMessage + stop)."""
    for v in ("X", "x", "✓", "+", "1", "0", "Y", "yes", "N", "-"):
        with pytest.raises(ValueError):
            lab._yes_no(v)
    assert lab._yes_no("e") is True and lab._yes_no(" Evet ") is True and lab._yes_no("E.") is True
    assert lab._yes_no("hayır") is False and lab._yes_no(None) is None and lab._yes_no("  ") is None
    _its, _s, _grp, d = _pref_setup(db_session)
    path = lab.build_xlsx(lab.plan(db_session, d), tmp_path / "onay.xlsx")
    _fill(path, "Tercih önerisi", lab.APPROVE_COL, {"A1-08-S1": "✓", "A1-09-S1": "X", "A1-34-S2": "E"})
    appr = lab.read_approvals(path)
    assert len(appr["errors"]) == 2 and "A1-08-S1" not in appr["prefs"] and "A1-09-S1" not in appr["prefs"]
    pp = lab.apply_prefs(db_session, appr, d)
    assert pp["applied"] is False
    db_session.expire_all()
    assert db_session.query(MaterialSupplierPref).count() == 1
    dv = load_workbook(path)["Tercih önerisi"].data_validations.dataValidation[0]
    assert dv.showErrorMessage and dv.errorStyle == "stop"


def test_unprocessed_corrections_block_price_and_pref_writes(db_session: Session, tmp_path):
    """'Belirsiz okumalar → Doğru değer' ve 'Eşleşmeyenler → Doğru IMS kartı'
    otomatik işlenmez; veri dosyasına aktarılmadan HİÇBİR ŞEY yazılmaz
    (2,15 yazılıp 7,15 düzeltmesi sessizce kaybolmasın)."""
    its = _items(db_session, ("COCO GLUCOSİDE", "g"), ("NAR ÇEKİRDEĞİ YAĞI", "g"))
    _sups(db_session, "UMAYCHEM")
    db_session.commit()
    d = data(rec("A1-06-S1", "COCO GLUCOSİDE", "UMAYCHEM", 2.15, secili=True, guven="belirsiz", pkg=220.0),
             rec("A2-01-S2", "NAR TOHUMU ÖZÜ", "UMAYCHEM", 18.57))
    path = lab.build_xlsx(lab.plan(db_session, d), tmp_path / "onay.xlsx")
    _fill(path, "Belirsiz okumalar", lab.FIX_VALUE_COL, {"A1-06-S1": "7,15"})
    _fill(path, "Eşleşmeyenler", lab.FIX_CARD_COL, {"A2-01-S2": "NAR ÇEKİRDEĞİ YAĞI"})
    appr = lab.read_approvals(path)
    assert not appr["errors"] and len(appr["fixes"]) == 2
    p = lab.apply(db_session, d, approvals=appr)
    assert p["applied"] is False and len(p["errors"]) == 2 and _lab_prices(db_session) == []
    assert any("7,15" in e for e in p["errors"]) and any("elle_eslesme" in e for e in p["errors"])
    assert lab.apply_prefs(db_session, appr, d)["applied"] is False
    # Düzeltmeler veri dosyasına işlendi → düzeltilmiş değerle yazılır
    d["kayitlar"][0].update(fiyat=7.15, guven="kesin")
    d["elle_eslesme"] = {"kart": {"A2-01": its["NAR ÇEKİRDEĞİ YAĞI"].id}}
    p = lab.apply(db_session, d, approvals=appr)
    assert p["applied"] and p["errors"] == []
    assert sorted(x.unit_price for x in _lab_prices(db_session)) == [7.15, 18.57]


@pytest.mark.parametrize("correction_kind", ["id", "name"])
def test_correct_card_must_match_excel_before_writing(db_session: Session, tmp_path, correction_kind):
    """Bir override'ın varlığı yetmez: yanlış karta aktarılmış Excel düzeltmesi
    fiyat ve tercih yazımını durdurur; doğru no veya adla eşleşince açılır."""
    its = _items(db_session, ("NAR ÇEKİRDEĞİ YAĞI", "g"), ("GLİSERİN", "g"))
    _sups(db_session, "UMAYCHEM")
    db_session.commit()
    rid = "A2-01-S1"
    d = data(rec(rid, "NAR TOHUMU ÖZÜ", "UMAYCHEM", 18.57, secili=True))
    path = lab.build_xlsx(lab.plan(db_session, d), tmp_path / "onay.xlsx")
    target = its["NAR ÇEKİRDEĞİ YAĞI"]
    value = target.id if correction_kind == "id" else target.name
    _fill(path, "Eşleşmeyenler", lab.FIX_CARD_COL, {rid: value})
    _fill(path, "Tercih önerisi", lab.APPROVE_COL, {rid: "E"})
    approvals = lab.read_approvals(path)
    d["elle_eslesme"] = {"kart": {"A2-01": its["GLİSERİN"].id}}
    p = lab.apply(db_session, d, approvals=approvals)
    assert p["applied"] is False and any("kartla aynı değil" in e for e in p["errors"])
    assert lab.apply_prefs(db_session, approvals, d)["applied"] is False
    assert _lab_prices(db_session) == [] and db_session.query(MaterialSupplierPref).count() == 0
    d["elle_eslesme"]["kart"]["A2-01"] = target.id
    p = lab.apply(db_session, d, approvals=approvals)
    assert p["applied"] and [x.item_id for x in _lab_prices(db_session)] == [target.id]


def test_pref_and_phase_out_conflict_is_error(db_session: Session, tmp_path):
    """Aynı firma hem tercih (E) hem bitirilecek (E) ya da tercih edilen firma
    zaten bitirilecek → hata, hiçbir şey yazılmaz (bitirilecek tercihi
    geçersiz kılar)."""
    _its, _s, _grp, d = _pref_setup(db_session)
    path = lab.build_xlsx(lab.plan(db_session, d), tmp_path / "onay.xlsx")
    _fill(path, "Tercih önerisi", lab.APPROVE_COL, {"A2-13-S1": "E", "A1-09-S1": "E"})
    small_ids = {x["sup"]["name"]: x["id"] for x in lab.plan(db_session, d)["small"]}
    _fill(path, "Küçük satıcı önerisi", lab.PHASE_COL, {small_ids["TATLIDİLİMLER"]: "E"})
    pp = lab.apply_prefs(db_session, lab.read_approvals(path), d)
    assert pp["applied"] is False
    assert any("TATLIDİLİMLER" in e and "bitirilecek" in e for e in pp["errors"])
    db_session.expire_all()
    assert db_session.query(MaterialSupplierPref).count() == 1
    t = db_session.query(Supplier).filter(Supplier.name == "TATLIDİLİMLER").one()
    assert t.purchase_status == "normal"
    _fill(path, "Küçük satıcı önerisi", lab.PHASE_COL, {small_ids["TATLIDİLİMLER"]: ""})
    t.purchase_status = "phase_out"
    db_session.commit()
    pp = lab.plan_prefs(db_session, lab.read_approvals(path), d)
    assert any("zaten 'bitirilecek'" in e for e in pp["errors"])


def test_pref_rows_vat_label_and_uludag_cheaper_warning(db_session: Session, tmp_path):
    _items(db_session, ("VANİLYA UÇUCU YAĞI", "ml"), ("ITIR HİDROSOLÜ", "g"))
    _sups(db_session, "PHARMATERM", "ULUDAĞ HERBAL")
    db_session.commit()
    p = lab.plan(db_session, data(
        rec("A2-40-S1", "VANİLYA UÇUCU YAĞI", "ULUDAĞ HERBAL", 47.58, birim="ml"),
        rec("A2-40-S2", "VANİLYA UÇUCU YAĞI", "PHARMATERM İLAÇ", 82.56, kdv=True, oran=20.0, secili=True,
            birim="ml"),
        rec("A1-27-S1", "ITIR HİDROSOLÜ", "ULUDAĞ HERBAL", 5.65),
        rec("A1-27-S2", "ITIR HİDROSOLÜ", "PHARMATERM İLAÇ", 7.95, secili=True),
    ))
    ws = load_workbook(lab.build_xlsx(p, tmp_path / "k.xlsx"))["Tercih önerisi"]
    head = [c.value for c in ws[1]]
    rows = {r[0]: r for r in ws.iter_rows(min_row=2, values_only=True)}
    assert rows["A2-40-S2"][head.index("Fiyat")] == "82,56 EUR/kg (KDV %20 dahil; net 68,8)"
    assert rows["A1-27-S2"][head.index("Fiyat")] == "7,95 EUR/kg"
    w = rows["A1-27-S2"][head.index("Uyarı")]
    assert "Uludağ ucuz olanları alacağız" in w and "5,65 EUR ↔ sarı 7,95 EUR" in w
    assert "Uludağ" not in (rows["A2-40-S2"][head.index("Uyarı")] or "")      # listede olmayan satır
    assert rows["A1-27-S1"][head.index("Öneri türü")] == "nottan alternatif"   # s.17 yeşil Uludağ


# ─── Birden çok aday kart · takma ad · belge ipucu (08.10.2026) ──────────────

def _card(db, name, unit="g", sup=None, stock=0.0, **kw):
    """Aynı adlı birden çok kart (lab her tedarikçiye ayrı kart tutar)."""
    it = Item(name=name, unit=unit, category=kw.pop("category", "Hammadde"),
              supplier_id=sup.id if sup is not None else None, current_stock=stock, **kw)
    db.add(it)
    db.flush()
    return it


def _recipe(db, *items, active=True):
    rc = Recipe(name=f"R{len(items)}{'' if active else ' pasif'}", is_active=active)
    db.add(rc)
    db.flush()
    for it in items:
        db.add(RecipeIngredient(recipe_id=rc.id, item_id=it.id, quantity=1.0, unit=it.unit))
    db.flush()
    return rc


def _group(db, name, *items):
    g = MaterialGroup(name=name, domain="cosmetics")
    db.add(g)
    db.flush()
    for it in items:
        it.material_group_id = g.id
    db.flush()
    return g


def test_rule_a_suppliers_own_card(db_session: Session):
    """(a) Adaylardan kart tedarikçisi teklifinkiyle aynı TEK kart — anahtar
    uzayında (HAMMADDE SEPETİ = HAMMADDESEPETİ, ULUDAĞ AGRO = ULUDAG HERBAL
    kartı); aynı grupta tedarikçinin kendi kartı, ad başka karta uysa da seçilir."""
    s = _sups(db_session, "UMAYCHEM", "HAMMADDESEPETİ", "ULUDAG HERBAL", "PHARMATERM", "DOALİNN", "NATURALYA")
    lg_u = _card(db_session, "LAURYL GLUCOSİDE", sup=s["UMAYCHEM"], stock=218020)
    lg_h = _card(db_session, "LAURYL GLUCOSİDE", sup=s["HAMMADDESEPETİ"], stock=1526)
    kb_u = _card(db_session, "KAKAO BUTTER", sup=s["ULUDAG HERBAL"], stock=1336)
    kb_p = _card(db_session, "KAKAO BUTTER", sup=s["PHARMATERM"], stock=4581)
    jn_d = _card(db_session, "JAPON NANESİ UÇUCU YAGI", "ml", sup=s["DOALİNN"], stock=0.4)
    jn_n = _card(db_session, "JAPON NANESİ", "ml", sup=s["NATURALYA"], stock=956)
    _group(db_session, "KAKAO BUTTER", kb_u, kb_p)
    _group(db_session, "JAPON NANESİ", jn_d, jn_n)
    _recipe(db_session, lg_u, lg_h, kb_u, kb_p, jn_n)
    db_session.commit()
    p = lab.plan(db_session, data(
        rec("A1-05-S1", "LAURYL GLUCOSİDE", "UMAYCHEM", 1.93, secili=True),
        rec("A1-05-S2", "LAURYL GLUCOSİDE", "HAMMADDE SEPETİ", 9.0),
        rec("A1-28-S1", "KAKAO BUTTER", "ULUDAĞ AGRO", 39.56),
        rec("A1-28-S2", "KAKAO BUTTER", "PHARMATERM İLAÇ", 40.13, secili=True),
        rec("A2-27-S1", "JAPON NANESİ UÇUCU YAGI", "NATURALYA", 38.7, birim="ml"),
        rec("A2-27-S2", "JAPON NANESİ UÇUCU YAGI", "DOALIN", 90.0, birim="ml"),
        rec("A2-27-S3", "JAPON NANESİ UÇUCU YAGI", "SURYA KİMYA", 50.0, birim="ml"),
    ))
    r = _by_id(p)
    for rid, it in (("A1-05-S1", lg_u), ("A1-05-S2", lg_h), ("A1-28-S1", kb_u), ("A1-28-S2", kb_p)):
        assert r[rid]["item"]["id"] == it.id, rid
        assert r[rid]["mat"]["how"] == lab.HOW_OWN and r[rid]["mat"]["rule"] == "a", rid
        assert r[rid]["mat"]["guven"] == "kesin" and r[rid]["status"] == "yazilacak", rid
    # Ad Doalin kartına uyuyor; Naturalya'nın aynı gruptaki kendi kartı seçilir
    assert r["A2-27-S1"]["item"]["id"] == jn_n.id and r["A2-27-S1"]["mat"]["rule"] == "a"
    assert "aynı malzeme grubu" in r["A2-27-S1"]["mat"]["how"]
    assert r["A2-27-S2"]["item"]["id"] == jn_d.id and r["A2-27-S2"]["mat"]["how"] == "ad"
    assert r["A2-27-S3"]["item"]["id"] == jn_d.id and r["A2-27-S3"]["mat"]["how"] == "ad"   # kartı yok
    assert p["counts"]["kural_a"] == 5 and p["counts"]["malzeme_belirsiz"] == 0
    # Kural yazılan nota da düşer
    assert "Kart eşleşmesi: tedarikçinin kendi kartı" in r["A1-05-S2"]["write_note"]


def test_rule_b_group_main_card(db_session: Session):
    """(b) Tedarikçinin kartı yok, adaylar aynı gruptaysa grubun ana kartı:
    aktif reçetede kullanılan TEK kart (stoğu düşük olsa da); birden çok
    reçete kartında / hiç yoksa stoğu en yüksek (kg/l ×1000).  Pasif reçete
    sayılmaz.  Raporda "grup ana kartına bağlandı"."""
    s = _sups(db_session, "UMAYCHEM", "SABUNARI", "BİLİNMEYEN", "ULUDAG HERBAL", "KRK GIDA")
    dp_u = _card(db_session, "D-PANTHENOL", sup=s["UMAYCHEM"], stock=2043)
    dp_s = _card(db_session, "D-PANTHENOL", sup=s["SABUNARI"], stock=5615)
    lv_b = _card(db_session, "LAVANTA HİDROSOLÜ", sup=s["BİLİNMEYEN"], stock=14448)
    lv_u = _card(db_session, "LAVANTA HİDROSOLÜ", sup=s["ULUDAG HERBAL"], stock=27.8)
    tb_g = _card(db_session, "TATLI BADEM YAGI", "g", sup=s["KRK GIDA"], stock=900)
    tb_k = _card(db_session, "TATLI BADEM YAGI", "kg", sup=s["KRK GIDA"], stock=1)       # 1000 g
    _group(db_session, "D-PANTHENOL", dp_u, dp_s)
    _group(db_session, "LAVANTA HİDROSOLÜ", lv_b, lv_u)
    _group(db_session, "TATLI BADEM YAĞI", tb_g, tb_k)
    _recipe(db_session, dp_u, lv_b, lv_u)
    _recipe(db_session, dp_s, tb_g, active=False)                    # pasif reçete sayılmaz
    db_session.commit()
    p = lab.plan(db_session, data(
        rec("A1-20-S1", "D-PANTHENOL", "ATAMAN KİMYA", 11.63, secili=True),
        rec("A1-04-S1", "LAVANTA HİDROSOLÜ", "ULUDAĞ HERBAL", 1.88),
        rec("A1-04-S2", "LAVANTA HİDROSOLÜ", "BEFCHEM", 9.0),
        rec("A1-26-S1", "TATLI BADEM YAGI", "NATURALYA", 12.9),
        rec("A1-26-S2", "TATLI BADEM YAGI", "KRK GIDA", 13.0),           # 2 kendi kartı → (b)
    ))
    r = _by_id(p)
    assert r["A1-20-S1"]["item"]["id"] == dp_u.id                         # tek reçete kartı > stok
    assert r["A1-04-S1"]["item"]["id"] == lv_u.id and r["A1-04-S1"]["mat"]["rule"] == "a"
    assert r["A1-04-S2"]["item"]["id"] == lv_b.id                         # 2 reçete kartı → stok
    assert r["A1-26-S1"]["item"]["id"] == tb_k.id                         # 1 kg > 900 g
    assert r["A1-26-S2"]["item"]["id"] == tb_k.id
    for rid in ("A1-20-S1", "A1-04-S2", "A1-26-S1", "A1-26-S2"):
        m = r[rid]["mat"]
        assert m["rule"] == "b" and m["how"] == lab.HOW_GROUP_MAIN and m["guven"] == "kesin", rid
        assert any("grup ana kartına bağlandı" in f for f in r[rid]["flags"]), rid
    assert "tedarikçinin kendi kartı yok" in r["A1-20-S1"]["flags"][0]
    assert "tedarikçinin birden çok kartı var" in r["A1-26-S2"]["flags"][0]
    assert "hiçbiri aktif reçetede değil" in r["A1-26-S1"]["flags"][0]
    assert p["counts"]["kural_b"] == 4 and p["counts"]["malzeme_belirsiz"] == 0


def test_rule_c_single_recipe_card_flagged_and_d_lists_candidates(db_session: Session, tmp_path):
    """(c) Tedarikçinin kartı yok, adaylar tek grupta değil, yalnız biri aktif
    reçetede → o kart, eşleşme güveni BELİRSİZ: Excel Fiyatlar'da 'Eşleşme
    güveni', 'Eşleşmeyenler'de doğrulama satırı, Tercih önerisinde uyarı.
    (d) Aksi hâlde belirsiz, yazılmaz; adaylar no · ad · tedarikçi · stok ·
    reçetede mi.  Grubun dışında aday varsa (b) uygulanmaz."""
    s = _sups(db_session, "KRK GIDA", "DOGASA", "DOALİNN", "MSA", "NATURALYA", "ULUDAĞ HERBAL", "SURYA KİMYA")
    hc = _card(db_session, "HİNDİSTAN CEVİZİ YAGI", sup=s["KRK GIDA"], stock=3857.8)
    hc_d = _card(db_session, "HİNDİSTAN CEVİZİ YAĞI", "ml", sup=s["DOGASA"], stock=20)
    bb_d = _card(db_session, "BİBERİYE UÇUCU YAĞI", "ml", sup=s["DOALİNN"], stock=34.844)
    bb_g = _card(db_session, "BİBERİYE UÇUCU YAĞI", "ml", sup=s["DOGASA"], stock=20)
    gn_m = _card(db_session, "GİNSENG EKSTRAKTI", "ml", sup=s["MSA"], stock=10.8)
    gn_n = _card(db_session, "GİNSENG EKSTRAKTI", "g", sup=s["NATURALYA"], stock=4983.5)
    gn_k = _card(db_session, "GİNSENG EKSTRAKTI", "kg", sup=s["NATURALYA"], stock=5)
    gn_d = _card(db_session, "GİNSENG EKSTRAKTI", "ml", sup=s["DOGASA"], stock=0)
    _group(db_session, "GİNSENG EKSTRAKTI", gn_m, gn_n, gn_k)            # gn_d grupta değil
    _recipe(db_session, hc, bb_d, bb_g, gn_n, gn_k)
    db_session.commit()
    p = lab.plan(db_session, data(
        rec("A1-22-S1", "HİNDİSTAN CEVİZİ YAGI", "ULUDAĞ HERBAL", 11.96),
        rec("A1-22-S2", "HİNDİSTAN CEVİZİ YAGI", "NATURALYA", 9.46, secili=True),
        rec("A2-41-S1", "BİBERİYE UÇUCU YAĞI", "NATURALYA", 36.12, secili=True, birim="ml"),
        rec("A2-29-S1", "GİNSENG EKSTRAKTI", "SURYA KİMYA", 30.1, secili=True, birim="ml"),
    ))
    r = _by_id(p)
    for rid in ("A1-22-S1", "A1-22-S2"):
        m = r[rid]["mat"]
        assert r[rid]["item"]["id"] == hc.id and m["rule"] == "c" and m["guven"] == "belirsiz", rid
        assert m["how"] == lab.HOW_ONLY_RECIPE and set(m["candidates"]) == {hc.id, hc_d.id}
        assert r[rid]["status"] == "yazilacak" and "GÜVEN BELİRSİZ" in r[rid]["write_note"]
    assert r["A2-41-S1"]["status"] == "belirsiz"                          # 2 reçete kartı → (d)
    assert set(r["A2-41-S1"]["mat"]["candidates"]) == {bb_d.id, bb_g.id}
    # gn_d grubun dışında → (b) yok; reçetede 2 aday → (c) yok → belirsiz
    assert r["A2-29-S1"]["status"] == "belirsiz" and r["A2-29-S1"]["item"] is None
    assert set(r["A2-29-S1"]["mat"]["candidates"]) == {gn_m.id, gn_n.id, gn_k.id, gn_d.id}
    c = p["counts"]
    assert c["kural_c"] == 2 and c["eslesme_guven_belirsiz"] == 2 and c["malzeme_belirsiz"] == 2
    wb = load_workbook(lab.build_xlsx(p, tmp_path / "k.xlsx"))
    fh = [x.value for x in wb["Fiyatlar"][1]]
    fiy = {row[0]: row for row in wb["Fiyatlar"].iter_rows(min_row=2, values_only=True)}
    assert fiy["A1-22-S2"][fh.index("Eşleşme güveni")] == "belirsiz"
    assert fiy["A1-22-S2"][fh.index("Eşleşme")] == lab.HOW_ONLY_RECIPE
    ws = wb["Eşleşmeyenler"]
    eh = [x.value for x in ws[1]]
    es = {row[0]: row for row in ws.iter_rows(min_row=2, values_only=True) if row[0]}
    assert "Eşleşme BELİRSİZ — otomatik seçildi" in es["A1-22-S2"][eh.index("Sorun")]
    assert es["A1-22-S2"][eh.index(lab.FIX_CARD_COL)] is None                  # lab düzeltebilir
    cand = es["A2-41-S1"][eh.index("Adaylar / benzer kartlar")]
    assert f"{bb_d.id} BİBERİYE UÇUCU YAĞI (ml) · DOALİNN · stok 34,844 ml · reçetede" in cand
    assert (f"{hc_d.id} HİNDİSTAN CEVİZİ YAĞI (ml) · DOGASA · stok 20 ml · reçetede değil"
            in es["A1-22-S2"][eh.index("Adaylar / benzer kartlar")])
    pref = wb["Tercih önerisi"]
    ph = [x.value for x in pref[1]]
    warn = {row[0]: row[ph.index("Uyarı")] for row in pref.iter_rows(min_row=2, values_only=True)}
    assert "Kart eşleşmesi BELİRSİZ" in warn["A1-22-S2"]


def test_material_aliases_and_lab_decides(db_session: Session):
    """`malzeme_takma_adlari`: belge adı → IMS kart adı (yalnız birebir ad
    yoksa; parantezli ek atılarak da aranır); liste → adaylar (kurallar seçer);
    {adaylar, lab_belirlesin} → belirsiz, reçetede olan aday bile OTOMATİK
    seçilmez.  Bozuk biçim ValueError."""
    s = _sups(db_session, "UMAYCHEM", "YİĞİTOĞLU KİMYA", "HAMMADDESEPETİ", "NATURALYA", "KRK GIDA", "DOGASA")
    bet = _card(db_session, "BETAİN 35", sup=s["UMAYCHEM"], stock=217164)
    xg_y = _card(db_session, "XHANTAN GUM", sup=s["YİĞİTOĞLU KİMYA"], stock=24706)
    _card(db_session, "XHANTAN GUM", sup=s["HAMMADDESEPETİ"], stock=180)
    nar_c = _card(db_session, "NAR ÇEKİRDEĞİ YAĞI", "ml", sup=s["NATURALYA"])
    nar_f = _card(db_session, "NAR ÇİÇEGİ YAGI", "g", sup=s["KRK GIDA"], stock=536)
    al_h = _card(db_session, "ALOE VERA EKSTRAKTI", "ml", sup=s["HAMMADDESEPETİ"], stock=0.11)
    al_d = _card(db_session, "ALOEVERA EKSTRAKTI", "ml", sup=s["DOGASA"])
    berg = _card(db_session, "BERGAMOT YAĞI", "ml")
    _card(db_session, "BERGAMOT UÇUCU YAĞI", "ml")
    _recipe(db_session, nar_c, al_h, al_d)
    db_session.commit()
    aliases = {"_aciklama": "test", "Betaine 35": "BETAİN 35", "XANTHAN GUM": "XHANTAN GUM",
               "BERGAMOT YAĞI": "BERGAMOT UÇUCU YAĞI",
               "ALEOVERA EKSTRAKTI": ["ALOE VERA EKSTRAKTI", "ALOEVERA EKSTRAKTI"],
               "NAR ÇEKİDEĞİ YAGI": {"adaylar": ["NAR ÇEKİRDEĞİ YAĞI", "NAR ÇİÇEGİ YAGI"],
                                     "lab_belirlesin": "ad ↔ tablo stoğu çelişiyor"}}
    p = lab.plan(db_session, data(
        rec("C-s12-2", "Betaine 35", "UMAYCHEM", 1.4, tur="proforma", tarih="2026-06-26", cur="USD"),
        rec("C-s14-1", "XANTHAN GUM (25 KG) (MEIUHA)", "YİĞİTOĞLU", 3.38, tur="proforma",
            tarih="2026-07-08", cur="USD"),
        rec("A2-01-S3", "NAR ÇEKİDEĞİ YAGI", "PHARMATERM İLAÇ", 9.08, secili=True),
        rec("A1-08-S1", "ALEOVERA EKSTRAKTI", "SURYA KİMYA", 16.34, birim="ml"),
        rec("A2-28-S1", "BERGAMOT YAĞI", "NATURALYA", 36.12, birim="ml"),    # birebir ad > takma ad
        malzeme_takma_adlari=aliases))
    r = _by_id(p)
    assert r["C-s12-2"]["item"]["id"] == bet.id and r["C-s12-2"]["mat"]["how"] == lab.HOW_ALIAS
    assert r["C-s14-1"]["item"]["id"] == xg_y.id                        # parantez atıldı + (a)
    assert r["C-s14-1"]["mat"]["how"] == f"{lab.HOW_ALIAS} → {lab.HOW_OWN}"
    nar = r["A2-01-S3"]
    assert nar["status"] == "belirsiz" and set(nar["mat"]["candidates"]) == {nar_c.id, nar_f.id}
    assert "lab belirlesin" in nar["mat"]["reason"] and "tablo stoğu" in nar["mat"]["reason"]
    assert r["A1-08-S1"]["status"] == "belirsiz"                        # iki sıvı kart da reçetede
    assert set(r["A1-08-S1"]["mat"]["candidates"]) == {al_h.id, al_d.id}
    assert r["A2-28-S1"]["item"]["id"] == berg.id and r["A2-28-S1"]["mat"]["how"] == "ad"
    # Takma adsız aynı kayıt bulunamaz
    p0 = lab.plan(db_session, data(rec("C-s12-2", "Betaine 35", "UMAYCHEM", 1.4, tur="proforma",
                                       tarih="2026-06-26", cur="USD")))
    assert p0["records"][0]["status"] == "eslesmedi"
    for bad in ({"X": []}, {"X": 5}, {"X": {"lab_belirlesin": "neden"}}, ["X"]):
        with pytest.raises(ValueError):
            lab.plan(db_session, data(rec("A1-01-S1", "X", "UMAYCHEM", 1.0), malzeme_takma_adlari=bad))


def test_lab_decision_blocks_exact_name_until_manual_mapping(db_session: Session):
    """Çelişkili kaynakta birebir ad da otomatik seçim için yeterli değildir;
    lab'ın elle eşlemesi yapıldıktan sonra belge o açık seçimi devralır."""
    its = _items(db_session, ("NAR ÇEKİDEĞİ YAGI", "ml"), ("NAR ÇİÇEGİ YAGI", "g"))
    _sups(db_session, "UMAYCHEM")
    db_session.commit()
    d = data(
        rec("A2-01-S1", "NAR ÇEKİDEĞİ YAGI", "UMAYCHEM", 18.57),
        rec("C-s99-1", "NAR ÇEKİDEĞİ YAGI", "UMAYCHEM", 20.0, tur="proforma",
            note="lab tablosu karşılığı: NAR ÇEKİDEĞİ YAGI (A2-01)"),
        malzeme_takma_adlari={"NAR ÇEKİDEĞİ YAGI": {
            "adaylar": ["NAR ÇEKİDEĞİ YAGI", "NAR ÇİÇEGİ YAGI"],
            "lab_belirlesin": "ad, birim ve stok çelişiyor"}})
    p = lab.apply(db_session, d)
    for r in p["records"]:
        assert r["status"] == "belirsiz" and r["item"] is None
        assert set(r["mat"]["candidates"]) == {i.id for i in its.values()}
    assert _lab_prices(db_session) == []
    d["elle_eslesme"] = {"kart": {"A2-01": its["NAR ÇİÇEGİ YAGI"].id}}
    p = lab.apply(db_session, d)
    records = _by_id(p)
    assert records["A2-01-S1"]["mat"]["how"] == "elle eşleme"
    assert records["C-s99-1"]["mat"]["how"] == f"{lab.HOW_HINT_SAME} A2-01-S1"
    assert [x.item_id for x in _lab_prices(db_session)] == [its["NAR ÇİÇEGİ YAGI"].id]


def test_document_follows_same_supplier_lab_record_and_overrides_it(db_session: Session):
    """C belgesi: notundaki "lab tablosu karşılığı" satırında AYNI firmanın
    kaydı hangi karta eşlendiyse oraya (ad hiçbir karta uymasa da) — öncelik
    kuralıyla lab kaydını ezer; kaydın eşleşme güvenini (c) belge de devralır.
    Aynı firmanın kaydı yoksa eski yol (ad / ipucu / material_key)."""
    s = _sups(db_session, "ULUDAG HERBAL", "PHARMATERM", "DOALİNN", "DOGASA", "KRK GIDA")
    kb_u = _card(db_session, "KAKAO BUTTER", sup=s["ULUDAG HERBAL"])
    kb_p = _card(db_session, "KAKAO BUTTER", sup=s["PHARMATERM"])
    _group(db_session, "KAKAO BUTTER", kb_u, kb_p)
    bg = _card(db_session, "BERGAMOT UÇUCU YAĞI", "ml", sup=s["DOALİNN"], stock=116.2)
    _card(db_session, "BERGAMOT UÇUCU YAĞI", "ml", sup=s["DOGASA"], stock=20)
    tb = _card(db_session, "TATLI BADEM YAGI", sup=s["KRK GIDA"])
    _recipe(db_session, kb_u, kb_p, bg, tb)
    db_session.commit()
    p = lab.apply(db_session, data(
        rec("A1-28-S1", "KAKAO BUTTER", "ULUDAĞ HERBAL", 39.56),
        rec("A1-28-S2", "KAKAO BUTTER", "PHARMATERM İLAÇ", 40.13, secili=True),
        rec("C-s10-1", "Kakao Yağı (Katı)", "PHARMATERM İLAÇ", 1654.0, tur="siparis", tarih="2026-06-25",
            cur="TRY", pkg=5.0, kdv=False, oran=1.0, sayfa=10,
            note="lab tablosu karşılığı: KAKAO BUTTER (A1-28)"),
        rec("A2-28-S2", "BERGAMOT UÇUCU YAĞI", "ULUDAĞ HERBAL", 44.99, birim="ml"),
        rec("C-s9-2", "BERGAMOT YAĞI 1LT", "ULUDAĞ AGRO", 11054.17, tur="fatura", tarih="2026-06-26",
            cur="TRY", unit="l", pkg=1.0, sayfa=9, note="lab tablosu karşılığı: BERGAMOT UÇUCU YAĞI (A2-28)"),
        rec("A1-26-S1", "TATLI BADEM YAGI", "NATURALYA", 12.9),
        rec("C-s13-1", "BADEM YAĞI (TATLI) 1 LT.", "KRK GIDA", 750.0, tur="proforma", tarih="2026-06-25",
            cur="TRY", unit="l", pkg=1.0, sayfa=13, note="lab tablosu karşılığı: TATLI BADEM YAGI (A1-26)"),
    ))
    r = _by_id(p)
    assert r["A1-28-S2"]["item"]["id"] == kb_p.id and r["A1-28-S1"]["item"]["id"] == kb_u.id
    c = r["C-s10-1"]
    assert c["item"]["id"] == kb_p.id and c["mat"]["how"] == f"{lab.HOW_HINT_SAME} A1-28-S2"
    assert c["status"] == "yazilacak" and r["A1-28-S2"]["status"] == "ezildi"
    assert r["A1-28-S1"]["status"] == "yazilacak"                       # başka firma, ayrı kart
    # İpucundaki kayıt (c) ile seçildiyse belge de güveni BELİRSİZ devralır
    assert r["A2-28-S2"]["mat"]["rule"] == "c" and r["C-s9-2"]["item"]["id"] == bg.id
    assert r["C-s9-2"]["mat"]["guven"] == "belirsiz" and r["C-s9-2"]["status"] == "yazilacak"
    # Aynı firmanın lab kaydı yok → ipucu (tek kart)
    assert r["C-s13-1"]["item"]["id"] == tb.id and r["C-s13-1"]["mat"]["how"] == "lab tablosu ipucu"
    rows = {(x.item_id, x.supplier_name): x for x in _lab_prices(db_session)}
    kp = rows[(kb_p.id, "PHARMATERM")]
    assert kp.unit_price == 1654.0 and kp.source == "siparis" and "A1-28-S2" in kp.note   # ezilen notta
    assert rows[(kb_u.id, "ULUDAG HERBAL")].unit_price == 39.56


def test_powder_form_goes_to_powder_card_only(db_session: Session):
    """Ayrı form (toz): yalnız adı verilen toz kartına (g/kg) eşlenir, sıvı
    karta asla; ipucundaki toz kaydı belgeye 'satırın kartı' diye geçmez."""
    s = _sups(db_session, "NATURALYA", "SURYA KİMYA")
    liq = _card(db_session, "ALEOVERA EKSTRAKTI", "ml", sup=s["SURYA KİMYA"])
    toz = _card(db_session, lab._TOZ_CARD, "g", sup=s["NATURALYA"], stock=292.6)
    db_session.commit()
    p = lab.plan(db_session, data(
        rec("A1-08-S2", "ALEOVERA EKSTRAKTI", "NATURALYA", 10.32, secili=True, birim="ml"),
        rec("A1-08-S3", "ALEOVERA EKSTRAKTI", "Naturalya", 39.6, note="toz hali", birim="ml"),
        rec("C-s8-1", "ALOEVERA EXTRACT PE", "NATURALYA KİMYA", 43.0, tur="proforma", tarih="2026-07-16",
            cur="USD", pkg=1.0, note="lab tablosu karşılığı: ALEOVERA EKSTRAKTI (A1-08, sıvı kart)"),
        rec("C-s99-1", "ALOE SIVI", "NATURALYA KİMYA", 12.0, tur="proforma", tarih="2026-07-16",
            cur="USD", pkg=25.0, note="lab tablosu karşılığı: ALEOVERA EKSTRAKTI (A1-08)"),
    ))
    r = _by_id(p)
    assert r["A1-08-S3"]["item"]["id"] == toz.id and r["C-s8-1"]["item"]["id"] == toz.id
    assert r["C-s8-1"]["mat"]["how"].startswith(lab.HOW_FORM)
    assert r["C-s8-1"]["status"] == "yazilacak" and r["A1-08-S3"]["status"] == "ezildi"
    assert r["A1-08-S2"]["item"]["id"] == liq.id
    # Sıvı belge: aynı firmanın TOZ kaydı ipucu sayılmaz → sıvı satırın (A1-08-S2)
    # kartı; sıvı lab fiyatını yalnız sıvı belge ezer (toz proforması değil)
    assert r["C-s99-1"]["item"]["id"] == liq.id
    assert r["C-s99-1"]["mat"]["how"] == f"{lab.HOW_HINT_SAME} A1-08-S2"
    assert r["A1-08-S2"]["status"] == "ezildi" and r["A1-08-S2"]["flags"][-1] == "kazanan: C-s99-1"
    assert [x.split(" ")[0] for x in r["C-s99-1"]["overridden"]] == ["A1-08-S2"]
    # Toz kartı sıvı birimliyse eşlenmez → ayrı form
    toz.unit = "ml"
    db_session.commit()
    p = lab.plan(db_session, data(rec("A1-08-S3", "ALEOVERA EKSTRAKTI", "Naturalya", 39.6, birim="ml")))
    assert p["records"][0]["status"] == "ayri_form"


# ─── Gerçek veri dosyası ────────────────────────────────────────────────────

def test_real_data_file_plans_and_builds_excel(db_session: Session, tmp_path):
    d = lab.load_data()
    assert len(d["kayitlar"]) == 160
    names = sorted({r["malzeme"] for r in d["kayitlar"] if r["tur"] == "lab_tablo"})
    _items(db_session, *[(n, "g") for n in names])
    _sups(db_session, "PHARMATERM", "DOALİNN", "ULUDAĞ HERBAL", "VESER KİMYEVİ", "NATURALYA",
          "SURYA KİMYA", "UMAYCHEM", "KRK GIDA", "TATLIDİLİMLER", "HAMMADDESEPETİ", "YİĞİTOĞLU KİMYA")
    db_session.commit()
    p = lab.plan(db_session, d)
    c = p["counts"]
    assert c["kayit"] == 160 and sum(c[k] for k in lab.STATUS_TEXT) == 160
    r = _by_id(p)
    # Pharmaterm KDV dahil uçucu yağlar (7) — yazılacak satırda KDV taşınır
    assert sum(1 for x in p["records"] if x["vat_included"] is True) == 7
    assert r["A2-40-S2"]["status"] == "yazilacak" and r["A2-40-S2"]["vat_rate"] == 20.0
    assert r["A1-08-S3"]["status"] == "ayri_form"
    # Tek (kart, firma) kuralı: yazılacaklarda çift yok
    keys = [(x["item"]["id"], x["sup"]["id"]) for x in p["records"] if x["status"] == "yazilacak"]
    assert len(keys) == len(set(keys))
    assert {"BEFCHEM", "ATAMAN KİMYA", "MARKET"} <= {n["name"] for n in p["new_suppliers"]}
    small = {x["sup"]["name"]: x for x in p["small"]}
    assert small["DOALİNN"]["kind"] == "Doalin notu" and small["SURYA KİMYA"]["kind"] == "bilgi"
    for nm in ("HAMMADDESEPETİ", "TATLIDİLİMLER", "ROSECE", "MARKET", "KİMYASAL EVİ"):
        assert small[nm]["kind"] == "küçük satıcı", nm
    pref_ids = [x["id"] for x in p["prefs"]]
    assert sum(1 for x in p["prefs"] if x["kind"] == "sarı seçim") == 80
    assert {"A2-13-S1", "C-s11-1", "C-s11-2", "A1-27-S1", "A1-28-S1"} <= set(pref_ids)
    # İnceleme düzeltmeleri: çizili teklifler + Doalin fiyat listesi yazılmaz;
    # çizili CLOVE OIL sarı karanfili ezmez; 2 sarılı aloe'de Naturalya üstte
    assert c["cizili"] == 2 and c["kapsam_disi"] == 2
    assert r["C-s16-4"]["status"] == "cizili" and r["A2-36-S1"]["status"] == "yazilacak"
    assert r["C-s11-1"]["status"] == "kapsam_disi" and r["C-s11-2"]["status"] == "kapsam_disi"
    assert pref_ids.index("A1-08-S2") < pref_ids.index("A1-08-S1")
    assert r["C-s10-1"]["material"] == "Kakao Yağı (Katı)"
    warn = {x["id"]: x["warn"] for x in p["prefs"]}
    for rid in ("A1-04-S3", "A1-10-S2", "A1-11-S2", "A1-14-S3", "A1-27-S2", "A1-28-S2", "A2-01-S3", "A2-08-S2"):
        assert "Uludağ ucuz olanları alacağız" in warn[rid], rid
    # Malzeme takma adları (08.10.2026): NAR kanıtı çelişkili → yalnız lab
    # Birebir ad bulunsa da çelişki engeli yalnız elle eşlemeyle çözülür.
    al = lab._material_aliases(d)
    assert al[lab.normalize("Betaine 35")]["names"] == ["BETAİN 35"]
    assert al[lab.normalize("NAR ÇEKİDEĞİ YAGI")]["lab"]
    assert r["A2-01-S3"]["status"] == "belirsiz" and r["A2-01-S3"]["item"] is None
    path = lab.build_xlsx(p, tmp_path / "gercek.xlsx")
    assert load_workbook(path).sheetnames == list(lab.SHEETS)
