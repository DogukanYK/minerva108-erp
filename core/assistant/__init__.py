# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Minerva Asistan — IMS içinde Claude tabanlı, YALNIZ OKUMA yapan soru-cevap ve
nasıl-yapılır asistanı (Faz 1).

Paket bilinçli olarak boş tutulur: alt modüller (`settings`, `client`,
`prompt`, `tools`, `handlers`, `budget`, `history`, `runner`) ihtiyaç anında
import edilir ki `api_main` import'u Anthropic SDK'yı yüklemesin.

Önbellek disiplini (bkz. runner.build_params): tools → system → messages
sırası; araçlar ada göre sıralı ve her istekte aynı; sistem istemi
DONDURULMUŞ (tarih/isim/panel yok); kullanıcıya özgü bağlam soru
mesajından SONRA `role:"system"` mesajı olarak gider.
"""
