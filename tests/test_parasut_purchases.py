"""
Paraşüt alış faturası fiyat onay kuyruğu (core/parasut_purchases.py) — AĞ YOK.

Gerçek Paraşüt API'si ASLA çağrılmaz: istemci `httpx.MockTransport`, token
`token_fn=`, bekleme `sleep=` ile enjekte edilir; senkron testleri sahte
istemciyle (yalnız `get`) çalışır.  Fikstürler sentetiktir — Rusya dökümündeki
tedarikçi iletişim bilgileri buraya KOPYALANMAZ.

Kilitlenenler:
  • Okuma-yalnızlık kod yapısıyla: Api'nin HTTP metotları yalnız `get` + `close`;
    kaynakta post/put/patch/delete/request yok; `core.parasut._api` kullanılmaz;
    yol izin listesi yalnız `purchase_bills`.
  • KVKK beyaz listesi: parse_bill çıktısında e-posta / telefon / vergi no /
    adres YOK.
  • 429 → Retry-After'a uyup tekrar; 5xx / taşıma hatası sınırlı; 3xx hata;
    401 token önbelleğini düşürür.  Ayarlar çağrı anında; eksikler yalnız adıyla.
  • Kimlik bilgisi / token hiçbir hata mesajına ve log satırına girmez.
  • Öneri yalnız tek ve ≥60 puanlı aday; Belge Oku'nun kesin cevabı DEĞİŞMEZ
    (CMC / MEYAN KÖKÜ EKSTRAKTI / karışım kartı genel karta yenilmez); ters
    kapsama (gerçek adlar, TR adı boş: KARNAUBA → CARNAUBA WAX, AKGÜNLÜK,
    HİDROLİZE İPEK; TIBBİ LAVANTA özgüllüğü) yalnız iyelik ekiyle ("-lı" değil),
    "A + B" satırına ve aynı malzemenin firmasız iki kartına öneri yok;
    "1" / "A" gibi metne öneri yok; senkron hiçbir fiyat YAZMAZ.
  • Eşzamanlı onay / atlama (iki oturum + iş parçacığı): ikinci istek 409
    not_pending ya da yerinde güncelleme — çift fiyat satırı / 500 yok;
    `lock_for_approval` çok satırlı çağıranla kilitlenme döngüsünü önler, kalan
    kilitlenmede uç 409 `busy`.
  • Onay hesabı (Nilkim 625 TL ÷ 25 kg = 25 TL/kg), yeni fatura kazanır / eski
    fatura ezmez, elle fiyat engeller (manual_exists), ambalaj hatırlanır.
  • Eski fatura koruması (12 ay) planda ve fiyat sayfasında; kaynak süzgeci
    `parasut`, geçersiz süzgeç 400; rota yetkileri.
"""
import inspect
import json
import logging
import pathlib
import re
import threading
from datetime import date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from core import parasut as P
from core import parasut_purchases as PP
from database import (AdminAuditLog, AppSetting, Item, ParasutProductPack, ParasutPurchaseBill, ParasutPurchaseLine,
                      Supplier, SupplierPrice)

_H = {"Origin": "http://testserver"}
SECRETS = ("cs-s3cr3t-XYZ", "pw-s3cr3t-ABC", "tok-SECRET-123")


@pytest.fixture(autouse=True)
def _stop_reset():
    PP.reset_stop()
    yield
    PP.reset_stop()


def _env(monkeypatch):
    for k, v in {"CLIENT_ID": "pc", "CLIENT_SECRET": "cs-s3cr3t-XYZ", "COMPANY_ID": "115",
                 "EMAIL": "it@example.com", "PASSWORD": "pw-s3cr3t-ABC"}.items():
        monkeypatch.setenv(f"PARASUT_{k}", v)


class Clock:
    def __init__(self):
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(round(s, 3))


def _api(monkeypatch, handler):
    _env(monkeypatch)
    clock = Clock()
    api = PP.Api(transport=httpx.MockTransport(handler), sleep=clock.sleep, token_fn=lambda: "tok-SECRET-123")
    return api, clock


# ─── Sentetik JSON:API fikstürleri (iletişim alanları UYDURMA, saklanmamalı) ──

def _contact(cid, name):
    return {"id": str(cid), "type": "contacts", "attributes": {
        "name": name, "email": "gizli@example.com", "phone": "+90 555 000 00 00",
        "tax_number": "1234567890", "tax_office": "Test VD", "address": "Gizli Sk. No:1", "city": "İstanbul"}}


def _bill(pid, no, issue, supplier, lines, *, currency="TRL", item_type="purchase_bill", description=None,
          net_total=None):
    """lines: [(detail_id, product_id, product_name, unit, qty, unit_price, vat, net, extra_attrs)]"""
    sid, sname = supplier
    details, included = [], [_contact(sid, sname)]
    for (did, prid, pname, unit, qty, up, vat, net, extra) in lines:
        details.append({"id": str(did), "type": "purchase_bill_details"})
        att = {"quantity": str(qty), "unit_price": str(up), "vat_rate": str(vat), "net_total": str(net),
               "description": pname}
        att.update(extra or {})
        det = {"id": str(did), "type": "purchase_bill_details", "attributes": att}
        if prid is not None:
            det["relationships"] = {"product": {"data": {"id": str(prid), "type": "products"}}}
            included.append({"id": str(prid), "type": "products", "attributes": {"name": pname, "unit": unit}})
        included.append(det)
    data = {"id": str(pid), "type": "purchase_bills", "attributes": {
        "item_type": item_type, "invoice_no": no, "issue_date": issue, "due_date": issue, "currency": currency,
        "exchange_rate": "1.0", "net_total": str(net_total if net_total is not None else sum(l[7] for l in lines)),
        "gross_total": None, "description": description},
        "relationships": {"supplier": {"data": {"id": str(sid), "type": "contacts"}},
                          "details": {"data": details}}}
    return data, included


NILKIM = (41, "NİLKİM TEKNİK KİMYA SAN.TİC.LTD.ŞTİ")
ORLIFE = (42, "ORLIFE GLOBAL İÇ VE DIŞ TİCARET A.Ş.")
ARAS = (43, "ARAS KARGO YURT İÇİ YURT DIŞI TAŞIMACILIK A.Ş.")

B_NILKIM = _bill(1001, "NIL2024000000554", "2024-07-29", NILKIM, [
    (5001, 777, "CAPRYLİC / CAPRİC TRİGLYCERİDE", "Adet", 1, 625.0, 20.0, 750.0, None),
    (5002, 778, "KARGO TAŞIMA BEDELİ", "Adet", 1, 77.5, 20.0, 93.0, None)])
B_ORLIFE = _bill(1002, "ORA2024000000058", "2024-05-27", ORLIFE, [
    (5003, 779, "DUY.FRK-SE.07 AKGÜNLÜK UÇUÇU YAĞI (Boswellia serrata) 10 ml.", "Adet", 4, 249.96, 20.0, 959.84,
     {"discount_type": "percentage", "discount_value": "20.0"})], description="Hammadde")
B_ARAS = _bill(1003, "AAA2024000988833", "2024-05-23", ARAS, [
    (5004, 780, "ARAS STANDART KARGO", "Adet", 1, 50.73, 20.0, 60.88, None)], description="Kargo")
B_NAT = _bill(1004, "M012026000002134", "2026-08-14", (44, "NATURALYA KİMYA GIDA LTD. ŞTİ."), [
    (5005, 781, "LICORICE EXTRACT LIQ", "Adet", 5, 620.798, 20.0, 3724.79, None),
    (5006, 782, "GINSENG EXTRACT LIQ", "Adet", 5, 716.306, 20.0, 4297.84, None)])


def _payload(bills, page=1, total_pages=1):
    return {"data": [b[0] for b in bills], "included": [x for b in bills for x in b[1]],
            "meta": {"current_page": page, "total_pages": total_pages, "total_count": len(bills)}}


class FakeApi:
    """Sahte istemci — yalnız `get`; her çağrıyı kaydeder."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, dict(params or {})))
        assert path == "/purchase_bills"
        n = int(params.get("page[number]", 1))
        return self.pages[n - 1] if n - 1 < len(self.pages) else {"data": [], "included": [], "meta": {}}


# ─── Okuma-yalnızlık + izin listesi + yapılandırma ──────────────────────────

def test_api_is_read_only_by_construction():
    public = sorted(n for n in dir(PP.Api) if not n.startswith("_"))
    assert public == ["close", "get"]
    src = pathlib.Path(PP.__file__).read_text(encoding="utf-8")
    hits = re.findall(r"\b(httpx|self\._http|_http|client)\.(post|put|patch|delete|request|send|stream)\(", src)
    assert hits == []
    assert "self._http.get(" in inspect.getsource(PP.Api.get)
    assert re.search(r"\._api\(", src) is None                       # yazar yardımcısı kullanılmaz
    assert [rx.pattern for rx in PP.ALLOWED_PATHS] == [r"^/purchase_bills$"]
    # Hız kilidi yazar modülle paylaşılır
    assert isinstance(P._RATE_LOCK, type(threading.Lock()))
    assert "with P._RATE_LOCK" in src and "with _RATE_LOCK" in inspect.getsource(P._api)


def test_get_only_allowlisted_paths(monkeypatch):
    seen = []
    api, _ = _api(monkeypatch, lambda req: seen.append(req.url.path) or httpx.Response(200, json={"data": []}))
    for bad in ("/contacts", "/purchase_bills/1", "/sales_invoices", "/products", "/purchase_bills/1/payments"):
        with pytest.raises(ValueError):
            api.get(bad)
    assert seen == []
    api.get("/purchase_bills", {"page[size]": 25, "x": None})
    assert seen == ["/v4/115/purchase_bills"]


def test_headers_bearer_and_params(monkeypatch):
    seen = {}

    def handler(req):
        seen.update(dict(req.headers))
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"data": []})

    api, clock = _api(monkeypatch, handler)
    api.get("/purchase_bills", {"sort": "-issue_date", "include": PP.INCLUDE, "page[size]": 25, "page[number]": 1})
    assert seen["authorization"] == "Bearer tok-SECRET-123"
    assert seen["url"].startswith("https://api.parasut.com/v4/115/purchase_bills?")
    assert "sort=-issue_date" in seen["url"] and "page%5Bsize%5D=25" in seen["url"]
    assert clock.sleeps == [P._RATE_SLEEP] and api.calls == 1


def test_config_read_at_call_time(monkeypatch):
    for k in ("CLIENT_ID", "CLIENT_SECRET", "COMPANY_ID", "EMAIL", "PASSWORD"):
        monkeypatch.delenv(f"PARASUT_{k}", raising=False)
    assert not PP.is_configured()
    assert PP.config_problems() == list(PP.REQUIRED_ENV)
    with pytest.raises(PP.ParasutPurchasesNotConfigured) as ei:
        PP.Api(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert "PARASUT_PASSWORD" in str(ei.value)
    monkeypatch.setenv("PARASUT_CLIENT_ID", "pc")
    monkeypatch.setenv("PARASUT_CLIENT_SECRET", "cs-s3cr3t-XYZ")
    monkeypatch.setenv("PARASUT_COMPANY_ID", "115")
    monkeypatch.setenv("PARASUT_EMAIL", "it@example.com")
    assert PP.config_problems() == ["PARASUT_PASSWORD"]
    monkeypatch.setenv("PARASUT_PASSWORD", "pw-s3cr3t-ABC")
    assert PP.is_configured() and PP.config_problems() == []
    assert all(s not in " ".join(PP.config_problems()) for s in SECRETS)


# ─── Taşıma: tekrar / hata eşlemesi / sırlar ────────────────────────────────

def test_429_retry_after_then_success(monkeypatch):
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(429, headers={"Retry-After": "7"}) if n["i"] == 1 else httpx.Response(200, json={"ok": 1})

    api, clock = _api(monkeypatch, handler)
    assert api.get("/purchase_bills") == {"ok": 1}
    assert clock.sleeps == [P._RATE_SLEEP, 7.0, P._RATE_SLEEP] and api.calls == 2


def test_429_exhausted_and_5xx_capped(monkeypatch):
    api, clock = _api(monkeypatch, lambda req: httpx.Response(429))
    with pytest.raises(PP.ParasutPurchasesError, match="429"):
        api.get("/purchase_bills")
    assert [s for s in clock.sleeps if s != P._RATE_SLEEP] == [10, 30, 60]
    api, clock = _api(monkeypatch, lambda req: httpx.Response(503))
    with pytest.raises(PP.ParasutPurchasesError, match="503"):
        api.get("/purchase_bills")
    assert api.calls == 4 and [s for s in clock.sleeps if s != P._RATE_SLEEP] == [2, 5, 15]


def test_transport_error_and_redirect(monkeypatch):
    def boom(req):
        raise httpx.ConnectError("boom", request=req)

    api, clock = _api(monkeypatch, boom)
    with pytest.raises(PP.ParasutPurchasesError, match="ulaşılamadı"):
        api.get("/purchase_bills")
    assert [s for s in clock.sleeps if s != P._RATE_SLEEP] == [2, 5, 15]
    api, _ = _api(monkeypatch, lambda req: httpx.Response(302, headers={"Location": "https://elsewhere"}))
    with pytest.raises(PP.ParasutPurchasesError, match="302"):
        api.get("/purchase_bills")


def test_401_clears_token_cache_and_no_secret_in_message(monkeypatch):
    api, _ = _api(monkeypatch, lambda req: httpx.Response(401, json={"error": "invalid_token"}))
    P._TOKEN_CACHE["token"] = ("tok-SECRET-123", 9999999999)
    with pytest.raises(PP.ParasutPurchasesError, match="401") as ei:
        api.get("/purchase_bills")
    assert "token" not in P._TOKEN_CACHE
    assert all(s not in str(ei.value) for s in SECRETS)


def test_credentials_never_logged(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    api, _ = _api(monkeypatch, lambda req: httpx.Response(500, text="server says cs-s3cr3t-XYZ? no"))
    with pytest.raises(PP.ParasutPurchasesError) as ei:
        api.get("/purchase_bills")
    api2, _ = _api(monkeypatch, lambda req: httpx.Response(400, json={"errors": [{"detail": "bad"}]}))
    with pytest.raises(PP.ParasutPurchasesError) as ei2:
        api2.get("/purchase_bills")
    for s in SECRETS:
        assert s not in caplog.text and s not in str(ei.value) and s not in str(ei2.value)


def test_stop_flag_interrupts(monkeypatch):
    calls = []
    api, _ = _api(monkeypatch, lambda req: calls.append(1) or httpx.Response(200, json={"data": []}))
    PP.request_stop()
    with pytest.raises(PP.ParasutPurchasesStopped):
        api.get("/purchase_bills")
    assert calls == []
    with pytest.raises(PP.ParasutPurchasesStopped):
        PP._interruptible_sleep(5)
    PP.reset_stop()
    assert api.get("/purchase_bills") == {"data": []}


# ─── Beyaz liste / gider ipucu / indirim ────────────────────────────────────

def test_parse_bill_whitelist_drops_contact_fields():
    data, inc = B_NILKIM
    b = PP.parse_bill(data, PP._incmap({"included": inc}))
    assert set(b) == set(PP.BILL_KEYS) and all(set(ln) == set(PP.LINE_KEYS) for ln in b["lines"])
    dump = json.dumps(b, default=str)
    for leak in ("gizli@example.com", "555 000", "1234567890", "Gizli Sk", "Test VD", "İstanbul"):
        assert leak not in dump, leak
    assert b["supplier_parasut_id"] == 41 and b["supplier_name"].startswith("NİLKİM")
    assert b["currency"] == "TRY" and b["issue_date"] == date(2024, 7, 29) and b["invoice_no"] == "NIL2024000000554"
    ln = b["lines"][0]
    assert ln["product_parasut_id"] == 777 and ln["quantity"] == 1.0 and ln["unit_price"] == 625.0
    assert ln["vat_rate"] == 20.0 and ln["net_total"] == 750.0 and ln["discount_rate"] is None
    assert ln["product_unit"] == "Adet" and ln["expense_hint"] is False
    # Tablo kolonlarında da iletişim alanı yok
    cols = {c.name for c in ParasutPurchaseBill.__table__.columns} | {c.name for c in ParasutPurchaseLine.__table__.columns}
    assert not cols & {"email", "phone", "tax_number", "tax_office", "address", "city", "iban"}


def test_expense_hint_and_item_type_filter():
    aras = PP.parse_bill(B_ARAS[0], PP._incmap({"included": B_ARAS[1]}))
    assert aras["expense_hint"] is True and aras["lines"][0]["expense_hint"] is True
    nil = PP.parse_bill(B_NILKIM[0], PP._incmap({"included": B_NILKIM[1]}))
    assert nil["expense_hint"] is False
    assert [ln["expense_hint"] for ln in nil["lines"]] == [False, True]      # kargo satırı işaretli
    assert PP.expense_hint("GENERE TURİZM SEYAHAT TİC. A.Ş.") and PP.expense_hint("OTOKOÇ ARAÇ KİRALAMA")
    assert PP.expense_hint("KARİYER NET ELEK.YAY. VE İLET. HİZ.AŞ") and not PP.expense_hint("KARNAUBA MUMU")
    data = dict(B_NILKIM[0]); data["attributes"] = dict(data["attributes"], item_type="refund")
    assert PP.parse_bill(data, {}) is None


def test_discount_rate_percentage_and_amount():
    orl = PP.parse_bill(B_ORLIFE[0], PP._incmap({"included": B_ORLIFE[1]}))
    assert orl["lines"][0]["discount_rate"] == pytest.approx(0.2) and orl["currency"] == "TRY"
    assert PP._discount_rate({"discount_type": "amount", "discount_value": "100"}, 4.0, 250.0) == pytest.approx(0.1)
    assert PP._discount_rate({"discount_type": "amount", "discount_value": "100"}, 0.0, 250.0) is None
    assert PP._discount_rate({"discount_type": "percentage", "discount_value": "150"}, 1.0, 1.0) == 1.0
    assert PP.currency_code("TRL") == "TRY" and PP.currency_code("usd") == "USD" and PP.currency_code(None) is None


def test_fetch_bills_paginates_and_stops_at_since():
    api = FakeApi([_payload([B_NAT, B_NILKIM], 1, 2), _payload([B_ORLIFE, B_ARAS], 2, 2)])
    bills = PP.fetch_bills(api)
    assert [b["parasut_id"] for b in bills] == [1004, 1001, 1002, 1003] and len(api.calls) == 2
    assert api.calls[0][1]["include"] == PP.INCLUDE and api.calls[0][1]["page[size]"] == 25
    api = FakeApi([_payload([B_NAT, B_NILKIM], 1, 2), _payload([B_ORLIFE, B_ARAS], 2, 2)])
    bills = PP.fetch_bills(api, since=date(2024, 7, 1))
    assert [b["parasut_id"] for b in bills] == [1004, 1001] and len(api.calls) == 2   # 2. sayfada eski faturada durdu
    api = FakeApi([_payload([B_NAT, B_NILKIM], 1, 2), _payload([B_ORLIFE, B_ARAS], 2, 2)])
    bills = PP.fetch_bills(api, since=date(2024, 8, 1))
    assert [b["parasut_id"] for b in bills] == [1004] and len(api.calls) == 1        # 1. sayfada durdu, 2. istenmedi


# ─── Öneri + senkron (DB) ───────────────────────────────────────────────────

def _seed(db, *, with_supplier=True):
    sup = Supplier(name="NİLKİM TEKNİK KİMYA", domain="cosmetics") if with_supplier else None
    if sup:
        db.add(sup); db.flush()
    cct = Item(name="CAPRYLIC CAPRIC TRIGLYCERIDE", name_tr="KAPRİLİK KAPRİK TRİGLİSERİT", sku="CCT-1",
               category="Hammadde", unit="g", domain="cosmetics", supplier_id=(sup.id if sup else None))
    lic = Item(name="MEYAN KÖKÜ EKSTRAKTI", name_tr="LICORICE EXTRACT LIQ", sku="LIC-1", category="Hammadde",
               unit="g", domain="cosmetics")
    # Aynı malzemenin iki kartı (lab her tedarikçiye ayrı kart tutar) → eşit puan → öneri YOK
    gin1 = Item(name="GINSENG EKSTRAKTI", name_tr="GINSENG EXTRACT LIQ", sku="GIN-1", category="Hammadde", unit="g",
                domain="cosmetics")
    gin2 = Item(name="GİNSENG EKSTRAKTI", name_tr="GINSENG EXTRACT LIQ", sku="GIN-2", category="Hammadde", unit="g",
                domain="cosmetics")
    krem = Item(name="LICORICE KREM", sku="KREM-1", category="Bitmiş Ürün", unit="adet", domain="cosmetics")
    db.add_all([cct, lic, gin1, gin2, krem]); db.commit()
    return {"sup": sup, "cct": cct, "lic": lic, "gin1": gin1, "gin2": gin2}


def test_suggest_item_only_unique_high_score(db_session: Session):
    s = _seed(db_session)
    idx = PP.build_item_index(db_session, "cosmetics")
    assert all(it.category != "Bitmiş Ürün" for it, *_ in idx)
    r = PP.suggest_item(db_session, {"product_name": "LICORICE EXTRACT LIQ"}, "cosmetics", index=idx)
    assert r["suggested"] and r["suggested"]["id"] == s["lic"].id and r["suggested"]["score"] >= 60
    r = PP.suggest_item(db_session, {"product_name": "GINSENG EXTRACT LIQ"}, "cosmetics", index=idx)
    assert r["suggested"] is None and len(r["candidates"]) >= 2        # eşit puanlı iki kart → öneri yok
    r = PP.suggest_item(db_session, {"product_name": "CAPRYLİC / CAPRİC TRİGLYCERİDE"}, "cosmetics", index=idx,
                        supplier_id=s["sup"].id)
    assert r["suggested"]["id"] == s["cct"].id
    assert PP.suggest_item(db_session, {"product_name": "ARAS STANDART KARGO"}, "cosmetics", index=idx)["suggested"] is None
    assert PP.suggest_item(db_session, {"product_name": None, "description": None}, "cosmetics", index=idx) == \
        {"suggested": None, "candidates": []}
    assert db_session.query(SupplierPrice).count() == 0


def _real_cards(db):
    """Canlıdaki gibi kartlar: TR adı BOŞ (name_tr=None), lab'ın yazımıyla."""
    orl = Supplier(name="ORLIFE GLOBAL", domain="cosmetics")
    nat = Supplier(name="NATURALYA KİMYA", domain="cosmetics")
    db.add_all([orl, nat]); db.flush()
    spec = {
        "carnauba": ("CARNAUBA WAX", "g", None), "akgunluk": ("AKGÜNLÜK UÇUCU YAĞI", "ml", orl.id),
        "ipek": ("HİDROLİZE İPEK PROTEİN", "g", None), "gin1": ("GİNSENG EKSTRATI", "g", None),
        "gin2": ("GİNSENG EKSTRAKTI", "ml", nat.id), "lav": ("LAVANTA UÇUCU YAĞI", "ml", None),
        "tibbi": ("TIBBI LAVANTA UÇUCU YAĞI", "ml", None), "melez": ("MELEZ LAVANTA UCUCU YAĞ", "ml", None),
        "lavyag": ("LAVANTA YAĞI", "ml", orl.id), "olivem": ("OLİVEM 1000", "g", None),
        "shea": ("SHEA YAĞI", "g", None), "meyan": ("MEYAN KÖKÜ EKSTRAKTI", "g", None),
        # Aynı malzemenin iki firmaya ayrı kartı (lab düzeni)
        "ber_a": ("BERGAMOT UÇUCU YAĞI", "ml", orl.id), "ber_b": ("BERGAMOT UÇUCU YAĞI", "ml", nat.id),
    }
    its = {}
    for k, (name, unit, sid) in spec.items():
        its[k] = Item(name=name, name_tr=None, sku=None, category="Hammadde", unit=unit, domain="cosmetics",
                      supplier_id=sid)
        db.add(its[k])
    db.commit()
    return its, orl, nat


def _sug(db, idx, text, supplier_id=None):
    return PP.suggest_item(db, {"product_name": text}, "cosmetics", index=idx, supplier_id=supplier_id)


def test_suggest_item_reverse_containment_on_real_card_names(db_session: Session):
    """Paraşüt ürün adı kart adını İÇERİR (Belge Oku'nun kuralı tersini arar):
    KARNAUBA 40 puanda, AKGÜNLÜK aday bile değildi, İPEK 42'de kalıyordu."""
    its, orl, nat = _real_cards(db_session)
    idx = PP.build_item_index(db_session, "cosmetics")
    r = _sug(db_session, idx, "KARNAUBA MUMU (CARNAUBA WAX)")
    assert r["suggested"]["id"] == its["carnauba"].id and r["suggested"]["score"] >= 80
    r = _sug(db_session, idx, "DUY.FRK-SE.07 AKGÜNLÜK UÇUÇU YAĞI (Boswellia serrata) 10 ml.")
    assert r["suggested"]["id"] == its["akgunluk"].id and 80 <= r["suggested"]["score"] < 85
    r = _sug(db_session, idx, "DUY.FRK-SE.07 AKGÜNLÜK UÇUÇU YAĞI (Boswellia serrata) 10 ml.", supplier_id=orl.id)
    assert r["suggested"]["id"] == its["akgunluk"].id and r["suggested"]["score"] >= 85        # firmanın kartı +5
    r = _sug(db_session, idx, "Hidrolize İpek Proteini 100 ml")                                 # ek: PROTEİN → PROTEİNİ
    assert r["suggested"]["id"] == its["ipek"].id and r["suggested"]["score"] >= 80
    # İki GİNSENG kartı (biri ml, biri g) — ne kapsama ne tekil yüksek puan → öneri yok
    r = _sug(db_session, idx, "GINSENG EXTRACT LIQ")
    assert r["suggested"] is None
    assert r["candidates"] and all(c["score"] < PP.SUGGEST_MIN_SCORE for c in r["candidates"])
    assert _sug(db_session, idx, "GINSENG EXTRACT LIQ", supplier_id=nat.id)["suggested"] is None
    # Özgüllük: daha uzun (daha özgül) kapsanan kart kazanır; kısa metinde kısa kart
    r = _sug(db_session, idx, "TIBBİ LAVANTA UÇUCU YAĞI 1 LT")
    ids = [c["id"] for c in r["candidates"]]
    assert r["suggested"]["id"] == its["tibbi"].id and its["lav"].id in ids
    assert ids.index(its["tibbi"].id) < ids.index(its["lav"].id)
    assert _sug(db_session, idx, "LAVANTA UÇUCU YAĞI 1 LT")["suggested"]["id"] == its["lav"].id
    # Orlife biçimi (kod + Latince ad metnin anahtarını sulandırır): özgül kart yine kazanır,
    # genel kartın faturanın firmasına ait olması (+5) da bunu değiştirmez
    orl_tibbi = "DUY.LAV.01 TIBBİ LAVANTA UÇUCU YAĞI (Lavandula angustifolia) 10 ml."
    for sup in (None, orl.id, nat.id):
        r = _sug(db_session, idx, orl_tibbi, supplier_id=sup)
        assert r["suggested"] and r["suggested"]["id"] == its["tibbi"].id, sup
        assert _scores(r).get(its["lav"].id, 0) < 80, sup
    assert _sug(db_session, idx, "DUY.LAV.02 LAVANTA UÇUCU YAĞI (Lavandula) 10 ml.")["suggested"]["id"] == its["lav"].id
    # Aynı ada sahip iki firma kartı: firmasız eşit → öneri yok; faturanın firması → onun kartı
    assert _sug(db_session, idx, "BERGAMOT UÇUCU YAĞI 50 ML")["suggested"] is None
    assert _sug(db_session, idx, "BERGAMOT UÇUCU YAĞI 50 ML", supplier_id=nat.id)["suggested"]["id"] == its["ber_b"].id
    # Kelime ortasında başlayan kapsama sayılmaz; tek kelimelik / kısa kart adı ters kapsamaya girmez
    assert all(c["score"] < 80 for c in _sug(db_session, idx, "XLAVANTA UÇUCU YAĞI")["candidates"]
               if c["id"] in (its["lav"].id, its["tibbi"].id))
    # Gider / ilgisiz satır öneri almaz
    for q in ("ARAS STANDART KARGO", "Dış hat iade bedeli", "Araç Kiralama Bedeli", "KARGO TAŞIMA BEDELİ"):
        assert _sug(db_session, idx, q)["suggested"] is None, q
    assert db_session.query(SupplierPrice).count() == 0


def test_suggest_item_short_or_numeric_text_gets_no_suggestion(db_session: Session):
    """Gerçek Kariyer.net satırının ürün adı '1' — alt-dizgi kuralı 'OLİVEM 1000'i
    80,9 puanla öneriyordu; 'A' → 'SHEA YAĞI'."""
    its, *_ = _real_cards(db_session)
    idx = PP.build_item_index(db_session, "cosmetics")
    for q in ("1", "10", "A", " a ", "1 A", "%20", "Ab 1"):
        assert _sug(db_session, idx, q) == {"suggested": None, "candidates": []}, q
    # Açıklamaya düşen kısa metin de aynı kural
    assert PP.suggest_item(db_session, {"product_name": None, "description": "1"}, "cosmetics",
                           index=idx) == {"suggested": None, "candidates": []}
    # 3 harf eşiği gerçek adları kesmez
    assert _sug(db_session, idx, "SHEA YAĞI")["suggested"]["id"] == its["shea"].id


def _mk_cards(db, spec):
    """spec: {anahtar: (ad, birim, tedarikçi id | None, kategori)} — canlıdaki gibi TR adı boş."""
    its = {}
    for k, (name, unit, sid, cat) in spec.items():
        its[k] = Item(name=name, name_tr=None, sku=None, category=cat, unit=unit, domain="cosmetics", supplier_id=sid)
        db.add(its[k])
    db.commit()
    return its


def _scores(r):
    return {c["id"]: c["score"] for c in r["candidates"]}


def test_suggest_item_keeps_belge_oku_answer_and_drops_generic_reverse_card(db_session: Session):
    """Doğrulayıcı bulgusu (gerçek kart adları): ters kapsama metnin içindeki
    GENEL kartı 80+ ile öne çıkarıp Belge Oku'nun doğru tam-anahtar cevabını (60)
    eziyordu — CMC ≠ metil selüloz, ekstre ≠ kök, karışım ≠ saf yüzey aktif.
    Belge Oku'nun kesin cevabı artık hiç değişmez; ters kapsamada da daha özgül
    kartın kapsadığı genel kart düşer."""
    from core.assistant.documents import _item_candidates
    hl = Supplier(name="HAMMADDECİ", domain="cosmetics")
    db_session.add(hl); db_session.flush()
    its = _mk_cards(db_session, {
        "cmc": ("KARBOKSİ METİL SELÜLOZ TEKNİK(CMC)", "g", None, "Hammadde"),
        "metil": ("METİL SELÜLOZ", "g", hl.id, "Hammadde"),
        "meyan": ("MEYAN KÖKÜ", "g", hl.id, "Hammadde"),
        "meyan_ext": ("MEYAN KÖKÜ EKSTRAKTI", "g", None, "Hammadde"),
        "coco": ("COCO GLUCOSIDE", "g", hl.id, "Hammadde"),
        "blend": ("COCO GLUCOSİDE- GLYCERYL OLEATE", "g", None, "Hammadde"),
    })
    idx = PP.build_item_index(db_session, "cosmetics")
    for text, key in (("CMC KARBOKSİ METİL SELÜLOZ TEKNİK 25 KG", "cmc"),
                      ("MEYAN KÖKÜ EKSTRAKT 1 KG", "meyan_ext"),
                      ("COCO GLUCOSIDE & GLYCERYL OLEATE", "blend")):
        for sup in (None, hl.id):                    # genel kart faturanın firmasına ait olsa da (+5) özgül kart kalır
            r = _sug(db_session, idx, text, supplier_id=sup)
            assert r["suggested"] and r["suggested"]["id"] == its[key].id, (text, sup)
    # Özgül kart metni tam karşılamıyor (TEKNİK yok) → genel kart yine düşer, öneri yok
    r = _sug(db_session, idx, "KARBOKSİ METİL SELÜLOZ (CMC) 25 KG")
    assert r["suggested"] is None and its["cmc"].id in _scores(r)
    assert _scores(r).get(its["metil"].id, 0) < 80
    r = _sug(db_session, idx, "LAMESOFT PO 65 (COCO GLUCOSIDE GLYCERYL OLEATE) 25 KG")
    assert r["suggested"] is None and _scores(r).get(its["coco"].id, 0) < 80
    # Özgül kart metne fazladan bir şey katmıyorsa genel kart kalır (ters kapsamanın amacı)
    assert _sug(db_session, idx, "MEYAN KÖKÜ TOZU 1 KG")["suggested"]["id"] == its["meyan"].id
    assert _sug(db_session, idx, "DUY.X-01 METİL SELÜLOZ 25 kg.")["suggested"]["id"] == its["metil"].id
    # Değişmez: Belge Oku'nun kesin cevabı (tek, ≥60) varsa öneri AYNEN odur
    texts = ("CMC KARBOKSİ METİL SELÜLOZ TEKNİK 25 KG", "KARBOKSİ METİL SELÜLOZ", "METİL SELÜLOZ 25 KG",
             "MEYAN KÖKÜ EKSTRAKTI 1 KG", "MEYAN KÖKÜ 1 KG", "COCO GLUCOSIDE 25 KG",
             "COCO GLUCOSIDE & GLYCERYL OLEATE", "MEYAN KÖKÜ EKSTRAKT 1 KG")
    seen = 0
    for text in texts:
        for sup in (None, hl.id):
            pick = PP._belge_oku_pick(_item_candidates(idx, {"material": text, "material_code": None}, sup))
            if pick is not None:
                seen += 1
                assert _sug(db_session, idx, text, supplier_id=sup)["suggested"]["id"] == pick["id"], (text, sup)
    assert seen >= 10


def test_reverse_span_only_possessive_endings():
    """Kart adı kelime ortasında yalnız iyelik / belirtme / kesik İngilizce ekiyle
    biter; "-lı" (ile), "-sız" (olmadan), çoğul ve rakam devamı kapsama değildir."""
    span = PP._reverse_span
    assert span("hidrolize ipek protein", "hidrolize ipek proteini 100 ml") == (0, 22)
    assert span("coco glucosid", "coco glucoside 25 kg") == (0, 13)
    assert span("gul suyu aroma", "gul suyu aromasi") == (0, 14)
    assert span("akgunluk ucucu yagi", "duy.frk-se.07 akgunluk ucucu yagi (boswellia serrata) 10 ml.") == (14, 33)
    assert span("50 ml gold kapak", "50 ml gold kapakli cam kavanoz") is None
    assert span("gold sprey pompa", "gold sprey pompali 100 ml cam sise") is None
    assert span("cam kavanoz kapak", "50 ml cam kavanoz kapaksiz") is None
    assert span("pet sise kapak", "pet sise kapaklar") is None
    assert span("olivem 1000", "olivem 10000") is None
    assert span("lavanta ucucu yagi", "xlavanta ucucu yagi") is None
    # ilk geçiş geçersizse sonraki geçerli geçiş bulunur
    assert span("gold kapak", "gold kapakli kavanoz + gold kapak") == (23, 33)
    assert span("gold kapak", "gold kapak") is not None and span("kapak", "kapak") is None   # tek kelime girmez


def test_suggest_item_bundle_lines_and_with_suffix_get_no_wrong_card(db_session: Session):
    """Doğrulayıcı bulgusu (ambalaj firmasının gerçek yazımı): "-lı" eki kavanozu
    kapağına, şişeyi pompasına bağlıyordu; "A + B" satırında 0,1 puan uzun ad
    kazanıyordu.  Artık: "-lı" kapsama değil, iki ayrı kalem → öneri yok (firma
    +5'i de seçtirmez), ikisi de aday."""
    amb = Supplier(name="AMBALAJCI", domain="cosmetics")
    db_session.add(amb); db_session.flush()
    its = _mk_cards(db_session, {
        "cap": ("50 ML GOLD KAPAK", "adet", amb.id, "Ambalaj"),
        "pump": ("GOLD SPREY POMPA", "adet", amb.id, "Ambalaj"),
        "bottle": ("100 ML CAM ŞİŞE", "adet", None, "Ambalaj"),
        "pet": ("100 ML PET  ŞEFFAF ŞİŞE", "adet", None, "Ambalaj"),
        "white": ("100ML BEYAZ KAPAK", "adet", None, "Ambalaj"),
        "jar": ("50 ML CAM KAVANOZ", "adet", None, "Ambalaj"),
        # İki kalemin sözcüklerini birden taşıyan uzak kart bir kalemi "genel" diye düşürmemeli
        "far": ("BÜYÜK BOY SİYAH CAM ŞİŞE GOLD KAPAK", "adet", None, "Ambalaj"),
    })
    idx = PP.build_item_index(db_session, "cosmetics")
    for sup in (None, amb.id):
        r = _sug(db_session, idx, "50 ML GOLD KAPAKLI CAM KAVANOZ", supplier_id=sup)     # kapaklı kavanoz ≠ kapak
        assert r["suggested"] is None and _scores(r).get(its["cap"].id, 0) < 80
        r = _sug(db_session, idx, "GOLD SPREY POMPALI 100 ML CAM ŞİŞE", supplier_id=sup)  # pompalı şişe = şişe
        assert r["suggested"]["id"] == its["bottle"].id and _scores(r).get(its["pump"].id, 0) < 80
        for text, a, b in (("100 ML CAM ŞİŞE + GOLD SPREY POMPA", "bottle", "pump"),
                           ("100 ML PET ŞEFFAF ŞİŞE + 100ML BEYAZ KAPAK", "pet", "white"),
                           ("50 ML CAM KAVANOZ + 50 ML GOLD KAPAK", "jar", "cap")):
            r = _sug(db_session, idx, text, supplier_id=sup)
            sc = _scores(r)
            assert r["suggested"] is None, (text, sup)
            assert sc[its[a].id] >= 80 and sc[its[b].id] >= 80, (text, sup)
    assert _sug(db_session, idx, "50 ML CAM KAVANOZ BEYAZ KAPAKLI")["suggested"]["id"] == its["jar"].id
    assert _sug(db_session, idx, "ROLL ON 100 ML CAM ŞİŞE LACİVERT")["suggested"]["id"] == its["bottle"].id


def test_suggest_item_same_material_twin_cards_need_bill_supplier(db_session: Session):
    """Aynı malzemenin firma başına iki kartı ikisi de metinde geçiyorsa yalnız
    faturanın firması (+5) seçer — kapsama farkı (0,1 puan) seçtirmez."""
    a = Supplier(name="FİRMA A", domain="cosmetics")
    nil = Supplier(name="NİLKİM", domain="cosmetics")
    db_session.add_all([a, nil]); db_session.flush()
    its = _mk_cards(db_session, {
        "misk_a": ("MİSK ADAÇAYI UCUCU YAĞ", "ml", a.id, "Hammadde"),
        "misk": ("MİSK ADAÇAYI UÇUCU YAĞI", "ml", None, "Hammadde"),
        "carn": ("CARNAUBA WAX", "g", nil.id, "Hammadde"),
        "karn": ("KARNAUBA MUMU", "g", a.id, "Hammadde"),
    })
    idx = PP.build_item_index(db_session, "cosmetics")
    r = _sug(db_session, idx, "MİSK ADAÇAYI UÇUCU YAĞI 10 ML")
    assert r["suggested"] is None and {its["misk"].id, its["misk_a"].id} <= set(_scores(r))
    assert _sug(db_session, idx, "MİSK ADAÇAYI UÇUCU YAĞI 10 ML", supplier_id=a.id)["suggested"]["id"] == its["misk_a"].id
    r = _sug(db_session, idx, "KARNAUBA MUMU (CARNAUBA WAX)")
    assert r["suggested"] is None and {its["carn"].id, its["karn"].id} <= set(_scores(r))
    assert _sug(db_session, idx, "KARNAUBA MUMU (CARNAUBA WAX)", supplier_id=nil.id)["suggested"]["id"] == its["carn"].id
    assert _sug(db_session, idx, "KARNAUBA MUMU (CARNAUBA WAX)", supplier_id=a.id)["suggested"]["id"] == its["karn"].id


def test_suggest_item_packaging_part_and_one_part_set_lines_get_no_container(db_session: Session):
    """Doğrulayıcı bulgusu (2. tur): Türkçede baş isim sondadır — "50 ML CAM
    KAVANOZ KAPAĞI" kavanozun kapağıdır, "100 ML CAM ŞİŞE POMPASI" şişenin
    pompası; ters kapsama kabı 80+ ile öneriyordu (Belge Oku'nun kuralı hiçbirine
    öneri vermez).  Gerçek dökümde "24 PP GOLD SIVI SABUN POMPASI".  Tek kalemi
    kartlı "A + B" set satırı da kaba bağlanıyordu."""
    amb = Supplier(name="AMBALAJCI", domain="cosmetics")
    db_session.add(amb); db_session.flush()
    its = _mk_cards(db_session, {
        "jar": ("50 ML CAM KAVANOZ", "adet", amb.id, "Ambalaj"),
        "bottle": ("100 ML CAM ŞİŞE", "adet", amb.id, "Ambalaj"),
        "roll": ("ROLL ON CAM ŞİŞE", "adet", None, "Ambalaj"),
        "cap": ("50 ML GOLD KAPAK", "adet", None, "Ambalaj"),
        "pump": ("GOLD SPREY POMPA", "adet", None, "Ambalaj"),
        "soap": ("24 PP GOLD SIVI SABUN", "adet", None, "Ambalaj"),
        "meyan": ("MEYAN KÖKÜ", "g", None, "Hammadde"),
    })
    idx = PP.build_item_index(db_session, "cosmetics")
    parts = (("50 ML CAM KAVANOZ KAPAĞI", "jar"), ("50 ML CAM KAVANOZU KAPAĞI", "jar"),
             ("100 ML CAM ŞİŞE POMPASI", "bottle"), ("ROLL ON CAM ŞİŞE BİLYESİ", "roll"),
             ("50 ML GOLD KAPAK CONTASI", "cap"), ("GOLD SPREY POMPA KAPAĞI", "pump"),
             ("100 ML CAM ŞİŞE İÇİN SPREY POMPA", "bottle"), ("24 PP GOLD SIVI SABUN POMPASI", "soap"))
    for sup in (None, amb.id):                       # firmanın kabı olsa da (+5) parça satırı kaba bağlanmaz
        for text, key in parts:
            r = _sug(db_session, idx, text, supplier_id=sup)
            assert r["suggested"] is None, (text, sup)
            assert _scores(r).get(its[key].id, 0) < 80, (text, sup)
        # Set satırı, yalnız bir kalemin kartı var: öneri yok, kap aday olarak kalır
        r = _sug(db_session, idx, "50 ML CAM KAVANOZ + 70 PP GOLD METALİZE KAPAK", supplier_id=sup)
        assert r["suggested"] is None and _scores(r)[its["jar"].id] >= 80, sup
        r = _sug(db_session, idx, "70 PP GOLD METALİZE KAPAK + 100 ML CAM ŞİŞE", supplier_id=sup)
        assert r["suggested"] is None and _scores(r)[its["bottle"].id] >= 80, sup
    # Kabın kendisini niteleyen kelime parça değildir: ek, "-lı" sıfatı, renk, ölçü
    for text, key in (("50 ML CAM KAVANOZU", "jar"), ("50 ML CAM KAVANOZ KAPAKLI", "jar"),
                      ("100 ML CAM ŞİŞE MAVİ", "bottle"), ("100 ML CAM ŞİŞE LACİVERT", "bottle"),
                      ("GOLD SPREY POMPALI 100 ML CAM ŞİŞE", "bottle"), ("ROLL ON CAM ŞİŞE 10 ML", "roll")):
        assert _sug(db_session, idx, text)["suggested"]["id"] == its[key].id, text
    # Kural yalnız AMBALAJ kartında: hammaddede iyelikli sonraki kelime kartı düşürmez
    assert _sug(db_session, idx, "MEYAN KÖKÜ TOZU 1 KG")["suggested"]["id"] == its["meyan"].id
    assert PP._part_head_after("50 ml cam kavanoz kapagi", 17)
    assert PP._part_head_after("100 ml cam sise icin pompa", 15)
    assert not PP._part_head_after("50 ml cam kavanoz kapakli", 17)
    assert not PP._part_head_after("50 ml cam kavanoz 24 mm", 17)
    assert not PP._part_head_after("50 ml cam kavanoz", 17)


def test_suggest_item_short_text_inside_several_card_names_is_a_tie(db_session: Session):
    """Doğrulayıcı bulgusu (2. tur): Belge Oku'nun alt-dizgi kuralında kısa / genel
    metin birden çok kart adında geçince en kısa ad 0,1 puanla (81,0 / 80,9)
    "tek" sayılıyordu ("Kil" → MAVİ KİL, "Etil" → ETİL ALKOL — METİL'in içinden).
    Artık öneri yok, adaylar kalır; firma +5'i ve kartın kendi adı yine seçer."""
    a = Supplier(name="KİLCİ", domain="cosmetics")
    b = Supplier(name="GÜLCÜ", domain="cosmetics")
    db_session.add_all([a, b]); db_session.flush()
    its = _mk_cards(db_session, {
        "mavi": ("MAVİ KİL", "g", a.id, "Hammadde"), "bent": ("BENTONİT KİL", "g", None, "Hammadde"),
        "etil": ("ETİL ALKOL", "ml", None, "Hammadde"), "setil": ("SETİL STEARİL ALKOL", "g", None, "Hammadde"),
        "metil": ("METİL SELÜLOZ", "g", None, "Hammadde"),
        "gul": ("GÜL SUYU", "ml", None, "Hammadde"), "saf": ("SAF GÜL SUYU", "ml", b.id, "Hammadde"),
    })
    idx = PP.build_item_index(db_session, "cosmetics")
    for text, keys in (("Kil", ("mavi", "bent")), ("Alkol", ("etil", "setil")), ("Etil", ("etil", "metil")),
                       ("Gül", ("gul", "saf"))):
        r = _sug(db_session, idx, text)
        assert r["suggested"] is None, text
        assert all(_scores(r)[its[k].id] >= 80 for k in keys), text
    # Faturanın firması farkı açar (+5) → onun kartı
    assert _sug(db_session, idx, "Kil", supplier_id=a.id)["suggested"]["id"] == its["mavi"].id
    assert _sug(db_session, idx, "Gül", supplier_id=b.id)["suggested"]["id"] == its["saf"].id
    # Metin kartın kendi adıysa eşitlik değil, eşleşmedir
    assert _sug(db_session, idx, "GÜL SUYU")["suggested"]["id"] == its["gul"].id
    assert _sug(db_session, idx, "ETİL ALKOL")["suggested"]["id"] == its["etil"].id
    # Metin tek kartı karşılıyorsa kesin cevap aynen kalır
    assert _sug(db_session, idx, "Mavi Kil")["suggested"]["id"] == its["mavi"].id


def test_run_sync_upserts_suggests_watermark_notifies_and_writes_no_price(db_session: Session, monkeypatch):
    s = _seed(db_session)
    pushed = []
    import core.notifications as N
    monkeypatch.setattr(N, "notify_parasut_queue", lambda count, samples: pushed.append((count, list(samples))))
    api = FakeApi([_payload([B_NAT, B_NILKIM, B_ORLIFE, B_ARAS])])
    res = PP.run_sync(db_session, api)
    assert res["bills"] == 4 and res["bills_new"] == 4 and res["lines_new"] == 6 and res["pending"] == 6
    assert res["watermark"] == "2026-08-14" and PP.get_watermark(db_session) == date(2026, 8, 14)
    assert pushed == [(6, pushed[0][1])] and pushed[0][1]
    assert db_session.query(SupplierPrice).count() == 0                 # senkron fiyat YAZMAZ
    assert all(p == "/purchase_bills" for p, _ in api.calls)
    lines = {ln.parasut_detail_id: ln for ln in db_session.query(ParasutPurchaseLine).all()}
    assert lines[5005].suggested_item_id == s["lic"].id and lines[5006].suggested_item_id is None
    assert lines[5001].suggested_item_id == s["cct"].id                 # firmanın kartı (+5) tekil
    assert lines[5002].expense_hint is True and lines[5004].expense_hint is True
    assert all(ln.status == "pending" for ln in lines.values())
    bill = db_session.query(ParasutPurchaseBill).filter(ParasutPurchaseBill.parasut_id == 1003).one()
    assert bill.expense_hint is True and bill.currency == "TRY"
    # İkinci okuma: aynı faturalar → yeni satır yok, bildirim yok, su işareti geri sarmaz
    res2 = PP.run_sync(db_session, api)
    assert res2["lines_new"] == 0 and res2["bills_new"] == 0 and len(pushed) == 1
    assert res2["since"] == (date(2026, 8, 14) - timedelta(days=PP.OVERLAP_DAYS)).isoformat()
    assert db_session.query(ParasutPurchaseLine).count() == 6
    # Sıra: gider olmayanlar önce, yeni fatura önce
    order = [ln.parasut_detail_id for ln in PP.pending_lines(db_session)]
    assert order[:2] == [5005, 5006] and order[-1] == 5004


def test_run_sync_stop_flag_is_not_an_error(db_session: Session):
    api = FakeApi([_payload([B_NAT])])
    PP.request_stop()
    with pytest.raises(PP.ParasutPurchasesStopped):
        PP.run_sync(db_session, api, notify=False)
    assert not PP.is_running() and db_session.query(ParasutPurchaseBill).count() == 0


def test_scheduler_job_gated_by_config_and_setting(db_session: Session, monkeypatch):
    from core import scheduler as S
    calls = []
    monkeypatch.setattr(PP, "run_sync", lambda db, *a, **k: calls.append(1) or {"bills": 0, "lines_new": 0})
    monkeypatch.setattr(P, "is_configured", lambda: False)
    S.parasut_purchases_sync()
    assert calls == []
    monkeypatch.setattr(P, "is_configured", lambda: True)
    S.parasut_purchases_sync()                                           # ayar yok → varsayılan kapalı
    assert calls == []
    PP.set_enabled(db_session, True)
    assert db_session.query(AppSetting).filter(AppSetting.key == PP.SETTING_ENABLED).one().value == "1"
    S.parasut_purchases_sync()
    assert calls == [1]
    PP.set_enabled(db_session, False)
    S.parasut_purchases_sync()
    assert calls == [1]


# ─── Onay ───────────────────────────────────────────────────────────────────

def _bill_row(db, b, *, parasut_id=None):
    data, inc = b
    parsed = PP.parse_bill(data, PP._incmap({"included": inc}))
    if parasut_id is not None:
        parsed["parasut_id"] = parasut_id
        for i, ln in enumerate(parsed["lines"]):
            ln["parasut_detail_id"] = parasut_id * 10 + i
    row, lines, _ = PP.upsert_bill(db, parsed)
    db.commit()
    return row, lines


def test_approve_math_nilkim_625_per_25kg(db_session: Session):
    s = _seed(db_session)
    _, lines = _bill_row(db_session, B_NILKIM)
    ln = lines[0]
    res = PP.approve_line(db_session, ln.id, item_id=s["cct"].id, supplier_id=s["sup"].id, package_qty=25,
                          price_unit="kg", domain="cosmetics", actor="Songül Arslan")
    db_session.commit()
    sp = db_session.query(SupplierPrice).one()
    assert res["supplier_price_id"] == sp.id and res["kept_newer"] is False and res["net_unit_price"] == 25.0
    assert sp.unit_price == 25.0 and sp.currency == "TRY" and sp.price_unit == "kg" and sp.package_size == 25.0
    assert sp.source == "parasut" and sp.source_label == "Paraşüt alış faturası NIL2024000000554 — 29.07.2024"
    assert sp.quoted_at == date(2024, 7, 29) and sp.vat_included is False and sp.vat_rate == 20.0
    assert sp.supplier_id == s["sup"].id and sp.supplier_name == s["sup"].name and sp.created_by == "Songül Arslan"
    assert "625" in sp.note and "NIL2024000000554" in sp.note
    assert res["price"]["source"] == "parasut" and res["price"]["material"] == s["cct"].name
    db_session.refresh(ln)
    assert ln.status == "approved" and ln.chosen_item_id == s["cct"].id and ln.supplier_price_id == sp.id
    assert ln.package_qty == 25.0 and ln.price_unit == "kg" and ln.decided_by == "Songül Arslan"
    pk = db_session.query(ParasutProductPack).filter(ParasutProductPack.product_parasut_id == 777).one()
    assert pk.package_qty == 25.0 and pk.price_unit == "kg" and pk.item_id == s["cct"].id
    # Aynı satır ikinci kez → not_pending
    with pytest.raises(PP.ApproveError) as ei:
        PP.approve_line(db_session, ln.id, item_id=s["cct"].id, supplier_id=s["sup"].id, package_qty=25,
                        price_unit="kg", domain="cosmetics", actor="x")
    assert ei.value.code == "not_pending" and ei.value.http == 409
    # Kuyruk görünümü ambalajı hatırlar
    _, lines2 = _bill_row(db_session, B_NILKIM, parasut_id=2001)
    v = PP.line_view(db_session, lines2[0], "cosmetics")
    assert v["pack"] == {"package_qty": 25.0, "price_unit": "kg", "item_id": s["cct"].id} and v["net_preview"] == 25.0
    assert v["stale"] is True and v["suggestion"]["id"] == s["cct"].id and v["supplier"]["id"] == s["sup"].id


def test_approve_discount_and_validation(db_session: Session):
    s = _seed(db_session)
    orl = Supplier(name="ORLIFE GLOBAL", domain="cosmetics")
    akg = Item(name="AKGÜNLÜK UÇUCU YAĞI", sku="AKG-1", category="Hammadde", unit="ml", domain="cosmetics")
    db_session.add_all([orl, akg]); db_session.commit()
    _, lines = _bill_row(db_session, B_ORLIFE)
    ln = lines[0]
    for kw, code in (({"package_qty": 0}, "bad_package"), ({"price_unit": "adet"}, "bad_unit"),
                     ({"item_id": 10 ** 6}, "item_not_found"), ({"supplier_id": 10 ** 6}, "supplier_not_found")):
        args = dict(item_id=akg.id, supplier_id=orl.id, package_qty=0.01, price_unit="l")
        args.update(kw)
        with pytest.raises(PP.ApproveError) as ei:
            PP.approve_line(db_session, ln.id, domain="cosmetics", actor="x", **args)
        assert ei.value.code == code, code
        db_session.rollback()
    res = PP.approve_line(db_session, ln.id, item_id=akg.id, supplier_name="Orlife (faturadaki ad)", package_qty=0.01,
                          price_unit="l", domain="cosmetics", actor="x")
    db_session.commit()
    sp = db_session.query(SupplierPrice).get(res["supplier_price_id"])
    assert sp.unit_price == pytest.approx(249.96 * 0.8 / 0.01) and sp.supplier_id is None
    assert sp.supplier_name == "Orlife (faturadaki ad)" and sp.price_unit == "l" and "indirim %20" in sp.note


def test_newest_wins_old_bill_keeps_and_manual_blocks(db_session: Session):
    s = _seed(db_session)
    old = _bill(3001, "NIL2024000000100", "2024-01-10", NILKIM, [
        (7001, 777, "CAPRYLİC / CAPRİC TRİGLYCERİDE", "Adet", 1, 500.0, 20.0, 600.0, None)])
    mid = B_NILKIM
    new = _bill(3002, "NIL2024000000900", "2024-11-20", NILKIM, [
        (7002, 777, "CAPRYLİC / CAPRİC TRİGLYCERİDE", "Adet", 1, 700.0, 20.0, 840.0, None)])
    _, l_mid = _bill_row(db_session, mid)
    _, l_new = _bill_row(db_session, new)
    _, l_old = _bill_row(db_session, old)
    kw = dict(item_id=s["cct"].id, supplier_id=s["sup"].id, package_qty=25, price_unit="kg", domain="cosmetics", actor="x")
    r1 = PP.approve_line(db_session, l_mid[0].id, **kw); db_session.commit()
    r2 = PP.approve_line(db_session, l_new[0].id, **kw); db_session.commit()
    assert r2["supplier_price_id"] == r1["supplier_price_id"] and r2["kept_newer"] is False   # yerinde, yeni kazanır
    sp = db_session.query(SupplierPrice).one()
    assert sp.unit_price == 28.0 and sp.quoted_at == date(2024, 11, 20) and "NIL2024000000900" in sp.source_label
    r3 = PP.approve_line(db_session, l_old[0].id, **kw); db_session.commit()
    assert r3["kept_newer"] is True and r3["supplier_price_id"] == sp.id
    db_session.refresh(sp)
    assert sp.unit_price == 28.0 and sp.quoted_at == date(2024, 11, 20)                     # eski fatura ezmedi
    assert db_session.query(ParasutPurchaseLine).get(l_old[0].id).status == "approved"
    assert db_session.query(SupplierPrice).count() == 1
    # Elle fiyat engeller (aynı firma, ad anahtarıyla da)
    db_session.add(SupplierPrice(item_id=s["cct"].id, supplier_id=None, supplier_name="Nilkim Teknik Kimya",
                                 unit_price=30.0, currency="TRY", price_unit="kg", source="manual", domain="cosmetics"))
    db_session.commit()
    _, l4 = _bill_row(db_session, B_NILKIM, parasut_id=3003)
    with pytest.raises(PP.ApproveError) as ei:
        PP.approve_line(db_session, l4[0].id, **kw)
    assert ei.value.code == "manual_exists" and ei.value.http == 409
    db_session.rollback()
    assert db_session.query(ParasutPurchaseLine).get(l4[0].id).status == "pending"
    # Atlama
    PP.skip_line(db_session, l4[0].id, actor="x", reason="gider"); db_session.commit()
    assert db_session.query(ParasutPurchaseLine).get(l4[0].id).status == "skipped"


# ─── Eşzamanlı onay / atlama (iki oturum + iş parçacığı) ────────────────────

def _race(first, second, *, wait=0.6):
    """A (`first`) flush eder ama commit ETMEZ (kilitleri tutar); B (`second`)
    ayrı oturumda iş parçacığında koşar; A commit → B biter.  Döner: B'nin sonucu
    ("ok" | ApproveError kodu | başka hata adı) ve B'nin A commit'inden önce
    beklemede olup olmadığı."""
    from database import SessionLocal
    A, B = SessionLocal(), SessionLocal()
    out = {}
    try:
        first(A)

        def run_b():
            try:
                second(B)
                B.commit()
                out["B"] = "ok"
            except PP.ApproveError as e:
                B.rollback()
                out["B"] = e.code
            except Exception as e:                       # noqa: BLE001 — testte sınıf adı yeter
                B.rollback()
                out["B"] = type(e).__name__
        t = threading.Thread(target=run_b, daemon=True)
        t.start()
        t.join(wait)
        out["blocked"] = t.is_alive()
        A.commit()
        t.join(15)
        assert not t.is_alive(), "B kilitte takıldı"
    finally:
        A.rollback(); A.close()
        B.rollback(); B.close()
    return out


def _approve(line_id, item_id, sup_id, pq=25):
    return lambda db: PP.approve_line(db, line_id, item_id=item_id, supplier_id=sup_id, package_qty=pq,
                                      price_unit="kg", domain="cosmetics", actor="u")


def test_concurrent_approve_same_line_is_409_and_writes_one_price(db_session: Session):
    """İki sekme aynı satırı onaylar: ikincisi kilitte bekler, sonra 409 not_pending;
    tek supplier_prices satırı (eskiden 2 özdeş satır + yetim kayıt, ambalaj yoksa 500)."""
    s = _seed(db_session)
    _, lines = _bill_row(db_session, B_NILKIM)
    lid, cct, sup = lines[0].id, s["cct"].id, s["sup"].id
    res = _race(_approve(lid, cct, sup), _approve(lid, cct, sup))                 # ambalaj henüz yok
    assert res == {"B": "not_pending", "blocked": True}
    db_session.expire_all()
    sp = db_session.query(SupplierPrice).one()
    ln = db_session.query(ParasutPurchaseLine).get(lid)
    assert ln.status == "approved" and ln.supplier_price_id == sp.id
    assert db_session.query(ParasutProductPack).count() == 1
    # Ambalaj hatırlanmışken (2. faturadan itibaren olağan durum) de aynı
    _, l2 = _bill_row(db_session, B_NILKIM, parasut_id=2001)
    res = _race(_approve(l2[0].id, cct, sup), _approve(l2[0].id, cct, sup))
    assert res["B"] == "not_pending"
    db_session.expire_all()
    assert db_session.query(SupplierPrice).count() == 1


def test_concurrent_approve_two_bills_same_card_and_firm_keeps_one_row(db_session: Session):
    """İki farklı fatura aynı (kart, firma)'ya aynı anda: kart kilidi sıraya sokar,
    ikinci onay ilk satırı yerinde günceller — (kart, firma) başına TEK parasut satırı."""
    s = _seed(db_session)
    db_session.add(ParasutProductPack(product_parasut_id=777, package_qty=25, price_unit="kg"))
    db_session.commit()
    _, l1 = _bill_row(db_session, B_NILKIM)
    _, l2 = _bill_row(db_session, B_NILKIM, parasut_id=2001)
    cct, sup = s["cct"].id, s["sup"].id
    res = _race(_approve(l1[0].id, cct, sup), _approve(l2[0].id, cct, sup))
    assert res == {"B": "ok", "blocked": True}
    db_session.expire_all()
    sp = db_session.query(SupplierPrice).one()
    lines = db_session.query(ParasutPurchaseLine).filter(ParasutPurchaseLine.id.in_([l1[0].id, l2[0].id])).all()
    assert {ln.status for ln in lines} == {"approved"} and {ln.supplier_price_id for ln in lines} == {sp.id}


def test_concurrent_approve_same_product_to_two_cards_upserts_pack(db_session: Session):
    """Aynı Paraşüt ürünü iki satırda iki farklı karta aynı anda: ambalaj hafızası
    ON CONFLICT ile yazılır (eskiden ikinci istek IntegrityError → 500)."""
    s = _seed(db_session)
    _, l1 = _bill_row(db_session, B_NILKIM)
    _, l2 = _bill_row(db_session, B_NILKIM, parasut_id=2001)
    res = _race(_approve(l1[0].id, s["cct"].id, s["sup"].id),
                _approve(l2[0].id, s["lic"].id, s["sup"].id, pq=5))
    assert res["B"] == "ok"
    db_session.expire_all()
    pk = db_session.query(ParasutProductPack).one()
    assert pk.product_parasut_id == 777 and pk.item_id == s["lic"].id and pk.package_qty == 5.0
    assert db_session.query(SupplierPrice).count() == 2                  # iki ayrı kart → iki satır


def test_concurrent_skip_after_approve_is_409(db_session: Session):
    """Onay sürerken atlama: atlama satır kilidinde bekler, sonra 409 — onaylı
    satırın üstüne "atlandı" yazılmaz (fiyat satırı kuyruktan kopmaz)."""
    s = _seed(db_session)
    _, lines = _bill_row(db_session, B_NILKIM)
    lid = lines[0].id
    res = _race(_approve(lid, s["cct"].id, s["sup"].id),
                lambda db: PP.skip_line(db, lid, actor="v", reason="gider"))
    assert res == {"B": "not_pending", "blocked": True}
    db_session.expire_all()
    ln = db_session.query(ParasutPurchaseLine).get(lid)
    sp = db_session.query(SupplierPrice).one()
    assert ln.status == "approved" and ln.decided_by == "u" and ln.supplier_price_id == sp.id
    # Ters sıra: atlama önce → onay 409
    _, l2 = _bill_row(db_session, B_NILKIM, parasut_id=2001)
    res = _race(lambda db: PP.skip_line(db, l2[0].id, actor="v"), _approve(l2[0].id, s["cct"].id, s["sup"].id))
    assert res == {"B": "not_pending", "blocked": True}
    db_session.expire_all()
    assert db_session.query(ParasutPurchaseLine).get(l2[0].id).status == "skipped"
    assert db_session.query(SupplierPrice).count() == 1


def _lock_waiter(timeout=10.0) -> bool:
    """Bu test veritabanında bir oturum satır kilidinde bekliyor mu (her yoklama
    ayrı bağlantı — pg_stat_activity işlem içinde anlık görüntü tutar)."""
    import time
    from sqlalchemy import text
    from database import engine
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        with engine.connect() as c:
            n = c.execute(text("SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
                               "AND wait_event_type = 'Lock'")).scalar()
        if n:
            return True
        time.sleep(0.02)
    return False


def test_lock_for_approval_breaks_multi_line_vs_ui_cycle(db_session: Session):
    """Doğrulayıcı bulgusu: tek işlemde iki satır onaylayan çağıran (A: L1→CCT,
    sonra L2→LIC) ile arayüz onayı (B: L1→LIC — LIC'i tutar, L1'i bekler)
    kilitlenme döngüsü kuruyordu.  `lock_for_approval` kartları satırlardan
    önce alınca B LIC'te bekler, A biter, B 409 not_pending alır."""
    from database import SessionLocal
    s = _seed(db_session)
    _, l1 = _bill_row(db_session, B_NILKIM)
    _, l2 = _bill_row(db_session, B_NILKIM, parasut_id=2001)
    a, b, cct, lic, sup = l1[0].id, l2[0].id, s["cct"].id, s["lic"].id, s["sup"].id
    A, B = SessionLocal(), SessionLocal()
    out = {}

    def ui():
        try:
            _approve(a, lic, sup, pq=5)(B)
            B.commit()
            out["B"] = "ok"
        except PP.ApproveError as e:
            B.rollback()
            out["B"] = e.code
        except Exception as e:                           # noqa: BLE001
            B.rollback()
            out["B"] = type(e).__name__
    try:
        PP.lock_for_approval(A, [cct, lic], [a, b])
        _approve(a, cct, sup)(A)
        t = threading.Thread(target=ui, daemon=True)
        t.start()
        assert _lock_waiter()                            # B LIC kartında bekliyor
        _approve(b, lic, sup, pq=5)(A)                   # A'nın ikinci onayı beklemez (LIC zaten A'da)
        A.commit()
        t.join(15)
        assert not t.is_alive()
    finally:
        A.rollback(); A.close()
        B.rollback(); B.close()
    assert out["B"] == "not_pending"
    db_session.expire_all()
    l1r, l2r = (db_session.query(ParasutPurchaseLine).get(i) for i in (a, b))
    assert (l1r.chosen_item_id, l2r.chosen_item_id) == (cct, lic)
    assert db_session.query(SupplierPrice).count() == 2


def test_route_lock_conflict_gets_409_busy_and_writes_nothing(client: TestClient, db_session: Session,
                                                               monkeypatch):
    """Arayüz onayı kilidi alamazsa (kalan nadir kilitlenme ya da kilit zaman
    aşımı) 500 yerine 409 `busy` alır, hiçbir şey yarım yazılmaz; diğer işlem
    tamamlanır.  Deterministik: A satırı tutarken arayüz isteğinin işlemine
    `lock_timeout` konur → gerçek 55P03 → rollback + 409.  (Eskiden gerçek
    kilitlenme kuruluyordu; PostgreSQL kurbanı `deadlock_timeout` (1 sn) sonunda
    denetimi yapan süreçten seçtiği için yüklü makinede test iş parçacığı kurban
    oluyordu.  40P01 eşlemesi `test_lock_busy_mapping_only_for_lock_codes`'ta.)"""
    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from database import SessionLocal
    s = _seed(db_session)
    _, l1 = _bill_row(db_session, B_NILKIM)
    _, l2 = _bill_row(db_session, B_NILKIM, parasut_id=2001)
    a, b, cct, lic, sup = l1[0].id, l2[0].id, s["cct"].id, s["lic"].id, s["sup"].id
    _login(client, "songul")
    url = "/api/supplier-prices/parasut-queue"
    out = {}
    real_approve = PP.approve_line

    def approve_with_timeout(db, line_id, **kw):
        db.execute(text("SET LOCAL lock_timeout = '300ms'"))
        try:
            return real_approve(db, line_id, **kw)
        except OperationalError as e:
            out["pgcode"] = getattr(e.orig, "pgcode", None)
            raise

    def ui():
        r = client.post(f"{url}/{a}/approve", headers=_H,
                        json={"item_id": lic, "supplier_id": sup, "package_qty": 5, "price_unit": "kg"})
        out["r"] = (r.status_code, r.json())
    A = SessionLocal()
    try:
        _approve(a, cct, sup)(A)                         # A: CCT + L1 kilitli, commit yok
        monkeypatch.setattr(PP, "approve_line", approve_with_timeout)
        t = threading.Thread(target=ui, daemon=True)
        t.start()
        t.join(15)                                       # arayüz LIC'i alır, L1'de zaman aşımına düşer
        assert not t.is_alive()
        monkeypatch.setattr(PP, "approve_line", real_approve)
        _approve(b, lic, sup, pq=5)(A)                   # arayüz rollback etti → LIC boş, A beklemez
        A.commit()
    finally:
        A.rollback(); A.close()
    assert out.get("pgcode") == "55P03", out
    assert out["r"][0] == 409 and out["r"][1]["code"] == "busy", out
    assert "tekrar deneyin" in out["r"][1]["detail"]
    db_session.expire_all()
    l1r, l2r = (db_session.query(ParasutPurchaseLine).get(i) for i in (a, b))
    assert (l1r.status, l1r.chosen_item_id, l2r.chosen_item_id) == ("approved", cct, lic)
    assert db_session.query(SupplierPrice).count() == 2
    assert not db_session.query(AdminAuditLog).filter(
        AdminAuditLog.action == "supplier_prices.parasut_approve").count()


class _PgOrig(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.pgcode = code


def test_lock_busy_mapping_only_for_lock_codes(client: TestClient, db_session: Session, monkeypatch):
    from sqlalchemy.exc import OperationalError
    assert PP.is_lock_busy(OperationalError("SELECT 1", {}, _PgOrig("40P01")))
    assert PP.is_lock_busy(OperationalError("SELECT 1", {}, _PgOrig("55P03")))
    assert not PP.is_lock_busy(OperationalError("SELECT 1", {}, _PgOrig("08006")))
    assert not PP.is_lock_busy(ValueError("x"))
    s = _seed(db_session)
    _, lines = _bill_row(db_session, B_NILKIM)
    _login(client, "songul")
    url = "/api/supplier-prices/parasut-queue"

    def raiser(code):
        def f(*a, **kw):
            raise OperationalError("SELECT 1", {}, _PgOrig(code))
        return f
    monkeypatch.setattr(PP, "skip_line", raiser("55P03"))
    r = client.post(f"{url}/{lines[0].id}/skip", json={}, headers=_H)
    assert r.status_code == 409 and r.json()["code"] == "busy"
    monkeypatch.setattr(PP, "approve_line", raiser("40P01"))
    body = {"item_id": s["cct"].id, "supplier_id": s["sup"].id, "package_qty": 25, "price_unit": "kg"}
    r = client.post(f"{url}/{lines[0].id}/approve", json=body, headers=_H)
    assert r.status_code == 409 and r.json()["code"] == "busy"
    monkeypatch.setattr(PP, "approve_line", raiser("08006"))          # kilit dışı hata yutulmaz
    with pytest.raises(OperationalError):
        client.post(f"{url}/{lines[0].id}/approve", json=body, headers=_H)


# ─── Eski fatura koruması (plan + fiyat sayfası) ────────────────────────────

def test_stale_guard_in_purchase_plan(monkeypatch):
    from core import purchase_pricing as PR
    from core.consumption import ItemRec
    from core.purchase_plan import PlanInputs, compute
    from core.purchase_plan_models import PlanRequest
    from core.consumption import IngredientRec, RecipeRec
    monkeypatch.setattr(PR, "_today", lambda: date(2026, 10, 16))
    inp = PlanInputs(items={1: ItemRec(id=1, name="KARNAUBA MUMU", category="Hammadde", unit="g", pkg_type=None,
                                       current_stock=0.0)},
                     recipes={1: RecipeRec(id=1, name="Ürün", ingredients=(IngredientRec(item_id=1, quantity=100.0),))},
                     stock_as_of=datetime(2026, 10, 5, 9, 0))
    res = compute(inp, PlanRequest(lines=[{"recipe_id": 1, "qty": 1000}], options={}))

    def offer(sup, price, src, q):
        return PR.OfferRec(item_id=1, supplier_name=sup, unit_price=price, currency="TRY", price_unit="kg",
                           source=src, source_label=f"{src} {sup}", quoted_at=q)
    pin = PR.PriceInputs(offers={1: [offer("Nilkim", 16.5, "parasut", date(2024, 8, 16)),
                                     offer("Umaychem", 20.0, "stok_son_durum", date(2024, 8, 16)),
                                     offer("Naturalya", 18.0, "parasut", date(2026, 8, 14))]})
    PR.attach(res, pin, None, currency="TRY")
    m = next(x for x in res["materials"] if 1 in x["member_ids"])
    by = {o["name"]: o for o in m["offers"]}
    assert by["Nilkim"]["status"] == "stale" and by["Nilkim"]["eligible"] is False
    assert by["Nilkim"]["source"] == "parasut" and by["Nilkim"]["quoted_at"] == "2024-08-16"
    assert by["Umaychem"]["eligible"] is True and by["Naturalya"]["eligible"] is True    # liste tarihsiz kural, yeni fatura uygun
    assert m["supplier"] == "Naturalya" and m["price"] == 18.0
    assert res["pricing"]["stale_listed"] is True and res["pricing"]["status_used"] is False
    tags, blocked = PR.offer_tags(by["Nilkim"], by["Naturalya"])
    assert blocked == "eski fatura (12 aydan eski)"
    assert any("eski fatura" in t for t, _ in PR.offer_lines(m, "TRY"))
    # Seçim kuralı kapalıyken de eski fatura seçilmez
    PR.attach(res, pin, None, currency="TRY", respect_status=False)
    m = next(x for x in res["materials"] if 1 in x["member_ids"])
    assert m["supplier"] == "Naturalya"
    assert PR._stale_offer({"source": "parasut", "quoted_at": None}) is False
    assert PR._stale_offer({"source": "lab_notu", "quoted_at": "2020-01-01"}) is False
    assert PR._stale_offer({"source": "parasut", "quoted_at": date(2025, 10, 16)}) is False   # tam 365 gün: eski değil
    assert PR._stale_offer({"source": "parasut", "quoted_at": date(2025, 10, 15)}) is True


def test_overview_mirrors_stale_and_parasut_filter(db_session: Session, monkeypatch, client: TestClient):
    from core import purchase_pricing as PR
    from core.fx import Rates
    from core.price_overview import build_overview
    monkeypatch.setattr(PR, "_today", lambda: date(2026, 10, 16))
    s = _seed(db_session)
    um = Supplier(name="UMAYCHEM", domain="cosmetics"); db_session.add(um); db_session.flush()
    db_session.add_all([
        SupplierPrice(item_id=s["cct"].id, supplier_id=s["sup"].id, supplier_name=s["sup"].name, unit_price=25.0,
                      currency="TRY", price_unit="kg", source="parasut", quoted_at=date(2024, 7, 29),
                      source_label="Paraşüt alış faturası NIL2024000000554 — 29.07.2024", domain="cosmetics"),
        SupplierPrice(item_id=s["cct"].id, supplier_id=um.id, supplier_name="UMAYCHEM", unit_price=30.0,
                      currency="TRY", price_unit="kg", source="stok_son_durum", domain="cosmetics"),
    ])
    db_session.commit()
    rates = Rates(try_per={"USD": 40.0, "EUR": 44.0, "TRY": 1.0}, source="test", as_of=None, stale=False, warning=None)
    ov = build_overview(db_session, "cosmetics", currency="TRY", rates_fn=lambda: rates)
    card = next(c for b in ov["blocks"] for c in b["cards"] if c["item_id"] == s["cct"].id)
    rows = {r["supplier_name"]: r for r in card["rows"]}
    nil = rows[s["sup"].name]
    assert nil["status"] == "stale" and nil["status_kind"] == "blocked" and nil["eligible"] is False
    assert nil["status_blocked"] == "eski fatura (12 aydan eski)" and nil["source_filter"] == "parasut"
    assert nil["cheapest"] is False and rows["UMAYCHEM"]["plan_pick"] is True   # eski fatura "en ucuz" olmaz
    assert [r["supplier_name"] for r in card["rows"]][0] == "UMAYCHEM"
    ov2 = build_overview(db_session, "cosmetics", currency="TRY", source="parasut", rates_fn=lambda: rates)
    card2 = next(c for b in ov2["blocks"] for c in b["cards"] if c["item_id"] == s["cct"].id)
    assert [r["source"] for r in card2["rows"]] == ["parasut"] and card2["filtered_rows"] == 1
    assert {o["key"] for o in ov2["options"]["sources"]} >= {"parasut", "lab", "manual"}
    with pytest.raises(ValueError):
        build_overview(db_session, "cosmetics", source="bogus", rates_fn=lambda: rates)
    r = client.post("/api/login", json={"username": "dogukan", "password": "minerva123"}, headers=_H)
    assert r.status_code == 200
    assert client.get("/api/supplier-prices/overview?source=bogus").status_code == 400
    assert client.get("/api/supplier-prices/overview?source=parasut&currency=TRY").status_code == 200


# ─── Rotalar ────────────────────────────────────────────────────────────────

def _login(client, user):
    client.post("/api/logout", headers=_H)
    r = client.post("/api/login", json={"username": user, "password": "minerva123"}, headers=_H)
    assert r.status_code == 200, r.text


def test_queue_routes_auth_and_flow(client: TestClient, db_session: Session):
    s = _seed(db_session)
    _, lines = _bill_row(db_session, B_NILKIM)
    url = "/api/supplier-prices/parasut-queue"
    assert client.get(url).status_code == 401
    assert client.post(f"{url}/{lines[0].id}/approve", json={}, headers=_H).status_code == 401
    _login(client, "meltem")                                              # LabTech: görür, karar veremez
    r = client.get(url)
    assert r.status_code == 200 and r.json()["count"] == 2 and r.json()["enabled"] is False
    row = r.json()["pending"][0]
    assert row["product_name"].startswith("CAPRYL") and row["suggestion"]["id"] == s["cct"].id
    assert row["supplier"]["id"] == s["sup"].id and row["bill"]["invoice_no"] == "NIL2024000000554"
    assert "email" not in json.dumps(row) and row["candidates"]
    body = {"item_id": s["cct"].id, "supplier_id": s["sup"].id, "package_qty": 25, "price_unit": "kg"}
    assert client.post(f"{url}/{lines[0].id}/approve", json=body, headers=_H).status_code == 403
    assert client.post(f"{url}/{lines[1].id}/skip", json={}, headers=_H).status_code == 403
    _login(client, "songul")                                              # LabLead: suppliers.prices
    r = client.post(f"{url}/{lines[0].id}/approve", json=body, headers=_H)
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["net_unit_price"] == 25.0 and d["price"]["source"] == "parasut" and d["kept_newer"] is False
    assert db_session.query(SupplierPrice).count() == 1
    r = client.post(f"{url}/{lines[0].id}/approve", json=body, headers=_H)
    assert r.status_code == 409 and r.json()["code"] == "not_pending"
    r = client.post(f"{url}/{lines[1].id}/skip", json={"reason": "kargo"}, headers=_H)
    assert r.status_code == 200 and r.json()["status"] == "skipped"
    assert client.get(url).json()["count"] == 0
    assert client.post(f"{url}/999999/approve", json=body, headers=_H).status_code == 404
    # Başka panelde kart bulunamaz (domain kapsamı)
    _, l2 = _bill_row(db_session, B_NILKIM, parasut_id=4001)
    client.cookies.set("active_domain", "supplement")
    r = client.post(f"{url}/{l2[0].id}/approve", json=body, headers=_H)
    assert r.status_code == 404 and r.json()["code"] == "item_not_found"
    client.cookies.set("active_domain", "cosmetics")
    # Elle fiyat → 409 manual_exists
    db_session.add(SupplierPrice(item_id=s["cct"].id, supplier_id=s["sup"].id, supplier_name=s["sup"].name,
                                 unit_price=30.0, currency="TRY", price_unit="kg", source="manual", domain="cosmetics"))
    db_session.commit()
    r = client.post(f"{url}/{l2[0].id}/approve", json=body, headers=_H)
    assert r.status_code == 409 and r.json()["code"] == "manual_exists"


# ─── İçe aktarma betiği `el_notu` türünü kabul eder ─────────────────────────

def test_importer_accepts_el_notu_as_low_priority_lab_note(db_session: Session):
    import importlib
    lab = importlib.import_module("scripts.import_lab_price_notes_20261007")
    assert lab.SOURCE_BY_TYPE["el_notu"] == "lab_notu" and "el_notu" in lab.TYPE_TEXT
    sup = Supplier(name="UMAYCHEM", domain="cosmetics"); db_session.add(sup); db_session.flush()
    db_session.add(Item(name="NEOWAX SE-PF", sku="NW-1", category="Hammadde", unit="g", domain="cosmetics",
                        supplier_id=sup.id))
    db_session.commit()
    data = {"kaynak": "test", "kapsam": "test", "kurallar": [], "kayitlar": [
        {"id": "D-s2-1", "kaynak_sayfa": "SongulHanimFiyat2.pdf s.2", "tur": "el_notu", "tarih": "2026-10-09",
         "malzeme": "NEOWAX SE-PF", "birim_kart": None, "tedarikci": "UMAYCHEM", "fiyat": 7.2, "para_birimi": "EUR",
         "fiyat_birimi": "kg", "ambalaj_kg": 25.0, "kdv_dahil": None, "kdv_orani": None, "secili": False,
         "guven": "kesin", "not": "", "ihtiyac": None, "cizili": False}]}
    p = lab.plan(db_session, data)
    rec = p["records"][0]
    assert rec["status"] == "yazilacak" and rec["source"] == "lab_notu" and rec["type"] == "el_notu"
    with pytest.raises(ValueError, match="bilinmeyen tür"):
        bad = dict(data); bad["kayitlar"] = [dict(data["kayitlar"][0], tur="xyz")]
        lab.plan(db_session, bad)
    real = lab.load_data()
    assert sum(1 for r in real["kayitlar"] if r["tur"] == "el_notu") == 16
    assert {r["id"] for r in real["kayitlar"]} >= {"D-s2-1", "D-s8-1", "D-s12-1", "D-s13-1", "H-s8-1", "H-s9-1"}
    assert all(r["guven"] == "belirsiz" for r in real["kayitlar"] if r["id"].startswith("H-s8-") and r["tedarikci"] == "DOGASA")
    assert all(r["secili"] is False and r.get("cizili") is False for r in real["kayitlar"]
               if r["id"].startswith(("D-", "H-")))
