# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
import os
from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Float,
    Boolean, Text, Date, DateTime, ForeignKey, UniqueConstraint, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime, timedelta


# ─── Saat dilimi — Türkiye (UTC+3) ───────────────────────────────────────────
# DB'de TÜM datetime'lar UTC saklanır (model default'ları datetime.utcnow).
# Sunucu da UTC.  Bu helper'lar SADECE kullanıcıya gösterim için UTC → Türkiye
# çevirir.  Türkiye 2016'dan beri yaz saati uygulamıyor — sabit +3 ofset,
# DST hesabı gerekmez.
TR_OFFSET = timedelta(hours=3)


def to_tr(dt):
    """UTC datetime'ı Türkiye saatine çevir — yalnızca kullanıcı gösterimi için.
    None güvenli; veritabanı değerleri her zaman UTC kalır."""
    return (dt + TR_OFFSET) if dt is not None else None


def tr_now() -> datetime:
    """Şu anki Türkiye saati (gösterim amaçlı)."""
    return datetime.utcnow() + TR_OFFSET

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


class AppSetting(Base):
    """Genel anahtar-değer ayar tablosu (SuperAdmin tarafından düzenlenir).
    Örn. özelleştirilebilir rol etiketleri: key='role_label.SuperAdmin',
    value='Patron'.  Tek satırlık ayarlar burada toplanır."""
    __tablename__ = "app_setting"

    key        = Column(String(80), primary_key=True)
    value      = Column(Text, nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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
    domain = Column(String(20), default="cosmetics", nullable=False, index=True)  # Faz 3 — Kozmetik / Food Supplement

    items = relationship("Item", back_populates="supplier")


class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    name_tr = Column(String(150), nullable=True)   # Türkçe ad (name = İngilizce/birincil); belge dili + çift-dilli arama
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
    # Etiket dil ayrımı: 'TR' / 'EN' / NULL (dilsiz).  label_group aynı mantıksal
    # etiketin TR+EN üyelerini bağlar — üretimde dil seçilince kardeşe inilir.
    language = Column(String(8), nullable=True)
    label_group = Column(String(255), nullable=True, index=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    domain = Column(String(20), default="cosmetics", nullable=False, index=True)  # Faz 3 — Kozmetik / Food Supplement

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
    production_notes = Column(Text, nullable=True)  # Üretim föyü "YAPILIŞI" metni
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    domain = Column(String(20), default="cosmetics", nullable=False, index=True)  # Faz 3 — Kozmetik / Food Supplement

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
    phase = Column(String(8), nullable=True)   # Üretim föyü FAZ sütunu (A/B/D/E)

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
    # Numune lotu mu?  Var olan bir hammaddenin ALTERNATİF bir tedarikçiden
    # numune olarak gelen partisi True ile işaretlenir (kendi supplier_id'siyle).
    # Stoğa girer ve üretimde kullanılabilir; stok sayfası ayrı rozetle gösterir,
    # üretimde hangi tedarikçinin/lotun tüketileceği seçilebilir.
    is_sample = Column(Boolean, default=False, nullable=False)
    domain = Column(String(20), default="cosmetics", nullable=False, index=True)  # Faz 3 — item.domain ile aynı
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow)

    item = relationship("Item", foreign_keys=[item_id])
    supplier = relationship("Supplier", foreign_keys=[supplier_id])


class SupplierPrice(Base):
    """Malzeme × tedarikçi fiyat / paket listesi — satın alma raporunu otomatik doldurur.

    Sistemde malzeme başına yalnızca tek `Item.cost_price` ve tek varsayılan
    tedarikçi var. Satın alma kararı için TEDARİKÇİ-bazlı **birim fiyat** ve
    **alınabilecek paket / min-sipariş miktarı** burada tutulur — Işık Hanım'ın
    "Stok Son Durum" tablosunun veri kaynağı. Bir malzemenin birden çok satırı
    (tedarikçisi) olabilir; rapor en ucuzdan başlayarak ilk N tanesini gösterir.
    `supplier_name` her zaman saklanır (görüntü için); `supplier_id` eşleşirse
    bağlanır, eşleşmezse NULL kalır (serbest metin tedarikçi).
    """
    __tablename__ = "supplier_prices"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    supplier_name = Column(String(150), nullable=True)   # görüntü adı (eşleşmese de saklanır)
    package_size = Column(Float, nullable=True)          # alınabilecek miktar / min sipariş (malzeme birimi cinsinden)
    unit_price = Column(Float, nullable=True)            # birim fiyat
    currency = Column(String(8), default="TRY")
    note = Column(Text, nullable=True)
    domain = Column(String(20), default="cosmetics", nullable=False, index=True)  # Kozmetik / Food Supplement
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    item = relationship("Item", foreign_keys=[item_id])
    supplier = relationship("Supplier", foreign_keys=[supplier_id])


class Delivery(Base):
    """Hediye / numune teslimatı — üretim/satış dışı stok çıkışı.

    Ofise gelene hediye, numune gönderimi gibi durumlarda ürünün barkodu
    okutularak (market kasası mantığı) stok düşülür ve imzalı bir teslim
    belgesi üretilir. Her teslimat bir veya çok kalemden oluşur; her kalem
    `Item.current_stock`'tan düşülür ve immutable bir Transaction(Output) yazılır.
    """
    __tablename__ = "deliveries"

    id = Column(Integer, primary_key=True, index=True)
    document_no = Column(String(40), unique=True, index=True)   # TES-2026-00042
    recipient_name = Column(String(150), nullable=False)        # alıcı ad-soyad (sistemden girilir)
    recipient_org = Column(String(150), nullable=True)          # firma / kurum (opsiyonel)
    recipient_phone = Column(String(40), nullable=True)         # kargo için (opsiyonel)
    delivery_type = Column(String(20), default="hediye")        # hediye / numune / diğer / proforma
    method = Column(String(20), default="elden")                # elden / kargo
    note = Column(Text, nullable=True)
    dispatched_by = Column(String(80), nullable=True)           # teslim eden / hazırlayan (audit)
    doc_lang = Column(String(8), default="TR")                  # belge dili: TR / EN (ürün adı dili)
    domain = Column(String(20), default="cosmetics", nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    # ── Proforma fatura alanları (yalnız delivery_type='proforma') ──
    customer_address = Column(Text, nullable=True)
    customer_country = Column(String(100), nullable=True)
    currency = Column(String(3), default="USD")                 # USD / EUR / TRY
    status = Column(String(20), default="completed", index=True)  # completed | pending | approved | rejected | preparing | shipped | canceled
    approved_by = Column(String(80), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    reject_reason = Column(Text, nullable=True)
    # ── Kargo / Sevkiyat alanları (yalnız delivery_type='kargo') ──
    # Kargo: create'te stok DÜŞMEZ (status='preparing'); takip no atanınca (ship)
    # stok o an düşer (status='shipped'). reject_reason iptal gerekçesi olarak da kullanılır.
    tracking_no = Column(String(100), nullable=True)            # PRIMARY (yerel) takip no — ship'i tetikler
    carrier = Column(String(80), nullable=True)                # primary taşıyıcı (Yurtiçi/Aras/MNG...)
    shipped_at = Column(DateTime, nullable=True)               # kargolandığı (stok düştüğü) UTC damga
    shipped_by = Column(String(80), nullable=True)             # takip no'yu girip kargolayan (audit)
    # Çok-bacaklı takip (yurtdışı: yerel TR → global → varış yereli). JSON liste:
    # [{"label": "Yerel (TR)", "carrier": "Yurtiçi", "tracking_no": "..."}]
    tracking_legs = Column(Text, nullable=True)

    items = relationship("DeliveryItem", back_populates="delivery",
                         cascade="all, delete-orphan")


class DeliveryItem(Base):
    __tablename__ = "delivery_items"

    id = Column(Integer, primary_key=True, index=True)
    delivery_id = Column(Integer, ForeignKey("deliveries.id"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=True)   # ürün silinse de belge okunur
    item_name = Column(String(150), nullable=False)   # snapshot (ürün adı sonradan değişse de belge sabit)
    quantity = Column(Float, nullable=False)
    unit = Column(String(20), nullable=True)
    unit_price = Column(Float, nullable=True)         # proforma — birim fiyat (Delivery.currency)
    weight_ml = Column(Float, nullable=True)          # proforma — ağırlık/hacim (ml)

    delivery = relationship("Delivery", back_populates="items")
    item = relationship("Item", foreign_keys=[item_id])


class ProductReturn(Base):
    """Ürün iadesi — çıkışı yapılmış (stoktan düşülmüş) ürünün geri alınması.

    İki tür: teslimata bağlı (delivery_id dolu — kısmi iade, aşırı-iade SUM guard'lı)
    ve serbest iade (delivery_id NULL — ör. online satış platformu iadesi).
    SAĞLAM kalemler stoğa geri eklenir (Transaction Input, notes='İade (RET-…) …');
    HASARLI/AÇILMIŞ kalemler stoğa GİRMEZ — fire izi ProductReturnItem.condition'da.
    Tablo adı 'returns' DEĞİL (PostgreSQL anahtar kelimesi) → product_returns.
    """
    __tablename__ = "product_returns"

    id = Column(Integer, primary_key=True, index=True)
    document_no = Column(String(40), unique=True, index=True)      # RET-2026-00001
    delivery_id = Column(Integer, ForeignKey("deliveries.id"), nullable=True, index=True)  # NULL = serbest iade
    delivery_document_no = Column(String(40), nullable=True)       # snapshot (TES/KRG-…)
    channel = Column(String(30), default="teslimat")               # teslimat | online | toplanti | diger
    reason = Column(Text, nullable=True)                           # iade gerekçesi
    returned_by = Column(String(150), nullable=True)               # iade eden (müşteri / platform)
    received_by = Column(String(80), nullable=True)                # teslim alan personel (audit)
    doc_lang = Column(String(8), default="TR")                     # belge dili (ürün adı snapshot dili)
    domain = Column(String(20), default="cosmetics", nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    items = relationship("ProductReturnItem", back_populates="ret",
                         cascade="all, delete-orphan")
    delivery = relationship("Delivery", foreign_keys=[delivery_id])


class ProductReturnItem(Base):
    __tablename__ = "product_return_items"

    id = Column(Integer, primary_key=True, index=True)
    return_id = Column(Integer, ForeignKey("product_returns.id"), nullable=False, index=True)
    delivery_item_id = Column(Integer, ForeignKey("delivery_items.id"), nullable=True, index=True)  # aşırı-iade SUM anahtarı
    item_id = Column(Integer, ForeignKey("items.id"), nullable=True)   # ürün silinse de belge okunur
    item_name = Column(String(150), nullable=False)                    # snapshot
    quantity = Column(Float, nullable=False)
    unit = Column(String(20), nullable=True)
    condition = Column(String(20), nullable=False, default="saglam")   # saglam | hasarli | acilmis
    restocked = Column(Boolean, nullable=False, default=False)         # stok geri eklendi mi (yalnız saglam)
    note = Column(Text, nullable=True)

    ret = relationship("ProductReturn", back_populates="items")
    item = relationship("Item", foreign_keys=[item_id])


class SampleAnalysis(Base):
    """Numune Analiz Formu (FR.KK.01) — AR-GE/KK formülasyon deneme analizi kaydı.

    Kağıt formun dijitali: bulk adı, üretim tarihi, lot, özellik tablosu
    (görünüm/koku/renk/pH × spesifikasyon/bulunan değer, JSON string —
    qc_form_data konvansiyonu), analiz sonucu (uygun/uygun_degil/NULL=beklemede),
    SONUÇ metni, imza adları snapshot. Reçete bağı OPSİYONEL (recipe_name
    snapshot'ı reçete silinse de belgeyi okunur tutar). form_code satır-başı
    snapshot: kağıt form revize olursa eski kayıtlar eski revizyonla basılır.
    Items sekmesindeki 'numune' (Inventory.is_sample = tedarikçi numune lotu)
    ile İLGİSİZ — bu tablo bir KK belgesidir.
    """
    __tablename__ = "sample_analyses"

    id = Column(Integer, primary_key=True, index=True)
    document_no = Column(String(40), unique=True, index=True)      # NA-2026-00001
    bulk_name = Column(String(200), nullable=False)                # BULK ADI
    production_date = Column(Date, nullable=True)                  # ÜRETİM TARİHİ
    lot_number = Column(String(100), nullable=True, index=True)    # LOT NUMARASI
    recipe_id = Column(Integer, ForeignKey("recipes.id"), nullable=True)
    recipe_name = Column(String(200), nullable=True)               # snapshot
    formulation_notes = Column(Text, nullable=True)                # çalışılan formülasyon / sapma
    properties = Column(Text, nullable=False, default="[]")        # JSON [{key,label,spec,found}]
    analyst_name = Column(String(100), nullable=True)              # ANALİZ YAPAN / KK SORUMLUSU
    result = Column(String(20), nullable=True)                     # uygun | uygun_degil | NULL=beklemede
    result_text = Column(Text, nullable=True)                      # SONUÇ
    notes = Column(Text, nullable=True)                            # açıklamalar
    approved_by = Column(String(100), nullable=True)               # ONAYLAYAN (ad snapshot)
    qa_representative = Column(String(100), nullable=True)         # AR-GE & KK YÖNETİM TEMSİLCİSİ
    form_code = Column(String(80), nullable=True)                  # FR.KK.01 … Rev:0 (satır snapshot)
    created_by = Column(String(100), nullable=True)
    domain = Column(String(20), default="cosmetics", nullable=False, index=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    recipe = relationship("Recipe", foreign_keys=[recipe_id])


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

    # Lifecycle audit — DRAFT / CONFIRMED (iç); PENDING / REJECTED (distribütör siparişi)
    status       = Column(String(20),  nullable=False, default="DRAFT", index=True)
    domain       = Column(String(20),  nullable=False, default="cosmetics", index=True)  # Faz 3
    created_at   = Column(DateTime,    default=datetime.utcnow)
    created_by   = Column(String(50),  nullable=True)
    confirmed_at = Column(DateTime,    nullable=True)
    confirmed_by = Column(String(50),  nullable=True)

    # Distribütör sipariş portalı: distributor_id set ise sipariş distribütörden
    # geldi (PENDING doğar → onay CONFIRMED / red REJECTED).  Müşteri alanları
    # distribütör profilinden snapshot'lanır.
    distributor_id = Column(Integer, ForeignKey("distributors.id"), nullable=True, index=True)
    submitted_at   = Column(DateTime,   nullable=True)   # distribütör sipariş gönderim anı
    rejected_at    = Column(DateTime,   nullable=True)
    rejected_by    = Column(String(50), nullable=True)
    reject_reason  = Column(Text,       nullable=True)

    items = relationship(
        "QuotationItem",
        back_populates="quotation",
        cascade="all, delete-orphan",
    )
    distributor = relationship("Distributor", foreign_keys=[distributor_id])


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


# ─── Distribütör Sipariş Portalı ─────────────────────────────────────────────

class Distributor(Base):
    """Distribütör hesabı — role='Distributor' bir User'a 1:1 bağlı B2B profili.
    Portal siparişlerinin müşteri (proforma) snapshot'ı buradan alınır."""
    __tablename__ = "distributors"

    id           = Column(Integer, primary_key=True, index=True)
    user_id      = Column(Integer, ForeignKey("users.id"), unique=True, nullable=False, index=True)
    company_name = Column(String(150), nullable=False)
    contact_name = Column(String(150), nullable=True)
    email        = Column(String(150), nullable=True)
    phone        = Column(String(50),  nullable=True)
    address      = Column(Text,        nullable=True)
    country      = Column(String(100), nullable=True)
    vat          = Column(String(50),  nullable=True)
    currency     = Column(String(3),   nullable=False, default="TRY")  # anlaşılan para birimi
    is_active    = Column(Boolean,     default=True)
    created_at   = Column(DateTime,    default=datetime.utcnow)

    user   = relationship("User", foreign_keys=[user_id])
    prices = relationship("DistributorPrice", back_populates="distributor", cascade="all, delete-orphan")


class DistributorPrice(Base):
    """Distribütör × ürün → anlaşılan birim fiyat (distributor.currency cinsinden).
    Bu tablo aynı zamanda distribütörün KATALOĞU: satır yoksa ürün sipariş edilemez."""
    __tablename__ = "distributor_prices"
    __table_args__ = (UniqueConstraint("distributor_id", "item_id", name="uq_distprice_dist_item"),)

    id             = Column(Integer, primary_key=True, index=True)
    distributor_id = Column(Integer, ForeignKey("distributors.id"), nullable=False, index=True)
    item_id        = Column(Integer, ForeignKey("items.id"),        nullable=False, index=True)
    unit_price     = Column(Float,   nullable=False)
    created_at     = Column(DateTime, default=datetime.utcnow)
    updated_at     = Column(DateTime, default=datetime.utcnow)

    distributor = relationship("Distributor", back_populates="prices")
    item        = relationship("Item", foreign_keys=[item_id])


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
    domain = Column(String(20), default="cosmetics", nullable=False, index=True)  # Faz 3 — Kozmetik / Food Supplement


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


class StockSnapshot(Base):
    """
    Aylık stok fotoğrafı — her ayın 1'inde bir önceki ay için dondurulur.

    Aylık stok raporu geçmiş bir ay için önce bu tabloya bakar; varsa
    dondurulmuş kesin değeri kullanır, yoksa transaction rekonstrüksiyonuna
    düşer.  Böylece bir undo / veri temizliği transaction silse bile geçmiş
    ay raporları kaymaz.

    item_id silinen ürünlerde NULL'a düşer (ON DELETE SET NULL); item_name
    ve category denormalize tutulur ki snapshot satırı tek başına okunabilsin.
    """
    __tablename__ = "stock_snapshot"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    year        = Column(Integer, nullable=False, index=True)
    month       = Column(Integer, nullable=False, index=True)
    item_id     = Column(Integer, ForeignKey("items.id", ondelete="SET NULL"), nullable=True)
    item_name   = Column(String(150), nullable=True)
    category    = Column(String(50),  nullable=True)
    pkg_type    = Column(String(20),  nullable=True)
    unit        = Column(String(20),  nullable=True)
    stock       = Column(Float, nullable=False, default=0.0)
    domain      = Column(String(20), default="cosmetics", nullable=False, index=True)  # Faz 3
    captured_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class SystemEvent(Base):
    """
    Sistem olay defteri — şimdilik yalnızca uygulama açılışlarını yazar.

    Uygulama her başladığında ('app_start') buraya bir satır düşer.  Aylık
    detaylı sistem raporu bu tablodan ay içindeki restart/deploy sayısını ve
    zamanlarını çıkarır; her restart ~3-4 sn'lik bir kesinti demek olduğu
    için tahmini downtime de buradan hesaplanır.

    Uygulama kendi *kapanışını* güvenilir biçimde yazamaz (süreç öldürülür),
    bu yüzden burada yalnızca 'açılış' olayı tutulur — gerçek erişilemezlik
    süresi için dışarıdan bir uptime servisi /health endpoint'ini izlemeli.
    """
    __tablename__ = "system_event"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    event_type = Column(String(40), nullable=False, index=True)   # 'app_start'
    detail     = Column(String(255), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


# ─── Minerva Drive — dosya paylaşım (self-hosted) ────────────────────────────

class DriveFolder(Base):
    """Drive klasörü — hiyerarşik ağaç (adjacency list).  `parent_id` NULL = kök.
    Dosyalar `drive_file.folder_id` ile bağlanır; silme uygulamada özyinelemeli
    yapılır (alt klasör + dosya + disk)."""
    __tablename__ = "drive_folder"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    name       = Column(String(255), nullable=False)
    parent_id  = Column(Integer, ForeignKey("drive_folder.id", ondelete="CASCADE"),
                        nullable=True, index=True)   # NULL = kök
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False)


class DriveFile(Base):
    """Yüklenmiş bir dosya.  Fiziksel dosya diskte `stored_name` ile (rastgele);
    `original_name` yalnızca gösterim için (artık SADECE dosya adı — yol klasör
    ağacında).  İçerik DB'de DEĞİL, diskte."""
    __tablename__ = "drive_file"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    original_name = Column(String(255), nullable=False)
    stored_name   = Column(String(80),  nullable=False, unique=True)   # diskteki rastgele ad
    size_bytes    = Column(Integer, nullable=False, default=0)
    content_type  = Column(String(120), nullable=True)
    folder_id     = Column(Integer, ForeignKey("drive_folder.id", ondelete="SET NULL"),
                          nullable=True, index=True)   # NULL = kök dizin
    uploaded_by   = Column(String(100), nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow, nullable=False)


class DriveCollection(Base):
    """Bir paylaşım 'linki' — bir ad + tahmin edilemez kod + (opsiyonel) şifre/süre.
    İçindeki dosyalar drive_collection_file ile (çoka-çok: bir dosya çok linkte)."""
    __tablename__ = "drive_collection"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    name          = Column(String(150), nullable=False)
    share_token   = Column(String(64), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=True)   # bcrypt; NULL = şifresiz
    expires_at    = Column(DateTime, nullable=True)      # NULL = süresiz
    created_by    = Column(String(100), nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow, nullable=False)


class DriveCollectionFile(Base):
    """Hangi dosya hangi koleksiyonda (çoka-çok bağ)."""
    __tablename__ = "drive_collection_file"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(Integer, ForeignKey("drive_collection.id", ondelete="CASCADE"), nullable=False, index=True)
    file_id       = Column(Integer, ForeignKey("drive_file.id", ondelete="CASCADE"), nullable=False, index=True)
    sort_order    = Column(Integer, default=0)


class DriveCollectionFolder(Base):
    """Hangi KLASÖR hangi koleksiyonda — klasör paylaşımı.  Paylaşım, bağlı
    klasörün TÜM alt ağacını (canlı/dinamik) kapsar; karşı taraf gezinebilir."""
    __tablename__ = "drive_collection_folder"

    id            = Column(Integer, primary_key=True, autoincrement=True)
    collection_id = Column(Integer, ForeignKey("drive_collection.id", ondelete="CASCADE"), nullable=False, index=True)
    folder_id     = Column(Integer, ForeignKey("drive_folder.id", ondelete="CASCADE"), nullable=False, index=True)
    sort_order    = Column(Integer, default=0)


# ─── CRM — Müşteri İlişkileri Yönetimi (cross-cutting, domain'siz) ────────────
# CRM tek birleşik platformdur — Kozmetik/Supplement domain ayrımına TABİ DEĞİL
# (Drive gibi cross-cutting).  Bu yüzden bu tablolarda `domain` kolonu YOKTUR.
# Erişim paylaşımlıdır: tüm CRM kullanıcıları tüm kayıt/aktiviteleri görür;
# `owner_*` alanları sorumluluk içindir, erişim kısıtı değil.
# Aktör alanları mevcut desene uyar: string snapshot (full_name) + opsiyonel FK.
# Tüm datetime'lar naive UTC (datetime.utcnow); gösterimde to_tr() ile çevrilir.

class CrmCompany(Base):
    """Firma kartı — bir B2B müşteri/aday firma."""
    __tablename__ = "crm_company"

    id            = Column(Integer, primary_key=True, index=True)
    name          = Column(String(200), nullable=False, index=True)
    sector        = Column(String(100), nullable=True)
    website       = Column(String(200), nullable=True)
    phone         = Column(String(50),  nullable=True)
    email         = Column(String(150), nullable=True)
    address       = Column(Text,        nullable=True)
    city          = Column(String(100), nullable=True)
    country       = Column(String(100), nullable=True)
    tax_office    = Column(String(120), nullable=True)
    tax_no        = Column(String(50),  nullable=True)
    notes         = Column(Text,        nullable=True)
    owner_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    owner_name    = Column(String(100), nullable=True)        # sorumlu kişi snapshot
    source        = Column(String(50),  nullable=True)        # manual / kommo / import …
    kommo_id      = Column(BigInteger, nullable=True, index=True)  # Kommo aynası — upsert anahtarı
    is_active     = Column(Boolean, default=True, nullable=False)   # soft-delete
    created_at    = Column(DateTime, default=datetime.utcnow)
    created_by    = Column(String(100), nullable=True)


class CrmContact(Base):
    """Kişi kartı — bir firmaya bağlı (opsiyonel) kişi."""
    __tablename__ = "crm_contact"

    id              = Column(Integer, primary_key=True, index=True)
    company_id      = Column(Integer, ForeignKey("crm_company.id", ondelete="SET NULL"), nullable=True, index=True)
    full_name       = Column(String(150), nullable=False, index=True)
    title           = Column(String(100), nullable=True)     # unvan
    phone           = Column(String(50),  nullable=True)
    mobile          = Column(String(50),  nullable=True)
    email           = Column(String(150), nullable=True)
    whatsapp_number = Column(String(50),  nullable=True)      # E.164 tercih edilir (+90…)
    source          = Column(String(50),  nullable=True)      # manual / kommo / lead_ad / whatsapp / referral
    kommo_id        = Column(BigInteger, nullable=True, index=True)   # Kommo aynası — upsert anahtarı
    notes           = Column(Text,        nullable=True)
    owner_user_id   = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    owner_name      = Column(String(100), nullable=True)
    is_active       = Column(Boolean, default=True, nullable=False)
    created_at      = Column(DateTime, default=datetime.utcnow)
    created_by      = Column(String(100), nullable=True)

    company = relationship("CrmCompany", foreign_keys=[company_id])


class CrmStage(Base):
    """Satış pipeline aşaması — varsayılan setle seed'lenir, düzenlenebilir."""
    __tablename__ = "crm_stage"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(80), nullable=False)
    sort_order = Column(Integer, default=0, nullable=False)
    is_won     = Column(Boolean, default=False, nullable=False)   # kazanıldı aşaması mı
    is_lost    = Column(Boolean, default=False, nullable=False)   # kaybedildi aşaması mı
    is_active  = Column(Boolean, default=True, nullable=False)


class CrmDeal(Base):
    """Fırsat (opportunity) — pipeline'da bir satış adayı."""
    __tablename__ = "crm_deal"

    id                = Column(Integer, primary_key=True, index=True)
    title             = Column(String(200), nullable=False)
    company_id        = Column(Integer, ForeignKey("crm_company.id", ondelete="SET NULL"), nullable=True, index=True)
    contact_id        = Column(Integer, ForeignKey("crm_contact.id", ondelete="SET NULL"), nullable=True, index=True)
    stage_id          = Column(Integer, ForeignKey("crm_stage.id",   ondelete="SET NULL"), nullable=True, index=True)
    value             = Column(Float,   default=0.0)
    currency          = Column(String(3), nullable=False, default="TRY")
    probability       = Column(Integer, default=0)          # 0–100
    expected_close_at = Column(DateTime, nullable=True)
    status            = Column(String(20), nullable=False, default="open", index=True)  # open / won / lost
    lost_reason       = Column(Text, nullable=True)
    owner_user_id     = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    owner_name        = Column(String(100), nullable=True)
    # B2B köprüsü — kazanılan fırsat mevcut bir Quotation'a atıfta bulunabilir
    quotation_id      = Column(Integer, ForeignKey("quotations.id", ondelete="SET NULL"), nullable=True)
    sort_order        = Column(Integer, default=0)          # Kanban'da aşama-içi sıralama
    source            = Column(String(50), nullable=True)   # manual / kommo / import …
    kommo_id          = Column(BigInteger, nullable=True, index=True)   # Kommo aynası — upsert anahtarı
    stage_changed_at  = Column(DateTime, nullable=True, default=datetime.utcnow)  # aşamaya giriş anı (yaş rozeti)
    is_active         = Column(Boolean, default=True, nullable=False)   # soft-delete (arşiv)
    created_at        = Column(DateTime, default=datetime.utcnow)
    created_by        = Column(String(100), nullable=True)
    won_at            = Column(DateTime, nullable=True)
    closed_at         = Column(DateTime, nullable=True)

    company = relationship("CrmCompany", foreign_keys=[company_id])
    contact = relationship("CrmContact", foreign_keys=[contact_id])
    stage   = relationship("CrmStage",   foreign_keys=[stage_id])


class CrmActivity(Base):
    """Paylaşımlı zaman çizelgesi kaydı — not / arama / toplantı / e-posta / whatsapp.
    Bir firmaya ve/veya kişiye ve/veya fırsata bağlanabilir.  Herkes görür."""
    __tablename__ = "crm_activity"

    id             = Column(Integer, primary_key=True, index=True)
    company_id     = Column(Integer, ForeignKey("crm_company.id", ondelete="CASCADE"), nullable=True, index=True)
    contact_id     = Column(Integer, ForeignKey("crm_contact.id", ondelete="CASCADE"), nullable=True, index=True)
    deal_id        = Column(Integer, ForeignKey("crm_deal.id",    ondelete="CASCADE"), nullable=True, index=True)
    type           = Column(String(20), nullable=False, default="note")   # note|call|meeting|email|whatsapp
    subject        = Column(String(200), nullable=True)
    body           = Column(Text, nullable=True)
    author_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    author_name    = Column(String(100), nullable=True)
    is_pinned      = Column(Boolean, default=False, nullable=False)
    # Harici kaynak referansı (örn. 'kommo_evt_<id>') — tekrar senkronda çift kayıt önler
    external_id    = Column(String(80), nullable=True, index=True)
    created_at     = Column(DateTime, default=datetime.utcnow, index=True)


class CrmTask(Base):
    """Hatırlatma / görev — son tarihli, bir kullanıcıya atanır.  Günlük scheduler
    taraması vadesi gelen/geçen açık görevler için web push gönderir."""
    __tablename__ = "crm_task"

    id                  = Column(Integer, primary_key=True, index=True)
    title               = Column(String(200), nullable=False)
    notes               = Column(Text, nullable=True)
    due_at              = Column(DateTime, nullable=True, index=True)
    status              = Column(String(20), nullable=False, default="open", index=True)  # open / done
    company_id          = Column(Integer, ForeignKey("crm_company.id", ondelete="SET NULL"), nullable=True, index=True)
    contact_id          = Column(Integer, ForeignKey("crm_contact.id", ondelete="SET NULL"), nullable=True, index=True)
    deal_id             = Column(Integer, ForeignKey("crm_deal.id",    ondelete="SET NULL"), nullable=True, index=True)
    assigned_to_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    assigned_to_name    = Column(String(100), nullable=True)
    created_at          = Column(DateTime, default=datetime.utcnow)
    created_by          = Column(String(100), nullable=True)
    completed_at        = Column(DateTime, nullable=True)
    # Tekrarlı push'u önlemek için — bir görev için hatırlatma yollandı mı?
    reminder_sent       = Column(Boolean, default=False, nullable=False)


class CrmSavedView(Base):
    """Kullanıcı başına kaydedilmiş liste görünümü (filtre seti) — örn. 'Meta lead'lerim'.
    criteria JSON: {q, source, owner} gibi liste filtreleri."""
    __tablename__ = "crm_saved_view"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    entity     = Column(String(20), nullable=False)   # companies | contacts | deals
    name       = Column(String(80), nullable=False)
    criteria   = Column(Text, nullable=True)          # JSON
    created_at = Column(DateTime, default=datetime.utcnow)


class CrmTag(Base):
    """Etiket — firmalara/kişilere/fırsatlara takılır (çoka-çok)."""
    __tablename__ = "crm_tag"

    id    = Column(Integer, primary_key=True, index=True)
    name  = Column(String(60), nullable=False, unique=True)
    color = Column(String(20), nullable=True)


class CrmEntityTag(Base):
    """Etiket bağı — (entity, entity_id) → tag.  Tek tablo, tüm varlıklar."""
    __tablename__ = "crm_entity_tag"

    id        = Column(Integer, primary_key=True, index=True)
    entity    = Column(String(20), nullable=False, index=True)   # company | contact | deal
    entity_id = Column(Integer, nullable=False, index=True)
    tag_id    = Column(Integer, ForeignKey("crm_tag.id", ondelete="CASCADE"), nullable=False, index=True)


class CrmFieldDef(Base):
    """Özel alan tanımı — entity bazında yapılandırılabilir ek alanlar."""
    __tablename__ = "crm_field_def"

    id         = Column(Integer, primary_key=True, index=True)
    entity     = Column(String(20), nullable=False, index=True)   # company | contact | deal
    key        = Column(String(40), nullable=False)               # makine adı
    label      = Column(String(80), nullable=False)               # gösterim
    field_type = Column(String(20), nullable=False, default="text")  # text|number|date|select
    options    = Column(Text, nullable=True)                      # select için JSON liste
    sort_order = Column(Integer, default=0, nullable=False)
    is_active  = Column(Boolean, default=True, nullable=False)


class CrmFieldValue(Base):
    """Özel alan değeri — (entity, entity_id, field) → değer."""
    __tablename__ = "crm_field_value"

    id        = Column(Integer, primary_key=True, index=True)
    field_id  = Column(Integer, ForeignKey("crm_field_def.id", ondelete="CASCADE"), nullable=False, index=True)
    entity    = Column(String(20), nullable=False, index=True)
    entity_id = Column(Integer, nullable=False, index=True)
    value     = Column(Text, nullable=True)


class CrmAttachment(Base):
    """Kayda eklenen dosya — fiziksel dosya Drive deposunda (stored_name)."""
    __tablename__ = "crm_attachment"

    id            = Column(Integer, primary_key=True, index=True)
    entity        = Column(String(20), nullable=False, index=True)   # company | contact | deal
    entity_id     = Column(Integer, nullable=False, index=True)
    original_name = Column(String(255), nullable=False)
    stored_name   = Column(String(80), nullable=False)               # Drive diskindeki rastgele ad
    size_bytes    = Column(Integer, nullable=False, default=0)
    content_type  = Column(String(120), nullable=True)
    uploaded_by   = Column(String(100), nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)


class CrmWaTemplate(Base):
    """WhatsApp mesaj şablonu — tıkla-konuş linkine hazır metin ekler.
    {ad} yer tutucusu kişinin ilk adıyla değiştirilir.  Ekip genelinde paylaşımlı."""
    __tablename__ = "crm_wa_template"

    id         = Column(Integer, primary_key=True, index=True)
    name       = Column(String(100), nullable=False)
    body       = Column(String(1000), nullable=False)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class CrmIntegrationState(Base):
    """Harici CRM entegrasyon durumu (şimdilik Kommo) — delta senkron imleci +
    son çalıştırma özeti.  Her sağlayıcı için tek satır (provider unique)."""
    __tablename__ = "crm_integration_state"

    id            = Column(Integer, primary_key=True, index=True)
    provider      = Column(String(40), nullable=False, unique=True, index=True)  # 'kommo'
    last_sync_at  = Column(DateTime, nullable=True)   # en son başarılı senkron (UTC)
    cursor        = Column(BigInteger, nullable=True) # delta için epoch (updated_at) imleci
    last_status   = Column(String(255), nullable=True)  # son çalıştırma özeti / hata
    last_run_at   = Column(DateTime, nullable=True)
    imported_total = Column(Integer, default=0, nullable=False)
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


def log_system_event(event_type: str, detail: str = None) -> None:
    """Bir sistem olayını (örn. 'app_start') kaydet.

    Kendi kısa-ömürlü session'ını açar/kapatır.  ASLA exception fırlatmaz —
    bir log kaydı uğruna uygulama açılışı bozulmamalı."""
    try:
        db = SessionLocal()
        try:
            db.add(SystemEvent(event_type=event_type, detail=detail))
            db.commit()
        finally:
            db.close()
    except Exception:
        pass


def _backfill_drive_folders():
    """original_name'inde '/' olan DriveFile'ları gerçek klasör ağacına yerleştir.

    Eski "klasör yükleme" yolu (Serenida/MSDS/x.pdf) görüntü adına gömüyordu;
    bunları `drive_folder` ağacına çevirir, `original_name`'i basename'e indirir.
    İdempotent: dönüşüm sonrası adlarda '/' kalmaz → tekrar çağrı no-op."""
    db = SessionLocal()
    try:
        pending = db.query(DriveFile).filter(DriveFile.original_name.like("%/%")).all()
        if not pending:
            return
        cache = {}   # (parent_id, name) -> folder.id

        def _get_or_create(name, parent_id, actor):
            key = (parent_id, name)
            if key in cache:
                return cache[key]
            q = db.query(DriveFolder).filter(DriveFolder.name == name)
            q = q.filter(DriveFolder.parent_id.is_(None) if parent_id is None
                         else DriveFolder.parent_id == parent_id)
            f = q.first()
            if not f:
                f = DriveFolder(name=name[:255], parent_id=parent_id, created_by=actor)
                db.add(f); db.flush()
            cache[key] = f.id
            return f.id

        for rec in pending:
            raw = (rec.original_name or "").replace("\\", "/")
            parts = [p.strip() for p in raw.split("/")
                     if p.strip() and p.strip() not in (".", "..")]
            if len(parts) < 2:
                rec.original_name = (parts[-1] if parts else "dosya")[:255]
                continue
            *folders, fname = parts
            pid = None
            for seg in folders:
                pid = _get_or_create(seg, pid, rec.uploaded_by)
            rec.folder_id = pid
            rec.original_name = fname[:255]
        db.commit()
    finally:
        db.close()


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
            # Phase 15 — Etiket dil ayrımı (TR/EN) + grup
            "ALTER TABLE items              ADD COLUMN language VARCHAR(8)",
            "ALTER TABLE items              ADD COLUMN label_group VARCHAR(255)",
            "CREATE INDEX IF NOT EXISTS ix_items_label_group ON items(label_group)",
            # Phase 16 — Üretim föyü: FAZ + YAPILIŞI
            "ALTER TABLE recipe_ingredients ADD COLUMN phase VARCHAR(8)",
            "ALTER TABLE recipes            ADD COLUMN production_notes TEXT",
            # Faz 2 — Numune lotu işareti (alternatif tedarikçi numuneleri)
            "ALTER TABLE inventory          ADD COLUMN is_sample BOOLEAN DEFAULT FALSE",
            # Faz 3 — Kozmetik / Food Supplement domain ayrımı.  PG'de DEFAULT'lu
            # ADD COLUMN mevcut satırları otomatik 'cosmetics' ile doldurur.
            "ALTER TABLE items              ADD COLUMN domain VARCHAR(20) NOT NULL DEFAULT 'cosmetics'",
            "ALTER TABLE suppliers          ADD COLUMN domain VARCHAR(20) NOT NULL DEFAULT 'cosmetics'",
            "ALTER TABLE recipes            ADD COLUMN domain VARCHAR(20) NOT NULL DEFAULT 'cosmetics'",
            "ALTER TABLE inventory          ADD COLUMN domain VARCHAR(20) NOT NULL DEFAULT 'cosmetics'",
            "ALTER TABLE production_history ADD COLUMN domain VARCHAR(20) NOT NULL DEFAULT 'cosmetics'",
            "ALTER TABLE quotations         ADD COLUMN domain VARCHAR(20) NOT NULL DEFAULT 'cosmetics'",
            "ALTER TABLE stock_snapshot     ADD COLUMN domain VARCHAR(20) NOT NULL DEFAULT 'cosmetics'",
            "CREATE INDEX IF NOT EXISTS ix_items_domain      ON items(domain)",
            "CREATE INDEX IF NOT EXISTS ix_suppliers_domain  ON suppliers(domain)",
            "CREATE INDEX IF NOT EXISTS ix_recipes_domain    ON recipes(domain)",
            "CREATE INDEX IF NOT EXISTS ix_inventory_domain  ON inventory(domain)",
            "CREATE INDEX IF NOT EXISTS ix_prodhist_domain   ON production_history(domain)",
            "CREATE INDEX IF NOT EXISTS ix_quotations_domain ON quotations(domain)",
            "CREATE INDEX IF NOT EXISTS ix_snapshot_domain   ON stock_snapshot(domain)",
            # CRM Kommo aynası — kommo_id (upsert anahtarı) + source
            "ALTER TABLE crm_company ADD COLUMN source VARCHAR(50)",
            "ALTER TABLE crm_company ADD COLUMN kommo_id BIGINT",
            "ALTER TABLE crm_contact ADD COLUMN kommo_id BIGINT",
            "ALTER TABLE crm_deal    ADD COLUMN source VARCHAR(50)",
            "ALTER TABLE crm_deal    ADD COLUMN kommo_id BIGINT",
            "CREATE INDEX IF NOT EXISTS ix_crm_company_kommo ON crm_company(kommo_id)",
            "CREATE INDEX IF NOT EXISTS ix_crm_contact_kommo ON crm_contact(kommo_id)",
            "CREATE INDEX IF NOT EXISTS ix_crm_deal_kommo    ON crm_deal(kommo_id)",
            # CRM aktivite — harici kaynak referansı (Kommo mesaj olayları dedup)
            "ALTER TABLE crm_activity ADD COLUMN external_id VARCHAR(80)",
            "CREATE INDEX IF NOT EXISTS ix_crm_activity_extid ON crm_activity(external_id)",
            # Minerva Drive — klasör ağacı.  drive_folder tablosu create_all ile
            # gelir; drive_file'a folder_id eklenir (kök = NULL).
            "ALTER TABLE drive_file ADD COLUMN folder_id INTEGER",
            "CREATE INDEX IF NOT EXISTS ix_drive_file_folder ON drive_file(folder_id)",
            # Teslimat: çift-dilli ad + belge dili + proforma fatura alanları
            "ALTER TABLE items       ADD COLUMN name_tr VARCHAR(150)",
            "ALTER TABLE deliveries  ADD COLUMN doc_lang VARCHAR(8) DEFAULT 'TR'",
            "ALTER TABLE deliveries  ADD COLUMN customer_address TEXT",
            "ALTER TABLE deliveries  ADD COLUMN customer_country VARCHAR(100)",
            "ALTER TABLE deliveries  ADD COLUMN currency VARCHAR(3) DEFAULT 'USD'",
            "ALTER TABLE deliveries  ADD COLUMN status VARCHAR(20) DEFAULT 'completed'",
            "ALTER TABLE deliveries  ADD COLUMN approved_by VARCHAR(80)",
            "ALTER TABLE deliveries  ADD COLUMN approved_at TIMESTAMP",
            "ALTER TABLE deliveries  ADD COLUMN reject_reason TEXT",
            "CREATE INDEX IF NOT EXISTS ix_deliveries_status ON deliveries(status)",
            "ALTER TABLE delivery_items ADD COLUMN unit_price DOUBLE PRECISION",
            "ALTER TABLE delivery_items ADD COLUMN weight_ml DOUBLE PRECISION",
            # Kargo / Sevkiyat (ertelenmiş stok — takip no atanınca düşer)
            "ALTER TABLE deliveries  ADD COLUMN tracking_no VARCHAR(100)",
            "ALTER TABLE deliveries  ADD COLUMN carrier VARCHAR(80)",
            "ALTER TABLE deliveries  ADD COLUMN shipped_at TIMESTAMP",
            "ALTER TABLE deliveries  ADD COLUMN shipped_by VARCHAR(80)",
            "ALTER TABLE deliveries  ADD COLUMN tracking_legs TEXT",
            # Distribütör sipariş portalı — distributors / distributor_prices tabloları
            # create_all ile gelir; quotations'a distribütör + red alanları eklenir.
            "ALTER TABLE quotations ADD COLUMN distributor_id INTEGER REFERENCES distributors(id)",
            "ALTER TABLE quotations ADD COLUMN submitted_at TIMESTAMP",
            "ALTER TABLE quotations ADD COLUMN rejected_at TIMESTAMP",
            "ALTER TABLE quotations ADD COLUMN rejected_by VARCHAR(50)",
            "ALTER TABLE quotations ADD COLUMN reject_reason TEXT",
            "CREATE INDEX IF NOT EXISTS ix_quotations_distributor ON quotations(distributor_id)",
            # CRM yükseltme — aşama yaşı + fırsat soft-delete (crm_wa_template
            # tablosu create_all ile gelir).  UPDATE'ler NULL-korumalı → idempotent.
            "ALTER TABLE crm_deal ADD COLUMN stage_changed_at TIMESTAMP",
            "UPDATE crm_deal SET stage_changed_at = created_at WHERE stage_changed_at IS NULL",
            "ALTER TABLE crm_deal ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE",
            "UPDATE crm_deal SET is_active = TRUE WHERE is_active IS NULL",
        ):
            alter_safe(stmt)

    # Eski Drive klasör-yüklemelerini (adında '/' olanlar) gerçek klasör ağacına
    # çevir — idempotent ('/' kalmayınca no-op).
    try:
        _backfill_drive_folders()
    except Exception:
        pass

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

    # ── CRM pipeline aşamaları — varsayılan set (idempotent) ────────────────
    # SEED_DEFAULT_USERS'tan BAĞIMSIZ: aşamalar prod'da da olmalı, yoksa
    # pipeline boş açılır.  Hiç aşama yoksa varsayılan akışı kurar; mevcutsa
    # dokunmaz (kullanıcı düzenlemiş olabilir).
    db = SessionLocal()
    try:
        if db.query(CrmStage).count() == 0:
            _default_stages = [
                ("Yeni",            0, False, False),
                ("İletişim Kuruldu", 1, False, False),
                ("Teklif",          2, False, False),
                ("Müzakere",        3, False, False),
                ("Kazanıldı",       4, True,  False),
                ("Kaybedildi",      5, False, True),
            ]
            for nm, so, won, lost in _default_stages:
                db.add(CrmStage(name=nm, sort_order=so, is_won=won, is_lost=lost))
            db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()
