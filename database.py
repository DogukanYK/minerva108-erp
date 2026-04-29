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
    role = Column(String(20), default="staff")  # admin, manager, staff
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
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    item = relationship("Item", foreign_keys=[item_id])
    supplier = relationship("Supplier", foreign_keys=[supplier_id])


class Transaction(Base):
    __tablename__ = "transactions"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    lot_number = Column(String(100))
    transaction_type = Column(String(20), nullable=False)  # Input / Output
    quantity = Column(Float, nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow)
    notes = Column(Text)

    item = relationship("Item", foreign_keys=[item_id])


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


class ProductionHistory(Base):
    __tablename__ = "production_history"

    id = Column(Integer, primary_key=True, index=True)
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=True)
    recipe_name = Column(String(150))
    target_item_id = Column(Integer, ForeignKey("items.id"), nullable=True)
    target_item_name = Column(String(150))
    produced_quantity = Column(Float, nullable=False)
    produced_at = Column(DateTime, default=datetime.utcnow)


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
