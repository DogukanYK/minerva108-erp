"""
Döviz kurları — core/fx.py (TCMB önbellek + yedek) ve onun üstündeki
`GET /api/currency-rates` sarmalayıcısı.

Testler AĞA ÇIKMAZ: fetcher enjekte edilir; ayrıca `urllib.request.urlopen`
her testte patlayan bir sahteyle değiştirilir (yanlışlıkla gerçek TCMB'ye
gidilirse test kırılsın, sessizce ağa çıkmasın).

  • today_rates   — canlı / 1 saat önbellek / bayat önbellek / sabit yedek
  • convert       — aynı birim, TRY↔döviz, çapraz kur, None, takma ad, bilinmeyen
  • _fetch_tcmb_rates — örnek XML'i (bülten tarihiyle) doğru okur, hata → None
  • /api/currency-rates — yanıt şekli taşımadan önceki ile birebir
"""
import datetime as dt
import io
import urllib.request

import pytest
from fastapi.testclient import TestClient

from core import fx


@pytest.fixture(autouse=True)
def _offline_fx(monkeypatch):
    """Her test temiz önbellekle başlar ve ağ kapalıdır."""
    def _no_network(*a, **k):
        raise AssertionError("test ağa çıkmaya çalıştı")
    monkeypatch.setattr(urllib.request, "urlopen", _no_network)
    fx.reset_cache()
    yield
    fx.reset_cache()


class _Fetcher:
    """Çağrı sayan sahte TCMB."""
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


_LIVE = {"USD": 41.25, "EUR": 48.10, "as_of": dt.date(2026, 10, 3)}


# ─── today_rates ────────────────────────────────────────────────────────────

def test_today_rates_live_fetch():
    f = _Fetcher(dict(_LIVE))
    r = fx.today_rates(fetcher=f)
    assert f.calls == 1
    assert r.source == "TCMB" and r.stale is False and r.warning is None
    assert r.try_per == {"USD": 41.25, "EUR": 48.10, "TRY": 1.0}
    assert r.as_of == dt.date(2026, 10, 3)
    assert isinstance(r.fetched_at, dt.datetime)


def test_today_rates_cached_for_one_hour():
    f = _Fetcher(dict(_LIVE))
    fx.today_rates(fetcher=f)
    fx.today_rates(fetcher=f)
    assert f.calls == 1                       # ikinci çağrı önbellekten
    # TTL dolunca yeniden çeker
    fx._rate_cache["fetched_at"] -= dt.timedelta(seconds=fx._RATE_TTL_SECONDS + 1)
    f.result = {"USD": 42.0, "EUR": 49.0}
    r = fx.today_rates(fetcher=f)
    assert f.calls == 2 and r.try_per["USD"] == 42.0
    assert r.as_of is None                    # fetcher tarih vermediyse uydurulmaz


def test_today_rates_stale_cache_on_outage():
    fx.today_rates(fetcher=_Fetcher(dict(_LIVE)))
    fx._rate_cache["fetched_at"] -= dt.timedelta(hours=2)
    for broken in (None, {}, {"USD": 1.0}, RuntimeError("boom")):   # her başarısızlık türü
        r = fx.today_rates(fetcher=_Fetcher(broken))
        assert r.stale is True and r.warning == fx.WARN_STALE
        assert r.source == "TCMB" and r.try_per["USD"] == 41.25
        assert r.as_of == dt.date(2026, 10, 3)


def test_today_rates_hardcoded_fallback_with_warning():
    r = fx.today_rates(fetcher=_Fetcher(None))
    assert r.source == "fallback" and r.stale is True
    assert r.warning == fx.WARN_FALLBACK and "Manuel doğrulama" in r.warning
    assert r.try_per == {"USD": fx._RATE_FALLBACK["USD"], "EUR": fx._RATE_FALLBACK["EUR"], "TRY": 1.0}
    assert r.as_of is None
    # yedek önbelleğe YAZILMAZ — TCMB dönünce hemen canlı kura geçilir
    f = _Fetcher(dict(_LIVE))
    assert fx.today_rates(fetcher=f).source == "TCMB" and f.calls == 1


# ─── convert ────────────────────────────────────────────────────────────────

def _rates():
    return fx.Rates(try_per={"USD": 40.0, "EUR": 50.0, "TRY": 1.0},
                    source="test", as_of=None, stale=False, warning=None)


def test_convert_same_currency_needs_no_rate():
    empty = fx.Rates(try_per={}, source="x", as_of=None, stale=False, warning=None)
    assert fx.convert(12.5, "USD", "usd", empty) == 12.5


def test_convert_try_and_cross():
    r = _rates()
    assert fx.convert(10, "USD", "TRY", r) == pytest.approx(400.0)
    assert fx.convert(400, "TRY", "USD", r) == pytest.approx(10.0)
    assert fx.convert(10, "EUR", "USD", r) == pytest.approx(12.5)    # TRY üzerinden çapraz
    assert fx.convert(12.5, "USD", "EUR", r) == pytest.approx(10.0)
    assert fx.convert(100, "TL", "USD", r) == pytest.approx(2.5)     # takma ad
    assert fx.convert(100, "₺", "$", r) == pytest.approx(2.5)


def test_convert_none_and_unknown():
    r = _rates()
    assert fx.convert(None, "USD", "TRY", r) is None
    with pytest.raises(ValueError):
        fx.convert(1, "GBP", "TRY", r)
    # TRY'si eksik elle kurulmuş Rates → TRY = 1.0 kabul edilir
    r2 = fx.Rates(try_per={"USD": 40.0}, source="x", as_of=None, stale=False, warning=None)
    assert fx.convert(40, "TRY", "USD", r2) == pytest.approx(1.0)


def test_convert_with_live_rates():
    r = fx.today_rates(fetcher=_Fetcher(dict(_LIVE)))
    assert fx.convert(1, "USD", "TRY", r) == pytest.approx(41.25)


# ─── TCMB ayrıştırıcı (ağsız) ───────────────────────────────────────────────

_TCMB_XML = """<?xml version="1.0" encoding="UTF-8"?>
<Tarih_Date Tarih="03.10.2026" Date="10/03/2026" Bulten_No="2026/188">
  <Currency CrossOrder="0" Kod="USD" CurrencyCode="USD">
    <Unit>1</Unit><Isim>ABD DOLARI</Isim>
    <ForexBuying>41.1800</ForexBuying><ForexSelling>41.2542</ForexSelling>
  </Currency>
  <Currency CrossOrder="1" Kod="AUD" CurrencyCode="AUD">
    <Unit>1</Unit><ForexSelling>27.1</ForexSelling>
  </Currency>
  <Currency CrossOrder="9" Kod="EUR" CurrencyCode="EUR">
    <Unit>1</Unit><ForexSelling>48.1037</ForexSelling>
  </Currency>
</Tarih_Date>"""


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_fetch_tcmb_parses_sample_xml(monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(_TCMB_XML.encode("utf-8")))
    out = fx._fetch_tcmb_rates()
    assert out == {"USD": 41.2542, "EUR": 48.1037, "as_of": dt.date(2026, 10, 3)}


def test_fetch_tcmb_failure_returns_none(monkeypatch):
    # autouse sahte urlopen zaten patlıyor → None, istisna dışarı sızmaz
    assert fx._fetch_tcmb_rates() is None
    # EUR eksik → None
    xml = _TCMB_XML.replace('CurrencyCode="EUR"', 'CurrencyCode="XXX"')
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _Resp(xml.encode("utf-8")))
    assert fx._fetch_tcmb_rates() is None


# ─── /api/currency-rates — b2b sarmalayıcısı (yanıt şekli değişmedi) ────────

def test_currency_rates_endpoint_live_then_cached(authed_client: TestClient, monkeypatch):
    f = _Fetcher(dict(_LIVE))
    monkeypatch.setattr(fx, "_fetch_tcmb_rates", f)
    j = authed_client.get("/api/currency-rates").json()
    assert list(j.keys()) == ["source", "USD", "EUR", "fetched_at", "cached"]
    assert j["source"] == "TCMB" and j["USD"] == 41.25 and j["EUR"] == 48.10
    assert j["cached"] is False and j["fetched_at"].endswith("Z")
    j2 = authed_client.get("/api/currency-rates").json()
    assert j2["cached"] is True and f.calls == 1 and j2["fetched_at"] == j["fetched_at"]
    assert "stale" not in j2 and "warning" not in j2


def test_currency_rates_endpoint_stale_cache(authed_client: TestClient, monkeypatch):
    monkeypatch.setattr(fx, "_fetch_tcmb_rates", _Fetcher(dict(_LIVE)))
    first = authed_client.get("/api/currency-rates").json()
    fx._rate_cache["fetched_at"] -= dt.timedelta(hours=2)
    monkeypatch.setattr(fx, "_fetch_tcmb_rates", _Fetcher(None))
    j = authed_client.get("/api/currency-rates").json()
    assert list(j.keys()) == ["source", "USD", "EUR", "fetched_at", "cached", "stale", "warning"]
    assert j["cached"] is True and j["stale"] is True
    assert j["warning"] == "TCMB'ye ulaşılamadı; önceki kur kullanılıyor."
    assert j["USD"] == 41.25 and j["fetched_at"] != first["fetched_at"]


def test_currency_rates_endpoint_hardcoded_fallback(authed_client: TestClient, monkeypatch):
    monkeypatch.setattr(fx, "_fetch_tcmb_rates", _Fetcher(None))
    j = authed_client.get("/api/currency-rates").json()
    assert list(j.keys()) == ["source", "USD", "EUR", "fetched_at", "cached", "stale", "warning"]
    assert j["source"] == "fallback" and j["USD"] == 34.50 and j["EUR"] == 37.20
    assert j["cached"] is False and j["stale"] is True
    assert j["warning"] == ("TCMB'ye ulaşılamadı; varsayılan kurlar kullanılıyor. "
                            "Manuel doğrulama önerilir.")


def test_currency_rates_endpoint_requires_finance(labtech_client: TestClient):
    assert labtech_client.get("/api/currency-rates").status_code == 403
