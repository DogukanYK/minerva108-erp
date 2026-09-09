"""Influencer motoru testleri — SAF (DB'siz): core/influencer, core/influencer_files,
core/delivery_note sample_notice bandı.
"""
import asyncio
import io
import math
from datetime import date, datetime

import pytest

from core import influencer as inf
from core.influencer import (
    DEFAULT_BENCHMARKS, DEFAULT_TIERS, LABEL_TEMPLATES, SCORE_WEIGHTS, STAGES, STAGE_LABELS,
    TRANSITIONS, authenticity_score, benchmark_map, breakeven_orders, can_transition,
    compliance_check, component_range_fit, contribution_per_order, decision_rule,
    growth_stability, kpis, label_template, loaded_gift_cost, match_affiliate,
    next_collab_code, parse_referral_marker, payout_batches, payout_period, post_stats,
    reminder_dates, tier_for, upgrade_signal,
)

BENCH = benchmark_map(DEFAULT_BENCHMARKS)


# ─── Kademeler ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("followers,expected", [
    (0, None), (199, None), (200, "nano"), (1999, "nano"), (2000, "mikro"),
    (19999, "mikro"), (20000, "orta"), (199999, "orta"), (200000, "makro"),
    (5_000_000, "makro"),
])
def test_tier_boundaries(followers, expected):
    assert tier_for(followers, DEFAULT_TIERS) == expected


def test_tier_never_returns_ambassador_and_seed_shape():
    assert tier_for(10, DEFAULT_TIERS) is None
    keys = {t["key"] for t in DEFAULT_TIERS}
    assert keys == {"nano", "mikro", "orta", "makro", "ambassador"}
    for t in DEFAULT_TIERS:
        for f in ("key", "label", "min_followers", "max_followers", "commission_pct",
                  "customer_discount_pct", "max_gift_items", "launch_access", "code_allowed", "sort_order"):
            assert f in t
    nano = next(t for t in DEFAULT_TIERS if t["key"] == "nano")
    assert nano["code_allowed"] is False and nano["commission_pct"] == 10


def test_default_benchmarks_cover_matrix():
    assert len(DEFAULT_BENCHMARKS) == 3 * 4 * 5
    assert BENCH[("instagram", "nano", "er_follower")] == (3.5, 9.0)
    assert BENCH[("tiktok", "makro", "er_follower")] == (2.0, 4.0)
    assert BENCH[("youtube", "orta", "view_per_follower")] == (10.0, 1000.0)
    assert BENCH[("instagram", "mikro", "comment_quality_pct")] == (0.0, 15.0)


# ─── Gönderi istatistikleri ──────────────────────────────────────────────────

def test_post_stats_er_and_cv():
    posts = [
        {"likes": 100, "comments": 10, "views": 1000, "saves": 5, "shares": 5},
        {"likes": 200, "comments": 20, "views": 3000, "saves": 10, "shares": 10},
    ]
    st = post_stats(posts, followers=5000)
    assert st["avg_likes"] == 150 and st["avg_comments"] == 15 and st["avg_views"] == 2000
    # (150+15+7.5+7.5)/5000 = 3.6%
    assert st["er_follower"] == pytest.approx(3.6)
    assert st["er_view"] == pytest.approx(9.0)
    assert st["view_per_follower"] == pytest.approx(40.0)
    assert st["comment_like_ratio"] == pytest.approx(10.0)
    # views 1000/3000: std=1000, mean=2000 → CV 50%
    assert st["views_cv"] == pytest.approx(50.0)


def test_post_stats_missing_data_is_none():
    st = post_stats([], followers=1000)
    assert st["er_follower"] is None and st["views_cv"] is None
    st = post_stats([{"likes": 10, "comments": 1}], followers=0)
    assert st["er_follower"] is None and st["view_per_follower"] is None
    assert st["comment_like_ratio"] == 10.0


# ─── Aralık uyumu ────────────────────────────────────────────────────────────

def test_range_fit_edges():
    assert component_range_fit(5, 4, 8) == 100
    assert component_range_fit(4, 4, 8) == 100
    assert component_range_fit(8, 4, 8) == 100
    assert component_range_fit(2, 4, 8) == 0         # low/2
    assert component_range_fit(1, 4, 8) == 0
    assert component_range_fit(3, 4, 8) == pytest.approx(50.0)
    assert component_range_fit(24, 4, 8) == 20       # 3×high
    assert component_range_fit(100, 4, 8) == 20
    assert component_range_fit(16, 4, 8) == pytest.approx(60.0)   # yarı yol 100→20
    assert component_range_fit(None, 4, 8) is None
    assert component_range_fit(0, 0, 15) == 100      # low=0 aralığı


# ─── Skor ────────────────────────────────────────────────────────────────────

def test_score_weights_sum_100():
    assert sum(SCORE_WEIGHTS.values()) == 100
    assert set(SCORE_WEIGHTS) == {"er_fit", "comment_quality", "comment_like", "view_follower",
                                  "geo", "growth", "saves_shares", "fake_pct"}


def _healthy_stats():
    posts = [{"likes": 300, "comments": 30, "views": 4000, "saves": 20, "shares": 10},
             {"likes": 250, "comments": 25, "views": 3000, "saves": 15, "shares": 8},
             {"likes": 350, "comments": 35, "views": 6000, "saves": 25, "shares": 12}]
    return post_stats(posts, followers=5000)   # er 7.2% (IG nano 3.5–9), vpf≈87%, c/l 10%


def test_authenticity_full_data_high_score():
    r = authenticity_score(_healthy_stats(), BENCH, "instagram", "nano",
                           geo_target_pct=80, growth_series=[(date(2026, 1, 1), 4000), (date(2026, 3, 1), 5000)],
                           comment_quality_pct=5, fake_pct=5)
    assert r["score"] == 100
    assert r["flags"] == []
    assert set(r["components"]) == set(SCORE_WEIGHTS)


def test_authenticity_missing_data_scores_50_and_flags():
    r = authenticity_score(_healthy_stats(), BENCH, "instagram", "nano")
    assert "eksik_veri:geo" in r["flags"]
    assert "eksik_veri:growth" in r["flags"]
    assert "eksik_veri:fake_pct" in r["flags"]
    assert "eksik_veri:comment_quality" in r["flags"]
    assert r["components"]["geo"] == 50
    # 4 eksik bileşen (15+10+5+15 = 45 ağırlık) yarı puan → 100 − 22.5
    assert r["score"] == 78


def test_authenticity_hidden_subs_and_fake_pct_flags():
    base = authenticity_score(_healthy_stats(), BENCH, "youtube", "nano", geo_target_pct=80,
                              comment_quality_pct=3, fake_pct=5,
                              growth_series=[(date(2026, 1, 1), 1000), (date(2026, 2, 1), 1200)])
    hidden = authenticity_score(_healthy_stats(), BENCH, "youtube", "nano", geo_target_pct=80,
                                comment_quality_pct=3, fake_pct=5, hidden_subs=True,
                                growth_series=[(date(2026, 1, 1), 1000), (date(2026, 2, 1), 1200)])
    assert "gizli_abone_sayisi" in hidden["flags"]
    assert hidden["score"] == max(0, base["score"] - 20)

    fake = authenticity_score(_healthy_stats(), BENCH, "instagram", "mikro", geo_target_pct=80,
                              comment_quality_pct=3, fake_pct=30,
                              growth_series=[(date(2026, 1, 1), 1000), (date(2026, 2, 1), 1200)])
    assert "sahte_takipci_yuksek" in fake["flags"]
    assert fake["components"]["fake_pct"] == 0

    geo = authenticity_score(_healthy_stats(), BENCH, "instagram", "mikro", geo_target_pct=40)
    assert "kitle_hedef_disi" in geo["flags"]
    assert geo["components"]["geo"] < 20      # 40 → low/2=35 ile 70 arası lineer (≈14)


def test_authenticity_flat_views_penalty():
    posts = [{"likes": 100, "comments": 10, "views": 1000, "saves": 5, "shares": 5}] * 4
    st = post_stats(posts, 2000)
    assert st["views_cv"] == 0
    r = authenticity_score(st, BENCH, "instagram", "nano", geo_target_pct=90,
                           comment_quality_pct=2, fake_pct=2,
                           growth_series=[(date(2026, 1, 1), 1900), (date(2026, 2, 1), 2000)])
    assert "izlenme_sabit" in r["flags"]
    assert r["score"] == 70


def test_growth_stability():
    steady = [(date(2026, 1, 1), 1000), (date(2026, 2, 1), 1300), (date(2026, 3, 1), 1500)]
    assert growth_stability(steady) == 100
    jump = [(date(2026, 1, 1), 1000), (date(2026, 1, 3), 6000)]      # 2 günde +5000
    assert growth_stability(jump) == 0
    weekly = [(date(2026, 1, 1), 50000), (date(2026, 1, 7), 61000)]  # 7 günde >10K
    assert growth_stability(weekly) == 0
    drop = [(date(2026, 1, 1), 10000), (date(2026, 3, 1), 10500), (date(2026, 6, 1), 7000)]
    assert growth_stability(drop) == 50
    assert growth_stability([(date(2026, 1, 1), 100)]) is None
    assert growth_stability([]) is None
    # datetime / ISO string girdisi de kabul
    assert growth_stability([(datetime(2026, 1, 1), 100), ("2026-02-01", 150)]) == 100


# ─── Statü makinesi ──────────────────────────────────────────────────────────

def test_stages_and_labels_complete():
    assert STAGES[0] == "teklif_gonderildi" and STAGES[-1] == "iptal"
    assert set(STAGE_LABELS) == set(STAGES)
    assert set(TRANSITIONS) == set(STAGES)
    for st in STAGES:
        assert TRANSITIONS[st] <= set(STAGES)


def test_transitions_rules():
    assert can_transition("teklif_gonderildi", "kabul_edildi")
    assert not can_transition("teklif_gonderildi", "kargoda")
    assert can_transition("kabul_edildi", "adres_bekleniyor")
    assert can_transition("urun_hazirlaniyor", "teslim_edildi")     # elden teslim
    assert can_transition("revizyon", "taslak_incelemede")
    assert not can_transition("revizyon", "onaylandi")
    assert can_transition("taslak_incelemede", "revizyon")
    assert can_transition("taslak_incelemede", "onaylandi")
    assert not can_transition("yayinlandi", "teklif_gonderildi")
    assert not can_transition("tamamlandi", "yayinlandi")
    assert not can_transition("iptal", "kabul_edildi")
    assert not can_transition("yok", "iptal")
    assert inf.can_flag_no_post("icerik_bekleniyor")
    assert not inf.can_flag_no_post("kargoda")


def test_cancel_from_every_active_stage():
    for st in STAGES:
        if st in ("tamamlandi", "iptal"):
            assert not can_transition(st, "iptal")
        else:
            assert can_transition(st, "iptal"), st


def test_collab_code_and_relationship_stages():
    assert next_collab_code(12, 2026) == "INF-2026-00012"
    assert next_collab_code(123456, 2027) == "INF-2027-123456"
    assert inf.RELATIONSHIP_STAGES == ["havuz", "basvurdu", "seeded", "affiliate",
                                       "ambassador", "pasif", "kara_liste"]
    assert set(inf.RELATIONSHIP_LABELS) == set(inf.RELATIONSHIP_STAGES)


# ─── Pazar etiketi + uyum ────────────────────────────────────────────────────

def test_label_template_markets():
    assert label_template("TR", "@minerva108") == "Reklam — @minerva108 tarafından sağlandı"
    assert label_template("US", "minerva108") == "Ad | Paid partnership with @minerva108"
    assert label_template("UK", "minerva108") == "Ad — @minerva108"
    assert label_template("DE", "minerva108") == "Werbung — @minerva108"
    assert label_template("EU", "minerva108") == "Ad — @minerva108"
    assert label_template("xx", "minerva108") == LABEL_TEMPLATES["EU"].format(brand="minerva108")


FORBID = inf.CFG_DEFAULTS[inf.CFG_FORBIDDEN_WORDS]


def test_compliance_tr_label_first_line():
    ok = compliance_check("Reklam — @minerva108 tarafından sağlandı\nCildim çok nemlendi.",
                          "TR", ["@minerva108"], FORBID)
    assert ok["has_label"] and ok["label_first_line"] and ok["brand_mentioned"]
    assert ok["forbidden_hits"] == [] and ok["score"] == 100

    late = compliance_check("Cildim çok nemlendi 🌿\n\n#reklam @minerva108", "TR", ["minerva108"], FORBID)
    assert late["has_label"] and not late["label_first_line"]
    assert late["score"] == 80

    none = compliance_check("Harika bir serum, akne izlerimi 3 günde geçirdi!", "TR", ["minerva108"], FORBID)
    assert not none["has_label"] and not none["brand_mentioned"]
    hits = [h.lower() for h in none["forbidden_hits"]]
    assert "akne" in hits and "3 günde" in hits and "geçir" in hits
    assert none["score"] == 0


def test_compliance_other_markets():
    us = compliance_check("Ad | Paid partnership with @serenida\nLove it", "US", ["serenida"], FORBID)
    assert us["has_label"] and us["label_first_line"] and us["score"] == 100
    de = compliance_check("Werbung — @minerva108", "DE", ["minerva108"], FORBID)
    assert de["has_label"]
    # TR pazarında 'Ad' etiket sayılmaz
    tr = compliance_check("Ad — @minerva108", "TR", ["minerva108"], FORBID)
    assert not tr["has_label"]
    # "adorable" içindeki 'ad' etiket değil
    uk = compliance_check("adorable packaging @minerva108", "UK", ["minerva108"], FORBID)
    assert not uk["has_label"]


# ─── Hatırlatmalar ───────────────────────────────────────────────────────────

def test_reminder_dates():
    d = date(2026, 9, 1)
    assert reminder_dates(d, [7, 14]) == [date(2026, 9, 8), date(2026, 9, 15)]
    assert reminder_dates(d, [14, 7, 7]) == [date(2026, 9, 8), date(2026, 9, 15)]
    assert reminder_dates(datetime(2026, 9, 1, 15, 0), []) == []
    assert inf.parse_int_list(inf.CFG_DEFAULTS[inf.CFG_REMINDER_DAYS]) == [7, 14]
    assert inf.parse_int_list("bozuk", [5]) == [5]
    assert inf.parse_handles("@minerva108, @Serenida") == ["minerva108", "serenida"]


def test_cfg_defaults_keys():
    assert inf.CFG_DEFAULTS["influencer.reminder_days"] == "7,14"
    assert inf.CFG_DEFAULTS["influencer.no_post_days"] == "45"
    assert inf.CFG_DEFAULTS["influencer.min_payout_try"] == "1000"
    assert inf.CFG_DEFAULTS["influencer.gross_margin_pct"] == "60"
    assert inf.CFG_DEFAULTS["influencer.brand_handles"] == "@minerva108"
    assert inf.CFG_DEFAULTS["influencer.guideline_version"] == "2026-08"
    assert set(inf.CFG_KEYS) == set(inf.CFG_DEFAULTS)


# ─── Atıf ────────────────────────────────────────────────────────────────────

def test_parse_referral_marker_variants():
    a = parse_referral_marker({"note_attributes": [{"name": "sca_ref", "value": "AbC123"}],
                               "tags": "UpPromote_order"})
    assert a == {"affiliate_key": "AbC123", "source": "note_attribute"}
    b = parse_referral_marker({"note_attributes": [], "tags": "vip, UpPromote_order"})
    assert b == {"affiliate_key": None, "source": "tag"}
    b2 = parse_referral_marker({"tags": ["uppromote_order"]})
    assert b2["source"] == "tag"
    c = parse_referral_marker({"note_attributes": [{"name": "gift", "value": "x"}], "tags": ""})
    assert c == {"affiliate_key": None, "source": None}
    assert parse_referral_marker({})["source"] is None
    # boş sca_ref değeri kesin sayılmaz
    e = parse_referral_marker({"note_attributes": [{"name": "sca_ref", "value": "  "}]})
    assert e["source"] is None


def test_match_affiliate_email_normalization():
    affs = [{"sca_ref": "K1", "affiliate_email": " Ayse@Example.com ", "creator_id": 7},
            {"sca_ref": "k2", "affiliate_email": "bob@x.com", "creator_id": 8}]
    assert match_affiliate("ayse@example.com", affs) == 7
    assert match_affiliate("AYSE@EXAMPLE.COM ", affs) == 7
    assert match_affiliate("K2", affs) == 8
    assert match_affiliate("k1", affs) == 7
    assert match_affiliate("nobody@x.com", affs) is None
    assert match_affiliate("", affs) is None


# ─── Ödeme dönemi ────────────────────────────────────────────────────────────

def test_payout_period_and_batches():
    assert payout_period(datetime(2026, 9, 5, 10, 0)) == "2026-09"
    assert payout_period(date(2026, 1, 31)) == "2026-01"
    assert payout_period("2026-03-05T00:00:00") == "2026-03"
    rows = [{"creator_id": 1, "currency": "TRY", "amount": 600},
            {"creator_id": 1, "currency": "TRY", "amount": 500},
            {"creator_id": 2, "currency": "TRY", "amount": 999.99},
            {"creator_id": 3, "currency": "USD", "amount": 12}]
    out = payout_batches(rows, {"TRY": 1000})
    by = {(r["creator_id"], r["currency"]): r for r in out}
    assert by[(1, "TRY")]["total"] == 1100 and by[(1, "TRY")]["payable"] is True
    assert by[(2, "TRY")]["total"] == 999.99 and by[(2, "TRY")]["payable"] is False   # eşik altı devreder
    assert by[(3, "USD")]["payable"] is True    # eşik tanımsız → 0
    assert payout_batches([], {}) == []


# ─── Ekonomi ─────────────────────────────────────────────────────────────────

def test_economics_and_breakeven_inf():
    assert loaded_gift_cost(120, 15, 65) == 200
    # AOV 500, indirim 0, COGS 150, kargo 50, komisyon %10 → 500−150−50−50 = 250
    assert contribution_per_order(500, 0, 150, 50, 10) == 250
    assert contribution_per_order(500, 20, 150, 50, 10) == pytest.approx(400 - 150 - 50 - 40)
    assert breakeven_orders(200, 250) == 0.8
    assert breakeven_orders(200, 0) == math.inf
    assert breakeven_orders(200, -5) == math.inf


def test_kpis_without_emv():
    refs = [{"order_total": 600, "refunded_total": 0, "commission_amount": 60, "is_new_customer": True},
            {"order_total": 400, "refunded_total": 100, "commission_amount": 40, "is_new_customer": False},
            {"order_total": 500, "refunded_total": 0, "commission_amount": 50, "is_new_customer": None}]
    k = kpis({"referrals": refs, "loaded_cost": 200, "store_aov": 400, "gross_margin_pct": 60,
              "cogs_per_order": 150, "ship_per_order": 50, "commission_pct": 10,
              "views": 4000, "received_count": 1, "posted_count": 1})
    assert "emv" not in {x.lower() for x in k}
    assert k["orders"] == 3 and k["net_revenue"] == 1400
    assert k["aov"] == pytest.approx(466.67)
    assert k["aov_vs_store"] == pytest.approx(16.7)
    assert k["new_customer_share"] == 50.0        # bilinen 2'den 1
    assert k["cac"] == 350                          # (200+150)/1
    assert k["roi"] == pytest.approx((1400 * 0.6 - 350) / 350, abs=1e-3)
    assert k["post_rate"] == 100.0
    assert k["orders_per_1k_views"] == 0.75
    assert math.isfinite(k["breakeven"])

    empty = kpis({"referrals": [], "loaded_cost": 0})
    assert empty["orders"] == 0 and empty["aov"] is None and empty["new_customer_share"] is None
    assert empty["cac"] is None and empty["roi"] is None and empty["post_rate"] is None
    assert empty["orders_per_1k_views"] is None


def test_decision_rule_three_branches():
    dec, why = decision_rule({"orders": 0, "breakeven": 1.5, "orders_per_1k_views": 0,
                              "uplift": False, "content_reusable": False}, {})
    assert dec == "birak" and why
    dec, why = decision_rule({"orders": 2, "breakeven": 1.5, "new_customer_share": 30}, {})
    assert dec == "tekrarla"
    dec, _ = decision_rule({"orders": 0, "breakeven": 1.5, "orders_per_1k_views": 1.2}, {})
    assert dec == "tekrarla"
    dec, _ = decision_rule({"orders": 0, "breakeven": math.inf, "survey_attribution": 2}, {})
    assert dec == "tekrarla"
    dec, why = decision_rule({"orders": 6, "breakeven": 1.5, "new_customer_share": 70,
                              "r90": 30, "store_r90": 25}, {})
    assert dec == "yukselt" and len(why) == 3
    # R90 bilinmiyor → yükseltme yok, tekrarla
    dec, _ = decision_rule({"orders": 6, "breakeven": 1.5, "new_customer_share": 70}, {})
    assert dec == "tekrarla"
    # kırılma sonsuz ama içerik yeniden kullanılabilir → tekrarla
    dec, _ = decision_rule({"orders": 0, "breakeven": math.inf, "content_reusable": True}, {})
    assert dec == "tekrarla"


def test_upgrade_signal():
    assert upgrade_signal({"relationship_stage": "seeded", "delivered_at": date(2026, 9, 1),
                           "posted_at": date(2026, 9, 10), "post_er": 5.0, "avg_er": 3.0}) == "affiliate_teklif"
    assert upgrade_signal({"relationship_stage": "seeded", "delivered_at": date(2026, 9, 1),
                           "posted_at": date(2026, 9, 20), "post_er": 5.0, "avg_er": 3.0}) is None
    assert upgrade_signal({"relationship_stage": "seeded", "days_since_delivered": 95,
                           "orders_90d": 0, "posts_90d": 0}) == "pasif"
    assert upgrade_signal({"relationship_stage": "affiliate", "orders_90d": 12}) == "ambassador_aday"
    assert upgrade_signal({"relationship_stage": "affiliate", "orders_90d": 3}) is None
    assert upgrade_signal({"relationship_stage": "kara_liste", "orders_90d": 50}) is None


# ─── Dosya yükleme (sihirli bayt + boyut) ────────────────────────────────────

def _upload(data: bytes, name="x.bin", ctype="application/octet-stream"):
    from starlette.datastructures import UploadFile, Headers
    return UploadFile(file=io.BytesIO(data), filename=name,
                      headers=Headers({"content-type": ctype}))


def _run(coro):
    return asyncio.run(coro)


def test_save_media_sniffs_and_limits(tmp_path, monkeypatch):
    from core import influencer_files as ifl
    monkeypatch.setattr(ifl, "DRIVE_DIR", tmp_path)
    monkeypatch.setattr(ifl, "MAX_IMAGE_BYTES", 32)
    monkeypatch.setattr(ifl, "MAX_VIDEO_BYTES", 64)

    mp4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 20
    name, size, mime = _run(ifl.save_media(_upload(mp4, "clip.exe")))
    assert name.endswith(".mp4") and mime == "video/mp4" and size == len(mp4)
    assert (tmp_path / name).read_bytes() == mp4

    mov = b"\x00\x00\x00\x14ftypqt  " + b"\x00" * 8
    assert _run(ifl.save_media(_upload(mov)))[2] == "video/quicktime"
    webm = b"\x1a\x45\xdf\xa3" + b"\x00" * 12
    assert _run(ifl.save_media(_upload(webm)))[0].endswith(".webm")
    jpg = b"\xff\xd8\xff\xe0" + b"\x00" * 8
    assert _run(ifl.save_media(_upload(jpg)))[2] == "image/jpeg"
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
    assert _run(ifl.save_media(_upload(png)))[2] == "image/png"
    pdf = b"%PDF-1.4\n" + b"\x00" * 8
    assert _run(ifl.save_media(_upload(pdf)))[2] == "application/pdf"

    # Content-Type / uzantı beyanı işe yaramaz — sihirli bayt yoksa red
    with pytest.raises(ValueError):
        _run(ifl.save_media(_upload(b"hello world!!!!!", "a.mp4", "video/mp4")))

    # Görsel 10 MB limiti (test: 32 bayt) — aşınca dosya silinir
    before = set(p.name for p in tmp_path.iterdir())
    with pytest.raises(ValueError):
        _run(ifl.save_media(_upload(b"\xff\xd8\xff\xe0" + b"\x00" * 40)))
    assert set(p.name for p in tmp_path.iterdir()) == before
    # Video limiti daha geniş (64) → aynı boyut video için geçer
    ok = _run(ifl.save_media(_upload(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 36)))
    assert ok[1] == 48


# ─── Teslim belgesi numune şerhi ─────────────────────────────────────────────

def _pdf_text(pdf: bytes) -> str:
    from pypdf import PdfReader
    return "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(pdf)).pages)


def test_delivery_pdf_sample_notice_band():
    from core.delivery_note import render_delivery_pdf, SAMPLE_NOTICE_TR, SAMPLE_NOTICE_EN
    view = {"document_no": "KRG-2026-00001", "recipient_name": "Test Creator", "type_label": "Kargo",
            "method_label": "Kargo", "dispatched_by": "dogukan", "date": "05.09.2026",
            "items": [{"item_name": "Argan Serum", "quantity": 1, "unit": "adet"}]}
    plain = _pdf_text(render_delivery_pdf(dict(view)))
    assert "NUMUNED" not in plain and "SAMPLE" not in plain
    tr = _pdf_text(render_delivery_pdf(dict(view, sample_notice=True)))
    assert SAMPLE_NOTICE_TR.replace("İ", "").split(" — ")[1] in tr.replace("İ", "")
    assert "NUMUNED" in tr
    en = _pdf_text(render_delivery_pdf(dict(view, sample_notice=True, doc_lang="EN")))
    assert SAMPLE_NOTICE_EN in en
