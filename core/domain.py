# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Aktif "domain" (panel) çözümü — Kozmetik ↔ Food Supplement ayrımı.

Aynı login içinde kullanıcı üst bardaki düğmeyle iki panel arasında geçer;
seçim `active_domain` çerezinde tutulur (gizli değil, sadece görünüm tercihi).
Tüm liste uçları aktif domaine göre filtrelenir, create'ler aktif domaini
damgalar.  Varsayılan ve tüm geçmiş veri 'cosmetics' — kozmetik deneyimi
değişmez, Food Supplement paneli boş başlar.
"""
from fastapi import Request, Depends

VALID_DOMAINS  = ("cosmetics", "supplement")
DEFAULT_DOMAIN = "cosmetics"
DOMAIN_COOKIE  = "active_domain"
DOMAIN_LABELS  = {"cosmetics": "Kozmetik", "supplement": "Food Supplement"}


def normalize_domain(value) -> str:
    """Serbest girdiyi geçerli bir domaine indir; tanınmazsa varsayılan."""
    v = (value or "").strip().lower()
    return v if v in VALID_DOMAINS else DEFAULT_DOMAIN


def get_active_domain(request: Request) -> str:
    """İsteğin çerezinden aktif domaini oku (yoksa 'cosmetics')."""
    return normalize_domain(request.cookies.get(DOMAIN_COOKIE))


def domain_label(domain) -> str:
    return DOMAIN_LABELS.get(normalize_domain(domain), DOMAIN_LABELS[DEFAULT_DOMAIN])


def active_domain(request: Request) -> str:
    """FastAPI bağımlılığı — uçlar `domain: str = Depends(active_domain)` ile
    aktif domaini alır (Request'i ayrıca tanımlamalarına gerek kalmaz)."""
    return get_active_domain(request)
