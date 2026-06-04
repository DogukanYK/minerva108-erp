# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Dijital Kalite Kontrol formu sorularının TEK kaynağı.

Bu liste hem QC sayfasına (templates/qc.html — sunucu context'inden
`window.QC_QUESTIONS` olarak enjekte edilir) hem de izlenebilirlik
görünümüne / PDF-Excel rapora (core/qc_report.py) beslenir.  Böylece soru
metinleri tek yerde tutulur, frontend ile rapor arasında kayma (drift) olmaz.

Soru anahtarları (`q01`, `q02`, …) `Inventory.qc_form_data` JSON'undaki
`checklist` sözlüğünün anahtarlarıyla eşleşir; metinler raporda anahtarın
yerine yazılır.
"""

# id: qc_form_data.checklist anahtarı · text: insan-okur soru metni
QC_QUESTIONS = [
    {"id": "q01", "text": "Ürün kokusu doğru mu?"},
    {"id": "q02", "text": "Likit ürün rengi uygun mu?"},
    {"id": "q03", "text": "Likit ürün şarj no doğru mu?"},
    {"id": "q04", "text": "Şişe rengi ve şekli doğru mu?"},
    {"id": "q05", "text": "Valf rengi ve şekli, kapak doğru mu?"},
    {"id": "q06", "text": "Ön etiket baskısı doğru mu?"},
    {"id": "q07", "text": "Arka etiket baskısı doğru mu?"},
    {"id": "q08", "text": "SKT ve şarj numarası baskısı doğru mu?"},
    {"id": "q09", "text": "Ambalaj fiziksel hasar kontrolü uygun mu?"},
    {"id": "q10", "text": "Ürün sızdırmazlık kontrolü uygun mu?"},
    # ── Kalan sorular buraya eklenir; raporlar ve QC ekranı otomatik yansıtır ──
]

# Hızlı arama için anahtar → metin haritası.
QC_QUESTION_TEXT = {q["id"]: q["text"] for q in QC_QUESTIONS}
