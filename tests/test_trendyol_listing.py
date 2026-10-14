"""
Trendyol Liste Doktoru — kural puanı (core/trendyol_listing.py, SAF; DB yok).

Kilitlenenler:
  • Ağırlıklar toplamı 100; kusursuz liste 100 alır, düzeltme önerisi yok.
  • Her bölümün sınır değerleri (başlık uzunluğu, süzgeç özelliği ×2,
    açıklama 200/600, görsel 4/5/6, buybox, teslimat 1/2 gün, aşırı satış).
  • İlk üç düzeltme kazanca göre sıralı.
  • Metin süzgeçleri (yasak iddia, promosyon, kanıtsız iddia) — yapay zekâ
    denetimi de AYNI listeleri kullanır.
"""
import pytest

from core import trendyol_listing as L

DEFS = [
    {"attribute_id": 47, "name": "Hacim", "required": True, "slicer": False, "varianter": False},
    {"attribute_id": 338, "name": "Cilt Tipi", "required": True, "slicer": True, "varianter": False},
    {"attribute_id": 60, "name": "Menşei", "required": False, "slicer": False, "varianter": False},
    {"attribute_id": 70, "name": "Etki", "required": False, "slicer": True, "varianter": False},
]

DESC = (
    "<p>Kırmızı yonca özü ile hazırlanan bu yüz kremi, kuru ve hassas ciltler için günlük bakım sunar. "
    "Hafif dokusu sayesinde hızla emilir ve ciltte yağlı his bırakmaz.</p>"
    "<p>Öne çıkan özellikler:</p><ul><li>Gün boyu nem desteği</li><li>Hafif, yağsız doku</li>"
    "<li>Parfümsüz formül</li></ul>"
    "<p>Kullanım: Temizlenmiş cilde sabah ve akşam yeterli miktarda sürün, nazikçe masaj yaparak yedirin. "
    "Göz çevresiyle temasından kaçının.</p>"
    "<p>İçerik: kırmızı yonca özü, aloe vera yaprak suyu, shea yağı. 50 ml. Serin ve kuru yerde saklayınız. "
    "Çocukların ulaşamayacağı yerde muhafaza ediniz. Yalnızca haricen kullanım içindir. "
    "Ambalajı açıldıktan sonra 12 ay içinde kullanılması önerilir.</p>"
)


def _attrs(*ids):
    names = {47: "Hacim", 338: "Cilt Tipi", 60: "Menşei", 70: "Etki"}
    return [{"id": i, "name": names[i], "values": ["x"], "value_ids": [1]} for i in ids]


def _p(**kw):
    base = {"title": "Minerva 108 Kırmızı Yonca Nemlendirici Yüz Kremi Hassas Ciltler İçin 50 ml",
            "brand": "Minerva 108", "category": "Yüz Kremi", "description": DESC,
            "attributes": _attrs(47, 338, 60, 70), "image_count": 6, "sale_price": 315.0, "list_price": 350.0,
            "buybox_order": 1, "has_multiple_seller": False, "delivery_duration": 1, "on_sale": True,
            "archived": False, "locked": False, "blacklisted": False, "quantity": 5}
    base.update(kw)
    return base


IMS = {"stock": 10.0, "brand": "Minerva 108"}


def _section(result, key):
    return next(b for b in result["breakdown"] if b["key"] == key)


def test_weights_sum_to_100_and_perfect_listing():
    assert sum(L.WEIGHTS.values()) == 100
    r = L.score_listing(_p(), DEFS, IMS)
    assert r["score"] == 100 and r["fixes"] == []
    assert [b["key"] for b in r["breakdown"]] == list(L.WEIGHTS)
    assert all(b["points"] == b["max"] for b in r["breakdown"])


@pytest.mark.parametrize("title,points", [
    ("Krem", 6.0),                                           # kısa: uzunluk 0 + tür 4 + temiz 2
    ("Minerva 108 Yüz Kremi Kırmızı Yonca 50 ml", 16.0),       # 30–49 karakter: uzunluk 4/8
    ("Minerva 108 Kırmızı Yonca Nemlendirici Yüz Kremi Hassas Ciltler İçin 50 ml", 20.0),
    ("MİNERVA 108 KIRMIZI YONCA NEMLENDİRİCİ YÜZ KREMİ HASSAS CİLT 50 ML", 18.0),   # büyük harf
    ("Minerva 108 Kırmızı Yonca Yüz Kremi 50 ml İndirimli Kampanya Fırsatı", 18.0),  # promosyon
])
def test_title_rules(title, points):
    assert _section(L.score_listing(_p(title=title), DEFS, IMS), "title")["points"] == points


def test_attribute_rules_weight_slicers_and_missing_list():
    sec = _section(L.score_listing(_p(attributes=_attrs(47, 338)), DEFS, IMS), "attributes")
    assert sec["points"] == 10.0                               # zorunlular tam, isteğe bağlı 0
    sec = _section(L.score_listing(_p(attributes=_attrs(47, 338, 70)), DEFS, IMS), "attributes")
    assert sec["points"] == pytest.approx(16.7, abs=0.05)      # süzgeç (×2) 2/3 ağırlık
    sec = _section(L.score_listing(_p(attributes=_attrs(47)), DEFS, IMS), "attributes")
    assert sec["checks"][0]["points"] == 5.0 and "Cilt Tipi" in sec["checks"][0]["detail"]
    no_defs = _section(L.score_listing(_p(), [], IMS), "attributes")
    assert no_defs["points"] == 0 and "henüz yok" in no_defs["checks"][0]["detail"]
    missing = L.missing_attributes(_p(attributes=_attrs(47)), DEFS)
    assert [a["attribute_id"] for a in missing] == [338, 70, 60]   # zorunlu → süzgeç → diğer


def test_description_rules():
    assert _section(L.score_listing(_p(description=""), DEFS, IMS), "description")["points"] == 0
    plain = "a" * 250
    sec = _section(L.score_listing(_p(description=plain), DEFS, IMS), "description")
    assert sec["points"] == 5
    assert _section(L.score_listing(_p(), DEFS, IMS), "description")["points"] == 15


@pytest.mark.parametrize("n,points", [(0, 0), (3, 0), (4, 8), (5, 11), (6, 15), (9, 15)])
def test_image_steps(n, points):
    assert _section(L.score_listing(_p(image_count=n), DEFS, IMS), "images")["points"] == points


def test_price_and_buybox():
    no_disc = _section(L.score_listing(_p(list_price=315.0), DEFS, IMS), "price")
    assert no_disc["points"] == 5
    lost = _section(L.score_listing(_p(buybox_order=2, buybox_price=290.0), DEFS, IMS), "price")
    assert lost["checks"][1]["points"] == 0 and "2. sırada" in lost["checks"][1]["detail"]
    single = _section(L.score_listing(_p(buybox_order=None, has_multiple_seller=False), DEFS, IMS), "price")
    assert single["checks"][1]["points"] == 5
    unknown = _section(L.score_listing(_p(buybox_order=None, has_multiple_seller=None), DEFS, IMS), "price")
    assert unknown["checks"][1]["points"] == 0


@pytest.mark.parametrize("days,points", [(1, 10), (0, 10), (2, 5), (3, 0), (None, 0)])
def test_delivery(days, points):
    assert _section(L.score_listing(_p(delivery_duration=days), DEFS, IMS), "delivery")["points"] == points


def test_status_and_oversell():
    over = _section(L.score_listing(_p(quantity=12), DEFS, IMS), "status")
    assert over["points"] == 7 and over["checks"][2]["points"] == 0
    zero = _section(L.score_listing(_p(quantity=0), DEFS, IMS), "status")
    assert zero["checks"][1]["points"] == 0
    unmatched = _section(L.score_listing(_p(), DEFS, {"stock": None}), "status")
    assert unmatched["checks"][2]["points"] == 0 and "eşleşmemiş" in unmatched["checks"][2]["detail"]
    locked = _section(L.score_listing(_p(locked=True), DEFS, IMS), "status")
    assert locked["checks"][0]["points"] == 0


def test_fixes_are_top_three_by_gain():
    r = L.score_listing(_p(image_count=0, delivery_duration=None, description=""), DEFS, IMS)
    assert [f["key"] for f in r["fixes"]] == ["images.count", "delivery.duration", "description.min"]
    assert [f["gain"] for f in r["fixes"]] == [15, 10, 5]


def test_text_filters_shared_with_ai():
    assert "tedavi iddiası" in L.claim_flags("Sivilceyi tedavi eder")
    assert "hastalık adı" in L.claim_flags("Egzama için ideal")
    assert "%100 etkinlik iddiası" in L.claim_flags("%100 etkili formül")
    assert L.claim_flags("Cildin nemlendirilmesine yardımcı olur") == []
    assert L.promo_words_in("Ücretsiz Kargo FIRSAT ürünü") == ["ucretsiz kargo", "firsat"]
    assert L.unverified_claims("Dermatolojik olarak test edilmiştir.", ["not yok"]) == ["dermatolojik test"]
    assert L.unverified_claims("Dermatolojik olarak test edilmiştir.",
                               ["Dermatolojik olarak test edildi (rapor 2025)"]) == []
    assert L.unverified_claims("Klinik olarak test edildi, hipoalerjenik.", [None, ""]) == ["klinik test", "hipoalerjenik"]


def test_strip_html():
    assert L.strip_html("<p>A &amp; B</p><ul><li>x</li></ul>") == "A & B\n\nx"
    assert L.strip_html(None) == ""
