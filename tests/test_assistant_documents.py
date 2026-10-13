"""Minerva Asistan — Belge Oku + metin yazma modu (13.10.2026).

SDK tamamen sahte (tests/assistant_fakes.py), ağ YOK.  Kilitler:
  • Dosya: yalnız PDF / JPEG / PNG (sihirli bayt), ≤10 MB, PDF ≤20 sayfa,
    parolalı PDF red; fotoğraf EXIF yönüne çevrilir, uzun kenar ≤ IMAGE_MAX_EDGE,
    yeniden kodlanır (EXIF atılır).  Dosya SAKLANMAZ (yalnız sha256).
  • İstek: belge bloğu + `output_config.format` (JSON şeması, nullable YOK —
    API 16 union sınırı); format reddedilirse süreç bir kez şemasız moda geçer.
  • Model çıktısı veridir: `normalize_extraction` tip/aralık/satır sınırını
    kesinleştirir.
  • Eşleşme yalnız aktif panel; TR/EN eşanlamlı ad, SKU, g↔kg çevrimi (kg↔l yok),
    SKT okunamaz/geçmiş, retest ≠ SKT, lot sistemde var, toplam uyuşmazlığı.
  • HTTP: NDJSON, usage HER durumda yazılır (hata dahil), aynı belge yeniden
    okunmaz (`reused`), belge yalnız sahibine + aynı panelde; sohbete
    `document_id` ile `<belge>` bloğu; `mode:"write"` → bağlam notu + yüksek
    max_tokens.
  • check_product_text: Ürün İçerik Kartı motoru (INCI dışı iddia, yasaklı
    iddia, hacim); INCI yoksa yalnız uyarı.
"""
import datetime as dt
import io
import json

import bcrypt
import pytest
from PIL import Image

from core.assistant import documents as D
from core.assistant import history as H
from core.assistant import runner as R
from core.assistant.tools import REGISTRY, ToolContext, run_tool
from database import AppSetting, AssistantDocument, AssistantUsage, Inventory, Item, Supplier, User
from tests.assistant_fakes import FakeClient, bad_request, message, text_block, usage

_H = {"Origin": "http://testserver"}
DOCS = "/api/assistant/documents"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    R.reset_cache()
    H.reset_cache()
    D.reset_cache()
    yield
    R.reset_cache()
    H.reset_cache()
    D.reset_cache()


def _install(monkeypatch, script=None) -> FakeClient:
    fake = FakeClient(script)
    monkeypatch.setattr("core.assistant.client.get_client", lambda: fake)
    return fake


def _login(client, db, username, role="LabLead", permissions=None):
    if db.query(User).filter(User.username == username).first() is None:
        db.add(User(username=username, full_name=username.title(), role=role, is_active=True,
                    password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode(),
                    permissions=json.dumps(permissions) if permissions is not None else None))
        db.commit()
    client.post("/api/logout", headers=_H)
    r = client.post("/api/login", json={"username": username, "password": "minerva123"}, headers=_H)
    assert r.status_code == 200, r.text
    return db.query(User).filter(User.username == username).one()


def _pdf(pages=1, password=None) -> bytes:
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(width=595, height=842)
    if password:
        w.encrypt(password)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def _jpeg(size=(4000, 3000), orientation=None) -> bytes:
    img = Image.new("RGB", size, (200, 180, 150))
    buf = io.BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[0x0112] = orientation
        exif[0x010F] = "SecretCam GPS"
        img.save(buf, format="JPEG", exif=exif.tobytes())
    else:
        img.save(buf, format="JPEG")
    return buf.getvalue()


EXTRACTION = {
    "doc_type": "fatura", "supplier_name": "Belge Kimya Ltd. Şti.", "document_no": "FTR-2026-77",
    "document_date": "2026-10-10", "document_date_raw": "10.10.2026", "currency": "eur",
    "vat_included": "hayir", "vat_rate": "20", "subtotal": "1.330,00", "vat_amount": "266", "total": "1596",
    "lines": [
        {"material": "Cetearyl Alcohol", "material_code": "", "lot": "CA-2026-11", "production_date": "2026-09-01",
         "expiry_date": "2028-09-01", "expiry_raw": "01.09.2028", "retest_date": "", "quantity": "25000",
         "unit": "g", "package": "25 kg çuval", "unit_price": "4.20", "price_unit": "kg", "line_total": "105"},
        {"material": "Glycerin 99.5%", "material_code": "GLY-995", "lot": "GL-7", "production_date": "",
         "expiry_date": "", "expiry_raw": "", "retest_date": "2027-01-01", "quantity": "50", "unit": "l",
         "package": "", "unit_price": "", "price_unit": "", "line_total": "1125"},
        {"material": "Unknown Extract XYZ", "material_code": "", "lot": "U-1", "production_date": "",
         "expiry_date": "2020-01-01", "expiry_raw": "01.01.2020", "retest_date": "", "quantity": "1",
         "unit": "kg", "package": "", "unit_price": "", "price_unit": "", "line_total": ""},
    ],
    "coa_results": [], "warnings": ["Sayfa 2 bulanık."],
}


def _world(db):
    sup = Supplier(name="BELGE KİMYA", domain="cosmetics")
    db.add(sup)
    db.flush()
    ceto = Item(name="SETİL STEARİL ALKOL", category="Hammadde", unit="kg", current_stock=3, domain="cosmetics",
                is_active=True)
    gly = Item(name="GLİSERİN", sku="GLY-995", category="Hammadde", unit="kg", current_stock=8, domain="cosmetics",
               is_active=True)
    other = Item(name="SETİL STEARİL ALKOL", category="Hammadde", unit="kg", domain="supplement", is_active=True)
    db.add_all([ceto, gly, other])
    db.flush()
    db.add(Inventory(item_id=ceto.id, lot_number="CA-2026-11", quantity=3, status="APPROVED", domain="cosmetics",
                     expiry_date=None))
    db.commit()
    return {"sup": sup, "ceto": ceto, "gly": gly, "other": other}


# ─── Dosya hazırlığı ─────────────────────────────────────────────────────────

def test_prepare_file_types_limits_and_image_reencode():
    p = D.prepare_file(_pdf(3), "fatura.pdf")
    assert p.kind == "pdf" and p.pages == 3 and p.media_type == "application/pdf" and len(p.sha256) == 64
    for raw, code, status in [(b"", "empty", 400), (b"PK\x03\x04zipzipzip", "unsupported", 415),
                              (b"%PDF-" + b"0" * (D.MAX_BYTES + 1), "too_large", 413),
                              (_pdf(D.MAX_PAGES + 1), "too_many_pages", 400),
                              (_pdf(1, password="gizli"), "encrypted", 400),
                              (b"%PDF-1.7 bozuk", "unreadable", 400)]:
        with pytest.raises(D.DocError) as e:
            D.prepare_file(raw, "x")
        assert (e.value.code, e.value.status) == (code, status), code
    raw = _jpeg((4000, 3000), orientation=6)                       # 90° döndürülmüş telefon fotoğrafı
    p = D.prepare_file(raw, "coa.jpg")
    img = Image.open(io.BytesIO(p.data))
    assert p.kind == "image" and p.media_type == "image/jpeg" and p.pages is None
    assert max(img.size) <= D.IMAGE_MAX_EDGE and img.size[0] < img.size[1]   # EXIF yönü uygulandı
    assert "exif" not in img.info and b"SecretCam" not in p.data
    rgba = Image.new("RGBA", (100, 80), (0, 0, 0, 0))
    buf = io.BytesIO()
    rgba.save(buf, format="PNG")
    p = D.prepare_file(buf.getvalue(), "etiket.png")
    assert p.media_type == "image/jpeg" and Image.open(io.BytesIO(p.data)).mode == "RGB"
    assert D.clean_name("../../etc/passwd") == "passwd" and D.clean_name("") == "belge"


def test_schema_has_no_unions_and_every_object_is_strict():
    def walk(node):
        if isinstance(node, dict):
            assert "anyOf" not in node and not isinstance(node.get("type"), list)
            for bad in ("minimum", "maximum", "maxItems", "maxLength", "minLength"):
                assert bad not in node
            if node.get("type") == "object":
                assert node["additionalProperties"] is False and set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(D.SCHEMA)
    assert D.OUTPUT_FORMAT == {"type": "json_schema", "schema": D.SCHEMA}


def test_normalize_extraction_coerces_model_output():
    ex = D.normalize_extraction(dict(EXTRACTION, doc_type="kitap", lines=EXTRACTION["lines"] * 30,
                                     vat_rate="250", warnings=["x"] * 20))
    assert ex["doc_type"] == "diger" and ex["currency"] == "EUR" and ex["vat_included"] is False
    assert ex["vat_rate"] is None and ex["subtotal"] == 1330.0 and ex["total"] == 1596.0
    assert len(ex["lines"]) == D.LINES_MAX and len(ex["warnings"]) == 11 and "ilk 60" in ex["warnings"][-1]
    ln = ex["lines"][0]
    assert ln["quantity"] == 25000.0 and ln["unit_price"] == 4.2 and ln["expiry_date"] == "2028-09-01"
    assert ex["lines"][1]["expiry_date"] is None and ex["lines"][1]["retest_date"] == "2027-01-01"
    bad = D.normalize_extraction({"lines": [{"material": ""}, "x", {"material": "Ok", "expiry_date": "2026-02-30",
                                                                    "quantity": "abc"}]})
    assert len(bad["lines"]) == 1 and bad["lines"][0]["expiry_date"] is None and bad["lines"][0]["quantity"] is None
    assert D.parse_json_text('Tamam:\n```json\n{"doc_type": "coa"}\n```') == {"doc_type": "coa"}
    with pytest.raises(ValueError):
        D.parse_json_text("belge okunamadı")


def test_match_document_scope_units_expiry_lots_and_totals(db_session):
    w = _world(db_session)
    m = D.match_document(db_session, "cosmetics", D.normalize_extraction(EXTRACTION), today=dt.date(2026, 10, 13))
    assert m["supplier"]["match"]["id"] == w["sup"].id                      # "Belge Kimya Ltd. Şti." = BELGE KİMYA
    l0, l1, l2 = m["lines"]
    assert l0["item"]["id"] == w["ceto"].id                                  # Cetearyl Alcohol ↔ SETİL STEARİL ALKOL
    assert l0["qty_in_item_unit"] == 25.0 and "25.000 g = 25 kg" in l0["unit_note"]
    assert l0["expiry_iso"] == "2028-09-01" and l0["lot_exists"] is True
    assert any("zaten kayıtlı" in x for x in l0["warnings"])
    assert l1["item"]["id"] == w["gly"].id and l1["qty_in_item_unit"] is None       # SKU; l ↔ kg ÇEVRİLMEZ
    assert "Birim farklı" in l1["unit_note"] and any("retest" in x for x in l1["warnings"])
    assert l2["item"] is None and any("SKT geçmiş" in x for x in l2["warnings"])
    assert all(c["id"] != w["other"].id for ln in m["lines"] for c in ln["candidates"])   # başka panel yok
    assert any("uyuşmuyor" in x for x in m["warnings"]) and "Sayfa 2 bulanık." in m["warnings"]


def test_doc_key_folds_english_invoice_names_onto_turkish_cards():
    same = [("Glycerin 99.5% USP", "GLİSERİN"), ("Vegetable Glycerine", "Gliserin"), ("Stearic Acid", "STEARİK ASİT"),
            ("Sodium Benzoate", "SODYUM BENZOAT"), ("Decyl Glucoside", "DESİL GLUKOZİT"),
            ("Hyaluronic Acid", "HİYALÜRONİK ASİT"), ("Niacinamide", "NİASİNAMİD"), ("Potassium Sorbate", "POTASYUM SORBAT"),
            ("Caprylic/Capric Triglyceride", "KAPRİLİK KAPRİK TRİGLİSERİT"), ("Xanthan Gum", "KSANTAN GAM"),
            ("Shea Butter", "SHEA YAĞI"), ("Jojoba Oil 1 L", "JOJOBA YAĞI"), ("Vitamin E Acetate", "E VİTAMİNİ ASETAT")]
    for a, b in same:
        assert D.doc_key(a) == D.doc_key(b), (a, b)
    for a, b in [("Cetyl Alcohol", "SETİL STEARİL ALKOL"), ("Citric Acid Anhydrous", "SİTRİK ASİT"),
                 ("Lavender Essential Oil", "LAVANTA YAĞI")]:
        assert D.doc_key(a) != D.doc_key(b), (a, b)          # ayrı malzeme / ayrı tür: kesin eşleşme YOK


def test_match_document_prefers_the_suppliers_card_and_uses_lot_numbers(db_session):
    umay = Supplier(name="UMAY KİMYA", domain="cosmetics")
    natur = Supplier(name="NATURALYA", domain="cosmetics")
    db_session.add_all([umay, natur])
    db_session.flush()
    # Lab düzeni: aynı malzemenin her tedarikçisi ayrı kart
    gly_umay = Item(name="GLİSERİN", category="Hammadde", unit="kg", supplier_id=umay.id, domain="cosmetics", is_active=True)
    gly_nat = Item(name="GLYCERIN", category="Hammadde", unit="kg", supplier_id=natur.id, domain="cosmetics", is_active=True)
    lanette = Item(name="LANETTE O", category="Hammadde", unit="kg", domain="cosmetics", is_active=True)
    stearic = Item(name="STEARİK ASİT", category="Hammadde", unit="kg", domain="cosmetics", is_active=True)
    db_session.add_all([gly_umay, gly_nat, lanette, stearic])
    db_session.flush()
    db_session.add(Inventory(item_id=lanette.id, lot_number="LN-55", quantity=4, status="APPROVED", domain="cosmetics"))
    db_session.commit()
    ex = D.normalize_extraction({"supplier_name": "Naturalya", "lines": [
        {"material": "Glycerin 99.5% USP", "lot": "G-1", "quantity": "20", "unit": "kg"},
        {"material": "Emulsifying Wax NF", "lot": "LN-55", "quantity": "5", "unit": "kg"},
        {"material": "Stearic Acid", "lot": "LN-55", "quantity": "10000", "unit": "g"}]})
    l0, l1, l2 = D.match_document(db_session, "cosmetics", ex, today=dt.date(2026, 10, 13))["lines"]
    assert l0["item"]["id"] == gly_nat.id                     # eşit iki kart → belgedeki firmanın kartı
    assert {c["id"] for c in l0["candidates"]} >= {gly_nat.id, gly_umay.id}
    assert l1["item"]["id"] == lanette.id and l1["lot_exists"] is True     # ticari ad: kart lot no'dan
    assert any("aynı lot numarasının" in x for x in l1["warnings"])
    assert l2["item"]["id"] == stearic.id and l2["qty_in_item_unit"] == 10.0
    assert any("başka kartta kayıtlı: LANETTE O" in x for x in l2["warnings"])
    ex["supplier_name"] = "Bilinmeyen Firma"
    l0 = D.match_document(db_session, "cosmetics", ex, today=dt.date(2026, 10, 13))["lines"][0]
    assert l0["item"] is None and any("adaylardan seçin" in x for x in l0["warnings"])   # firma yok → sorar


# ─── Model çağrısı ───────────────────────────────────────────────────────────

def _settings():
    from core.assistant import settings as S
    return S.Settings()


@pytest.mark.asyncio
async def test_extract_request_shape_and_format_fallback():
    prepared = D.prepare_file(_pdf(1), "f.pdf")
    fake = FakeClient([message(text_block(json.dumps(EXTRACTION)), usage_obj=usage(input_tokens=3000, output=600))])
    usages = []
    ex = await D.extract(fake, _settings(), prepared, "f.pdf", usages)
    assert ex["document_no"] == "FTR-2026-77" and len(usages) == 1 and usages[0]["cost_usd"] > 0
    params = fake.calls[0]
    block = params["messages"][0]["content"][0]
    assert block["type"] == "document" and block["source"]["media_type"] == "application/pdf"
    assert params["output_config"]["format"] == D.OUTPUT_FORMAT and params["max_tokens"] >= D.DOC_MAX_TOKENS
    assert params["system"][0]["text"] == D.EXTRACTION_RULES and "tools" not in params
    assert "temperature" not in params and "tool_choice" not in params

    fake = FakeClient([bad_request("output_config.format: schema is too complex"),
                       message(text_block("```json\n" + json.dumps(EXTRACTION) + "\n```"))])
    usages = []
    ex = await D.extract(fake, _settings(), prepared, "f.pdf", usages)
    assert ex["supplier_name"] == "Belge Kimya Ltd. Şti." and "format" not in fake.calls[1]["output_config"]
    assert D._NO_FORMAT is True and len(usages) == 1

    fake = FakeClient([message(text_block("okuyamadım"))])
    usages = []
    with pytest.raises(D.DocError) as e:
        await D.extract(fake, _settings(), prepared, "f.pdf", usages)
    assert e.value.code == "unparseable" and len(usages) == 1                 # maliyet yine kayıtlı
    fake = FakeClient([message(text_block(""), stop_reason="refusal")])
    with pytest.raises(D.DocError) as e:
        await D.extract(fake, _settings(), prepared, "f.pdf", [])
    assert e.value.code == "refusal"


# ─── HTTP ────────────────────────────────────────────────────────────────────

def _post(client, raw, name="fatura.pdf"):
    with client.stream("POST", DOCS, files={"file": (name, raw, "application/octet-stream")}, headers=_H) as r:
        if r.status_code != 200:
            r.read()
            return r.status_code, json.loads(r.content or b"{}")
        return 200, [json.loads(x) for x in r.iter_lines() if x.strip()]


def test_documents_endpoint_reads_matches_persists_and_reuses(client, db_session, monkeypatch):
    db = db_session
    w = _world(db)
    me = _login(client, db, "belge_lab", "LabLead")
    fake = _install(monkeypatch, [message(text_block(json.dumps(EXTRACTION)), usage_obj=usage(output=700))])
    raw = _pdf(2)
    status, events = _post(client, raw)
    assert status == 200 and [e["t"] for e in events] == ["meta", "done"]
    assert events[0]["pages"] == 2 and events[0]["kind"] == "pdf"
    doc = events[1]["document"]
    assert events[1]["reused"] is False and doc["doc_type_label"] == "Fatura" and doc["document_no"] == "FTR-2026-77"
    assert doc["supplier"]["match"]["name"] == "BELGE KİMYA" and doc["lines"][0]["item"]["id"] == w["ceto"].id
    row = db.query(AssistantDocument).one()
    assert row.user_id == me.id and row.domain == "cosmetics" and row.pages == 2 and row.file_name == "fatura.pdf"
    assert row.sha256 and json.loads(row.extraction_json)["document_no"] == "FTR-2026-77"
    u = db.query(AssistantUsage).one()
    assert u.question_preview == "Belge: fatura.pdf" and json.loads(u.tool_calls_json)[0]["name"] == "belge_okuma"
    assert u.conversation_id is None and u.cost_usd > 0 and row.cost_usd == u.cost_usd

    status, events = _post(client, raw, "aynisi.pdf")                       # aynı içerik → yeniden okunmaz
    assert status == 200 and events[1]["reused"] is True and len(fake.calls) == 1
    assert db.query(AssistantUsage).count() == 1 and db.query(AssistantDocument).count() == 1

    lst = client.get(DOCS).json()
    assert [d["id"] for d in lst] == [row.id] and lst[0]["doc_type_label"] == "Fatura"
    assert client.get(f"{DOCS}/{row.id}").json()["lines"][1]["item"]["id"] == w["gly"].id
    client.cookies.set("active_domain", "supplement")
    assert client.get(f"{DOCS}/{row.id}").status_code == 404 and client.get(DOCS).json() == []
    client.cookies.set("active_domain", "cosmetics")
    _login(client, db, "belge_baska", "LabLead")
    assert client.get(f"{DOCS}/{row.id}").status_code == 404 and client.get(DOCS).json() == []


def test_documents_endpoint_errors_and_cost_on_failure(client, db_session, monkeypatch):
    db = db_session
    assert client.post(DOCS, files={"file": ("a.pdf", _pdf(1))}, headers=_H).status_code == 401
    _login(client, db, "belge_kapali", "LabTech", {"assistant": {"use": False}, "inventory": {"view": True}})
    assert client.post(DOCS, files={"file": ("a.pdf", _pdf(1))}, headers=_H).status_code == 403
    _login(client, db, "belge_hata", "LabLead")
    fake = _install(monkeypatch, [message(text_block("anlaşılmadı"))])
    assert _post(client, b"GIF89a....")[0] == 415
    status, events = _post(client, _pdf(1))
    assert status == 200 and events[-1]["t"] == "error" and events[-1]["code"] == "unparseable"
    assert db.query(AssistantDocument).count() == 0 and db.query(AssistantUsage).count() == 1   # maliyet yazıldı
    db.add(AppSetting(key="assistant.enabled", value="0"))
    db.commit()
    status, body = _post(client, _pdf(2))
    assert status == 409 and len(fake.calls) == 1


def test_chat_with_document_block_and_write_mode(client, db_session, monkeypatch):
    db = db_session
    _world(db)
    _login(client, db, "belge_soru", "LabLead")
    fake = _install(monkeypatch, [message(text_block(json.dumps(EXTRACTION))),
                                  message(text_block("Lot CA-2026-11 sistemde kayıtlı.")),
                                  message(text_block("Taslak — yayınlamadan önce kontrol edin.\nMetin…"))])
    _, events = _post(client, _pdf(1))
    doc_id = events[-1]["document"]["id"]
    with client.stream("POST", "/api/assistant/chat", json={"message": "Bu faturadaki lotlar var mı?",
                                                            "document_id": doc_id}, headers=_H) as r:
        assert r.status_code == 200
        evs = [json.loads(x) for x in r.iter_lines() if x.strip()]
    assert evs[-1]["t"] == "done"
    q = fake.calls[1]["messages"][-2]["content"][0]["text"]
    assert q.startswith('<belge dosya="fatura.pdf">') and "CA-2026-11" in q and q.endswith("Bu faturadaki lotlar var mı?")
    assert '"kart":"SETİL STEARİL ALKOL"' in q and "</belge>" in q
    ctx = fake.calls[1]["messages"][-1]["content"]
    assert "Mod: metin yazma" not in ctx and fake.calls[1]["max_tokens"] == 1500
    conv = client.get("/api/assistant/conversations").json()[0]
    shown = client.get(f"/api/assistant/conversations/{conv['id']}").json()["messages"][0]["text"]
    assert shown == "[Belge: fatura.pdf] Bu faturadaki lotlar var mı?"

    with client.stream("POST", "/api/assistant/chat", json={"message": "Amazon açıklaması yaz", "mode": "write"},
                       headers=_H) as r:
        assert r.status_code == 200
        [json.loads(x) for x in r.iter_lines() if x.strip()]
    assert fake.calls[2]["max_tokens"] == R.WRITE_MAX_TOKENS
    assert "Mod: metin yazma" in fake.calls[2]["messages"][-1]["content"]
    assert client.post("/api/assistant/chat", json={"message": "x", "mode": "kod"}, headers=_H).status_code == 422

    _login(client, db, "belge_yabanci", "LabLead")
    r = client.post("/api/assistant/chat", json={"message": "Bu ne?", "document_id": doc_id}, headers=_H)
    assert r.status_code == 404 and r.json()["code"] == "document_not_found"


# ─── check_product_text ──────────────────────────────────────────────────────

def test_check_product_text_tool(db_session):
    tool = REGISTRY["check_product_text"]
    assert tool.perms == (("assistant", "use"),) and tool.spec()["strict"] is True
    ctx = ToolContext.from_user(db_session.query(User).filter_by(username="meltem").one(), "cosmetics", db_session)
    res = run_tool(ctx, "check_product_text", {
        "text": "Lavanta özlü bu krem egzamayı tedavi eder.", "title": "Lavanta Krem 50 ml",
        "inci": "Aqua, Glycerin, Cetearyl Alcohol", "size": "100 ml", "lang": "tr"})
    p = json.loads(res.content)
    codes = {i["code"] for i in p["issues"]}
    assert p["ok"] is False and {"FORBIDDEN_CLAIM", "SIZE_MISMATCH", "TEXT_INGREDIENT_UNSUPPORTED"} <= codes
    assert res.summary.endswith("hata")
    res = run_tool(ctx, "check_product_text", {"text": "Yoğun nemlendirici krem.", "title": None, "inci": None,
                                               "size": None, "lang": None})
    p = json.loads(res.content)
    assert p["ok"] is True and [i["code"] for i in p["issues"]] == ["INCI_NOT_GIVEN"]
    assert run_tool(ctx, "check_product_text", {"text": "", "title": None, "inci": None, "size": None,
                                                "lang": None}).is_error
