# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Numune Analiz Formu (FR.KK.01) — tek kaynak tanımlar.

Kağıt formdaki varsayılan özellik satırları + form kodu + sonuç etiketleri
burada yaşar (qc_questions.py kalıbı). Sayfa route'u template'e enjekte eder,
router varsayılan olarak kullanır, PDF üreticisi altbilgi/rozet için okur.
Form revize olursa (Rev:1) FORM_CODE güncellenir; eski kayıtlar satırlarındaki
form_code snapshot'ıyla eski revizyonu basmaya devam eder.
"""

FORM_CODE = "FR.KK.01 · Yayın trh. 31.07.2025 · Rev: 0"

# Kağıt formdaki sabit satırlar — kullanıcı forma ek satır da ekleyebilir.
SAMPLE_PROPERTIES = [
    {"key": "gorunum", "label": "GÖRÜNÜM", "spec": ""},
    {"key": "koku",    "label": "KOKU",    "spec": "KARAKTERİSTİK"},
    {"key": "renk",    "label": "RENK",    "spec": ""},
    {"key": "ph",      "label": "PH",      "spec": ""},
]

RESULT_LABELS = {
    "uygun":        "UYGUNDUR",
    "uygun_degil":  "UYGUN DEĞİLDİR",
    None:           "BEKLEMEDE",
}


def result_label(result) -> str:
    return RESULT_LABELS.get(result, RESULT_LABELS[None])
