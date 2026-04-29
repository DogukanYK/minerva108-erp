from sqlalchemy import (
    create_engine, Column, Integer, String, Float,
    Boolean, Text, DateTime, ForeignKey, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime

DATABASE_URL = "sqlite:///./minerva108.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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


def init_db():
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        try:
            conn.execute(text("ALTER TABLE recipes ADD COLUMN target_item_id INTEGER"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("ALTER TABLE inventory ADD COLUMN qc_notes TEXT"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("ALTER TABLE recipes ADD COLUMN waste_percentage REAL DEFAULT 0.0"))
            conn.commit()
        except Exception:
            pass
        try:
            conn.execute(text("ALTER TABLE inventory ADD COLUMN qc_form_data TEXT"))
            conn.commit()
        except Exception:
            pass

        # ── Phase 2 / Task 9 — Full traceability columns (idempotent) ────────
        for stmt in (
            "ALTER TABLE inventory          ADD COLUMN received_by VARCHAR(50)",
            "ALTER TABLE inventory          ADD COLUMN qc_approved_by VARCHAR(50)",
            "ALTER TABLE transactions       ADD COLUMN performed_by VARCHAR(50)",
            "ALTER TABLE production_history ADD COLUMN produced_by VARCHAR(50)",
            "ALTER TABLE production_history ADD COLUMN lot_number VARCHAR(100)",
        ):
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass

        # ── Phase 3 / Task 1 — Product variations hierarchy (idempotent) ─────
        for stmt in (
            "ALTER TABLE items ADD COLUMN parent_id INTEGER REFERENCES items(id)",
            "ALTER TABLE items ADD COLUMN variation_name VARCHAR(100)",
        ):
            try:
                conn.execute(text(stmt))
                conn.commit()
            except Exception:
                pass

        # ── Phase 4 / Task 2 — Granular RBAC 2.0 (idempotent) ────────────────
        try:
            conn.execute(text("ALTER TABLE users ADD COLUMN permissions TEXT"))
            conn.commit()
        except Exception:
            pass

    # ── Varsayılan kullanıcıları oluştur (idempotent — her başlatmada güvenli) ──
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
                    password_hash=_hash("minerva123"),
                    full_name=fname,
                    role=urole,
                ))
        db.commit()
    finally:
        db.close()
