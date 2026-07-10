"""
CRM ↔ B2B teklif köprüsü testleri.

  • fırsat detayında bağlı teklif özeti (maliyet/marj alanları ASLA yok)
  • /quotations-lookup: crm.edit ister; cross-domain (Kozmetik + Supplement)
  • /deals/from-quotation: firma/kişi eşle-veya-oluştur, değer = teklif toplamı,
    aynı teklife ikinci fırsat → 409 + deal_id, RBAC
"""
from fastapi.testclient import TestClient

from database import Quotation

_H = {"Origin": "http://testserver"}


def _quotation(db, number="Q-2026-001", **extra):
    q = Quotation(quote_number=number, customer_name=extra.pop("customer_name", "ACME Ltd."),
                  currency=extra.pop("currency", "USD"),
                  total_amount=extra.pop("total_amount", 1500.0), **extra)
    db.add(q); db.commit(); db.refresh(q)
    return q


def test_deal_detail_includes_quotation(authed_client: TestClient, db_session):
    q = _quotation(db_session, customer_email="buy@acme.com")
    d = authed_client.post("/api/crm/deals",
                           json={"title": "Köprülü Fırsat", "quotation_id": q.id}, headers=_H).json()
    detail = authed_client.get(f"/api/crm/deals/{d['id']}").json()
    qq = detail["quotation"]
    assert qq and qq["quote_number"] == "Q-2026-001" and qq["total_amount"] == 1500.0
    assert qq["status"] == "DRAFT" and qq["currency"] == "USD"
    # maliyet/marj alanları serileştirilmez (finance hassasiyeti)
    for forbidden in ("unit_cost_try", "exchange_rate", "subtotal_amount", "tax_amount"):
        assert forbidden not in qq


def test_quotations_lookup_requires_crm_edit(labtech_client: TestClient):
    assert labtech_client.get("/api/crm/quotations-lookup").status_code == 403


def test_quotations_lookup_cross_domain(authed_client: TestClient, db_session):
    _quotation(db_session, number="Q-KOZ-1", domain="cosmetics")
    _quotation(db_session, number="Q-SUP-1", domain="supplement")
    rows = authed_client.get("/api/crm/quotations-lookup").json()
    numbers = [r["quote_number"] for r in rows]
    # CRM cross-cutting: iki domain'in teklifleri de listelenir (bilinçli karar)
    assert "Q-KOZ-1" in numbers and "Q-SUP-1" in numbers
    assert all("label" in r for r in rows)


def test_from_quotation_creates_company_contact_deal(authed_client: TestClient, db_session):
    # Katlanmış ad eşleşmesi: 'acme ltd' mevcut 'ACME Ltd.' firmasına bağlanmalı
    co = authed_client.post("/api/crm/companies", json={"name": "ACME Ltd."}, headers=_H).json()
    q = _quotation(db_session, number="Q-2026-777", customer_name="acme ltd",
                   customer_contact="John Buyer", customer_email="john@acme.com",
                   total_amount=2500.0, currency="EUR")
    r = authed_client.post(f"/api/crm/deals/from-quotation/{q.id}", headers=_H)
    assert r.status_code == 201, r.text
    d = r.json()
    assert d["company_id"] == co["id"]                      # yeni firma AÇILMADI
    assert d["value"] == 2500.0 and d["currency"] == "EUR"
    assert d["quotation_id"] == q.id and "Q-2026-777" in d["title"]
    # kişi customer_contact'tan oluşturuldu ve firmaya bağlı
    cts = authed_client.get("/api/crm/contacts?q=John").json()
    assert cts and cts[0]["company_id"] == co["id"]
    # firma sayısı hâlâ 1 (mükerrer yok)
    cos = [c for c in authed_client.get("/api/crm/companies").json() if "acme" in c["name"].lower()]
    assert len(cos) == 1


def test_from_quotation_conflict_409(authed_client: TestClient, db_session):
    q = _quotation(db_session, number="Q-2026-888")
    first = authed_client.post(f"/api/crm/deals/from-quotation/{q.id}", headers=_H)
    assert first.status_code == 201
    second = authed_client.post(f"/api/crm/deals/from-quotation/{q.id}", headers=_H)
    assert second.status_code == 409
    assert second.json()["deal_id"] == first.json()["id"]


def test_from_quotation_missing_404(authed_client: TestClient):
    assert authed_client.post("/api/crm/deals/from-quotation/999999", headers=_H).status_code == 404


def test_from_quotation_requires_crm_create(labtech_client: TestClient):
    assert labtech_client.post("/api/crm/deals/from-quotation/1", headers=_H).status_code == 403
