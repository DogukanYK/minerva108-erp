# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
"Aynı malzeme" grupları — farklı tedarikçi kartlarını bağlar, BİRLEŞTİRMEZ.

NEDEN VAR (06.10.2026)
──────────────────────
Lab aynı malzemenin her tedarikçisini AYRI kart tutuyor (24.08 sonrası 18
kopya kümesinde "farklı ürünler, ayrı kalsın" kararı).  Stearil alkol üç
kartta: SETİL STEARİL ALKOL / Tatlıdilimler, CETYL STEARYL ALCOHOL /
Yiğitoğlu, CETEARYL ALCOHOL / Veser.  Sistem bunların aynı malzeme olduğunu
bilmiyordu — numune yanlış tedarikçinin kartına çevrildi (Naturalya jojobası
KRK GIDA kartına), satın alma planı alternatif tedarikçiyi göstermedi.

Grup stok ya da defter BİRLEŞTİRMEZ (o iş core/item_merge'ün); yalnız "bu
kartlar aynı malzeme" bilgisini taşır.  Kart en fazla tek grupta
(`Item.material_group_id`).  Ad AKTİF kayıtlarda panel içinde TR-katlanmış
tekil (`core.supplier_prices.normalize`).

İçerik:
  • `join` / `group_cluster` / `transfer_on_merge` — numune çevirme, kopya
    popup'ının "ayrı kalsın" kararı ve kart birleştirme kancaları.
  • `dissolve` / `prune` — aktif üyesi 2'nin altına düşen grup dağılır.
  • `group_payload` / `groups_payload` — arayüz ve satın alma için görünüm.
  • `material_key` + `suggest` — "bu kartlar aynı malzeme olabilir" önerisi
    (TR/EN eşanlamlılar; SETİL STEARİL ALKOL = CETYL STEARYL ALCOHOL =
    CETEARYL ALCOHOL).  Öneri yalnız ÖNERİDİR — grubu lab kurar.
  • `dismissed_pairs` / `add_dismissed` — lab'ın "bunlar farklı" kaydı
    (AppSetting `material_groups.dismissed.<domain>`).
Uçlar: routers/material_groups.py.
"""
import difflib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session

from core.supplier_prices import normalize as _tr_fold
from database import AppSetting, DuplicateItemDecision, Item, MaterialGroup, Supplier, to_tr


def find_name_conflict(db: Session, domain: str, name: str,
                       exclude_id: Optional[int] = None) -> Optional[MaterialGroup]:
    """Aynı panelde TR-katlanmış adı eşleşen AKTİF grup (yoksa None)."""
    key = _tr_fold(name)
    if not key:
        return None
    q = db.query(MaterialGroup).filter(MaterialGroup.domain == domain,
                                       MaterialGroup.is_active == True)   # noqa: E712
    if exclude_id:
        q = q.filter(MaterialGroup.id != exclude_id)
    for g in q.all():
        if _tr_fold(g.name) == key:
            return g
    return None


def unique_name(db: Session, domain: str, base: str) -> str:
    """`base` boşsa kullanılır; doluysa "base (2)", "base (3)" … — tohumlama ve
    otomatik açılan gruplar ad çakışmasında durmasın diye."""
    base = (base or "").strip()[:140] or "Aynı malzeme"
    if not find_name_conflict(db, domain, base):
        return base
    n = 2
    while find_name_conflict(db, domain, f"{base} ({n})"):
        n += 1
    return f"{base} ({n})"


def join(db: Session, a: Item, b: Item, *, actor: str) -> Optional[MaterialGroup]:
    """`b` kartını `a`'nın grubuna kat; `a` grupsuzsa ikisi için grup aç.

    • `a`'nın grubu varsa `b` ona eklenir.
    • yoksa `MaterialGroup(name=a.name, source='convert')` açılır, ikisi konur.
    • `b` zaten BAŞKA bir gruptaysa ya da kartlar farklı paneldeyse dokunulmaz
      → None (bir kart tek gruptadır; sessizce taşımak lab'ın kurduğu düzeni
      bozardı).
    Commit ÇAĞIRANA aittir (flush eder).
    """
    if a is None or b is None or a.id == b.id:
        return None
    if (a.domain or "cosmetics") != (b.domain or "cosmetics"):
        return None
    if a.material_group_id:
        grp = db.query(MaterialGroup).filter(MaterialGroup.id == a.material_group_id).first()
        if grp is not None and grp.is_active:
            if b.material_group_id and b.material_group_id != grp.id:
                return None
            b.material_group_id = grp.id
            return grp
    if b.material_group_id:
        return None
    dom = a.domain or "cosmetics"
    grp = MaterialGroup(name=unique_name(db, dom, a.name), domain=dom,
                        source="convert", created_by=(actor or "")[:100] or None)
    db.add(grp)
    db.flush()
    a.material_group_id = grp.id
    b.material_group_id = grp.id
    return grp


def group_cluster(db: Session, items: Sequence[Item], *, name: str, source: str,
                  source_ref: Optional[int] = None, actor: str = "") -> Optional[MaterialGroup]:
    """Kartları TEK grupta topla — kopya popup'ındaki "ayrı kalsın" kancası.

    • Yalnız aktif kartlar; aynı panelde ≥2 aktif kart yoksa None.
    • Kartlardan hiçbiri grupta değilse `name` ile yeni grup açılır
      (`source`/`source_ref` iz için).
    • Tam olarak BİR grup varsa grupsuz kartlar ona katılır.
    • İki ayrı grup varsa dokunulmaz → None (lab'ın kurduğu iki grubu sessizce
      birleştirmek düzeni bozardı; arayüzdeki "taşı" ile yapılır).
    Commit ÇAĞIRANA aittir (flush eder).
    """
    live = [it for it in items if it is not None and it.is_active]
    if len(live) < 2 or len({it.domain or "cosmetics" for it in live}) != 1:
        return None
    dom = live[0].domain or "cosmetics"
    gids = {it.material_group_id for it in live if it.material_group_id}
    groups = (db.query(MaterialGroup)
              .filter(MaterialGroup.id.in_(gids), MaterialGroup.is_active == True)   # noqa: E712
              .all()) if gids else []
    if len(groups) > 1:
        return None
    if groups:
        grp = groups[0]
    else:
        grp = MaterialGroup(name=unique_name(db, dom, name), domain=dom, source=source,
                            source_ref=source_ref, created_by=(actor or "")[:100] or None)
        db.add(grp)
        db.flush()
    for it in live:
        it.material_group_id = grp.id      # grupsuz ya da dağılmış gruba bakan kart
    db.flush()
    return grp


def group_decision(db: Session, decision: DuplicateItemDecision, *,
                   actor: str) -> List[MaterialGroup]:
    """Kopya popup'ında "ayrı kalsın" (keep) kararı → kümenin aktif kartları
    aynı malzeme grubunda (panel başına bir grup; ad = karar başlığı,
    source='dup_kept', source_ref = karar id).  Tohumlamadan
    (`database._backfill_material_groups_from_kept`) farkı: kartlardan biri
    zaten bir gruptaysa diğerleri ONA katılır (canlı karar, elle kurulmuş
    düzeni ezmez, tamamlar).  Commit ÇAĞIRANA aittir."""
    try:
        ids = [int(x) for x in json.loads(decision.item_ids)]
    except (TypeError, ValueError):
        return []
    by_domain: Dict[str, List[Item]] = {}
    for it in (db.query(Item)
               .filter(Item.id.in_(ids), Item.is_active == True)            # noqa: E712
               .order_by(Item.id).all()):
        by_domain.setdefault(it.domain or "cosmetics", []).append(it)
    out = []
    for items in by_domain.values():
        grp = group_cluster(db, items, name=decision.title, source="dup_kept",
                            source_ref=decision.id, actor=actor)
        if grp is not None:
            out.append(grp)
    return out


def active_member_count(db: Session, group_id: int) -> int:
    """Grubun AKTİF kart sayısı (çağıran önce flush etmeli — autoflush kapalı)."""
    return int(db.query(func.count(Item.id))
               .filter(Item.material_group_id == group_id,
                       Item.is_active == True).scalar() or 0)          # noqa: E712


def dissolve(db: Session, grp: MaterialGroup) -> List[int]:
    """Grubu dağıt: satır pasif kalır (iz), bütün kartların bağı çözülür.
    Dönüş: bağı çözülen kart id'leri.  Commit ÇAĞIRANA aittir."""
    db.flush()
    ids = []
    for it in db.query(Item).filter(Item.material_group_id == grp.id).all():
        it.material_group_id = None
        ids.append(it.id)
    grp.is_active = False
    grp.updated_at = datetime.utcnow()
    db.flush()
    return sorted(ids)


def prune(db: Session, group_id: Optional[int]) -> bool:
    """Aktif üyesi 2'nin altına düşen grubu dağıt — tek kartlık "aynı malzeme"
    grubu anlamsız.  Dağıttıysa True."""
    if not group_id:
        return False
    db.flush()
    grp = db.query(MaterialGroup).filter(MaterialGroup.id == group_id).first()
    if grp is None or not grp.is_active:
        return False
    if active_member_count(db, grp.id) >= 2:
        return False
    dissolve(db, grp)
    return True


def transfer_on_merge(db: Session, loser: Item,
                      survivor: Item) -> Tuple[Optional[int], List[dict]]:
    """core/item_merge.merge_items kancası — kaybeden kart pasifleşti.

    Kaybedenin grubu varsa ve kazananın (aktif) grubu yoksa kazanan o gruba
    geçer; kaybeden pasif üye olarak kalır (iz).  Ardından kaybedenin grubu
    budanır (kazanan zaten aynı gruptaysa ya da kendi grubunda kaldıysa grup
    tek aktif karta inmiş olabilir).
    Dönüş: (kazananın bu çağrıdan sonraki grup id'si ya da None, dağılan
    grupların [{id, name}]'i — çağıran audit'e yazar)."""
    gid = loser.material_group_id
    if not gid:
        return survivor.material_group_id, []
    grp = db.query(MaterialGroup).filter(MaterialGroup.id == gid).first()
    if grp is None or not grp.is_active:
        return survivor.material_group_id, []
    cur = survivor.material_group_id
    cur_live = bool(cur) and db.query(MaterialGroup.id).filter(
        MaterialGroup.id == cur, MaterialGroup.is_active == True).first() is not None  # noqa: E712
    if not cur_live and (survivor.domain or "cosmetics") == (grp.domain or "cosmetics"):
        survivor.material_group_id = grp.id
    dissolved = [{"id": grp.id, "name": grp.name}] if prune(db, grp.id) else []
    return survivor.material_group_id, dissolved


# ─── Görünüm ────────────────────────────────────────────────────────────────

def _unit_norm(unit) -> str:
    from core.purchase_plan import unit_norm      # geç import — döngü olmasın
    return unit_norm(unit)


def unit_warning(units: Iterable[str]) -> Optional[str]:
    """Birim ailesi farklıysa kullanıcı uyarısı (g/kg da farklı sayılır —
    stoklar çevrilmeden karşılaştırılamaz)."""
    fam = sorted({_unit_norm(u) for u in units})
    if len(fam) < 2:
        return None
    return (f"Birimler farklı ({', '.join(fam)}) — stok ve miktarlar kartlar arasında "
            f"doğrudan karşılaştırılamaz.")


def _member_view(it: Item, supplier_name: Optional[str]) -> dict:
    return {
        "item_id": it.id,
        "name": it.name,
        "name_tr": it.name_tr or "",
        "unit": it.unit or "",
        "category": it.category or "",
        "supplier_id": it.supplier_id,
        "supplier_name": supplier_name,
        "stock": round(float(it.current_stock or 0.0), 4),
        "is_active": bool(it.is_active),
    }


def _group_view(grp: MaterialGroup, members: List[dict]) -> dict:
    units = sorted({_unit_norm(m["unit"]) for m in members if m["is_active"]})
    return {
        "id": grp.id,
        "name": grp.name,
        "note": grp.note or "",
        "source": grp.source or "manual",
        "created_by": grp.created_by or "",
        "created_at": to_tr(grp.created_at).strftime("%d.%m.%Y %H:%M") if grp.created_at else "",
        "members": members,
        "active_count": sum(1 for m in members if m["is_active"]),
        "units": units,
        "unit_mismatch": len(units) > 1,
    }


def _member_rows(db: Session, group_ids: List[int], domain: Optional[str] = None):
    q = (db.query(Item, Supplier.name)
         .outerjoin(Supplier, Supplier.id == Item.supplier_id)
         .filter(Item.material_group_id.in_(group_ids)))
    if domain:
        q = q.filter(Item.domain == domain)
    return q.order_by(Item.is_active.desc(), Item.name, Item.id).all()


def group_payload(db: Session, group_id: Optional[int]) -> Optional[dict]:
    """Grup + üyeleri: ``{id, name, note, source, created_by, created_at,
    members: [{item_id, name, name_tr, unit, category, supplier_id,
    supplier_name, stock, is_active}], active_count, units, unit_mismatch}``
    — grup yok/pasifse None.

    Pasif kartlar da listelenir (`is_active` ile işaretli) — numunesi pasif
    karta bağlı kalmış lotlar arayüzde gizlenmesin.  `units` yalnız aktif
    üyelerin birim aileleridir (`unit_norm`)."""
    if not group_id:
        return None
    grp = db.query(MaterialGroup).filter(MaterialGroup.id == group_id).first()
    if grp is None or not grp.is_active:
        return None
    return _group_view(grp, [_member_view(it, sname)
                             for it, sname in _member_rows(db, [grp.id])])


def groups_payload(db: Session, domain: str, q: Optional[str] = None) -> List[dict]:
    """Paneldeki aktif gruplar (`group_payload` şekliyle), ada göre sıralı.
    `q` TR-katlanmış alt dize: grup adı, üye adı / TR adı ya da tedarikçi."""
    groups = (db.query(MaterialGroup)
              .filter(MaterialGroup.domain == domain,
                      MaterialGroup.is_active == True).all())          # noqa: E712
    if not groups:
        return []
    by_gid: Dict[int, List[dict]] = {}
    for it, sname in _member_rows(db, [g.id for g in groups], domain):
        by_gid.setdefault(it.material_group_id, []).append(_member_view(it, sname))
    out = [_group_view(g, by_gid.get(g.id, [])) for g in groups]
    key = _tr_fold(q) if q else ""
    if key:
        def hit(g):
            if key in _tr_fold(g["name"]):
                return True
            return any(key in _tr_fold(m["name"]) or key in _tr_fold(m["name_tr"])
                       or key in _tr_fold(m["supplier_name"] or "") for m in g["members"])
        out = [g for g in out if hit(g)]
    out.sort(key=lambda g: (_tr_fold(g["name"]), g["id"]))
    return out


# ─── "Bunlar farklı" kaydı ──────────────────────────────────────────────────

DISMISSED_KEY = "material_groups.dismissed.{domain}"


def _parse_pairs(raw) -> Set[frozenset]:
    try:
        data = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return set()
    out = set()
    for p in data if isinstance(data, list) else []:
        try:
            pair = frozenset(int(x) for x in p)
        except (TypeError, ValueError):
            continue
        if len(pair) == 2:
            out.add(pair)
    return out


def dismissed_pairs(db: Session, domain: str) -> Set[frozenset]:
    """Lab'ın "bunlar farklı malzeme" dediği kart çiftleri (panel bazında)."""
    row = (db.query(AppSetting)
           .filter(AppSetting.key == DISMISSED_KEY.format(domain=domain)).first())
    return _parse_pairs(row.value) if row else set()


def add_dismissed(db: Session, domain: str, item_ids: Iterable[int]) -> int:
    """Verilen kartların BÜTÜN çiftlerini "farklı" diye kaydet; yeni çift
    sayısını döner.  Commit ÇAĞIRANA aittir."""
    ids = sorted({int(i) for i in item_ids})
    key = DISMISSED_KEY.format(domain=domain)
    row = db.query(AppSetting).filter(AppSetting.key == key).with_for_update().first()
    pairs = _parse_pairs(row.value) if row else set()
    added = 0
    for x in range(len(ids)):
        for y in range(x + 1, len(ids)):
            p = frozenset((ids[x], ids[y]))
            if p not in pairs:
                pairs.add(p)
                added += 1
    value = json.dumps(sorted(sorted(p) for p in pairs))
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    db.flush()
    return added


# ─── Malzeme anahtarı ───────────────────────────────────────────────────────
# Kart adları TR/EN karışık ve tedarikçiye göre farklı yazılıyor: SETİL
# STEARİL ALKOL / CETYL STEARYL ALCOHOL / CETEARYL ALCOHOL aynı malzeme.
# Anahtar = eşanlamlıları kanonik İngilizce belirteçe çeviren, sıralı belirteç
# kümesi.  Rakamlar KORUNUR (PEG-40 ≠ PEG-60), "(NUMUNE)" ve firma ön/son eki
# atılır.

_SYNONYM_TABLE = (
    (("ALCOHOL",), ("alkol", "alkolu", "alcohol")),
    (("CETYL",), ("setil", "cetyl")),
    (("STEARYL",), ("stearil", "stearyl")),
    (("CETYL", "STEARYL"), ("cetearyl", "setearil", "cetostearyl", "setostearil")),
    (("OIL",), ("yag", "yagi", "yaglari", "oil", "oils")),
    (("EXTRACT",), ("ekstrakt", "ekstrakti", "ekstresi", "ekstre", "extract")),
    (("HYDROSOL",), ("hidrosol", "hidrosolu", "hydrosol", "hidrolat", "hydrolat")),
    (("GLYCERIN",), ("gliserin", "glycerin", "glycerine")),
    (("GLUCOSIDE",), ("glukozit", "glucoside", "glukozid")),
    (("LAVENDER",), ("lavanta", "lavender")),
    (("ORANGE",), ("portakal", "orange")),
    (("ALMOND",), ("badem", "almond")),
    (("VITAMIN",), ("vitamin", "vitamini")),
)
#: katlanmış küçük harf belirteç → kanonik belirteç(ler)
SYNONYMS: Dict[str, Tuple[str, ...]] = {w: canon for canon, words in _SYNONYM_TABLE
                                        for w in words}
ESSENTIAL_OIL = "ESSENTIALOIL"
#: "uçucu yağ(ı)" / "essential oil" → tek belirteç (yalnız "uçucu" da sayılır)
_ESSENTIAL_BIGRAMS = frozenset({("ucucu", "yag"), ("ucucu", "yagi"), ("ucucu", "yaglari"),
                                ("essential", "oil"), ("essential", "oils")})
_ESSENTIAL_SOLO = frozenset({"ucucu", "essential"})
STOP_WORDS = frozenset({"saf", "dogal", "organik", "natural", "organic", "pure",
                        "numune", "numunesi", "sample"})

_PAREN = re.compile(r"[(\[]([^()\[\]]*)[)\]]")
_DASH_SEP = re.compile(r"\s+[-–—]+\s+")
_PCT_100 = re.compile(r"%\s*100(?![\d.,])|(?<![\d.,])100\s*%")
_FIRM_TOKENS = frozenset({"GMBH", "LTD", "LTDSTI", "STI", "AS", "INC", "LLC", "SRL",
                          "SPA", "BV", "AG", "KG", "CORP", "PLC"})
_FIRM_PARTS = ("KIMYA", "KIMYEVI", "CHEM")


def _is_firm(part: str, supplier_keys) -> bool:
    """"PERA GmbH", "X Kimya Ltd. Şti.", "UMAYCHEM" ya da paneldeki bir
    tedarikçinin anahtarı (`core.purchase_pricing.supplier_key`)."""
    from core.purchase_plan import alnum_fold
    toks = [t for t in (alnum_fold(w) for w in re.split(r"[\s,/&()+\-]+", part.replace(".", "")))
            if t]
    if not toks:
        return False
    if any(t in _FIRM_TOKENS for t in toks) or any(m in t for t in toks for m in _FIRM_PARTS):
        return True
    if supplier_keys:
        from core.purchase_pricing import supplier_key
        k = supplier_key(part)
        return bool(k) and k in supplier_keys
    return False


def _strip_affixes(name: str, supplier_keys=()) -> str:
    """"(NUMUNE)"/"[SAMPLE]" parantezleri, "%100"/"100%" ve " – " ile ayrılmış
    firma ön/son ekini at ("PERA GmbH – Golden Jojoba Oil", çevirme
    penceresinin önerdiği "JOJOBA YAĞI — NATURALYA").  Ad hiç boşalmaz."""
    from core.purchase_plan import alnum_fold

    def paren(m):
        inner = alnum_fold(m.group(1))
        return " " if ("NUMUNE" in inner or "SAMPLE" in inner) else m.group(0)

    s = _PCT_100.sub(" ", _PAREN.sub(paren, name or ""))
    parts = [p for p in _DASH_SEP.split(s) if p.strip()]
    if len(parts) >= 2 and _is_firm(parts[0], supplier_keys):
        parts = parts[1:]
    if len(parts) >= 2 and _is_firm(parts[-1], supplier_keys):
        parts = parts[:-1]
    return " ".join(p.strip() for p in parts)


def material_key(name, supplier_keys: Iterable[str] = ()) -> Tuple[str, ...]:
    """Kart adının malzeme anahtarı — sıralı, tekil kanonik belirteçler.

        SETİL STEARİL ALKOL / CETYL STEARYL ALCOHOL / CETEARYL ALCOHOL
            → ('ALCOHOL', 'CETYL', 'STEARYL')
        JOJOBA YAĞI (NUMUNE) / JOJOBA YAGI → ('JOJOBA', 'OIL')
        LAVANTA UÇUCU YAĞI → ('ESSENTIALOIL', 'LAVENDER')

    Katlama `core.purchase_plan.alnum_spaced` (TR harfleri, büyük/küçük,
    noktalama).  `supplier_keys`: paneldeki tedarikçilerin `supplier_key`
    kümesi — " – " ile ayrılmış ön/son ek bunlardan biriyse atılır.  Eşanlamlı
    tablosunda olmayan belirteç olduğu gibi (büyük harf) kalır; boş ad → ().
    """
    from core.purchase_plan import alnum_spaced
    keys = supplier_keys if isinstance(supplier_keys, (set, frozenset)) else set(supplier_keys or ())
    toks = alnum_spaced(_strip_affixes(name or "", keys)).split()
    out: Set[str] = set()
    i = 0
    while i < len(toks):
        t = toks[i]
        if i + 1 < len(toks) and (t, toks[i + 1]) in _ESSENTIAL_BIGRAMS:
            out.add(ESSENTIAL_OIL)
            i += 2
            continue
        i += 1
        if t in STOP_WORDS:
            continue
        if t in _ESSENTIAL_SOLO:
            out.add(ESSENTIAL_OIL)
            continue
        out.update(SYNONYMS.get(t, (t.upper(),)))
    return tuple(sorted(out))


# ─── Öneri motoru ───────────────────────────────────────────────────────────

# Belirteç kümesi örtüşmesi — 2/3'ün ÜSTÜ: tek belirteç eksik iki/üç belirteçli
# alt küme benzer DEĞİL (SETİL ALKOL ↔ SETİL STEARİL ALKOL, BADEM YAĞI ↔ ACI
# BADEM YAĞI, E VİTAMİNİ ↔ VİTAMİN E ASETAT, LAVANTA ↔ MELEZ LAVANTA UÇUCU
# YAĞI ayrı malzemeler); 3/4 (CETEARYL ALCOHOL ↔ … NF) benzer.
SIMILAR_JACCARD = 0.75
SIMILAR_RATIO = 0.9         # difflib (yazım hatası: STERAİL ↔ STEARİL)
SUGGEST_MAX = 200


@dataclass
class MatRec:
    """`suggest`'in saf girdisi — bir ürün kartı."""
    id: int
    name: str
    unit: str = ""
    category: str = "Hammadde"
    is_active: bool = True
    domain: str = "cosmetics"
    material_group_id: Optional[int] = None
    name_tr: Optional[str] = None
    supplier_id: Optional[int] = None
    supplier_name: Optional[str] = None
    stock: float = 0.0


class _Key:
    __slots__ = ("toks", "set", "text", "digits", "_sm")

    def __init__(self, toks: Tuple[str, ...]):
        self.toks = toks
        self.set = frozenset(toks)
        self.text = " ".join(toks)
        self.digits = tuple(sorted(re.findall(r"\d+", self.text)))
        self._sm = None

    def matcher(self) -> difflib.SequenceMatcher:
        """seq2 = bu anahtar — b2j bir kez kurulur (çift döngüde set_seqs
        her seferinde yeniden kurardı: 900 kartta 1,7 sn)."""
        if self._sm is None:
            self._sm = difflib.SequenceMatcher(None, "", self.text, autojunk=False)
        return self._sm


def _short_token_diff(a: frozenset, b: frozenset) -> bool:
    """Anahtarlar tek kısa (≤2 harf) belirteçle ayrışıyor: E ↔ C VİTAMİNİ."""
    x, y = a - b, b - a
    return (len(x) == 1 and len(y) == 1
            and len(next(iter(x))) <= 2 and len(next(iter(y))) <= 2)


def _one_typo(a: str, b: str) -> bool:
    """`a` ile `b` arasında en çok bir yazım hatası: tek harf ekleme / silme /
    değiştirme ya da komşu iki harfin yer değiştirmesi (STERAIL ↔ STEARIL).
    Y ve I aynı sayılır — TR/EN yazım farkı (STEARİL ↔ STEARYL, SODYUM ↔
    SODIUM).  LAURIL ↔ LAURET (iki harf) yazım hatası DEĞİL — SLS ile SLES."""
    a, b = a.replace("Y", "I"), b.replace("Y", "I")
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    i = 0
    while i < la and i < lb and a[i] == b[i]:
        i += 1
    if la == lb:
        if a[i + 1:] == b[i + 1:]:
            return True                                      # değiştirme
        return (i + 1 < la and a[i] == b[i + 1] and a[i + 1] == b[i]
                and a[i + 2:] == b[i + 2:])                  # komşu yer değiştirme
    if la > lb:
        return a[i + 1:] == b[i:]                            # silme
    return a[i:] == b[i + 1:]                                # ekleme


def _similarity(ka: _Key, kb: _Key) -> float:
    """0 = benzer değil; aksi hâlde Jaccard ya da difflib oranı.

    difflib yolunda anahtarlar TEK belirteçle ayrışıyorsa (biri ötekinin
    yerinde) bu yalnız yazım hatasıysa benzerdir (`_one_typo`): SODYUM LAURİL
    SÜLFAT ↔ SODYUM LAURET SÜLFAT farklı malzeme, ZINC OXIDE ↔ ZINC OXID değil."""
    if ka.digits != kb.digits:
        return 0.0
    score = 0.0
    inter = len(ka.set & kb.set)
    if inter:
        jac = inter / len(ka.set | kb.set)
        if jac >= SIMILAR_JACCARD:
            score = jac
    if not score:
        la, lb = len(ka.text), len(kb.text)
        if 2 * min(la, lb) < SIMILAR_RATIO * (la + lb):       # real_quick_ratio
            return 0.0
        sm = ka.matcher()
        sm.set_seq1(kb.text)
        if sm.quick_ratio() < SIMILAR_RATIO:
            return 0.0
        r = sm.ratio()
        if r < SIMILAR_RATIO:
            return 0.0
        x, y = ka.set - kb.set, kb.set - ka.set
        if len(x) == 1 and len(y) == 1 and not _one_typo(next(iter(x)), next(iter(y))):
            return 0.0
        score = r
    return 0.0 if _short_token_diff(ka.set, kb.set) else score


def _item_view(r: MatRec, groups: Dict[int, str]) -> dict:
    gid = r.material_group_id if r.material_group_id in groups else None
    return {
        "item_id": r.id, "name": r.name, "name_tr": r.name_tr or "",
        "unit": r.unit or "", "category": r.category or "",
        "supplier_id": r.supplier_id, "supplier_name": r.supplier_name,
        "stock": round(float(r.stock or 0.0), 4),
        "material_group_id": gid, "material_group_name": groups.get(gid) if gid else None,
    }


def _suggestion(kind: str, members: List[MatRec], groups: Dict[int, str], *,
                score: float, key: Optional[Tuple[str, ...]] = None,
                title: Optional[str] = None, decision_id: Optional[int] = None,
                supplier_keys=()) -> dict:
    members = sorted(members, key=lambda r: r.id)
    gids = []
    for r in members:
        g = r.material_group_id if r.material_group_id in groups else None
        if g is not None and g not in gids:
            gids.append(g)
    units = sorted({_unit_norm(r.unit) for r in members})
    if len(gids) == 1:
        action = "join"
        title = groups[gids[0]]
    elif gids:
        action = "move"
    else:
        action = "create"
    if not title:
        title = (_strip_affixes(members[0].name, supplier_keys) or members[0].name).strip()
    ids = [r.id for r in members]
    return {
        "id": f"{kind}:{'-'.join(str(i) for i in ids)}",
        "kind": kind,
        "source": "pending_decision" if kind == "pending_decision" else "name",
        "decision_id": decision_id,
        "score": round(score, 3),
        "key": " ".join(key) if key else None,
        "title": (title or "")[:150],
        "item_ids": ids,
        "items": [_item_view(r, groups) for r in members],
        "units": units,
        "unit_mismatch": len(units) > 1,
        "group": {"id": gids[0], "name": groups[gids[0]]} if len(gids) == 1 else None,
        "groups": [{"id": g, "name": groups[g]} for g in gids],
        "action": action,
    }


def suggest(items: Iterable[MatRec], groups: Dict[int, str],
            dismissed: Iterable = (), pending: Iterable = (), *,
            supplier_keys: Iterable[str] = ()) -> List[dict]:
    """"Aynı malzeme olabilir" önerileri — SAF (DB'siz); yükleyici
    `load_suggestions`.

    Girdi: `items` paneldeki kartlar (`MatRec`), `groups` aktif grup
    {id: ad}, `dismissed` lab'ın "farklı" dediği çiftler, `pending` bekleyen
    kopya kümeleri [(karar id, başlık, [kart id])].

    • ``same_key`` (skor 1.0): `material_key`'i (ad ya da TR ad) aynı kartlar —
      bağlı bileşen olarak tek öneri.  Bileşen içinde reddedilmiş çift
      kalırsa (126 ↔ 593 "farklı", ikisi de 599'la aynı anahtarda) bileşen
      reddi taşımayan alt kümelere bölünür (`_split_component`).
    • ``similar``: Jaccard > 2/3 (`SIMILAR_JACCARD`) ya da difflib ≥ 0.9;
      addaki sayılar farklıysa, E/C VİTAMİNİ gibi tek kısa belirteç farkında
      ve difflib yolunda yazım hatası olmayan tek belirteç farkında (LAURİL ↔
      LAURET) benzer DEĞİL.  Aynı (bileşen | grup | kart) ikilisi için en iyi
      çift tek öneri olur.
    • ``pending_decision``: bekleyen kopya kümeleri (`decision_id` ile).
    Yalnız aktif, aynı panel, hammadde-benzeri (Ambalaj / Bitmiş Ürün değil)
    kartlar; aynı gruptakiler ve reddedilen çiftler hariç — red GRUP
    üzerinden de geçerli: 126 ↔ 593 "farklı" ve 126 bir gruptaysa 593 o
    gruba önerilmez.  Her öneride `unit_mismatch` (birim ailesi farklı:
    599/593 "adet" ↔ 126 "g") ve `action` (create | join | move) vardır.
    """
    from core.purchase_plan import _single_short_token_diff
    from core.stock_lots import lot_kind

    groups = dict(groups or {})
    skeys = set(supplier_keys or ())
    dis: Set[frozenset] = set()
    for p in dismissed or ():
        try:
            pair = frozenset(int(x) for x in p)
        except (TypeError, ValueError):
            continue
        if len(pair) == 2:
            dis.add(pair)
    active = {r.id: r for r in items if r.is_active}
    recs = {i: r for i, r in active.items() if lot_kind(r.category) == "raw"}

    def gid(r):
        return r.material_group_id if r.material_group_id in groups else None

    # "Farklı" kaydı kart → kartlar; grup üyeleri (aktif hammadde kartları).
    # Gruptaki karta katılmak gruba katılmaktır: red grubun her üyesi için
    # geçerli (`closure`).
    dis_adj: Dict[int, Set[int]] = {}
    for p in dis:
        x, y = tuple(p)
        dis_adj.setdefault(x, set()).add(y)
        dis_adj.setdefault(y, set()).add(x)
    members: Dict[int, Set[int]] = {}
    for r in recs.values():
        if gid(r) is not None:
            members.setdefault(gid(r), set()).add(r.id)

    def closure(r: MatRec) -> Set[int]:
        g = gid(r)
        return members.get(g, {r.id}) if g is not None else {r.id}

    def conflict(xs: Set[int], ys: Set[int]) -> bool:
        return any(not dis_adj.get(x, set()).isdisjoint(ys) for x in xs)

    def blocked(a: MatRec, b: MatRec) -> bool:
        g = gid(a)
        return ((g is not None and g == gid(b))
                or (a.domain or "cosmetics") != (b.domain or "cosmetics")
                or (bool(dis_adj) and conflict(closure(a), closure(b))))

    def _split_component(ids: List[int]) -> List[List[int]]:
        """Bileşeni reddedilmiş çift taşımayan alt kümelere böl (açgözlü, id
        sırasıyla; kart kendi grubunun bütün üyeleriyle birlikte sayılır).
        Red yoksa bileşenin kendisi döner."""
        parts: List[List[int]] = []
        seen: List[Set[int]] = []
        for i in ids:
            c = closure(recs[i])
            for k, part in enumerate(parts):
                if not conflict(c, seen[k]):
                    part.append(i)
                    seen[k] |= c
                    break
            else:
                parts.append([i])
                seen.append(set(c))
        return parts

    keys: Dict[int, List[_Key]] = {}
    for i, r in recs.items():
        ks: List[_Key] = []
        for n in (r.name, r.name_tr):
            t = material_key(n, skeys) if n else ()
            if t and all(k.toks != t for k in ks):
                ks.append(_Key(t))
        if ks:
            keys[i] = ks

    # ── Aynı anahtar → bağlı bileşenler ──
    parent: Dict[int, int] = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    bucket: Dict[Tuple[str, ...], List[int]] = {}
    for i in sorted(keys):
        for k in keys[i]:
            bucket.setdefault(k.toks, []).append(i)
    for ids in bucket.values():
        for x in range(len(ids)):
            for y in range(x + 1, len(ids)):
                a, b = recs[ids[x]], recs[ids[y]]
                if not blocked(a, b):
                    ra, rb = find(a.id), find(b.id)
                    if ra != rb:
                        parent[max(ra, rb)] = min(ra, rb)
    comps: Dict[int, List[int]] = {}
    for i in list(parent):
        comps.setdefault(find(i), []).append(i)
    comps = {root: sorted(ids) for root, ids in comps.items() if len(ids) >= 2}
    comp_of = {i: root for root, ids in comps.items() for i in ids}

    out: List[dict] = []
    for root, ids in comps.items():
        # Union geçişlidir: 126–599 ve 593–599 bağlıysa 126 ↔ 593 "farklı"
        # denmiş olsa da üçü tek bileşende kalırdı → reddi taşımayan parçalar.
        for part in (_split_component(ids) if dis_adj else [ids]):
            gs = {gid(recs[i]) for i in part}
            if len(part) < 2 or (len(gs) == 1 and None not in gs):
                continue                                   # tek kart ya da hepsi aynı grupta
            out.append(_suggestion("same_key", [recs[i] for i in part], groups, score=1.0,
                                   key=keys[part[0]][0].toks, supplier_keys=skeys))

    # ── Benzer çiftler ──
    # Çift döngü O(n²): ucuz elemeler (rakam imzası, ortak belirteç yoksa
    # uzunluk penceresi) satır içinde, pahalı difflib yalnız geçenlerde; grup/
    # red kontrolü yalnız benzer çıkan (az sayıdaki) çiftte.
    def ident(i):
        if i in comp_of:
            return ("c", comp_of[i])
        g = gid(recs[i])
        return ("g", g) if g is not None else ("i", i)

    best: Dict[frozenset, Tuple[float, int, int]] = {}
    order = sorted(keys)
    min_ratio = SIMILAR_RATIO
    for x, i in enumerate(order):
        ki = keys[i]
        for j in order[x + 1:]:
            score = 0.0
            for ka in ki:
                for kb in keys[j]:
                    if ka.digits != kb.digits:
                        continue
                    if ka.set.isdisjoint(kb.set):
                        la, lb = len(ka.text), len(kb.text)
                        if 2 * (la if la < lb else lb) < min_ratio * (la + lb):
                            continue
                    sc = _similarity(ka, kb)
                    if sc > score:
                        score = sc
            if not score:
                continue
            ia, ib = ident(i), ident(j)
            if ia == ib:
                continue
            a, b = recs[i], recs[j]
            if blocked(a, b) or _single_short_token_diff(a.name, b.name):
                continue
            pk = frozenset((ia, ib))
            if pk not in best or score > best[pk][0]:
                best[pk] = (score, i, j)
    for score, i, j in best.values():
        out.append(_suggestion("similar", [recs[i], recs[j]], groups, score=score,
                               key=keys[i][0].toks, supplier_keys=skeys))

    # ── Bekleyen kopya kümeleri ──
    pend_sets: List[Set[int]] = []
    for row in pending or ():
        dec_id, title, ids = row
        members = [active[i] for i in sorted({int(x) for x in ids}) if i in active]
        if len(members) < 2:
            continue
        doms = {m.domain or "cosmetics" for m in members}
        if len(doms) != 1:
            continue
        gset = {gid(m) for m in members}
        if len(gset) == 1 and None not in gset:
            continue                                   # hepsi zaten aynı grupta
        pairs = [frozenset((members[x].id, members[y].id))
                 for x in range(len(members)) for y in range(x + 1, len(members))]
        if all(p in dis for p in pairs):
            continue
        pend_sets.append({m.id for m in members})
        out.append(_suggestion("pending_decision", members, groups, score=1.0,
                               title=title, decision_id=dec_id, supplier_keys=skeys))
    if pend_sets:
        out = [s for s in out if s["kind"] == "pending_decision"
               or not any(set(s["item_ids"]) <= p for p in pend_sets)]

    rank = {"same_key": 0, "pending_decision": 1, "similar": 2}
    out.sort(key=lambda s: (-s["score"], rank[s["kind"]], _tr_fold(s["title"]), s["item_ids"]))
    return out[:SUGGEST_MAX]


def load_suggestions(db: Session, domain: str) -> List[dict]:
    """`suggest`'i paneldeki kartlar, aktif gruplar, "farklı" kaydı ve
    bekleyen kopya kümeleriyle besle."""
    from core.purchase_pricing import supplier_key

    sup_rows = db.query(Supplier.id, Supplier.name, Supplier.domain).all()
    sup_names = {sid: name for sid, name, _ in sup_rows}
    skeys = {supplier_key(name) for _, name, dom in sup_rows
             if (dom or "cosmetics") == domain} - {""}
    items = [MatRec(id=it.id, name=it.name or "", unit=it.unit or "",
                    category=it.category or "", is_active=bool(it.is_active),
                    domain=it.domain or "cosmetics", material_group_id=it.material_group_id,
                    name_tr=it.name_tr, supplier_id=it.supplier_id,
                    supplier_name=sup_names.get(it.supplier_id) if it.supplier_id else None,
                    stock=float(it.current_stock or 0.0))
             for it in db.query(Item).filter(Item.domain == domain,
                                             Item.is_active == True).all()]   # noqa: E712
    groups = dict(db.query(MaterialGroup.id, MaterialGroup.name)
                  .filter(MaterialGroup.domain == domain,
                          MaterialGroup.is_active == True).all())     # noqa: E712
    pending = []
    for dec in (db.query(DuplicateItemDecision)
                .filter(DuplicateItemDecision.status == "pending")
                .order_by(DuplicateItemDecision.id).all()):
        try:
            ids = [int(x) for x in json.loads(dec.item_ids)]
        except (TypeError, ValueError):
            continue
        pending.append((dec.id, dec.title, ids))
    return suggest(items, groups, dismissed_pairs(db, domain), pending, supplier_keys=skeys)
