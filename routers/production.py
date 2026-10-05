# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Production router — manufacturing workflows + Quality Control (QA).
"""
import io
import re

from fastapi import APIRouter, Depends, BackgroundTasks
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.orm import Session
from pydantic import BaseModel, Field
from typing import Optional

from database import (
    to_tr, AppSetting,
    get_db, Item, Recipe, ProductionHistory, Inventory, Transaction,
    RetentionSample, RetentionSampleMovement,
)
from core import lots
from core.auth import get_current_user
from core.brands import cabinet_location, cabinet_of
from core.permissions import require_permission
from core.notifications import notify_low_stock
from core.domain import active_domain
from core.retention import (CFG_EXTRA, CFG_SHELF_LIFE, DEFAULT_EXTRA_MONTHS,
                            DEFAULT_SHELF_LIFE_MONTHS, retention_until)


def retention_cfg_months(db: Session) -> tuple:
    """(raf ömrü, ek süre) — AppSetting'ten; bozuk/eksikse varsayılan.

    routers/retention.py'deki `_cfg_months` ile aynı okumayı yapar; ikisi de
    core/retention.py'deki sabitleri kullanır (tek kaynak).
    """
    def _read(key, default):
        row = db.query(AppSetting).filter(AppSetting.key == key).first()
        try:
            return int(float(row.value)) if row and row.value not in (None, "") else default
        except (TypeError, ValueError):
            return default
    return _read(CFG_SHELF_LIFE, DEFAULT_SHELF_LIFE_MONTHS), _read(CFG_EXTRA, DEFAULT_EXTRA_MONTHS)

router = APIRouter(prefix="/api", tags=["production"])


# ─── Schemas ────────────────────────────────────────────────────────────────

class ProductionCreateRequest(BaseModel):
    recipe_id: int
    produced_quantity: float = Field(..., gt=0, le=1_000_000)
    # Etiket dili — 'TR' / 'EN'.  Reçetedeki dile özel etiketlerden bu dile
    # ait olan stoktan düşülür, diğeri atlanır.  Varsayılan TR.
    label_language: Optional[str] = Field("TR", max_length=8)
    # Şahit numune adedi — üretilen X adetten kaçı şahit numune dolabına
    # ayrılacak.  Varsayılan 2.  Kalan X-witness adet showroom'a gider.
    witness_quantity: Optional[float] = Field(0, ge=0)
    # Lot numarası — boş bırakılırsa sunucu üretir (MNR006).  Dolu gelirse
    # normalize edilir; AYNI ürün için kullanılmışsa 400 + öneri döner.
    lot_number: Optional[str] = Field(None, max_length=100)
    # Faz 2 — her hammadde için hangi lot/tedarikçiden tüketileceği seçimi:
    # {item_id: inventory_id}.  Seçilmeyen kalemler FIFO (en eski APPROVED lot)
    # ile düşer.  Seçilen lot yetersizse üretim NET HATA ile durur (sessizce
    # başka lottan düşmez).  Ambalaj/etiket bu seçimden muaftır (toplam stok).
    ingredient_lot_choices: Optional[dict] = None


class QCActionRequest(BaseModel):
    notes:  str = Field(..., max_length=2000)
    status: str = Field(..., max_length=20)  # 'APPROVED' veya 'REJECTED'


class QCFormRequest(BaseModel):
    """Digital QC form — full checklist + lab results + decision."""
    status:      str  = Field(..., max_length=20)
    checklist:   dict = Field(...)            # { "q01": "Evet", ... } — endpoint validation yapıyor
    lab_ml:      Optional[float] = Field(None, ge=0, le=10_000)
    lab_density: Optional[float] = Field(None, ge=0, le=100)
    lab_color:   Optional[str]   = Field(None, max_length=50)
    notes:       Optional[str]   = Field("",   max_length=2000)


# ─── Production Endpoints ────────────────────────────────────────────────────

@router.get("/production")
def list_production_history(db: Session = Depends(get_db), domain: str = Depends(active_domain)):
    rows = (db.query(ProductionHistory)
            .filter(ProductionHistory.domain == domain)
            .order_by(ProductionHistory.id.desc()).limit(100).all())
    return [
        {
            "id": r.id,
            "recipe_name": r.recipe_name,
            "target_item_name": r.target_item_name,
            "produced_quantity": r.produced_quantity,
            "produced_at": to_tr(r.produced_at).strftime("%d.%m.%Y %H:%M") if r.produced_at else "",
        }
        for r in rows
    ]


def _mark_retention_rejected(db: Session, inv: Inventory, actor: str, reason: str) -> None:
    """QC'de reddedilen şahit lotunun dolap kaydını kapat.

    Stok matematiği ÇAĞIRANA aittir (Adjustment orada yazılıyor) — burada
    yalnız dolap görünürlüğü düzeltilir, ikinci kez stok DÜŞÜLMEZ.  Yoksa
    reddedilmiş bir numune dolapta duruyormuş gibi görünürdü.
    """
    row = (db.query(RetentionSample)
           .filter(RetentionSample.inventory_id == inv.id,
                   RetentionSample.is_active == True).first())      # noqa: E712
    if not row:
        return
    qty = row.quantity or 0.0
    row.qc_status = "rejected"
    row.quantity = 0.0
    row.status = "destroyed"
    db.add(RetentionSampleMovement(
        sample_id=row.id, movement_type="duzeltme", quantity=qty,
        note=f"QC reddi — {reason}"[:500], performed_by=actor))


# ─── Lot numarası önerisi ───────────────────────────────────────────────────
# DİKKAT: bu route "/production/{prod_id}"nin ÜSTÜNDE kalmalı — altına
# taşınırsa "next-lot" prod_id sanılıp int'e çevrilmeye çalışılır → 422.

@router.get("/production/next-lot")
def suggest_next_lot(
    recipe_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("production", "create")),
    domain: str = Depends(active_domain),
):
    """Bu reçetenin hedef ürünü için bir sonraki lot numarası önerisi.

    Sayaç ürün bazlıdır: 'bu üründen en son MNR005'i ürettiniz, bu MNR006
    olmalı'.  Kullanıcı öneriyi düzenleyebilir (üretim POST'unda gönderilir).
    """
    recipe = (db.query(Recipe)
              .filter(Recipe.id == recipe_id, Recipe.domain == domain).first())
    if not recipe:
        return JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})
    if not recipe.target_item_id:
        return {"lot_number": "", "message": "Bu reçetenin hedef ürünü yok — "
                                             "lot numarası üretimde otomatik atanır."}
    item = db.query(Item).filter(Item.id == recipe.target_item_id).first()
    if not item:
        return JSONResponse(status_code=404, content={"detail": "Hedef ürün bulunamadı."})
    out = lots.suggest(db, item)
    out["cabinet"] = cabinet_of(item.name)
    return out


# ─── Üretim föyü (production sheet) ─────────────────────────────────────────

# İsim/varyasyondan ml değeri çeken yardımcı core/consumption.parse_ml'e taşındı
# (Satın Alma Planı da ürün boyunu aynı kuralla okuyor).  Bu satır RE-EXPORT
# SHIM'idir — eski ad buradan çağrılıyor.
from core.consumption import parse_ml as _parse_ml  # noqa: E402


def _build_production_sheet(prod: ProductionHistory, db: Session) -> Optional[dict]:
    """
    Üretim föyünü reçeteden yeniden hesaplar — üretim öncesi canlı önizlemenin
    aynısı: net / brüt(fireli) / fire.  Reçete silinmişse None döner.
    """
    recipe = db.query(Recipe).filter(Recipe.id == prod.recipe_id).first()
    if not recipe:
        return None

    multiplier   = prod.produced_quantity / (recipe.output_quantity or 1.0)
    waste        = recipe.waste_percentage or 0.0
    waste_factor = 1.0 + waste / 100.0

    # % bileşim hammadde net'i üzerinden — Excel föyündeki "% MİKTAR" mantığı
    hammadde_qty_total = 0.0
    ings = []
    for ing in recipe.ingredients:
        item = db.query(Item).filter(Item.id == ing.item_id).first()
        if not item:
            continue
        is_amb = (item.category == "Ambalaj")
        if not is_amb:
            hammadde_qty_total += ing.quantity
        ings.append((ing, item, is_amb))

    rows = []
    net_total = gross_total = 0.0
    for ing, item, is_amb in ings:
        factor = 1.0 if is_amb else waste_factor
        net    = round(ing.quantity * multiplier, 6)
        gross  = round(net * factor, 6)
        pct    = (round(ing.quantity / hammadde_qty_total * 100, 4)
                  if (not is_amb and hammadde_qty_total) else None)
        net_total   += net
        gross_total += gross
        rows.append({
            "phase":      ing.phase or "",
            "item_name":  item.name,
            "unit":       ing.unit or item.unit or "",
            "percent":    pct,
            "net":        net,
            "gross":      gross,
            "is_ambalaj": is_amb,
        })

    target = db.query(Item).filter(Item.id == recipe.target_item_id).first() if recipe.target_item_id else None
    bottle_ml = _parse_ml(
        getattr(target, "variation_name", None) if target else None,
        target.name if target else None,
        prod.target_item_name,
    )

    return {
        "id":               prod.id,
        "recipe_id":         recipe.id,
        "recipe_name":       prod.recipe_name or recipe.name,
        "target_item_name":  prod.target_item_name or (target.name if target else ""),
        "produced_quantity": prod.produced_quantity,
        "produced_at":       to_tr(prod.produced_at).strftime("%d.%m.%Y %H:%M") if prod.produced_at else "",
        "produced_at_date":  to_tr(prod.produced_at).strftime("%d.%m.%Y") if prod.produced_at else "",
        "produced_by":       prod.produced_by or "",
        "lot_number":        prod.lot_number or "",
        "bottle_ml":         bottle_ml,
        "waste_percentage":  round(waste, 2),
        "production_notes":  recipe.production_notes or "",
        "ingredients":       rows,
        "totals": {
            "net":   round(net_total, 4),
            "gross": round(gross_total, 4),
            "fire":  round(gross_total - net_total, 4),
        },
    }


@router.get("/production/{prod_id}")
def production_detail(
    prod_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    """Tek üretim kaydının föyü — canlı önizleme tarzı brüt/fireli döküm."""
    prod = db.query(ProductionHistory).filter(ProductionHistory.id == prod_id).first()
    if not prod:
        return JSONResponse(status_code=404, content={"detail": "Üretim kaydı bulunamadı."})
    sheet = _build_production_sheet(prod, db)
    if sheet is None:
        return JSONResponse(status_code=409, content={
            "detail": "Bu üretimin reçetesi silinmiş — föy yeniden hesaplanamıyor."
        })
    return sheet


@router.get("/production/{prod_id}/export")
def production_export(
    prod_id: int,
    db: Session = Depends(get_db),
    _: dict = Depends(get_current_user),
):
    """Üretim föyünü .xlsx olarak indir — lab Excel formatının birebir aynısı."""
    prod = db.query(ProductionHistory).filter(ProductionHistory.id == prod_id).first()
    if not prod:
        return JSONResponse(status_code=404, content={"detail": "Üretim kaydı bulunamadı."})
    sheet = _build_production_sheet(prod, db)
    if sheet is None:
        return JSONResponse(status_code=409, content={"detail": "Reçete silinmiş — föy üretilemiyor."})

    import openpyxl
    from openpyxl.styles import Font, Alignment, Border, Side, PatternFill

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Üretim Föyü"

    bold      = Font(bold=True)
    big       = Font(bold=True, size=12)
    thin      = Side(style="thin", color="999999")
    box       = Border(left=thin, right=thin, top=thin, bottom=thin)
    hdr_fill  = PatternFill("solid", fgColor="232E6E")
    hdr_font  = Font(bold=True, color="FFFFFF")
    wrap      = Alignment(wrap_text=True, vertical="top")

    t = sheet["totals"]
    waste = sheet["waste_percentage"]

    # ── Başlık bloğu ────────────────────────────────────────────────────────
    ws["A1"] = "YAPILMASI GEREKEN ADET:";          ws["B1"] = sheet["produced_quantity"]
    ws["C1"] = "ŞİŞE ML :";                         ws["D1"] = sheet["bottle_ml"] or ""
    ws["A2"] = "YAPILMASI GEREKEN MİKTAR (GR) :";   ws["B2"] = t["net"]
    ws["A3"] = f"% {waste:g} FİRE (GR) :";          ws["B3"] = t["fire"]
    ws["A4"] = "YAPILMASI GEREKEN TOPLAM MİKTAR:";  ws["B4"] = t["gross"]
    ws["D4"] = f"TARİH: {sheet['produced_at_date']}"
    for r in range(1, 5):
        ws[f"A{r}"].font = bold
        ws[f"C{r}"].font = bold
    ws["A5"] = sheet["target_item_name"]
    ws["A5"].font = big

    # ── Bileşen tablosu başlığı ─────────────────────────────────────────────
    hdr_row = 6
    headers = ["FAZ", "HAMMADDE İSİM", "% MİKTAR", f"MİKTAR (GR) — {t['gross']:g}"]
    for ci, h in enumerate(headers, start=1):
        c = ws.cell(row=hdr_row, column=ci, value=h)
        c.font = hdr_font; c.fill = hdr_fill; c.border = box
        c.alignment = Alignment(horizontal="center")

    # ── Satırlar ────────────────────────────────────────────────────────────
    row = hdr_row + 1
    for ing in sheet["ingredients"]:
        ws.cell(row=row, column=1, value=ing["phase"]).border = box
        ws.cell(row=row, column=2, value=ing["item_name"]).border = box
        pct_cell = ws.cell(row=row, column=3,
                           value=(round(ing["percent"], 3) if ing["percent"] is not None else ""))
        pct_cell.border = box
        amt_cell = ws.cell(row=row, column=4, value=round(ing["gross"], 4))
        amt_cell.border = box
        row += 1

    # ── TOPLAM satırı ───────────────────────────────────────────────────────
    ws.cell(row=row, column=2, value="TOPLAM").font = bold
    ws.cell(row=row, column=3, value=100).font = bold
    ws.cell(row=row, column=4, value=round(t["gross"], 4)).font = bold
    for ci in range(1, 5):
        ws.cell(row=row, column=ci).border = box
    row += 2

    # ── YAPILIŞI ────────────────────────────────────────────────────────────
    ws.cell(row=row, column=1, value="YAPILIŞI").font = bold
    row += 1
    notes_cell = ws.cell(row=row, column=1, value=sheet["production_notes"] or "—")
    notes_cell.alignment = wrap
    ws.merge_cells(start_row=row, start_column=1, end_row=row + 6, end_column=4)

    # ── Kolon genişlikleri ──────────────────────────────────────────────────
    ws.column_dimensions["A"].width = 10
    ws.column_dimensions["B"].width = 38
    ws.column_dimensions["C"].width = 14
    ws.column_dimensions["D"].width = 22

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    safe_name = re.sub(r'[^A-Za-z0-9_-]+', '_', sheet["target_item_name"] or "uretim").strip("_")
    filename  = f"uretim_foyu_{prod.id}_{safe_name}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


_EPS = 1e-9   # kayan nokta toleransı (stok karşılaştırmaları)


class _LotChoiceError(Exception):
    """Seçilen lot bulunamadı / yetersiz — üretim net hatayla durur."""


def _plan_lot_allocation(db, item, gross_qty, chosen_inv_id):
    """
    Bir hammadde kalemi için gross_qty'yi hangi Inventory lot(lar)ından
    düşeceğimizi planlar.  Inventory satırları with_for_update ile kilitlenir
    (aynı anda iki üretim aynı lottan düşmesin).

    Dönüş: (allocations, uncovered)
      • allocations: [(inventory_row, take_qty), …]
      • uncovered:   lot kaydı bulunmayan ama current_stock'tan düşülecek artık
                     (eski/lotsuz kalemlerde geri uyum — üretimi engellemez)

    Kurallar:
      • chosen_inv_id verilmişse O lottan düşülür; lot yok/uygun değil ya da
        miktarı yetersizse _LotChoiceError fırlatır (sessizce başka lota geçmez).
      • Seçim yoksa FIFO: en eski APPROVED lotlardan sırayla düşülür.
    """
    # NOT: with_for_update() ile joinedload(supplier) BİRLEŞTİRİLMEZ —
    # PostgreSQL "FOR UPDATE cannot be applied to the nullable side of an
    # outer join" hatası verir.  Sadece inventory satırları kilitlenir;
    # supplier (gerekiyorsa) tüketim aşamasında lazy yüklenir.
    lots = (
        db.query(Inventory)
        .filter(
            Inventory.item_id == item.id,
            Inventory.status == "APPROVED",
            Inventory.quantity > 0,
            Inventory.is_sample == False,   # noqa: E712 — numune üretimde KULLANILAMAZ (2026-08-24)
        )
        .order_by(Inventory.created_at.asc(), Inventory.id.asc())
        .with_for_update()
        .all()
    )

    if chosen_inv_id:
        lot = next((l for l in lots if l.id == int(chosen_inv_id)), None)
        if not lot:
            raise _LotChoiceError(
                f"'{item.name}' için seçilen lot bulunamadı veya stokta uygun değil."
            )
        if (lot.quantity or 0) + _EPS < gross_qty:
            raise _LotChoiceError(
                f"'{item.name}' için seçilen lot ({lot.lot_number}) yetersiz: "
                f"{round(lot.quantity, 4)} {item.unit or ''} var, "
                f"{round(gross_qty, 4)} {item.unit or ''} gerekiyor."
            )
        return [(lot, gross_qty)], 0.0

    # FIFO — en eski lotlardan tüket.  Motor TEK KAYNAK: core/stock_lots.
    # (Lotlar yukarıda zaten kilitlendi; tekrar kilitlemeye gerek yok.)
    from core.stock_lots import plan_fifo
    return plan_fifo(db, item, gross_qty, exclude_samples=True, lock=False)


@router.post("/production", status_code=201)
def start_production(
    data: ProductionCreateRequest,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("production", "create")),
):
    recipe = db.query(Recipe).filter(Recipe.id == data.recipe_id).first()
    if not recipe:
        return JSONResponse(status_code=404, content={"detail": "Reçete bulunamadı."})
    if data.produced_quantity <= 0:
        return JSONResponse(status_code=400, content={"detail": "Üretim miktarı sıfırdan büyük olmalıdır."})

    actor = current_user.get("full_name") or current_user.get("username") or "—"
    multiplier = data.produced_quantity / recipe.output_quantity

    # ── Brüt Girdi Hesabı ──────────────────────────────────────────────────
    # Fire (waste) EKLEMELI çalışır: brüt_girdi = net_miktar × (1 + fire% / 100)
    # Örnek: 50 ml hammadde + %10 fire = 55 ml stoktan düşülür.
    # Ambalaj bileşenlerine fire uygulanmaz (reçete mantığıyla tutarlı).
    waste_factor = 1.0 + (recipe.waste_percentage or 0.0) / 100.0

    # ── Seçilen etiket dili ─────────────────────────────────────────────────
    sel_lang = "EN" if (data.label_language or "").strip().upper().startswith("EN") else "TR"
    sel_lang_label = "İngilizce" if sel_lang == "EN" else "Türkçe"

    try:
        # ── Önce tüm brüt miktarları hesapla ve stok kontrolü yap ──────────
        ing_plan = []          # (ing, item, gross_qty, is_ambalaj, allocations, uncovered)
        label_warnings = []    # seçilen dilde etiketi olmayan kalemler
        processed_groups = set()  # aynı label_group iki kez tüketilmesin
        # Hammadde lot/tedarikçi seçimleri — {item_id: inventory_id}
        lot_choices = {}
        for _k, _v in (data.ingredient_lot_choices or {}).items():
            try:
                lot_choices[int(_k)] = int(_v)
            except (TypeError, ValueError):
                continue
        for ing in recipe.ingredients:
            item = db.query(Item).filter(Item.id == ing.item_id).with_for_update().first()
            if not item:
                db.rollback()
                return JSONResponse(status_code=404, content={"detail": f"Hammadde bulunamadı (ID: {ing.item_id})."})

            # ── Etiket dil çözümü ──────────────────────────────────────────
            # Malzeme dile özel etiketse: seçilen dile uygun kardeşe in.
            #   • dili seçilen dile eşit → aynen kullan
            #   • farklı → aynı label_group'ta seçilen dildeki kardeşi bul
            #   • kardeş yok → uyar + atla (üretim durmaz)
            #   • reçetede iki kardeş varsa ikincisini atla (çift düşmesin)
            if item.language and item.label_group:
                if item.label_group in processed_groups:
                    continue
                processed_groups.add(item.label_group)
                if item.language != sel_lang:
                    sibling = (
                        db.query(Item)
                        .filter(Item.label_group == item.label_group,
                                Item.language == sel_lang,
                                Item.is_active == True)
                        .with_for_update()
                        .first()
                    )
                    if not sibling:
                        label_warnings.append(item.name)
                        continue   # bu dilde etiket tanımlı değil — atla
                    item = sibling

            is_ambalaj = (item.category == "Ambalaj")
            factor     = 1.0 if is_ambalaj else waste_factor   # ambalaja fire uygulanmaz
            gross_qty  = round(ing.quantity * multiplier * factor, 6)

            # ── Lot/tedarikçi tahsisi (yalnızca hammadde) ──────────────────
            # Ambalaj/etiket toplam stoktan düşer (allocations=None).  Hammadde
            # için seçili lot ya da FIFO planlanır; seçili lot yetersizse net hata.
            allocations, uncovered = None, 0.0
            if not is_ambalaj:
                try:
                    allocations, uncovered = _plan_lot_allocation(
                        db, item, gross_qty, lot_choices.get(item.id)
                    )
                except _LotChoiceError as e:
                    db.rollback()
                    return JSONResponse(status_code=400, content={"detail": str(e)})
            ing_plan.append((ing, item, gross_qty, is_ambalaj, allocations, uncovered))

        for ing, item, gross_qty, is_ambalaj, _alloc, _unc in ing_plan:
            if item.current_stock < gross_qty:
                db.rollback()
                fire_note = "" if is_ambalaj else f" (%{recipe.waste_percentage or 0} fire dahil)"
                return JSONResponse(status_code=400, content={
                    "detail": f"'{item.name}' için yeterli stok yok. "
                              f"Gereken: {gross_qty} {item.unit}{fire_note}, "
                              f"Mevcut: {item.current_stock} {item.unit}"
                })

        # ── Lot numarası şimdiden üret — tüm transaction notlarına stamp atılır
        #
        # Hedef ürün satırı BURADA kilitlenir (eskiden çıktı yazılırken, aşağıda
        # kilitleniyordu).  Sebep: lot sayacı bu satırda tutuluyor — kilit
        # olmadan aynı ürünün iki eşzamanlı üretimi aynı numarayı alırdı.
        # Kilit sırası korunuyor: malzeme item/inventory satırları yukarıda
        # zaten kilitlendi, hedef ürün en son geliyor → deadlock riski yok.
        import datetime as _dt
        now = _dt.datetime.utcnow()
        target_item_row = None
        if recipe.target_item_id:
            target_item_row = (db.query(Item)
                               .filter(Item.id == recipe.target_item_id)
                               .with_for_update().first())

        if target_item_row is not None:
            requested_lot = lots.normalize_lot(data.lot_number)
            if requested_lot:
                if lots.is_taken(db, target_item_row.id, requested_lot):
                    db.rollback()
                    sug = lots.suggest(db, target_item_row)
                    return JSONResponse(status_code=400, content={
                        "detail": (f"{requested_lot} bu ürün için zaten kullanılmış — "
                                   f"önerilen: {sug['lot_number']}"),
                        "suggested_lot": sug["lot_number"],
                    })
                produced_lot = requested_lot
                # Elle ileri bir numara verildiyse sayaç oradan devam etsin.
                seq = lots.parse_sequence(produced_lot, lots.lot_prefix(target_item_row))
                if seq and seq > (target_item_row.lot_seq or 0):
                    target_item_row.lot_seq = seq
            else:
                seq = lots.next_sequence(db, target_item_row)
                produced_lot = lots.format_lot(lots.lot_prefix(target_item_row), seq)
                target_item_row.lot_seq = seq
        else:
            # Hedef ürünü olmayan reçete (yarı mamul denemesi) — eski biçim.
            produced_lot = f"PRD-{now.strftime('%Y%m%d-%H%M%S')}"

        # ── Stok düş + Output transaction kaydet ───────────────────────────
        # NOT: item burada *çözülmüş* malzeme — etiket dil çözümü sonrası
        # kardeş etikete inilmiş olabilir, o yüzden Transaction.item_id = item.id
        # (ing.item_id değil — reçetedeki orijinal değil, gerçekten tüketilen).
        for ing, item, gross_qty, is_ambalaj, allocations, uncovered in ing_plan:
            # current_stock kaynak-of-truth: her zaman tam brüt kadar düşer.
            item.current_stock = round(item.current_stock - gross_qty, 6)
            fire_note = (
                f" | %{recipe.waste_percentage or 0} fire dahil, brüt girdi"
                if not is_ambalaj and (recipe.waste_percentage or 0) > 0
                else ""
            )
            base_note = (f"Üretim tüketimi — Reçete: {recipe.name}{fire_note} | "
                         f"Dil: {sel_lang_label}")

            if not allocations:
                # Ambalaj/etiket ya da lotsuz hammadde — toplam stoktan, lot kaydı yok
                db.add(Transaction(
                    item_id=item.id, transaction_type="Output", quantity=gross_qty,
                    notes=f"{base_note} | Üretim Lot: {produced_lot}",
                    performed_by=actor,
                ))
            else:
                # Hammadde — seçilen/FIFO lot(lar)ından düş, her tahsis ayrı Output
                # (Transaction.lot_number = KAYNAK lot → izlenebilirlikte kesin).
                for lot, take in allocations:
                    lot.quantity = round((lot.quantity or 0) - take, 6)
                    sup = lot.supplier.name if lot.supplier else "—"
                    smp = " (numune)" if lot.is_sample else ""
                    db.add(Transaction(
                        item_id=item.id, transaction_type="Output", quantity=take,
                        lot_number=lot.lot_number,
                        notes=(f"{base_note} | Tedarikçi: {sup}{smp} | "
                               f"Kaynak Lot: {lot.lot_number} | Üretim Lot: {produced_lot}"),
                        performed_by=actor,
                    ))
                # Lot toplamı brütü karşılamadıysa (eksik lot verisi) artığı toplam
                # stoktan düş — audit bütünlüğü için yine Output yaz (toplam = brüt).
                if uncovered > _EPS:
                    db.add(Transaction(
                        item_id=item.id, transaction_type="Output", quantity=round(uncovered, 6),
                        notes=(f"{base_note} | (lot kaydı dışı, toplam stoktan) | "
                               f"Üretim Lot: {produced_lot}"),
                        performed_by=actor,
                    ))

        # ── Şahit numune ayrımı ─────────────────────────────────────────────
        # Üretilen X adetin Y'si "Şahit Numune Dolabı"na (marka bazlı), kalanı
        # "Showroom"a ayrılır.  Item.current_stock yine X kadar artar (hepsi
        # stoktadır, sadece konum farklı).  Witness=0 ise tek lot, eski davranış.
        target_item_obj = target_item_row      # yukarıda kilitlenmiş satır
        witness_qty = max(0.0, float(data.witness_quantity or 0))
        if witness_qty > data.produced_quantity:
            witness_qty = data.produced_quantity        # taşmayı kırp
        showroom_qty = round(data.produced_quantity - witness_qty, 6)

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
            # 1) Showroom lot'u — kalan kısım
            if showroom_qty > 0:
                db.add(Inventory(
                    item_id=recipe.target_item_id,
                    lot_number=produced_lot,
                    quantity=showroom_qty,
                    location="Showroom",
                    status="APPROVED",
                    received_by=actor,
                    qc_required=True,
                    domain=(recipe.domain or "cosmetics"),   # Faz 3 — reçetenin paneli
                ))
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
                db.flush()                                 # inventory_id gerekli
                shelf_life_m, extra_m = retention_cfg_months(db)
                retention_row = RetentionSample(
                    inventory_id=witness_inv.id,
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
            db.add(Transaction(
                item_id=recipe.target_item_id,
                lot_number=produced_lot,
                transaction_type="Input",
                quantity=data.produced_quantity,
                notes=(f"Üretim çıktısı — Reçete: {recipe.name} | "
                       f"Dil: {sel_lang_label} | Lot: {produced_lot}{split_note}"),
                performed_by=actor,
            ))
            # Item.current_stock = TOPLAM artar (witness de stokta sayılır —
            # dolaptaki numuneler satılabilir stoktan DÜŞMEZ, bilinçli karar).
            # Satır yukarıda lot sayacı için zaten kilitlendi, yeniden sorgulanmaz.
            if target_item_row:
                target_item_row.current_stock = round(
                    (target_item_row.current_stock or 0) + data.produced_quantity, 6
                )

        # Üretim kaydı
        db.add(ProductionHistory(
            recipe_id=recipe.id,
            recipe_name=recipe.name,
            target_item_id=recipe.target_item_id,
            target_item_name=recipe.target_item.name if recipe.target_item else recipe.description,
            produced_quantity=data.produced_quantity,
            produced_by=actor,                       # Audit
            lot_number=produced_lot if recipe.target_item_id else None,
            witness_quantity=witness_qty,            # şahit numuneye ayrılan adet
            domain=(recipe.domain or "cosmetics"),   # Faz 3 — reçetenin paneli
        ))

        db.commit()

        # ── Low-stock alert: any consumed ingredient that crossed its threshold
        #     queues exactly one notification (one per ingredient line, not one
        #     per stock unit). Snapshots primitive values now; the BackgroundTask
        #     fires after the response is sent so the user sees no extra latency.
        for _ing, item, _gross, _amb, _al, _un in ing_plan:
            if item.min_stock_level > 0 and item.current_stock <= item.min_stock_level:
                background_tasks.add_task(
                    notify_low_stock,
                    item.name, item.current_stock, item.min_stock_level, item.unit or "",
                )

        # ── Stoktan düşülen kalem özeti — lab "ne düştü" diye sormasın ─────
        # ing_plan = gerçekten tüketilen kalemler.  Hammadde / ambalaj ayrımı.
        hammadde_n = sum(1 for _i, _it, _g, amb, _al, _un in ing_plan if not amb)
        ambalaj_n  = sum(1 for _i, _it, _g, amb, _al, _un in ing_plan if amb)

        msg = f"Üretim tamamlandı ({sel_lang_label}). {data.produced_quantity} birim stoğa eklendi."
        msg += f"  Stoktan düşülen: {hammadde_n} hammadde + {ambalaj_n} ambalaj/etiket kalemi."
        if ambalaj_n == 0:
            # En sık kafa karışıklığı: reçeteye ambalaj/etiket hiç eklenmemiş.
            msg += ("  ⚠ DİKKAT: Bu reçetede hiç ambalaj/etiket kalemi yok — "
                    "kavanoz, kapak, etiket stoktan DÜŞÜLMEDİ. Gerekiyorsa "
                    "Reçeteler sayfasından ambalaj bileşenlerini ekleyin.")
        if label_warnings:
            msg += (f"  ⚠ Şu kalemlerin {sel_lang_label} etiketi tanımlı değil, "
                    f"stoktan düşülmedi: {', '.join(label_warnings)}.")
        return {
            "message": msg,
            "lot_number": produced_lot if recipe.target_item_id else None,
            "label_language": sel_lang,
            "label_warnings": label_warnings,
            "consumed_hammadde": hammadde_n,
            "consumed_ambalaj":  ambalaj_n,
            "witness_quantity": witness_qty,
            "showroom_quantity": showroom_qty,
            "cabinet": brand or "",
        }

    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "Üretim sırasında hata oluştu, stoklar değiştirilmedi."})


# ─── QC Endpoints ────────────────────────────────────────────────────────────

@router.get("/qc/quarantine")
def list_quarantine(db: Session = Depends(get_db), domain: str = Depends(active_domain)):
    """
    QC sayfasının beslediği endpoint.  İki kaynaktan gelir:
      • status='QUARANTINE' — geleneksel mal kabul karantinası
      • qc_required=True   — üretim çıktıları (stok eklendi ama QC görmeli)
    Aktif panele (domain) göre süzülür.
    """
    from sqlalchemy import or_
    rows = (
        db.query(Inventory)
        .filter(
            Inventory.domain == domain,
            or_(
                Inventory.status == "QUARANTINE",
                Inventory.qc_required == True,
            )
        )
        .order_by(Inventory.id.desc())
        .all()
    )
    return [
        {
            "id":           r.id,
            "item_name":    r.item.name if r.item else "—",
            "item_id":      r.item_id,
            "item_unit":    r.item.unit if r.item else "",
            "lot_number":   r.lot_number,
            "quantity":     r.quantity,
            "expiry_date":  r.expiry_date or "—",
            "location":     r.location or "—",
            "status":       r.status,
            "qc_required":  r.qc_required,
            # 'source' = "Üretim" veya "Mal Kabul" — UI bunu rozetle gösterebilir
            "source":       "Üretim" if r.qc_required else "Mal Kabul",
            "created_at":   to_tr(r.created_at).strftime("%d.%m.%Y") if r.created_at else "",
        }
        for r in rows
    ]


@router.post("/qc/process/{inventory_id}")
def process_qc(
    inventory_id: int,
    data: QCActionRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("qc", "approve")),
):
    if data.status not in ("APPROVED", "REJECTED"):
        return JSONResponse(status_code=400, content={"detail": "Geçersiz statü. 'APPROVED' veya 'REJECTED' olmalıdır."})

    inv = db.query(Inventory).filter(Inventory.id == inventory_id).first()
    if not inv:
        return JSONResponse(status_code=404, content={"detail": "Envanter kaydı bulunamadı."})
    # Hem mal kabul karantinası hem de üretim qc_required lot'ları işlenebilir
    if inv.status != "QUARANTINE" and not inv.qc_required:
        return JSONResponse(status_code=400, content={"detail": "Bu kayıt QC inceleme listesinde değil."})

    actor = current_user.get("full_name") or current_user.get("username") or "—"
    # production lot'u mu (qc_required+APPROVED) yoksa mal kabul karantinası mı?
    was_quarantine = (inv.status == "QUARANTINE")

    try:
        old_status         = inv.status
        inv.status         = data.status
        inv.qc_notes       = data.notes
        inv.qc_approved_by = actor
        inv.qc_required    = False     # QC karar verdi, artık listede çıkmasın
        inv.updated_at     = __import__("datetime").datetime.utcnow()

        item = db.query(Item).filter(Item.id == inv.item_id).first()
        # Stok değişikliği — karmaşık ama anlamlı:
        #   QUARANTINE + APPROVED → stok henüz eklenmemiş, ekle
        #   QUARANTINE + REJECTED → stok henüz eklenmemiş, hiçbir şey yapma
        #   qc_required (PROD)  + APPROVED → stok zaten üretimde eklendi, hiçbir şey yapma
        #   qc_required (PROD)  + REJECTED → stok üretimde eklendi, geri al + Adjustment audit
        if was_quarantine and data.status == "APPROVED" and item:
            item.current_stock = round((item.current_stock or 0) + inv.quantity, 6)
        elif not was_quarantine and data.status == "REJECTED" and item:
            item.current_stock = round((item.current_stock or 0) - inv.quantity, 6)
            db.add(Transaction(
                item_id=inv.item_id,
                lot_number=inv.lot_number,
                transaction_type="Adjustment",
                quantity=-inv.quantity,
                notes=(
                    f"Üretim QC reddi — Lot: {inv.lot_number}. "
                    f"Eklenen {inv.quantity} {item.unit or ''} stok geri alındı. "
                    f"Sebep: {data.notes}"
                ),
                performed_by=actor,
            ))
            _mark_retention_rejected(db, inv, actor, data.notes or "")

        tx_type = "QC Approval" if data.status == "APPROVED" else "QC Rejection"
        db.add(Transaction(
            item_id=inv.item_id,
            lot_number=inv.lot_number,
            transaction_type=tx_type,
            quantity=inv.quantity,
            notes=f"{tx_type} — Lot: {inv.lot_number}. Not: {data.notes}",
            performed_by=actor,
        ))

        db.commit()
        label = "Onaylandı" if data.status == "APPROVED" else "Reddedildi"
        return {"message": f"Lot #{inv.lot_number} başarıyla {label}."}
    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "QC işlemi sırasında hata oluştu."})


@router.post("/inventory/{inventory_id}/qc-approve")
def qc_approve_form(
    inventory_id: int,
    data: QCFormRequest,
    db: Session = Depends(get_db),
    current_user: dict = Depends(require_permission("qc", "approve")),
):
    """
    Digital QC form endpoint — the ONLY approved path to change a lot from
    QUARANTINE to APPROVED or REJECTED.  Stores the full form JSON in
    inventory.qc_form_data so the audit trail is permanent.
    """
    import json
    from datetime import datetime as _dt

    if data.status not in ("APPROVED", "REJECTED"):
        return JSONResponse(status_code=400, content={"detail": "Geçersiz statü. 'APPROVED' veya 'REJECTED' olmalıdır."})

    inv = db.query(Inventory).filter(Inventory.id == inventory_id).first()
    if not inv:
        return JSONResponse(status_code=404, content={"detail": "Envanter kaydı bulunamadı."})
    # Hem mal kabul karantinası hem üretim qc_required lot'ları işlenebilir
    if inv.status != "QUARANTINE" and not inv.qc_required:
        return JSONResponse(status_code=400, content={"detail": "Bu lot zaten işlenmiş — tekrar değiştirilemez."})

    was_quarantine = (inv.status == "QUARANTINE")

    if not data.checklist:
        return JSONResponse(status_code=422, content={"detail": "Kontrol listesi boş gönderilemez."})

    # ── Serialize full form payload for permanent audit ────────────────────
    form_payload = {
        "checklist":    data.checklist,
        "lab_ml":       data.lab_ml,
        "lab_density":  data.lab_density,
        "lab_color":    data.lab_color,
        "notes":        data.notes or "",
        "status":       data.status,
        "submitted_at": _dt.utcnow().isoformat(),
    }

    actor = current_user.get("full_name") or current_user.get("username") or "—"

    try:
        inv.status         = data.status
        inv.qc_notes       = data.notes or ""
        inv.qc_form_data   = json.dumps(form_payload, ensure_ascii=False)
        inv.qc_approved_by = actor                  # Audit: who QC'd
        inv.qc_required    = False                  # QC karar verdi
        inv.updated_at     = _dt.utcnow()

        label = "Onaylandı ✓" if data.status == "APPROVED" else "Reddedildi ✗"

        # Stok ayarlaması — process_qc ile aynı mantık:
        #   QUARANTINE + APPROVED → stok henüz yok, ekle
        #   QUARANTINE + REJECTED → stok henüz yok, hiçbir şey
        #   qc_required (PROD) + APPROVED → stok zaten var, hiçbir şey
        #   qc_required (PROD) + REJECTED → stok geri al + Adjustment audit
        approved_item = db.query(Item).filter(Item.id == inv.item_id).first()
        if was_quarantine and data.status == "APPROVED" and approved_item:
            approved_item.current_stock = round((approved_item.current_stock or 0) + inv.quantity, 6)
        elif not was_quarantine and data.status == "REJECTED" and approved_item:
            approved_item.current_stock = round((approved_item.current_stock or 0) - inv.quantity, 6)
            db.add(Transaction(
                item_id=inv.item_id,
                lot_number=inv.lot_number,
                transaction_type="Adjustment",
                quantity=-inv.quantity,
                notes=(
                    f"Üretim QC reddi — Lot: {inv.lot_number}. "
                    f"Eklenen {inv.quantity} {approved_item.unit or ''} stok geri alındı."
                ),
                performed_by=actor,
            ))
            _mark_retention_rejected(db, inv, actor, data.notes or "")

        note_text = f"QC Form — {label} — Lot: {inv.lot_number}"
        if data.notes:
            note_text += f" | Not: {data.notes[:120]}"

        db.add(Transaction(
            item_id=inv.item_id,
            lot_number=inv.lot_number,
            transaction_type="QC Approval" if data.status == "APPROVED" else "QC Rejection",
            quantity=inv.quantity,
            notes=note_text,
            performed_by=actor,                      # Audit
        ))

        db.commit()
        return {"message": f"Lot #{inv.lot_number} QC formu kaydedildi — {label}."}

    except Exception:
        db.rollback()
        return JSONResponse(status_code=500, content={"detail": "QC işlemi sırasında hata oluştu."})


@router.get("/qc/{inventory_id}/form/export")
def export_qc_form(
    inventory_id: int,
    format: str = "pdf",
    db: Session = Depends(get_db),
    _: dict = Depends(require_permission("qc", "view")),
):
    """
    Bir lot'un kayıtlı QC formunu PDF veya Excel olarak indir.
    İzlenebilirlik sayfasından (lot detayı → Kalite Kontrol Formu) çağrılır.
    """
    fmt = (format or "pdf").lower()
    if fmt not in ("pdf", "excel", "xlsx"):
        return JSONResponse(status_code=400, content={"detail": "Geçersiz format (pdf veya excel)."})

    inv = db.query(Inventory).filter(Inventory.id == inventory_id).first()
    if not inv:
        return JSONResponse(status_code=404, content={"detail": "Envanter kaydı bulunamadı."})

    from core.qc_report import (
        parse_qc_form, render_qc_pdf, render_qc_excel, qc_export_filename)
    item = db.query(Item).filter(Item.id == inv.item_id).first()
    view = parse_qc_form(inv, item)
    if not view:
        return JSONResponse(status_code=404, content={"detail": "Bu lot için QC formu bulunamadı."})

    try:
        if fmt == "pdf":
            content, media, ext = render_qc_pdf(view), "application/pdf", "pdf"
        else:
            content = render_qc_excel(view)
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            ext = "xlsx"
    except Exception:
        return JSONResponse(status_code=500, content={"detail": "QC formu üretilemedi."})

    return StreamingResponse(
        io.BytesIO(content),
        media_type=media,
        headers={"Content-Disposition": f'attachment; filename="{qc_export_filename(view, ext)}"'},
    )
