"""758 JOJOBA UÇUCU YAĞI (NUMUNE) çift sayım düzeltmesi — 06.10.2026
(idempotent, kuru çalıştırma varsayılan).

**Ne oldu:** 758 numaralı kartın tek fiziksel varlığı 20 ml'lik numune lotu
(lot NUMUNE, DOĞASA).  Numune o tarihte henüz stok sayılmıyordu; Betül Hanım
10.09.2026'da kartın stoğunu elle 0 → 20 ml yaptı (`adjust_stock`,
"Stok düzeltme — … | Sebep: DÜZELT", Adjustment +20).  05.10.2026'da aynı
numune Ürünler → Numune → "Stoğa çevir" ile çevrildi; çevirme +20'lik Input
daha yazdı.  Sonuç: kart 40 ml gösteriyor, lot 20 ml — aynı 20 ml iki kez
sayıldı.  Kod tarafı ayrıca düzeltildi: çevirme artık numune geldikten sonra
yapılmış elle "Stok düzeltme"leri gösterip onay istiyor ve "Zaten sayıldı —
yalnız lotu bağla" (`link_only`, defter/stok değişmez) seçeneği sunuyor
(`routers/inventory.py::_count_guard`).  Bu script yalnız 758'in GEÇMİŞ
hasarını onarır.

Yaptıkları — TEK commit'te:

  ① **Doğrulama** — 758 birebir beklenen durumda mı: ad, birim ml, stok 40,
     normal lot toplamı 20, 10.09.2026 tarihli +20 "Stok düzeltme" Adjustment'ı
     TEK, 05.10.2026 tarihli +20 çevirme Input'u (lot NUMUNE) TEK.  Herhangi
     biri uymazsa HİÇBİR ŞEY yazılmaz (veri script yazıldıktan sonra değişmiş
     olabilir — elle bak).
  ② **Telafi Adjustment'ı** — `Transaction(Adjustment, −20, lot_number=None)`
     + `current_stock` 40 → 20 (`core.item_merge._adjust`).  Transaction
     SİLİNMEZ (defter kuralı, core/snapshots.py): 10.09'daki +20 ve 05.10'daki
     +20 yerinde kalır, bu kayıt farkı kapatır.  Lot satırına DOKUNULMAZ
     (draw_down yok) — lot 20 ml zaten doğru, stok ona iner.
     Not "Çift sayım düzeltmesi —" ile başlar: "Stok düzeltme" ile BAŞLAMAZ
     (çevirme guard'ı o öneki arıyor) ve "Numune stoğa çevrildi" GEÇMEZ (satın
     alma motoru o işaretle çevrilmiş lot tanıyor).
  ③ **Audit** — `inventory.double_count_fix` (`core.audit.log_admin_event`).

**İdempotent:** kartta notu "Çift sayım düzeltmesi" ile başlayan Transaction
varsa script hiçbir şey yazmaz ve "zaten uygulanmış" der (doğrulama da
atlanır — uygulandıktan sonra stok artık 40 değildir).

**Rapor (yazmaz):** hâlâ numune olan ve kartında numune geldikten sonra onu
karşılayan elle "Stok düzeltme" bulunan lotlar (746 PORTAKAL, 753 LAVANTA
beklenir) — aynı hata tekrarlanmasın diye lab bunları "Stoğa çevir"
penceresinde **"Zaten sayıldı — yalnız lotu bağla"** ile çevirmeli.  Tespit
çevirme ekranının kullandığı `_count_guard`'la aynıdır.

Kullanım (prod — ÖNCE pg_dump, sonra kuru çalıştır, çıktıyı gözden geçir,
sonra --commit):
    cd /var/www/minerva && set -a && source .env && set +a \\
      && venv/bin/python scripts/fix_numune_double_count_20261006.py            # kuru
    cd /var/www/minerva && set -a && source .env && set +a \\
      && venv/bin/python scripts/fix_numune_double_count_20261006.py --commit   # yaz

Çıkış kodu: 0 = uygulanabilir / uygulandı / zaten uygulanmış; 2 = DURDURULDU.
"""
import sys
from datetime import date
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session                                   # noqa: E402

from core.audit import log_admin_event                               # noqa: E402
from core.item_merge import _adjust                                  # noqa: E402
from core.supplier_prices import normalize                           # noqa: E402
from database import (Inventory, Item, SessionLocal, Transaction,    # noqa: E402
                      to_tr)
from routers.inventory import _count_guard                           # noqa: E402

TARGET_ITEM_ID = 758
EXPECT_NAME = "JOJOBA UÇUCU YAĞI (NUMUNE)"
EXPECT_UNIT = "ml"
EXPECT_STOCK = 40.0
EXPECT_QTY = 20.0                      # numune = elle düzeltme = çevirme = fazlalık
EXPECT_LOT = "NUMUNE"
MANUAL_DAY = date(2026, 9, 10)         # TR günü — Betül'ün elle +20'si
CONVERT_DAY = date(2026, 10, 5)        # TR günü — "Stoğa çevir" Input'u
FIX_MARK = "Çift sayım düzeltmesi"     # idempotens işareti (not öneki)
MANUAL_PREFIX = "Stok düzeltme"        # adjust_stock'un not öneki (guard bunu arar)
CONVERT_MARK = "Numune stoğa çevrildi"
ACTOR = "sistem (betik)"
AUDIT_ACTION = "inventory.double_count_fix"
EPS = 1e-6


def _tr_day(ts) -> Optional[date]:
    return to_tr(ts).date() if ts else None


def _tx_view(t: Transaction) -> dict:
    return {"id": t.id, "qty": float(t.quantity or 0.0), "lot": t.lot_number,
            "by": t.performed_by or "",
            "date": to_tr(t.timestamp).strftime("%d.%m.%Y %H:%M") if t.timestamp else ""}


def suspect_samples(db: Session) -> List[dict]:
    """Hâlâ numune olan ve kartında, numune geldikten sonra onu karşılayan elle
    "Stok düzeltme" bulunan lotlar — çevrilirlerse 758'in hatası tekrarlanır.
    Ölçüt çevirme ekranındakiyle aynı (`_count_guard`)."""
    rows = (db.query(Inventory)
            .filter(Inventory.is_sample == True, Inventory.quantity > EPS)   # noqa: E712
            .order_by(Inventory.item_id.asc(), Inventory.id.asc()).all())
    out = []
    for r in rows:
        guard = _count_guard(db, r.item_id, r.created_at, r.quantity)
        if not guard["maybe_already_counted"]:
            continue
        it = db.query(Item).filter(Item.id == r.item_id).first()
        out.append({
            "item_id": r.item_id, "name": it.name if it else "?",
            "unit": (it.unit or "") if it else "",
            "stock": float(it.current_stock or 0.0) if it else 0.0,
            "active": bool(it.is_active) if it else False,
            "domain": r.domain,
            "inventory_id": r.id, "lot": r.lot_number, "qty": float(r.quantity or 0.0),
            "supplier": r.supplier.name if r.supplier else "—",
            "received": to_tr(r.created_at).strftime("%d.%m.%Y") if r.created_at else "",
            "adjustments": guard["adjustments"],
        })
    return out


def plan(db: Session, item_id: int = TARGET_ITEM_ID, *, lock: bool = False) -> dict:
    """Ne yapılacağını hesaplar — hiçbir şey YAZMAZ.

    `status`: "ready" (uygulanabilir) · "already_applied" (işaret notu var) ·
    "mismatch" (`problems` dolu; uygulanmaz).  `others` her durumda
    `suspect_samples` raporudur."""
    p = {"item_id": item_id, "status": "mismatch", "problems": [], "item": None,
         "lots": [], "manual": None, "convert": None, "fix_tx": None,
         "delta": -EXPECT_QTY, "new_stock": None, "note": None,
         "others": suspect_samples(db)}
    q = db.query(Item).filter(Item.id == item_id)
    item = (q.with_for_update() if lock else q).first()
    if not item:
        p["problems"].append(f"kart id={item_id} bulunamadı")
        return p
    stock = float(item.current_stock or 0.0)
    p["item"] = {"id": item.id, "name": item.name, "unit": item.unit or "",
                 "stock": stock, "active": bool(item.is_active)}

    done = (db.query(Transaction)
            .filter(Transaction.item_id == item.id,
                    Transaction.notes.like(f"{FIX_MARK}%"))
            .order_by(Transaction.id.asc()).first())
    if done:
        p["status"] = "already_applied"
        p["fix_tx"] = _tx_view(done)
        return p

    probs = p["problems"]
    if normalize(item.name) != normalize(EXPECT_NAME):
        probs.append(f"ad «{item.name}» — beklenen «{EXPECT_NAME}»")
    if (item.unit or "") != EXPECT_UNIT:
        probs.append(f"birim {item.unit or '—'} — beklenen {EXPECT_UNIT}")
    if abs(stock - EXPECT_STOCK) > EPS:
        probs.append(f"stok {stock:g} — beklenen {EXPECT_STOCK:g}")

    lots = (db.query(Inventory)
            .filter(Inventory.item_id == item.id, Inventory.is_sample == False)   # noqa: E712
            .order_by(Inventory.id.asc()).all())
    p["lots"] = [{"id": r.id, "lot": r.lot_number, "qty": float(r.quantity or 0.0)}
                 for r in lots]
    lot_total = sum(x["qty"] for x in p["lots"])
    if abs(lot_total - EXPECT_QTY) > EPS:
        probs.append(f"normal lot toplamı {lot_total:g} — beklenen {EXPECT_QTY:g}")

    manual = [t for t in (db.query(Transaction)
                          .filter(Transaction.item_id == item.id,
                                  Transaction.transaction_type == "Adjustment",
                                  Transaction.notes.like(f"{MANUAL_PREFIX}%")).all())
              if abs(float(t.quantity or 0.0) - EXPECT_QTY) <= EPS
              and _tr_day(t.timestamp) == MANUAL_DAY]
    if len(manual) != 1:
        probs.append(f"{MANUAL_DAY:%d.%m.%Y} tarihli +{EXPECT_QTY:g} '{MANUAL_PREFIX}' "
                     f"Adjustment'ı {len(manual)} adet — beklenen 1")
    else:
        p["manual"] = _tx_view(manual[0])

    convert = [t for t in (db.query(Transaction)
                           .filter(Transaction.item_id == item.id,
                                   Transaction.transaction_type == "Input",
                                   Transaction.notes.like(f"{CONVERT_MARK}%")).all())
               if abs(float(t.quantity or 0.0) - EXPECT_QTY) <= EPS
               and _tr_day(t.timestamp) == CONVERT_DAY]
    if len(convert) != 1:
        probs.append(f"{CONVERT_DAY:%d.%m.%Y} tarihli +{EXPECT_QTY:g} çevirme Input'u "
                     f"{len(convert)} adet — beklenen 1")
    else:
        p["convert"] = _tx_view(convert[0])
        if (convert[0].lot_number or "") != EXPECT_LOT:
            probs.append(f"çevirme Input'unun lotu {convert[0].lot_number or '—'} — "
                         f"beklenen {EXPECT_LOT}")
    if p["manual"] and p["convert"] and manual[0].timestamp >= convert[0].timestamp:
        probs.append("elle düzeltme çevirmeden SONRA — senaryo farklı, elle bak")
    if probs:
        return p

    unit = item.unit or ""
    new_stock = round(stock - EXPECT_QTY, 6)
    m, c = p["manual"], p["convert"]
    p.update(status="ready", new_stock=new_stock, note=(
        f"{FIX_MARK} — Eski: {stock:g} {unit} → Yeni: {new_stock:g} {unit} "
        f"(Δ -{EXPECT_QTY:g}) | Sebep: aynı {EXPECT_QTY:g} {unit} numune iki kez "
        f"sayıldı — {m['date'][:10]} elle +{EXPECT_QTY:g} (tx {m['id']}, {m['by'] or '—'}) "
        f"ve {c['date'][:10]} 'Stoğa çevir' +{EXPECT_QTY:g} (tx {c['id']}, Lot: {c['lot']}); "
        f"lot satırına dokunulmadı"))
    return p


def apply(db: Session, item_id: int = TARGET_ITEM_ID) -> dict:
    """Kartı kilitleyip `plan`'ı yeniden kurar; yalnız "ready" ise telafi
    Adjustment'ını yazar ve commit eder, sonra audit düşer.  Diğer durumlarda
    hiçbir şey yazmadan plan'ı döner."""
    p = plan(db, item_id, lock=True)
    if p["status"] != "ready":
        db.rollback()                  # satır kilidini bırak
        return p
    item = db.query(Item).filter(Item.id == item_id).first()
    _adjust(db, item, p["delta"], p["note"], ACTOR)
    db.flush()
    tx = (db.query(Transaction)
          .filter(Transaction.item_id == item.id, Transaction.notes.like(f"{FIX_MARK}%"))
          .order_by(Transaction.id.desc()).first())
    p["fix_tx"] = _tx_view(tx)
    db.commit()
    p["status"] = "applied"
    # Betik kullanıcısı yok: actor=None (actor_id NULL).  {"sub": 0} users FK'sini
    # ihlal eder ve log_admin_event kaydı sessizce yutar — aktör adı details'te.
    log_admin_event(
        db, None, actor=None, action=AUDIT_ACTION,
        target_type="item", target_id=item.id, target_name=item.name,
        details={"actor": ACTOR, "script": Path(__file__).name,
                 "transaction_id": tx.id, "delta": p["delta"],
                 "old_stock": p["item"]["stock"], "new_stock": p["new_stock"],
                 "manual_adjustment_id": p["manual"]["id"],
                 "convert_input_id": p["convert"]["id"],
                 "lot": p["convert"]["lot"]})
    return p


def _print(p: dict, commit: bool) -> None:
    it = p["item"]
    title = f"{it['id']} {it['name']}" if it else str(p["item_id"])
    print(f"=== ① {title} — çift sayım ===")
    if it:
        print(f"  Kart          : stok {it['stock']:g} {it['unit']}"
              f"{'' if it['active'] else ' · PASİF'}")
    if p["status"] == "already_applied":
        f = p["fix_tx"]
        print(f"  · zaten uygulanmış — tx {f['id']} {f['date']} {f['by']} ({f['qty']:+g}); "
              f"hiçbir şey yazılmayacak")
    elif p["status"] == "mismatch":
        print("  ⛔ DURDURULDU — beklenen durum tutmuyor, hiçbir şey yazılmadı:")
        for pr in p["problems"]:
            print(f"     ✖ {pr}")
    else:
        lots = ", ".join(f"{x['lot']} {x['qty']:g} (inv {x['id']})" for x in p["lots"]) or "—"
        m, c = p["manual"], p["convert"]
        print(f"  Normal lot    : {lots}")
        print(f"  Elle düzeltme : tx {m['id']} · {m['date']} · {m['by'] or '—'} · {m['qty']:+g}")
        print(f"  Çevirme       : tx {c['id']} · {c['date']} · {c['by'] or '—'} · {c['qty']:+g} "
              f"· Lot {c['lot']}")
        print(f"  → Adjustment {p['delta']:+g} {it['unit']} (lot_number boş, lota dokunulmaz) "
              f"· stok {it['stock']:g} → {p['new_stock']:g}")
        print(f"  Not: {p['note']}")
        if p["status"] == "applied":
            print(f"  ✓ yazıldı — tx {p['fix_tx']['id']}, audit {AUDIT_ACTION}")

    others = p["others"]
    print(f"\n=== ② ELLE SAYILMIŞ OLABİLECEK NUMUNELER ({len(others)}) — yazılmaz, rapor ===")
    for o in others:
        dom = "" if o["domain"] == "cosmetics" else f" [{o['domain']}]"
        print(f"  id={o['item_id']} {o['name']}{dom} · stok {o['stock']:g} {o['unit']}"
              f"{'' if o['active'] else ' · PASİF'}")
        print(f"      numune: inv {o['inventory_id']} · lot {o['lot']} · {o['qty']:g} {o['unit']} "
              f"· {o['supplier']} · geldi {o['received']}")
        for a in o["adjustments"]:
            print(f"      elle düzeltme: tx {a['id']} · {a['date']} · {a['by'] or '—'} · {a['qty']:+g}")
    if others:
        print("  → Lab bunları Ürünler → Numune → \"Stoğa çevir\" penceresinde "
              "\"Zaten sayıldı — yalnız lotu bağla\" ile çevirsin (stok değişmez).")

    print()
    if p["status"] == "applied":
        print("✓ YAZILDI.")
    elif p["status"] == "ready" and not commit:
        print("KURU ÇALIŞTIRMA — hiçbir şey yazılmadı.  Uygulamak için: --commit")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    commit = "--commit" in argv
    db = SessionLocal()
    try:
        p = apply(db) if commit else plan(db)
        _print(p, commit)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return 2 if p["status"] == "mismatch" else 0


if __name__ == "__main__":
    raise SystemExit(main())
