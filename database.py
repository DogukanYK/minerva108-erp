# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
import os
from sqlalchemy import (
    create_engine, Column, Integer, BigInteger, String, Float,
    Boolean, Text, Date, DateTime, ForeignKey, UniqueConstraint, Index, text
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
    # Ürün bazlı üretim sayacı — lot önerisi (MNR006).  Geçmiş taramasıyla
    # birlikte kullanılır (core/lots.next_sequence), tek başına otorite değil.
    lot_seq = Column(Integer, nullable=False, default=0)

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

    Fiyat temeli: `currency` + `price_unit` (kg | l | adet) birlikte okunur —
    Stok Son Durum listesi USD/kg'dir, adet birimli malzemede fiyat adet
    başınadır.  `currency` model varsayılanı tarihsel olarak 'TRY'; eski satırlar
    `_backfill_supplier_price_units()` ile bir kez USD + kg/adet işaretlendi.
    `source` (stok_son_durum | manual) + `source_label` + `quoted_at` raporda
    "fiyat kaynağı" notunu besler.
    """
    __tablename__ = "supplier_prices"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    supplier_name = Column(String(150), nullable=True)   # görüntü adı (eşleşmese de saklanır)
    package_size = Column(Float, nullable=True)          # alınabilecek miktar / min sipariş (malzeme birimi cinsinden)
    unit_price = Column(Float, nullable=True)            # birim fiyat
    currency = Column(String(8), default="TRY")
    price_unit = Column(String(8), nullable=True)        # fiyatın temeli: kg / l / adet
    source = Column(String(30), nullable=True)           # stok_son_durum / manual
    source_label = Column(String(120), nullable=True)    # ör. "Stok Son Durum — Eylül 2026"
    quoted_at = Column(Date, nullable=True)              # teklif/liste tarihi
    note = Column(Text, nullable=True)
    domain = Column(String(20), default="cosmetics", nullable=False, index=True)  # Kozmetik / Food Supplement
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    item = relationship("Item", foreign_keys=[item_id])
    supplier = relationship("Supplier", foreign_keys=[supplier_id])


class StockOrderFlag(Base):
    """Eksik Hammaddeler raporu — 'sipariş verildi' işareti.

    Ürün başına EN FAZLA bir AÇIK (closed_at IS NULL) bayrak — kısmi tekil
    indeks bunu DB seviyesinde zorlar. Yeniden işaretlemede eskisi
    `closed_reason='manual'` ile kapanır. Gerçek mal kabul (receive_stock
    numune-olmayan dal + samples/convert) `closed_reason='received'` ile
    kapatır (routers/inventory.py, core/stock_gaps.close_open_flags).
    Elle stok düzeltmesi (adjust_stock) KAPATMAZ — sayım düzeltmesi teslimat
    değildir. Görüntüleme core/stock_gaps.py'dedir.
    """
    __tablename__ = "stock_order_flags"

    id = Column(Integer, primary_key=True, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False, index=True)
    supplier_id = Column(Integer, ForeignKey("suppliers.id"), nullable=True)
    quantity = Column(Float, nullable=True)
    unit = Column(String(20), nullable=True)             # item.unit snapshot
    expected_date = Column(Date, nullable=True)
    note = Column(String(300), nullable=True)
    ordered_by = Column(String(50), nullable=True)
    ordered_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    closed_at = Column(DateTime, nullable=True)
    closed_reason = Column(String(10), nullable=True)    # received | manual
    domain = Column(String(20), default="cosmetics", nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        Index("uq_stock_order_flags_open_item", "item_id", unique=True,
              postgresql_where=text("closed_at IS NULL")),
    )

    item = relationship("Item", foreign_keys=[item_id])
    supplier = relationship("Supplier", foreign_keys=[supplier_id])


class PurchasePlan(Base):
    """Satın Alma Planı — kaydedilmiş senaryo ("Rusya Siparişi" gibi).

    `config` = `core.purchase_plan_models.PlanRequest`'in JSON'u, sürümlü:
    `{"version": 1, "title", "lines", "manual_lines", "options"}`.  Model
    alanları daima varsayılanlı genişletilir → eski senaryo aynen açılır.
    Senaryo id'lere (reçete/kalem) bakar; silinmiş ya da pasif reçete/kart
    `GET /api/purchase-plan/scenarios/{id}` yanıtında `missing[]` olarak
    işaretlenir, sessizce düşürülmez.  Domain-kapsamlı; ad AKTİF kayıtlarda
    panel içinde tekil (kısmi tekil indeks).  Düzenleme/silme: sahibi
    (`created_by_id`), SuperAdmin ya da Manager.  Silme yumuşak
    (`is_active=False`).  `last_run_at` önizleme/dışa aktarma
    `?scenario_id=` ile çağrılınca damgalanır.  Uçlar `routers/purchase_plan.py`.
    """
    __tablename__ = "purchase_plans"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String(150), nullable=False)
    config = Column(Text, nullable=False)                # JSON {"version": 1, ...}
    domain = Column(String(20), default="cosmetics", nullable=False, index=True)
    created_by_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_by = Column(String(80), nullable=True)
    updated_by = Column(String(80), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_run_at = Column(DateTime, nullable=True)
    is_active = Column(Boolean, default=True, nullable=False)

    __table_args__ = (
        Index("uq_purchase_plans_domain_name_active", "domain", "name", unique=True,
              postgresql_where=text("is_active")),
    )


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
    Bileşen satırları `SampleAnalysisIngredient`'ta: hangi hammadde, nereden
    (Items 'Numune' sekmesindeki numune lotu / normal stok / henüz gelmedi), ne
    kadar.  Kullanılan miktar kaynağından DÜŞÜLÜR — motor core/sample_trial_stock.
    `mode`: 'existing' (mevcut reçete üzerinde çalışma) | 'new' (yeni reçete).
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

    mode = Column(String(10), nullable=False, default="new")      # existing | new

    recipe = relationship("Recipe", foreign_keys=[recipe_id])
    ingredients = relationship("SampleAnalysisIngredient", back_populates="analysis",
                               cascade="all, delete-orphan",
                               order_by="SampleAnalysisIngredient.position")


class SampleAnalysisIngredient(Base):
    """FR.KK.01 deneme satırı — hangi hammadde, nereden, ne kadar.

    source: 'sample' (Inventory.is_sample lotu, inventory_id dolu) | 'stock'
    (normal stok, FIFO) | 'pending' (henüz gelmedi / kullanılmadı).
    consumed_qty = şu an fiilen DÜŞÜLMÜŞ miktar; PUT/DELETE bu sayaca göre fark
    uygular (core/sample_trial_stock.py).  Numune lotunda yalnız
    Inventory.quantity iner (numune stok DEĞİLDİR — Transaction yok); stokta
    core/stock_lots.consume (lot başına Output + current_stock).
    inventory_id ondelete=SET NULL: numune stoğa çevrilirken aynı lotlu normal
    satıra birleşince numune satırı silinir; belge snapshot'larla okunur kalır.
    """
    __tablename__ = "sample_analysis_ingredients"

    id = Column(Integer, primary_key=True, index=True)
    analysis_id = Column(Integer, ForeignKey("sample_analyses.id"), nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False, index=True)
    item_name = Column(String(150), nullable=False)                 # snapshot
    unit = Column(String(20), nullable=True)                        # item.unit snapshot
    source = Column(String(10), nullable=False, default="pending")  # sample | stock | pending
    inventory_id = Column(Integer, ForeignKey("inventory.id", ondelete="SET NULL"),
                          nullable=True, index=True)
    lot_number = Column(String(100), nullable=True)                 # snapshot
    supplier_name = Column(String(150), nullable=True)              # snapshot
    quantity = Column(Float, nullable=False, default=0.0)
    consumed_qty = Column(Float, nullable=False, default=0.0)
    note = Column(String(300), nullable=True)
    position = Column(Integer, nullable=False, default=0)

    analysis = relationship("SampleAnalysis", back_populates="ingredients")
    item = relationship("Item", foreign_keys=[item_id])
    inventory = relationship("Inventory", foreign_keys=[inventory_id])


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
    # Kaç adedi şahit numune dolabına ayrıldı.  Bugüne kadar bu bilgi yalnız
    # Transaction not metninde vardı; rapor/denetim için kalıcı kolon.
    witness_quantity = Column(Float, nullable=False, default=0.0)
    domain = Column(String(20), default="cosmetics", nullable=False, index=True)  # Faz 3 — Kozmetik / Food Supplement


class RetentionSample(Base):
    """Şahit numune dolabı kaydı — `Inventory` üstünde bir YÖNETİM katmanı.

    Üretimde şahit numuneye ayrılan adetler bugünkü gibi `Inventory`'ye `-S`
    lotu olarak yazılmaya devam eder (stok kaynağı orası; `Item.current_stock`
    içinde sayılırlar).  Bu tablo onların üstüne, `Inventory`'de yeri olmayan
    bilgiyi ekler: hangi dolapta, hangi rafta/gözde, ne zamana kadar saklanacak,
    kim ne zaman çıkardı.

    `inventory_id` UNIQUE — bir `-S` lotunun tek yönetim kaydı olur; backfill
    script'inin idempotency anahtarı da budur.

    Dolap = MARKA (ayrı tablo yok).  `brand` kanonik yazımdır ("Minerva 108"),
    `core.brands.cabinet_of()` üretir.
    """
    __tablename__ = "retention_samples"

    id = Column(Integer, primary_key=True, index=True)
    inventory_id = Column(Integer, ForeignKey("inventory.id"), nullable=True,
                          unique=True, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False, index=True)
    item_name = Column(String(150), nullable=True)                 # snapshot
    lot_number = Column(String(100), nullable=False, index=True)    # Inventory'dekiyle aynı (-S dahil)
    production_history_id = Column(Integer, ForeignKey("production_history.id"), nullable=True)
    brand = Column(String(60), nullable=False, index=True)          # = dolap kimliği
    shelf = Column(String(20), nullable=True)                       # raf
    slot = Column(String(20), nullable=True)                        # göz
    quantity = Column(Float, nullable=False, default=0.0)           # dolapta KALAN
    initial_quantity = Column(Float, nullable=False, default=0.0)   # ilk konulan (değişmez)
    unit = Column(String(20), nullable=True)
    produced_at = Column(DateTime, nullable=True)
    retention_until = Column(Date, nullable=True, index=True)       # imha uyarısının anahtarı
    status = Column(String(20), nullable=False, default="stored")   # stored | depleted | destroyed
    qc_status = Column(String(20), nullable=True)                   # NULL | rejected
    source = Column(String(20), nullable=False, default="production")  # production | backfill | manual
    placed_by = Column(String(80), nullable=True)
    note = Column(Text, nullable=True)
    # Sayım aktarımında SKT'si belirsiz kalan kayıtlar için yapısal bayrak.
    # Eskiden not METNİNE "TEYİT BEKLİYOR" gömülüydü — filtrelenemiyordu ve
    # "teyit edildi" durumu hiçbir yerde saklanmıyordu.
    needs_review = Column(Boolean, nullable=False, default=False)
    # Soft-delete: YANLIŞ GİRİLEN kaydı gizler.  Fiziksel imhadan (status=
    # 'destroyed', stoktan düşer) tamamen ayrı bir kavramdır — bu bayrak
    # stoğa/Transaction defterine ASLA dokunmaz.
    is_active = Column(Boolean, nullable=False, default=True)
    domain = Column(String(20), default="cosmetics", nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    item = relationship("Item", foreign_keys=[item_id])
    movements = relationship("RetentionSampleMovement", back_populates="sample",
                             cascade="all, delete-orphan",
                             order_by="RetentionSampleMovement.created_at")
    checks = relationship("RetentionSampleCheck", back_populates="sample",
                          cascade="all, delete-orphan",
                          order_by="RetentionSampleCheck.checked_on")


class RetentionSampleMovement(Base):
    """Dolap hareketi — kim / ne zaman / neden / kaç adet.

    `quantity` DAİMA pozitiftir; yönü `movement_type` belirler.  Dolap içi
    hareketler (`konum`, `duzeltme`) stok yazmaz; `cikis`/`imha` ise çağıran
    router'da ayrıca `Inventory` + `Item.current_stock` düşümü ve bir
    `Transaction(Output)` üretir (bkz. routers/retention.py).
    """
    __tablename__ = "retention_sample_movements"

    id = Column(Integer, primary_key=True, index=True)
    sample_id = Column(Integer, ForeignKey("retention_samples.id"), nullable=False, index=True)
    movement_type = Column(String(20), nullable=False)   # giris|cikis|imha|konum|duzeltme|kontrol
    quantity = Column(Float, nullable=False, default=0.0)
    reason = Column(String(40), nullable=True)           # test|musteri|denetim|lab|diger
    note = Column(Text, nullable=True)
    performed_by = Column(String(80), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

    sample = relationship("RetentionSample", back_populates="movements")


class RetentionSampleCheck(Base):
    """Şahit numunenin periyodik kontrol kaydı (GMP gözlemi).

    Numune dolaptan alınıp bakıldığında doldurulur: görünüm/koku/renk/ayrışma/
    ambalaj için 'normal | değişim var' + not, sonunda genel sonuç.

    Neden AYRI TABLO (`RetentionSampleMovement`'a gömülmedi): hareket tablosu
    "kim/ne zaman/neden/**kaç adet**" için; kontrolün adedi yok, `quantity`
    semantiği bozulurdu.  Ayrıca `note` alanına JSON gömmek Detay ekranında
    ham JSON gösterirdi ve "son kontrol sonucu" sorgulanamazdı.
    `Inventory.qc_form_data` kalıbı da uymuyor — o TEK seferlik/lot-başına-tek
    form (kolon üzerine yazılır); kontrol ise N kayıt × 1 numunedir.

    Gözlem tablosu `SampleAnalysis.properties` kalıbı: JSON string.  Etiket
    metni YAZILMAZ, yalnız `key` — kalem listesi `core/retention.CHECK_ITEMS`
    tek kaynağından okunur.

    APPEND-ONLY: kontrol kaydı düzenlenmez/silinmez.  Yanlış girilen kontrol
    için yeni kayıt açılır — KK gözleminde doğru olan budur.
    `domain` kolonu YOK: erişim daima ebeveyn numune üzerinden (`_get`) geçer,
    o da domain-scoped'tur.
    """
    __tablename__ = "retention_sample_checks"

    id = Column(Integer, primary_key=True, index=True)
    sample_id = Column(Integer, ForeignKey("retention_samples.id"), nullable=False, index=True)
    checked_on = Column(Date, nullable=False)            # kontrolün YAPILDIĞI gün (kullanıcı girer)
    properties = Column(Text, nullable=False, default="[]")   # JSON [{key,state,note}]
    result = Column(String(20), nullable=False)          # uygun | uygun_degil
    result_note = Column(Text, nullable=True)
    checked_by = Column(String(80), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)  # sunucu damgası

    sample = relationship("RetentionSample", back_populates="checks")


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


class DuplicateItemDecision(Base):
    """Kopya hammadde kartı karar kaydı — 24.08.2026 numune olayı taramasından.

    Aynı fiziksel malzemenin birden fazla kartla yaşadığı tespit edildi (stok
    bölünüyor, reçeteler farklı kartlara bakıyor).  Hangi kartların aynı ürün
    olduğuna patron değil LAB karar verir — Ürünler sayfasındaki popup bu
    tablodan beslenir.  'merge' kararı sunucuda core/item_merge.merge_items
    ile ANINDA uygulanır (Adjustment çifti + FK taşıma + pasifleştirme),
    'keep' kararı kümeyi kapatır ve bir daha sorulmaz.
    """
    __tablename__ = "duplicate_item_decisions"

    id             = Column(Integer, primary_key=True, index=True)
    cluster_key    = Column(String(80), unique=True, nullable=False, index=True)
    title          = Column(String(150), nullable=False)     # popup başlığı
    item_ids       = Column(Text, nullable=False)            # JSON int listesi
    status         = Column(String(20), default="pending", nullable=False, index=True)  # pending|merged|kept
    target_item_id = Column(Integer, ForeignKey("items.id"), nullable=True)  # merge hedefi
    decided_by     = Column(String(80), nullable=True)
    decided_at     = Column(DateTime, nullable=True)
    result_note    = Column(Text, nullable=True)             # birleştirme özeti / sebep
    created_at     = Column(DateTime, default=datetime.utcnow)


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
    # Influencer programı — "ürün ulaştı, içerik bekleniyor" hatırlatmaları
    # aynı 08:00 taramasından push'lanır; bu kolon görevi iş birliğine bağlar.
    # FK YOK (bilinçli): crm_task, influencer_collab'dan önce kurulur; kolon
    # prod'a init_db() alter_safe satırıyla ulaşır (migration b5d7f9a1c3e5).
    influencer_collab_id = Column(Integer, nullable=True)

    __table_args__ = (
        Index("ix_crm_task_inf_collab", "influencer_collab_id"),
    )


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


class ShopifySyncState(Base):
    """IMS → Shopify stok push durumu — mağaza (marka) başına tek satır.

    IMS tek doğruluk kaynağı; bu tablo yalnız son push denemesinin sonucunu tutar
    (CrmIntegrationState kalıbı). Global buffer/enabled ayarları AppSetting'te
    (`shopify.buffer` / `shopify.enabled`); buradaki `enabled` per-store duraklatma.
    """
    __tablename__ = "shopify_sync_state"

    id            = Column(Integer, primary_key=True, index=True)
    store_key     = Column(String(20), nullable=False, unique=True, index=True)  # minerva/evanira/serenida
    brand         = Column(String(40), nullable=True)                            # görüntü adı
    enabled       = Column(Boolean, default=True, nullable=False)                # per-store duraklat
    last_sync_at  = Column(DateTime, nullable=True)     # son BAŞARILI push (UTC)
    last_run_at   = Column(DateTime, nullable=True)     # son deneme
    last_status   = Column(String(255), nullable=True)  # özet / hata
    matched_count = Column(Integer, default=0, nullable=False)
    pushed_count  = Column(Integer, default=0, nullable=False)
    unmatched     = Column(Text, nullable=True)         # JSON {shopify_only, ims_only, ambiguous}
    updated_at    = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ShopifyOrder(Base):
    """Shopify orders/paid webhook kaydı — idempotency + fatura durum makinesi.

    Durumlar: received → stock_done → invoiced → paid → legalized
    Uçlar: skipped_export (TR dışı — ihracat manuel) · failed (step+last_error ile;
    retry job kaldığı ADIMDAN devam eder — stok ASLA ikinci kez düşmez).
    UniqueConstraint(store_key, shopify_order_id) = webhook redelivery dedup anahtarı.
    lines_json: yalnız gerekli alt küme (barcode/sku/title/qty/price/vat) — retry için.
    """
    __tablename__ = "shopify_orders"
    __table_args__ = (UniqueConstraint("store_key", "shopify_order_id",
                                       name="uq_shopify_orders_store_order"),)

    id = Column(Integer, primary_key=True, index=True)
    store_key = Column(String(20), nullable=False, index=True)     # minerva/evanira/serenida
    shopify_order_id = Column(BigInteger, nullable=False)
    order_number = Column(String(40), nullable=True)               # #1001 görünen no
    status = Column(String(30), nullable=False, default="received", index=True)
    step = Column(String(30), nullable=True)                       # stock/contact/invoice/payment/legalize
    # Stok düşümü YAPILDI mı — stok commit'iyle BİRLİKTE yazılır. Retry'da tek
    # güvenilir kaynak budur: hata sonrası rollback `step`'i geri sarabilir, bu
    # bayrak sarmaz → stok asla ikinci kez düşmez (regresyon testi mevcut).
    stock_applied = Column(Boolean, nullable=False, default=False)
    total = Column(Float, nullable=True)
    currency = Column(String(8), nullable=True)
    country = Column(String(8), nullable=True)                     # billing/shipping country_code
    customer_email = Column(String(150), nullable=True)
    customer_name = Column(String(150), nullable=True)
    vkn_tckn = Column(String(20), nullable=True)                   # varsa (B2B/TCKN)
    attempts = Column(Integer, nullable=False, default=0)
    last_error = Column(String(500), nullable=True)
    parasut_contact_id = Column(BigInteger, nullable=True)
    parasut_invoice_id = Column(BigInteger, nullable=True)
    parasut_doc_type = Column(String(20), nullable=True)           # e_archive | e_invoice
    parasut_doc_id = Column(BigInteger, nullable=True)
    trackable_job_id = Column(String(80), nullable=True)           # asenkron e-belge takibi
    lines_json = Column(Text, nullable=True)
    # Doğrulanmış-alıcı yorum daveti bu siparişten üretildi mi — idempotency
    # bayrağı (webhook redelivery / retry_order aynı siparişe iki davet üretmesin).
    invites_created = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


# ─── Ürün yorumları (Shopify mağaza vitrini) — sesli not destekli ───────────
# Cross-cutting (CRM/Drive/PDKS gibi): IMS domain kolonuyla İLGİSİZ — bu,
# Shopify ürünlerine (ims Item değil) bağlı bir mağaza-vitrini özelliği.
# Gerçeğin kaynağı burası; onay sonrası Shopify'a (metaobject + Files) itilir.
# Moderasyon burada tutulur çünkü: (a) sesli not için transkript zorunluluğu
# gibi kurallar burada uygulanır, (b) bir yorumu geri çekmek tek yerden olur,
# (c) mağaza vitrini IMS'e runtime bağımlı OLMAZ — burada TUTULAN veri, orada
# YAYINLANAN verinin kaynağıdır, ikisi aynı şey değildir.


class ReviewInvite(Base):
    """Tek kullanımlık yorum davet linki — hediye ürün ya da doğrulanmış
    alışveriş sonrası üretilir.  Token URL'de taşınır (`/yorum/{token}`),
    şifre yok — token'ın kendisi sır.  `used_at`, review INSERT'iyle AYNI
    transaction'da ve satır kilidiyle (`with_for_update`, bkz.
    core.shopify._decrement_stock aynı deseni kullanır) yazılır; iki eşzamanlı
    submit aynı davetten iki yorum üretemez."""
    __tablename__ = "review_invites"

    id = Column(Integer, primary_key=True, index=True)
    token = Column(String(64), nullable=False, unique=True, index=True)
    kind = Column(String(20), nullable=False)                      # gifted | verified_buyer
    store_key = Column(String(20), nullable=False, index=True)     # minerva/evanira/serenida
    shopify_product_id = Column(BigInteger, nullable=False)
    product_title = Column(String(255), nullable=True)
    product_handle = Column(String(255), nullable=True)
    shopify_order_id = Column(BigInteger, nullable=True)           # yalnız verified_buyer
    order_number = Column(String(40), nullable=True)
    recipient_email = Column(String(150), nullable=True)
    recipient_name = Column(String(150), nullable=True)
    locale = Column(String(10), nullable=False, default="tr")
    expires_at = Column(DateTime, nullable=False)
    used_at = Column(DateTime, nullable=True)
    created_by = Column(String(100), nullable=True)                # hediye daveti: personel kullanıcı adı; otomatikse NULL
    created_at = Column(DateTime, default=datetime.utcnow)


class ProductReview(Base):
    """Ürün yorumu — IMS'te toplanır/moderasyon edilir, onayda Shopify'a
    (metaobject + Files) itilir.  `publish_step`, itme akışının hangi adımda
    olduğunu tutar; `is_published`, etkiyle AYNI commit'te yazılan ayrı bir
    bayraktır (ShopifyOrder.stock_applied ile birebir aynı gerekçe: rollback
    `publish_step`'i geri sarabilir ama etkiyle birlikte commit'lenmiş bir
    bayrağı geri alamaz — bu olmadan yarıda kalan bir retry puanı iki kez
    sayardı).  `status` durumları: pending → approved → publishing →
    published; ayrıca rejected, unpublished, publish_failed, deleted."""
    __tablename__ = "product_reviews"

    id = Column(Integer, primary_key=True, index=True)
    invite_id = Column(Integer, ForeignKey("review_invites.id"), nullable=False, index=True)
    store_key = Column(String(20), nullable=False, index=True)
    shopify_product_id = Column(BigInteger, nullable=False, index=True)
    product_title = Column(String(255), nullable=True)

    source = Column(String(20), nullable=False)                    # verified_buyer | gifted
    author_name = Column(String(60), nullable=False)
    author_email = Column(String(150), nullable=True)
    rating = Column(Integer, nullable=False)
    body = Column(Text, nullable=True)
    locale = Column(String(10), nullable=False, default="tr")

    audio_stored_name = Column(String(80), nullable=True)          # core.reviews.save_audio üretir; disk dosya adı
    audio_mime = Column(String(60), nullable=True)
    audio_bytes = Column(Integer, nullable=True)
    audio_duration_s = Column(Integer, nullable=True)
    transcript = Column(Text, nullable=True)                       # ses varsa yayın için ZORUNLU (WCAG 1.2.1)

    consent_voice = Column(Boolean, nullable=False, default=False)
    consent_text_version = Column(String(20), nullable=True)
    consent_ip = Column(String(45), nullable=True)
    consent_at = Column(DateTime, nullable=True)

    status = Column(String(24), nullable=False, default="pending", index=True)
    publish_step = Column(String(30), nullable=True)                # file_staged/file_created/file_ready/metaobject/metafields
    is_published = Column(Boolean, nullable=False, default=False)   # bkz. sınıf docstring'i — etkiyle AYNI commit'te yazılır

    moderation_note = Column(Text, nullable=True)
    moderated_by = Column(String(100), nullable=True)
    moderated_at = Column(DateTime, nullable=True)

    shopify_metaobject_id = Column(String(80), nullable=True)
    shopify_file_id = Column(String(80), nullable=True)
    shopify_file_url = Column(String(500), nullable=True)
    publish_error = Column(String(500), nullable=True)
    publish_attempts = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    invite = relationship("ReviewInvite", foreign_keys=[invite_id])


# ─── PDKS — Personel Devam Takip Sistemi ─────────────────────────────────────
# Cross-cutting modül (CRM/Drive gibi): domain kolonu YOK — devam takibi kişiye
# aittir, Kozmetik/Supplement paneline değil.  Tüm DateTime'lar UTC; gün bazlı
# sorgular için TR-yerel work_date ayrıca yazılır ((utc + TR_OFFSET).date()).


class Employee(Base):
    """Personel kaydı — opsiyonel olarak bir User'a 1:1 bağlı (Distributor kalıbı).

    user_id NULL olabilir: çalışan IMS hesabı açılmadan önce de tanımlanabilir
    (yönetici manuel olay girebilir); kendi cihazından giriş/çıkış için bağ
    şarttır.  RBAC'taki 'Staff' rol etiketiyle ("Personel") İLGİSİZ — bu tablo
    puantajın öznesidir, login hesabı değil."""
    __tablename__ = "pdks_employees"

    id         = Column(Integer, primary_key=True, index=True)
    user_id    = Column(Integer, ForeignKey("users.id"), unique=True, nullable=True, index=True)
    full_name  = Column(String(150), nullable=False)
    title      = Column(String(100), nullable=True)      # görev/ünvan
    start_date = Column(Date, nullable=True)             # işe giriş (TR-yerel)
    # İşten ayrılış (TR-yerel, dahil).  Bu tarihten SONRAKİ günler puantajda
    # devamsızlık değil 'İşten ayrıldı'dır — is_active tek başına yetmiyordu:
    # kart pasifleşse bile aylık rapor ayın kalanını devamsız yazıyordu ve
    # ayrılan personelin son ay bordrosu yanlış çıkıyordu (bkz. core/pdks.py).
    end_date   = Column(Date, nullable=True)
    notes      = Column(Text, nullable=True)
    is_active  = Column(Boolean, default=True)           # soft delete (işten ayrılan)
    created_at = Column(DateTime, default=datetime.utcnow)

    user = relationship("User", foreign_keys=[user_id])


class EmployeeSchedule(Base):
    """Personelin haftalık çalışma programı — versiyonlu (effective_from).

    Puantaj CANLI hesaplanır; ay ortası program değişikliği geçmiş günlerin
    mesaisini bozmasın diye her değişiklik YENİ satır olarak eklenir, eski
    versiyonlar asla değiştirilmez.  weekly_template JSON: anahtar
    date.weekday() (str "0"=Pazartesi … "6"=Pazar), değer
    {"start": "09:00", "end": "18:00"} ya da null (tatil günü)."""
    __tablename__ = "pdks_schedules"
    __table_args__ = (UniqueConstraint("employee_id", "effective_from",
                                       name="uq_pdks_schedule_emp_from"),)

    id                  = Column(Integer, primary_key=True, index=True)
    employee_id         = Column(Integer, ForeignKey("pdks_employees.id"), nullable=False, index=True)
    effective_from      = Column(Date, nullable=False)   # TR-yerel
    weekly_template     = Column(Text, nullable=False)   # JSON — üstteki şema
    lunch_break_minutes = Column(Integer, nullable=False, default=60)
    created_by          = Column(String(100), nullable=True)   # actor snapshot
    created_at          = Column(DateTime, default=datetime.utcnow)


class AttendanceEvent(Base):
    """Tek giriş/çıkış olayı — gün satırı değil OLAY satırı.

    Çoklu giriş/çıkış (öğle arası), unutulan çıkış (açık çift) ve olay bazlı
    düzeltme izi bu modeli gerektirir.  ts_utc self olaylarda DAİMA sunucu
    saatidir (istemci saatine güvenilmez).  work_date TR-yerel gündür:
    'in' → kendi TR günü; 'out' → kapattığı açık 'in' <16 saatlikse ONUN
    work_date'i (gece yarısını aşan vardiya doğru güne yazılır)."""
    __tablename__ = "pdks_events"
    __table_args__ = (Index("ix_pdks_events_emp_date", "employee_id", "work_date"),)

    id                 = Column(Integer, primary_key=True, index=True)
    employee_id        = Column(Integer, ForeignKey("pdks_employees.id"), nullable=False, index=True)
    event_type         = Column(String(10), nullable=False)        # 'in' | 'out'
    ts_utc             = Column(DateTime, nullable=False)          # sunucu saati (UTC)
    work_date          = Column(Date, nullable=False, index=True)  # TR-yerel gün
    source             = Column(String(20), nullable=False, default="self")  # self | manual
    created_by_user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    ip_address         = Column(String(64), nullable=True)
    # Üçlü doğrulama (ofis ağı + konum + canlı QR) açıkken self olaylarda
    # doldurulur; manuel olaylarda ve doğrulama kapalıyken NULL kalabilir.
    geo_lat            = Column(Float, nullable=True)
    geo_lon            = Column(Float, nullable=True)
    geo_accuracy_m     = Column(Float, nullable=True)
    # Hangi yolla doğrulandı: 'qr' (kiosk QR) | 'code' (kiosk sayısal kod)
    # | 'static' (basılı afiş) | 'static_fallback' (rotating moddayken afiş)
    # | 'off' (doğrulama kapalıydı).  Kesinti sonrası "kim nasıl imza attı"
    # sorusu adli inceleme gerektirmesin diye tutuluyor.
    verify_method      = Column(String(16), nullable=True)
    corrected_by       = Column(String(100), nullable=True)
    corrected_at       = Column(DateTime, nullable=True)
    correction_note    = Column(String(300), nullable=True)        # PUT/DELETE'te zorunlu
    is_active          = Column(Boolean, default=True)             # soft delete — iz kalır
    created_at         = Column(DateTime, default=datetime.utcnow)


class LeaveRecord(Base):
    """İzin kaydı — KAPSAYICI tarih aralığı (start_date..end_date, TR-yerel).

    leave_type: yillik | raporlu | ucretsiz | diger ('diger'de note zorunlu).
    Buradaki 'izin' devamsızlık mazeretidir (yıllık izin, rapor…) — RBAC
    yetki ('permission') kavramıyla karıştırma; RBAC kategorisi 'pdks'."""
    __tablename__ = "pdks_leaves"

    id          = Column(Integer, primary_key=True, index=True)
    employee_id = Column(Integer, ForeignKey("pdks_employees.id"), nullable=False, index=True)
    leave_type  = Column(String(20), nullable=False)
    start_date  = Column(Date, nullable=False)
    end_date    = Column(Date, nullable=False)
    note        = Column(String(300), nullable=True)
    # e-rapor / istirahat belgesi no — muhasebecinin SGK listesiyle
    # eşleştirdiği anahtar.  Belgenin kendisi Drive'da durur.
    document_no = Column(String(60), nullable=True)
    created_by  = Column(String(100), nullable=True)
    is_active   = Column(Boolean, default=True)
    created_at  = Column(DateTime, default=datetime.utcnow)


class PublicHoliday(Base):
    """Resmi tatil — herkes için ortak (employee_id yok).  Arefe → is_half_day.

    Soft delete (is_active): tatil silmek geçmiş puantajı geriye dönük
    değiştirir; iz kalsın ve geri alınabilsin diye satır silinmez.  Aynı
    tarihe yeniden ekleme mümkün olsun diye DB-unique yok — tekillik aktif
    satırlar arasında router'da doğrulanır."""
    __tablename__ = "pdks_holidays"

    id           = Column(Integer, primary_key=True, index=True)
    holiday_date = Column(Date, nullable=False, index=True)
    name         = Column(String(150), nullable=False)
    is_half_day  = Column(Boolean, default=False)
    created_by   = Column(String(100), nullable=True)
    is_active    = Column(Boolean, default=True)
    created_at   = Column(DateTime, default=datetime.utcnow)


class AttendanceRequest(Base):
    """Personelin "giriş/çıkış yapmayı unuttum" bildirimi — YÖNETİCİ ONAYLI.

    Doğrulama (ofis ağı + konum + QR) açıkken personel ofis dışından imza
    atamaz; bu kasıtlıdır.  Unutulan çıkış için tek yol yöneticinin manuel
    düzeltmesiydi — bu tablo o talebi personelin kendisinin başlatmasını
    sağlar: saat + gerekçe bildirilir, yönetici onaylayınca GERÇEK
    AttendanceEvent (source='request') yazılır.  Onaysız hiçbir puantaj
    etkisi YOKTUR — güvenlik zayıflamaz, yalnız yöneticinin işi kolaylaşır."""
    __tablename__ = "pdks_requests"

    id            = Column(Integer, primary_key=True, index=True)
    employee_id   = Column(Integer, ForeignKey("pdks_employees.id"), nullable=False, index=True)
    event_type    = Column(String(10), nullable=False)        # 'in' | 'out'
    ts_utc        = Column(DateTime, nullable=False)          # bildirilen an (UTC)
    work_date     = Column(Date, nullable=False, index=True)  # TR-yerel gün
    note          = Column(String(300), nullable=False)       # personelin gerekçesi
    status        = Column(String(12), nullable=False, default="pending", index=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    decided_by    = Column(String(100), nullable=True)
    decided_at    = Column(DateTime, nullable=True)
    decision_note = Column(String(300), nullable=True)
    event_id      = Column(Integer, ForeignKey("pdks_events.id"), nullable=True)  # onayda oluşan olay


class LeaveRequest(Base):
    """Personelin "izin/rapor bildirimi" — YÖNETİCİ ONAYLI.

    Doğrulama (ofis ağı + konum + QR) açıkken personel ofis dışından imza
    atamaz; hastalanan biri de evden izin giremezdi çünkü `POST /leaves`
    `pdks.manage` istiyor.  Bu tablo talebi personelin kendisinin
    başlatmasını sağlar: tür + tarih aralığı + gerekçe (+ e-rapor belge no)
    bildirilir, yönetici onaylayınca GERÇEK `LeaveRecord` yazılır.
    Onaysız hiçbir puantaj etkisi YOKTUR.

    AttendanceRequest'ten farkı: bu bir ARALIK (start..end, kapsayıcı) ve
    zaman iki yöne akar — yıllık izin gelecek, rapor geçmiş tarihlidir."""
    __tablename__ = "pdks_leave_requests"

    id            = Column(Integer, primary_key=True, index=True)
    employee_id   = Column(Integer, ForeignKey("pdks_employees.id"), nullable=False, index=True)
    leave_type    = Column(String(20), nullable=False)   # yillik|raporlu|ucretsiz|diger
    start_date    = Column(Date, nullable=False, index=True)
    end_date      = Column(Date, nullable=False)
    note          = Column(String(300), nullable=False)  # personelin gerekçesi (zorunlu)
    document_no   = Column(String(60), nullable=True)    # e-rapor / belge no
    status        = Column(String(12), nullable=False, default="pending", index=True)
    created_at    = Column(DateTime, default=datetime.utcnow)
    decided_by    = Column(String(100), nullable=True)
    decided_at    = Column(DateTime, nullable=True)
    decision_note = Column(String(300), nullable=True)
    leave_id      = Column(Integer, ForeignKey("pdks_leaves.id"), nullable=True)


# ═══════════════════════════════════════════════════════════════════════════
# Influencer / Creator programı — cross-cutting modül (domain kolonu YOK;
# CRM/Drive/PDKS emsali).  Tablolar `influencer_*`; create_all ile gelir,
# migration `b5d7f9a1c3e5` alembic geçmişi + temiz kurulum içindir.
#
# DEFTER KURALI: bu modülde stok hareketi YOKTUR — ürün gönderimi mevcut
# `Delivery` (kargo) üzerinden yapılır, `InfluencerShipment.delivery_id`
# yalnız bağdır.  Komisyon HESABI IMS'te yapılmaz (UpPromote'tan okunur).
# Kişisel veri: T.C. kimlik no ve IBAN TUTULMAZ (bkz. InfluencerCreator /
# InfluencerPayout docstring'leri).
# ═══════════════════════════════════════════════════════════════════════════


class InfluencerTier(Base):
    """Kademe merdiveni (nano/mikro/orta/makro/ambassador) — takipçi aralığı,
    komisyon, müşteri indirimi, hediye ürün adedi, lansman erişimi, kod hakkı.
    `init_db()` boşsa `core.influencer.DEFAULT_TIERS` ile seed'ler; sonra
    ayarlar ekranından düzenlenir.  `ambassador` takipçiye bakmaz, elle atanır."""
    __tablename__ = "influencer_tier"

    id                    = Column(Integer, primary_key=True, index=True)
    key                   = Column(String(20), nullable=False, unique=True, index=True)
    label                 = Column(String(60), nullable=False)
    min_followers         = Column(Integer, nullable=False, default=0)
    max_followers         = Column(Integer, nullable=True)            # NULL = üst sınır yok
    commission_pct        = Column(Float, nullable=False, default=10.0)
    customer_discount_pct = Column(Float, nullable=False, default=0.0)
    max_gift_items        = Column(Integer, nullable=False, default=1)
    launch_access         = Column(Boolean, nullable=False, default=False)
    code_allowed          = Column(Boolean, nullable=False, default=False)
    sort_order            = Column(Integer, nullable=False, default=0)
    is_active             = Column(Boolean, nullable=False, default=True)
    updated_at            = Column(DateTime, nullable=True)
    updated_by            = Column(String(100), nullable=True)


class InfluencerBenchmark(Base):
    """Platform × kademe × metrik için "sağlıklı aralık" (low–high).  Skor
    motoru (`core.influencer`) hesabı bu satırlara göre normalize eder.
    Seed: `DEFAULT_BENCHMARKS`.  metric ∈ er_follower | er_view |
    view_per_follower | comment_like_ratio | geo_target_pct | comment_quality_pct."""
    __tablename__ = "influencer_benchmark"
    __table_args__ = (
        UniqueConstraint("platform", "tier_key", "metric", name="uq_inf_benchmark_platform_tier_metric"),
    )

    id         = Column(Integer, primary_key=True, index=True)
    platform   = Column(String(20), nullable=False)       # instagram | tiktok | youtube
    tier_key   = Column(String(20), nullable=False)       # nano | mikro | orta | makro
    metric     = Column(String(30), nullable=False)
    low        = Column(Float, nullable=False, default=0.0)
    high       = Column(Float, nullable=False, default=0.0)
    updated_at = Column(DateTime, nullable=True)
    updated_by = Column(String(100), nullable=True)


class InfluencerCreator(Base):
    """Creator kartı — programın ana öznesi (CrmContact'tan AYRI).

    `slug` UNIQUE: utm_campaign + indirim kodu tabanı.  `birth_year` 18+
    kontrolü içindir; **T.C. kimlik no TUTULMAZ** (vergi belgesi gerekirse
    dosya olarak `influencer_file` kind='tax_doc').  `tier_key` motorun
    hesapladığı kademe, `tier_override_key` yöneticinin elle atadığı
    (ambassador buradan).  `relationship_stage`: havuz / basvurdu / seeded /
    affiliate / ambassador / pasif / kara_liste."""
    __tablename__ = "influencer_creator"

    id                  = Column(Integer, primary_key=True, index=True)
    slug                = Column(String(60), nullable=False, unique=True, index=True)
    full_name           = Column(String(150), nullable=False, index=True)
    email               = Column(String(150), nullable=True, index=True)
    phone               = Column(String(50), nullable=True)
    country             = Column(String(60), nullable=True)
    city                = Column(String(100), nullable=True)
    language            = Column(String(10), nullable=True)           # tr / en / de …
    birth_year          = Column(Integer, nullable=True)
    categories_json     = Column(Text, nullable=True)                 # ["cilt bakımı","vegan",…]
    skin_type           = Column(String(40), nullable=True)           # cilt tipi
    clothing_size       = Column(String(20), nullable=True)           # beden
    allergies           = Column(Text, nullable=True)
    accepted_model      = Column(String(20), nullable=True)           # barter / kod / karma
    tier_key            = Column(String(20), nullable=True, index=True)
    tier_override_key   = Column(String(20), nullable=True)
    relationship_stage  = Column(String(20), nullable=False, default="havuz", index=True)
    authenticity_score  = Column(Float, nullable=True)                # 0–100 (motor)
    score_json          = Column(Text, nullable=True)                 # bileşen kırılımı
    verification_level  = Column(String(4), nullable=True)            # K1 / K2 / K3
    aqs                 = Column(Float, nullable=True)                # audience quality score
    fake_pct            = Column(Float, nullable=True)                # tahmini sahte takipçi %
    do_not_resend       = Column(Boolean, nullable=False, default=False)
    rating              = Column(Integer, nullable=True)              # 1–5 ekip puanı
    owner_user_id       = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    owner_name          = Column(String(100), nullable=True)
    source              = Column(String(50), nullable=True)           # basvuru / manual / import / referral
    notes               = Column(Text, nullable=True)
    is_active           = Column(Boolean, nullable=False, default=True)
    created_at          = Column(DateTime, default=datetime.utcnow)
    created_by          = Column(String(100), nullable=True)
    updated_at          = Column(DateTime, nullable=True)

    owner = relationship("User", foreign_keys=[owner_user_id])


class InfluencerAccount(Base):
    """Creator'ın bir platformdaki hesabı (IG/TikTok/YouTube) + son metrikler.
    Metrik alanları en son snapshot'ın özetidir (seri `influencer_metric_snapshot`).
    `metrics_source`: manual / api / oauth.  `oauth_token_enc` Faz 4 (şifreli)."""
    __tablename__ = "influencer_account"
    __table_args__ = (
        UniqueConstraint("platform", "handle", name="uq_inf_account_platform_handle"),
    )

    id                       = Column(Integer, primary_key=True, index=True)
    creator_id               = Column(Integer, ForeignKey("influencer_creator.id", ondelete="CASCADE"), nullable=False, index=True)
    platform                 = Column(String(20), nullable=False)     # instagram | tiktok | youtube
    handle                   = Column(String(120), nullable=False)
    external_id              = Column(String(80), nullable=True)      # YT channel id / IG user id
    followers                = Column(Integer, nullable=True)
    following                = Column(Integer, nullable=True)
    posts_count              = Column(Integer, nullable=True)
    hidden_subscriber_count  = Column(Boolean, nullable=False, default=False)
    avg_views                = Column(Float, nullable=True)
    avg_likes                = Column(Float, nullable=True)
    avg_comments             = Column(Float, nullable=True)
    er_follower              = Column(Float, nullable=True)           # % — etkileşim / takipçi
    er_view                  = Column(Float, nullable=True)           # % — etkileşim / görüntülenme
    view_per_follower        = Column(Float, nullable=True)           # %
    views_cv                 = Column(Float, nullable=True)           # görüntülenme değişkenlik katsayısı
    audience_geo_json        = Column(Text, nullable=True)
    audience_age_gender_json = Column(Text, nullable=True)
    metrics_source           = Column(String(20), nullable=False, default="manual")
    metrics_at               = Column(DateTime, nullable=True)
    oauth_token_enc          = Column(Text, nullable=True)
    is_primary               = Column(Boolean, nullable=False, default=False)
    created_at               = Column(DateTime, default=datetime.utcnow)
    updated_at               = Column(DateTime, nullable=True)

    creator = relationship("InfluencerCreator", foreign_keys=[creator_id])


class InfluencerMetricSnapshot(Base):
    """Hesap metrik anlık görüntüsü — büyüme serisi buradan çizilir.
    `posts_json` son 12/30 gönderinin ham sayıları, `computed_json` motorun
    türettiği oranlar."""
    __tablename__ = "influencer_metric_snapshot"

    id            = Column(Integer, primary_key=True, index=True)
    account_id    = Column(Integer, ForeignKey("influencer_account.id", ondelete="CASCADE"), nullable=False, index=True)
    taken_at      = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    source        = Column(String(20), nullable=False, default="manual")   # manual / api / oauth
    followers     = Column(Integer, nullable=True)
    posts_json    = Column(Text, nullable=True)
    computed_json = Column(Text, nullable=True)
    entered_by    = Column(String(100), nullable=True)


class InfluencerApplication(Base):
    """Halka açık başvuru formu kaydı (`/basvuru`, anonim).  Onaylanınca
    `creator_id` ile creator kartına bağlanır.  KVKK onayı sürüm + zaman + IP
    olarak tutulur; `honeypot_hit` bot tuzağına takılan gönderim (listede
    varsayılan gizli)."""
    __tablename__ = "influencer_application"

    id                   = Column(Integer, primary_key=True, index=True)
    store_key            = Column(String(20), nullable=False, index=True)   # minerva/evanira/serenida
    full_name            = Column(String(150), nullable=False)
    email                = Column(String(150), nullable=False, index=True)
    phone                = Column(String(50), nullable=True)
    country              = Column(String(60), nullable=True)
    city                 = Column(String(100), nullable=True)
    language             = Column(String(10), nullable=True)
    birth_year           = Column(Integer, nullable=True)
    accounts_json        = Column(Text, nullable=True)      # [{platform, handle, followers}, …]
    form_json            = Column(Text, nullable=True)      # serbest form alanları
    status               = Column(String(12), nullable=False, default="pending", index=True)  # pending/maybe/approved/rejected
    reject_reason        = Column(Text, nullable=True)
    reviewed_by          = Column(String(100), nullable=True)
    reviewed_at          = Column(DateTime, nullable=True)
    creator_id           = Column(Integer, ForeignKey("influencer_creator.id", ondelete="SET NULL"), nullable=True, index=True)
    consent_text_version = Column(String(20), nullable=True)
    consent_at           = Column(DateTime, nullable=True)
    consent_ip           = Column(String(64), nullable=True)
    honeypot_hit         = Column(Boolean, nullable=False, default=False)
    user_agent           = Column(String(300), nullable=True)
    created_at           = Column(DateTime, default=datetime.utcnow, index=True)


class InfluencerAddress(Base):
    """Gönderim adresi — creator claim-link ile kendisi girer (`verified_at/ip`).
    Delivery'ye kopyalanır; burada yalnız güncel adres defteri tutulur."""
    __tablename__ = "influencer_address"

    id             = Column(Integer, primary_key=True, index=True)
    creator_id     = Column(Integer, ForeignKey("influencer_creator.id", ondelete="CASCADE"), nullable=False, index=True)
    label          = Column(String(60), nullable=True)           # ev / iş
    recipient_name = Column(String(150), nullable=True)
    line1          = Column(String(200), nullable=True)
    line2          = Column(String(200), nullable=True)
    district       = Column(String(100), nullable=True)
    city           = Column(String(100), nullable=True)
    postal_code    = Column(String(20), nullable=True)
    country        = Column(String(60), nullable=True)
    phone          = Column(String(50), nullable=True)
    is_default     = Column(Boolean, nullable=False, default=False)
    verified_at    = Column(DateTime, nullable=True)
    verified_ip    = Column(String(64), nullable=True)
    created_at     = Column(DateTime, default=datetime.utcnow)


class InfluencerCampaign(Base):
    """Kampanya / lansman — iş birliklerini gruplar; brief, hashtag'ler, claim
    listesi ve varsayılan teslimatlar buradan miras alınır."""
    __tablename__ = "influencer_campaign"

    id                        = Column(Integer, primary_key=True, index=True)
    name                      = Column(String(150), nullable=False)
    store_key                 = Column(String(20), nullable=False, index=True)
    market                    = Column(String(10), nullable=True)        # TR / US / UK / DE / EU
    start_on                  = Column(Date, nullable=True)
    end_on                    = Column(Date, nullable=True)
    hashtags                  = Column(String(300), nullable=True)
    brief_text                = Column(Text, nullable=True)
    claim_sheet_json          = Column(Text, nullable=True)
    default_deliverables_json = Column(Text, nullable=True)
    status                    = Column(String(20), nullable=False, default="taslak", index=True)  # taslak/aktif/kapandi
    is_active                 = Column(Boolean, nullable=False, default=True)
    created_at                = Column(DateTime, default=datetime.utcnow)
    created_by                = Column(String(100), nullable=True)
    updated_at                = Column(DateTime, nullable=True)


class InfluencerCollab(Base):
    """İş birliği (tek creator × tek mağaza × tek dönem) — Kanban'ın kartı.

    `code` INF-YYYY-NNNNN.  `stage` statü makinesi `core.influencer.STAGES` /
    `TRANSITIONS` ile doğrulanır; her geçiş `influencer_collab_stage_log`'a
    yazılır.  `model`: barter / kod / karma.  İptalde `cancel_reason` ZORUNLU.
    `decision` (devam / kod ver / ambassador / durdur) motorun
    `decision_suggested_json` önerisi üzerine yöneticinin verdiği karar."""
    __tablename__ = "influencer_collab"

    id                      = Column(Integer, primary_key=True, index=True)
    code                    = Column(String(20), nullable=False, unique=True, index=True)
    creator_id              = Column(Integer, ForeignKey("influencer_creator.id", ondelete="CASCADE"), nullable=False, index=True)
    campaign_id             = Column(Integer, ForeignKey("influencer_campaign.id", ondelete="SET NULL"), nullable=True, index=True)
    store_key               = Column(String(20), nullable=False, index=True)
    market                  = Column(String(10), nullable=True)
    model                   = Column(String(10), nullable=False, default="barter")
    tier_key_at_start       = Column(String(20), nullable=True)
    stage                   = Column(String(30), nullable=False, default="teklif_gonderildi", index=True)
    stage_changed_at        = Column(DateTime, nullable=True, default=datetime.utcnow)
    offered_at              = Column(DateTime, nullable=True)
    accepted_at             = Column(DateTime, nullable=True)
    content_due_on          = Column(Date, nullable=True)
    usage_rights            = Column(String(60), nullable=True)          # organik / reklam 6 ay …
    exclusivity_days        = Column(Integer, nullable=False, default=0)
    guideline_ack_at        = Column(DateTime, nullable=True)
    guideline_version       = Column(String(20), nullable=True)
    guideline_ack_ip        = Column(String(64), nullable=True)
    deliverables_json       = Column(Text, nullable=True)                # [{platform,type,qty}, …]
    cancel_reason           = Column(Text, nullable=True)
    no_post_flagged_at      = Column(DateTime, nullable=True)
    decision                = Column(String(20), nullable=True)
    decision_suggested_json = Column(Text, nullable=True)
    owner_user_id           = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    owner_name              = Column(String(100), nullable=True)
    notes                   = Column(Text, nullable=True)
    is_active               = Column(Boolean, nullable=False, default=True)
    created_at              = Column(DateTime, default=datetime.utcnow)
    created_by              = Column(String(100), nullable=True)
    updated_at              = Column(DateTime, nullable=True)

    creator  = relationship("InfluencerCreator",  foreign_keys=[creator_id])
    campaign = relationship("InfluencerCampaign", foreign_keys=[campaign_id])


class InfluencerCollabStageLog(Base):
    """İş birliği aşama geçiş günlüğü — hangi aşamadan hangisine, kim, neden."""
    __tablename__ = "influencer_collab_stage_log"

    id         = Column(Integer, primary_key=True, index=True)
    collab_id  = Column(Integer, ForeignKey("influencer_collab.id", ondelete="CASCADE"), nullable=False, index=True)
    from_stage = Column(String(30), nullable=True)
    to_stage   = Column(String(30), nullable=False)
    reason     = Column(Text, nullable=True)
    actor      = Column(String(100), nullable=True)
    at         = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)


class InfluencerToken(Base):
    """Tek kullanımlık creator linki (ReviewInvite kalıbı): purpose ∈ address /
    insights / agreement / draft_upload.  `used_at` satır kilidiyle
    (`with_for_update`) yazılır — iki eşzamanlı submit aynı token'ı iki kez
    kullanamaz."""
    __tablename__ = "influencer_token"

    id         = Column(Integer, primary_key=True, index=True)
    token      = Column(String(64), nullable=False, unique=True, index=True)
    purpose    = Column(String(20), nullable=False)
    creator_id = Column(Integer, ForeignKey("influencer_creator.id", ondelete="CASCADE"), nullable=True, index=True)
    collab_id  = Column(Integer, ForeignKey("influencer_collab.id", ondelete="SET NULL"), nullable=True, index=True)
    expires_at = Column(DateTime, nullable=False)
    used_at    = Column(DateTime, nullable=True)
    created_by = Column(String(100), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class InfluencerActivity(Base):
    """Creator / iş birliği zaman çizelgesi (CrmActivity kalıbı; CRM
    timeline'ından AYRI).  type: note | dm | email | call | system."""
    __tablename__ = "influencer_activity"

    id             = Column(Integer, primary_key=True, index=True)
    creator_id     = Column(Integer, ForeignKey("influencer_creator.id", ondelete="CASCADE"), nullable=True, index=True)
    collab_id      = Column(Integer, ForeignKey("influencer_collab.id",  ondelete="CASCADE"), nullable=True, index=True)
    type           = Column(String(20), nullable=False, default="note")
    subject        = Column(String(200), nullable=True)
    body           = Column(Text, nullable=True)
    author_user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    author_name    = Column(String(100), nullable=True)
    is_pinned      = Column(Boolean, nullable=False, default=False)
    created_at     = Column(DateTime, default=datetime.utcnow, index=True)


class InfluencerFile(Base):
    """Program dosyası (Insights videosu/ekran görüntüsü, sözleşme, vergi
    belgesi, taslak içerik) — fiziksel dosya Drive diskinde (`stored_name`,
    `core.drive.save_upload` kalıbı; `core/influencer_files.save_media`).
    entity: creator | collab | content | payout | application."""
    __tablename__ = "influencer_file"

    id            = Column(Integer, primary_key=True, index=True)
    entity        = Column(String(20), nullable=False, index=True)
    entity_id     = Column(Integer, nullable=False, index=True)
    kind          = Column(String(20), nullable=False)   # insights_video/screenshot/agreement/tax_doc/draft
    original_name = Column(String(255), nullable=False)
    stored_name   = Column(String(80), nullable=False)
    size_bytes    = Column(Integer, nullable=False, default=0)
    content_type  = Column(String(120), nullable=True)
    uploaded_by   = Column(String(100), nullable=True)
    created_at    = Column(DateTime, default=datetime.utcnow)


class InfluencerShipment(Base):
    """Ürün gönderimi — STOK YOLU DELIVERY'DEDİR, burada yeni stok hareketi
    YOK.  `delivery_id` → `deliveries.id` UNIQUE (bir teslimat tek gönderim).
    Maliyet alanları o anki snapshot: `cogs_total` = Σ adet × Item.cost_price,
    `loaded_cost` = cogs + paketleme + kargo (kırılma/ROI matrahı).
    `reminder1/2_*` "ürün ulaştı, içerik bekleniyor" hatırlatmaları — mevcut
    CRM görev taramasından push'lanır (crm_task.influencer_collab_id)."""
    __tablename__ = "influencer_shipment"

    id                = Column(Integer, primary_key=True, index=True)
    collab_id         = Column(Integer, ForeignKey("influencer_collab.id", ondelete="CASCADE"), nullable=False, index=True)
    delivery_id       = Column(Integer, ForeignKey("deliveries.id"), nullable=True, unique=True, index=True)
    address_id        = Column(Integer, ForeignKey("influencer_address.id", ondelete="SET NULL"), nullable=True)
    cogs_total        = Column(Float, nullable=False, default=0.0)
    packaging_cost    = Column(Float, nullable=False, default=0.0)
    shipping_cost     = Column(Float, nullable=False, default=0.0)
    loaded_cost       = Column(Float, nullable=False, default=0.0)
    handwritten_note  = Column(Text, nullable=True)
    shipped_at        = Column(DateTime, nullable=True)
    delivered_at      = Column(DateTime, nullable=True)                 # elle işaretlenir
    reminder1_due     = Column(Date, nullable=True)
    reminder2_due     = Column(Date, nullable=True)
    reminder1_task_id = Column(Integer, ForeignKey("crm_task.id", ondelete="SET NULL"), nullable=True)
    reminder2_task_id = Column(Integer, ForeignKey("crm_task.id", ondelete="SET NULL"), nullable=True)
    created_at        = Column(DateTime, default=datetime.utcnow)
    created_by        = Column(String(100), nullable=True)

    collab   = relationship("InfluencerCollab", foreign_keys=[collab_id])
    delivery = relationship("Delivery", foreign_keys=[delivery_id])


class InfluencerContent(Base):
    """Teslim edilen içerik (reel/story/post/tiktok/short/video/ugc).
    status: taslak → revizyon → onaylandi → yayinlandi.  Yayın sonrası
    `compliance_json` (etiket/hashtag/yasaklı kelime kontrolü) + skor,
    D7/D30 metrikleri elle/API ile girilir."""
    __tablename__ = "influencer_content"

    id               = Column(Integer, primary_key=True, index=True)
    collab_id        = Column(Integer, ForeignKey("influencer_collab.id", ondelete="CASCADE"), nullable=False, index=True)
    platform         = Column(String(20), nullable=True)
    type             = Column(String(20), nullable=True)
    url              = Column(String(500), nullable=True)
    status           = Column(String(20), nullable=False, default="taslak", index=True)
    draft_file_id    = Column(Integer, ForeignKey("influencer_file.id", ondelete="SET NULL"), nullable=True)
    caption          = Column(Text, nullable=True)
    review_note      = Column(Text, nullable=True)
    approved_by      = Column(String(100), nullable=True)
    approved_at      = Column(DateTime, nullable=True)
    published_at     = Column(DateTime, nullable=True)
    metrics_d7_json  = Column(Text, nullable=True)
    metrics_d30_json = Column(Text, nullable=True)
    compliance_json  = Column(Text, nullable=True)
    compliance_score = Column(Float, nullable=True)
    created_at       = Column(DateTime, default=datetime.utcnow)
    created_by       = Column(String(100), nullable=True)
    updated_at       = Column(DateTime, nullable=True)


class InfluencerAffiliate(Base):
    """UpPromote affiliate eşlemesi — link/kod altyapısı UpPromote'tadır, IMS
    yalnız eşler.  `affiliate_email` normalize (küçük harf) eşleme anahtarı,
    `sca_ref` UpPromote link kodu — webhook/sipariş eşleşmesinin anahtarı.
    `creator_id` NULL = UpPromote'ta var ama creator'a bağlanmamış
    (`/affiliates/unmatched`)."""
    __tablename__ = "influencer_affiliate"
    __table_args__ = (
        UniqueConstraint("store_key", "affiliate_email", name="uq_inf_affiliate_store_email"),
        UniqueConstraint("store_key", "sca_ref",         name="uq_inf_affiliate_store_sca_ref"),
    )

    id                     = Column(Integer, primary_key=True, index=True)
    creator_id             = Column(Integer, ForeignKey("influencer_creator.id", ondelete="SET NULL"), nullable=True, index=True)
    store_key              = Column(String(20), nullable=False, index=True)
    uppromote_affiliate_id = Column(BigInteger, nullable=True)
    affiliate_email        = Column(String(150), nullable=False)
    sca_ref                = Column(String(40), nullable=True)
    affiliate_link         = Column(String(500), nullable=True)
    coupon_code            = Column(String(60), nullable=True)
    program                = Column(String(60), nullable=True)            # "Creator %10"
    commission_pct         = Column(Float, nullable=False, default=10.0)
    status                 = Column(String(12), nullable=False, default="bekliyor", index=True)  # bekliyor/aktif/pasif
    linked_at              = Column(DateTime, nullable=True)
    synced_at              = Column(DateTime, nullable=True)
    created_at             = Column(DateTime, default=datetime.utcnow)

    creator = relationship("InfluencerCreator", foreign_keys=[creator_id])


class InfluencerReferral(Base):
    """UpPromote referral siparişi — komisyon HESABI IMS'te YAPILMAZ,
    UpPromote'un değeri okunur (`commission_amount/status`); IMS raporlar.
    `shopify_order_row_id` webhook'tan eşleşirse `shopify_orders.id`.
    source: uppromote_api / order_marker / csv / manual."""
    __tablename__ = "influencer_referral"
    __table_args__ = (
        UniqueConstraint("store_key", "shopify_order_id", name="uq_inf_referral_store_order"),
    )

    id                   = Column(Integer, primary_key=True, index=True)
    shopify_order_row_id = Column(Integer, ForeignKey("shopify_orders.id", ondelete="SET NULL"), nullable=True)
    store_key            = Column(String(20), nullable=False, index=True)
    shopify_order_id     = Column(BigInteger, nullable=False)
    order_number         = Column(String(40), nullable=True)
    creator_id           = Column(Integer, ForeignKey("influencer_creator.id",   ondelete="SET NULL"), nullable=True, index=True)
    affiliate_id         = Column(Integer, ForeignKey("influencer_affiliate.id", ondelete="SET NULL"), nullable=True, index=True)
    collab_id            = Column(Integer, ForeignKey("influencer_collab.id",    ondelete="SET NULL"), nullable=True, index=True)
    source               = Column(String(20), nullable=False, default="uppromote_api")
    order_total          = Column(Float, nullable=False, default=0.0)
    commission_amount    = Column(Float, nullable=False, default=0.0)
    commission_status    = Column(String(12), nullable=False, default="pending", index=True)  # pending/approved/paid/declined
    is_new_customer      = Column(Boolean, nullable=True)
    refunded_total       = Column(Float, nullable=False, default=0.0)
    order_at             = Column(DateTime, nullable=True, index=True)
    imported_at          = Column(DateTime, default=datetime.utcnow)


class InfluencerPayout(Base):
    """IMS tarafı ödeme belge kaydı — ödemenin kendisi UpPromote/banka
    tarafındadır.  `period` 'YYYY-MM'.  status: uppromote_talep → onaylandi
    → odendi.  `tax_doc_type`: istisna_20b / fatura / gider_pusulasi /
    yurtdisi (odendi için zorunlu).  **IBAN TUTULMAZ** — `bank_note` yalnız
    dekont referansı."""
    __tablename__ = "influencer_payout"
    __table_args__ = (
        UniqueConstraint("creator_id", "period", "store_key", name="uq_inf_payout_creator_period_store"),
    )

    id                   = Column(Integer, primary_key=True, index=True)
    creator_id           = Column(Integer, ForeignKey("influencer_creator.id", ondelete="CASCADE"), nullable=False, index=True)
    period               = Column(String(7), nullable=False, index=True)
    store_key            = Column(String(20), nullable=True)
    total                = Column(Float, nullable=False, default=0.0)
    currency             = Column(String(3), nullable=False, default="TRY")
    status               = Column(String(20), nullable=False, default="uppromote_talep", index=True)
    uppromote_payment_id = Column(String(60), nullable=True)
    tax_doc_type         = Column(String(20), nullable=True)
    tax_doc_no           = Column(String(60), nullable=True)
    withholding_pct      = Column(Float, nullable=False, default=0.0)
    bank_note            = Column(String(200), nullable=True)
    paid_at              = Column(DateTime, nullable=True)
    paid_by              = Column(String(100), nullable=True)
    doc_file_id          = Column(Integer, ForeignKey("influencer_file.id", ondelete="SET NULL"), nullable=True)
    notes                = Column(Text, nullable=True)
    created_at           = Column(DateTime, default=datetime.utcnow)
    created_by           = Column(String(100), nullable=True)

    creator = relationship("InfluencerCreator", foreign_keys=[creator_id])


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


def _backfill_retention_needs_review():
    """Sayım aktarımında belirsiz kalan kayıtları `needs_review` ile işaretle.

    BİR KEZ çalışır — `AppSetting` sentinel'i ile korunur.  Bu şart:
    `init_db()` HER AÇILIŞTA koşuyor, dolayısıyla koşulsuz bir
    `UPDATE … WHERE note LIKE '%TEYİT BEKLİYOR%'` kullanıcının temizlediği
    bayrakları her deploy'da geri getirirdi (not metni yerinde durduğu için).

    Kalıp: `_backfill_drive_folders()` — idempotent, hata-toleranslı.
    """
    from core.retention import REVIEW_MARKERS

    SENTINEL = "retention.review_backfill_done"
    db = SessionLocal()
    try:
        if db.query(AppSetting).filter(AppSetting.key == SENTINEL).first():
            return                                  # zaten koştu → O(1) no-op
        rows = db.query(RetentionSample).filter(
            RetentionSample.needs_review == False).all()      # noqa: E712
        n = 0
        for r in rows:
            note = r.note or ""
            if any(m in note for m in REVIEW_MARKERS):
                r.needs_review = True
                n += 1
        db.add(AppSetting(key=SENTINEL, value=str(n)))
        db.commit()
        if n:
            print(f"[init_db] retention needs_review backfill: {n} kayıt işaretlendi")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def _backfill_supplier_price_units():
    """Eski tedarikçi fiyat satırlarına fiyat temelini yaz (USD + kg/adet).

    Stok Son Durum listesi USD/kg'dir ama satırlar model varsayılanıyla
    `currency='TRY'` yazılmış, birim hiç tutulmamıştı (panel her fiyatı "₺"
    gösteriyordu).  `price_unit IS NULL` olan her satır: `currency='USD'`,
    `source='stok_son_durum'`, `price_unit` = malzeme adet (sayılan) birimliyse
    'adet', değilse 'kg' (`core.supplier_prices.default_price_unit` — içe
    aktarmayla aynı kural).

    BİR KEZ çalışır — `AppSetting` sentinel'i ile korunur (kalıp:
    `_backfill_retention_needs_review`).  Alembic revizyonu a3c5e7f9b1d4 aynı
    doldurmayı SQL ile yapar (yalnız bu UPDATE sıra-bağımsızdır, WHERE
    price_unit IS NULL); revizyon alembic geçmişi + temiz kurulum içindir —
    kolonları alter_safe'in zaten eklediği DB'de `alembic stamp head` kullan,
    `upgrade` `add_column`'da duplicate-column ile düşer.
    """
    from core.supplier_prices import default_price_unit, SOURCE_STOK_SON_DURUM

    SENTINEL = "backfill.supplier_price_units.v1"
    db = SessionLocal()
    try:
        if db.query(AppSetting).filter(AppSetting.key == SENTINEL).first():
            return                                  # zaten koştu → O(1) no-op
        rows = (db.query(SupplierPrice, Item.unit)
                .join(Item, Item.id == SupplierPrice.item_id)
                .filter(SupplierPrice.price_unit.is_(None)).all())
        for sp, item_unit in rows:
            sp.currency = "USD"
            sp.source = SOURCE_STOK_SON_DURUM
            sp.price_unit = default_price_unit(item_unit)
        db.add(AppSetting(key=SENTINEL, value=str(len(rows))))
        db.commit()
        if rows:
            print(f"[init_db] supplier_prices birim backfill: {len(rows)} satır USD + kg/adet işaretlendi")
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


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
            # PDKS — pdks_* tabloları (personel/program/olay/izin/tatil)
            # create_all ile gelir.  is_active, tabloyu ilk sürümden kurmuş
            # dev DB'ler için idempotent eklenir.
            "ALTER TABLE pdks_holidays ADD COLUMN is_active BOOLEAN DEFAULT TRUE",
            # PDKS — üçlü doğrulama (ofis ağı + konum + canlı QR) geo kolonları
            "ALTER TABLE pdks_events ADD COLUMN geo_lat DOUBLE PRECISION",
            "ALTER TABLE pdks_events ADD COLUMN geo_lon DOUBLE PRECISION",
            "ALTER TABLE pdks_events ADD COLUMN geo_accuracy_m DOUBLE PRECISION",
            # PDKS — imzanın hangi yolla doğrulandığı (qr/code/static/
            # static_fallback/off).  deploy.sh alembic ÇALIŞTIRMIYOR; kolon
            # prod'a yalnız bu satırla ulaşır (migration tarih içindir).
            "ALTER TABLE pdks_events ADD COLUMN verify_method VARCHAR(16)",
            # PDKS — izinde e-rapor/belge no (pdks_leave_requests tablosu
            # create_all ile gelir, ayrıca ALTER gerekmez).
            "ALTER TABLE pdks_leaves ADD COLUMN document_no VARCHAR(60)",
            # Şahit numune dolabı — retention_sample* tabloları create_all ile
            # gelir; bunlar mevcut tablolara eklenen kolonlar.
            "ALTER TABLE items ADD COLUMN lot_seq INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE production_history ADD COLUMN witness_quantity DOUBLE PRECISION NOT NULL DEFAULT 0",
            # Şahit numune — bilgi girişi turu: teyit bayrağı + soft-delete.
            # DİKKAT: deploy.sh alembic ÇALIŞTIRMIYOR; prod şeması fiilen bu
            # blokla evriliyor.  Migration dosyası alembic geçmişi + temiz
            # kurulum içindir — kolonu buraya yazmazsan canlıya ULAŞMAZ.
            "ALTER TABLE retention_samples ADD COLUMN needs_review BOOLEAN NOT NULL DEFAULT FALSE",
            "ALTER TABLE retention_samples ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE",
            # Ürün yorumları — review_invites/product_reviews tabloları create_all
            # ile gelir; shopify_orders'a eklenen tek kolon burada.  Doğrulanmış-
            # alıcı daveti sipariş işleme akışında bu bayrakla idempotent üretilir
            # (webhook redelivery / retry_order aynı siparişe iki davet üretmesin).
            "ALTER TABLE shopify_orders ADD COLUMN invites_created BOOLEAN NOT NULL DEFAULT FALSE",
            # PDKS — işten ayrılış tarihi.  deploy.sh alembic ÇALIŞTIRMIYOR;
            # kolon prod'a yalnız bu satırla ulaşır.
            "ALTER TABLE pdks_employees ADD COLUMN end_date DATE",
            # Influencer programı — influencer_* tabloları create_all ile
            # gelir; mevcut crm_task'a eklenen tek kolon + indeks burada.
            # deploy.sh alembic ÇALIŞTIRMIYOR; kolon prod'a yalnız bu satırla
            # ulaşır (migration b5d7f9a1c3e5 geçmiş + temiz kurulum içindir).
            "ALTER TABLE crm_task ADD COLUMN influencer_collab_id INTEGER",
            "CREATE INDEX IF NOT EXISTS ix_crm_task_inf_collab ON crm_task(influencer_collab_id)",
            # Numune analizi — deneme satırları tablosu create_all ile gelir;
            # mevcut sample_analyses'a eklenen tek kolon burada.  deploy.sh
            # alembic ÇALIŞTIRMIYOR; kolon prod'a yalnız bu satırla ulaşır
            # (migration c7e9a1b3d5f7 geçmiş + temiz kurulum içindir).
            "ALTER TABLE sample_analyses ADD COLUMN mode VARCHAR(10) NOT NULL DEFAULT 'new'",
            # Eksik Hammaddeler raporu — stock_order_flags YENİ bir tablo,
            # create_all ile gelir; buraya eklenecek kolon yok (bilgi notu).
            # Tedarikçi fiyatları — fiyat temeli (birim + kaynak + tarih).
            # deploy.sh alembic ÇALIŞTIRMIYOR; kolonlar prod'a yalnız bu
            # satırlarla ulaşır (migration a3c5e7f9b1d4 geçmiş + temiz kurulum
            # içindir).  Eski satırların doldurulması aşağıda, sentinel'li.
            "ALTER TABLE supplier_prices ADD COLUMN price_unit VARCHAR(8)",
            "ALTER TABLE supplier_prices ADD COLUMN source VARCHAR(30)",
            "ALTER TABLE supplier_prices ADD COLUMN source_label VARCHAR(120)",
            "ALTER TABLE supplier_prices ADD COLUMN quoted_at DATE",
            # Satın Alma Planı senaryoları — purchase_plans YENİ bir tablo,
            # create_all ile gelir (kısmi tekil indeks dahil); buraya eklenecek
            # kolon yok (bilgi notu).  Migration b7d9f1a3c5e8 geçmiş + temiz
            # kurulum içindir.
        ):
            alter_safe(stmt)

    # Eski Drive klasör-yüklemelerini (adında '/' olanlar) gerçek klasör ağacına
    # çevir — idempotent ('/' kalmayınca no-op).
    # Şahit numune teyit bayrağı — sentinel'li, bir kez koşar.
    try:
        _backfill_retention_needs_review()
    except Exception:
        pass

    try:
        _backfill_drive_folders()
    except Exception:
        pass

    # Tedarikçi fiyatlarının para birimi/birim düzeltmesi — sentinel'li, bir kez.
    try:
        _backfill_supplier_price_units()
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

    # ── Influencer programı — kademe + benchmark seed (idempotent) ─────────
    # CrmStage kalıbı: tablo BOŞSA varsayılan set kurulur, doluysa dokunulmaz
    # (yönetici ayarlar ekranından düzenlemiş olabilir).  Varsayılanlar
    # core/influencer.py'de; modül henüz yoksa seed sessizce atlanır.
    try:
        from core.influencer import DEFAULT_TIERS, DEFAULT_BENCHMARKS
    except ImportError:
        DEFAULT_TIERS, DEFAULT_BENCHMARKS = None, None
    if DEFAULT_TIERS is not None:
        db = SessionLocal()
        try:
            if db.query(InfluencerTier).count() == 0:
                for t in DEFAULT_TIERS:
                    db.add(InfluencerTier(
                        key=t["key"], label=t["label"],
                        min_followers=t.get("min_followers", 0),
                        max_followers=t.get("max_followers"),
                        commission_pct=t.get("commission_pct", 10.0),
                        customer_discount_pct=t.get("customer_discount_pct", 0.0),
                        max_gift_items=t.get("max_gift_items", 1),
                        launch_access=bool(t.get("launch_access", False)),
                        code_allowed=bool(t.get("code_allowed", False)),
                        sort_order=t.get("sort_order", 0),
                    ))
                db.commit()
            if db.query(InfluencerBenchmark).count() == 0:
                for b in (DEFAULT_BENCHMARKS or []):
                    db.add(InfluencerBenchmark(
                        platform=b["platform"], tier_key=b["tier_key"],
                        metric=b["metric"], low=b["low"], high=b["high"],
                    ))
                db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()
