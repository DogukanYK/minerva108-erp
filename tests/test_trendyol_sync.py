"""
Trendyol senkron motoru (core.trendyol) — DB'li, AĞ YOK (FakeApi).

Kilitlenenler:
  • Parser beyaz listesi: kişisel veri (ad/adres/telefon/vergi no) ALINMAZ;
    satır toplamları discountDetails'ten (Trendyol "Örnek 6": 2 × 350, birim
    satıcı indirimi 35 → net 630), yoksa birim × adet; eski alan adları yedek.
  • Sipariş upsert'i idempotent; eski lastModified yeniyi ezmez; satırlar
    yerinde güncellenir.
  • Stream: cursor izlenir, aynı imleç tekrar ederse durur; pencereler ≤13
    gün, ilk çalıştırma 89 gün; watermark yalnız TAMAMLANAN pencereyle ve
    yalnız ileri; ikinci çalıştırma watermark − 6 sa'ten başlar.
  • Finans: tür başına istek; en yeni pencerede 400 = tür kabul edilmedi
    (diğerleri sürer), hepsi reddedilirse HATA; PlatformServiceFee etiketi
    kalıcı; kargo faturası kalemleri bir kez çekilir, 400'lü seri atlanır.
  • Katalog: onaylı ürün + stok/fiyat birleşir; görülmeyen satır silinmez.
  • run(): süreç içi kilit; kimlik hatası kalan akışları durdurur; durum
    kaydı + bildirim eşikleri (orders 3, finans 1, kimlik 1; 24 sa'te bir).
  • Zamanlayıcı işi: yapılandırma yoksa / kapalıysa çalışmaz.
  • IMS stoğu ve Transaction defteri HİÇ değişmez.
"""
from datetime import datetime, timedelta

import pytest

from core import trendyol as T
from database import (Item, Transaction, TrendyolCargoItem, TrendyolFinRow, TrendyolLine,
                      TrendyolPackage, TrendyolProduct, TrendyolSyncState)
from tests.trendyol_fakes import (FakeApi, approved, cargo_item, deduction, inventory, line, package,  # noqa: F401
                                  paged, settlement, stream, trendyol_stop_reset)

NOW = datetime(2026, 10, 13, 9, 0, 0)


def _ms_window(params):
    return params["lastModifiedEndDate"] - params["lastModifiedStartDate"]


# ─── Parser ──────────────────────────────────────────────────────────────────

def test_parse_package_whitelists_and_totals():
    pk = T.parse_package(package())
    flat = repr(pk)
    for secret in ("Ayşe", "Yılmaz", "ayse@example.com", "12345678901", "11111111111", "Gizli", "0555"):
        assert secret not in flat
    assert pk["package_id"] == 3330000001 and pk["order_number"] == "1065440001"
    assert pk["status"] == "Delivered" and pk["total_price"] == 630.0
    ln = pk["lines"][0]
    assert ln["quantity"] == 2 and ln["unit_gross"] == 350.0 and ln["unit_price"] == 315.0
    assert ln["gross_total"] == 700.0 and ln["seller_discount_total"] == 70.0 and ln["net_total"] == 630.0
    assert ln["commission_rate"] == 15.0 and ln["vat_rate"] == 20.0


def test_parse_line_without_discount_details_uses_unit_times_qty():
    ln = T.parse_line(line(qty=3, gross=100.0, seller=10.0, ty=5.0, details=False))
    assert ln["gross_total"] == 300.0 and ln["seller_discount_total"] == 30.0
    assert ln["ty_discount_total"] == 15.0 and ln["net_total"] == 255.0


def test_parse_old_field_names_fallback():
    raw = {"orderNumber": "9", "id": 77, "status": "Created", "totalPrice": 90.0, "grossAmount": 100.0,
           "lines": [{"id": 5, "merchantSku": "OLD", "price": 45.0, "quantity": 2, "vatBaseAmount": 20,
                      "barcode": "B1", "productCode": 31}]}
    pk = T.parse_package(raw)
    assert pk["package_id"] == 77 and pk["total_price"] == 90.0 and pk["gross_amount"] == 100.0
    ln = pk["lines"][0]
    assert ln["line_id"] == 5 and ln["stock_code"] == "OLD" and ln["unit_price"] == 45.0
    assert ln["net_total"] == 90.0 and ln["vat_rate"] == 20.0 and ln["content_id"] == 31


def test_parse_rejects_packages_without_identity():
    assert T.parse_package({"orderNumber": "1"}) is None
    assert T.parse_package({"shipmentPackageId": 1}) is None


def test_personal_columns_do_not_exist():
    cols = {c.name for c in TrendyolPackage.__table__.columns} | {c.name for c in TrendyolLine.__table__.columns}
    for bad in ("customer", "address", "email", "phone", "tax", "identity", "name_surname"):
        assert not any(bad in c for c in cols), bad


# ─── Sipariş upsert + stream ─────────────────────────────────────────────────

def _orders_api(pages_by_call):
    """Her stream çağrısında sıradaki sayfayı döner (bitti ise boş)."""
    state = {"i": 0}

    def handler(path, params):
        i = state["i"]
        state["i"] += 1
        return pages_by_call[i] if i < len(pages_by_call) else stream([])
    return FakeApi({"orders/stream": handler})


def test_upsert_idempotent_and_stale_ignored(db_session):
    T.upsert_package(db_session, T.parse_package(package()))
    T.upsert_package(db_session, T.parse_package(package()))
    db_session.commit()
    assert db_session.query(TrendyolPackage).count() == 1
    assert db_session.query(TrendyolLine).count() == 1
    old = package(status="Shipped", lm=1762865408581 - 60_000)
    assert T.upsert_package(db_session, T.parse_package(old)) == "stale"
    db_session.commit()
    assert db_session.query(TrendyolPackage).one().status == "Delivered"


def test_lines_updated_in_place(db_session):
    T.upsert_package(db_session, T.parse_package(package(lines=[line(1), line(2, barcode="B2")])))
    db_session.commit()
    newer = package(lm=1762865408581 + 1000, status="Returned",
                    lines=[line(2, barcode="B2", qty=1), line(3, barcode="B3")])
    assert T.upsert_package(db_session, T.parse_package(newer)) == "updated"
    db_session.commit()
    rows = {ln.line_id: ln for ln in db_session.query(TrendyolLine).all()}
    assert set(rows) == {2, 3} and rows[2].quantity == 1
    assert db_session.query(TrendyolPackage).one().status == "Returned"


def test_stream_follows_cursor_and_keeps_filters(db_session):
    api = _orders_api([stream([package(1)], has_more=True, cursor="c1"), stream([package(2, order="2")])])
    res = T.sync_orders(db_session, api, now=NOW, lookback_days=5)
    calls = api.calls_to("orders/stream")
    assert res["new"] == 2 and len(calls) == 2
    first, second = calls[0][2], calls[1][2]
    assert "nextCursor" not in first and second["nextCursor"] == "c1"
    for k in ("lastModifiedStartDate", "lastModifiedEndDate", "size", "supplierId"):
        assert first[k] == second[k]                      # cursor ortasında süzgeç değişmez
    assert db_session.query(TrendyolPackage).count() == 2


def test_stream_repeated_cursor_stops(db_session):
    api = FakeApi({"orders/stream": stream([package()], has_more=True, cursor="same")})
    with pytest.raises(T.TrendyolError, match="imleci"):
        T.sync_orders(db_session, api, now=NOW, lookback_days=2)


def test_first_run_covers_89_days_in_13_day_windows(db_session):
    api = FakeApi({"orders/stream": stream([])})
    res = T.sync_orders(db_session, api, now=NOW)
    calls = api.calls_to("orders/stream")
    assert res["windows"] == len(calls) == 7             # ceil(89 / 13)
    assert all(_ms_window(c[2]) <= 13 * 86400_000 for c in calls)
    assert calls[0][2]["lastModifiedStartDate"] == T.to_ty_ms(NOW - timedelta(days=89))
    assert calls[-1][2]["lastModifiedEndDate"] == T.to_ty_ms(NOW)
    st = db_session.query(TrendyolSyncState).filter_by(stream="orders").one()
    assert st.watermark == NOW


def test_second_run_starts_from_watermark_minus_overlap(db_session):
    T.sync_orders(db_session, FakeApi({"orders/stream": stream([])}), now=NOW)
    api = FakeApi({"orders/stream": stream([])})
    later = NOW + timedelta(minutes=30)
    T.sync_orders(db_session, api, now=later)
    (c,) = api.calls_to("orders/stream")
    assert c[2]["lastModifiedStartDate"] == T.to_ty_ms(NOW - timedelta(hours=6))


def test_watermark_only_advances_after_complete_window(db_session):
    state = {"i": 0}

    def handler(path, params):
        state["i"] += 1
        if state["i"] == 2:
            raise T.TrendyolError("ağ koptu")
        return stream([package(state["i"], order=str(state["i"]))])

    with pytest.raises(T.TrendyolError):
        T.sync_orders(db_session, FakeApi({"orders/stream": handler}), now=NOW, lookback_days=20)
    db_session.rollback()
    st = db_session.query(TrendyolSyncState).filter_by(stream="orders").one()
    assert st.watermark == NOW - timedelta(days=20) + timedelta(days=13)
    assert db_session.query(TrendyolPackage).count() == 1     # ilk pencere kalıcı


def test_backfill_never_moves_watermark_back(db_session):
    T.sync_orders(db_session, FakeApi({"orders/stream": stream([])}), now=NOW)
    T.sync_orders(db_session, FakeApi({"orders/stream": stream([])}), now=NOW - timedelta(days=1),
                  lookback_days=30)
    st = db_session.query(TrendyolSyncState).filter_by(stream="orders").one()
    assert st.watermark == NOW


# ─── Finans ──────────────────────────────────────────────────────────────────

def _finance_api(reject=(), older_reject=(), deduction_rows=None, fee_rows=None, cargo=None):
    deduction_rows = deduction_rows if deduction_rows is not None else []

    def fin(path, params):
        t = params["transactionType"]
        newest = params["endDate"] == T.to_ty_ms(NOW)
        if t in reject and newest:
            raise T.TrendyolBadRequest("Trendyol isteği kabul etmedi (400): type")
        if t in older_reject and not newest:
            raise T.TrendyolBadRequest("Trendyol isteği kabul etmedi (400): date")
        if not newest:
            return paged([])
        if "settlements" in path and t == "Sale":
            return paged([settlement()])
        if t == "DeductionInvoices":
            if params.get("transactionSubType") == T.PLATFORM_FEE:
                return paged(fee_rows or [])
            return paged(deduction_rows)
        return paged([])

    routes = {"settlements": fin, "otherfinancials": fin}
    if cargo is not None:
        routes["cargo-invoice"] = cargo
    return FakeApi(routes)


def test_finance_one_request_per_type_and_window(db_session):
    api = _finance_api()
    res = T.sync_finance(db_session, api, now=NOW, days=28)
    s_calls = api.calls_to("/settlements")
    o_calls = [c for c in api.calls_to("/otherfinancials") if "transactionSubType" not in c[2]]
    assert len(s_calls) == len(T.SETTLEMENT_TYPES) * 2 and len(o_calls) == len(T.OTHER_TYPES) * 2
    assert all(c[2]["endDate"] - c[2]["startDate"] <= 15 * 86400_000 for c in s_calls)
    assert all(c[2]["size"] == 1000 and "," not in c[2]["transactionType"] for c in s_calls)
    assert res["new"] == 1 and res["windows"] == 2
    row = db_session.query(TrendyolFinRow).one()
    assert row.type_param == "Sale" and row.type_label == "Satış" and row.commission_amount == 67.4985
    assert row.package_id == 3330000001 and row.order_number == "1065440001"


def test_finance_rejected_type_recorded_others_continue(db_session):
    res = T.sync_finance(db_session, _finance_api(reject=("PayByLink",)), now=NOW, days=10)
    assert "settlement:PayByLink" in res["rejected"]
    assert db_session.query(TrendyolFinRow).count() == 1


def test_finance_all_types_rejected_is_error(db_session):
    everything = T.SETTLEMENT_TYPES + T.OTHER_TYPES
    with pytest.raises(T.TrendyolError, match="hiçbir"):
        T.sync_finance(db_session, _finance_api(reject=everything), now=NOW, days=10)


def test_finance_history_limit_stops_only_that_type(db_session):
    api = _finance_api(older_reject=("Sale",))
    res = T.sync_finance(db_session, api, now=NOW, days=40)
    sale = [c for c in api.calls_to("/settlements") if c[2]["transactionType"] == "Sale"]
    ret = [c for c in api.calls_to("/settlements") if c[2]["transactionType"] == "Return"]
    assert len(sale) == 2 and len(ret) == 3              # Sale 2. pencerede durdu
    assert res["rejected"] == {}


def test_platform_fee_tag_sticks(db_session):
    fee = deduction("PSF-1", label="Platform Hizmet Bedeli", debt=10.0)
    api = _finance_api(deduction_rows=[fee], fee_rows=[fee])
    T.sync_finance(db_session, api, now=NOW, days=10)
    T.sync_finance(db_session, _finance_api(deduction_rows=[fee], fee_rows=[]), now=NOW, days=10)
    row = db_session.query(TrendyolFinRow).filter_by(row_id="PSF-1").one()
    assert row.sub_type == T.PLATFORM_FEE and row.type_param == "DeductionInvoices"


def test_cargo_items_fetched_once(db_session):
    def cargo(path, params):
        assert "TYC2026000001" in path
        return paged([cargo_item(), cargo_item(order="2", kind="İade Kargo Bedeli", amount=20.0)])

    api = _finance_api(deduction_rows=[deduction()], cargo=cargo)
    res = T.sync_finance(db_session, api, now=NOW, days=10)
    assert res["cargo"]["invoices"] == 1 and res["cargo"]["items"] == 2
    items = db_session.query(TrendyolCargoItem).order_by(TrendyolCargoItem.seq).all()
    assert [i.is_return for i in items] == [False, True]
    assert items[0].parcel_unique_id == "7260001151141191" and items[0].amount == 34.24
    api2 = _finance_api(deduction_rows=[deduction()], cargo=cargo)
    T.sync_finance(db_session, api2, now=NOW, days=10)
    assert api2.calls_to("cargo-invoice") == []
    assert db_session.query(TrendyolCargoItem).count() == 2


def test_cargo_invoice_400_marked_and_skipped(db_session):
    api = _finance_api(deduction_rows=[deduction()],
                       cargo=T.TrendyolBadRequest("Trendyol isteği kabul etmedi (400): yok", status=400))
    res = T.sync_finance(db_session, api, now=NOW, days=10)
    assert "TYC2026000001" in res["cargo"]["errors"]
    row = db_session.query(TrendyolFinRow).filter_by(row_id="TYC2026000001").one()
    assert row.detail_fetched_at is not None


def test_non_cargo_deduction_not_fetched(db_session):
    api = _finance_api(deduction_rows=[deduction("REK-1", label="Reklam Faturası")])
    T.sync_finance(db_session, api, now=NOW, days=10)
    assert api.calls_to("cargo-invoice") == []


# ─── Katalog ─────────────────────────────────────────────────────────────────

def test_catalog_merges_approved_and_inventory(db_session):
    api = FakeApi({"products/approved/inventory-and-price": paged([inventory(qty=12)]),
                   "products/approved": paged([approved(), approved(barcode="B2", title="Tonik", on_sale=False)])})
    res = T.sync_catalog(db_session, api, now=NOW)
    assert res["products"] == 2 and res["new"] == 2 and res["with_stock"] == 1
    p = db_session.query(TrendyolProduct).filter_by(barcode="8680000000011").one()
    assert p.quantity == 12 and p.sale_price == 315.0 and p.list_price == 350.0
    assert p.brand == "Minerva 108" and p.vat_rate == 20 and p.on_sale is True
    assert db_session.query(TrendyolProduct).filter_by(barcode="B2").one().on_sale is False
    later = NOW + timedelta(hours=1)
    res2 = T.sync_catalog(db_session, FakeApi({"products/approved": paged([approved()])}), now=later)
    assert res2["missing"] == 1
    gone = db_session.query(TrendyolProduct).filter_by(barcode="B2").one()
    assert gone.last_seen_at == NOW                      # silinmedi, eski kaldı


def test_catalog_pagination_stops_at_total_pages(db_session):
    def handler(path, params):
        return {"page": params["page"], "totalPages": 2, "content": [approved(barcode=f"P{params['page']}")]}
    api = FakeApi({"products/approved": handler, "products/approved/inventory-and-price": paged([])})
    T.sync_catalog(db_session, api, now=NOW)
    assert len([c for c in api.log if c[1].endswith("/products/approved")]) == 2
    assert db_session.query(TrendyolProduct).count() == 2


# ─── run() + durum + bildirim ────────────────────────────────────────────────

def _full_api():
    return FakeApi({"orders/stream": stream([package()]),
                    "settlements": paged([]), "otherfinancials": paged([]),
                    "products/approved/inventory-and-price": paged([inventory()]),
                    "products/approved": paged([approved()])})


def test_run_all_marks_states_and_leaves_ims_untouched(db_session):
    it = Item(name="Minerva Krem", category="Bitmiş Ürün", unit="adet", barcode="8680000000011",
              current_stock=5.0, cost_price=40.0, domain="cosmetics", is_active=True)
    db_session.add(it)
    db_session.commit()
    res = T.run("all", db=db_session, api=_full_api())
    assert all(r["ok"] for r in res["results"].values())
    states = {s.stream: s for s in db_session.query(TrendyolSyncState).all()}
    assert set(states) == {"orders", "finance", "catalog"}
    assert all(s.last_ok_at and s.consecutive_failures == 0 for s in states.values())
    db_session.refresh(it)
    assert it.current_stock == 5.0 and db_session.query(Transaction).count() == 0


def test_run_lock_skips_concurrent(db_session):
    assert T._RUN_LOCK.acquire(blocking=False)
    try:
        assert T.run("orders", db=db_session, api=_full_api()) ["skipped"] == "running"
    finally:
        T._RUN_LOCK.release()
    assert not T.is_running()


def test_auth_error_stops_remaining_streams(db_session):
    api = FakeApi({"orders/stream": T.TrendyolAuthError("401"), "products/approved": paged([])})
    res = T.run("all", db=db_session, api=api)
    assert set(res["results"]) == {"orders"} and res["results"]["orders"]["auth"] is True
    assert api.calls_to("products") == []
    st = db_session.query(TrendyolSyncState).filter_by(stream="orders").one()
    assert st.consecutive_failures == 1 and st.last_error


def test_run_without_config_reports_error(db_session, monkeypatch):
    for n in ("TRENDYOL_SELLER_ID", "TRENDYOL_API_KEY", "TRENDYOL_API_SECRET"):
        monkeypatch.delenv(n, raising=False)
    res = T.run("orders", db=db_session)
    assert "yapılandırılmamış" in res["error"] and not T.is_running()


def test_alert_thresholds_and_throttle(db_session):
    bad = FakeApi({"orders/stream": T.TrendyolError("ağ"), "settlements": T.TrendyolError("ağ"),
                   "otherfinancials": T.TrendyolError("ağ")})
    for i in range(2):
        res = T.run("orders", db=db_session, api=bad)
        assert T.alerts_due(db_session, res["results"], now=NOW) == []      # orders: 3. hatada
    res = T.run("orders", db=db_session, api=bad)
    assert [s for s, _ in T.alerts_due(db_session, res["results"], now=NOW)] == ["orders"]
    res = T.run("orders", db=db_session, api=bad)
    assert T.alerts_due(db_session, res["results"], now=NOW + timedelta(hours=1)) == []    # 24 sa
    assert T.alerts_due(db_session, res["results"], now=NOW + timedelta(hours=25)) != []
    res = T.run("finance", db=db_session, api=bad)
    assert [s for s, _ in T.alerts_due(db_session, res["results"], now=NOW)] == ["finance"]   # ilk hatada
    T.run("orders", db=db_session, api=_full_api())
    st = db_session.query(TrendyolSyncState).filter_by(stream="orders").one()
    assert st.consecutive_failures == 0 and st.last_notified_at is None


def test_auth_alert_on_first_failure(db_session):
    res = T.run("orders", db=db_session, api=FakeApi({"orders/stream": T.TrendyolAuthError("403")}))
    assert [s for s, _ in T.alerts_due(db_session, res["results"], now=NOW)] == ["orders"]


# ─── Zamanlayıcı işi ─────────────────────────────────────────────────────────

def _env(monkeypatch):
    monkeypatch.setenv("TRENDYOL_SELLER_ID", "123456")
    monkeypatch.setenv("TRENDYOL_API_KEY", "k")
    monkeypatch.setenv("TRENDYOL_API_SECRET", "s")


def test_scheduler_job_noop_without_config(db_session, monkeypatch):
    from core import scheduler as sch
    for n in ("TRENDYOL_SELLER_ID", "TRENDYOL_API_KEY", "TRENDYOL_API_SECRET"):
        monkeypatch.delenv(n, raising=False)
    called = []
    monkeypatch.setattr(T, "make_api", lambda: called.append(1) or _full_api())
    monkeypatch.setattr(sch, "SessionLocal", lambda: db_session)
    sch.trendyol_orders_sync()
    assert called == []


def test_scheduler_job_respects_toggle_and_runs(db_session, monkeypatch):
    from core import scheduler as sch
    _env(monkeypatch)
    api = _full_api()
    monkeypatch.setattr(T, "make_api", lambda: api)
    monkeypatch.setattr(sch, "SessionLocal", lambda: db_session)
    T.set_enabled(db_session, False)
    sch.trendyol_orders_sync()
    assert api.calls == 0
    T.set_enabled(db_session, True)
    sch.trendyol_orders_sync()
    assert api.calls_to("orders/stream") and db_session.query(TrendyolPackage).count() == 1


def test_scheduler_job_notifies_on_threshold(db_session, monkeypatch):
    from core import scheduler as sch
    from core import notifications as N
    _env(monkeypatch)
    sent = []
    monkeypatch.setattr(N, "notify_trendyol_sync_failure", lambda s, d: sent.append((s, d)))
    monkeypatch.setattr(T, "make_api", lambda: FakeApi({"settlements": T.TrendyolAuthError("401 anahtar")}))
    monkeypatch.setattr(sch, "SessionLocal", lambda: db_session)
    sch.trendyol_finance_sync()
    assert sent and sent[0][0] == "finance"


# ─── İnceleme bulguları (boşluk, yalıtım, çakışma, biçim, bayat stok, bölünme, kapanış) ──

def test_short_backfill_never_skips_gap_behind_watermark(db_session):
    db_session.add(TrendyolSyncState(stream="orders", watermark=NOW - timedelta(days=20), consecutive_failures=0))
    db_session.commit()
    api = FakeApi({"orders/stream": stream([])})
    T.sync_orders(db_session, api, now=NOW, lookback_days=7)
    first = api.calls_to("orders/stream")[0][2]
    assert first["lastModifiedStartDate"] == T.to_ty_ms(NOW - timedelta(days=20) - timedelta(hours=6))


def test_finance_covers_gap_since_last_success(db_session):
    db_session.add(TrendyolSyncState(stream="finance", last_ok_at=NOW - timedelta(days=60), consecutive_failures=0))
    db_session.commit()
    api = _finance_api()
    res = T.sync_finance(db_session, api, now=NOW)                      # zamanlayıcı (gün yok)
    assert res["days"] == 60 + T.FIN_REPULL_DAYS
    res = T.sync_finance(db_session, _finance_api(), now=NOW, days=7)   # kısa elle istek de boşluğu kapsar
    assert res["days"] == 60 + T.FIN_REPULL_DAYS


def test_cargo_transient_error_isolated_per_invoice(db_session):
    def cargo(path, params):
        if "BAD" in path:
            raise T.TrendyolError("Trendyol sunucu hatası (503)")
        return paged([cargo_item()])

    rows = [deduction("TYC-OLD"), deduction("TYC-BAD")]
    res = T.sync_finance(db_session, _finance_api(deduction_rows=rows, cargo=cargo), now=NOW, days=10)
    assert res["cargo"]["invoices"] == 1 and "TYC-BAD" in res["cargo"]["errors"]
    bad = db_session.query(TrendyolFinRow).filter_by(row_id="TYC-BAD").one()
    assert bad.detail_fetched_at is None                                 # geçici — yeniden denenecek
    assert db_session.query(TrendyolFinRow).filter_by(row_id="TYC-OLD").one().detail_fetched_at is not None


def test_cargo_auth_error_still_fails_stream(db_session):
    api = _finance_api(deduction_rows=[deduction()], cargo=T.TrendyolAuthError("401"))
    with pytest.raises(T.TrendyolAuthError):
        T.sync_finance(db_session, api, now=NOW, days=10)


def test_fin_row_conflict_keeps_first_record(db_session):
    first = T.parse_fin_row(settlement(credit=449.99, comm=67.4985), "settlement", "Sale")
    second = T.parse_fin_row(settlement(credit=0.0, comm=12.0), "settlement", "CommissionPositive")
    assert T.upsert_fin_row(db_session, first) == "new"
    assert T.upsert_fin_row(db_session, second) == "conflict"
    db_session.commit()
    row = db_session.query(TrendyolFinRow).one()
    assert row.type_param == "Sale" and row.credit == 449.99 and row.commission_amount == 67.4985


def test_response_without_content_is_an_error_not_an_empty_window(db_session):
    api = FakeApi({"orders/stream": {"unexpected": True}})
    with pytest.raises(T.TrendyolError, match="biçimi"):
        T.sync_orders(db_session, api, now=NOW, lookback_days=2)
    assert db_session.query(TrendyolSyncState).filter_by(stream="orders").one().watermark is None


def test_catalog_quantity_cleared_when_inventory_missing(db_session):
    T.sync_catalog(db_session, FakeApi({"products/approved/inventory-and-price": paged([inventory(qty=12)]),
                                        "products/approved": paged([approved()])}), now=NOW)
    T.sync_catalog(db_session, FakeApi({"products/approved/inventory-and-price": paged([]),
                                        "products/approved": paged([approved()])}), now=NOW + timedelta(hours=1))
    assert db_session.query(TrendyolProduct).one().quantity is None


def test_unpacked_original_excluded_from_lists(db_session):
    T.upsert_package(db_session, T.parse_package(package(1, order="SPLIT", status="UnPacked", lines=[line(1, barcode="X1")])))
    T.upsert_package(db_session, T.parse_package(package(2, order="SPLIT", status="Created", lines=[line(2, barcode="X1")])))
    db_session.commit()
    view = T.orders_view(db_session)
    assert view["total"] == 1 and view["orders"][0]["package_id"] == 2
    assert view["status_counts"] == {"UnPacked": 1, "Created": 1}
    assert T.orders_view(db_session, status="UnPacked")["total"] == 1
    nic = {r["barcode"]: r for r in T.catalog_view(db_session)["not_in_catalog"]}
    assert nic["X1"]["lines"] == 1


def test_stop_during_run_is_not_a_failure(db_session):
    def handler(path, params):
        T.request_stop()                                 # kapanış ilk sayfa sırasında geldi
        return stream([package()])

    res = T.run("all", db=db_session, api=FakeApi({"orders/stream": handler}))
    assert res["results"]["orders"]["stopped"] is True and set(res["results"]) == {"orders"}
    st = db_session.query(TrendyolSyncState).filter_by(stream="orders").one()
    assert st.consecutive_failures == 0 and st.last_error is None
    assert T.alerts_due(db_session, res["results"], now=NOW) == []
    assert not T.is_running()
