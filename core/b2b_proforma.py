# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""B2B sipariş proforması — İMZALI ticari sürümden (canlı veriden DEĞİL), ortak
proforma şablonuyla (`core/proforma_template`).

Belge yalnız ticari bilgidir: müşteri, kalemler (+ ml), toplamlar, şartlar,
imzada sabitlenmiş banka hesapları ve yönetim onayı ("Best Regards." altında
imza, ad, tarih, belge özeti).  Reçete, alım maliyeti ve teknik değerlendirme
BU BELGEYE GİRMEZ — onlar iç teknik föyde (`core/b2b_technical_sheet`).
"""
from core.consumption import parse_ml
from core.proforma_template import merged_terms, payment_terms_text, render_proforma
from core.proforma_template import proforma_filename as _proforma_filename


def proforma_filename(quote_number, customer=None) -> str:
    return _proforma_filename(quote_number, customer)


def order_proforma_doc(com: dict, signature: dict) -> dict:
    """İmzalı ticari snapshot → çizici sözleşmesi.  Bugünden önceki tek-banka /
    şartsız sürümler de basılır (`bank` → tek sütun, şablon şartları)."""
    banks = com.get("banks")
    if banks is None:
        banks = [com["bank"]] if com.get("bank") else []
    for b in banks:                                      # eski snapshot: tek IBAN alanı
        if b and "ibans" not in b:
            b["ibans"] = {b.get("currency") or "USD": b.get("iban") or ""}
    terms = com.get("terms") or merged_terms(
        {}, payment_terms_text(com.get("payment_terms"), com.get("advance_percent")))
    cust = com.get("customer") or {}
    signed_at = str(signature.get("signed_at") or "")
    return {
        "number": com.get("quote_number"),
        "date": signed_at[:10],
        "currency": com.get("currency"),
        "notes": com.get("notes") or "",
        "customer": {"name": cust.get("name"), "address": cust.get("address"),
                     "country": cust.get("country"),
                     "phone": " · ".join(x for x in (cust.get("phone"), cust.get("email"),
                                                     f"VAT: {cust['vat']}" if cust.get("vat") else "") if x)},
        "lines": [{"name": l.get("name"),
                   "weight_ml": l["weight_ml"] if "weight_ml" in l else parse_ml(l.get("name")),
                   "quantity": l.get("quantity"), "unit_price": l.get("unit_price"),
                   "line_total": l.get("line_total")} for l in com.get("lines") or []],
        "totals": {k: com.get(k) for k in ("subtotal", "shipping", "tax_percentage", "tax_amount", "total")},
        "terms": terms,
        "banks": [b for b in banks if b],
        "signature": {"name": signature.get("signer_name"), "date": signed_at,
                      "png_b64": signature.get("png_b64"), "document_hash": signature.get("document_hash"),
                      "revision": signature.get("revision")},
    }


def render_order_proforma(com: dict, signature: dict) -> bytes:
    return render_proforma(order_proforma_doc(com, signature))
