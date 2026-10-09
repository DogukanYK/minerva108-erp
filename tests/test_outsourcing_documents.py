"""External PDF privacy, recipe quantities and usable physical labels."""
from copy import deepcopy
from io import BytesIO

import pytest
from pypdf import PdfReader
from reportlab.lib.units import mm

from core.outsourcing_documents import outsourcing_filename, render_packet_pdf


@pytest.fixture(autouse=True)
def clean_db():
    """Rendering is pure: these tests need no database setup or destructive reset."""
    yield


@pytest.fixture
def packet():
    return {
        "job_reference": "FS-17", "partner_reference": "F-2", "revision": 3,
        "packet_hash": "PRIVATE-HASH-NEVER-PRINT", "status": "approved", "approved": True,
        "product_name": "Ürün X", "quantity": 100, "unit": "adet", "label_language": "TR",
        "instructions": "A fazını karıştırın. B fazını soğukken ekleyin.",
        "materials": [
            {"code": "M-0042", "kind": "raw", "quantity": 20, "unit": "kg", "phase": "A",
             "parts": [{"phase": "A", "quantity": 12}, {"phase": "B", "quantity": 8}]},
            {"code": "M-0071", "kind": "packaging", "quantity": 100, "unit": "adet", "phase": "Dolum"},
        ],
        "containers": [
            {"container_uid": "FC-4ef8f3ba-40a4-42f4-8417-4af014cf01ba", "code": "M-0042",
             "external_lot": "EL-61", "quantity": 22, "unit": "kg", "expiry_date": "2028-06-30"},
            {"container_uid": "FC-bd28d42d-775c-4cd8-b1ce-c528d6d8dd23", "code": "M-0071",
             "external_lot": "EL-62", "quantity": 100, "unit": "adet", "expiry_date": ""},
        ],
        "shipments": [],
        "balance": [
            {"code": "M-0042", "unit": "kg", "dispatched": 22, "consumed": 18,
             "waste": 1, "returned": 2, "outstanding": 1},
            {"code": "M-0071", "unit": "adet", "dispatched": 100, "consumed": 100,
             "waste": 0, "returned": 0, "outstanding": 0},
        ],
    }


def _reader(data):
    assert data.startswith(b"%PDF-")
    return PdfReader(BytesIO(data))


def _text(data):
    return "\n".join(page.extract_text() for page in _reader(data).pages)


@pytest.mark.parametrize("document", ["sheet", "labels", "manifest", "report"])
def test_external_documents_ignore_private_fields_and_do_not_mutate_packet(packet, document):
    secret = "CONFIDENTIAL-NEVER-PRINT-6971"
    forbidden = {"item_name": secret, "supplier_name": secret, "source_lot_number": secret,
                 "item_id": secret, "internal_notes": secret, "cost": secret,
                 "meta": {"source": secret}, "private_snapshot": {"material": secret}}
    packet.update(forbidden)
    for key in ["materials", "containers", "balance"]:
        for row in packet[key]:
            row.update(forbidden)
    original = deepcopy(packet)
    result = render_packet_pdf(packet, document)
    reader = _reader(result)
    text = _text(result)
    assert secret not in text + str(reader.metadata)
    assert packet["packet_hash"] not in text + str(reader.metadata)
    assert "M-0042" in text
    assert packet == original
    assert outsourcing_filename(packet, document) == f"fason_{document}_r3.pdf"
    assert secret not in outsourcing_filename(packet, document)


def test_sheet_keeps_phase_targets_separate_from_dispatch_extra(packet):
    text = _text(render_packet_pdf(packet, "sheet"))
    assert "Onaylı reçete hedefleri" in text
    assert "Hedef üstü sevk miktarları" in text
    # Both target phases remain 12 and 8; the separately identified excess is 2.
    assert "A\n12\nkg" in text and "B\n8\nkg" in text
    assert "M-0042\n2\nkg" in text
    assert "22" not in text
    assert "B fazını soğukken ekleyin." in text


def test_manifest_uses_selected_shipment_actual_containers(packet):
    packet["shipments"] = [{"reference": "FS-17-S2", "dispatched_at": "2026-10-09 12:30",
                            "containers": [packet["containers"][1]]}]
    text = _text(render_packet_pdf(packet, "manifest"))
    assert "FS-17-S2" in text and "EL-62" in text
    assert packet["containers"][1]["container_uid"] in text.replace("\n", "")
    assert "EL-61" not in text and packet["containers"][0]["container_uid"] not in text.replace("\n", "")


def test_report_compares_actual_use_plus_waste_against_target(packet):
    text = _text(render_packet_pdf(packet, "report"))
    assert "Hedef farkı = kullanım + fire" in text
    assert "M-0042\nkg\n20\n22\n18\n1\n2\n1\n-1" in text
    assert "İade, teslim alınan fiziksel miktardır" in text


def test_labels_have_printable_physical_size_and_only_uid_in_qr(packet, monkeypatch):
    import core.outsourcing_documents as documents
    real_qr = documents.QrCodeWidget
    payloads = []

    def capture_payload(value, **kwargs):
        payloads.append(value)
        return real_qr(value, **kwargs)

    monkeypatch.setattr(documents, "QrCodeWidget", capture_payload)
    reader = _reader(render_packet_pdf(packet, "labels"))
    assert len(reader.pages) == 2
    assert payloads == [row["container_uid"] for row in packet["containers"]]
    for page, container in zip(reader.pages, packet["containers"]):
        assert float(page.mediabox.width) == pytest.approx(100 * mm, abs=.01)
        assert float(page.mediabox.height) == pytest.approx(70 * mm, abs=.01)
        text = page.extract_text()
        assert container["container_uid"] in text
        assert container["external_lot"] in text and container["code"] in text
        assert "FASON KAP ETİKETİ" in text


def test_long_manifest_paginates_on_a4_with_complete_trace_identifiers(packet):
    packet["containers"] = [dict(packet["containers"][0], container_uid=f"FC-TRACE-{i:03}",
                                  external_lot=f"EL-TRACE-{i:03}") for i in range(100)]
    reader = _reader(render_packet_pdf(packet, "manifest"))
    assert len(reader.pages) > 1
    for page in reader.pages:
        assert float(page.mediabox.width) == pytest.approx(210 * mm, abs=.01)
        assert float(page.mediabox.height) == pytest.approx(297 * mm, abs=.01)
    text = "\n".join(page.extract_text() for page in reader.pages)
    for container in packet["containers"]:
        assert container["container_uid"] in text and container["external_lot"] in text


def test_public_text_is_literal_and_cannot_inject_markup(packet):
    packet["instructions"] = '<b>Do not inject</b> & <img src="/private/file"/>'
    text = _text(render_packet_pdf(packet, "sheet"))
    assert packet["instructions"] in text


@pytest.mark.parametrize("document", ["sheet", "labels", "manifest", "report"])
def test_unapproved_packets_cannot_produce_external_documents(packet, document):
    packet["approved"] = False
    with pytest.raises(ValueError, match="tüm onaylar"):
        render_packet_pdf(packet, document)


def test_empty_label_packet_is_rejected(packet):
    packet["containers"] = []
    with pytest.raises(ValueError, match="en az bir kap"):
        render_packet_pdf(packet, "labels")


def test_invalid_document_type_is_rejected(packet):
    with pytest.raises(ValueError, match="Bilinmeyen"):
        render_packet_pdf(packet, "internal_recipe")
