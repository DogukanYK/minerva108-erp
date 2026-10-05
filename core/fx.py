# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Döviz kurları — TEK KAYNAK (TCMB ForexSelling, TRY karşılığı).

Önceden `routers/b2b.py` içindeydi (Phase 3 / Task 2); satın alma planı da kur
çevirisi istediği için buraya taşındı.  `GET /api/currency-rates`
(`b2b.get_currency_rates`) artık `lookup()` üzerinde ince bir sarmalayıcı —
yanıtı ve davranışı birebir aynı.

Sıra: 1 saatlik süreç-içi önbellek → canlı TCMB → bayat önbellek (uyarılı) →
sabit yedek kurlar (uyarılı, "manuel doğrulama önerilir").

  • lookup(fetcher=None)       → ham durum (b2b yanıtını kuran sözleşme)
  • today_rates(fetcher=None)  → `Rates` (rapor/hesap kullanımı)
  • convert(amount, from, to, rates)

`fetcher` testte enjekte edilir — testler ağa ÇIKMAZ.  Sözleşmesi:
`() -> {"USD": float, "EUR": float, "as_of"?: date} | None` (TRY / 1 birim);
istisna atarsa başarısız sayılır.
"""
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable, Optional

_rate_cache = {"data": None, "fetched_at": None, "as_of": None}
_RATE_TTL_SECONDS = 3600   # 1 hour

# Sabit yedek — süreç hiç başarılı çekim yapmadıysa kullanılır.  Başarılı
# çekimle GÜNCELLENMEZ (o işi bayat önbellek görür); değerler eskidir, bu
# yüzden yanıt daima "Manuel doğrulama önerilir" uyarısı taşır.
_RATE_FALLBACK = {"USD": 34.50, "EUR": 37.20}

WARN_STALE = "TCMB'ye ulaşılamadı; önceki kur kullanılıyor."
WARN_FALLBACK = ("TCMB'ye ulaşılamadı; varsayılan kurlar kullanılıyor. "
                 "Manuel doğrulama önerilir.")

# Sembol / yerel yazım → ISO kodu
_ALIASES = {"TL": "TRY", "₺": "TRY", "$": "USD", "€": "EUR"}


def _parse_tcmb_date(root) -> Optional[date]:
    """Bülten tarihi — kök düğümün `Tarih="03.10.2026"` niteliği."""
    try:
        return datetime.strptime((root.get("Tarih") or "").strip(), "%d.%m.%Y").date()
    except ValueError:
        return None


def _fetch_tcmb_rates():
    """
    Fetch today's USD/EUR forex selling rates from TCMB.
    Returns dict like {"USD": 34.61, "EUR": 37.25, "as_of": date} on success,
    None on failure.  `as_of` is the bulletin date (None if unparseable).
    Uses stdlib only — no extra deps.
    """
    import urllib.request
    import xml.etree.ElementTree as ET

    url = "https://www.tcmb.gov.tr/kurlar/today.xml"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Minerva108-ERP/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            xml_data = resp.read().decode("utf-8")

        root = ET.fromstring(xml_data)
        out = {}
        for currency in root.findall("Currency"):
            code = currency.get("CurrencyCode")
            if code in ("USD", "EUR"):
                node = currency.find("ForexSelling")
                if node is not None and node.text:
                    out[code] = float(node.text.strip())
        if not {"USD", "EUR"}.issubset(out.keys()):
            return None
        out["as_of"] = _parse_tcmb_date(root)
        return out
    except Exception:
        return None


def reset_cache() -> None:
    """Süreç-içi önbelleği boşalt (testler için)."""
    _rate_cache.update({"data": None, "fetched_at": None, "as_of": None})


def lookup(fetcher: Optional[Callable] = None, *, now: Optional[datetime] = None) -> dict:
    """Önbellek → canlı → bayat önbellek → sabit yedek.

    Döner: {"data": {"source", "USD", "EUR"}, "fetched_at": datetime|None (UTC),
            "cached": bool, "stale": bool, "warning": str|None, "as_of": date|None}
    `fetcher` verilmezse TCMB (modül niteliği çağrı anında okunur → monkeypatch'lenebilir).
    """
    fetch = fetcher or _fetch_tcmb_rates
    now = now or datetime.utcnow()
    cached_data = _rate_cache.get("data")
    cached_at = _rate_cache.get("fetched_at")
    cached_as_of = _rate_cache.get("as_of")

    # ── Serve fresh cache ───────────────────────────────────────────────
    if cached_data and cached_at and (now - cached_at).total_seconds() < _RATE_TTL_SECONDS:
        return {"data": cached_data, "fetched_at": cached_at, "cached": True,
                "stale": False, "warning": None, "as_of": cached_as_of}

    # ── Try live fetch ──────────────────────────────────────────────────
    try:
        fresh = fetch()
    except Exception:
        fresh = None
    if fresh and "USD" in fresh and "EUR" in fresh:
        data = {"source": "TCMB", "USD": fresh["USD"], "EUR": fresh["EUR"]}
        as_of = fresh.get("as_of")
        _rate_cache.update({"data": data, "fetched_at": now, "as_of": as_of})
        return {"data": data, "fetched_at": now, "cached": False,
                "stale": False, "warning": None, "as_of": as_of}

    # ── Fall back to stale cache if available ──────────────────────────
    if cached_data:
        return {"data": cached_data, "fetched_at": cached_at, "cached": True,
                "stale": True, "warning": WARN_STALE, "as_of": cached_as_of}

    # ── Final hardcoded fallback ───────────────────────────────────────
    data = {"source": "fallback", "USD": _RATE_FALLBACK["USD"], "EUR": _RATE_FALLBACK["EUR"]}
    return {"data": data, "fetched_at": now, "cached": False,
            "stale": True, "warning": WARN_FALLBACK, "as_of": None}


@dataclass
class Rates:
    """1 birim döviz kaç TRY — `try_per = {"USD": x, "EUR": y, "TRY": 1.0}`."""
    try_per: dict
    source: str                        # "TCMB" | "fallback" | (testte serbest)
    as_of: Optional[date]              # TCMB bülten tarihi; yedek kurda None
    stale: bool
    warning: Optional[str]
    fetched_at: Optional[datetime] = None   # UTC, gösterimde to_tr()

    def rate(self, currency) -> float:
        """1 birim `currency` kaç TRY.  Bilinmeyen kod → ValueError."""
        code = _code(currency)
        if code == "TRY":
            return float(self.try_per.get("TRY", 1.0))
        val = self.try_per.get(code)
        if val is None or float(val) <= 0:
            raise ValueError(f"Kur bulunamadı: {currency!r}")
        return float(val)


def today_rates(fetcher: Optional[Callable] = None) -> Rates:
    """Bugünün kurları (1 saat önbellekli; TCMB yoksa bayat ya da sabit yedek, uyarılı)."""
    st = lookup(fetcher)
    d = st["data"]
    return Rates(
        try_per={"USD": float(d["USD"]), "EUR": float(d["EUR"]), "TRY": 1.0},
        source=d["source"],
        as_of=st["as_of"],
        stale=st["stale"],
        warning=st["warning"],
        fetched_at=st["fetched_at"],
    )


def _code(currency) -> str:
    c = (currency or "").strip().upper()
    return _ALIASES.get(c, c)


def convert(amount, from_cur, to_cur, rates: Rates) -> Optional[float]:
    """`amount`'u `from_cur`'dan `to_cur`'a çevirir (TRY üzerinden çapraz kur).

    Aynı para biriminde aynen döner (kur gerekmez); `amount` None ise None
    (fiyatsız teklif).  Yuvarlama YAPMAZ — çağıran yuvarlar.
    """
    if amount is None:
        return None
    f, t = _code(from_cur), _code(to_cur)
    if f == t:
        return float(amount)
    return float(amount) * rates.rate(f) / rates.rate(t)
