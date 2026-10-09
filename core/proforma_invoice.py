"""
Teslimat proforması (PRF-) — ortak proforma şablonuyla (`core/proforma_template`).

Eskiden antetli kâğıda lacivert bir tablo basıyordu; 09.10.2026'dan beri B2B
sipariş proforması ve teklif PDF'iyle AYNI şablon (beğenilen `Proforma_Sablon_Bos`).
Bu modül yalnız teslimat görünümünü (`routers.delivery._view` + seçili bankalar)
çizicinin `doc` sözleşmesine çevirir.  Ürün adı teslimat oluşturulurken belge
diline göre snapshot edilmiştir (DeliveryItem.item_name).
"""
from core.delivery_note import slug_part
from core.proforma_template import _rows_that_fit, _story, merged_terms, render_proforma


def proforma_filename(document_no, recipient=None, ext: str = "pdf") -> str:
    base = (document_no or "proforma").replace("/", "-").replace(" ", "_")
    who = slug_part(recipient)
    return f"{base}{'_' + who if who else ''}.{ext}"


def proforma_doc(view: dict) -> dict:
    """Teslimat görünümü → çizici sözleşmesi.  `view["banks"]` router'da seçili
    profillerden (yoksa ülke kuralı / varsayılan) doldurulur."""
    org = (view.get("recipient_org") or "").strip()
    person = (view.get("recipient_name") or "").strip()
    name = org or person
    if org and person and person.casefold() != org.casefold():
        name = f"{org} (Attn: {person})"
    lines, subtotal = [], 0.0
    for it in view.get("items") or []:
        qty = float(it.get("quantity") or 0)
        up = it.get("unit_price")
        total = round(float(up) * qty, 2) if up is not None else None
        subtotal += total or 0.0
        lines.append({"name": it.get("item_name"), "weight_ml": it.get("weight_ml"),
                      "quantity": qty, "unit_price": up, "line_total": total})
    terms = view.get("proforma_terms") or merged_terms({}, None)
    subtotal = round(subtotal, 2)
    return {
        "number": view.get("document_no"),
        "date": str(view.get("date") or "")[:10],
        "currency": view.get("currency") or "USD",
        "notes": view.get("note") or "",
        "customer": {"name": name, "address": view.get("customer_address"),
                     "country": view.get("customer_country"), "phone": view.get("recipient_phone")},
        "lines": lines,
        "totals": {"subtotal": subtotal, "shipping": 0, "tax_percentage": 0, "tax_amount": 0,
                   "total": subtotal},
        "terms": terms,
        "banks": view.get("banks") or [],
        "signature": None,
    }


def render_proforma_pdf(view: dict) -> bytes:
    return render_proforma(proforma_doc(view))


def _proforma_story(view: dict, s: float = 1.0):
    """Ölçekli flowable listesi — render ile aynı satır sayısı (auto-fit testleri
    tam boy sayfa sayısını ölçer)."""
    doc = proforma_doc(view)
    return _story(doc, s, rows=_rows_that_fit(doc))
