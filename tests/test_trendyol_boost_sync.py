"""
Trendyol satış artırma — senkron eklentileri (core.trendyol) + DB görünümleri
(core.trendyol_boost) + bildirimler.  DB'li, AĞ YOK (FakeApi).

Kilitlenenler:
  • Katalog içeriği: açıklama, özellikler (içerik + varyant), görsel sayısı,
    kategori, teslimat süresi; içerik özeti içerik değişince değişir.
  • Kategori özellikleri haftalık; listeden çıkan özellik silinir; izinli
    değerler tembel ve önbellekli.
  • Buybox: yalnız izin listeli SORGU (10'arlı); 20 sa'ten taze ürün tekrar
    sorulmaz; kayıpta ürün başına günde en fazla bir bildirim; geçici hata
    katalog akışını düşürmez, kimlik hatası düşürür.
  • Sorular: müşteri adı/id SAKLANMAZ; barkod productMainId'den; yeni cevap
    bekleyen soru bir kez bildirilir; 90 günlük tam tarama günde bir.
  • Günlük puan geçmişi katalog senkronunda yazılır.
  • IMS stoğu ve Transaction defteri değişmez.
"""
from datetime import datetime, timedelta

import pytest

from core import notifications as N
from core import trendyol as T
from core import trendyol_boost as B
from database import (Item, Transaction, TrendyolCategoryAttr, TrendyolProduct, TrendyolQuestion,
                      TrendyolScoreHistory)
from tests.trendyol_fakes import (FakeApi, buybox, category_attrs, content, inventory, paged, question,  # noqa: F401
                                  trendyol_stop_reset)

NOW = datetime(2026, 10, 13, 9, 0, 0)
BC = "8680000000011"


def _catalog_api(products=None, inv=None, extra=None):
    routes = {"products/approved/inventory-and-price": paged(inv if inv is not None else [inventory()]),
              "products/approved": paged(products if products is not None else [content()]),
              "categories/2/attributes": category_attrs()}
    routes.update(extra or {})
    return FakeApi(routes)


@pytest.fixture
def pushes(monkeypatch):
    sent = []
    monkeypatch.setattr(N, "_send_push", lambda audience, payload: sent.append((audience, payload)))
    return sent


# ─── İçerik ──────────────────────────────────────────────────────────────────

def test_catalog_content_parsed_and_hashed(db_session):
    T.sync_catalog(db_session, _catalog_api(), now=NOW)
    p = db_session.query(TrendyolProduct).one()
    assert p.description == "<p>Nemlendirici krem.</p>" and p.image_count == 3
    assert p.category_id == 2 and p.delivery_duration == 1 and p.rush_delivery is False
    d = B.product_dict(p)
    assert d["attributes"] == [{"id": 47, "name": "Hacim", "values": ["50 ml"], "value_ids": [501]}]
    first = p.content_hash
    T.sync_catalog(db_session, _catalog_api(products=[content(description="<p>Yeni açıklama.</p>")]),
                   now=NOW + timedelta(hours=1))
    db_session.refresh(p)
    assert p.content_hash and p.content_hash != first


def test_parse_attributes_merges_variant_values():
    out = T.parse_attributes(
        [{"attributeId": 47, "attributeName": "Hacim", "attributeValues": [{"attributeValueId": 501, "attributeValue": "50 ml"}]}],
        [{"attributeId": 338, "attributeName": "Cilt Tipi", "attributeValueId": 9001, "attributeValue": "Kuru Cilt"},
         {"attributeId": 47, "attributeName": "Hacim", "attributeValueId": 501, "attributeValue": "50 ml"}])
    assert out == [{"id": 47, "name": "Hacim", "values": ["50 ml"], "value_ids": [501]},
                   {"id": 338, "name": "Cilt Tipi", "values": ["Kuru Cilt"], "value_ids": [9001]}]


# ─── Kategori özellikleri ────────────────────────────────────────────────────

def test_category_attrs_weekly_and_pruned(db_session):
    api = _catalog_api()
    T.sync_catalog(db_session, api, now=NOW)
    rows = {a.attribute_id: a for a in db_session.query(TrendyolCategoryAttr).all()}
    assert set(rows) == {47, 338, 60} and rows[338].required and rows[338].slicer
    T.sync_catalog(db_session, api, now=NOW + timedelta(days=1))
    assert len(api.calls_to("categories/2/attributes")) == 1              # haftalık
    api2 = _catalog_api(extra={"categories/2/attributes": category_attrs(attrs=[(47, "Hacim", True, False, False)])})
    T.sync_catalog(db_session, api2, now=NOW + timedelta(days=8))
    assert [a.attribute_id for a in db_session.query(TrendyolCategoryAttr).all()] == [47]


def test_attribute_values_lazy_and_cached(db_session):
    T.sync_catalog(db_session, _catalog_api(), now=NOW)
    pages = {0: paged([{"attributeValueId": 9001, "attributeValueName": "Kuru Cilt"}], total_pages=2, page=0),
             1: paged([{"attributeValueId": 9002, "attributeValueName": "Yağlı Cilt"}], total_pages=2, page=1)}
    api = FakeApi({"categories/2/attributes/338/values": lambda path, params: pages[params["page"]]})
    vals = T.ensure_attribute_values(db_session, api, 2, 338, now=NOW)
    assert vals == [{"id": 9001, "name": "Kuru Cilt"}, {"id": 9002, "name": "Yağlı Cilt"}]
    assert T.ensure_attribute_values(db_session, api, 2, 338, now=NOW + timedelta(days=1)) == vals
    assert api.calls == 2                                                 # ikinci çağrı önbellekten


# ─── Buybox ──────────────────────────────────────────────────────────────────

def test_buybox_query_batches_ttl_and_alert_throttle(db_session, pushes):
    products = [content(barcode=f"86800000001{i:02d}") for i in range(12)]
    invs = [inventory(barcode=f"86800000001{i:02d}") for i in range(12)]
    bodies = []

    def bb(path, body):
        bodies.append(list(body["barcodes"]))
        return {"buyboxInfo": [buybox(barcode=b, order=2 if b.endswith("00") else 1, price=290.0,
                                      multiple=b.endswith("01")) for b in body["barcodes"]]}

    api = _catalog_api(products=products, inv=invs, extra={"buybox-information": bb})
    out = T.sync_catalog(db_session, api, now=NOW)
    assert [len(b) for b in bodies] == [10, 2] and out["buybox"]["checked"] == 12 and out["buybox"]["lost"] == 1
    lost = db_session.query(TrendyolProduct).filter(TrendyolProduct.barcode == "8680000000100").one()
    assert T.buybox_state(lost)["state"] == "lost" and lost.buybox_price == 290.0
    shared = db_session.query(TrendyolProduct).filter(TrendyolProduct.barcode == "8680000000101").one()
    assert T.buybox_state(shared)["state"] == "shared"
    assert len([p for p in pushes if p[1]["tag"] == "trendyol-buybox"]) == 1
    T.sync_catalog(db_session, api, now=NOW + timedelta(hours=2))
    assert len(bodies) == 2                                               # 20 sa'ten taze: sorulmaz
    T.sync_buybox(db_session, api, now=NOW + timedelta(hours=3), force=True)
    assert len([p for p in pushes if p[1]["tag"] == "trendyol-buybox"]) == 1   # aynı gün ikinci bildirim yok
    T.sync_buybox(db_session, api, now=NOW + timedelta(hours=25), force=True)
    assert len([p for p in pushes if p[1]["tag"] == "trendyol-buybox"]) == 2


def test_buybox_errors_isolated_auth_raises(db_session):
    out = T.sync_catalog(db_session, _catalog_api(extra={"buybox-information": T.TrendyolError("503 geçici")}), now=NOW)
    assert out["products"] == 1 and "error" in out["buybox"]
    with pytest.raises(T.TrendyolAuthError):
        T.sync_catalog(db_session, _catalog_api(extra={"buybox-information": T.TrendyolAuthError("401")}),
                       now=NOW + timedelta(days=2))


def test_catalog_writes_daily_score_history_and_leaves_ims_untouched(db_session):
    it = Item(name="Minerva Krem", category="Bitmiş Ürün", unit="adet", barcode=BC, current_stock=5.0,
              cost_price=40.0, domain="cosmetics", is_active=True)
    db_session.add(it)
    db_session.commit()
    out = T.sync_catalog(db_session, _catalog_api(), now=NOW)
    assert out["scores"]["products"] == 1
    T.sync_catalog(db_session, _catalog_api(), now=NOW + timedelta(hours=1))   # aynı gün → güncelle
    h = db_session.query(TrendyolScoreHistory).one()
    assert 0 < h.score <= 100 and h.barcode == BC
    db_session.refresh(it)
    assert it.current_stock == 5.0 and db_session.query(Transaction).count() == 0


# ─── Müşteri soruları ────────────────────────────────────────────────────────

def test_parse_question_drops_personal_data():
    q = T.parse_question(question())
    assert "customerId" not in str(q) and "Ayşe" not in str(q) and "998877" not in str(q)
    cols = {c.name for c in TrendyolQuestion.__table__.columns}
    assert not ({"customer_id", "user_name", "customer_name"} & cols)


def test_questions_sync_maps_barcode_and_notifies_once(db_session, pushes):
    T.sync_catalog(db_session, _catalog_api(), now=NOW)
    api = FakeApi({"questions/filter": paged([question(5001), question(5002, status="ANSWERED",
                                                                       answer="Evet, uygundur.")])})
    res = T.sync_questions(db_session, api, now=NOW)
    assert res["full"] is True and res["notified"] == 1
    assert len(api.calls_to("questions/filter")) == 7                      # 90 gün / 13 günlük pencere
    q = db_session.query(TrendyolQuestion).filter(TrendyolQuestion.question_id == 5001).one()
    assert q.barcode == BC and q.notified_at == NOW and q.status == T.WAITING
    a = db_session.query(TrendyolQuestion).filter(TrendyolQuestion.question_id == 5002).one()
    assert a.answer_text == "Evet, uygundur." and a.notified_at is None
    qn = [p for p in pushes if p[1]["tag"] == "trendyol-questions"]
    assert len(qn) == 1 and qn[0][1]["url"] == "/trendyol#sorular" and "yağlı cilde" in qn[0][1]["body"]
    st = T._state(db_session, "questions")                                # run() başarıda damgalar
    st.last_ok_at = NOW
    db_session.commit()
    api2 = FakeApi({"questions/filter": paged([question(5001)])})
    res2 = T.sync_questions(db_session, api2, now=NOW + timedelta(hours=1))
    assert res2["full"] is False and res2["notified"] == 0 and len(api2.calls_to("questions/filter")) == 1
    assert len([p for p in pushes if p[1]["tag"] == "trendyol-questions"]) == 1


def test_questions_answered_later_updates_row(db_session):
    T.sync_questions(db_session, FakeApi({"questions/filter": paged([question(5001)])}), now=NOW)
    T.sync_questions(db_session, FakeApi({"questions/filter": paged([question(5001, status="ANSWERED",
                                                                              answer="Uygundur.")])}),
                     now=NOW + timedelta(hours=1))
    q = db_session.query(TrendyolQuestion).one()
    assert q.status == "ANSWERED" and q.answer_text == "Uygundur."
    view = B.questions_view(db_session)
    assert view["questions"][0]["waiting_hours"] is None and view["counts"] == {"ANSWERED": 1}


def test_run_questions_stream_and_scheduler_registration(db_session, monkeypatch):
    res = T.run("questions", db=db_session, api=FakeApi({"questions/filter": paged([question()])}))
    assert res["results"]["questions"]["ok"] is True
    from core import scheduler as sch
    assert callable(sch.trendyol_questions_sync)


# ─── Bildirim metinleri ──────────────────────────────────────────────────────

def test_notification_payloads(pushes):
    N.notify_trendyol_buybox_lost([{"barcode": BC, "title": "Krem", "order": 2, "buybox_price": 290.0,
                                    "our_price": 315.0}])
    N.notify_trendyol_buybox_lost([{"barcode": "1", "title": "A"}, {"barcode": "2", "title": "B"}])
    N.notify_trendyol_buybox_lost([])
    N.notify_trendyol_questions(0, [])
    N.notify_trendyol_questions(2, ["", "Kargo ne zaman gelir?"])
    assert [p[0] for p in pushes] == ["managers"] * 3
    assert "2. sırada" in pushes[0][1]["body"] and "290.00" in pushes[0][1]["body"]
    assert pushes[1][1]["body"].startswith("2 üründe")
    assert pushes[2][1]["body"] == '2 yeni soru cevap bekliyor — "Kargo ne zaman gelir?"'
