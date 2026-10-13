# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Minerva Asistan — metin yazma modu aracı (13.10.2026).

`check_product_text`: modelin yazdığı ürün metni taslağını (açıklama, Amazon
maddeleri, etiket metni) `core.product_content_rules.validate_card` ile
denetler — Ürün İçerik Kartı'nın AYNI motoru (ikinci kural kopyası YOK):
INCI'de karşılığı olmayan bileşen/koku iddiası, yasaklı (tıbbi/abartılı)
iddia kalıpları, başlıktaki hacmin verilen hacimle tutarlılığı.  DB'siz,
yan etkisiz; `handlers.py` ile aynı sözleşme (`fn(ctx, db, args) -> dict`).
"""
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from core.assistant.handlers import ToolError, _text

if TYPE_CHECKING:
    from core.assistant.tools import ToolContext

TEXT_MAX = 8000
ISSUES_MAX = 20
#: INCI verilmediyse bu kontroller anlamsız (her bileşen "karşılıksız" görünür)
INCI_DEPENDENT = {"INCI_EMPTY", "KI_NOT_IN_INCI", "SCENT_NOT_IN_INCI", "TITLE_SCENT_UNSUPPORTED",
                  "TEXT_INGREDIENT_UNSUPPORTED", "ALLERGEN_NOT_IN_INCI"}
#: Kartın yapısal alanlarına özgü, serbest metinde geçersiz kontroller
CARD_ONLY = {"KI_MISSING", "BRAND_MISMATCH", "BODY_CONTRACT"}


def check_product_text(ctx: "ToolContext", db: Session, args: dict) -> dict:
    from core.product_content_rules import parse_size, validate_card
    text = _text(args.get("text"))[:TEXT_MAX]
    if not text:
        raise ToolError("Denetlenecek metin boş")
    lang = args.get("lang") if args.get("lang") in ("tr", "en") else "tr"
    title = _text(args.get("title"))[:300]
    inci = _text(args.get("inci"))[:4000]
    size = _text(args.get("size"))[:40]
    card = {"title": {lang: title}, "intro": {lang: text}, "inci_declared": inci or None}
    qty, unit = parse_size(size)
    if qty:
        card["sizes"] = [{"net_qty": qty, "net_unit": unit}]
    issues, seen = [], set()
    for i in validate_card(card):
        if i.code in CARD_ONLY or (not inci and i.code in INCI_DEPENDENT):
            continue
        key = (i.code, i.msg_tr)
        if key in seen:
            continue
        seen.add(key)
        issues.append({"code": i.code, "severity": "hata" if i.severity == "E" else "uyarı",
                       "field": {"intro": "metin", "title.tr": "başlık", "title.en": "başlık"}.get(i.field, i.field),
                       "msg": i.msg_tr})
    if not inci:
        issues.append({"code": "INCI_NOT_GIVEN", "severity": "uyarı", "field": "inci",
                       "msg": "INCI listesi verilmedi: bileşen/koku iddiaları doğrulanamadı — ürünün INCI'sini "
                              "kullanıcıdan isteyin ya da metinde bileşen iddiası kullanmayın."})
    errors = sum(1 for i in issues if i["severity"] == "hata")
    return {"ok": errors == 0, "errors": errors, "warnings": len(issues) - errors,
            "issues": issues[:ISSUES_MAX], "checked": {"inci": bool(inci), "size": size or None, "lang": lang}}
