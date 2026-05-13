"""
Debug & performance router — SuperAdmin'e özel.

Bu modül lab tarafından "yavaş geliyor" şeklinde rapor edilen perf
şikayetlerini *objektif* olarak ölçer.  Bir tarafta "şu anki p95 daha kötü
mü?" sorusu, diğer tarafta "kalıcı bir regresyon mu, yoksa cold-start /
network gürültüsü mü?" sorusu var — bu endpoint ikisini de cevaplar.

Çıktı şeması: { metrics: [ { endpoint, ms, rows, status } ], db: {...}, ts }

Önemli notlar
-------------
* Bu endpoint *production*'da SuperAdmin'e açık — istek sayısı tek-haneli
  olur (manuel ölçüm), rate-limit bile koymadık.
* Ölçümler "warm" query'dir (DB connection pool zaten ısınmış).  İlk çağrı
  için ekstra "cold" run ekledik — fark anlamlı olabilir.
* Tüm endpoint'ler yerelden çağrılır (kendi DB session'umuz), HTTP roundtrip
  ölçmüyoruz; saf SQL + Python serialization süresi.

İlerideki kullanım: bir Grafana / cron task bunu pekleyip metric hub'a
yazabilir; şimdilik manuel curl yeterli.
"""
import time
from datetime import datetime
from typing import Any, Callable

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session, joinedload

from database import (
    get_db, Item, Inventory, Transaction, Recipe, RecipeIngredient, Supplier,
)
from core.auth import require_role


router = APIRouter(prefix="/api/debug", tags=["debug"])


# ── Tek bir senkron blok'un süresini ms cinsinden ölçen helper ────────────
def _timeit(fn: Callable[[], Any]) -> tuple[float, Any, str]:
    t0 = time.perf_counter()
    try:
        result = fn()
        ms = (time.perf_counter() - t0) * 1000.0
        return ms, result, "ok"
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000.0
        return ms, None, f"error:{type(e).__name__}:{str(e)[:120]}"


@router.get("/perf")
def perf_snapshot(
    db: Session = Depends(get_db),
    _: dict = Depends(require_role(["SuperAdmin"])),
):
    """
    Hot endpoint'lerin ne kadar sürdüğünü ölç + DB istatistikleri.

    Manuel kullanım:
        curl -b cookies.txt https://srv.minervaims.com/api/debug/perf | jq
    """
    metrics: list[dict] = []

    # ── 1) /api/items — list_items eşdeğeri (B3 cache'in arkasındaki call)
    def q_items():
        rows = db.query(Item).order_by(Item.name).all()
        # Serialize'a dokunmadan en azından row count'u dön
        return len(rows)

    # ── 2) /api/inventory — joinedload (B2'de fix edildi)
    def q_inventory():
        rows = (
            db.query(Inventory)
            .options(joinedload(Inventory.item), joinedload(Inventory.supplier))
            .order_by(Inventory.id.desc())
            .all()
        )
        return len(rows)

    # ── 3) /api/transactions — son 500, joinedload (B2)
    def q_transactions():
        rows = (
            db.query(Transaction)
            .options(joinedload(Transaction.item))
            .order_by(Transaction.id.desc())
            .limit(500)
            .all()
        )
        return len(rows)

    # ── 4) /api/recipes — basit list
    def q_recipes():
        return db.query(Recipe).count()

    # ── 5) /api/suppliers
    def q_suppliers():
        return db.query(Supplier).filter(Supplier.is_active == True).count()

    # ── 6) Karmaşık: recipe ingredient + item join (B1 index test'i)
    def q_recipe_ingredients_join():
        return (
            db.query(RecipeIngredient)
            .join(Item, Item.id == RecipeIngredient.item_id)
            .count()
        )

    cases = [
        ("items_list",                  q_items),
        ("inventory_list_joinedload",   q_inventory),
        ("transactions_top500_joined",  q_transactions),
        ("recipes_count",               q_recipes),
        ("suppliers_active_count",      q_suppliers),
        ("recipe_ingredients_join",     q_recipe_ingredients_join),
    ]
    # Her query'i iki kez çalıştır — cold + warm.  PG cache devreye girdiğinde
    # ikinci tipik olarak çok daha hızlı.
    for name, fn in cases:
        cold_ms, cold_rows, cold_status = _timeit(fn)
        warm_ms, warm_rows, warm_status = _timeit(fn)
        metrics.append({
            "endpoint": name,
            "cold_ms":  round(cold_ms, 2),
            "warm_ms":  round(warm_ms, 2),
            "rows":     cold_rows,
            "status":   cold_status if cold_status != "ok" else warm_status,
        })

    # ── DB-side stats: tablo sayıları, eksik index uyarısı
    db_stats: dict[str, Any] = {}
    try:
        # Tablo row count'ları (B1 index'lerin korumaya çalıştığı tablolar)
        for tbl in ("items", "inventory", "transactions", "recipes",
                    "recipe_ingredients", "suppliers", "quotations"):
            r = db.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar()
            db_stats[f"{tbl}_count"] = r
    except Exception as e:
        db_stats["row_counts_error"] = str(e)[:200]

    try:
        # En çok sequential scan yiyen tablolar (PG analyze pov)
        rows = db.execute(text(
            "SELECT schemaname, relname, seq_scan, idx_scan, seq_tup_read, n_live_tup "
            "FROM pg_stat_user_tables "
            "WHERE schemaname = current_schema() "
            "ORDER BY seq_tup_read DESC LIMIT 8"
        )).fetchall()
        db_stats["top_seq_scan_tables"] = [
            {
                "table":         r[1],
                "seq_scan":      r[2],
                "idx_scan":      r[3],
                "seq_tup_read":  r[4],
                "live_rows":     r[5],
            } for r in rows
        ]
    except Exception as e:
        db_stats["seq_scan_error"] = str(e)[:200]

    return {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "metrics":      metrics,
        "db":           db_stats,
        "hint": (
            "warm_ms >> cold_ms → cache miss / IO; warm_ms < 5ms → sağlıklı. "
            "Bir endpoint > 100ms ise EXPLAIN ANALYZE ile bak."
        ),
    }
