# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Anthropic istemci fabrikası.  Anahtar YALNIZ ortamdan (`ANTHROPIC_API_KEY`) ve
çağrı anında okunur (core/parasut._env kalıbı) — import zamanında env'e
bakılmaz, sohbete yapıştırılmaz, loglanmaz.

Runner yalnız `client.beta.messages.stream(**params)` kullanır; testler
`core.assistant.client.get_client`'ı sahte istemciyle monkeypatch'ler
(tests/assistant_fakes.py) — ağ hiç açılmaz.

SDK `anthropic==0.125.0` (httpx tabanlı; `anthropic.Timeout` = httpx.Timeout).
`max_retries=1`: SDK yalnız bağlantı hatası / 408 / 409 / 429 / 5xx'te yeniden
dener; 4xx'te asla (runner'daki tek seferlik "safe mode" ayrı bir yoldur).

TEK İSTEMCİ: `get_client()` süreç başına bir `AsyncAnthropic` tutar (anahtara
göre önbellek) — her soruda yeni httpx havuzu + TLS el sıkışması kurulmaz,
bağlantı yeniden kullanılır (README "tek istemci" kalıbı).  Anahtar env'de
değişirse yeni istemci kurulur; `reset_cache()` testler için.
"""
import os
from typing import Optional, Tuple

_CACHED: Optional[Tuple[str, object]] = None     # (api_key, AsyncAnthropic)

CONNECT_TIMEOUT = 5.0
READ_TIMEOUT = 60.0
WRITE_TIMEOUT = 10.0
MAX_RETRIES = 1


class NotConfigured(RuntimeError):
    """`ANTHROPIC_API_KEY` yok — 503 'not_configured' (budget.check_budget)."""


def api_key() -> str:
    return os.getenv("ANTHROPIC_API_KEY", "").strip()


def is_configured() -> bool:
    return bool(api_key())


def reset_cache() -> None:
    """Önbellekli istemciyi bırak (testler / anahtar değişimi)."""
    global _CACHED
    _CACHED = None


def get_client():
    """-> anthropic.AsyncAnthropic (süreç başına tek, anahtara göre önbellekli).
    SDK burada, tembel import edilir."""
    global _CACHED
    import anthropic   # modül anahtarsız da import edilebilsin diye burada

    key = api_key()
    if not key:
        raise NotConfigured("ANTHROPIC_API_KEY tanımlı değil")
    if _CACHED is not None and _CACHED[0] == key:
        return _CACHED[1]
    client = anthropic.AsyncAnthropic(
        api_key=key,
        max_retries=MAX_RETRIES,
        timeout=anthropic.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT,
                                  read=READ_TIMEOUT, write=WRITE_TIMEOUT),
    )
    _CACHED = (key, client)
    return client
