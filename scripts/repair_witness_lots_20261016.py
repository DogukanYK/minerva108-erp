"""Şahit lot onarımı — 16.10.2026 (idempotent, kuru çalıştırma varsayılan).

**Ne oldu:** Elle stok düzeltmesi (`adjust_stock`) eksi farkı lot seçmeden
`stock_lots.draw_down` ile FIFO düşüyordu ve FIFO havuzu şahit numune lotlarını
(`-S`, aktif RetentionSample'a bağlı) dışlamıyordu.  Ölçüldü: 49 lot düşümlü
düzeltmenin 29'u şahit lotlarından da düştü (ör. Serenida Niacinamide #32 —
08.10 "66 → 15 yanlış sayım" SR002-S×2 düştü).  Dolap kaydı
(`RetentionSample.quantity`) değişmedi, yalnız `Inventory` lot satırı azaldı;
düşüm Transaction da YAZMAMIŞTI (draw_down yalnız lot satırına dokunur).
Kod tarafı ayrıca düzeltildi: draw_down / consume artık şahit lotuna dokunmaz.

**Ne yapar:** Kayıtta duran (`status='stored'`), aktif bir şahit kaydının bağlı
APPROVED lotu kayıttaki miktarın ALTINDAYSA lot satırı kayıttaki miktara geri
yazılır.  Yalnız `Inventory.quantity` değişir — **kart stoğuna (current_stock)
DOKUNULMAZ, Transaction yazılmaz** (düşüm de yazmamıştı; defter ve aylık
rekonstrüksiyon değişmez).  Sonuç: kart aynı, şahit satırı büyür, "lot kaydı
olmayan" satırı aynı miktarda küçülür.  Lot başına bir `AdminAuditLog`
(`inventory.witness_lot_repair`) aynı işlemde yazılır.

**Çapraz kontrol (yazmaz):** notunda "Lot düşümü: …-S×" geçen elle düzeltmeler
listelenir ve onarım planıyla eşleştirilir (canlıda 29 beklenir).

**Sonrası (kod dışı):** etkilenen ürünlerin listesi lab'a verilir; lab yeni
raf sayım formuyla bir kez sayar → kart = raf + şahit.

**İdempotent:** onarılan lot kayıttaki miktara eşittir; ikinci çalıştırma
hiçbir şey bulmaz.

Kullanım (prod — ÖNCE pg_dump, sonra kuru çalıştır, çıktıyı gözden geçir,
sonra onayla --commit):
    cd /var/www/minerva && set -a && source .env && set +a \\
      && venv/bin/python scripts/repair_witness_lots_20261016.py            # kuru
    cd /var/www/minerva && set -a && source .env && set +a \\
      && venv/bin/python scripts/repair_witness_lots_20261016.py --commit   # yaz

Çıkış kodu: 0.
"""
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy.orm import Session                                   # noqa: E402

from core.stock_lots import stock_breakdown                          # noqa: E402
from database import (AdminAuditLog, Inventory, Item, RetentionSample,  # noqa: E402
                      SessionLocal, Transaction, to_tr)

ACTOR = "sistem (betik)"
AUDIT_ACTION = "inventory.witness_lot_repair"
MANUAL_PREFIX = "Stok düzeltme"        # adjust_stock'un not öneki
DRAW_MARK = "Lot düşümü:"
EPS = 1e-6

_DRAW_RE = re.compile(r"Lot düşümü:\s*([^|]+)")
_PART_RE = re.compile(r"^\s*(.+?)×([0-9.]+)\s*$")


def _fmt_dt(ts) -> str:
    return to_tr(ts).strftime("%d.%m.%Y %H:%M") if ts else "—"


def witness_draws(db: Session) -> List[dict]:
    """Notunda "Lot düşümü: …-S×" geçen elle düzeltmeler — lot başına satır."""
    txs = (db.query(Transaction)
           .filter(Transaction.transaction_type == "Adjustment",
                   Transaction.notes.like(f"{MANUAL_PREFIX}%"),
                   Transaction.notes.like(f"%{DRAW_MARK}%"))
           .order_by(Transaction.id.asc()).all())
    out = []
    for t in txs:
        m = _DRAW_RE.search(t.notes or "")
        if not m:
            continue
        parts = []
        for chunk in m.group(1).split("·"):
            pm = _PART_RE.match(chunk)
            if pm and pm.group(1).strip().endswith("-S"):
                parts.append((pm.group(1).strip(), float(pm.group(2))))
        for lot, qty in parts:
            out.append({"tx_id": t.id, "item_id": t.item_id, "date": _fmt_dt(t.timestamp),
                        "by": t.performed_by or "", "lot": lot, "qty": qty})
    return out


def _candidates(db: Session) -> List[Tuple[RetentionSample, Inventory]]:
    """Kayıtta duran aktif şahit kaydı + bağlı APPROVED lotu, lot < kayıt."""
    q = (db.query(RetentionSample, Inventory)
         .join(Inventory, Inventory.id == RetentionSample.inventory_id)
         .filter(RetentionSample.is_active == True,                  # noqa: E712
                 RetentionSample.status == "stored",
                 Inventory.status == "APPROVED",
                 Inventory.is_sample == False)                       # noqa: E712
         .order_by(Inventory.item_id.asc(), Inventory.id.asc()))
    rows = q.all()
    return [(rs, inv) for rs, inv in rows
            if float(inv.quantity or 0.0) + EPS < float(rs.quantity or 0.0)]


def plan(db: Session) -> dict:
    """Ne yapılacağını hesaplar — hiçbir şey YAZMAZ."""
    by_item: Dict[int, List[dict]] = defaultdict(list)
    for rs, inv in _candidates(db):
        by_item[inv.item_id].append({
            "inventory_id": inv.id, "lot": inv.lot_number, "retention_id": rs.id,
            "brand": rs.brand, "before": round(float(inv.quantity or 0.0), 6),
            "after": round(float(rs.quantity or 0.0), 6)})
    items = []
    for item_id in sorted(by_item):
        it = db.query(Item).filter(Item.id == item_id).first()
        if it is None:
            continue
        bd = stock_breakdown(db, it)
        add = round(sum(x["after"] - x["before"] for x in by_item[item_id]), 6)
        diff_after = round(bd["card"] - (bd["lot_total"] + add), 6)
        items.append({
            "item_id": it.id, "name": it.name, "unit": it.unit or "", "domain": it.domain,
            "card": bd["card"], "shelf": bd["shelf"], "qc": bd["qc_pending"],
            "witness": bd["witness"], "lotless": bd["lotless"], "excess": bd["lot_excess"],
            "witness_after": round(bd["witness"] + add, 6),
            "lotless_after": diff_after if diff_after > EPS else 0.0,
            "excess_after": -diff_after if diff_after < -EPS else 0.0,
            "lots": by_item[item_id], "add": add})
    planned = {(i["item_id"], x["lot"]): x for i in items for x in i["lots"]}
    draws = witness_draws(db)
    names = dict(db.query(Item.id, Item.name)
                 .filter(Item.id.in_({d["item_id"] for d in draws} or {0})).all())
    drawn: Dict[tuple, float] = defaultdict(float)
    for d in draws:
        drawn[(d["item_id"], d["lot"])] += d["qty"]
    for d in draws:
        d["name"] = names.get(d["item_id"], "?")
        x = planned.get((d["item_id"], d["lot"]))
        d["planned"] = round(x["after"] - x["before"], 6) if x else 0.0
        d["drawn_total"] = round(drawn[(d["item_id"], d["lot"])], 6)
    return {"status": "ready" if items else "nothing", "items": items, "draws": draws,
            "lot_count": sum(len(i["lots"]) for i in items),
            "total_add": round(sum(i["add"] for i in items), 6)}


def apply(db: Session) -> dict:
    """Kilitleyip yeniden doğrular (kart → lot → şahit kaydı), lotları kayıttaki
    miktara yazar, lot başına audit ekler, TEK commit."""
    p = plan(db)
    if p["status"] != "ready":
        return p
    written = 0
    for i in p["items"]:
        db.query(Item).filter(Item.id == i["item_id"]).with_for_update().first()
        for x in i["lots"]:
            inv = (db.query(Inventory).filter(Inventory.id == x["inventory_id"])
                   .with_for_update().populate_existing().first())
            rs = (db.query(RetentionSample).filter(RetentionSample.id == x["retention_id"])
                  .with_for_update().populate_existing().first())
            if (inv is None or rs is None or not rs.is_active or rs.status != "stored"
                    or rs.inventory_id != inv.id or inv.status != "APPROVED"
                    or float(inv.quantity or 0.0) + EPS >= float(rs.quantity or 0.0)):
                x["skipped"] = True                 # arada değişti — dokunma
                continue
            before = round(float(inv.quantity or 0.0), 6)
            after = round(float(rs.quantity or 0.0), 6)
            inv.quantity = after
            x.update(before=before, after=after, applied=True)
            db.add(AdminAuditLog(
                actor_id=None, actor_name=ACTOR, action=AUDIT_ACTION,
                target_type="inventory", target_id=inv.id, target_name=(inv.lot_number or "")[:150],
                details=json.dumps({"script": Path(__file__).name, "item_id": i["item_id"],
                                    "item_name": i["name"], "retention_id": rs.id,
                                    "lot_before": before, "lot_after": after,
                                    "card_stock": i["card"], "card_stock_changed": False},
                                   ensure_ascii=False),
                ip_address="—", user_agent=""))
            written += 1
    db.commit()
    p["status"] = "applied"
    p["written"] = written
    return p


def _print(p: dict, commit: bool) -> None:
    mode = "YAZILDI" if p["status"] == "applied" else "KURU ÇALIŞTIRMA"
    print(f"=== Şahit lot onarımı — {mode} ===")
    print("Kart stoğuna dokunulmaz; yalnız şahit lot satırı dolap kaydındaki miktara döner.\n")
    if not p["items"]:
        print("  · onarılacak şahit lotu yok (zaten uygulanmış ya da hiç düşülmemiş).")
    else:
        hdr = (f"{'Kart':<48} {'Stok':>7} {'Raf':>7} {'QC':>6} {'Şahit':>7} {'Lotsuz':>7}"
               f"  →  {'Şahit':>7} {'Lotsuz':>7} {'Fazla':>6}")
        print(hdr)
        print("-" * len(hdr))
        for i in p["items"]:
            dom = "" if i["domain"] == "cosmetics" else f" [{i['domain']}]"
            name = f"#{i['item_id']} {i['name']}{dom}"[:48]
            print(f"{name:<48} {i['card']:>7g} {i['shelf']:>7g} {i['qc']:>6g} {i['witness']:>7g} "
                  f"{i['lotless']:>7g}  →  {i['witness_after']:>7g} {i['lotless_after']:>7g} "
                  f"{i['excess_after']:>6g}")
            for x in i["lots"]:
                tag = " (atlandı — arada değişti)" if x.get("skipped") else ""
                print(f"      {x['lot']}: lot {x['before']:g} → {x['after']:g} {i['unit']} "
                      f"(şahit kaydı #{x['retention_id']}, {x['brand']}){tag}")
        print(f"\n  {p['lot_count']} lot · {len(p['items'])} ürün · toplam +{p['total_add']:g} "
              f"geri yazılacak.")

    draws = p["draws"]
    print(f"\n=== Çapraz kontrol: \"Lot düşümü: …-S×\" notlu düzeltmeler ({len({d['tx_id'] for d in draws})} "
          f"düzeltme, {len(draws)} şahit lot satırı) — yazılmaz ===")
    for d in draws:
        ok = "✓" if abs(d["planned"] - d["drawn_total"]) <= EPS else (
            "≈ onarım farklı" if d["planned"] > EPS else "— onarım yok")
        same = (f" (bu lottan toplam {d['drawn_total']:g})"
                if abs(d["drawn_total"] - d["qty"]) > EPS else "")
        print(f"  tx {d['tx_id']} · {d['date']} · {d['by'] or '—'} · #{d['item_id']} {d['name']} · "
              f"{d['lot']}×{d['qty']:g}{same} → onarımda +{d['planned']:g} {ok}")
    if draws:
        print("  (\"onarım farklı/yok\": şahit sonradan çıkış/imha görmüş ya da kayıt pasif — "
              "dolap kaydı esas alınır.)")

    print()
    if p["status"] == "applied":
        print(f"✓ YAZILDI — {p.get('written', 0)} lot, audit {AUDIT_ACTION}.")
        print("Sıradaki adım: listedeki ürünleri lab yeni raf sayım formuyla bir kez saysın.")
    elif p["status"] == "ready" and not commit:
        print("KURU ÇALIŞTIRMA — hiçbir şey yazılmadı.  Uygulamak için (önce pg_dump): --commit")


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    commit = "--commit" in argv
    db = SessionLocal()
    try:
        p = apply(db) if commit else plan(db)
        if not commit:
            db.rollback()
        _print(p, commit)
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
