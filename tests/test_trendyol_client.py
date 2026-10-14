"""
Trendyol HTTP istemcisi (core.trendyol.Api) — AĞ YOK (httpx.MockTransport).

Kilitlenenler:
  • User-Agent "{satıcı id} - SelfIntegration" + Basic auth; storeFrontCode
    yalnız ayarlıysa.
  • 429 → Retry-After'a uyup tekrar; tükenince anlaşılır hata.  5xx/taşıma
    hatası sınırlı tekrar.  401/403 → TrendyolAuthError, 400 → TrendyolBadRequest.
  • Grup başına bekleme (sipariş akışı ≥5 sn).
  • Kimlik bilgileri hiçbir hata mesajına / log satırına girmez.
  • Okuma-yalnızlık kod yapısıyla: Api'nin HTTP metotları `get` + izin listeli
    `query` (yalnız buybox SORGUSU POST'u); kaynakta başka post/put/patch/delete yok.
  • Ayarlar çağrı anında env'den; eksikler YALNIZ adlarıyla raporlanır.
"""
import base64
import inspect
import json
import logging
import pathlib
import re

import httpx
import pytest

from core import trendyol as T
from tests.trendyol_fakes import trendyol_stop_reset  # noqa: F401 (autouse)

CFG = {"seller_id": "123456", "api_key": "KEY-abc", "api_secret": "s3cr3t-XYZ",
       "base_url": "https://apigw.trendyol.com/integration", "storefront": ""}


class Clock:
    def __init__(self):
        self.t = 1000.0
        self.sleeps = []

    def sleep(self, s):
        self.sleeps.append(round(s, 3))
        self.t += s

    def now(self):
        return self.t


def _api(handler, cfg=None):
    clock = Clock()
    api = T.Api(cfg or CFG, transport=httpx.MockTransport(handler), sleep=clock.sleep, clock=clock.now)
    return api, clock


def test_headers_auth_and_user_agent():
    seen = {}

    def handler(req):
        seen.update(dict(req.headers))
        seen["url"] = str(req.url)
        return httpx.Response(200, json={"content": []})

    api, _ = _api(handler)
    api.get("products", "/product/sellers/123456/products/approved", {"page": 0, "size": 1, "x": None})
    assert seen["user-agent"] == "123456 - SelfIntegration"
    assert seen["authorization"] == "Basic " + base64.b64encode(b"KEY-abc:s3cr3t-XYZ").decode()
    assert "storefrontcode" not in seen
    assert seen["url"].startswith("https://apigw.trendyol.com/integration/product/sellers/123456/products/approved")
    assert "x=" not in seen["url"]                      # None parametre gönderilmez


def test_storefront_header_only_when_set():
    seen = {}

    def handler(req):
        seen.update(dict(req.headers))
        return httpx.Response(200, json={})

    api, _ = _api(handler, dict(CFG, storefront="TR"))
    api.get("products", "/x")
    assert seen["storefrontcode"] == "TR"


def test_429_retry_after_then_success():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        if n["i"] == 1:
            return httpx.Response(429, headers={"Retry-After": "7"})
        return httpx.Response(200, json={"ok": 1})

    api, clock = _api(handler)
    assert api.get("finance", "/f") == {"ok": 1}
    assert clock.sleeps == [7.0] and api.calls == 2


def test_429_exhausted_raises_readable_error():
    api, clock = _api(lambda req: httpx.Response(429))
    with pytest.raises(T.TrendyolError, match="429"):
        api.get("finance", "/f")
    assert clock.sleeps == [10, 30, 60]


def test_5xx_retry_capped():
    api, clock = _api(lambda req: httpx.Response(503))
    with pytest.raises(T.TrendyolError, match="503"):
        api.get("finance", "/f")
    assert api.calls == 4                                # 1 + 3 tekrar
    assert clock.sleeps == [2, 5, 15]


def test_transport_error_retried_then_raises():
    def handler(req):
        raise httpx.ConnectError("boom", request=req)

    api, clock = _api(handler)
    with pytest.raises(T.TrendyolError, match="ulaşılamadı"):
        api.get("finance", "/f")
    assert clock.sleeps == [2, 5, 15]


@pytest.mark.parametrize("code, cls", [(401, T.TrendyolAuthError), (403, T.TrendyolAuthError),
                                       (400, T.TrendyolBadRequest), (404, T.TrendyolBadRequest)])
def test_error_mapping(code, cls):
    api, _ = _api(lambda req: httpx.Response(code, json={"errors": [{"message": "bad things"}]}))
    with pytest.raises(cls) as ei:
        api.get("finance", "/f")
    assert "s3cr3t" not in str(ei.value) and "KEY-abc" not in str(ei.value)


def test_bad_request_carries_short_body():
    api, _ = _api(lambda req: httpx.Response(400, text="x" * 1000))
    with pytest.raises(T.TrendyolBadRequest) as ei:
        api.get("finance", "/f")
    assert len(str(ei.value)) < 400


def test_pacing_per_group():
    api, clock = _api(lambda req: httpx.Response(200, json={}))
    api.get("orders", "/a")
    api.get("orders", "/b")                              # stream: ≥5 sn
    api.get("finance", "/c")                             # başka grup: bekleme yok
    assert clock.sleeps == [5.0]


def test_credentials_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    api, _ = _api(lambda req: httpx.Response(401))
    with pytest.raises(T.TrendyolAuthError):
        api.get("orders", "/o")
    assert "s3cr3t" not in caplog.text and "KEY-abc" not in caplog.text


def test_api_is_read_only_by_construction():
    """Tek yazma-biçimli çağrı Api.query'deki izin listeli POST'tur (buybox
    SORGUSU — Trendyol'da hiçbir şeyi değiştirmez); başka post/put/patch/delete yok."""
    public = sorted(n for n in dir(T.Api) if not n.startswith("_"))
    assert public == ["close", "get", "query"]
    src = pathlib.Path(T.__file__).read_text(encoding="utf-8")
    hits = re.findall(r"\b(httpx|self\._http|_http|client)\.(post|put|patch|delete|request|send|stream)\(", src)
    assert hits == [("self._http", "post")]
    assert "self._http.post(" in inspect.getsource(T.Api.query)
    assert [rx.pattern for rx in T.QUERY_PATHS] == [r"^/product/sellers/\d+/products/buybox-information$"]


def test_query_only_allowlisted_paths():
    seen = []

    def handler(req):
        seen.append((req.method, req.url.path, json.loads(req.content or b"{}")))
        return httpx.Response(200, json={"buyboxInfo": []})

    api, _ = _api(handler)
    for bad in ("/product/sellers/123456/products", "/order/sellers/123456/orders/stream",
                "/product/sellers/123456/products/buybox-information/x", "/qna/sellers/123456/questions/1/answers"):
        with pytest.raises(ValueError):
            api.query("products", bad, {"barcodes": ["1"]})
    assert seen == []
    api.query("products", "/product/sellers/123456/products/buybox-information", {"barcodes": ["868"]})
    assert seen == [("POST", "/integration/product/sellers/123456/products/buybox-information", {"barcodes": ["868"]})]


def test_config_read_at_call_time(monkeypatch):
    for n in ("TRENDYOL_SELLER_ID", "TRENDYOL_API_KEY", "TRENDYOL_API_SECRET", "TRENDYOL_BASE_URL"):
        monkeypatch.delenv(n, raising=False)
    assert not T.is_configured()
    assert T.config_problems() == ["TRENDYOL_SELLER_ID", "TRENDYOL_API_KEY", "TRENDYOL_API_SECRET"]
    with pytest.raises(T.TrendyolNotConfigured):
        T.Api()
    monkeypatch.setenv("TRENDYOL_SELLER_ID", "12a")
    monkeypatch.setenv("TRENDYOL_API_KEY", "k")
    monkeypatch.setenv("TRENDYOL_API_SECRET", "very-secret")
    monkeypatch.setenv("TRENDYOL_BASE_URL", "http://insecure")
    probs = T.config_problems()
    assert any("TRENDYOL_SELLER_ID" in p for p in probs) and any("https" in p for p in probs)
    assert all("very-secret" not in p for p in probs)
    monkeypatch.setenv("TRENDYOL_SELLER_ID", "123456")
    monkeypatch.delenv("TRENDYOL_BASE_URL")
    assert T.is_configured()
    assert T.config()["base_url"] == T.DEFAULT_BASE_URL


def test_timestamp_helpers_roundtrip(monkeypatch):
    from datetime import datetime
    dt = datetime(2026, 10, 13, 9, 30, 0)
    plain = T.to_ty_ms(dt)
    assert T.ty_ts(plain) == dt
    assert T.ty_ts(None) is None and T.ty_ts("x") is None and T.ty_ts(0) is None
    monkeypatch.setattr(T, "TS_TR_WALLCLOCK", True)     # kalibrasyon anahtarı
    assert T.to_ty_ms(dt) == plain + 3 * 3600 * 1000
    assert T.ty_ts(T.to_ty_ms(dt)) == dt                # iki yön aynı ofseti uygular


def test_redirect_is_not_success():
    api, _ = _api(lambda req: httpx.Response(302, headers={"Location": "https://elsewhere"}))
    with pytest.raises(T.TrendyolError, match="302"):
        api.get("orders", "/o")


def test_408_is_retried_like_5xx():
    n = {"i": 0}

    def handler(req):
        n["i"] += 1
        return httpx.Response(408) if n["i"] == 1 else httpx.Response(200, json={"content": []})

    api, clock = _api(handler)
    assert api.get("finance", "/f") == {"content": []} and clock.sleeps == [2]


def test_bad_request_carries_status():
    api, _ = _api(lambda req: httpx.Response(404, json={}))
    with pytest.raises(T.TrendyolBadRequest) as ei:
        api.get("finance", "/f")
    assert ei.value.status == 404


def test_stop_flag_interrupts_requests_and_waits():
    calls = []
    api, _ = _api(lambda req: calls.append(1) or httpx.Response(200, json={}))
    T.request_stop()
    with pytest.raises(T.TrendyolStopped):
        api.get("orders", "/o")
    assert calls == []                                   # istek hiç gitmedi
    with pytest.raises(T.TrendyolStopped):
        T._interruptible_sleep(5)                        # bekleme anında kesilir
    T.reset_stop()
    assert api.get("orders", "/o") == {}
