"""Minerva Asistan — `b2b_orders` aracı (core/assistant/handlers_ops.py, 13.10.2026).

`tests/test_b2b_orders.py`'nin `world` kurulumunu kullanır (ayrı dosya: iki
modülün `world` fikstürü aynı adı taşır).  Kilitler:
  • Liste + tek sipariş (teklif no / SIP-<id>) aktif panelle süzülür.
  • Tutar/ödeme tutarı yalnız `b2b.view` (sistemdeki `_show_prices`); izinsiz
    kullanıcıya ödeme kapısı yalnız evet/hayır olarak gider.
  • Müşteri iletişimi (kişi, e-posta, adres), banka/IBAN, imza görseli ve belge
    özeti hiçbir koşulda çıkmaz.
"""
import json

from core.assistant.tools import ToolContext, run_tool
from database import B2BOrder, BankProfile, User
from tests.test_b2b_orders import _approved_order, _convert, _quote, world  # noqa: F401  (fikstür)

CONTACT = ("Test Kişi", "alici@example.com", "Teststr. 1")


def _ctx(db, username, domain="cosmetics"):
    return ToolContext.from_user(db.query(User).filter_by(username=username).one(), domain, db)


def _run(ctx, args):
    res = run_tool(ctx, "b2b_orders", {"status": None, "reference": None, **args})
    return res, json.loads(res.content)


def _never_leaks(db, content):
    for s in CONTACT:
        assert s not in content, s
    for bank in db.query(BankProfile).all():
        for iban in (bank.iban_usd, bank.iban_eur, bank.iban_try):
            if iban:
                assert iban not in content and iban.replace(" ", "") not in content
    assert "data:image/png" not in content and "document_hash" not in content and "signature" not in content


def test_b2b_orders_list_and_detail_follow_price_permission_and_hide_contacts(world):
    w, db = world, world["db"]
    signed = _approved_order(w)                                   # APPROVED, imzalı
    waiting = _convert(w, _quote(w, [(w["serum"], 4, 7.5)]))["order"]["id"]   # SUBMITTED
    ref_signed = db.get(B2BOrder, signed).quotation.quote_number

    manager = _ctx(db, "isik")                                    # b2b.view VAR
    res, p = _run(manager, {})
    assert not res.is_error and p["filter"] == "open" and p["count"] == 2
    rows = {r["reference"]: r for r in p["rows"]}
    assert rows[ref_signed]["status"] == "APPROVED" and rows[ref_signed]["total"] == 50.0
    assert rows[ref_signed]["currency"] == "USD" and rows[ref_signed]["customer"] == "Sentetik Müşteri GmbH"
    sub = next(r for r in p["rows"] if r["status"] == "SUBMITTED")
    assert sub["next_step"]["text"] == "Teknik değerlendirme"
    assert res.summary == "2 sipariş"
    _never_leaks(db, res.content)

    res, p = _run(manager, {"reference": ref_signed.lower()})      # teklif no, büyük/küçük harf
    assert not res.is_error
    assert p["order"]["reference"] == ref_signed and p["order"]["payment_terms"]
    assert p["payment_gate"]["total"] == 50.0 and p["payment_gate"]["production_ok"] is False
    assert p["lines"] == [{"name": "Sentetik Krem 50 ml", "qty": 10.0, "unit": "adet", "use_stock": 0.0,
                           "produce": 10.0, "recipe": "Sentetik Krem Reçetesi"}]
    assert p["link"] == "/b2b-siparisler" and res.summary == f"{ref_signed} · Onaylandı"
    assert {"Teknik değerlendirme", "Yönetim onayı (imza)"} <= {t["action"] for t in p["timeline"]}
    assert all(set(t) == {"at", "action"} for t in p["timeline"])   # işlemi yapan kişi adı yok
    _never_leaks(db, res.content)

    res, p = _run(manager, {"reference": f"SIP-{waiting}"})
    assert not res.is_error and p["order"]["status"] == "SUBMITTED" and p["batches"] == []

    labtech = _ctx(db, "meltem")                                  # b2b_orders.view, b2b.view YOK
    res, p = _run(labtech, {"status": "all"})
    assert not res.is_error and p["count"] == 2
    assert all("total" not in r and "currency" not in r for r in p["rows"])
    res, p = _run(labtech, {"reference": ref_signed})
    assert not res.is_error and "payment_terms" not in p["order"] and "total" not in p["order"]
    assert p["payment_gate"] == {"production_ok": False, "shipment_ok": False}
    assert "50.0" not in res.content and "unit_price" not in res.content
    _never_leaks(db, res.content)

    res, p = _run(manager, {"status": "SHIPPED"})
    assert not res.is_error and p["count"] == 0 and p["rows"] == []
    res, p = _run(manager, {"status": "bogus"})
    assert res.is_error


def test_b2b_orders_are_panel_scoped_and_need_b2b_orders_view(world):
    w, db = world, world["db"]
    oid = _approved_order(w)
    ref = db.get(B2BOrder, oid).quotation.quote_number
    other_panel = _ctx(db, "isik", domain="supplement")
    res, p = _run(other_panel, {})
    assert not res.is_error and p["count"] == 0
    res, p = _run(other_panel, {"reference": ref})
    assert res.is_error and "bulunamadı" in p["error"]
    res, p = _run(other_panel, {"reference": f"SIP-{oid}"})
    assert res.is_error

    staff = User(username="ops_b2b_staff", full_name="Staff Test", role="Staff", is_active=True,
                 password_hash="x")
    db.add(staff)
    db.commit()
    res, p = _run(ToolContext.from_user(staff, "cosmetics", db), {})
    assert res.is_error and "b2b_orders.view" in res.content
