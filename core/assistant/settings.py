# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Minerva Asistan ayarları — AppSetting KV satırları (`assistant.<alan>`),
deploy'suz değiştirilebilir (SuperAdmin, PUT /api/assistant/admin/settings).

Alan başına bir satır; satır yoksa/bozuksa dataclass varsayılanı geçerlidir.
`enabled` için `core.production_plan.source_choice_enabled` ile aynı
`_OFF_VALUES` semantiği: boş → AÇIK, '0'/'false'/'off'/'hayır'… → KAPALI.

DİKKAT — önbellek: `model`, `effort` ve `cache_ttl` istek önekine girer;
değişince mevcut sohbetler rotasyonla yeni sohbete geçer (history.needs_rotation
`model`'e bakar, prompt_hash dondurulmuş istemi kilitler).
"""
from dataclasses import dataclass, fields, replace
from typing import Dict, Optional

from sqlalchemy.orm import Session

from database import AppSetting
from core.assistant.budget import PRICES

PREFIX = "assistant."
EFFORTS = ("low", "medium", "high")
TTLS = ("5m", "1h")
_OFF_VALUES = ("0", "false", "off", "no", "hayir", "hayır", "kapali", "kapalı")

MODEL_LABELS = {
    "claude-opus-5-5":   "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-haiku-5-5":  "Claude Haiku 5.5",
}


@dataclass(frozen=True)
class Settings:
    enabled: bool = True
    model: str = "claude-opus-5-5"
    effort: str = "low"                 # low | medium | high
    monthly_cap_usd: float = 100.0      # aşınca 402, ay başında açılır
    daily_user_cap_usd: float = 3.0     # kişi başı günlük
    max_tool_rounds: int = 6            # bir soruda en fazla araç turu
    max_output_tokens: int = 1500       # thinking de sayılır; stop_reason=max_tokens artarsa yükselt
    max_turns: int = 12                 # sohbet başına soru; aşınca rotasyon
    max_history_tokens: int = 8000      # (son istek boyutu − statik önek) eşiği
    cache_ttl: str = "1h"               # sistem öneki üzerindeki açık breakpoint TTL'i


#: AppSetting anahtarı → alan adı
KEYS: Dict[str, str] = {PREFIX + f.name: f.name for f in fields(Settings)}

#: (min, max) — save_settings doğrulaması
RANGES = {
    "monthly_cap_usd":    (0.0, 10000.0),
    "daily_user_cap_usd": (0.0, 1000.0),
    "max_tool_rounds":    (0, 12),
    "max_output_tokens":  (256, 32000),
    "max_turns":          (1, 200),
    "max_history_tokens": (1000, 150000),
}


def _parse(name: str, raw: Optional[str], default):
    """Tek satır değerini alanın tipine toleranslı çevir; bozuksa varsayılan."""
    v = (raw or "").strip()
    if not v:
        return default
    try:
        if name == "enabled":
            return v.lower() not in _OFF_VALUES
        if isinstance(default, bool):
            return v.lower() not in _OFF_VALUES
        if isinstance(default, int):
            return int(float(v))
        if isinstance(default, float):
            return float(v)
        return v
    except (TypeError, ValueError):
        return default


def load_settings(db: Session) -> Settings:
    """Tek sorgu (`key LIKE 'assistant.%'`), eksik/bozuk satırda varsayılan."""
    base = Settings()
    rows = db.query(AppSetting).filter(AppSetting.key.like(PREFIX + "%")).all()
    values = {}
    for r in rows:
        name = KEYS.get(r.key)
        if name is None:
            continue
        values[name] = _parse(name, r.value, getattr(base, name))
    s = replace(base, **values)
    # Tanınmayan model/efor/TTL satırı (elle yazılmış) varsayılana düşer —
    # fiyat tablosunda olmayan modelle maliyet hesaplanamaz.
    if s.model not in PRICES:
        s = replace(s, model=base.model)
    if s.effort not in EFFORTS:
        s = replace(s, effort=base.effort)
    if s.cache_ttl not in TTLS:
        s = replace(s, cache_ttl=base.cache_ttl)
    return s


def validate(changes: dict) -> dict:
    """Gelen değişiklikleri doğrula ve tipine çevir; hata → ValueError (Türkçe)."""
    out = {}
    for name, value in (changes or {}).items():
        if name not in KEYS.values():
            raise ValueError(f"Bilinmeyen ayar: {name}")
        if name == "enabled":
            out[name] = bool(value) if not isinstance(value, str) else value.lower() not in _OFF_VALUES
        elif name == "model":
            if value not in PRICES:
                raise ValueError(f"Model fiyat tablosunda yok: {value}")
            out[name] = value
        elif name == "effort":
            if value not in EFFORTS:
                raise ValueError("effort low|medium|high olmalı")
            out[name] = value
        elif name == "cache_ttl":
            if value not in TTLS:
                raise ValueError("cache_ttl 5m|1h olmalı")
            out[name] = value
        else:
            lo, hi = RANGES[name]
            try:
                num = float(value)
            except (TypeError, ValueError):
                raise ValueError(f"{name} sayı olmalı")
            if num < lo or num > hi:
                raise ValueError(f"{name} {lo}–{hi} aralığında olmalı")
            out[name] = int(num) if isinstance(lo, int) else float(num)
    return out


def save_settings(db: Session, changes: dict) -> Settings:
    """Doğrular, satırları yazar (`flush`; commit çağıranın) ve yeni ayarı döner."""
    clean = validate(changes)
    for name, value in clean.items():
        key = PREFIX + name
        text = ("true" if value else "false") if isinstance(value, bool) else str(value)
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        if row is None:
            db.add(AppSetting(key=key, value=text))
        else:
            row.value = text
    db.flush()
    return load_settings(db)


def model_label(model: str) -> str:
    """'claude-opus-5-5' → 'Claude Opus 5.5' (bilinmeyen model olduğu gibi)."""
    if model in MODEL_LABELS:
        return MODEL_LABELS[model]
    parts = (model or "").split("-")
    if len(parts) >= 4 and parts[0] == "claude":
        return f"Claude {parts[1].title()} {parts[2]}.{parts[3]}"
    return model or "—"
