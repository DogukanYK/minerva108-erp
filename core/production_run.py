# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""Üretim defter yazımı — `start_production`'dan TAŞINDI (davranış birebir).

İki yarı ayrıdır ki B2B sipariş partisi (core/b2b_orders) başlangıçta YALNIZ
tüketimi, tamamlamada YALNIZ bitmiş ürünü yazabilsin; günlük üretim
(`POST /api/production`) ikisini aynı transaction'da arka arkaya çağırır.

  • recorder(...)          → ProductionConsumption dökümü yazan geri çağırım
  • write_consumption(...) → kart stoğu düşümü + reçete satırı başına Output
  • write_output(...)      → showroom / şahit lotları + RetentionSample + TEK Input

NOT SÖZLEŞMELERİ (core/production_cancel ve trace_lot okur) burada aynen
korunur — metni değiştirme.  Hiçbiri commit ETMEZ.
"""
from core.brands import cabinet_location, cabinet_of
from core.consumption import _kind as consumption_kind
from core.retention import retention_until
from database import Inventory, ProductionConsumption, RetentionSample, RetentionSampleMovement, Transaction


def recorder(db, prod, pl, rec_domain):
    """Tüketim/çıktı dökümü satırı yazan fonksiyonu döndür (prod.id'ye bağlı)."""
    def _consumption(kind, tx, *, item_id, recipe_item_id=None, lot=None,
                     lot_number=None, quantity, factor=1.0, unit=None, phase=None):
        """Defter satırının dökümü (core/production_cancel bunu okur).
        İlişkiyle bağlanır → satır başına flush gerekmez."""
        sup = lot.supplier if (lot is not None and lot.supplier_id) else None
        if sup is None and kind != "output":
            card = pl.cards.get(item_id)
            sup = card.supplier if card is not None and card.supplier_id else None
        pc = ProductionConsumption(
            production_id=prod.id, kind=kind, recipe_item_id=recipe_item_id,
            item_id=item_id, lot_number=lot_number,
            supplier_id=sup.id if sup else None, supplier_name=sup.name if sup else None,
            quantity=quantity, factor=factor, unit=unit, phase=phase,
            source="live", domain=rec_domain)
        pc.transaction = tx
        if lot is not None:
            pc.inventory = lot
        db.add(pc)
    return _consumption


def write_consumption(db, pl, recipe, *, produced_lot, actor, sel_lang_label, record):
    """Planın her parçası için kart stoğunu düş ve Output yaz (+ döküm)."""

    # ── Stok düş + Output transaction kaydet ───────────────────────────
    # Kart stoğu seçim (pick) başına TAM pay kadar düşer (current_stock
    # kaynak-of-truth).  Output'lar `pl.segments()` sırasıyla: reçete
    # satırı sırası, her tahsis (lot) ayrı Output — Transaction.lot_number
    # = KAYNAK lot.  Aynı kart reçetede iki satırdaysa (ör. su A ve C
    # fazında) her reçete satırı eskisi gibi kendi Output'unu/fazını alır.
    # Transaction.item_id = FİİLEN düşülen kart (etikette dil kardeşi,
    # kaynak seçiminde gruptaki diğer kart); döküm satırı reçetedeki kartı
    # `recipe_item_id`'de ayrıca taşır.
    # NOT SÖZLEŞMESİ: not "Üretim tüketimi — Reçete: {ad} | " ile başlar
    # ve "Üretim Lot: {lot}" ile BİTER — core/production_cancel eski
    # üretimi bu imzayla kurar, trace_lot damgaya düşer.  Kaynak kart
    # reçetedekinden farklıysa " | Kaynak kart: … (reçetede: …)" araya
    # ("Tedarikçi:"den önce) girer.
    for ln in pl.lines:
        for pick in ln.chosen:
            item = pl.cards[pick.item_id]
            item.current_stock = round(item.current_stock - pick.quantity, 6)
    waste_pct = recipe.waste_percentage or 0
    for seg in pl.segments():
        ln, item = seg.line, pl.cards[seg.pick.item_id]
        fire_note = (f" | %{waste_pct} fire dahil, brüt girdi"
                     if not ln.is_ambalaj and waste_pct > 0 else "")
        note = (f"Üretim tüketimi — Reçete: {recipe.name}{fire_note} | "
                f"Dil: {sel_lang_label}")
        if ln.kind != "label" and item.id != ln.recipe_item_id:
            note += (f" | Kaynak kart: {item.name} "
                     f"(reçetede: {pl.names.get(ln.recipe_item_id, '—')})")
        lot = seg.lot
        if lot is not None:
            # Hammadde — seçilen/FIFO lot
            lot.quantity = round((lot.quantity or 0) - seg.quantity, 6)
            sup = lot.supplier.name if lot.supplier else "—"
            smp = " (numune)" if lot.is_sample else ""
            note += (f" | Tedarikçi: {sup}{smp} | Kaynak Lot: {lot.lot_number} | "
                     f"Üretim Lot: {produced_lot}")
        elif seg.uncovered:
            # Lot toplamı payı karşılamadı (eksik lot verisi) — artık toplam
            # stoktan; audit bütünlüğü için yine Output (toplam = pay).
            note += f" | (lot kaydı dışı, toplam stoktan) | Üretim Lot: {produced_lot}"
        else:
            # Ambalaj/etiket ya da hiç lotu olmayan hammadde — toplam stoktan
            note += f" | Üretim Lot: {produced_lot}"
        tx = Transaction(
            item_id=item.id, transaction_type="Output", quantity=seg.quantity,
            lot_number=lot.lot_number if lot is not None else None,
            notes=note, performed_by=actor,
        )
        db.add(tx)
        record(consumption_kind(item), tx, item_id=item.id,
               recipe_item_id=ln.recipe_item_id, lot=lot,
               lot_number=lot.lot_number if lot is not None else None,
               quantity=seg.quantity, factor=ln.factor, unit=item.unit,
               phase=seg.phase)


def write_output(db, recipe, *, target_item_row, produced_quantity, witness_quantity,
                 produced_lot, actor, sel_lang_label, prod, now, retention_months, record):
    """Bitmiş ürün: showroom + şahit lotları, dolap kaydı, TEK Input, stok artışı.

    `retention_months` — (raf ömrü, ek süre) döndüren çağrılabilir; yalnız şahit
    varken okunur.  Dönüş: (witness_qty, showroom_qty, brand).
    """
    # ── Şahit numune ayrımı ─────────────────────────────────────────────
    # Üretilen X adetin Y'si "Şahit Numune Dolabı"na (marka bazlı), kalanı
    # "Showroom"a ayrılır.  Item.current_stock yine X kadar artar (hepsi
    # stoktadır, sadece konum farklı).  Witness=0 ise tek lot, eski davranış.
    target_item_obj = target_item_row      # yukarıda kilitlenmiş satır
    witness_qty = max(0.0, float(witness_quantity or 0))
    if witness_qty > produced_quantity:
        witness_qty = produced_quantity        # taşmayı kırp
    showroom_qty = round(produced_quantity - witness_qty, 6)
    prod.witness_quantity = witness_qty          # şahit numuneye ayrılan adet

    # Marka/dolap adı — TEK KAYNAK core/brands.py (eskiden burada hard-coded
    # bir harita vardı; üç ayrı marka implementasyonundan biriydi).
    target_name = target_item_obj.name if target_item_obj else ""
    brand = cabinet_of(target_name) if target_name else ""
    witness_location = cabinet_location(target_name) if target_name else "Şahit Numune Dolabı"

    # Üretilen lot APPROVED + qc_required=True olarak yaratılır:
    # → Stok hemen artar (patron şartı: üretim biter bitmez stoğa düşmeli)
    # → QC sayfası bu lotu görür ve inceler (qc_required=True flag'i ile)
    # → QC onaylarsa qc_required=False, status APPROVED kalır
    # → QC reddederse status=REJECTED + stok düşülür + Adjustment audit
    if recipe.target_item_id:
        finished_rows = []                     # [(Inventory, adet)] — output dökümü
        # 1) Showroom lot'u — kalan kısım
        if showroom_qty > 0:
            showroom_inv = Inventory(
                item_id=recipe.target_item_id,
                lot_number=produced_lot,
                quantity=showroom_qty,
                location="Showroom",
                status="APPROVED",
                received_by=actor,
                qc_required=True,
                domain=(recipe.domain or "cosmetics"),   # Faz 3 — reçetenin paneli
            )
            db.add(showroom_inv)
            finished_rows.append((showroom_inv, showroom_qty))
        # 2) Şahit numune lot'u — varsa.  Stok kaynağı burasıdır (adet
        #    current_stock içinde sayılmaya devam eder); RetentionSample
        #    bunun ÜSTÜNE dolap yönetimini ekler (raf/göz, saklama süresi,
        #    çıkış geçmişi) ve inventory_id ile bu satıra bağlanır.
        if witness_qty > 0:
            witness_inv = Inventory(
                item_id=recipe.target_item_id,
                lot_number=f"{produced_lot}-S",       # "-S" suffix = Şahit
                quantity=witness_qty,
                location=witness_location,
                status="APPROVED",
                received_by=actor,
                qc_required=True,
                domain=(recipe.domain or "cosmetics"),
            )
            db.add(witness_inv)
            finished_rows.append((witness_inv, witness_qty))
            db.flush()                                 # inventory_id gerekli
            shelf_life_m, extra_m = retention_months()
            retention_row = RetentionSample(
                inventory_id=witness_inv.id,
                production_history_id=prod.id,
                item_id=recipe.target_item_id,
                item_name=target_name,
                lot_number=witness_inv.lot_number,
                brand=brand or "Genel",
                quantity=witness_qty,
                initial_quantity=witness_qty,
                unit=(target_item_obj.unit if target_item_obj else None),
                produced_at=now,
                retention_until=retention_until(now, shelf_life_m, extra_m),
                status="stored",
                source="production",
                placed_by=actor,
                domain=(recipe.domain or "cosmetics"),
            )
            db.add(retention_row)
            db.flush()
            db.add(RetentionSampleMovement(
                sample_id=retention_row.id, movement_type="giris",
                quantity=witness_qty, note=f"Üretim — Lot {produced_lot}",
                performed_by=actor))
        # Tek toplam Input transaction'ı — audit'te bölünme not olarak yazılır
        split_note = (f" | Showroom: {showroom_qty}, Şahit: {witness_qty} ({brand})"
                      if witness_qty > 0 else "")
        input_tx = Transaction(
            item_id=recipe.target_item_id,
            lot_number=produced_lot,
            transaction_type="Input",
            quantity=produced_quantity,
            notes=(f"Üretim çıktısı — Reçete: {recipe.name} | "
                   f"Dil: {sel_lang_label} | Lot: {produced_lot}{split_note}"),
            performed_by=actor,
        )
        db.add(input_tx)
        # Her bitmiş satır (showroom, -S) ayrı output satırı; ikisi de
        # aynı tek Input'u taşır.
        for inv_row, qty_row in finished_rows:
            record("output", input_tx, item_id=recipe.target_item_id,
                   lot=inv_row, lot_number=inv_row.lot_number, quantity=qty_row,
                   unit=(target_item_obj.unit if target_item_obj else None))
        # Item.current_stock = TOPLAM artar (witness de stokta sayılır —
        # dolaptaki numuneler satılabilir stoktan DÜŞMEZ, bilinçli karar).
        # Satır yukarıda lot sayacı için zaten kilitlendi, yeniden sorgulanmaz.
        if target_item_row:
            target_item_row.current_stock = round(
                (target_item_row.current_stock or 0) + produced_quantity, 6
            )
    return witness_qty, showroom_qty, brand
