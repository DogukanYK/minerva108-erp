"""
Marka anahtarı testleri (core/brands.py — brand_key / brand_label / product_brand).

Elle girilmiş ürün adlarında aynı marka 'MİNERVA-108', 'Minerva108',
'SERENİDA', 'SER5ENİDA' gibi yazımlarla yaşıyor; raporlar tek çipte
toplamalı.  Dolap adı / lot öneki sözleşmesi (brand_of / canonical_brand /
lot_code) DEĞİŞMEMELİ — son test bunu kilitler.
"""
import pytest

from core.brands import (brand_key, brand_label, brand_of, canonical_brand, lot_code,
                         product_brand)


@pytest.mark.parametrize("name,key,label", [
    ("MİNERVA-108 X Krem", "minerva", "Minerva 108"),
    ("Minerva108 Şampuan", "minerva", "Minerva 108"),
    ("minerva-108 tonik", "minerva", "Minerva 108"),
    ("MİNERVA 108 ALTIN SERİ SAÇ KREMİ", "minerva", "Minerva 108"),
    ("Minerva 108 Red Clover Day Cream", "minerva", "Minerva 108"),
    ("SERENIDA Tonik", "serenida", "Serenida"),
    ("SERENİDA YÜZ KREMİ", "serenida", "Serenida"),
    ("SER5ENİDA El Kremi", "serenida", "Serenida"),        # rakam araya kaçmış
    ("Serinida Duş Jeli", "serenida", "Serenida"),         # tek harf yazım hatası
    ("SERENİDE DUŞ JELİ", "serenida", "Serenida"),
    ("Evanira Fresh Shower Gel", "evanira", "Evanira"),
    ("EVANIRA SHOWER GEL", "evanira", "Evanira"),
])
def test_known_brand_variants(name, key, label):
    assert brand_key(name) == key
    assert brand_label(name) == label


def test_unknown_brand_falls_back_to_first_word():
    assert brand_key("ALTIN SERİ Tonik") == "altin"
    assert brand_label("ALTIN SERİ Tonik") == "ALTIN"     # brand_of davranışı


def test_fuzzy_guard_does_not_swallow_similar_words():
    # difflib oranı 0.86 ama iki harf farkı — Minerva'ya bağlanmamalı
    assert brand_key("Mineral Su") == "mineral"
    assert brand_label("Mineral Su") == "Mineral"


def test_empty_and_numeric_names():
    assert brand_key("") == "diger" and brand_key(None) == "diger"
    assert brand_label("") == "—"
    assert brand_key("108 Krem") == "diger"


def test_product_brand_prefers_known_alias():
    # Hedef ürün adı markasız ama reçete adı biliniyor → reçeteden
    assert product_brand("ALTIN SERİ Tonik", "MİNERVA 108 ALTIN SERİ TONİK") == "Minerva 108"
    # İkisi de biliniyorsa hedef ürün kazanır
    assert product_brand("Serenida Tonik", "Minerva Tonik") == "Serenida"
    # Hiçbiri bilinmiyorsa hedefin ilk kelimesi
    assert product_brand("Foo Krem", "Bar Krem") == "Foo"
    assert product_brand(None, "Evanira Losyon") == "Evanira"


def test_legacy_contract_unchanged():
    # Şahit numune dolabı / lot öneki bu fonksiyonlara bağlı — davranış aynen
    assert brand_of("MİNERVA-108 Krem") == "MİNERVA-108"
    assert canonical_brand("MİNERVA Krem") == "Minerva 108"
    assert canonical_brand("MİNERVA-108 Krem") == "MİNERVA-108"
    assert lot_code("Serenida Tonik") == "SR"
