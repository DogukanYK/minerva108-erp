"""
Sistem genelinde üretilen PDF belgeleri HER ZAMAN A4 olmalı (içerik uzunluğundan
bağımsız).  Asıl regresyon: antetli kağıt US Letter idi ve merge antetliyi taban
aldığı için teslim belgesi + proforma Letter çıkıyordu.  merge_letterhead artık
antetliyi A4'e ölçekler; reportlab üreticileri zaten pagesize=A4.
"""
from io import BytesIO

from pypdf import PdfReader

A4_W, A4_H = 595.276, 841.89   # pt


def _all_pages_a4(pdf_bytes: bytes) -> bool:
    pages = PdfReader(BytesIO(pdf_bytes)).pages
    assert len(pages) >= 1
    for pg in pages:
        b = pg.mediabox
        if not (abs(float(b.width) - A4_W) < 2 and abs(float(b.height) - A4_H) < 2):
            return False
    return True


def test_delivery_note_is_a4_on_letterhead():
    from core.delivery_note import render_delivery_pdf
    view = {
        "document_no": "TES-2026-09999", "recipient_name": "Test Alıcı",
        "recipient_org": "ACME A.Ş.", "recipient_phone": "0555", "delivery_type": "hediye",
        "method": "elden", "note": "deneme", "dispatched_by": "dogukan",
        "created_at": "2026-06-26 12:00",
        "items": [{"item_name": "Argan Oil", "quantity": 2, "unit": "adet"}],
    }
    assert _all_pages_a4(render_delivery_pdf(view))


def test_proforma_is_a4_on_letterhead():
    from core.proforma_invoice import render_proforma_pdf
    view = {
        "document_no": "PRF-2026-00001", "recipient_name": "Test", "recipient_org": "ACME GmbH",
        "recipient_phone": "0555", "customer_address": "Berlin Str. 1", "customer_country": "Germany",
        "currency": "EUR", "doc_lang": "EN", "date": "26.06.2026 12:00", "dispatched_by": "d",
        "items": [{"item_name": "Argan Oil", "quantity": 10, "unit": "adet",
                   "unit_price": 4.5, "weight_ml": 100}],
    }
    assert _all_pages_a4(render_proforma_pdf(view))


# ── Auto-fit politikası: %15'e kadar küçült → tek sayfa; fazlası → çok sayfa ──

def _proforma_view(n):
    return {
        "document_no": "PRF-FIT", "recipient_name": "Müşteri", "recipient_org": "ACME Trading GmbH",
        "recipient_phone": "+49 555", "customer_address": "Some address, Berlin",
        "customer_country": "Germany", "currency": "EUR", "doc_lang": "EN", "date": "26.06.2026",
        "items": [{"item_name": f"Minerva 108 Anti-Aging Serum variant {i}", "quantity": i + 1,
                   "unit": "adet", "unit_price": 12.5, "weight_ml": 50} for i in range(n)],
    }


def _full_scale_pages(view) -> int:
    """Hiç küçültmeden (scale=1.0) kaç sayfa olurdu."""
    from io import BytesIO
    from reportlab.platypus import SimpleDocTemplate
    from reportlab.lib.units import mm
    from core.proforma_invoice import _proforma_story
    from core.delivery_note import _A4_PT, _count_pages
    buf = BytesIO()
    SimpleDocTemplate(buf, pagesize=_A4_PT, leftMargin=20 * mm, rightMargin=20 * mm,
                      topMargin=48 * mm, bottomMargin=30 * mm).build(_proforma_story(view, 1.0))
    return _count_pages(buf.getvalue())


def test_autofit_absorbs_small_overflow_into_one_page():
    """Tam boyda yeni 2. sayfaya taşan ilk durum, %15 küçültmeyle tek sayfaya sığmalı."""
    from core.proforma_invoice import render_proforma_pdf
    from core.delivery_note import _count_pages
    n = next(k for k in range(8, 80) if _full_scale_pages(_proforma_view(k)) >= 2)
    assert _full_scale_pages(_proforma_view(n)) >= 2          # tam boyda taşıyor
    assert _count_pages(render_proforma_pdf(_proforma_view(n))) == 1   # ama küçülterek sığdı


def test_autofit_paginates_when_beyond_15pct():
    """Çok uzun içerik %15 bütçesini aşar → gerçekten çok sayfa, hepsi A4."""
    from core.proforma_invoice import render_proforma_pdf
    from core.delivery_note import _count_pages
    pdf = render_proforma_pdf(_proforma_view(120))
    assert _count_pages(pdf) >= 2
    assert _all_pages_a4(pdf)
