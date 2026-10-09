# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""Proformada basılacak banka hesapları — TEK KAYNAK.

Üç proforma (teklif önizlemesi, teslimat PRF-, B2B sipariş) belge başına 1–3
banka profili seçer.  Fiyat para birimi USD/EUR/TRY'dir; banka profili ayrıca
RUB IBAN taşıyabilir (ör. Rusya ödemeleri için Emlak Bank) ve proformada o satır
yalnız seçili bankada RUB varsa basılır.

Varsayılan seçim: ülke + para birimi kuralı (`bank_rules`, `*` = tüm ülkeler)
varsa o banka; yoksa `is_default` işaretli aktif bankalar (şablon: Kuveyt Türk +
Vakıfbank).  Hiçbiri yoksa seçim boş kalır, kullanıcı seçer.
"""
import json

from database import BankProfile, BankRule

MAX_BANKS = 3
#: (para birimi, BankProfile alanı, proforma satır etiketi)
IBAN_FIELDS = (("USD", "iban_usd", "USD IBAN NO"), ("EUR", "iban_eur", "EURO IBAN NO"),
               ("TRY", "iban_try", "TRY IBAN NO"), ("RUB", "iban_rub", "RUB IBAN NO"))


class BankSelectionError(ValueError):
    def __init__(self, detail, code="invalid_bank"):
        self.detail, self.code = detail, code
        super().__init__(detail)


def fold_country(text):
    from core.supplier_prices import normalize
    return normalize(text or "")


def iban_for(profile, currency):
    field = next((f for c, f, _ in IBAN_FIELDS if c == (currency or "").upper()), None)
    return (getattr(profile, field) or None) if (profile is not None and field) else None


def has_iban(profile):
    return any(getattr(profile, f) for _, f, _ in IBAN_FIELDS)


def bank_snapshot(profile, currency=None):
    """Belgeye sabitlenecek banka bilgisi.  `iban`/`currency` eski tek-banka
    sözleşmesi için (belge para biriminin IBAN'ı) korunur."""
    if profile is None:
        return None
    cur = (currency or "").upper()
    return {"id": profile.id, "label": profile.label, "bank_name": profile.bank_name,
            "branch": profile.branch or "", "swift": profile.swift or "",
            "account_holder": profile.account_holder,
            "ibans": {c: (getattr(profile, f) or "") for c, f, _ in IBAN_FIELDS},
            "currency": cur, "iban": iban_for(profile, cur) or ""}


def profile_view(profile):
    """Yönetim / seçim ekranları için (aktiflik ve varsayılan dahil)."""
    return {"id": profile.id, "label": profile.label, "bank_name": profile.bank_name,
            "branch": profile.branch or "", "swift": profile.swift or "",
            "account_holder": profile.account_holder,
            "iban_usd": profile.iban_usd or "", "iban_eur": profile.iban_eur or "",
            "iban_try": profile.iban_try or "", "iban_rub": profile.iban_rub or "",
            "is_active": bool(profile.is_active), "is_default": bool(profile.is_default),
            "sort_order": profile.sort_order or 0}


def parse_ids(value):
    """JSON metni / liste / tek id → tekrarsız sıralı int listesi (geçersiz atlanır)."""
    if value in (None, ""):
        return []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return []
    if isinstance(value, int):
        value = [value]
    out = []
    for v in value if isinstance(value, (list, tuple)) else []:
        try:
            i = int(v)
        except (TypeError, ValueError):
            continue
        if i > 0 and i not in out:
            out.append(i)
    return out


def dump_ids(ids):
    return json.dumps(list(ids)) if ids else None


def suggest_bank(db, country, currency):
    """Ülke + para birimi kuralı → banka profili (yoksa ülkeden bağımsız '*' kuralı)."""
    cur = (currency or "").upper()
    for key in (fold_country(country), "*"):
        if not key:
            continue
        rule = db.query(BankRule).filter(BankRule.country == key, BankRule.currency == cur).first()
        if rule:
            prof = db.query(BankProfile).filter_by(id=rule.bank_profile_id, is_active=True).first()
            if prof:
                return prof
    return None


def default_bank_ids(db, country, currency):
    """Yeni belge için önerilen seçim: kural bankası, yoksa varsayılan bankalar."""
    rule_bank = suggest_bank(db, country, currency)
    if rule_bank is not None:
        return [rule_bank.id]
    rows = (db.query(BankProfile).filter(BankProfile.is_active == True,             # noqa: E712
                                         BankProfile.is_default == True)            # noqa: E712
            .order_by(BankProfile.sort_order, BankProfile.id).limit(MAX_BANKS).all())
    return [r.id for r in rows if has_iban(r)]


def resolve_banks(db, ids, *, required=True):
    """Seçilen id'ler → BankProfile listesi (sıra korunur).  1–3 banka, aktif,
    en az bir IBAN'lı; aksi hâlde `BankSelectionError`."""
    ids = parse_ids(ids)
    if not ids:
        if required:
            raise BankSelectionError("Proformada en az bir banka hesabı seçilmeli.", "bank_required")
        return []
    if len(ids) > MAX_BANKS:
        raise BankSelectionError(f"Proformaya en fazla {MAX_BANKS} banka basılabilir.", "too_many_banks")
    rows = {b.id: b for b in db.query(BankProfile).filter(BankProfile.id.in_(ids))}
    out = []
    for i in ids:
        prof = rows.get(i)
        if prof is None or not prof.is_active:
            raise BankSelectionError("Seçilen banka profili bulunamadı ya da pasif.", "invalid_bank")
        if not has_iban(prof):
            raise BankSelectionError(f"«{prof.label}» profilinde IBAN tanımlı değil.", "bank_iban_missing")
        out.append(prof)
    return out


def banks_for_document(db, stored_ids, country, currency):
    """Kayıtlı seçim (belge oluşturulurken) varsa onu, yoksa varsayılanı çöz.
    Pasifleşmiş / silinmiş profiller sessizce atlanır — belge yine basılır."""
    ids = parse_ids(stored_ids) or default_bank_ids(db, country, currency)
    rows = {b.id: b for b in db.query(BankProfile).filter(BankProfile.id.in_(ids or [0]))}
    return [rows[i] for i in ids if i in rows and rows[i].is_active and has_iban(rows[i])][:MAX_BANKS]
