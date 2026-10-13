# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
"Aynı malzeme" grup stoğu — raporların ORTAK çekirdeği (13.10.2026).

NEDEN VAR
─────────
Lab aynı malzemenin her tedarikçisini ayrı kart tutar (core/material_groups).
XHANTAN GUM: kart #105 HAMMADDESEPETİ 180 g + kart #636 YİĞİTOGLU KİMYA
24.688 g.  Satın Alma Planı "elimizde 180 g" diyordu.  Kullanıcı kararı:
raporlar grubun TOPLAMINI göstersin, kart dökümü altında dursun.

KURALLAR
────────
  • Birim: yalnız çevrilebilenler toplanır — g↔kg ve ml↔l (×1000), aynı
    `unit_norm` 1:1.  g ile ml TOPLANMAZ: grubun "taban" ailesi dışındaki
    kart `aside`'a düşer ve ayrı gösterilir (`pick_base_unit`).
  • Üretimde yerine kullanılabilirlik (`can_substitute`) DAHA SIKI: aktif,
    aynı panel, aynı tür (`stock_lots.lot_kind`, bitmiş ürün asla), aynı
    tüketim türü (etiket ≠ şişe), BİREBİR aynı `unit_norm` — üretim birim
    çevirmeden düşer.  `core.production_plan` seçenek kartlarını bu kuralla
    kurar; kural TEK yerde.
  • Stok `max(0, current_stock)` (numune lotları bu sayıda zaten yok).
  • Grup minimumu = üye minimumlarının tabana çevrilmiş EN BÜYÜĞÜ — toplamı
    sahte alarm üretirdi.
  • Kova: Stoklar sayfasının `classify`'ı (templates/ledger.html) BİREBİR —
    hammadde | ambalaj | etiket | finished | other.  Karışık kategorili grup
    kovaya göre bölünür; kovada 2'den az aktif üye kalırsa o kova grup sayılmaz.
  • TEK ANAHTAR: "yeterli mi" sonucunu değiştiren her şey (satın alma
    düşümü, Eksik/dashboard grup kuralı, bildirim bastırma)
    `group_stock_enabled(db)`'ye bağlıdır = üretimdeki
    `production.source_choice.enabled`.  Ayar 0 → eski kart başı kural
    (deploysuz geri dönüş).  Yalnız görünüm değiştiren Stoklar/Aylık her
    zaman gruplar.
  • Bilinen sınır: grup üyeliği tarihli tutulmaz — Aylık raporun
    dondurulmuş eski ayları BUGÜNKÜ gruplamayla gösterilir (dipnot yazar).

Döngü yok: bu modül üstte yalnız database + stock_lots + consumption'ı
içe aktarır; purchase_plan (birim/biçim) ve production_plan (ayar) geç
içe aktarılır — purchase_plan bu modülü üstte içe aktarabilir.
"""
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from sqlalchemy.orm import Session

from core.consumption import _kind as consumption_kind
from core.stock_lots import lot_kind
from database import Item, MaterialGroup, Supplier

EPS = 1e-9

BUCKETS = ("hammadde", "ambalaj", "etiket", "finished", "other")
BUCKET_LABELS = {"hammadde": "Hammadde", "ambalaj": "Ambalaj", "etiket": "Etiket",
                 "finished": "Bitmiş Ürün", "other": "Diğer"}

# Birim ailesi ve ailenin küçük birimine çarpan (unit_norm çıktısı üzerinden).
_FAMILY = {"g": "mass", "kg": "mass", "ml": "volume", "l": "volume", "adet": "count"}
_FACTOR = {"g": 1.0, "kg": 1000.0, "ml": 1.0, "l": 1000.0, "adet": 1.0}
_SMALL_FIRST = {"g": 0, "ml": 0, "adet": 0, "kg": 1, "l": 1}


# ─── Geç içe aktarımlar (döngü olmasın) ────────────────────────────────────

def unit_norm(u) -> str:
    """`core.purchase_plan.unit_norm` — g | kg | ml | l | adet."""
    from core.purchase_plan import unit_norm as _un
    return _un(u)


def group_stock_enabled(db: Session) -> bool:
    """Grup toplamı "yeterli mi" kararına girer mi — TEK ANAHTAR, üretimdeki
    kaynak seçimi ayarı (`production.source_choice.enabled`, satır yok = açık)."""
    from core.production_plan import source_choice_enabled
    return source_choice_enabled(db)


# ─── Saf yardımcılar ───────────────────────────────────────────────────────

def convert_qty(v, from_u, to_u) -> Optional[float]:
    """`v` miktarını `from_u` biriminden `to_u`'ya çevir.  Aynı `unit_norm`
    → 1:1; g↔kg, ml↔l → ×/÷1000; başka her durumda (g↔ml, g↔adet) None."""
    a, b = unit_norm(from_u), unit_norm(to_u)
    x = float(v or 0.0)
    if a == b:
        return x
    if _FAMILY[a] != _FAMILY[b]:
        return None
    return round(x * _FACTOR[a] / _FACTOR[b], 9)


def can_substitute(a, b) -> bool:
    """`b` kartı üretimde `a` kartının YERİNE düşülebilir mi — aktif, aynı
    panel, aynı tür (bitmiş ürün asla), aynı tüketim türü, BİREBİR aynı
    `unit_norm`.  Aynı grupta olmak çağıranın koşuludur (ItemRec grubu
    taşımaz).  ORM `Item` ve `consumption.ItemRec` ikisi de kabul edilir.
    `a`'nın kendisi pasif olabilir (reçete pasif kartı gösterebilir)."""
    if a is None or b is None or not b.is_active:
        return False
    if (a.domain or "cosmetics") != (b.domain or "cosmetics"):
        return False
    kind = lot_kind(a.category)
    if kind == "finished" or lot_kind(b.category) != kind:
        return False
    if consumption_kind(a) != consumption_kind(b):
        return False
    return unit_norm(a.unit) == unit_norm(b.unit)


def classify(category, pkg_type) -> str:
    """Stoklar sayfasındaki `classify` (templates/ledger.html) BİREBİR:
    kategori tam eşleşme, pkg_type küçük harf."""
    cat = category or ""
    pkg = (pkg_type or "").lower()
    if cat == "Hammadde":
        return "hammadde"
    if cat == "Bitmiş Ürün":
        return "finished"
    if cat == "Ambalaj":
        return "etiket" if pkg == "etiket" else "ambalaj"
    return "other"


def _attr(m, name, default=None):
    if isinstance(m, dict):
        return m.get(name, default)
    return getattr(m, name, default)


def _stock_of(m) -> float:
    s = _attr(m, "stock")
    if s is None:
        s = _attr(m, "current_stock", 0.0)
    return max(0.0, float(s or 0.0))


def pick_base_unit(members: Sequence) -> Tuple[Optional[str], list, list]:
    """Grubun taban birimi + (sayılan, ayrı) üyeler.

    Aile (kütle g/kg · hacim ml/l · adet) = en çok stoklu aile (küçük
    birime çevrilmiş toplam; eşitlikte üye sayısı, sonra ilk üyenin ailesi).
    Taban birim = ailede en çok üyenin birimi (eşitlikte stoğu büyük olan,
    sonra küçük birim g/ml).  Başka ailedeki üyeler `aside`.
    Üye: `unit` + `stock` (ya da `current_stock`) taşıyan nesne/sözlük.
    Boş liste → (None, [], [])."""
    members = list(members)
    if not members:
        return None, [], []
    fam_stock: Dict[str, float] = {}
    fam_count: Dict[str, int] = {}
    fam_first: Dict[str, int] = {}
    for i, m in enumerate(members):
        u = unit_norm(_attr(m, "unit"))
        f = _FAMILY[u]
        fam_stock[f] = fam_stock.get(f, 0.0) + _stock_of(m) * _FACTOR[u]
        fam_count[f] = fam_count.get(f, 0) + 1
        fam_first.setdefault(f, i)
    fam = max(fam_stock, key=lambda f: (round(fam_stock[f], 6), fam_count[f], -fam_first[f]))
    counted = [m for m in members if _FAMILY[unit_norm(_attr(m, "unit"))] == fam]
    aside = [m for m in members if _FAMILY[unit_norm(_attr(m, "unit"))] != fam]
    u_count: Dict[str, int] = {}
    u_stock: Dict[str, float] = {}
    for m in counted:
        u = unit_norm(_attr(m, "unit"))
        u_count[u] = u_count.get(u, 0) + 1
        u_stock[u] = u_stock.get(u, 0.0) + _stock_of(m) * _FACTOR[u]
    base = max(u_count, key=lambda u: (u_count[u], round(u_stock[u], 6), -_SMALL_FIRST[u]))
    return base, counted, aside


def amount_short(v, unit) -> str:
    """Kısa miktar metni — "180 g", "24,7 kg", "750 ml", "1,2 l", "12 adet"
    (`purchase_plan._amount_near(short=True)`; TR sayı biçimi Python'da)."""
    from core.purchase_plan import _amount_near
    return _amount_near(float(v or 0.0), unit, short=True)


_EXACT_DECIMALS = {"kg": 3, "l": 3, "g": 2, "ml": 2, "adet": 2}


def amount_exact(v, unit) -> str:
    """Kesin miktar metni — Eksik Hammaddeler grup satırı ve dashboard
    kritik grup girdisi (mevcut / min / eksik / numune).  `amount_short`
    1.000 g'dan sonra kg'a geçip 1 haneye yuvarladığı için 999,6 g ile
    1.000 g'ı "1 kg / 1 kg", 0,4 g eksiği "0 g" gösteriyordu.  Burada:
    verilen taban birim KORUNUR (g→kg geçişi yok), kg/l en çok 3, g/ml/adet
    en çok 2 ondalık, sondaki sıfırlar atılır, TR ayraçları ("1.234,56 g").
    Pozitif miktar asla "0" yazılmaz: en küçük adımın altındaysa o adım
    gösterilir ("0,01 g", "0,001 kg")."""
    from core.purchase_plan import tr_num
    u = unit_norm(unit)
    d = _EXACT_DECIMALS.get(u, 2)
    x = float(v or 0.0)
    a = abs(x)
    if a <= EPS:
        a = 0.0
    elif round(a, d) == 0:
        a = 10.0 ** -d
    t = tr_num(a, d)
    if "," in t:
        t = t.rstrip("0").rstrip(",")
    sign = "-" if x < 0 and t != "0" else ""
    return f"{sign}{t} {u}"


def member_label(m) -> str:
    """Dökümde kartın etiketi: tedarikçi adı, yoksa kart adı."""
    return " ".join(str(_attr(m, "supplier_name") or _attr(m, "name") or "—").split())


def breakdown_text(parts: Iterable) -> str:
    """"HAMMADDESEPETİ 180 g · YİĞİTOGLU KİMYA 24,7 kg" — her kart KENDİ
    biriminde.  Etiket tedarikçi adıdır; tedarikçisiz kart ya da aynı
    tedarikçinin birden çok kartı varsa kart adı yazılır."""
    parts = list(parts)
    labels = [member_label(p) for p in parts]
    out = []
    for p, lab in zip(parts, labels):
        if labels.count(lab) > 1:
            lab = " ".join(str(_attr(p, "name") or lab).split())
        out.append(f"{lab} {amount_short(_stock_of(p), _attr(p, 'unit'))}")
    return " · ".join(out)


def group_min(gs, base_unit: Optional[str] = None) -> float:
    """Grup minimumu = sayılan üyelerin minimumlarının tabana çevrilmiş EN
    BÜYÜĞÜ (toplam DEĞİL).  `group_min(gs)` — GroupStock; ya da
    `group_min(üyeler, taban_birim)`.  Minimum yoksa 0."""
    if base_unit is None:
        members, base_unit = gs.counted, gs.base_unit
    else:
        members = list(gs)
    best = 0.0
    for m in members:
        mn = _attr(m, "min_stock")
        if mn is None:
            mn = _attr(m, "min_stock_level", 0.0)
        mn = float(mn or 0.0)
        if mn <= EPS:
            continue
        c = convert_qty(mn, _attr(m, "unit"), base_unit)
        if c is not None and c > best:
            best = c
    return round(best, 6)


# ─── Yükleyici ─────────────────────────────────────────────────────────────

@dataclass
class GroupMember:
    id: int
    name: str
    unit: str                  # kartın kendi birimi (ham)
    stock: float               # max(0, current_stock)
    raw_stock: float           # current_stock (negatif olabilir)
    min_stock: float
    category: str
    pkg_type: Optional[str]
    supplier_id: Optional[int]
    supplier_name: Optional[str]

    @property
    def unit_norm(self) -> str:
        return unit_norm(self.unit)

    @property
    def label(self) -> str:
        return member_label(self)


@dataclass
class GroupStock:
    """Bir grubun bir kovadaki stoğu.  `total`/`min_stock` taban birimde
    (`base_unit`, unit_norm değeri); `counted` toplananlar, `aside` başka
    birim ailesindeki (ayrı gösterilen) aktif üyeler."""
    group_id: int
    name: str
    bucket: str
    base_unit: str
    members: List[GroupMember]
    counted: List[GroupMember]
    aside: List[GroupMember] = field(default_factory=list)
    total: float = 0.0
    min_stock: float = 0.0

    @property
    def member_ids(self) -> List[int]:
        return [m.id for m in self.members]

    @property
    def counted_ids(self) -> List[int]:
        return [m.id for m in self.counted]

    @property
    def aggregates(self) -> bool:
        """Toplam tek karttan fazlasını kapsıyor mu (grup kuralı anlamlı)."""
        return len(self.counted) >= 2

    @property
    def total_text(self) -> str:
        return amount_short(self.total, self.base_unit)

    @property
    def breakdown_text(self) -> str:
        return breakdown_text(self.counted)

    @property
    def aside_text(self) -> str:
        return breakdown_text(self.aside)

    def as_dict(self) -> dict:
        """JSON görünümü — metinler sunucuda biçimli (JS yeniden yazmasın)."""
        aside_ids = {m.id for m in self.aside}
        return {
            "group_id": self.group_id, "name": self.name, "bucket": self.bucket,
            "base_unit": self.base_unit, "total": self.total, "total_text": self.total_text,
            "min_stock": self.min_stock,
            "min_text": amount_short(self.min_stock, self.base_unit) if self.min_stock > EPS else "",
            "breakdown_text": self.breakdown_text, "aside_text": self.aside_text,
            "member_count": len(self.members), "aggregates": self.aggregates,
            "members": [{
                "item_id": m.id, "name": m.name, "unit": m.unit, "stock": round(m.stock, 6),
                "raw_stock": round(m.raw_stock, 6), "min_stock": round(m.min_stock, 6),
                "supplier_id": m.supplier_id, "supplier_name": m.supplier_name,
                "stock_text": amount_short(m.stock, m.unit), "counted": m.id not in aside_ids,
            } for m in self.members],
        }


def build_group(group_id: int, name: str, bucket: str, members: Sequence[GroupMember]) -> GroupStock:
    """Üyelerden GroupStock (saf) — taban birim, toplam, minimum."""
    members = sorted(members, key=lambda m: m.id)
    base, counted, aside = pick_base_unit(members)
    total = 0.0
    for m in counted:
        total += convert_qty(m.stock, m.unit, base) or 0.0
    gs = GroupStock(group_id=group_id, name=name, bucket=bucket, base_unit=base or "adet",
                    members=list(members), counted=counted, aside=aside, total=round(total, 6))
    gs.min_stock = group_min(gs)
    return gs


def load_groups(db: Session, domain: str, *, group_ids: Optional[Iterable[int]] = None
                ) -> Dict[Tuple[int, str], GroupStock]:
    """Panelin AKTİF gruplarının kova başına stoğu — TEK sorgu.

    Anahtar `(grup id, kova)`; kova `classify` (BUCKETS).  Yalnız aktif,
    aynı panel üyeler; kovada 2'den az üye kalırsa o kova yoktur.
    `group_ids` verilirse yalnız o gruplar okunur."""
    q = (db.query(Item.id, Item.name, Item.unit, Item.current_stock, Item.min_stock_level,
                  Item.category, Item.pkg_type, Item.supplier_id, Item.material_group_id,
                  MaterialGroup.name, Supplier.name)
         .join(MaterialGroup, MaterialGroup.id == Item.material_group_id)
         .outerjoin(Supplier, Supplier.id == Item.supplier_id)
         .filter(MaterialGroup.domain == domain, MaterialGroup.is_active == True,   # noqa: E712
                 Item.domain == domain, Item.is_active == True))                    # noqa: E712
    if group_ids is not None:
        ids = sorted({int(g) for g in group_ids if g})
        if not ids:
            return {}
        q = q.filter(Item.material_group_id.in_(ids))
    acc: Dict[Tuple[int, str], List[GroupMember]] = {}
    names: Dict[int, str] = {}
    for (iid, iname, unit, stock, mn, cat, pkg, sid, gid, gname, sname) in q.all():
        names[gid] = gname or f"Grup #{gid}"
        raw = float(stock or 0.0)
        acc.setdefault((gid, classify(cat, pkg)), []).append(GroupMember(
            id=iid, name=iname or "", unit=unit or "", stock=max(0.0, raw), raw_stock=raw,
            min_stock=float(mn or 0.0), category=cat or "", pkg_type=pkg,
            supplier_id=sid, supplier_name=(sname or None)))
    out: Dict[Tuple[int, str], GroupStock] = {}
    for (gid, bucket) in sorted(acc, key=lambda k: (k[0], BUCKETS.index(k[1]))):
        members = acc[(gid, bucket)]
        if len(members) < 2:
            continue
        out[(gid, bucket)] = build_group(gid, names[gid], bucket, members)
    return out


def index_by_item(groups: Dict[Tuple[int, str], GroupStock]) -> Dict[int, GroupStock]:
    """Kart id → bulunduğu GroupStock (sayılan + ayrı üyeler)."""
    return {m.id: gs for gs in groups.values() for m in gs.members}


# ─── Düşük stok bildirimi ──────────────────────────────────────────────────

def _old_rule(it) -> Optional[Tuple[str, float, float, str]]:
    mn = float(it.min_stock_level or 0.0)
    cur = float(it.current_stock or 0.0)
    if mn > 0 and cur <= mn:
        return (it.name or "", round(cur, 6), round(mn, 6), it.unit or "")
    return None


def group_alert_name(gs: GroupStock) -> str:
    return f"{gs.name} (aynı malzeme, {len(gs.counted)} kart)"


def low_stock_alerts(db: Session, items: Iterable, domain: str
                     ) -> List[Tuple[str, float, float, str]]:
    """Stoğu değişen kartların kritik stok uyarıları —
    `notify_low_stock(*t)` argümanları `(ad, mevcut, min, birim)`.

    Grupsuz kart (ya da ayar kapalı, grup pasif, kovada tek sayılan kart,
    başka birim ailesindeki kart): eski kural `min > 0 ve stok ≤ min`.
    Gruplu kart: grup başına TEK uyarı, yalnız grup toplamı ≤ grup minimumu
    (> 0) iken — grubu yeterliyse kartın kendi minimumu altında olsa bile
    uyarı gitmez.  Kart başına en çok bir kez (yinelenen kart atlanır)."""
    items = [it for it in items if it is not None]
    seen: set = set()
    uniq = []
    for it in items:
        if it.id in seen:
            continue
        seen.add(it.id)
        uniq.append(it)
    by_item: Dict[int, GroupStock] = {}
    gids = {it.material_group_id for it in uniq
            if getattr(it, "material_group_id", None)
            and (getattr(it, "domain", None) or "cosmetics") == domain}
    if gids and group_stock_enabled(db):
        by_item = {iid: gs for iid, gs in index_by_item(load_groups(db, domain, group_ids=gids)).items()
                   if gs.aggregates and iid in gs.counted_ids}
    out: List[Tuple[str, float, float, str]] = []
    done: set = set()
    for it in uniq:
        gs = by_item.get(it.id)
        if gs is None:
            row = _old_rule(it)
            if row is not None:
                out.append(row)
            continue
        key = (gs.group_id, gs.bucket)
        if key in done:
            continue
        done.add(key)
        if gs.min_stock > EPS and gs.total <= gs.min_stock + EPS:
            out.append((group_alert_name(gs), gs.total, gs.min_stock, gs.base_unit))
    return out
