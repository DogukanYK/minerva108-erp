import os
from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    Boolean, Text, DateTime, ForeignKey, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime

# ── Environment-driven DB URL ────────────────────────────────────────────────
# Local dev: SQLite (default).
# Production: set DATABASE_URL=postgresql://user:pass@host:5432/dbname
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./minerva108.db")

# Engine config differs between SQLite and PostgreSQL
if DATABASE_URL.startswith("sqlite"):
    # SQLite: tek bir dosya, threading shenanigans
    engine = create_engine(
        DATABASE_URL,
        connect_args={"check_same_thread": False},
    )
else:
    # PostgreSQL (or any networked DB): connection pooling + dead-conn detection
    engine = create_engine(
        DATABASE_URL,
        pool_pre_ping=True,   # ping before reuse → no "server closed connection" surprises
        pool_size=10,
        max_overflow=20,
    )

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(100), nullable=False)
    role = Column(String(20), default="staff")  # SuperAdmin, Manager, LabLead, LabTech, Staff
    permissions = Column(Text, nullable=True)   # JSON: granular RBAC 2.0 — overrides role defaults
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # Account lockout (R3) — N başarısız deneme sonrası geçici kilit.
    # IP-based rate limit zaten 5/15dk; kullanıcı-bazlı bu ikinci katman
    # saldırgan IP rotate etse bile hesabı koruyor.
    failed_login_attempts = Column(Integer, default=0)
    lockout_until         = Column(DateTime, nullable=True)
    # Inaktiflik süresi (dakika).  Kullanıcı bu kadar süre boyunca herhangi
    # bir mouse/keyboard hareketi yapmazsa frontend otomatik çıkış yapar.
    # NULL → global default (5 dk).  Lab'a göre özelleştirilebilir
    # (örn. üretim takımına 30 dk, ofise 5 dk).
    idle_timeout_minutes  = Column(Integer, nullable=True)


class Supplier(Base):
    __tablename__ = "suppliers"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    contact_person = Column(String(100))
    phone = Column(String(20))
    email = Column(String(100))
    address = Column(Text)
    notes = Column(Text)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    items = relationship("Item", back_populates="supplier")


class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    sku = Column(String(50), unique=True, index=True)
    category = Column(String(50))
    unit = Column(String(20), default="adet")
    current_stock = Column(Float, default=0.0)
    min_stock_level = Column(Float, default=0.0)
    cost_price = Column(Float, default=0.0)
    selling_price = Column(Float, default=0.0)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    parent_id = Column(Integer, ForeignKey("items.id"), nullable=True, index=True)   # Variations: points to parent product
    variation_name = Column(String(100), nullable=True)                              # e.g. "200ml", "500ml"
    barcode = Column(String(64), nullable=True, index=True)                          # EAN-13 / QR / Code-128 — phone scanner pre-fill
    pkg_type = Column(String(20), nullable=True)                                     # Ambalaj alt-tipi: şişe / kavanoz / pompa / kapak — sadece kategori=Ambalaj için
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    supplier = relationship("Supplier", back_populates="items")
    recipe_ingredients = relationship("RecipeIngredient", back_populates="item")


class Recipe(Base):
    __tablename__ = "recipes"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    description = Column(Text)
    output_quantity = Column(Float, default=1.0)
    output_unit = Column(String(20), default="adet")
    target_item_id = Column(Integer, ForeignKey("items.id"), nullable=True)
    waste_percentage = Column(Float, default=0.0)   # % fire oranı (üretimde brüt girdiye eklenir)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    target_item = relationship("Item", foreign_keys=[target_item_id])
    ingredients = relationship(
        "RecipeIngredient",
        back_populates="recipe",
        cascade="all, delete-orphan"
    )


class RecipeIngredient(Base):
    __tablename__ = "recipe_ingredients"

    id = Column(Integer, primary_key=True, index=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    quantity = Column(Float, nullable=False)
    unit = Column(String(20))

    recipe = relationship("Recipe", back_populates="ingredients")
    item = relationship("Item", back_populates="recipe_ingredients")


class Inventory(Base):
    __tablename__ = "inventory"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    lot_number = Column(String(100), nullable=False, index=True)
    expiry_date = Column(String(20), nullable=True)
    quantity = Column(Float, nullable=False, default=0.0)
    location = Column(String(100), nullable=True)
    status = Column(String(20), default="APPROVED")
    qc_notes = Column(Text, nullable=True)
    qc_form_data = Column(Text, nullable=True)   # JSON — full digital QC form answers
    received_by = Column(String(50), nullable=True)      # Audit: who received this lot
    qc_approved_by = Column(String(50), nullable=True)   # Audit: who approved/rejected the lot
    # Üretim çıktısı QC ekibinin gözüne düşmeli — bu flag QC sayfasının
    # QUARANTINE dışında da bu lot'u listelemesini sağlar.  True ise
    # status APPROVED bile olsa QC ekrana çıkar; QC karar verince
    # qc_required=False'a iner.
    qc_required = Column(Boolean, default=False, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    item = relationship("Item", foreign_keys=[item_id])
    supplier = relationship("Supplier", foreign_keys=[supplier_id])


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    lot_number = Column(String(100))
    transaction_type = Column(String(20), nullable=False)  # Input / Output / QC Approval / QC Rejection
    quantity = Column(Float, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text)
    performed_by = Column(String(50), nullable=True)   # Audit: who triggered this transaction

    item = relationship("Item", foreign_keys=[item_id])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# ─── Phase 3 / Task 3 — Order Fulfillment & Sales ────────────────────────────

class Quotation(Base):
    """B2B proforma / quotation. Lifecycle: DRAFT → CONFIRMED (immutable)."""
    __tablename__ = "quotations"

    id            = Column(Integer, primary_key=True, index=True)
    quote_number  = Column(String(50), unique=True, nullable=False, index=True)

    # Customer (snapshot at save time)
    customer_name    = Column(String(150), nullable=False)
    customer_contact = Column(String(150), nullable=True)
    customer_email   = Column(String(150), nullable=True)
    customer_phone   = Column(String(50),  nullable=True)
    customer_address = Column(Text,        nullable=True)
    customer_country = Column(String(100), nullable=True)
    customer_vat     = Column(String(50),  nullable=True)

    # Money
    currency        = Column(String(3),  nullable=False, default="TRY")  # USD / EUR / TRY
    exchange_rate   = Column(Float,      nullable=True)                  # TRY per 1 unit foreign at save time
    subtotal_amount = Column(Float,      default=0.0)
    tax_percentage  = Column(Float,      default=0.0)
    tax_amount      = Column(Float,      default=0.0)
    shipping_amount = Column(Float,      default=0.0)
    total_amount    = Column(Float,      nullable=False, default=0.0)

    # Meta
    notes      = Column(Text,    nullable=True)
    valid_days = Column(Integer, default=30)

    # Lifecycle audit
    status       = Column(String(20),  nullable=False, default="DRAFT", index=True)  # DRAFT / CONFIRMED
    created_at   = Column(DateTime,    default=datetime.utcnow)
    created_by   = Column(String(50),  nullable=True)
    confirmed_at = Column(DateTime,    nullable=True)
    confirmed_by = Column(String(50),  nullable=True)

    items = relationship(
        "QuotationItem",
        back_populates="quotation",
        cascade="all, delete-orphan",
    )


class QuotationItem(Base):
    """Single line of a quotation. Snapshots item_name + unit_cost_try at save time for audit/margin reporting."""
    __tablename__ = "quotation_items"

    id           = Column(Integer, primary_key=True, index=True)
    quotation_id = Column(Integer, ForeignKey("quotations.id"), nullable=False, index=True)
    item_id      = Column(Integer, ForeignKey("items.id"),       nullable=False)

    item_name_snapshot = Column(String(200), nullable=True)   # Frozen at save time — survives item rename/delete
    quantity           = Column(Float,       nullable=False)
    unit_price_foreign = Column(Float,       nullable=False)  # In quotation.currency
    unit_cost_try      = Column(Float,       nullable=True)   # Cost in TRY at quote time (for margin analytics)
    line_total         = Column(Float,       nullable=False)  # quantity × unit_price_foreign

    quotation = relationship("Quotation", back_populates="items")
    item      = relationship("Item",      foreign_keys=[item_id])


class ProductionHistory(Base):
    __tablename__ = "production_history"

    id = Column(Integer, primary_key=True, index=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=True)
    recipe_name = Column(String(150))
    target_item_id = Column(Integer, ForeignKey("items.id"), nullable=True)
    target_item_name = Column(String(150))
    produced_quantity = Column(Float, nullable=False)
    produced_at = Column(DateTime, default=datetime.utcnow)
    produced_by = Column(String(50), nullable=True)             # Audit: who started production
    lot_number = Column(String(100), nullable=True, index=True) # Genealogy: lot of finished good


class AdminAuditLog(Base):
    """
    İmmutable admin/operasyonel olay defteri.

    Transaction tablosu *stok hareketlerini* yazıyor; bu tablo ise
    onları kapsamayan ama hesap güvenliği açısından kritik olan olayları
    yazıyor: kullanıcı yarat/sil/pasifleştir, şifre sıfırla, izin değiştir,
    rol değiştir, vb.  Bir admin kötüye kullanıldığında bu defterden iz
    çıkar.

    Tasarım:
      - Append-only (silinmez, güncellenmez).
      - actor = işlemi yapan; target = etkilenen kullanıcı/varlık.
      - details JSON — değişen alanların eski/yeni değerleri (opsiyonel).
      - ip_address & user_agent — saldırı izleri için (opsiyonel; isteğin
        kaynağından doldurulur).
    """
    __tablename__ = "admin_audit_log"

    id           = Column(Integer, primary_key=True, index=True)
    timestamp    = Column(DateTime, default=datetime.utcnow, index=True)
    actor_id     = Column(Integer, ForeignKey("users.id"), nullable=True)
    actor_name   = Column(String(100), nullable=True)             # full_name veya username snapshot
    action       = Column(String(50), nullable=False, index=True) # 'user.create', 'user.password_reset', ...
    target_type  = Column(String(50), nullable=True)              # 'user', 'permission', vb.
    target_id    = Column(Integer, nullable=True)                 # etkilenen kayıt ID
    target_name  = Column(String(150), nullable=True)             # etkilenen kayıt adı snapshot
    details      = Column(Text, nullable=True)                    # JSON — değişim ayrıntıları
    ip_address   = Column(String(64), nullable=True)
    user_agent   = Column(String(255), nullable=True)


class PushSubscription(Base):
    """
    Web Push subscription record. One user can have many subscriptions
    (Mac Chrome + iPhone Safari + work desktop = 3 separate rows). The
    `endpoint` URL is globally unique to a single browser/device install
    of a single user — when a different user logs in on the same browser,
    the row is reassigned (see /api/notifications/subscribe).

    Cleanup: rows whose endpoint returns 404/410 from the push service
    are pruned automatically by core.notifications._send_push.
    """
    __tablename__ = "push_subscriptions"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    endpoint     = Column(Text,        nullable=False, unique=True, index=True)
    p256dh       = Column(String(255), nullable=False)   # client public key (b64url, ~88 chars)
    auth         = Column(String(64),  nullable=False)   # auth secret (b64url, ~24 chars)
    user_agent   = Column(String(255), nullable=True)    # for debugging "which device"
    created_at   = Column(DateTime, default=datetime.utcnow)
    last_used_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User", backref="push_subscriptions")


class UndoLog(Base):
    """
    Per-user 50-deep undoable action ring buffer.

    Bir lab kullanıcısı yanlış stok düzeltmesi yapar, Ctrl+Z'ye basar → bu
    tablodan en son undoable entry'si bulunur, payload'daki BEFORE state'e
    göre geri alınır.  Trim: per-user en yeni 50 entry tutulur.

    payload (JSONB) ↓ action_type'a göre değişir:
      stock_adjust:       { item_id, before_stock, after_stock,
                            transaction_id }   (Transaction da silinir)
      item_edit:          { item_id, before: {name, category, unit, ...} }
      inventory_receive:  { inventory_id, transaction_id, item_id,
                            received_quantity, item_stock_before, item_stock_after }

    undone_at:
      NULL        — undoable (kullanıcı henüz Ctrl+Z'lemedi)
      NOT NULL    — geri alındı (storage'da audit + ileride redo için kalır)

    SuperAdmin "delete user" yaparsa CASCADE ile bu satırlar da silinir.
    """
    __tablename__ = "undo_log"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    user_id      = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    action_type  = Column(String(40),  nullable=False)
    target_table = Column(String(40),  nullable=False)
    target_id    = Column(Integer,     nullable=True)
    # JSONB kullanılır prod'da (PG); dev SQLite'da text'e fallback olur (SQLAlchemy
    # JSON tipi her ikisini de destekler).  Import burada minimal.
    from sqlalchemy import JSON
    payload      = Column(JSON,        nullable=False)
    description  = Column(String(255), nullable=False)
    created_at   = Column(DateTime,    default=datetime.utcnow, nullable=False)
    undone_at    = Column(DateTime,    nullable=True)

    user = relationship("User", backref="undo_logs")


def init_db():
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        # ── Helper: idempotent ALTER TABLE that survives PG transaction abort ──
        # PostgreSQL aborts the transaction on any error; subsequent statements
        # in the same transaction silently fail until ROLLBACK is called.
        # SQLite has no such issue, but conn.rollback() is harmless there.
        def alter_safe(sql: str):
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                conn.rollback()   # ← critical for PG; harmless on SQLite

        # ── All schema migrations in chronological order (idempotent) ────────
        # If a column already exists, alter_safe rolls back & moves on.
        for stmt in (
            "ALTER TABLE recipes            ADD COLUMN target_item_id INTEGER",
            "ALTER TABLE inventory          ADD COLUMN qc_notes TEXT",
            "ALTER TABLE recipes            ADD COLUMN waste_percentage REAL DEFAULT 0.0",
            "ALTER TABLE inventory          ADD COLUMN qc_form_data TEXT",
            # Phase 2 / Task 9 — Full traceability columns
            "ALTER TABLE inventory          ADD COLUMN received_by VARCHAR(50)",
            "ALTER TABLE inventory          ADD COLUMN qc_approved_by VARCHAR(50)",
            "ALTER TABLE transactions       ADD COLUMN performed_by VARCHAR(50)",
            "ALTER TABLE production_history ADD COLUMN produced_by VARCHAR(50)",
            "ALTER TABLE production_history ADD COLUMN lot_number VARCHAR(100)",
            # Phase 3 / Task 1 — Product variations hierarchy
            "ALTER TABLE items              ADD COLUMN parent_id INTEGER REFERENCES items(id)",
            "ALTER TABLE items              ADD COLUMN variation_name VARCHAR(100)",
            # Phase 4 / Task 2 — Granular RBAC 2.0
            "ALTER TABLE users              ADD COLUMN permissions TEXT",
            # Phase 9 — Barcode scanning
            "ALTER TABLE items              ADD COLUMN barcode VARCHAR(64)",
            "CREATE INDEX IF NOT EXISTS ix_items_barcode ON items(barcode)",
            # Phase 10 — Ambalaj alt-tipi (şişe / kavanoz / pompa / kapak)
            "ALTER TABLE items              ADD COLUMN pkg_type VARCHAR(20)",
            # Phase 11 — Admin audit log (security hardening)
            "CREATE INDEX IF NOT EXISTS ix_admin_audit_action_ts ON admin_audit_log(action, timestamp)",
            "CREATE INDEX IF NOT EXISTS ix_admin_audit_actor ON admin_audit_log(actor_id)",
            # Phase 12 — Account lockout (R3)
            "ALTER TABLE users              ADD COLUMN failed_login_attempts INTEGER DEFAULT 0",
            "ALTER TABLE users              ADD COLUMN lockout_until TIMESTAMP",
            # Phase 13 — Üretim çıktısı QC inceleme zinciri
            "ALTER TABLE inventory          ADD COLUMN qc_required BOOLEAN DEFAULT FALSE",
            # Phase 14 — Idle timeout (kullanıcı başına özelleştirilebilir)
            "ALTER TABLE users              ADD COLUMN idle_timeout_minutes INTEGER",
        ):
            alter_safe(stmt)

    # ── Varsayılan kullanıcı seed'i — sadece dev'de çalışır ─────────────────
    # Eskiden prod dahil her başlatmada "minerva123" şifreli 4 hesap oluşurdu;
    # bu, herkesin bildiği bir şifre ile arka kapı sağlıyordu.  Artık seed
    # SEED_DEFAULT_USERS=true env'i set olduğunda çalışır (lokal .env'de
    # default açık, prod .env'inde kapalı).  İdempotent — varsa atlar.
    if os.getenv("SEED_DEFAULT_USERS", "false").lower() in ("1", "true", "yes"):
        # Şifre env'den okunabilir, yoksa "minerva123" (eski davranış, sadece dev)
        seed_pw = os.getenv("SEED_DEFAULT_PASSWORD", "minerva123")
        import bcrypt
        def _hash(pw: str) -> str:
            return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()

        db = SessionLocal()
        try:
            defaults = [
                ("dogukan", "Doğukan Yalçınkaya", "SuperAdmin"),
                ("isik",    "Işık Demir",          "Manager"),
                ("songul",  "Songül Arslan",        "LabLead"),
                ("meltem",  "Meltem Kaya",          "LabTech"),
            ]
            for uname, fname, urole in defaults:
                if not db.query(User).filter(User.username == uname).first():
                    db.add(User(
                        username=uname,
                        password_hash=_hash(seed_pw),
                        full_name=fname,
                        role=urole,
                    ))
            db.commit()
        finally:
            db.close()
