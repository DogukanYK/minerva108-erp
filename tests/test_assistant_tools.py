"""Minerva Asistan — araç kataloğu ve işleyiciler (core/assistant/tools.py,
handlers.py, prompt.py, help_docs.py).

Sözleşmeler:
  • 15 araç, ada göre sıralı, her çağrıda byte-aynı; strict şema
    (additionalProperties False, tüm özellikler required, opsiyoneller null'lu).
  • TR katlama + eşanlamlı eşleşme ("stearil alkol" → STEARYL ALCOHOL / SETİL
    STEARİL ALKOL / CETEARYL ALCOHOL), SKU tam eşleşme önde, ≤8 + more.
  • Panel izolasyonu; finans alanı YALNIZ rol ∈ finans VE finance.view.
  • İzin reddi is_error + DB'ye dokunmaz; lot belirsizliği; raf etiketi;
    kategori sayımı + varsayılan TR ayı; kırpma bayrağı JSON'u bozmaz.
  • Rehber: önekte yalnız giriş + konu dizini (≤ GUIDE_MAX_CHARS, byte-sabit);
    konu metinleri search_help ile gelir (DB'siz, birinci konu kırpılmaz, her
    konu gerçekçi bir Türkçe soruyla bulunur).
"""
import copy
import dataclasses
import datetime as dt
import json
import pathlib

import bcrypt
import pytest

from core.assistant import handlers as H
from core.assistant import help_docs as HD
from core.assistant import prompt as P
from core.assistant import tools as T
from core.assistant.handlers import score_item
from core.assistant.tools import PROMPT_HASH, REGISTRY, TOOL_NAMES, ToolContext, run_tool, tool_specs
from core.permissions import PERMISSION_CATEGORIES
from database import (DriveFile, DriveFolder, Inventory, Item, MaterialGroup, MaterialSupplierPref, ProductionHistory,
                      Recipe, RecipeIngredient, RetentionSample, StockOrderFlag, Supplier, SupplierPrice,
                      User, tr_now)


# ─── Yardımcılar ─────────────────────────────────────────────────────────────

def _user(db, username, role, permissions=None):
    u = User(username=username, full_name=username.title() + " Test", role=role, is_active=True,
             password_hash=bcrypt.hashpw(b"minerva123", bcrypt.gensalt()).decode(),
             permissions=json.dumps(permissions) if permissions is not None else None)
    db.add(u)
    db.commit()
    return u


def _ctx(db, role="LabLead", permissions=None, domain="cosmetics", username=None):
    u = _user(db, username or f"u_{role.lower()}_{dt.datetime.utcnow().timestamp():.0f}", role, permissions)
    return ToolContext.from_user(u, domain, db)


def _card(db, name, category="Hammadde", unit="kg", stock=0.0, min_level=0.0, domain="cosmetics",
          sku=None, supplier_id=None, group_id=None, cost=0.0, name_tr=None, active=True):
    it = Item(name=name, name_tr=name_tr, category=category, unit=unit, current_stock=stock,
              min_stock_level=min_level, domain=domain, sku=sku, supplier_id=supplier_id,
              material_group_id=group_id, cost_price=cost, is_active=active)
    db.add(it)
    db.flush()
    return it


def _sup(db, name, domain="cosmetics", **kw):
    s = Supplier(name=name, domain=domain, **kw)
    db.add(s)
    db.flush()
    return s


def _lot(db, item, lot, qty, location="Depo", status="APPROVED", is_sample=False, expiry=None,
         supplier_id=None, domain=None):
    r = Inventory(item_id=item.id, lot_number=lot, quantity=qty, location=location, status=status,
                  is_sample=is_sample, expiry_date=expiry, supplier_id=supplier_id,
                  domain=domain or item.domain)
    db.add(r)
    db.flush()
    return r


def _run(ctx, name, args):
    res = run_tool(ctx, name, args)
    return res, json.loads(res.content)


# ─── Katalog ─────────────────────────────────────────────────────────────────

OPS_TOOLS = {"deliveries", "product_returns", "b2b_orders", "quotations", "qc_pending", "outsourcing_jobs",
             "stock_movements", "consumption", "month_end_stock", "production_capacity", "retention_samples",
             "sample_analyses", "trendyol_orders", "shopify_orders", "influencer_overview", "reviews_overview"}


def test_registry_tools_sorted_strict_and_stable():
    # 13.10.2026: CRM ve PDKS araçları KALDIRILDI (kullanıcı kararı), 16 operasyon aracı +
    # metin yazma modunun check_product_text'i eklendi
    assert len(TOOL_NAMES) == 30
    assert TOOL_NAMES == tuple(sorted(TOOL_NAMES))
    assert set(TOOL_NAMES) == {"find_item", "item_detail", "find_lot", "find_recipe", "recipe_detail",
                               "production_summary", "recent_productions", "find_supplier",
                               "supplier_detail", "stock_gaps", "expiring_lots", "drive_search",
                               "search_help", "check_product_text"} | OPS_TOOLS
    assert not {"my_pdks_today", "crm_search"} & set(TOOL_NAMES)
    assert not any(c in ("crm", "pdks") for n in TOOL_NAMES for c, _ in REGISTRY[n].perms)
    specs = tool_specs()
    assert [s["name"] for s in specs] == list(TOOL_NAMES)
    for s in specs:
        assert s["strict"] is True
        sch = s["input_schema"]
        assert sch["type"] == "object" and sch["additionalProperties"] is False
        assert set(sch["required"]) == set(sch["properties"]), s["name"]
        for key, prop in sch["properties"].items():
            if "anyOf" in prop:
                # boş bırakılabilir seçim listesi: string enum + null
                kinds = [b["type"] for b in prop["anyOf"]]
                assert kinds == ["string", "null"], (s["name"], key)
                assert prop["anyOf"][0]["enum"] and None not in prop["anyOf"][0]["enum"]
                continue
            t = prop["type"]
            assert isinstance(t, str) or (isinstance(t, list) and "null" in t), (s["name"], key)
            # API, liste tipi + enum birleşimini 400'le reddeder (13.10.2026 canlı)
            assert not ("enum" in prop and isinstance(t, list)), (s["name"], key)
        assert s["description"].strip()
    # byte-aynı ve dışarıya verilen kopya mutasyona kapalı
    a = json.dumps(tool_specs(), ensure_ascii=False)
    tool_specs()[0]["description"] = "BOZ"
    assert json.dumps(tool_specs(), ensure_ascii=False) == a == json.dumps(json.loads(T.tool_specs_json()),
                                                                           ensure_ascii=False)
    # Her aracın izni geçerli bir kategori/eylem çiftidir
    for n in TOOL_NAMES:
        assert REGISTRY[n].perms, n
        for cat, act in REGISTRY[n].perms:
            assert act in PERMISSION_CATEGORIES[cat], (n, cat, act)


DOCS_DIR = pathlib.Path(__file__).resolve().parent.parent / "docs" / "assistant"


def test_prompt_hash_guide_block_limits_and_system_blocks():
    assert len(PROMPT_HASH) == 12 and int(PROMPT_HASH, 16) >= 0
    # İki blok: [kurallar, rehber]; breakpoint YALNIZ son blokta
    assert len(P.SYSTEM_BLOCKS) == 2 and P.SYSTEM_BLOCKS[0]["text"] == P.SYSTEM_RULES
    assert P.SYSTEM_BLOCKS[-1]["cache_control"] == {"type": "ephemeral", "ttl": "1h"}
    assert "cache_control" not in P.SYSTEM_BLOCKS[0]
    guide = P.SYSTEM_BLOCKS[-1]["text"]
    assert guide == P.GUIDE_TEXT and guide.startswith("<rehber>\n") and guide.endswith("\n</rehber>")
    assert len(guide) <= P.GUIDE_MAX_CHARS, len(guide)
    b5 = P.system_blocks("5m")
    assert b5[-1]["cache_control"]["ttl"] == "5m" and P.SYSTEM_BLOCKS[-1]["cache_control"]["ttl"] == "1h"
    assert P.system_blocks("bogus")[-1]["cache_control"]["ttl"] == "1h"
    assert P.system_blocks()[0] is not P.SYSTEM_BLOCKS[0]
    # Giriş (menü haritası + panel değiştirme) önekte, tam metin
    assert P.HELP_INTRO is not None and P.HELP_INTRO.id == "giris"
    assert P.HELP_INTRO.body in guide
    assert "Dashboard → /" in guide and "Panel değiştir" in guide
    # Konu dizini: docs 01..18'in HEPSİ, dosya sırasıyla, satır başına "- id: başlık"
    stems = sorted(f.stem for f in DOCS_DIR.glob("*.md") if not f.name.startswith("00_"))
    assert [d.filename for d in P.HELP_TOPICS] == [f"{st}.md" for st in stems]
    assert [d.id for d in P.HELP_TOPICS] == [HD.doc_id(st) for st in stems]
    assert len(P.HELP_TOPICS) == 18 and P.HELP_TOPICS[1].id == "lot_tasima"
    lines = [ln for ln in guide.splitlines() if ln.startswith("- ")]
    assert lines == [f"- {d.id}: {d.title}" for d in P.HELP_TOPICS]
    for d in P.HELP_TOPICS:
        # konu metni önekte DEĞİL (search_help'te); tek konu araç sonucuna sığar
        assert d.body and d.body[:80] not in guide, d.id
        assert d.yetki and d.sayfa, d.id
        assert len(d.text) <= P.HELP_DOC_MAX_CHARS, (d.id, len(d.text))
    # Sınırdaki bir konu (tırnak + satır sonu kaçışlarıyla) tek başına araç sonucuna sığar
    body = ('1. "Kaydet"e bas; “Sil” değil.\n' * 200)[:P.HELP_DOC_MAX_CHARS - 40]
    big = HD.HelpDoc(id="sinir_konu", filename="99_sinir_konu.md", title="Sınır konu", yetki="a.b",
                     sayfa="/x", body=body)
    assert len(big.text) <= P.HELP_DOC_MAX_CHARS
    r = HD.search((big,) + P.HELP_TOPICS, "sinir_konu", limit=T.MAX_RESULT_CHARS)
    assert r["topics"][0]["text"] == big.text and len(T.compact_json(r)) <= T.MAX_RESULT_CHARS
    # Kurallar SSS'yi değil rehber + search_help akışını anlatır
    assert "SSS" not in P.SYSTEM_RULES and "<sss>" not in guide
    assert P.SYSTEM_RULES.count("search_help") >= 2
    assert '"bu konuda rehber yok, yöneticinize sorun"' in P.SYSTEM_RULES


def test_guide_block_is_byte_stable_and_context_free():
    """İki bağımsız kurulum byte-aynı; tarih/kullanıcı/panel metni yok."""
    intro, topics = P.load_help()
    assert P.guide_block_text(intro, topics) == P.GUIDE_TEXT
    assert P.guide_block_text(*P.load_help(DOCS_DIR)) == P.GUIDE_TEXT
    assert json.dumps(P.system_blocks(), ensure_ascii=False) == json.dumps(P.system_blocks(), ensure_ascii=False)
    today = dt.datetime.utcnow() + dt.timedelta(hours=3)
    for bad in (today.strftime("%d.%m.%Y"), today.strftime("%Y-%m-%d"), "Songül", "Kozmetik",
                "Food Supplement", "Lab Sorumlusu"):
        assert bad not in P.GUIDE_TEXT, bad
    # PROMPT_HASH rehber bloğunu kapsar
    assert P.GUIDE_TEXT in P.system_text()


def test_load_help_missing_dir_and_parsing(tmp_path):
    assert P.load_help(tmp_path / "yok") == (None, ())
    assert P.guide_block_text(None, ()) == "<rehber>\n\n</rehber>"
    (tmp_path / "00_giris.md").write_text("# Giriş\nYetki: assistant.use\n\nMenü haritası.\n", encoding="utf-8")
    (tmp_path / "02_ikinci-konu.md").write_text("# İkinci\nYetki: a.b\nSayfa: /x\n\n1. Adım.\n", encoding="utf-8")
    (tmp_path / "01_birinci.md").write_text("Başlıksız gövde.\n", encoding="utf-8")
    (tmp_path / "03_bos.md").write_text("  \n", encoding="utf-8")
    intro, topics = P.load_help(tmp_path)
    assert intro.title == "Giriş" and intro.text == "Yetki: assistant.use\n\nMenü haritası."
    assert [(d.id, d.title, d.yetki, d.sayfa) for d in topics] == [
        ("birinci", "01_birinci", "", ""), ("ikinci_konu", "İkinci", "a.b", "/x")]
    assert topics[1].text == "Yetki: a.b\nSayfa: /x\n\n1. Adım."
    g = P.guide_block_text(intro, topics)
    assert g.endswith("- birinci: 01_birinci\n- ikinci_konu: İkinci\n</rehber>") and "Menü haritası." in g
    assert "1. Adım." not in g


def test_help_fold_matches_supplier_prices_normalize():
    from core.supplier_prices import normalize
    for s in ("Şahit NUMUNE dolabı", "İzİn  ıIğüşöç ÇĞÖŞÜ", "  SKT  düzelt ", "", None, "Lot-Taşıma"):
        assert HD.fold(s) == normalize(s), s
    assert HD.doc_id("02_lot_tasima") == "lot_tasima" and HD.doc_id("Lot Taşıma") == "lot_tasima"


# ─── search_help ─────────────────────────────────────────────────────────────

def _help(q):
    """İşleyici doğrudan — ctx ve db KULLANILMAZ (None)."""
    return H.search_help(None, None, {"q": q})


#: Lab'ın gerçekçi soruları → beklenen ilk konu (her konu en az bir kez).
HELP_CASES = {
    "numune stoğa nasıl çevrilir": "numune_stoga_cevir",
    "alternatif tedarikçi numunesini stoğa çevir": "numune_stoga_cevir",
    "lotu başka karta taşı": "lot_tasima",
    "yanlış karta girilen lotu nasıl taşırım": "lot_tasima",
    "üretimi iptal": "uretim_iptali",
    "yanlış üretimi nasıl iptal ederim": "uretim_iptali",
    "skt düzelt": "skt_duzeltme",
    "son kullanma tarihi yanlış girilmiş lot": "skt_duzeltme",
    "rapor bildirimi": "pdks_izin_rapor_bildirimi",
    "izin nasıl girilir": "pdks_izin_rapor_bildirimi",
    "giriş yapmayı unuttum": "pdks_izin_rapor_bildirimi",
    "qc onay": "qc_onayi",
    "karantinadaki lotu onaylama": "qc_onayi",
    "aynı malzeme": "ayni_malzeme_gruplari",
    "kartları aynı malzeme olarak grupla": "ayni_malzeme_gruplari",
    "tedarikçi fiyat": "tedarikci_tercihleri_ve_fiyat",
    "tedarikçiyi bitirilecek yap": "tedarikci_tercihleri_ve_fiyat",
    "fiyatı olmayan malzemeleri nerede görürüm": "tedarikci_tercihleri_ve_fiyat",
    "plandaki maliyet sekmesi": "satin_alma_plani",
    "satın alma planı": "satin_alma_plani",
    "alım listesi excel indir": "satin_alma_plani",
    "şahit numune": "sahit_numune_dolabi",
    "şahit numune imha": "sahit_numune_dolabi",
    "mal kabul": "mal_kabul",
    "faturayı okut mal kabule aktar": "belge_oku_metin_yaz",
    "amazon ürün açıklaması yaz": "belge_oku_metin_yaz",
    "hammadde girişi nasıl yapılır": "mal_kabul",
    "teslimat": "teslimat_iade",
    "müşteriden gelen iade": "teslimat_iade",
    "kargo ile gönderim": "teslimat_iade",
    "drive": "drive",
    "dosya paylaşım linki oluştur": "drive",
    "üretim başlat": "uretim_baslatma",
    "yeni üretim nasıl başlatılır": "uretim_baslatma",
    "fason": "fason_uretim",
    "dış üretime malzeme gönder": "fason_uretim",
    "b2b sipariş": "b2b_siparis",
    "teklifi siparişe dönüştür": "b2b_siparis",
    "hangi iş hangi sayfada": "genel_navigasyon",
    "stoğu elle nasıl düzeltirim": "genel_navigasyon",
    "stok düzeltme nasıl yapılır": "genel_navigasyon",      # SKT/numune başlığına kaymasın
    "elle stok düzeltme": "genel_navigasyon",
    "yeni ürün kartı nasıl açılır": "genel_navigasyon",
    "üretim nasıl yapılır": "uretim_baslatma",              # eşit skorda iptal öne geçmesin
    "üretimi geri al": "uretim_iptali",                     # "geri al" → "iptal" eşanlamı
    "müşteriye ürün gönderme": "teslimat_iade",             # rehber "Alıcı"/"Kargo" diyor
}


def test_help_cases_cover_every_doc():
    assert set(HELP_CASES.values()) == {d.id for d in P.HELP_TOPICS}


@pytest.mark.parametrize("q,want", sorted(HELP_CASES.items()))
def test_search_help_turkish_queries(q, want):
    p = _help(q)
    assert p["topics"], (q, p.get("others"))
    assert p["topics"][0]["id"] == want, (q, [t["id"] for t in p["topics"]])
    doc = next(d for d in P.HELP_TOPICS if d.id == want)
    assert p["topics"][0]["text"] == doc.text and p["topics"][0]["title"] == doc.title
    assert len(p["topics"]) <= HD.TOPICS_MAX and len(p["others"]) <= HD.OTHERS_MAX
    ids = [t["id"] for t in p["topics"]] + [o["id"] for o in p["others"]]
    assert len(ids) == len(set(ids))
    assert len(T.compact_json(p)) <= T.MAX_RESULT_CHARS and "truncated" not in p
    assert p["query"] == q


@pytest.mark.parametrize("q", ["lot_tasima", "lot-tasima", "Lot Taşıma", "02_lot_tasima", "LOT_TAŞIMA.md"])
def test_search_help_exact_id_is_single_topic(q):
    p = _help(q)
    assert [t["id"] for t in p["topics"]] == ["lot_tasima"]
    assert "lot_tasima" not in [o["id"] for o in p["others"]]


def test_search_help_every_doc_fits_untruncated():
    for d in P.HELP_TOPICS:
        p = _help(d.id)
        assert p["topics"] == [{"id": d.id, "title": d.title, "text": d.text}], d.id
        assert len(T.compact_json(p)) <= T.MAX_RESULT_CHARS, d.id


def test_search_help_second_topic_yields_to_limit(monkeypatch):
    q = "numuneyi stoka ekle"
    p = _help(q)
    assert len(p["topics"]) == 2 and p["topics"][0]["id"] == "numune_stoga_cevir"
    one = len(T.compact_json({"query": q, "topics": p["topics"][:1], "others": []}))
    monkeypatch.setattr(T, "MAX_RESULT_CHARS", one + 10)
    small = _help(q)
    assert [t["id"] for t in small["topics"]] == ["numune_stoga_cevir"]
    assert small["topics"][0]["text"] == p["topics"][0]["text"]          # birinci konu kırpılmaz
    assert len(T.compact_json(small)) <= one + 10 and "truncated" not in small


def test_search_help_no_match_weak_match_and_short_query():
    p = _help("uzay gemisi rotası")
    assert p == {"query": "uzay gemisi rotası", "topics": [], "others": [], "note": "rehberde bu konu yok"}
    weak = _help("fatura kes")                      # tek seyrek gövde eşleşmesi → konu değil
    assert weak["topics"] == [] and weak["note"] == "rehberde bu konu yok"
    # Belge Oku rehberi de "fatura"yı anar (fatura okutma) — ikisi de yalnız aday
    assert [o["id"] for o in weak["others"]] == ["belge_oku_metin_yaz", "teslimat_iade"]
    with pytest.raises(H.ToolError):
        _help(" a ")
    assert HD.query_tokens("nasıl yapılır") == []


def test_search_help_via_run_tool_never_touches_db(db_session, monkeypatch):
    ctx = _ctx(db_session, "Staff")                 # varsayılan Staff: assistant.use açık
    denied = _ctx(db_session, "Staff", {"items": {"view": True}}, username="no_assistant")

    class _NoDB:
        def close(self):
            pass

        def __getattr__(self, name):
            raise AssertionError(f"search_help DB'ye dokunmamalı ({name})")
    monkeypatch.setattr(T, "SessionLocal", _NoDB)
    res, p = _run(ctx, "search_help", {"q": "lot_tasima"})
    assert not res.is_error and p["topics"][0]["id"] == "lot_tasima"
    assert res.summary == "Rehber: Lotu başka karta taşıma" and res.links == []
    res, p = _run(ctx, "search_help", {"q": "uzay gemisi"})
    assert not res.is_error and res.summary == "Rehber: konu yok" and p["topics"] == []
    res, p = _run(ctx, "search_help", {"q": "x"})
    assert res.is_error and "2 karakter" in p["error"]
    res, p = _run(denied, "search_help", {"q": "lot_tasima"})
    assert res.is_error and p == {"error": "yetkiniz yok", "permission": "assistant.use"}
    assert "search_help" in T.allowed_tools(ctx) and ("search_help", "assistant.use") in T.denied_tools(denied)


def test_score_item_pure_function():
    from core.material_groups import material_key
    from core.supplier_prices import normalize
    q, qk = normalize("stearil alkol"), material_key("stearil alkol")
    assert score_item("abc-1", qk, "zeta", "", "abc-1", "", ()) == 100.0
    sub = score_item(q, qk, normalize("SETİL STEARİL ALKOL"), "", "", "", material_key("SETİL STEARİL ALKOL"))
    assert 80.0 <= sub <= 81.0
    syn = score_item(q, qk, normalize("STEARYL ALCOHOL"), "", "", "", material_key("STEARYL ALCOHOL"))
    assert syn == 60.0
    part = score_item(q, qk, normalize("CETEARYL ALCOHOL"), "", "", "", material_key("CETEARYL ALCOHOL"))
    assert 39.0 < part < 41.0
    assert score_item(q, qk, normalize("JOJOBA YAĞI"), "", "", "", material_key("JOJOBA YAĞI")) == 0.0
    typo = score_item(normalize("jojoba yagi"), material_key("jojoba yagi"), normalize("JOJOBA YAĞİ"), "", "", "", ())
    assert typo >= 40.0


# ─── find_item / item_detail ─────────────────────────────────────────────────

def test_find_item_tr_fold_synonyms_group_and_domain(db_session):
    db = db_session
    ctx = _ctx(db, "Staff")
    g = MaterialGroup(name="Setil stearil alkol", domain="cosmetics")
    db.add(g)
    db.flush()
    sup = _sup(db, "YİĞİTOĞLU KİMYA")
    a = _card(db, "STEARYL ALCOHOL", supplier_id=sup.id, group_id=g.id)
    b = _card(db, "SETİL STEARİL ALKOL", group_id=g.id, stock=3944)
    c = _card(db, "CETEARYL ALCOHOL", unit="adet")
    jojoba = _card(db, "JOJOBA YAĞI")
    other = _card(db, "STEARYL ALCOHOL", domain="supplement")
    passive = _card(db, "STEARİL ALKOL ESKİ", active=False)
    db.commit()
    res, p = _run(ctx, "find_item", {"q": "stearil alkol", "category": None})
    assert not res.is_error
    ids = [i["id"] for i in p["items"]]
    assert {a.id, b.id, c.id} <= set(ids)
    assert jojoba.id not in ids and other.id not in ids and passive.id not in ids
    assert ids[0] == b.id                       # alt-dizgi (80) eşanlamlıdan (60) önde
    by_id = {i["id"]: i for i in p["items"]}
    assert by_id[a.id]["supplier"] == "YİĞİTOĞLU KİMYA" and by_id[a.id]["group"] == "Setil stearil alkol"
    assert by_id[b.id]["stock"] == 3944 and by_id[c.id]["unit"] == "adet"
    assert p["link"].startswith("/items?q=") and by_id[a.id]["link"].startswith("/items?q=")
    assert res.links == [{"label": "Ürünler", "href": p["link"]}] and res.summary == "3 kart"
    assert p["more"] is False and p["count"] == 3


def test_find_item_sku_exact_limit_category_and_arg_cleaning(db_session):
    db = db_session
    ctx = _ctx(db, "LabTech")
    zeta = _card(db, "Zeta Malzeme", sku="ABC-1")
    cards = [_card(db, f"Test Krem {i}") for i in range(9)]
    amb = _card(db, "Test Krem Kapak", category="Ambalaj")
    db.commit()
    _, p = _run(ctx, "find_item", {"q": "abc-1", "category": "all"})
    assert p["items"][0]["id"] == zeta.id
    _, p = _run(ctx, "find_item", {"q": "test krem", "category": None, "bogus": 1})
    assert len(p["items"]) == 8 and p["count"] == 10 and p["more"] is True
    _, p = _run(ctx, "find_item", {"q": "test krem", "category": "Ambalaj"})
    assert [i["id"] for i in p["items"]] == [amb.id]
    _, p = _run(ctx, "find_item", {"q": "test krem", "category": "Hammadde"})
    assert amb.id not in [i["id"] for i in p["items"]] and p["count"] == 9
    res, p = _run(ctx, "find_item", {"q": "   "})
    assert res.is_error and "boş" in p["error"]
    res, _ = _run(ctx, "find_item", "not-a-dict")
    assert res.is_error


def test_item_detail_lots_locations_sample_group_order_and_finance(db_session):
    db = db_session
    g = MaterialGroup(name="Jojoba", domain="cosmetics")
    db.add(g)
    db.flush()
    sup = _sup(db, "Naturalya")
    a = _card(db, "JOJOBA YAĞI", stock=15, min_level=5, group_id=g.id, cost=12.5)
    b = _card(db, "JOJOBA OIL — PERA", stock=4, group_id=g.id)
    _lot(db, a, "L1", 10, location="Depo", supplier_id=sup.id, expiry="2027-01-01")
    _lot(db, a, "L2", 5, location="Showroom")
    _lot(db, a, "N1", 2, location="Depo", is_sample=True)
    db.add(StockOrderFlag(item_id=a.id, supplier_id=sup.id, quantity=25, unit="kg",
                          expected_date=dt.date(2026, 11, 1), domain="cosmetics"))
    db.commit()

    lab = _ctx(db, "LabLead", username="lab_fin")
    res, p = _run(lab, "item_detail", {"item_id": a.id})
    assert not res.is_error
    assert p["current_stock"] == 15 and p["lot_total"] == 15 and p["sample_total"] == 2
    assert p["by_location"][0] == {"location": "Depo", "total": 12}
    assert {l["lot"] for l in p["lots"]} == {"L1", "L2", "N1"}
    assert [l for l in p["lots"] if l["lot"] == "N1"][0]["is_sample"] is True
    assert [l for l in p["lots"] if l["lot"] == "L1"][0]["supplier"] == "Naturalya"
    assert p["group"]["name"] == "Jojoba" and [m["id"] for m in p["group"]["members"]] == [b.id]
    assert p["open_order"]["qty"] == 25 and p["open_order"]["expected_date"] == "2026-11-01"
    assert "cost_price" not in p                       # LabLead → finans yok
    assert res.summary == "JOJOBA YAĞI: 15 kg"

    # finance.view override'ı olan LabLead: rol finans değil → yine yok
    lab2 = _ctx(db, "LabLead", {"finance": {"view": True}, "inventory": {"view": True}}, username="lab_ovr")
    assert "cost_price" not in _run(lab2, "item_detail", {"item_id": a.id})[1]
    # Manager ama finance.view kapalı → yok
    mgr_off = _ctx(db, "Manager", {"finance": {"view": False}, "inventory": {"view": True}}, username="mgr_off")
    assert "cost_price" not in _run(mgr_off, "item_detail", {"item_id": a.id})[1]
    # Manager varsayılan (finance.view True) → var
    mgr = _ctx(db, "Manager", username="mgr_on")
    assert mgr.can_finance and _run(mgr, "item_detail", {"item_id": a.id})[1]["cost_price"] == 12.5
    sa = ToolContext.from_user(db.query(User).filter_by(username="dogukan").one(), "cosmetics", db)
    assert _run(sa, "item_detail", {"item_id": a.id})[1]["cost_price"] == 12.5
    # Panel dışı kart → is_error
    res, p = _run(_ctx(db, "Staff", domain="supplement", username="supp"), "item_detail", {"item_id": a.id})
    assert res.is_error and "panelde" in p["error"]


# ─── İzin / hata yolu ────────────────────────────────────────────────────────

def test_permission_denied_is_error_and_never_opens_db(db_session, monkeypatch):
    db = db_session
    ctx = _ctx(db, "Staff", {"items": {"view": True}})           # b2b.view yok

    def _boom():
        raise AssertionError("yetkisiz çağrı DB açmamalı")
    monkeypatch.setattr(T, "SessionLocal", _boom)
    res, p = _run(ctx, "quotations", {"status": None, "q": "minerva"})
    assert res.is_error and p == {"error": "yetkiniz yok", "permission": "b2b.view"}
    assert res.summary == "Yetki yok" and res.links == []
    res, p = _run(ctx, "stock_gaps", {"status": None, "q": None})
    assert res.is_error and p["permission"] == "reports.view"
    res, p = _run(ctx, "yok_boyle_arac", {})
    assert res.is_error and p["error"] == "bilinmeyen araç"
    # items.view Drive kuralının altı izninden biri → drive_search de açık
    assert T.allowed_tools(ctx) == ["drive_search", "find_item", "find_supplier", "item_detail", "supplier_detail"]
    assert ("quotations", "b2b.view") in T.denied_tools(ctx)


def test_handler_exception_and_tool_error(db_session, monkeypatch):
    db = db_session
    ctx = _ctx(db, "Staff")

    def _broken(ctx, db, args):
        raise RuntimeError("patladı")
    monkeypatch.setitem(T.REGISTRY, "find_item", dataclasses.replace(REGISTRY["find_item"], handler=_broken))
    res, p = _run(ctx, "find_item", {"q": "x"})
    assert res.is_error and p == {"error": "araç hatası"} and res.summary == "Araç hatası"


# ─── find_lot ────────────────────────────────────────────────────────────────

def test_find_lot_ambiguous_retention_label_and_not_found(db_session):
    db = db_session
    ctx = _ctx(db, "LabTech")
    p1 = _card(db, "Minerva 108 Lavanta Şampuan", category="Bitmiş Ürün", unit="adet")
    p2 = _card(db, "Serenida Yüz Kremi", category="Bitmiş Ürün", unit="adet")
    p3 = _card(db, "Supp Ürün", category="Bitmiş Ürün", domain="supplement")
    _lot(db, p1, "SR005", 90, location="Showroom")
    _lot(db, p1, "SR005-S", 3, location="Şahit Numune Dolabı — Minerva 108")
    _lot(db, p2, "SR005", 40, location="Showroom")
    _lot(db, p3, "SR005", 7, domain="supplement")
    db.add(ProductionHistory(recipe_name="Lavanta Şampuan", target_item_id=p1.id, target_item_name=p1.name,
                             produced_quantity=93, lot_number="SR005", produced_by="songul", domain="cosmetics"))
    db.add(RetentionSample(item_id=p1.id, item_name=p1.name, lot_number="SR005-S", brand="Minerva 108",
                           shelf="2", slot="B", quantity=3, initial_quantity=3, unit="adet",
                           retention_until=dt.date(2028, 1, 1), domain="cosmetics"))
    db.add(RetentionSample(item_id=p2.id, item_name=p2.name, lot_number="SR005-S", brand="Serenida",
                           quantity=2, initial_quantity=2, unit="adet", domain="cosmetics"))
    db.commit()
    res, p = _run(ctx, "find_lot", {"lot_number": " sr 005 "})
    assert not res.is_error and p["lot"] == "SR005" and p["ambiguous"] is True
    assert [x["item_name"] for x in p["products"]] == [p1.name, p2.name]      # supplement görünmez
    one = p["products"][0]
    assert one["production"]["qty"] == 93 and one["production"]["cancelled"] is False
    assert {i["lot"] for i in one["inventory"]} == {"SR005", "SR005-S"}
    assert one["retention"][0]["cabinet"] == "Minerva 108"
    assert one["retention"][0]["shelf_slot_label"] == "Raf 2 · Göz B"
    assert one["retention"][0]["retention_until"] == "2028-01-01"
    two = p["products"][1]
    assert two["production"] is None and two["retention"][0]["shelf_slot_label"] == "raf bilgisi girilmemiş"
    assert p["link"] == "/traceability?lot=SR005" and res.summary == "2 ürün (belirsiz)"
    res, p = _run(ctx, "find_lot", {"lot_number": "NOPE1"})
    assert res.is_error and "Lot bulunamadı" in p["error"]
    res, _ = _run(ctx, "find_lot", {"lot_number": ""})
    assert res.is_error


# ─── Reçete ──────────────────────────────────────────────────────────────────

def test_find_recipe_and_recipe_detail_finance_gate(db_session):
    db = db_session
    target = _card(db, "Minerva 108 Lavanta Şampuan", category="Bitmiş Ürün", unit="adet")
    water = _card(db, "DİSTİLE SU", unit="kg", stock=500, cost=0.1)
    lav = _card(db, "LAVANTA UÇUCU YAĞI", unit="g", stock=200, cost=2.0)
    r = Recipe(name="Lavanta Şampuan", target_item_id=target.id, output_quantity=100, output_unit="adet",
               waste_percentage=5, production_notes="A fazını 70°C'ye ısıt. " * 100, domain="cosmetics")
    db.add(r)
    db.flush()
    db.add_all([RecipeIngredient(recipe_id=r.id, item_id=water.id, quantity=80, unit="kg", phase="A"),
                RecipeIngredient(recipe_id=r.id, item_id=lav.id, quantity=0.5, unit="g", phase="B")])
    db.add(Recipe(name="Gizli Supp", output_quantity=1, domain="supplement"))
    db.commit()

    lab = _ctx(db, "LabLead", username="lab_rec")
    _, p = _run(lab, "find_recipe", {"q": "lavanta"})
    assert [x["id"] for x in p["recipes"]] == [r.id] and p["recipes"][0]["ingredients_count"] == 2
    assert p["recipes"][0]["target_item"] == target.name
    _, p = _run(lab, "find_recipe", {"q": "şampuan"})      # hedef ürün adından da bulunur
    assert [x["id"] for x in p["recipes"]] == [r.id]
    _, p = _run(lab, "find_recipe", {"q": "gizli"})
    assert p["recipes"] == []
    res, p = _run(lab, "recipe_detail", {"recipe_id": r.id})
    assert not res.is_error and p["expected_yield"] == 100 and p["waste_pct"] == 5
    assert [i["phase"] for i in p["ingredients"]] == ["A", "B"]
    assert p["ingredients"][0]["stock"] == 500 and p["ingredients"][1]["qty"] == 0.5
    assert len(p["production_notes"]) == 1500 and p["notes_truncated"] is True
    assert "total_cost" not in p and all("cost_price" not in i for i in p["ingredients"])
    mgr = _ctx(db, "Manager", username="mgr_rec")
    _, p = _run(mgr, "recipe_detail", {"recipe_id": r.id})
    assert p["total_cost"] > 0 and p["unit_cost"] > 0
    assert p["ingredients"][0]["cost_price"] == 0.1 and "line_cost" in p["ingredients"][1]
    res, _ = _run(lab, "recipe_detail", {"recipe_id": 999999})
    assert res.is_error


# ─── Üretim ──────────────────────────────────────────────────────────────────

def test_production_summary_category_counts_and_default_tr_month(db_session):
    db = db_session
    ctx = _ctx(db, "Staff")
    now = dt.datetime.utcnow()
    shampoo = _card(db, "Minerva 108 Lavanta Şampuan", category="Bitmiş Ürün", unit="adet")
    cream = _card(db, "Serenida Yüz Kremi", category="Bitmiş Ürün", unit="adet")

    def _ph(item, qty, at, cancelled=False, domain="cosmetics"):
        db.add(ProductionHistory(recipe_name=item.name, target_item_id=item.id, target_item_name=item.name,
                                 produced_quantity=qty, produced_at=at, lot_number="X", domain=domain,
                                 cancelled_at=at if cancelled else None))
    _ph(shampoo, 100, now)
    _ph(shampoo, 50, now - dt.timedelta(hours=1))
    _ph(shampoo, 70, now - dt.timedelta(days=40))          # geçen ay
    _ph(shampoo, 999, now, cancelled=True)                 # iptal sayılmaz
    _ph(cream, 20, now)
    db.commit()
    res, p = _run(ctx, "production_summary", {"start": None, "end": None, "group": "category", "q": None})
    assert not res.is_error
    today = tr_now().date()
    assert p["start"] == today.replace(day=1).isoformat() and p["end"] == today.isoformat()
    rows = {r["name"]: r for r in p["rows"]}
    assert rows["Şampuan"]["total"] == 150 and rows["Şampuan"]["count"] == 2
    assert rows["Yüz Kremi"]["total"] == 20
    assert p["totals"]["count"] == 3 and p["totals"]["quantity"] == 170
    _, p = _run(ctx, "production_summary", {"start": (today - dt.timedelta(days=60)).isoformat(),
                                            "end": today.isoformat(), "group": None, "q": "şampuan"})
    assert p["group"] == "product" and len(p["rows"]) == 1 and p["rows"][0]["total"] == 220
    assert p["rows"][0]["category"] == "Şampuan" and p["rows"][0]["brand"]
    res, p = _run(ctx, "production_summary", {"start": "2026-13-01", "end": None, "group": None, "q": None})
    assert res.is_error
    res, p = _run(ctx, "production_summary", {"start": "2026-10-10", "end": "2026-10-01", "group": None, "q": None})
    assert res.is_error


def test_recent_productions_excludes_cancelled_and_limits(db_session):
    db = db_session
    ctx = _ctx(db, "Staff")
    it = _card(db, "Minerva 108 Krem", category="Bitmiş Ürün", unit="adet")
    for i, (lot, cancelled) in enumerate([("MNR001", False), ("MNR002", True), ("MNR003", False), ("MNR004", False)]):
        db.add(ProductionHistory(recipe_name="Krem", target_item_id=it.id, target_item_name=it.name,
                                 produced_quantity=10 + i, lot_number=lot, domain="cosmetics",
                                 cancelled_at=dt.datetime.utcnow() if cancelled else None))
    db.commit()
    _, p = _run(ctx, "recent_productions", {"limit": 2})
    assert [x["lot"] for x in p["productions"]] == ["MNR004", "MNR003"]
    assert p["productions"][0]["unit"] == "adet" and p["productions"][0]["link"] == "/traceability?lot=MNR004"
    _, p = _run(ctx, "recent_productions", {"limit": None})
    assert [x["lot"] for x in p["productions"]] == ["MNR004", "MNR003", "MNR001"]
    _, p = _run(ctx, "recent_productions", {"limit": 99})
    assert len(p["productions"]) == 3


# ─── Tedarikçi ───────────────────────────────────────────────────────────────

def test_find_supplier_and_supplier_detail_prices_prefs(db_session):
    db = db_session
    ctx = _ctx(db, "LabTech")                       # finans değil — fiyatlar yine görünür
    surya = _sup(db, "SURYA KİMYA", purchase_status="preferred", phone="0212", contact_person="Ali")
    tatli = _sup(db, "Tatlıdilimler", purchase_status="phase_out", status_reason="küçük parti")
    _sup(db, "SURYA SUPP", domain="supplement")
    g = MaterialGroup(name="Setil stearil alkol", domain="cosmetics")
    db.add(g)
    db.flush()
    a = _card(db, "SETİL STEARİL ALKOL", supplier_id=surya.id, group_id=g.id)
    b = _card(db, "GLİSERİN", supplier_id=surya.id)
    db.add_all([
        SupplierPrice(item_id=a.id, supplier_id=surya.id, supplier_name=surya.name, unit_price=120,
                      currency="USD", price_unit="kg", vat_included=True, vat_rate=20, source="manual",
                      quoted_at=dt.date(2026, 10, 1), domain="cosmetics"),
        SupplierPrice(item_id=b.id, supplier_id=surya.id, supplier_name=surya.name, unit_price=3.5,
                      currency="USD", price_unit="kg", vat_included=False, domain="cosmetics"),
        MaterialSupplierPref(material_group_id=g.id, supplier_id=surya.id, preference="preferred", rank=1,
                             domain="cosmetics"),
        MaterialSupplierPref(item_id=b.id, supplier_id=tatli.id, preference="avoid", rank=1, domain="cosmetics"),
    ])
    db.commit()
    _, p = _run(ctx, "find_supplier", {"q": "surya"})
    assert [s["id"] for s in p["suppliers"]] == [surya.id]
    assert p["suppliers"][0]["status_label"] == "Tercih edilen" and p["suppliers"][0]["phone"] == "0212"
    _, p = _run(ctx, "find_supplier", {"q": "tatlidilimler"})
    assert p["suppliers"][0]["id"] == tatli.id and p["suppliers"][0]["status_reason"] == "küçük parti"
    res, p = _run(ctx, "supplier_detail", {"supplier_id": surya.id})
    assert not res.is_error and p["items_count"] == 2 and p["contact_person"] == "Ali"
    prices = {x["item"]: x for x in p["prices"]}
    assert prices["SETİL STEARİL ALKOL"]["net_unit_price"] == 100 and prices["SETİL STEARİL ALKOL"]["vat"] == "KDV %20 dahil"
    assert prices["GLİSERİN"]["vat"] == "KDV hariç" and prices["GLİSERİN"]["quoted_at"] is None
    assert p["prefs"] == [{"material": "Setil stearil alkol", "preference": "preferred", "rank": 1}]
    _, p = _run(ctx, "supplier_detail", {"supplier_id": tatli.id})
    assert p["prefs"] == [{"material": "GLİSERİN", "preference": "avoid", "rank": 1}]
    res, _ = _run(_ctx(db, "Staff", domain="supplement", username="supp2"), "supplier_detail", {"supplier_id": surya.id})
    assert res.is_error
    # Fiyatlar sistem kuralıyla aynı kapıda (items.view | reports.view — /api/suppliers/{id}/prices):
    # yalnız inventory.view taşıyan "depocu" override'ı iletişimi görür, fiyat anahtarı HİÇ gelmez
    depocu = _ctx(db, "Staff", {"inventory": {"view": True}}, username="depocu_t")
    res, p = _run(depocu, "supplier_detail", {"supplier_id": surya.id})
    assert not res.is_error and p["contact_person"] == "Ali" and p["items_count"] == 2
    assert "prices" not in p and "prices_total" not in p and p["prefs"]
    res, p = _run(_ctx(db, "Staff", {"reports": {"view": True}}, username="raporcu_t"), "supplier_detail",
                  {"supplier_id": surya.id})
    assert not res.is_error and p["prices_total"] == 2


def test_find_lot_sublists_follow_system_permissions(db_session):
    """Lot aracı: inventory/production/retention alt listeleri yalnız ilgili
    view izniyle dolar — şahit dolabı hesabı (retention.view) envanter
    konumu/tedarikçisini, depo hesabı şahit rafını asistandan öğrenmez."""
    db = db_session
    p1 = _card(db, "Minerva 108 Lavanta Şampuan", category="Bitmiş Ürün", unit="adet")
    _lot(db, p1, "SR009", 90, location="Showroom")
    db.add(ProductionHistory(recipe_name="Lavanta Şampuan", target_item_id=p1.id, target_item_name=p1.name,
                             produced_quantity=93, lot_number="SR009", produced_by="songul", domain="cosmetics"))
    db.add(RetentionSample(item_id=p1.id, item_name=p1.name, lot_number="SR009-S", brand="Minerva 108",
                           shelf="1", slot="A", quantity=3, initial_quantity=3, unit="adet", domain="cosmetics"))
    db.commit()
    sahit = _ctx(db, "Staff", {"retention": {"view": True}}, username="sahit_only")
    res, p = _run(sahit, "find_lot", {"lot_number": "SR009"})
    assert not res.is_error and len(p["products"]) == 1
    one = p["products"][0]
    assert one["inventory"] == [] and one["production"] is None and one["retention"][0]["shelf_slot_label"] == "Raf 1 · Göz A"
    depo = _ctx(db, "Staff", {"inventory": {"view": True}}, username="depo_only")
    _, p = _run(depo, "find_lot", {"lot_number": "SR009"})
    one = p["products"][0]
    assert [i["lot"] for i in one["inventory"]] == ["SR009"] and one["production"] is None and one["retention"] == []
    full = _ctx(db, "LabTech", username="lab_full")
    _, p = _run(full, "find_lot", {"lot_number": "SR009"})
    one = p["products"][0]
    assert one["inventory"] and one["production"]["qty"] == 93 and one["retention"]


def test_tool_links_reject_backslash_and_protocol_relative(db_session, monkeypatch):
    """`/\\evil.com` tarayıcıda `https://evil.com`'a çözülür (WHATWG) — iç
    bağlantı süzgeci `//` gibi `/\\`'ı ve içindeki `\\`'ı da reddeder."""
    assert T.is_internal_href("/items?q=a") and T.is_internal_href("/traceability?lot=SR005")
    for bad in ("//evil.com", "/\\evil.com/x", "/ok/\\x", "https://ims.minerva108.com/items", "items", "", None, 5):
        assert not T.is_internal_href(bad), bad
    db = db_session
    ctx = _ctx(db, "Staff")

    def _evil(ctx, db, args):
        return {"items": [], "link": "/\\evil.com/login"}
    monkeypatch.setitem(T.REGISTRY, "find_item", dataclasses.replace(REGISTRY["find_item"], handler=_evil))
    res, p = _run(ctx, "find_item", {"q": "x"})
    assert not res.is_error and res.links == [] and p["link"] == "/\\evil.com/login"


# ─── Raporlar ────────────────────────────────────────────────────────────────

def test_stock_gaps_statuses_and_filter(db_session):
    db = db_session
    ctx = _ctx(db, "Staff")
    zero = _card(db, "ARGAN YAĞI", stock=0, min_level=5)
    low = _card(db, "BADEM YAĞI", stock=2, min_level=5)
    samp = _card(db, "ÇAY AĞACI YAĞI", stock=0)
    _lot(db, samp, "N-1", 0.2, is_sample=True)
    _card(db, "DİSTİLE SU", stock=100, min_level=5)
    _card(db, "KAPAK", category="Ambalaj", stock=0, min_level=10)
    db.add(StockOrderFlag(item_id=zero.id, quantity=10, unit="kg", domain="cosmetics"))
    db.commit()
    _, p = _run(ctx, "stock_gaps", {"status": "kritik", "q": None})
    assert [r["item_id"] for r in p["rows"]] == [low.id] and p["rows"][0]["status_label"] == "Kritik altı"
    _, p = _run(ctx, "stock_gaps", {"status": None, "q": None})
    assert [r["item_id"] for r in p["rows"]] == [zero.id, samp.id, low.id]
    assert p["summary"]["sifir"] == 1 and p["summary"]["kritik"] == 1 and p["summary"]["numune"] == 1
    assert p["summary"]["open_orders"] == 1
    assert p["rows"][0]["ordered"]["qty"] == 10 and p["rows"][1]["sample_qty"] == 0.2
    assert p["rows"][2]["ordered"] is None
    _, p = _run(ctx, "stock_gaps", {"status": "all", "q": "badem"})
    assert [r["item_id"] for r in p["rows"]] == [low.id]
    res, _ = _run(ctx, "stock_gaps", {"status": "yok", "q": None})
    assert res.is_error


def test_expiring_lots_window(db_session):
    db = db_session
    ctx = _ctx(db, "Staff")
    it = _card(db, "ALOE VERA JELİ")
    today = tr_now().date()
    _lot(db, it, "E10", 5, expiry=(today + dt.timedelta(days=10)).isoformat())
    _lot(db, it, "E100", 6, expiry=(today + dt.timedelta(days=100)).isoformat())
    _lot(db, it, "EPAST", 7, expiry=(today - dt.timedelta(days=1)).isoformat())
    _lot(db, it, "EBAD", 8, expiry="??")
    _lot(db, it, "EQ", 9, expiry=(today + dt.timedelta(days=5)).isoformat(), status="QUARANTINE")
    supp = _card(db, "SUPP", domain="supplement")
    _lot(db, supp, "ES", 1, expiry=(today + dt.timedelta(days=3)).isoformat())
    db.commit()
    _, p = _run(ctx, "expiring_lots", {"days": None})
    assert p["days"] == 60 and [l["lot"] for l in p["lots"]] == ["E10"] and p["lots"][0]["days_left"] == 10
    _, p = _run(ctx, "expiring_lots", {"days": 120})
    assert [l["lot"] for l in p["lots"]] == ["E10", "E100"] and p["count"] == 2
    _, p = _run(ctx, "expiring_lots", {"days": 9999})
    assert p["days"] == 180


# ─── Drive / PDKS / CRM ──────────────────────────────────────────────────────

def test_drive_search_names_only_and_min_length(db_session):
    db = db_session
    ctx = _ctx(db, "Staff")
    root = DriveFolder(name="Sertifikalar")
    db.add(root)
    db.flush()
    sub = DriveFolder(name="2026 Sertifika Yenileme", parent_id=root.id)
    db.add(sub)
    db.flush()
    db.add(DriveFile(original_name="ISO_sertifika.pdf", stored_name="abc123", size_bytes=2048, folder_id=sub.id))
    db.add(DriveFile(original_name="fatura.pdf", stored_name="def456", size_bytes=10, folder_id=None))
    db.commit()
    res, p = _run(ctx, "drive_search", {"q": "sertifika"})
    assert not res.is_error
    assert p["files"] == [{"id": p["files"][0]["id"], "name": "ISO_sertifika.pdf",
                           "path": "Sertifikalar / 2026 Sertifika Yenileme", "size": p["files"][0]["size"]}]
    assert {f["name"] for f in p["folders"]} == {"Sertifikalar", "2026 Sertifika Yenileme"}
    assert "stored_name" not in res.content and "abc123" not in res.content
    assert p["link"] == "/drive?q=sertifika"
    res, _ = _run(ctx, "drive_search", {"q": "s"})
    assert res.is_error
    # Drive kuralı: yalnız pdks yetkisi olan hesap giremez
    kiosk = _ctx(db, "Staff", {"pdks": {"kiosk": True, "view_own": True}}, username="kiosk")
    res, p = _run(kiosk, "drive_search", {"q": "sertifika"})
    assert res.is_error and p["error"] == "yetkiniz yok"


# ─── Kırpma ──────────────────────────────────────────────────────────────────

def test_truncation_keeps_valid_json(db_session, monkeypatch):
    db = db_session
    ctx = _ctx(db, "Staff")
    it = _card(db, "ÇOK LOTLU MALZEME", stock=300)
    for i in range(30):
        _lot(db, it, f"LOT{i:03d}", 10, location=f"Raf {i}")
    db.commit()
    monkeypatch.setattr(T, "MAX_RESULT_CHARS", 700)
    res, p = _run(ctx, "item_detail", {"item_id": it.id})
    assert not res.is_error and len(res.content) <= 700
    assert p["truncated"] is True and p["id"] == it.id and len(p["lots"]) < 20
    s, flag = T.fit_payload({"a": list(range(5))}, limit=10_000)
    assert flag is False and json.loads(s) == {"a": [0, 1, 2, 3, 4]}
    s, flag = T.fit_payload({"text": "x" * 5000}, limit=200)
    assert flag is True and len(s) <= 200 and json.loads(s)["truncated"] is True
