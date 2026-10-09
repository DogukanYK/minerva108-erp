"""Ortak proforma şablonu (beğenilen boş şablon, 09.10.2026) + belge başına banka seçimi.

Kilitlenen sözleşmeler:
  • Tek çizici (core/proforma_template): A4, ≤24 satır tek sayfa (boş satırlar
    numaralı), >24 çok sayfa; şartlar (TIME OF DELIVERY satırında gün), 1–3 banka
    sütunu, RUB satırı YALNIZ seçili bankada RUB varsa; imza "Best Regards." altında.
  • Eski teklif formunun varsayılan "Notlar / Şartlar" metni NOTE olarak tekrar basılmaz.
  • Banka seçimi (core/bank_accounts): ülke + para birimi kuralı > varsayılan
    bankalar (Kuveyt Türk + Vakıfbank); 1–3, aktif, IBAN'lı.
  • Teklif ve teslimat proforması seçimi + şartları saklar, PDF aynı şablonla basılır.
  • Banka profili güncellemesi kısmi (eski istemci RUB/varsayılanı silmez) ve audit'li.
Yalnız izole test DB'si; sentetik müşteri adları.
"""
import json
import re
from io import BytesIO

import bcrypt
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from core import bank_accounts, proforma_template
from database import AdminAuditLog, AppSetting, BankProfile, BankRule, Item, Quotation, User

HDR = {"Origin": "http://testserver"}
A4_W, A4_H = 595.276, 841.89


def _text(pdf: bytes) -> str:
    return " ".join(p.extract_text() or "" for p in PdfReader(BytesIO(pdf)).pages)


def _pages(pdf: bytes):
    return PdfReader(BytesIO(pdf)).pages


def _flat(s: str) -> str:
    return "".join(s.split())


def _last_row(text: str, pieces: int) -> int:
    """Kalem tablosunun son numaralı satırı (TOTAL satırındaki adet toplamından hemen önce)."""
    m = re.search(rf"\n (\d+)\n {pieces}\n TOTAL", text)
    assert m, text
    return int(m.group(1))


def _bank(**over):
    base = {"id": 1, "label": "Kuveyt", "bank_name": "KUVEYT TÜRK KATILIM BANKASI", "branch": "214 LEVENT",
            "swift": "KTEFTRISXXX", "account_holder": "MİNERVA 108 YÖNETİM & DANIŞMANLIK A.Ş.",
            "ibans": {"USD": "TR090020500009096875200101", "EUR": "TR790020500009096875200102",
                      "TRY": "", "RUB": ""}}
    base.update(over)
    return base


def _doc(n_lines=3, banks=None, **over):
    lines = [{"name": f"Sentetik Serum {i + 1} 50 ml", "weight_ml": 50, "quantity": 10 + i,
              "unit_price": 4.25, "line_total": round((10 + i) * 4.25, 2)} for i in range(n_lines)]
    sub = round(sum(l["line_total"] for l in lines), 2)
    doc = {"number": "PRF-T-1", "date": "09.10.2026", "currency": "USD", "notes": "",
           "customer": {"name": "Sentetik Alıcı LLC", "address": "Testovaya 1, Moscow", "country": "Russia",
                        "phone": "+7 000"},
           "lines": lines, "totals": {"subtotal": sub, "shipping": 0, "tax_percentage": 0, "tax_amount": 0,
                                      "total": sub},
           "terms": proforma_template.merged_terms({"loading_days": 30},
                                                   proforma_template.payment_terms_text("advance", 50)),
           "banks": banks if banks is not None else [_bank()], "signature": None}
    doc.update(over)
    return doc


# ─── Çizici ──────────────────────────────────────────────────────────────────

def test_template_single_page_with_terms_banks_rub_and_signature():
    emlak = _bank(id=2, label="Emlak", bank_name="EMLAK KATILIM BANKASI", swift="EMLKTRIS",
                  ibans={"USD": "", "EUR": "", "TRY": "", "RUB": "TR000000000000000000000077"})
    sig = {"name": "Imzaci Test", "date": "09.10.2026 18:00", "png_b64": None,
           "document_hash": "ab" * 32, "revision": 2}
    pdf = proforma_template.render_proforma(_doc(banks=[_bank(), emlak], signature=sig))
    pages = _pages(pdf)
    assert len(pages) == 1
    assert abs(float(pages[0].mediabox.width) - A4_W) < 2 and abs(float(pages[0].mediabox.height) - A4_H) < 2
    text = _text(pdf)
    flat = _flat(text)
    for needle in ("PROFORMA INVOICE / PROFORMA FATURA", "COMPANY NAME", "TRANSPORTATION: EXCLUDING",
                   "SHIPMENT: - BY SEA - BY AIR", "TYPE OF DELIVERY: EX-FACTORY", "BANK ACCOUNT INFO",
                   "Best Regards.", "Imzaci Test", "Sentetik Alıcı LLC"):
        assert needle in text, needle
    assert "% 50 IN ADVANCE, BALANCE BEFORE SHIPMENT" in text
    assert "LOADING WITHIN 30 DAYS AFTER THE PAYMENT" in text
    assert "(USD)" in text                                   # para birimi sütun başlığında
    assert "RUBIBANNO" in flat and "TR000000000000000000000077" in flat
    assert "EMLAKKATILIMBANKASI" in flat and "KUVEYTTÜRKKATILIMBANKASI" in flat
    # Şablonun 24 numaralı satırı; RUB satırı + imza bloğu sığsın diye boş satır eksilir
    assert 3 < _last_row(text, 33) < 24
    assert "Rev 2" in text and "abababababababab" in text


def test_template_without_rub_bank_hides_rub_row_and_blank_days():
    doc = _doc(terms=proforma_template.merged_terms({}, None), notes="Etiketler Rusça olacak.")
    text = _text(proforma_template.render_proforma(doc))
    assert "RUB IBAN" not in text
    assert "LOADING WITHIN ____ DAYS" in text
    assert "PAYMENT TERMS: % 100 IN ADVANCE" in text
    assert "NOTE: Etiketler Rusça olacak." in text
    plain = proforma_template.render_proforma(_doc())
    assert _last_row(_text(plain), 33) == 24 and len(_pages(plain)) == 1   # şablondaki gibi 24 satır
    assert proforma_template._rows_that_fit(_doc()) == 24


def test_template_paginates_beyond_template_rows():
    from core.delivery_note import _count_pages
    pdf = proforma_template.render_proforma(_doc(n_lines=70))
    assert _count_pages(pdf) >= 2
    for pg in _pages(pdf):
        assert abs(float(pg.mediabox.width) - A4_W) < 2
    text = _text(pdf)
    assert "Sentetik Serum 1 50 ml" in text and "Sentetik Serum 70 50 ml" in text


def test_bundled_arial_metric_font_is_used():
    """Depodaki Liberation Sans (core/fonts) — sunucuda Arial / Liberation kurulu
    olmasa da proforma şablondaki gibi basılır."""
    from reportlab.pdfbase import pdfmetrics
    assert proforma_template._fonts() == ("PFA", "PFA-B")
    path = pdfmetrics.getFont("PFA").face.filename
    assert path.endswith("core/fonts/LiberationSans-Regular.ttf"), path


def test_terms_helpers_and_legacy_notes():
    pt = proforma_template.payment_terms_text
    assert pt("prepaid") == "% 100 IN ADVANCE"
    assert pt("advance", 30) == "% 30 IN ADVANCE, BALANCE BEFORE SHIPMENT"
    assert pt("net") == "AS AGREED"
    clean = proforma_template.clean_terms
    assert clean({}) == {"transportation": "EXCLUDING", "shipment": "- BY SEA - BY AIR",
                         "delivery_type": "EX-FACTORY", "loading_days": None}
    assert clean({"loading_days": "45", "transportation": " INCLUDING "})["loading_days"] == 45
    assert clean({"payment": "x"}).get("payment") is None          # B2B: ödeme koşulundan
    assert clean({"payment": "% 30 IN ADVANCE"}, with_payment=True)["payment"] == "% 30 IN ADVANCE"
    for bad in (400, -1, "abc"):
        with pytest.raises(ValueError):
            clean({"loading_days": bad})
    assert proforma_template.printable_notes(proforma_template.LEGACY_DEFAULT_NOTES) == ""
    assert proforma_template.printable_notes("  Özel   not ") == "Özel not"
    doc = _doc(notes=proforma_template.LEGACY_DEFAULT_NOTES)
    assert "NOTE:" not in _text(proforma_template.render_proforma(doc))


# ─── Banka seçimi ────────────────────────────────────────────────────────────

def _profiles(db):
    rows = {b.bank_name: b for b in db.query(BankProfile)}
    kuveyt = next(b for n, b in rows.items() if "KUVEYT" in n)
    vakif = next(b for n, b in rows.items() if "VAKIF" in n)
    isb = next(b for n, b in rows.items() if "İŞ" in n)
    return kuveyt, vakif, isb


def _emlak(db, **over):
    row = BankProfile(label="Emlak — Rusya", bank_name="EMLAK KATILIM BANKASI", branch="Test",
                      swift="EMLKTRIS", account_holder="MİNERVA 108 YÖNETİM & DANIŞMANLIK A.Ş.",
                      iban_rub="TR000000000000000000000077", sort_order=4, **over)
    db.add(row)
    db.commit()
    return row


def test_backfill_marks_template_banks_default_once(db_session):
    from database import _backfill_bank_defaults
    db = db_session
    kuveyt, vakif, isb = _profiles(db)
    assert kuveyt.is_default and vakif.is_default and not isb.is_default
    assert vakif.iban_try == "TR82 0001 5001 5800 7322 5156 62"
    assert vakif.bank_name == "TÜRKİYE VAKIFLAR BANKASI T.A.O."
    assert db.query(AppSetting).filter_by(key="backfill.bank_defaults.v1").one().value == "2"
    # Kullanıcı varsayılanları değiştirdiyse yeniden çalışma (sentinel silinse bile) ezmez
    kuveyt.is_default = vakif.is_default = False
    isb.is_default = True
    db.query(AppSetting).filter_by(key="backfill.bank_defaults.v1").delete()
    db.commit()
    _backfill_bank_defaults()
    db.expire_all()
    kuveyt, vakif, isb = _profiles(db)
    assert isb.is_default and not kuveyt.is_default and not vakif.is_default


def test_bank_selection_rules_and_validation(db_session):
    db = db_session
    kuveyt, vakif, isb = _profiles(db)
    assert bank_accounts.default_bank_ids(db, "Germany", "USD") == [kuveyt.id, vakif.id]
    emlak = _emlak(db)
    db.add(BankRule(country=bank_accounts.fold_country("Russia"), currency="USD", bank_profile_id=emlak.id))
    db.commit()
    assert bank_accounts.default_bank_ids(db, "russia", "USD") == [emlak.id]
    assert bank_accounts.default_bank_ids(db, "Russia", "EUR") == [kuveyt.id, vakif.id]
    assert bank_accounts.parse_ids('[3, "1", 3, 0, "x"]') == [3, 1]
    assert bank_accounts.parse_ids(None) == [] and bank_accounts.parse_ids(5) == [5]

    def code(ids, **kw):
        with pytest.raises(bank_accounts.BankSelectionError) as exc:
            bank_accounts.resolve_banks(db, ids, **kw)
        return exc.value.code
    assert code([]) == "bank_required"
    assert bank_accounts.resolve_banks(db, [], required=False) == []
    assert code([kuveyt.id, vakif.id, isb.id, emlak.id]) == "too_many_banks"
    assert code([999999]) == "invalid_bank"
    isb.is_active = False
    db.commit()
    assert code([isb.id]) == "invalid_bank"
    bare = BankProfile(label="IBAN'sız", bank_name="X BANK", account_holder="Y")
    db.add(bare)
    db.commit()
    assert code([bare.id]) == "bank_iban_missing"
    # Belgeye kayıtlı seçimde pasifleşen banka sessizce atlanır (belge yine basılır)
    got = bank_accounts.banks_for_document(db, json.dumps([isb.id, emlak.id]), "Russia", "USD")
    assert [b.id for b in got] == [emlak.id]
    snap = bank_accounts.bank_snapshot(emlak, "usd")
    assert snap["ibans"]["RUB"] == "TR000000000000000000000077" and snap["iban"] == ""


# ─── Teklif ──────────────────────────────────────────────────────────────────

def _finished(db, name="Sentetik Krem 50 ml", stock=50):
    it = Item(name=name, sku=f"PT-{abs(hash(name)) % 10**6}", unit="adet", category="Bitmiş Ürün",
              current_stock=stock, domain="cosmetics")
    db.add(it)
    db.commit()
    return it


def _quote_body(item, number, **extra):
    body = {"quote_number": number, "customer_name": "Sentetik Alıcı LLC", "customer_country": "Russia",
            "customer_address": "Testovaya 1", "customer_phone": "+7 000", "currency": "USD",
            "subtotal_amount": 50, "total_amount": 50,
            "items": [{"item_id": item.id, "quantity": 10, "unit_price_foreign": 5.0,
                       "item_name_snapshot": item.name}]}
    body.update(extra)
    return body


def test_quotation_stores_banks_terms_and_renders_server_pdf(authed_client: TestClient, db_session):
    c, db = authed_client, db_session
    kuveyt, vakif, _ = _profiles(db)
    item = _finished(db)
    r = c.post("/api/quotations", headers=HDR, json=_quote_body(
        item, "PT-Q-1", bank_profile_ids=[vakif.id, kuveyt.id], notes=proforma_template.LEGACY_DEFAULT_NOTES,
        proforma_terms={"transportation": "INCLUDING", "payment": "% 30 IN ADVANCE", "loading_days": 45}))
    assert r.status_code == 201, r.text
    qid = r.json()["id"]
    q = c.get(f"/api/quotations/{qid}").json()
    assert q["bank_profile_ids"] == [vakif.id, kuveyt.id]
    assert q["proforma_terms"]["transportation"] == "INCLUDING"
    assert q["proforma_terms"]["shipment"] == "- BY SEA - BY AIR"       # boş → şablon varsayılanı
    assert q["proforma_terms"]["payment"] == "% 30 IN ADVANCE" and q["proforma_terms"]["loading_days"] == 45

    r = c.get(f"/api/quotations/{qid}/proforma")
    assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
    assert "Proforma_PT-Q-1" in r.headers["content-disposition"]
    text = _text(r.content)
    flat = _flat(text)
    assert "TRANSPORTATION: INCLUDING" in text and "PAYMENT TERMS: % 30 IN ADVANCE" in text
    assert "LOADING WITHIN 45 DAYS" in text
    assert "NOTE:" not in text                                          # eski varsayılan not basılmaz
    assert flat.index("TÜRKİYEVAKIFLAR") < flat.index("KUVEYTTÜRK")       # seçim sırası = sütun sırası
    assert "Sentetik Krem 50 ml" in text and "50.00" in text

    # Seçim yoksa ülke kuralı / varsayılan bankalar basılır
    qid2 = c.post("/api/quotations", headers=HDR, json=_quote_body(item, "PT-Q-2")).json()["id"]
    flat2 = _flat(_text(c.get(f"/api/quotations/{qid2}/proforma").content))
    assert "KUVEYTTÜRK" in flat2 and "TÜRKİYEVAKIFLAR" in flat2 and "İŞBANKASI" not in flat2

    # Doğrulama: en fazla 3 banka, geçerli gün, var olan aktif banka
    emlak = _emlak(db)
    for extra, code in (({"bank_profile_ids": [kuveyt.id, vakif.id, emlak.id, 999]}, "too_many_banks"),
                        ({"bank_profile_ids": [999999]}, "invalid_bank"),
                        ({"proforma_terms": {"loading_days": 400}}, "invalid_terms")):
        r = c.post("/api/quotations", headers=HDR, json=_quote_body(item, f"PT-Q-X-{code}", **extra))
        assert r.status_code == 400 and r.json()["code"] == code, r.text
    # Başka panelin teklifi bu panelde basılmaz
    db.query(Quotation).filter_by(id=qid).update({"domain": "supplement"})
    db.commit()
    assert c.get(f"/api/quotations/{qid}/proforma").status_code == 404


def _user(db, username, role, permissions=None):
    u = User(username=username, full_name=username.title(), role=role, is_active=True,
             password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode(),
             permissions=json.dumps(permissions) if permissions is not None else None)
    db.add(u)
    db.commit()
    return u


def _as(client, username):
    r = client.post("/api/login", json={"username": username, "password": "minerva123"}, headers=HDR)
    assert r.status_code == 200, r.text
    return client


def test_bank_profiles_endpoint_suggests_by_rule_and_is_internal_only(client: TestClient, db_session):
    db = db_session
    kuveyt, vakif, _ = _profiles(db)
    emlak = _emlak(db)
    bare = BankProfile(label="IBAN'sız", bank_name="X BANK", account_holder="Y", sort_order=9)
    db.add(bare)
    db.commit()
    _as(client, "dogukan")
    d = client.get("/api/bank-profiles", params={"country": "Russia", "currency": "USD"}).json()
    assert d["default_ids"] == [kuveyt.id, vakif.id] and d["max_banks"] == 3
    assert emlak.id in [b["id"] for b in d["banks"]] and bare.id not in [b["id"] for b in d["banks"]]
    assert d["default_terms"]["payment"] == "% 100 IN ADVANCE"
    r = client.post("/api/b2b-orders/bank-rules", headers=HDR,
                    json={"country": "Russia", "currency": "USD", "bank_profile_id": emlak.id})
    assert r.status_code == 200, r.text
    d = client.get("/api/bank-profiles", params={"country": "RUSSIA", "currency": "usd"}).json()
    assert d["default_ids"] == [emlak.id]
    # Teslimat proforması hazırlayan (inventory.adjust) görür; ikisi de yoksa / bayi 403
    _user(db, "pt_store", "Staff", {"inventory": {"view": True, "adjust": True}})
    _user(db, "pt_plain", "Staff", {"items": {"view": True}})
    _user(db, "pt_dist", "Distributor", {"b2b": {"view": True}})
    assert _as(client, "pt_store").get("/api/bank-profiles").status_code == 200
    assert _as(client, "pt_plain").get("/api/bank-profiles").status_code == 403
    assert _as(client, "pt_dist").get("/api/bank-profiles").status_code == 403


# ─── Teslimat proforması (PRF-) ──────────────────────────────────────────────

def _prf(item, **extra):
    p = {"recipient_name": "John Buyer", "recipient_org": "ACME LLC", "delivery_type": "proforma",
         "doc_lang": "EN", "currency": "USD", "customer_address": "5th Ave", "customer_country": "USA",
         "items": [{"item_id": item.id, "quantity": 3, "unit_price": 7.5, "weight_ml": 100}]}
    p.update(extra)
    return p


def test_delivery_proforma_uses_template_with_selected_banks_and_terms(authed_client: TestClient,
                                                                       db_session):
    c, db = authed_client, db_session
    kuveyt, vakif, _ = _profiles(db)
    emlak = _emlak(db)
    item = _finished(db, "Sentetik Tonik 100 ml")
    r = c.post("/api/delivery", headers=HDR, json=_prf(
        item, bank_profile_ids=[emlak.id, kuveyt.id],
        proforma_terms={"payment": "% 30 IN ADVANCE", "loading_days": 10}))
    assert r.status_code == 201, r.text
    did = r.json()["id"]
    pend = c.get("/api/delivery/pending").json()["pending"][0]
    assert pend["bank_profile_ids"] == [emlak.id, kuveyt.id]
    assert pend["proforma_terms"]["payment"] == "% 30 IN ADVANCE"
    assert pend["proforma_terms"]["transportation"] == "EXCLUDING"
    assert c.post(f"/api/delivery/{did}/approve", headers=HDR).status_code == 200
    text = _text(c.get(f"/api/delivery/{did}/proforma?format=pdf").content)
    flat = _flat(text)
    assert "ACME LLC (Attn: John Buyer)" in text
    assert "PAYMENT TERMS: % 30 IN ADVANCE" in text and "LOADING WITHIN 10 DAYS" in text
    assert "RUBIBANNO" in flat and "TR000000000000000000000077" in flat and "VAKIF" not in flat
    assert "22.50" in text

    # Seçim gönderilmezse varsayılan bankalar (RUB satırı yok)
    did2 = c.post("/api/delivery", headers=HDR, json=_prf(item)).json()["id"]
    assert c.post(f"/api/delivery/{did2}/approve", headers=HDR).status_code == 200
    flat2 = _flat(_text(c.get(f"/api/delivery/{did2}/proforma?format=pdf").content))
    assert "KUVEYT" in flat2 and "VAKIF" in flat2 and "RUBIBANNO" not in flat2

    r = c.post("/api/delivery", headers=HDR, json=_prf(item, proforma_terms={"loading_days": "x"}))
    assert r.status_code == 400
    r = c.post("/api/delivery", headers=HDR, json=_prf(item, bank_profile_ids=[kuveyt.id, vakif.id,
                                                                               emlak.id, 99]))
    assert r.status_code == 400


# ─── Banka profili yönetimi ──────────────────────────────────────────────────

def test_bank_profile_update_is_partial_and_audits_iban_changes(authed_client: TestClient, db_session):
    c, db = authed_client, db_session
    _, vakif, _ = _profiles(db)
    r = c.post("/api/b2b-orders/banks", headers=HDR, json={
        "label": "Emlak — Rusya", "bank_name": "EMLAK KATILIM BANKASI", "account_holder": "MİNERVA 108",
        "iban_rub": " TR000000000000000000000077 ", "is_default": False})
    assert r.status_code == 200, r.text
    emlak = db.get(BankProfile, r.json()["id"])
    assert emlak.iban_rub == "TR000000000000000000000077" and emlak.is_active

    # Eski istemci (RUB / varsayılan alanı yok) yalnız USD IBAN'ı değiştirir
    body = {k: getattr(vakif, k) for k in ("label", "bank_name", "branch", "swift", "account_holder")}
    r = c.put(f"/api/b2b-orders/banks/{vakif.id}", headers=HDR, json={**body, "iban_usd": "TR000000000000000000000001"})
    assert r.status_code == 200 and r.json()["changed"] == ["iban_usd"], r.text
    db.expire_all()
    vakif = db.get(BankProfile, vakif.id)
    assert vakif.is_default and vakif.iban_try and vakif.iban_usd == "TR000000000000000000000001"
    log = db.query(AdminAuditLog).filter_by(action="b2b_order.bank_update").order_by(AdminAuditLog.id.desc()).first()
    details = json.loads(log.details)
    assert details["changes"]["iban_usd"] == ["TR36 0001 5001 5804 8023 3113 21", "TR000000000000000000000001"]

    # Tüm IBAN'lar boşaltılamaz; zorunlu alanlar boşluktan ibaret olamaz
    r = c.put(f"/api/b2b-orders/banks/{emlak.id}", headers=HDR, json={
        "label": "Emlak", "bank_name": "EMLAK", "account_holder": "M", "iban_rub": ""})
    assert r.status_code == 400 and r.json()["code"] == "bank_iban_missing"
    r = c.post("/api/b2b-orders/banks", headers=HDR, json={
        "label": " ", "bank_name": "X", "account_holder": "Y", "iban_usd": "TR1"})
    assert r.status_code == 400 and r.json()["code"] == "bank_fields_required"
