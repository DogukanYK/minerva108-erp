# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# ─────────────────────────────────────────────────────────────────────────────
"""core/product_content_rules.py testleri.

Fixture'lar 2026-09-10 denetiminde Minerva mağazasında bulunan gerçek hata
kalıplarını yeniden üretir (laboratuvar Excel'i × Shopify karşılaştırması):
Vitamin E / temizleme jeli, Red Clover / toner, lavanta / body scrub, argan+
badem / yasemin yağı — bu dosya, motorun bu hataları GERÇEKTEN yakaladığını
kanıtlar (regresyon: bir dahaki sefere aynı hata sessizce geçmesin).
"""
import pytest

from core.product_content_rules import (
    Issue, body_contract_ok, diff_cards, has_errors, inci_supports,
    key_ingredients_metafield, load_synonyms, normalize_inci_text, parse_size,
    render_body_html, suggest_key_ingredients, validate_card,
)


def _codes(issues: list[Issue]) -> set[str]:
    return {i.code for i in issues}


def _base_card(**overrides) -> dict:
    """Minimal geçerli kart — testler yalnız farklı olan alanları geçer."""
    card = {
        "brand": "minerva",
        "title": {"en": "Minerva108 Test Product", "tr": "Minerva108 Test Ürünü"},
        "scent": None,
        "sizes": [{"net_qty": 200, "net_unit": "ml"}],
        "inci_declared": ["Aqua", "Glycerin", "Cocamidopropyl Betaine"],
        "allergens": [],
        "key_ingredients": [],
        "tagline": {"en": "", "tr": ""},
        "intro": {"en": "A gentle daily formula.", "tr": "Günlük kullanım için nazik bir formül."},
        "benefits": [{"en": "Cleanses gently", "tr": "Nazikçe temizler"}],
        "usage": {"en": "Apply and rinse.", "tr": "Uygulayın ve durulayın."},
        "details": [{"en": "Vegan, PETA approved", "tr": "Vegan, PETA onaylı"}],
        "warnings": {"en": "", "tr": ""},
        "claims": {"allowed": ["vegan", "peta_approved"]},
        "seo": {"title": {"en": "", "tr": ""}, "description": {"en": "", "tr": ""}},
        "marketplace": {"amazon": {"bullets": [], "search_terms": ""}, "etsy": {"tags": []}},
    }
    card.update(overrides)
    return card


# ─── Gerçek hata regresyonları ──────────────────────────────────────────────

def test_cleansing_gel_vitamin_e_not_in_inci():
    """Minerva108 Facial Cleansing Gel: öne çıkan içerik 'Vitamin E' diyor, INCI'de tokoferol yok."""
    card = _base_card(
        key_ingredients=[
            {"name_en": "Safflower Seed Oil", "benefit_en": "provides gentle nourishment"},
            {"name_en": "Vitamin E", "benefit_en": "supports the skin's natural barrier"},
        ],
        inci_declared=["Aqua", "Carthamus Tinctorius Seed Oil", "Cocamidopropyl Betaine"],
    )
    issues = validate_card(card)
    assert "KI_NOT_IN_INCI" in _codes(issues)
    hit = [i for i in issues if i.code == "KI_NOT_IN_INCI"]
    assert any("vitamin e" in i.msg_tr.lower() for i in hit)
    # Safflower İSE destekleniyor — yanlış pozitif olmamalı
    assert not any("safflower" in i.msg_tr.lower() for i in hit)


def test_toner_red_clover_and_niacinamide_unsupported():
    """Red Clover Anti-Aging Toner: başlık + öne çıkan 'Red Clover' diyor ama INCI'de Trifolium yok; niacinamide de yok."""
    card = _base_card(
        title={"en": "Minerva108 Red Clover Anti-Aging Toner", "tr": "Minerva108 Kırmızı Yonca Toner"},
        key_ingredients=[{"name_en": "Niacinamide", "benefit_en": "brightens"}],
        inci_declared=["Aqua", "Pelargonium Graveolens Flower Water", "Glycerin"],
    )
    issues = validate_card(card)
    codes = _codes(issues)
    assert "TEXT_INGREDIENT_UNSUPPORTED" in codes or "TITLE_SCENT_UNSUPPORTED" in codes  # red clover başlıkta
    assert "KI_NOT_IN_INCI" in codes  # niacinamide öne çıkanda
    titled = [i for i in issues if i.field == "title" and "red clover" in i.msg_tr.lower()]
    assert titled, "başlıktaki 'Red Clover' iddiası yakalanmalı"


def test_body_scrub_lavender_scent_not_in_inci():
    """Body Scrub: koku alanı 'lavender' ama INCI'de Lavandula yok — hem SCENT_NOT_IN_INCI hem KI_NOT_IN_INCI."""
    card = _base_card(
        scent="lavender",
        key_ingredients=[{"name_en": "Lavender", "benefit_en": "calms the skin"}],
        inci_declared=["Aqua", "Prunus Amygdalus Dulcis Shell Powder", "Glycerin"],
    )
    issues = validate_card(card)
    codes = _codes(issues)
    assert "SCENT_NOT_IN_INCI" in codes
    assert "KI_NOT_IN_INCI" in codes


def test_jasmine_oil_argan_and_almond_not_in_inci():
    """Multi-Functional Beauty Oil – Jasmine: öne çıkanlarda 'Argan' + 'Almond' var, INCI'de yok."""
    card = _base_card(
        key_ingredients=[
            {"name_en": "Argan Oil", "benefit_en": "nourishes hair and skin"},
            {"name_en": "Sweet Almond Oil", "benefit_en": "softens"},
        ],
        inci_declared=["Jasminum Officinale Extract", "Helianthus Annuus Seed Oil"],
    )
    issues = validate_card(card)
    ki_fields = {i.field for i in issues if i.code == "KI_NOT_IN_INCI"}
    assert ki_fields == {"key_ingredients[0]", "key_ingredients[1]"}


def test_fully_supported_card_has_no_ingredient_errors():
    """Pozitif kontrol: INCI, koku ve öne çıkanlar tutarlıysa hiçbir bileşen/koku hatası çıkmamalı."""
    card = _base_card(
        scent="lavender",
        key_ingredients=[{"name_en": "Lavender", "name_tr": "Lavanta", "benefit_en": "calms", "benefit_tr": "yatıştırır"}],
        inci_declared=["Aqua", "Lavandula Angustifolia Oil", "Glycerin"],
    )
    issues = validate_card(card)
    bad_codes = {"KI_NOT_IN_INCI", "SCENT_NOT_IN_INCI", "TITLE_SCENT_UNSUPPORTED", "TEXT_INGREDIENT_UNSUPPORTED"}
    assert not (bad_codes & _codes(issues))


# ─── Diğer kurallar ─────────────────────────────────────────────────────────

def test_inci_empty_flagged():
    card = _base_card(inci_declared=[])
    issues = validate_card(card)
    assert "INCI_EMPTY" in _codes(issues)
    assert has_errors(issues)


def test_forbidden_claims_english_and_turkish():
    card_en = _base_card(details=[{"en": "100% Natural, chemical free formula", "tr": ""}])
    card_tr = _base_card(details=[{"en": "", "tr": "Cildi iyileştirir, kimyasal içermez"}])
    assert "FORBIDDEN_CLAIM" in _codes(validate_card(card_en))
    assert "FORBIDDEN_CLAIM" in _codes(validate_card(card_tr))


def test_size_mismatch_in_title():
    """Su Bazlı Saç Maskesi 100/200 ml uyuşmazlığı gibi: başlık 100 ml diyor, kart 200 ml."""
    card = _base_card(
        title={"en": "Minerva108 Water-Based Hair Mask (100 ml)", "tr": ""},
        sizes=[{"net_qty": 200, "net_unit": "ml"}],
    )
    issues = validate_card(card)
    assert "SIZE_MISMATCH" in _codes(issues)


def test_size_matching_title_no_false_positive():
    card = _base_card(title={"en": "Minerva108 Hair Mask (200 ml)", "tr": ""}, sizes=[{"net_qty": 200, "net_unit": "ml"}])
    issues = validate_card(card)
    assert "SIZE_MISMATCH" not in _codes(issues)


def test_allergen_not_in_inci_warns():
    card = _base_card(allergens=["Linalool"], inci_declared=["Aqua", "Glycerin"])
    issues = validate_card(card)
    hit = [i for i in issues if i.code == "ALLERGEN_NOT_IN_INCI"]
    assert hit and hit[0].severity == "W"


def test_seo_length_warning():
    card = _base_card(seo={"title": {"en": "x" * 61, "tr": ""}, "description": {"en": "", "tr": "y" * 161}})
    issues = validate_card(card)
    fields = {i.field for i in issues if i.code == "SEO_LENGTH"}
    assert {"seo.title.en", "seo.description.tr"} <= fields


def test_key_ingredients_missing_is_warning_not_error():
    card = _base_card(key_ingredients=[])
    issues = validate_card(card)
    ki = [i for i in issues if i.code == "KI_MISSING"]
    assert ki and ki[0].severity == "W"


def test_brand_mismatch_warns():
    card = _base_card(brand="serenida", title={"en": "Minerva108 Cleansing Gel", "tr": ""})
    issues = validate_card(card)
    assert "BRAND_MISMATCH" in _codes(issues)


# ─── Metin normalizasyonu (Türkçe İ/ı) ──────────────────────────────────────

def test_turkish_i_normalization_matches_synonym():
    """Lab Excel'i 'TRİTİCUM', 'OİL' gibi büyük-Türkçe-İ yazıyor — eşleşme kırılmamalı."""
    syn = load_synonyms()
    # 'castor' -> 'ricinus'; laboratuvar tarzı büyük harf + Türkçe İ ile yaz
    evidence = inci_supports("castor", ["Rİ CİNUS COMMUNİS SEED OİL".replace(" ", "")], syn)
    assert evidence, "Türkçe İ normalize edilmeden 'Ricinus' eşleşmesi kaçırılmamalı"


def test_normalize_inci_text_splits_and_trims():
    assert normalize_inci_text("Aqua, Glycerin ;  Niacinamide.") == ["Aqua", "Glycerin", "Niacinamide"]
    assert normalize_inci_text(None) == []
    assert normalize_inci_text(["Aqua", " Glycerin "]) == ["Aqua", "Glycerin"]


@pytest.mark.parametrize("raw,expected", [
    ("50 ML", (50.0, "ml")), ("10 g", (10.0, "g")), ("7ml", (7.0, "ml")),
    ("150.5 ML", (150.5, "ml")), ("boş kutu", (None, None)),
])
def test_parse_size(raw, expected):
    assert parse_size(raw) == expected


# ─── body_html üretimi ve sözleşme ──────────────────────────────────────────

def test_render_body_html_passes_own_contract():
    card = _base_card(
        intro={"en": "Fresh start.", "tr": "Taze bir başlangıç."},
        benefits=[{"en": "Cleanses gently", "tr": "Nazikçe temizler"}, {"en": "Non-drying", "tr": "Kurutmaz"}],
        usage={"en": "Massage and rinse.", "tr": "Masaj yapıp durulayın."},
        details=[{"en": "100% Vegan", "tr": "%100 Vegan"}],
        sizes=[{"net_qty": 200, "net_unit": "ml"}],
    )
    for lang in ("en", "tr"):
        html = render_body_html(card, lang)
        assert html.count("<h3>") == 3
        errs = body_contract_ok(html)
        assert errs == [], errs
    assert "Size: 200 ml" in render_body_html(card, "en")
    assert "Hacim: 200 ml" in render_body_html(card, "tr")


def test_body_contract_rejects_wrong_h3_count():
    errs = body_contract_ok("<p>x</p><h3>A</h3><ul><li>1</li></ul>")
    assert errs and "3 adet" in errs[0]


def test_body_contract_rejects_inci_leak():
    """Serenida'nın 'Ürün Detayları' listesine gömdüğü INCI gibi — gövdede INCI olmamalı."""
    html = ("<p>i</p><h3>Key Benefits</h3><ul><li>x</li></ul>"
            "<h3>How to Use</h3><p>y</p>"
            "<h3>Details</h3><ul><li>200 ml</li><li>İÇİNDEKİLER: Aqua, Glycerin</li></ul>")
    errs = body_contract_ok(html)
    assert any("INCI" in e for e in errs)


def test_body_contract_rejects_disallowed_tag():
    html = ("<h3>Key Benefits</h3><ul><li>x</li></ul><h3>How to Use</h3><p>y</p>"
            "<h3>Details</h3><ul><li>200 ml <span>x</span></li></ul>")
    errs = body_contract_ok(html)
    assert any("span" in e for e in errs)


def test_body_contract_details_first_line_must_have_size():
    html = ("<h3>Key Benefits</h3><ul><li>x</li></ul><h3>How to Use</h3><p>y</p>"
            "<h3>Details</h3><ul><li>Vegan formula</li><li>200 ml</li></ul>")
    errs = body_contract_ok(html)
    assert any("beden/hacim" in e for e in errs)


# ─── metafield / öneri / diff ───────────────────────────────────────────────

def test_key_ingredients_metafield_format():
    card = _base_card(key_ingredients=[
        {"name_en": "Safflower Seed Oil", "benefit_en": "provides gentle nourishment while cleansing"},
        {"name_en": "Jasmine Oil", "benefit_en": "offers a refreshing aromatic experience"},
    ])
    text = key_ingredients_metafield(card, "en")
    assert text == ("Safflower Seed Oil: provides gentle nourishment while cleansing. "
                     "Jasmine Oil: offers a refreshing aromatic experience.")


def test_suggest_key_ingredients_from_inci():
    suggestions = suggest_key_ingredients(
        ["Aqua", "Lavandula Angustifolia Oil", "Butyrospermum Parkii Butter", "Glycerin"], n=4)
    names = {s["name_en"] for s in suggestions}
    assert "Lavender" in names and "Shea" in names
    # insan onayı gereksinimi: fayda cümlesi otomatik doldurulmaz
    assert all(s["benefit_en"] == "" for s in suggestions)


def test_diff_cards_nested():
    a = {"tagline": {"en": "old", "tr": "eski"}, "status": "draft"}
    b = {"tagline": {"en": "new", "tr": "eski"}, "status": "draft"}
    d = diff_cards(a, b)
    assert d == {"tagline.en": {"old": "old", "new": "new"}}
