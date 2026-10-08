# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Üretim planlayıcısı — "bu reçeteden N adet üretirsek HANGİ KARTTAN, hangi
lottan ne kadar düşer?" sorusunun TEK KAYNAĞI.  Önizleme
(`POST /api/production/preview`) ve başlatma (`POST /api/production`) aynı
`plan()`'ı çağırır; ekrandaki hesap ile stoktan düşen birebir aynıdır.

NEDEN VAR (08.10.2026 — P2)
───────────────────────────
Lab aynı malzemenin her tedarikçisini AYRI kart tutuyor ("aynı malzeme"
grupları, core/material_groups.py).  Songül Hanım: "Üretimde önce küçük
miktarlı satış yapan tedarikçilerden elimizdeki hammaddeleri bitirmek
istiyoruz."  Kullanıcı kararı (07.10.2026): reçete satırının kartı bir grupta
ve gruptaki başka AKTİF kartta stok varsa üretim ekranı hangi karttan
kullanılacağını SORAR — kendiliğinden değiştirmez, bölmeye (iki karttan)
izin verir, önce "bitirilecek" tedarikçiyi önerir.

Eski akışın iki hatası da burada kapanır:
  • Stok kapısı satır başınaydı (`item.current_stock < gross`) — aynı kart iki
    satırda geçerse toplam stoğu aşabiliyordu.  Artık kart başına TOPLAM.
  • Aynı lot iki satıra FIFO ile verilebiliyordu.  Artık `reserved` ile
    önceki satırların aldığı lot payı düşülür (FIFO yine TEK yerde:
    core/stock_lots.plan_fifo).

KURALLAR
────────
  • Satırlar `core.consumption.expand_recipe` ile kurulur (fire, etiket dil
    kardeşi, aynı kart tek satırda birleşir) — parite yapı gereği tutar.
    Aynı kart reçetede birden çok satırdaysa (ör. su A ve C fazında) satır
    `parts`'ı reçete satırlarını tutar; yazımda her reçete satırı eskisi gibi
    kendi Output'unu ve fazını alır (`segments`), lotlar sırayla dilimlenir.
  • Seçenekler yalnız hammadde ve ambalaj satırlarında (etiket HARİÇ): reçete
    kartı + kartın AKTİF grubundaki aktif, aynı panel, aynı tür
    (`stock_lots.lot_kind`), aynı birim ailesi (`purchase_plan.unit_norm`)
    kartlar.
  • `needs_choice` — reçete kartı dışındaki bir seçenekte stok var.  Böyle bir
    satırda `sources` gelmezse hata `source_choice_required`.
  • Öneri (bütün satırlarda ORTAK havuzla, açgözlü): (1) bitirilecek firma /
    "bu malzemede alma" kartları — en eski lot önce, (2) reçete kartı,
    (3) tercih edilen (malzeme tercihi sırası, sonra firma durumu),
    (4) diğerleri (FIFO yaşı).
  • Eski `ingredient_lot_choices {item_id: inventory_id}` → reçete kartından
    tam brüt + o lot (geriye uyum; seçim gereken satırı CEVAPLAMAZ — eski
    ekran grubu bilmiyordu).
  • Kill switch: AppSetting `production.source_choice.enabled` (varsayılan
    açık).  Kapalıyken seçenek yalnız reçete kartıdır → eski davranış
    (deploysuz geri dönüş).

KİLİT SIRASI (lock=True)
────────────────────────
Düşülecek kartlar + hedef ürün TEK sorguda id sıralı FOR UPDATE → hammadde
kartlarının lotları id sıralı FOR UPDATE.  Üretim iptali
(core/production_cancel.lock_rows) aynı sırayı kullanır.

`plan()` HİÇBİR ŞEY YAZMAZ; hatalar `Plan.errors`'ta toplanır (başlatma 400,
önizleme `blockers`).  Yalnız reçetede kartı bulunamayan kalem `PlanError`
(404, eski davranış) fırlatır.
"""
import math
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from sqlalchemy import or_
from sqlalchemy.orm import Session

from core.consumption import _kind as consumption_kind, expand_recipe, load_recipe_recs
from core.stock_lots import APPROVED, lot_kind, plan_fifo
from database import (AppSetting, Inventory, Item, MaterialGroup, MaterialSupplierPref,
                      Recipe, Supplier, to_tr)

CFG_ENABLED = "production.source_choice.enabled"
_OFF_VALUES = ("0", "false", "off", "no", "hayir", "hayır", "kapali", "kapalı")

EPS = 1e-9
SUM_TOL_ABS = 1e-6               # toplam toleransı: max(1e-6, 1e-4 × brüt)
SUM_TOL_REL = 1e-4
MAX_SOURCES_PER_LINE = 20

# Hata kodları — istemci `code` alanına göre davranır.
SOURCE_CHOICE_REQUIRED = "source_choice_required"
INVALID_SOURCE = "invalid_source"
LOT_NOT_FOUND = "lot_not_found"
LOT_INSUFFICIENT = "lot_insufficient"
INSUFFICIENT_STOCK = "insufficient_stock"

STATUS_LABELS = {"normal": "Normal", "preferred": "Tercih edilen",
                 "phase_out": "Bitirilecek"}
PREF_LABELS = {"preferred": "Tercih", "avoid": "Bu malzemede alma"}


class PlanError(Exception):
    """Planlanamayan reçete (kartı silinmiş kalem) — HTTP `status` ile döner."""

    def __init__(self, status: int, detail: str, code: str = "plan_error"):
        super().__init__(detail)
        self.status = status
        self.detail = detail
        self.code = code

    def payload(self) -> dict:
        return {"detail": self.detail, "code": self.code}


@dataclass
class Pick:
    """Bir satırın bir karttan düşülecek payı.  `allocations` lot planı
    (yalnız hammadde), `uncovered` lot kaydıyla karşılanamayan artık."""
    item_id: int
    quantity: float
    inventory_id: Optional[int] = None
    allocations: List[Tuple[Inventory, float]] = field(default_factory=list)
    uncovered: float = 0.0


@dataclass
class Segment:
    """Yazılacak TEK Output: hangi reçete satırının (`row`, `phase`) hangi
    seçiminden (`pick`), hangi lottan (`lot` None → lotsuz), ne kadar.
    `uncovered` — lot kaydı dışı artık ("lot kaydı dışı, toplam stoktan")."""
    row: int
    phase: Optional[str]
    line: "Line"
    pick: Pick
    lot: Optional[Inventory]
    quantity: float
    uncovered: bool = False


@dataclass
class Line:
    """Reçetenin tüketim satırı (aynı kart birleşmiş).  `key` = reçete
    kartının id'si (str) — reçete düzenlenince satır id'leri değiştiği için
    seçimler kart id'siyle anahtarlanır.  `parts` — birleşen reçete
    satırları: [(sıra, faz, brüt pay)]."""
    key: str
    recipe_item_id: int            # reçetedeki kart
    item_id: int                   # varsayılan tüketilen kart (etikette dil kardeşi)
    kind: str                      # raw | packaging | label
    unit: str
    phase: Optional[str]           # görünüm: fazlar birleşik ('A+C')
    net: float
    gross: float
    factor: float
    parts: List[Tuple[int, Optional[str], float]] = field(default_factory=list)
    language: Optional[str] = None # dil etiketinde fiilen düşülen kartın dili
    group: Optional[dict] = None
    options: List[dict] = field(default_factory=list)
    needs_choice: bool = False
    suggested: List[dict] = field(default_factory=list)
    chosen: List[Pick] = field(default_factory=list)
    answered: bool = False
    shortfall: float = 0.0
    errors: List[dict] = field(default_factory=list)

    @property
    def is_ambalaj(self) -> bool:
        return self.kind != "raw"

    @property
    def option_ids(self) -> List[int]:
        return [o["item_id"] for o in self.options] or [self.item_id]


@dataclass
class Plan:
    recipe: Recipe
    quantity: float
    lang: str
    enabled: bool
    lines: List[Line]
    per_card: List[dict]
    cards: Dict[int, Item]         # düşülecek kartlar (lock=True ise kilitli, taze)
    names: Dict[int, str]
    label_warnings: List[str]
    errors: List[dict]

    @property
    def can_start(self) -> bool:
        return not self.errors

    def first_error(self) -> Optional[dict]:
        """Yanıtın `detail`/`code`'u: seçim eksikse o (istemci seçim bloklarını
        açar), değilse ilk hata."""
        for e in self.errors:
            if e["code"] == SOURCE_CHOICE_REQUIRED:
                return e
        return self.errors[0] if self.errors else None

    def segments(self) -> List[Segment]:
        """Yazım sırası: reçete satırı sırasıyla, her reçete satırının payı
        kendi seçim/lot akışından dilimlenmiş Output'lar.  Eski akış da
        Output'ları reçete satırı sırasıyla yazıyordu."""
        out: List[Segment] = []
        for ln in self.lines:
            out.extend(_line_segments(ln))
        out.sort(key=lambda s: s.row)          # sıralama kararlı: satır içi sıra korunur
        return out


# ─── Ayar ────────────────────────────────────────────────────────────────────

def source_choice_enabled(db: Session) -> bool:
    """Kill switch — AppSetting `production.source_choice.enabled`.  Satır
    yok/boş → AÇIK; '0' / 'false' / 'off' / 'hayır' … → KAPALI."""
    row = db.query(AppSetting).filter(AppSetting.key == CFG_ENABLED).first()
    v = (row.value or "").strip().lower() if row is not None and row.value is not None else ""
    return not v or v not in _OFF_VALUES


# ─── Yardımcılar ────────────────────────────────────────────────────────────

def _r6(x) -> float:
    return round(float(x or 0.0), 6)


def _fold(s) -> str:
    from core.supplier_prices import normalize
    return normalize(s or "")


def _unit_norm(u) -> str:
    from core.purchase_plan import unit_norm      # geç import — döngü olmasın
    return unit_norm(u)


def _join_phases(phases) -> Optional[str]:
    """Görünüm için birleşik faz — aynı kart birden çok fazda: 'A+C'."""
    out = []
    for p in phases:
        p = (p or "").strip()
        if p and p not in out:
            out.append(p)
    return "+".join(out) or None


def _lot_query(db: Session, item_ids):
    """Üretim lot havuzu — APPROVED, stoklu, numune DEĞİL (24.08.2026 kuralı)."""
    return (db.query(Inventory)
            .filter(Inventory.item_id.in_(sorted(item_ids)),
                    Inventory.status == APPROVED,
                    Inventory.quantity > 0,
                    Inventory.is_sample == False))       # noqa: E712


def _date_tr(dt) -> str:
    return to_tr(dt).strftime("%d.%m.%Y") if dt else ""


def _err(code: str, key: Optional[str], detail: str, **extra) -> dict:
    return {"code": code, "key": key, "detail": detail, **extra}


# ─── 1) Satırlar ────────────────────────────────────────────────────────────

def _build_lines(rec, exp, qty: float) -> List[Line]:
    """expand_recipe satırları → Line.  `parts`: birleşen reçete satırları
    (dil etiketinde yalnız işlenen ilk satır).  Parçaların toplamı brüte
    eşit değilse (beklenmez) tek parçaya düşülür."""
    mult = float(qty) / (rec.output_quantity or 1.0)
    lines: List[Line] = []
    for ln in exp.lines:
        src = ln.source_item_id or ln.item_id
        rows = [(i, ing) for i, ing in enumerate(rec.ingredients) if ing.item_id == src]
        if ln.kind == "label" and ln.item_id != src:
            rows = rows[:1]                     # dil kardeşi: grup bir kez işlenir
        parts = [(i, (ing.phase or "").strip() or None, ing.quantity * mult * ln.factor)
                 for i, ing in rows]
        gross = _r6(ln.gross)
        if not parts or abs(sum(p[2] for p in parts) - ln.gross) > max(1e-6, 1e-9 * gross):
            parts = [(rows[0][0] if rows else 0, parts[0][1] if parts else None, ln.gross)]
        lines.append(Line(
            key=str(src), recipe_item_id=src, item_id=ln.item_id, kind=ln.kind,
            unit="", phase=_join_phases(p[1] for p in parts),
            net=_r6(ln.net_per_unit * float(qty)), gross=gross, factor=ln.factor,
            parts=parts))
    return lines


# ─── 2) Seçenekler ──────────────────────────────────────────────────────────

def _group_members(db: Session, lines: List[Line], orm: Dict[int, Item], domain: str):
    """Satır kartlarının AKTİF grupları + gruplardaki aktif, aynı panel kartlar."""
    gids = {orm[ln.item_id].material_group_id for ln in lines
            if ln.kind != "label" and orm[ln.item_id].material_group_id}
    if not gids:
        return {}, {}
    groups = {g.id: g for g in db.query(MaterialGroup)
              .filter(MaterialGroup.id.in_(gids), MaterialGroup.domain == domain,
                      MaterialGroup.is_active == True).all()}  # noqa: E712
    members: Dict[int, List[Item]] = {}
    if groups:
        for m in (db.query(Item)
                  .filter(Item.material_group_id.in_(list(groups)),
                          Item.is_active == True, Item.domain == domain)   # noqa: E712
                  .order_by(Item.id.asc()).all()):
            members.setdefault(m.material_group_id, []).append(m)
            orm.setdefault(m.id, m)
    return groups, members


def _option_cards(lines: List[Line], orm: Dict[int, Item], groups, members) -> Dict[str, List[Item]]:
    """Satır başına seçenek kartları: reçete kartı + aynı tür ve birim
    ailesindeki grup kartları (bitmiş ürün kartında grup seçeneği yok)."""
    out: Dict[str, List[Item]] = {}
    for ln in lines:
        if ln.kind == "label":
            continue
        card = orm[ln.item_id]
        cands = [card]
        grp = groups.get(card.material_group_id) if card.material_group_id else None
        kind = lot_kind(card.category)
        if grp is not None and kind != "finished":
            unit = _unit_norm(card.unit)
            cands += [m for m in members.get(grp.id, [])
                      if m.id != card.id and lot_kind(m.category) == kind
                      and consumption_kind(m) == ln.kind
                      and _unit_norm(m.unit) == unit]
            if len(cands) > 1:
                ln.group = {"id": grp.id, "name": grp.name}
        out[ln.key] = cands
    return out


def _supplier_context(db: Session, domain: str, sids) -> Tuple[Dict[int, str], Dict[int, str],
                                                               Dict[int, str]]:
    """(ad, durum, firma anahtarı) — durum firma anahtarı düzeyinde
    (`core.suppliers.status_by_key`; TATLIDİLİMLER / TATLİDİLİMLER kartlarından
    biri bitirilecekse ikisi de), satın alma planıyla aynı kural."""
    from core.purchase_pricing import SupplierIndex
    from core.suppliers import normalize_status, status_by_key
    sids = {int(s) for s in sids if s}
    if not sids:
        return {}, {}, {}
    sups = db.query(Supplier).filter(or_(Supplier.domain == domain, Supplier.id.in_(sids))).all()
    names = {s.id: s.name or "" for s in sups}
    ix = SupplierIndex([s.name or "" for s in sups])
    by_key, _ = status_by_key([s for s in sups if (s.domain or "cosmetics") == domain], ix)
    key_of = {s.id: ix.key(s.name or "") for s in sups}
    status = {s.id: (by_key.get(key_of[s.id]) or normalize_status(s.purchase_status) or "normal")
              for s in sups}
    return names, status, key_of


def _card_prefs(db: Session, domain: str, card_ids: List[int], groups,
                orm: Dict[int, Item]) -> Dict[int, Dict[int, Tuple[str, int]]]:
    """Kart başına geçerli malzeme tercihleri {kart: {tedarikçi: (pref, rank)}}
    — kartın grup satırları + kendi satırları (kart kapsamı ezer,
    `core.suppliers.effective_prefs`)."""
    from core.suppliers import effective_prefs
    prows = (db.query(MaterialSupplierPref)
             .filter(MaterialSupplierPref.domain == domain,
                     or_(MaterialSupplierPref.item_id.in_(card_ids),
                         MaterialSupplierPref.material_group_id.in_(list(groups) or [-1])))
             .all())
    out: Dict[int, Dict[int, Tuple[str, int]]] = {}
    for cid in card_ids:
        gid = orm[cid].material_group_id
        rows = [p for p in prows
                if p.item_id == cid or (p.item_id is None and gid and p.material_group_id == gid)]
        if rows:
            out[cid] = effective_prefs(rows)
    return out


def _attach_options(db: Session, domain: str, lines: List[Line], opt_cards, orm, groups,
                    enabled: bool) -> Dict[int, str]:
    """Seçenek görünümü (tedarikçi, durum, tercih, lotlar) + öneri sırası +
    `needs_choice`.  Dönüş: tedarikçi adları (lot görünümü için de)."""
    alt = any(len(c) > 1 for c in opt_cards.values())
    all_ids = sorted({c.id for cs in opt_cards.values() for c in cs})
    sup_names, sup_status, key_of = _supplier_context(
        db, domain, {orm[i].supplier_id for i in all_ids if orm[i].supplier_id})
    prefs = _card_prefs(db, domain, all_ids, groups, orm) if alt else {}

    def card_pref(cid: int) -> Tuple[Optional[str], Optional[int]]:
        sid, eff = orm[cid].supplier_id, prefs.get(cid) or {}
        if not sid or not eff:
            return None, None
        if sid in eff:
            return eff[sid]
        k = key_of.get(sid)
        return next((v for s, v in eff.items() if k and key_of.get(s) == k), (None, None))

    raw_ids = sorted({c.id for ln in lines if ln.kind == "raw" for c in opt_cards.get(ln.key, [])})
    lots_by_card: Dict[int, List[Inventory]] = {}
    if raw_ids:
        for inv in _lot_query(db, raw_ids).order_by(Inventory.created_at.asc(), Inventory.id.asc()):
            lots_by_card.setdefault(inv.item_id, []).append(inv)
    extra = {inv.supplier_id for invs in lots_by_card.values() for inv in invs
             if inv.supplier_id and inv.supplier_id not in sup_names}
    if extra:
        sup_names.update({s.id: s.name or "" for s in
                          db.query(Supplier).filter(Supplier.id.in_(extra)).all()})

    for ln in lines:
        if ln.kind == "label":
            continue
        opts = []
        for c in opt_cards[ln.key]:
            sid = c.supplier_id
            status = sup_status.get(sid, "normal") if sid else "normal"
            pref, rank = card_pref(c.id)
            invs = lots_by_card.get(c.id, [])
            opts.append({
                "item_id": c.id, "name": c.name or "", "supplier_id": sid,
                "supplier_name": sup_names.get(sid) if sid else None,
                "supplier_status": status, "supplier_status_label": STATUS_LABELS[status],
                "material_pref": pref, "material_pref_rank": rank,
                "material_pref_label": PREF_LABELS.get(pref) if pref else None,
                "is_recipe_card": c.id == ln.item_id,
                "stock": _r6(c.current_stock), "unit": c.unit or "",
                "lots": [{"inventory_id": inv.id, "lot_number": inv.lot_number,
                          "qty": _r6(inv.quantity), "created_at": _date_tr(inv.created_at),
                          "expiry_date": inv.expiry_date or "",
                          "supplier_name": sup_names.get(inv.supplier_id) if inv.supplier_id else None}
                         for inv in invs],
                "_sort": _suggest_key(c, ln, status, pref, rank, invs),
            })
        opts.sort(key=lambda o: o["_sort"])
        for i, o in enumerate(opts):
            o["suggest_order"] = i + 1
            del o["_sort"]
        ln.options = opts
        ln.needs_choice = enabled and any(
            (not o["is_recipe_card"]) and o["stock"] > EPS for o in opts)
    return sup_names


def _suggest_key(card: Item, ln: Line, status: str, pref, rank, invs) -> tuple:
    """Öneri sırası: (0) bitirilecek firma / bu malzemede alma — en eski lot
    önce, (1) reçete kartı, (2) tercih (malzeme tercihi rank'i, sonra firma
    "tercih edilen"), (3) diğerleri (FIFO yaşı); eşitlikte ad, id."""
    oldest = invs[0].created_at.timestamp() if invs and invs[0].created_at else float("inf")
    if status == "phase_out" or pref == "avoid":
        tier, sub = 0, (oldest,)
    elif card.id == ln.item_id:
        tier, sub = 1, ()
    elif pref == "preferred" or status == "preferred":
        tier, sub = 2, (rank if pref == "preferred" else 99,
                        0 if status == "preferred" else 1, oldest)
    else:
        tier, sub = 3, (oldest,)
    return (tier, sub, _fold(card.name), card.id)


# ─── 3) Öneri ───────────────────────────────────────────────────────────────

def _suggest(lines: List[Line], orm: Dict[int, Item]) -> None:
    """Önerilen bölme — bütün satırlarda ORTAK havuz: önce seçim gerektirmeyen
    satırlar kendi kartından sabit düşülür, sonra seçim gereken satırlar
    öneri sırasıyla açgözlü doldurulur.  `shortfall` = karşılanamayan brüt."""
    pool: Dict[int, float] = {}
    for ln in lines:
        for cid in ln.option_ids:
            pool.setdefault(cid, float(orm[cid].current_stock or 0.0) if cid in orm else 0.0)
    for ln in lines:
        if ln.needs_choice:
            continue
        have = max(0.0, pool.get(ln.item_id, 0.0))
        ln.suggested = [{"item_id": ln.item_id, "quantity": ln.gross}]
        ln.shortfall = _r6(max(0.0, ln.gross - have))
        pool[ln.item_id] = pool.get(ln.item_id, 0.0) - ln.gross
    for ln in lines:
        if not ln.needs_choice:
            continue
        remaining, sugg = ln.gross, []
        for o in ln.options:
            if remaining <= EPS:
                break
            take = min(max(0.0, pool.get(o["item_id"], 0.0)), remaining)
            if take > EPS:
                sugg.append({"item_id": o["item_id"], "quantity": _r6(take)})
                pool[o["item_id"]] = pool.get(o["item_id"], 0.0) - take
                remaining -= take
        ln.suggested = sugg
        ln.shortfall = _r6(max(0.0, remaining))


# ─── 4) Seçimler ────────────────────────────────────────────────────────────

def _normalize_sources(sources) -> Dict[str, list]:
    """{anahtar(str): [giriş]} — pydantic modeli ya da düz sözlük kabul edilir."""
    out: Dict[str, list] = {}
    for k, v in (sources or {}).items():
        entries = []
        for e in (v or []):
            if hasattr(e, "model_dump"):
                e = e.model_dump()
            entries.append(e if isinstance(e, dict) else {"_bad": True})
        out[str(k).strip()] = entries
    return out


def _normalize_lot_choices(lot_choices) -> Dict[int, int]:
    """Eski {item_id: inventory_id} — bozuk girdiler sessizce atlanır (eski
    davranış)."""
    out: Dict[int, int] = {}
    for k, v in (lot_choices or {}).items():
        try:
            out[int(k)] = int(v)
        except (TypeError, ValueError):
            continue
    return out


def _validate_sources(ln: Line, entries: list, name: str) -> List[Pick]:
    """Satırın seçimlerini doğrula → Pick listesi; sorunlar `ln.errors`'a.
    Toplam brüte tolerans içinde eşitse SON giriş tam kalana oturtulur."""
    def bad(msg):
        ln.errors.append(_err(INVALID_SOURCE, ln.key, f"«{name}»: {msg}"))
        return []

    if not entries:
        return bad("en az bir kaynak kart seçin.")
    if len(entries) > MAX_SOURCES_PER_LINE:
        return bad(f"en fazla {MAX_SOURCES_PER_LINE} kaynak seçilebilir.")
    allowed = set(ln.option_ids)
    picks: List[Pick] = []
    for e in entries:
        if e.get("_bad"):
            return bad("kaynak biçimi geçersiz.")
        try:
            iid = int(e.get("item_id"))
            q = float(e.get("quantity"))
        except (TypeError, ValueError):
            return bad("kaynak kart ve miktar sayı olmalı.")
        inv = e.get("inventory_id")
        try:
            inv = int(inv) if inv not in (None, "", 0) else None
        except (TypeError, ValueError):
            return bad("lot seçimi geçersiz.")
        if iid not in allowed:
            return bad("seçilen kart bu kalemin seçenekleri arasında değil "
                       "(aynı malzeme grubu, aynı tür ve birim, aktif kart olmalı).")
        if not math.isfinite(q) or q <= EPS:
            return bad("miktar sıfırdan büyük bir sayı olmalı.")
        if inv is not None and ln.kind != "raw":
            return bad("ambalajda lot seçilmez.")
        picks.append(Pick(item_id=iid, quantity=q, inventory_id=inv))
    total = sum(p.quantity for p in picks)
    if abs(total - ln.gross) > max(SUM_TOL_ABS, SUM_TOL_REL * ln.gross):
        unit = ln.unit or ""
        return bad(f"seçilen miktarların toplamı ({round(total, 4)} {unit}) brüt ihtiyaca "
                   f"({round(ln.gross, 4)} {unit}) eşit değil.")
    # İlk payları defter hassasiyetine yuvarla, son payı tam kalana oturt.
    # Payları ayrı ayrı yuvarlamak toplam tüketimi brütten kaydırırdı.
    for p in picks[:-1]:
        p.quantity = _r6(p.quantity)
        if p.quantity <= EPS:
            return bad("miktar en az 0.000001 olmalı.")
    rest = _r6(ln.gross - sum(p.quantity for p in picks[:-1]))
    if rest <= EPS:
        return bad("miktarlar brüt ihtiyacı aşıyor.")
    picks[-1].quantity = rest
    return picks


def _resolve_choices(lines: List[Line], orm: Dict[int, Item], sources, lot_choices) -> List[dict]:
    """Her satırın `chosen`'ı: gönderilen kaynaklar → (seçim gerekmiyorsa)
    reçete kartından tam brüt + eski lot seçimi.  Seçim gereken satırda
    kaynak yoksa `source_choice_required`.  Dönüş: bu adımın hataları."""
    errors: List[dict] = []
    by_key = {ln.key: ln for ln in lines}
    src_in = _normalize_sources(sources)
    for k in src_in:
        if k not in by_key:
            errors.append(_err(INVALID_SOURCE, k, "Reçetede olmayan bir kalem için kaynak gönderildi."))
    legacy = _normalize_lot_choices(lot_choices)
    for ln in lines:
        name = orm[ln.recipe_item_id].name if ln.recipe_item_id in orm else ln.key
        if ln.key in src_in:
            ln.answered = True
            if ln.kind == "label":
                ln.errors.append(_err(INVALID_SOURCE, ln.key, f"«{name}» etiket — kaynak kart seçilmez."))
            else:
                ln.chosen = _validate_sources(ln, src_in[ln.key], name)
        elif ln.needs_choice:
            ln.errors.append(_err(SOURCE_CHOICE_REQUIRED, ln.key,
                                  f"«{name}» için hangi tedarikçinin kartından kullanılacağını seçin."))
        else:
            ln.answered = True
            inv_id = legacy.get(ln.item_id) if ln.kind == "raw" else None
            ln.chosen = [Pick(item_id=ln.item_id, quantity=ln.gross, inventory_id=inv_id)]
        errors.extend(ln.errors)
    return errors


# ─── 5) Kilit + lot dağıtımı + kart kapısı ──────────────────────────────────

def _load_consumed(db: Session, lines: List[Line], lock: bool,
                   target_item_id: Optional[int] = None):
    """Düşülecek kartlar ve hammadde lot havuzları.  lock=True: kartlar TEK
    sorguda HEDEF DAHİL id sıralı FOR UPDATE, sonra lotlar id sıralı FOR UPDATE —
    `populate_existing` ile kilitten SONRAKİ değer okunur (planlayıcı aynı
    satırları önce kilitsiz okudu)."""
    card_ids = sorted({p.item_id for ln in lines for p in ln.chosen})
    lock_ids = sorted(set(card_ids) | ({target_item_id} if lock and target_item_id else set()))
    cards: Dict[int, Item] = {}
    if lock_ids:
        q = db.query(Item).filter(Item.id.in_(lock_ids)).order_by(Item.id.asc())
        if lock:
            q = q.with_for_update().populate_existing()
        # Hedef yalnız kilit için eklendi; tüketim/uyarı kartlarına karışmaz.
        # Aynı kart hem kaynak hem hedefse tek kez kilitlenir, cards'ta kalır.
        cards = {it.id: it for it in q.all() if it.id in card_ids}
    raw_ids = sorted({p.item_id for ln in lines if ln.kind == "raw" for p in ln.chosen})
    pools: Dict[int, List[Inventory]] = {}
    if raw_ids:
        if lock:
            _lot_query(db, raw_ids).order_by(Inventory.id.asc()).with_for_update() \
                .populate_existing().all()
        for inv in _lot_query(db, raw_ids).order_by(Inventory.created_at.asc(), Inventory.id.asc()):
            pools.setdefault(inv.item_id, []).append(inv)
    return cards, pools


def _check_consumed_cards(lines: List[Line], cards: Dict[int, Item], domain: str) -> None:
    """Seçenek okumasıyla kart kilidi arasında düzenlenen/silinen kartı
    reddet; kilit sonrası gerçek tür, birim ve panel üzerinden tüketilir."""
    for ln in lines:
        for p in ln.chosen:
            card = cards.get(p.item_id)
            valid = (card is not None and card.domain == domain
                     and consumption_kind(card) == ln.kind
                     and _unit_norm(card.unit) == _unit_norm(ln.unit))
            if valid and p.item_id != ln.item_id:
                valid = bool(card.is_active and ln.group
                             and card.material_group_id == ln.group["id"])
            if not valid:
                raise PlanError(400, "Seçilen kaynak kart değişmiş veya artık uygun değil. "
                                "Üretim önizlemesini yenileyin.", INVALID_SOURCE)


def _allocate_lots(db: Session, lines: List[Line], cards: Dict[int, Item],
                   pools: Dict[int, List[Inventory]]) -> List[dict]:
    """Hammadde seçimlerinin lot planı.  Önce AÇIK seçilmiş lotlar (FIFO
    onları kapmasın), sonra FIFO — `reserved` önceki satırların aldığı lot
    payını düşer, aynı lot iki kez verilmez.  Seçili lot yok/kısaysa hata
    (sessizce başka lota geçilmez — eski kural)."""
    errors: List[dict] = []
    reserved: Dict[int, float] = {}
    for ln in lines:
        if ln.kind != "raw":
            continue
        for p in ln.chosen:
            if not p.inventory_id:
                continue
            card = cards[p.item_id]
            lot = next((x for x in pools.get(p.item_id, []) if x.id == p.inventory_id), None)
            if lot is None:
                e = _err(LOT_NOT_FOUND, ln.key,
                         f"'{card.name}' için seçilen lot bulunamadı veya stokta uygun değil.")
            else:
                avail = float(lot.quantity or 0.0) - reserved.get(lot.id, 0.0)
                if avail + EPS >= p.quantity:
                    p.allocations = [(lot, p.quantity)]
                    reserved[lot.id] = reserved.get(lot.id, 0.0) + p.quantity
                    continue
                e = _err(LOT_INSUFFICIENT, ln.key,
                         f"'{card.name}' için seçilen lot ({lot.lot_number}) yetersiz: "
                         f"{round(avail, 4)} {card.unit or ''} var, "
                         f"{round(p.quantity, 4)} {card.unit or ''} gerekiyor.")
            ln.errors.append(e)
            errors.append(e)
    for ln in lines:
        if ln.kind != "raw":
            continue
        for p in ln.chosen:
            if p.inventory_id:
                continue
            p.allocations, p.uncovered = plan_fifo(db, cards[p.item_id], p.quantity,
                                                   exclude_samples=True, lock=False,
                                                   reserved=reserved)
            for lot, take in p.allocations:
                reserved[lot.id] = reserved.get(lot.id, 0.0) + take
    return errors


def _gate(lines: List[Line], cards: Dict[int, Item], recipe: Recipe) -> Tuple[List[dict], List[dict]]:
    """Kart başına TOPLAM stok kapısı — bütün satırların o karta istediği ≤
    current_stock.  Dönüş: (per_card görünümü, hatalar)."""
    per: Dict[int, dict] = {}
    for ln in lines:
        for p in ln.chosen:
            d = per.setdefault(p.item_id, {"requested": 0.0, "keys": []})
            d["requested"] += p.quantity
            if ln.key not in d["keys"]:
                d["keys"].append(ln.key)
    by_key = {ln.key: ln for ln in lines}
    per_card, errors = [], []
    for cid, d in per.items():
        card = cards[cid]
        stock = float(card.current_stock or 0.0)
        req = _r6(d["requested"])
        ok = req <= stock + EPS
        per_card.append({"item_id": cid, "name": card.name or "", "unit": card.unit or "",
                         "requested": req, "stock": _r6(stock), "ok": ok, "lines": d["keys"]})
        if ok:
            continue
        fire_note = ("" if card.category == "Ambalaj"
                     else f" (%{recipe.waste_percentage or 0} fire dahil)")
        multi = f" — {len(d['keys'])} satırın toplamı" if len(d["keys"]) > 1 else ""
        e = _err(INSUFFICIENT_STOCK, d["keys"][0],
                 f"'{card.name}' için yeterli stok yok. Gereken: {req} {card.unit}"
                 f"{fire_note}{multi}, Mevcut: {card.current_stock} {card.unit}", item_id=cid)
        errors.append(e)
        for k in d["keys"]:
            by_key[k].errors.append(e)
    return per_card, errors


# ─── Planlayıcı ─────────────────────────────────────────────────────────────

def plan(db: Session, recipe: Recipe, qty: float, lang: str, *, domain: str,
         sources: Optional[dict] = None, lot_choices: Optional[dict] = None,
         lock: bool = False) -> Plan:
    """Reçeteden `qty` adet üretimin tam planı — hiçbir şey yazmaz.

    `sources` — {reçete kartı id (str): [{item_id, quantity, inventory_id?}]}
    `lot_choices` — eski {item_id: inventory_id} (geriye uyum)
    `lock` — başlatmada True: düşülecek kartlar + lotları FOR UPDATE.
    """
    lang = "EN" if (lang or "").strip().upper().startswith("EN") else "TR"
    recs, items, sibs = load_recipe_recs(db, [recipe.id], domain, active_only=False)
    if not recs:
        raise PlanError(404, "Reçete bulunamadı.", "recipe_not_found")
    if recipe.target_item_id and recipe.target_item_id not in items:
        raise PlanError(404, "Hedef ürün bu panelde bulunamadı.", "item_not_found")
    exp = expand_recipe(recs[0], qty, items, sibs, label_mode=lang)
    if exp.missing_item_ids:
        raise PlanError(404, f"Hammadde bulunamadı (ID: {exp.missing_item_ids[0]}).",
                        "item_not_found")

    lines = _build_lines(recs[0], exp, qty)
    for ln in lines:
        it = items[ln.item_id]
        ln.unit = it.unit or ""
        ln.language = it.language if (it.language and it.label_group) else None
    base_ids = {ln.item_id for ln in lines} | {ln.recipe_item_id for ln in lines}
    orm: Dict[int, Item] = ({it.id: it for it in db.query(Item).filter(Item.id.in_(base_ids))}
                            if base_ids else {})

    enabled = source_choice_enabled(db)
    groups, members = _group_members(db, lines, orm, domain) if enabled else ({}, {})
    opt_cards = _option_cards(lines, orm, groups, members)
    _attach_options(db, domain, lines, opt_cards, orm, groups, enabled)
    _suggest(lines, orm)

    errors = _resolve_choices(lines, orm, sources, lot_choices)
    cards, pools = _load_consumed(db, lines, lock, recipe.target_item_id)
    _check_consumed_cards(lines, cards, domain)
    errors += _allocate_lots(db, lines, cards, pools)
    per_card, gate_errors = _gate(lines, cards, recipe)
    errors += gate_errors

    names = {i: (it.name or "") for i, it in orm.items()}
    names.update({i: (c.name or "") for i, c in cards.items()})
    return Plan(recipe=recipe, quantity=float(qty), lang=lang, enabled=enabled, lines=lines,
                per_card=per_card, cards=cards, names=names,
                label_warnings=[s["label"] for s in exp.skipped_labels], errors=errors)


# ─── Yazım dilimleri ────────────────────────────────────────────────────────

def _line_segments(ln: Line) -> List[Segment]:
    """Satırın seçim/lot akışını reçete satırlarına (`parts`) sırayla dilimle.
    Tek parçalı satırda dilim = tahsis (eski akışla birebir).  Son parça
    kalan her şeyi alır (yuvarlama kayması olmasın)."""
    stream: List[Tuple[Pick, Optional[Inventory], float, bool]] = []
    for p in ln.chosen:
        if not p.allocations:
            stream.append((p, None, p.quantity, False))
            continue
        stream.extend((p, lot, take, False) for lot, take in p.allocations)
        if p.uncovered > EPS:
            stream.append((p, None, _r6(p.uncovered), True))
    parts = ln.parts or [(0, ln.phase, ln.gross)]
    if len(parts) == 1:
        row, phase, _ = parts[0]
        return [Segment(row, phase, ln, p, lot, q, unc) for p, lot, q, unc in stream]
    # Bölmeyi defterin altı ondalık hassasiyetinde TAM sayılarla yap.
    # Her parçayı bağımsız yuvarlayıp yuvarlanmamış kalanı azaltmak,
    # üç fazda 20 g -> 6.666667 * 3 = 20.000001 g üretiyordu.
    # Son faz, önceki fazlarda gerçekten yazılan paylardan kalanı alır.
    micro = 1_000_000
    amounts = [int(round(q * micro)) for _p, _lot, q, _unc in stream]
    out: List[Segment] = []
    si, left = 0, (amounts[0] if amounts else 0)
    for pi, (row, phase, need) in enumerate(parts):
        last = pi == len(parts) - 1
        need = int(round(need * micro))
        while si < len(stream) and (last or need > 0):
            p, lot, _q, unc = stream[si]
            take = left if last else min(left, need)
            if take > 0:
                out.append(Segment(row, phase, ln, p, lot, take / micro, unc))
            need -= take
            left -= take
            if left <= 0:
                si += 1
                left = amounts[si] if si < len(stream) else 0
    return out


# ─── Önizleme görünümü ──────────────────────────────────────────────────────

def _pick_view(p: Pick, names: Dict[int, str], ln: Line) -> dict:
    return {
        "item_id": p.item_id,
        "name": names.get(p.item_id, ""),
        "quantity": _r6(p.quantity),
        "inventory_id": p.inventory_id,
        "is_recipe_card": p.item_id == ln.item_id,
        "lots": [{"inventory_id": lot.id, "lot_number": lot.lot_number, "quantity": _r6(take),
                  "supplier_name": (lot.supplier.name if lot.supplier_id and lot.supplier else None)}
                 for lot, take in p.allocations],
        "uncovered": _r6(p.uncovered),
    }


def preview_payload(pl: Plan) -> dict:
    """`POST /api/production/preview` yanıtı (başlatmanın 400'ünde `lines`
    aynı biçimdedir).  Sayılar 6 haneye yuvarlı; biçimlendirme istemcide."""
    recipe = pl.recipe
    sel_label = "İngilizce" if pl.lang == "EN" else "Türkçe"
    lines_out = []
    by_unit: Dict[str, dict] = {}
    raw_net = raw_gross = 0.0
    for ln in pl.lines:
        card = pl.cards.get(ln.item_id)
        stock = float(card.current_stock or 0.0) if card is not None else next(
            (o["stock"] for o in ln.options if o["item_id"] == ln.item_id), 0.0)
        u = by_unit.setdefault(ln.unit, {"unit": ln.unit, "net": 0.0, "gross": 0.0, "loss": 0.0})
        u["net"] += ln.net
        u["gross"] += ln.gross
        u["loss"] += ln.gross - ln.net
        if ln.kind == "raw":
            raw_net += ln.net
            raw_gross += ln.gross
        lines_out.append({
            "key": ln.key,
            "recipe_item_id": ln.recipe_item_id,
            "recipe_item_name": pl.names.get(ln.recipe_item_id, ""),
            "item_id": ln.item_id,
            "item_name": pl.names.get(ln.item_id, ""),
            "kind": ln.kind,
            "is_ambalaj": ln.is_ambalaj,
            "language": ln.language,
            "unit": ln.unit,
            "phase": ln.phase or "",
            "parts": [{"phase": ph or "", "gross": _r6(g)} for _r, ph, g in ln.parts],
            "net": _r6(ln.net),
            "gross": _r6(ln.gross),
            "loss": _r6(ln.gross - ln.net),
            "factor": ln.factor,
            "stock": _r6(stock),
            "group": ln.group,
            "needs_choice": ln.needs_choice,
            "answered": ln.answered,
            "options": ln.options,
            "suggested": [dict(s, name=pl.names.get(s["item_id"], "")) for s in ln.suggested],
            "chosen": [_pick_view(p, pl.names, ln) for p in ln.chosen],
            "shortfall": ln.shortfall,
            "ok": not ln.errors and ln.answered,
            "errors": [e["detail"] for e in ln.errors],
        })
    warnings = []
    if pl.label_warnings:
        warnings.append(f"{sel_label} etiketi tanımlı olmayan kalem(ler): "
                        f"{', '.join(pl.label_warnings)} — stoktan düşülmeyecek.")
    if not any(ln.is_ambalaj for ln in pl.lines):
        warnings.append("Bu reçetede hiç ambalaj/etiket kalemi yok — kavanoz, kapak, etiket "
                        "stoktan düşülmeyecek.")
    blockers, seen = [], set()
    for e in pl.errors:
        if e["detail"] not in seen:
            seen.add(e["detail"])
            blockers.append(e["detail"])
    first = pl.first_error()
    target = recipe.target_item
    return {
        "recipe": {
            "id": recipe.id, "name": recipe.name,
            "target_item_id": recipe.target_item_id,
            "target_item_name": (target.name if target else recipe.description) or "",
            "output_quantity": recipe.output_quantity,
            "waste_percentage": float(recipe.waste_percentage or 0.0),
        },
        "quantity": pl.quantity,
        "label_language": pl.lang,
        "label_language_label": sel_label,
        "source_choice_enabled": pl.enabled,
        "lines": lines_out,
        "per_card": pl.per_card,
        "totals": {
            "raw_net": _r6(raw_net), "raw_gross": _r6(raw_gross),
            "raw_loss": _r6(raw_gross - raw_net),
            "by_unit": [{"unit": d["unit"], "net": _r6(d["net"]), "gross": _r6(d["gross"]),
                         "loss": _r6(d["loss"])} for d in by_unit.values()],
        },
        "label_warnings": pl.label_warnings,
        "needs_choice": [ln.key for ln in pl.lines if ln.needs_choice],
        "can_start": pl.can_start,
        "code": first["code"] if first else None,
        "blockers": blockers,
        "errors": pl.errors,
        "warnings": warnings,
    }
