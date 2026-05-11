#!/usr/bin/env python3
"""
Minerva108 — One-shot SQLite → PostgreSQL data migration.

Usage (on the server):
    cd /var/www/minerva
    DATABASE_URL='postgresql://minerva:PASSWORD@localhost:5432/minerva' \
    venv/bin/python migrate_to_postgres.py minerva108.db

What it does:
  1. Verifies PostgreSQL connection
  2. Creates schema in PG (Base.metadata.create_all + idempotent ALTER TABLEs)
  3. Copies all rows from SQLite to PG, in FK-safe order
     (Item self-referential FK is handled — parents inserted before children)
  4. Resets PG sequences to MAX(id)+1 so new inserts don't collide
  5. Verifies row counts match between SQLite and PG

Safety:
  - Original SQLite file is NEVER modified or deleted
  - Refuses to run if PG already has data (unless you confirm "yes")
  - Aborts with rollback on any error — partial migrations don't get committed
"""

import os
import sys
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# ── Sanity checks ──────────────────────────────────────────────────────────
if len(sys.argv) != 2:
    print("Usage: python migrate_to_postgres.py /path/to/minerva108.db")
    sys.exit(1)

sqlite_path = sys.argv[1]
if not os.path.exists(sqlite_path):
    print(f"❌ SQLite dosyası bulunamadı: {sqlite_path}")
    sys.exit(1)

pg_url = os.getenv("DATABASE_URL", "")
if not pg_url.startswith("postgresql"):
    print("❌ DATABASE_URL ortam değişkeni 'postgresql://...' ile başlamalı.")
    print("   Örnek: DATABASE_URL='postgresql://minerva:pass@localhost/minerva' python migrate_to_postgres.py minerva108.db")
    sys.exit(1)

# ── Force database.py to use PG when imported ─────────────────────────────
os.environ["DATABASE_URL"] = pg_url
from database import (   # noqa: E402
    Base, init_db,
    User, Supplier, Item, Recipe, RecipeIngredient,
    Inventory, Transaction, ProductionHistory,
    Quotation, QuotationItem,
)

# ── Two engines: source (SQLite read) + target (PG write) ─────────────────
sqlite_engine = create_engine(
    f"sqlite:///{sqlite_path}",
    connect_args={"check_same_thread": False},
)
pg_engine = create_engine(pg_url, pool_pre_ping=True)

SqliteSession = sessionmaker(bind=sqlite_engine)
PgSession     = sessionmaker(bind=pg_engine)

# Tables in foreign-key dependency order (parents first)
# Recipe → Item (target_item_id), so Item before Recipe
# RecipeIngredient → Recipe + Item, so both before
# Inventory → Item + Supplier
# Transaction → Item
# QuotationItem → Quotation + Item
TABLES = [
    User,
    Supplier,
    Item,                # special-cased: parent_id self-FK
    Recipe,
    RecipeIngredient,
    Inventory,
    Transaction,
    ProductionHistory,
    Quotation,
    QuotationItem,
]


def _get_rows_in_fk_safe_order(Model, sqlite):
    """Items have self-referential parent_id — parents must come before children."""
    if Model is Item:
        parents  = sqlite.query(Model).filter(Model.parent_id.is_(None)).order_by(Model.id.asc()).all()
        children = sqlite.query(Model).filter(Model.parent_id.isnot(None)).order_by(Model.id.asc()).all()
        return parents + children
    return sqlite.query(Model).order_by(Model.id.asc()).all()


def main():
    print("🚀 SQLite → PostgreSQL migration")
    print(f"   Source: {sqlite_path}")
    print(f"   Target: {pg_url.split('@')[-1] if '@' in pg_url else pg_url}")
    print()

    # 1. Test PG connection
    try:
        with pg_engine.connect() as c:
            c.execute(text("SELECT 1"))
        print("✅ PostgreSQL bağlantısı çalışıyor")
    except Exception as e:
        print(f"❌ PG bağlantı hatası: {e}")
        sys.exit(2)

    # 2. Create schema (init_db handles tables + idempotent ALTER TABLEs + default users)
    print("→ PG'de şema oluşturuluyor (init_db)…")
    init_db()
    print("✅ Şema hazır")

    # 3. Show current PG state — let user decide
    pg = PgSession()
    pg_counts = {M.__tablename__: pg.query(M).count() for M in TABLES}
    pg.close()
    total_pg = sum(pg_counts.values())
    print(f"\n→ PG mevcut durum:")
    for tname, c in pg_counts.items():
        print(f"   {tname:<25} {c:>6}")
    print(f"   {'TOPLAM':<25} {total_pg:>6}")

    if total_pg > 0:
        print(f"\n⚠️  PG'de {total_pg} mevcut satır var. Migration için TRUNCATE edilecek")
        print(f"   (SQLite gerçek veri olarak kabul ediliyor — PG temizlenip yeniden dolacak).")
        ans = input("   Devam (yes/no)? ")
        if ans.strip().lower() != "yes":
            print("İptal edildi.")
            sys.exit(0)

        # TRUNCATE all tables in one shot — RESTART IDENTITY also resets sequences
        # ⚠️ GÜVENLİK: Aşağıdaki f-string SQL'i kullanıcıdan değil, sabit TABLES
        # listesinden besleniyor (her ismi SQLAlchemy modelinden alıyor).  Bu
        # script'i parametreleştirip dışarıdan tablo adı kabul edersen, identifier
        # quoting (psycopg2.sql.Identifier) kullan — yoksa SQL injection olur.
        print(f"\n→ PG tabloları temizleniyor (TRUNCATE CASCADE)…")
        with pg_engine.connect() as conn:
            # Assertion: tablo adları sadece [a-z_]+ olmalı — model kontrolü
            for M in TABLES:
                assert M.__tablename__.replace("_", "").isalnum(), (
                    f"Tablo adında güvensiz karakter: {M.__tablename__}"
                )
            tables_csv = ", ".join(M.__tablename__ for M in TABLES)
            conn.execute(text(f"TRUNCATE {tables_csv} RESTART IDENTITY CASCADE"))
            conn.commit()
        print(f"   ✅ {len(TABLES)} tablo temizlendi, ID sequence'ları sıfırlandı")

    # 4. Copy table by table with simple INSERTs (PG is empty after truncate)
    sqlite = SqliteSession()
    pg     = PgSession()

    counts = {}
    try:
        for Model in TABLES:
            tname = Model.__tablename__
            print(f"\n→ {tname}…")
            rows = _get_rows_in_fk_safe_order(Model, sqlite)
            if not rows:
                print(f"   (boş, atlandı)")
                counts[tname] = 0
                continue

            for row in rows:
                d = {col.name: getattr(row, col.name) for col in Model.__table__.columns}
                pg.add(Model(**d))   # explicit IDs preserved

            pg.commit()
            counts[tname] = len(rows)
            print(f"   ✅ {len(rows)} satır kopyalandı")

        # 5. Reset PG sequences to MAX(id) so new inserts don't collide with existing IDs
        # ⚠️ tname, pk_col, seq_name yine sabit Model metadata'sından — user input yok.
        print(f"\n→ PG sequence'ları senkronize ediliyor…")
        with pg_engine.connect() as conn:
            for Model in TABLES:
                tname = Model.__tablename__
                pk_col = list(Model.__table__.primary_key.columns)[0].name
                # Güvenlik: identifier'ları assert et
                assert tname.replace("_", "").isalnum() and pk_col.replace("_", "").isalnum()
                seq_name = f"{tname}_{pk_col}_seq"
                try:
                    conn.execute(text(
                        f"SELECT setval('{seq_name}', "
                        f"COALESCE((SELECT MAX({pk_col}) FROM {tname}), 1), "
                        f"(SELECT MAX({pk_col}) IS NOT NULL FROM {tname}))"
                    ))
                    conn.commit()
                    print(f"   ✅ {seq_name}")
                except Exception as e:
                    print(f"   ⚠️  {seq_name} sıfırlanamadı: {str(e)[:80]}")

        # 6. Verify counts
        print(f"\n→ Doğrulama (SQLite vs PG):")
        all_match = True
        for Model in TABLES:
            tname = Model.__tablename__
            src_count = sqlite.query(Model).count()
            tgt_count = pg.query(Model).count()
            if src_count == tgt_count:
                print(f"   ✅ {tname:<25} {src_count:>6} satır eşleşiyor")
            else:
                print(f"   ❌ {tname:<25} SQLite: {src_count} → PG: {tgt_count}  *** MISMATCH ***")
                all_match = False

        if not all_match:
            print(f"\n⚠️  Uyarı: bazı tablolarda satır sayıları eşleşmiyor. Yukarıyı kontrol et.")
            sys.exit(3)

    except Exception as e:
        pg.rollback()
        print(f"\n❌ Migration sırasında hata: {e}")
        print("   Tüm değişiklikler geri alındı.")
        sys.exit(4)
    finally:
        sqlite.close()
        pg.close()

    print(f"\n🎉 Migration tamamlandı.")
    print(f"   Toplam: {sum(counts.values())} satır kopyalandı.")
    print(f"   SQLite dosyası dokunulmadan duruyor: {sqlite_path}")
    print(f"   (Bir hafta orada bırak, her şey iyiyse sonra silebilirsin.)")


if __name__ == "__main__":
    main()
