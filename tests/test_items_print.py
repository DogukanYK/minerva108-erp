"""
Ürünler — sekme/filtre bazlı antetli PDF liste yazdırma (GET /api/items/print).

  • 4 sekme (hammadde/ambalaj/bitmis_urun/numune) → 200 + %PDF + tüm sayfalar A4
  • filtreler: pkg (ambalaj alt-tipi) + q (arama) → extract_text ile içerik doğrulanır
  • Maliyet sütunu finance-gated: SuperAdmin PDF'inde VAR, LabTech PDF'inde YOK
  • geçersiz sekme 400; auth'suz 401
"""
from io import BytesIO

from fastapi.testclient import TestClient
from pypdf import PdfReader

from api_main import app
from database import Item, Inventory, Supplier

_H = {"Origin": "http://testserver"}
A4_W, A4_H = 595.276, 841.89


def _pdf_ok(resp):
    assert resp.status_code == 200, resp.text
    assert resp.content[:4] == b"%PDF"
    pages = PdfReader(BytesIO(resp.content)).pages
    for pg in pages:
        b = pg.mediabox
        assert abs(float(b.width) - A4_W) < 2 and abs(float(b.height) - A4_H) < 2
    return "\n".join((pg.extract_text() or "") for pg in pages)


def _seed(db):
    sup = Supplier(name="Print Tedarikçi", is_active=True)
    db.add(sup); db.flush()
    ham = Item(name="Print Hammadde Argan", sku="pr-ham-1", category="Hammadde", unit="kg",
               current_stock=8, cost_price=12.5, supplier_id=sup.id, domain="cosmetics")
    amb1 = Item(name="Print Şişe 100ml", sku="pr-amb-1", category="Ambalaj", pkg_type="şişe",
                unit="adet", current_stock=50, domain="cosmetics")
    amb2 = Item(name="Print Etiket TR", sku="pr-amb-2", category="Ambalaj", pkg_type="etiket",
                language="TR", unit="adet", current_stock=200, domain="cosmetics")
    parent = Item(name="Print Cream", sku="pr-fin-p", category="Bitmiş Ürün", unit="",
                  name_tr="Print Krem", current_stock=0, domain="cosmetics")
    db.add_all([ham, amb1, amb2, parent]); db.flush()
    var = Item(name="Print Cream 100ml", sku="pr-fin-v", category="Bitmiş Ürün", unit="adet",
               parent_id=parent.id, variation_name="100ml", current_stock=12, domain="cosmetics")
    db.add(var); db.flush()
    inv = Inventory(item_id=ham.id, lot_number="PRN-LOT-1", quantity=0.5,
                    is_sample=True, status="APPROVED", location="Numune",
                    supplier_id=sup.id, domain="cosmetics")
    db.add(inv); db.commit()
    return ham, amb1, amb2, parent, var


def test_all_tabs_render_a4(authed_client: TestClient, db_session):
    _seed(db_session)
    for tab in ("hammadde", "ambalaj", "bitmis_urun", "numune"):
        text = _pdf_ok(authed_client.get(f"/api/items/print?tab={tab}"))
        assert "ÜRÜN LİSTESİ" in text


def test_pkg_and_search_filters(authed_client: TestClient, db_session):
    _seed(db_session)
    # ambalaj + etiket alt-tipi → şişe dışlanır
    text = _pdf_ok(authed_client.get("/api/items/print?tab=ambalaj&pkg=etiket"))
    assert "Print Etiket TR" in text and "Print Şişe 100ml" not in text
    # __none__ → pkg_type'sız (tohum verisinde yok → boş liste mesajı)
    text2 = _pdf_ok(authed_client.get("/api/items/print?tab=ambalaj&pkg=__none__"))
    assert "eşleşen kayıt yok" in text2 or "Print" not in text2
    # arama
    text3 = _pdf_ok(authed_client.get("/api/items/print?tab=hammadde&q=argan"))
    assert "Print Hammadde Argan" in text3


def test_finished_hierarchy_and_name_tr(authed_client: TestClient, db_session):
    _seed(db_session)
    text = _pdf_ok(authed_client.get("/api/items/print?tab=bitmis_urun"))
    assert "Print Cream" in text and "Print Krem" in text     # ana ürün + Türkçe ad
    assert "100ml" in text                                    # varyasyon


def test_samples_tab_lists_lot(authed_client: TestClient, db_session):
    _seed(db_session)
    text = _pdf_ok(authed_client.get("/api/items/print?tab=numune"))
    assert "PRN-LOT-1" in text and "Print Tedarikçi" in text


def test_finance_column_for_superadmin(authed_client: TestClient, db_session):
    _seed(db_session)
    text = _pdf_ok(authed_client.get("/api/items/print?tab=hammadde"))
    # pozitif kanıt: içerik gerçekten çıkarılıyor + Maliyet başlığı var
    assert "Print Hammadde Argan" in text
    assert "Maliyet" in text and "12" in text


def test_finance_column_hidden_for_labtech(labtech_client: TestClient, db_session):
    _seed(db_session)
    text = _pdf_ok(labtech_client.get("/api/items/print?tab=hammadde"))
    assert "Print Hammadde Argan" in text     # liste görünür (items.view var)
    assert "Maliyet" not in text              # ama maliyet sütunu YOK


def test_invalid_tab_and_auth(authed_client: TestClient):
    assert authed_client.get("/api/items/print?tab=bilinmeyen").status_code == 400
    pub = TestClient(app)
    assert pub.get("/api/items/print?tab=hammadde").status_code in (401, 403)
