# ─────────────────────────────────────────────────────────────────────────────
# Copyright (c) 2026 Doğukan Yalçınkaya.  All rights reserved.
# Bu dosya FSEK kapsamında bir bilgisayar programı eserinin parçasıdır.
# Mali haklar yazılı devir olmadıkça eser sahibinde kalır (FSEK m.48).
# Bkz. LICENSE ve AUTHORS.md.
# ─────────────────────────────────────────────────────────────────────────────
"""
Ürün yorumları — Shopify şema kurulumu (Faz C3).

`m108_review` metaobject tanımını, ona bağlı `m108.reviews` ürün metafield'ını
(list.metaobject_reference) ve standart `reviews.rating` / `reviews.rating_count`
ürün metafield tanımlarını oluşturur. Yorum İÇERİĞİ burada YOK — sadece şema.
Gerçek yorumları yazmak (metaobjectUpsert) C4'ün publish pipeline'ında olacak.

ÖN KOŞUL — bu script'i --commit ile çalıştırmadan önce
──────────────────────────────────────────────────────
Shopify Admin → Ayarlar → Uygulamalar ve satış kanalları → Uygulamalarınızı
geliştirin → **PARASUT** (Client ID 3f00e4ef041cb727e7d19ea7742c5450 —
dkkwdr-rr.myshopify.com'da kurulu ve prod .env'in kullandığı uygulama BU,
yerel .env'deki "IMS STOK ENTEGRASYONU" / 906581ae... DEĞİL) → Yapılandırma →
Admin API entegrasyon kapsamları'na şunları ekle:

    write_files
    write_metaobjects
    write_metaobject_definitions
    write_products

Kaydet → "Yeniden yükle" (reinstall) onayı Admin ister, onayla. Ardından PROD
servisini yeniden başlat: `core/shopify.py`'deki `_TOKEN_CACHE` eski scope'lu
access_token'ı 24 saate kadar bellekte tutuyor, yeniden başlatmadan yeni
scope'lar etkiye girmez.

Not: yalnız `write_*` eklemek yeterli — Shopify `write_X` verilince `read_X`'i
zımnen kapsıyor. Dry-run'ı scope'suz çalıştırınca hata metninde
`read_metaobject_definitions` görürsen bu YUKARIDAKİ listeden eksik bir scope
DEĞİL, `write_metaobject_definitions` henüz eklenmediği için bekleniyor.

UYARI: daha önce farklı bir uygulamada ("IMS STOK ENTEGRASYONU") benzer bir
scope eklemesi başarısız olmuştu, bu yüzden prod "PARASUT" uygulamasına
geçirilmişti. Aynı sorun burada da çıkarsa Shopify Partner desteğine
başvurulmalı — bu script'in --commit modu net bir GraphQL hatasıyla durur,
sessizce yarım kalmaz (bkz. idempotency notu altta).

KULLANIM
────────
  Dry-run (varsayılan — sadece ne yapılacağını yazdırır, YAZMAZ):
      python3 scripts/create_review_metaobject_definition.py
  Uygula:
      python3 scripts/create_review_metaobject_definition.py --commit
  Farklı marka (v1'de yorum toplama yalnız Minerva'da — bkz. onaylı plan):
      python3 scripts/create_review_metaobject_definition.py --brand Evanira --commit

  Prod'da:  cd /var/www/minerva && set -a && source .env && set +a \\
            && venv/bin/python scripts/create_review_metaobject_definition.py --commit

İDEMPOTENT: her adım önce var mı diye SORAR (metaobjectDefinitionByType /
metafieldDefinitions), varsa atlar. Yarıda kesilirse (örn. scope hatası)
yeniden çalıştırmak güvenli — zaten oluşmuş adımları tekrar yaratmaz.
"""
import argparse
import sys
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import shopify as S  # noqa: E402

REVIEW_TYPE = "m108_review"

# (key, name, type, required, validations|None) — plandaki alan listesiyle birebir
_FIELD_DEFS = [
    {"key": "product", "name": "Ürün", "type": "product_reference", "required": True},
    {"key": "author_name", "name": "Yazan", "type": "single_line_text_field", "required": True},
    {"key": "rating", "name": "Puan", "type": "rating", "required": True,
     "validations": [{"name": "scale_min", "value": "1.0"}, {"name": "scale_max", "value": "5.0"}]},
    {"key": "body", "name": "Yorum Metni", "type": "multi_line_text_field", "required": False},
    {"key": "audio", "name": "Sesli Not", "type": "file_reference", "required": False},
    {"key": "audio_duration_seconds", "name": "Ses Süresi (sn)", "type": "number_integer", "required": False},
    {"key": "transcript", "name": "Transkript", "type": "multi_line_text_field", "required": False},
    {"key": "locale", "name": "Dil", "type": "single_line_text_field", "required": True},
    {"key": "source", "name": "Kaynak", "type": "single_line_text_field", "required": True,
     "validations": [{"name": "choices", "value": '["verified_buyer","gifted"]'}]},
    {"key": "submitted_at", "name": "Gönderim Tarihi", "type": "date_time", "required": True},
    {"key": "ims_id", "name": "IMS Kaydı", "type": "number_integer", "required": True},
]

_Q_METAOBJECT_DEF_BY_TYPE = """
query($type: String!) {
  metaobjectDefinitionByType(type: $type) { id type name }
}
"""

_M_METAOBJECT_DEF_CREATE = """
mutation($definition: MetaobjectDefinitionCreateInput!) {
  metaobjectDefinitionCreate(definition: $definition) {
    metaobjectDefinition { id type }
    userErrors { field message code }
  }
}
"""

_Q_METAFIELD_DEFS = """
query($namespace: String!, $key: String!, $ownerType: MetafieldOwnerType!) {
  metafieldDefinitions(first: 1, namespace: $namespace, key: $key, ownerType: $ownerType) {
    nodes { id namespace key name }
  }
}
"""

_M_METAFIELD_DEF_CREATE = """
mutation($definition: MetafieldDefinitionInput!) {
  metafieldDefinitionCreate(definition: $definition) {
    createdDefinition { id namespace key }
    userErrors { field message code }
  }
}
"""

_Q_STANDARD_TEMPLATES = """
query {
  standardMetafieldDefinitionTemplates(first: 250) {
    nodes { id namespace key name ownerTypes type { name } }
  }
}
"""

_M_STANDARD_DEF_ENABLE = """
mutation($definition: StandardMetafieldDefinitionEnableInput!) {
  standardMetafieldDefinitionEnable(definition: $definition) {
    createdDefinition { id namespace key }
    userErrors { field message code }
  }
}
"""


def _print_step(title: str) -> None:
    print(f"\n{'─' * 70}\n{title}\n{'─' * 70}")


def ensure_metaobject_definition(client, commit: bool) -> Optional[str]:
    """m108_review metaobject tanımını oluşturur (yoksa). GID döner (var ya da yeni)."""
    _print_step(f"1) Metaobject tanımı: {REVIEW_TYPE}")
    data = S._graphql(client, _Q_METAOBJECT_DEF_BY_TYPE, {"type": REVIEW_TYPE})
    existing = data.get("metaobjectDefinitionByType")
    if existing:
        print(f"  zaten var — id={existing['id']}, atlanıyor")
        return existing["id"]

    definition = {
        "name": "M108 Ürün Yorumu",
        "type": REVIEW_TYPE,
        "fieldDefinitions": [
            {k: v for k, v in {
                "key": f["key"], "name": f["name"], "type": f["type"],
                "required": f["required"], "validations": f.get("validations"),
            }.items() if v is not None}
            for f in _FIELD_DEFS
        ],
        "access": {"storefront": "PUBLIC_READ", "admin": "MERCHANT_READ"},
        "capabilities": {"publishable": {"enabled": True}},
    }
    print(f"  YOK — {'oluşturuluyor' if commit else 'oluşturulacak (dry-run)'}: "
          f"{len(_FIELD_DEFS)} alan, access=PUBLIC_READ/MERCHANT_READ, publishable=True")
    for f in _FIELD_DEFS:
        req = "zorunlu" if f["required"] else "opsiyonel"
        print(f"    · {f['key']:<24} {f['type']:<24} {req}")
    if not commit:
        return None

    res = S._graphql(client, _M_METAOBJECT_DEF_CREATE, {"definition": definition})
    ue = (res.get("metaobjectDefinitionCreate") or {}).get("userErrors") or []
    if ue:
        print(f"  HATA: {ue}")
        return None
    new_id = res["metaobjectDefinitionCreate"]["metaobjectDefinition"]["id"]
    print(f"  oluşturuldu — id={new_id}")
    return new_id


def ensure_reviews_list_metafield(client, metaobject_def_id: Optional[str], commit: bool) -> None:
    """Product.m108.reviews (list.metaobject_reference) — m108_review tanımına bağlı."""
    _print_step("2) Ürün metafield'ı: m108.reviews")
    data = S._graphql(client, _Q_METAFIELD_DEFS,
                      {"namespace": "m108", "key": "reviews", "ownerType": "PRODUCT"})
    nodes = (data.get("metafieldDefinitions") or {}).get("nodes") or []
    if nodes:
        print(f"  zaten var — id={nodes[0]['id']}, atlanıyor")
        return

    if not commit:
        ref = metaobject_def_id or "(1. adımda oluşacak m108_review tanımı)"
        print(f"  YOK — oluşturulacak (dry-run): list.metaobject_reference → {ref}")
        return

    if not metaobject_def_id:
        print("  ATLANDI — m108_review tanımının GID'i yok (1. adım başarısız olmuş olmalı)")
        return

    definition = {
        "name": "Ürün Yorumları",
        "namespace": "m108",
        "key": "reviews",
        "type": "list.metaobject_reference",
        "ownerType": "PRODUCT",
        "validations": [{"name": "metaobject_definition_id", "value": metaobject_def_id}],
    }
    print(f"  YOK — oluşturuluyor: list.metaobject_reference → {metaobject_def_id}")
    res = S._graphql(client, _M_METAFIELD_DEF_CREATE, {"definition": definition})
    ue = (res.get("metafieldDefinitionCreate") or {}).get("userErrors") or []
    if ue:
        print(f"  HATA: {ue}")
        return
    print(f"  oluşturuldu — id={res['metafieldDefinitionCreate']['createdDefinition']['id']}")


def ensure_standard_aggregate_fields(client, commit: bool) -> None:
    """reviews.rating + reviews.rating_count (Shopify standart şablonlarından)."""
    _print_step("3) Standart agregat metafield'ları: reviews.rating / reviews.rating_count")
    targets = ["rating", "rating_count"]
    to_create = []
    for key in targets:
        data = S._graphql(client, _Q_METAFIELD_DEFS,
                          {"namespace": "reviews", "key": key, "ownerType": "PRODUCT"})
        nodes = (data.get("metafieldDefinitions") or {}).get("nodes") or []
        if nodes:
            print(f"  reviews.{key} zaten var — id={nodes[0]['id']}, atlanıyor")
        else:
            print(f"  reviews.{key} YOK — standart şablondan eklenecek")
            to_create.append(key)

    if not to_create:
        return
    if not commit:
        print(f"  (dry-run) --commit ile standardMetafieldDefinitionTemplates aranıp "
              f"{to_create} etkinleştirilecek")
        return

    tpl_data = S._graphql(client, _Q_STANDARD_TEMPLATES, {})
    templates = (tpl_data.get("standardMetafieldDefinitionTemplates") or {}).get("nodes") or []
    by_key = {t["key"]: t for t in templates
              if t["namespace"] == "reviews" and "PRODUCT" in (t.get("ownerTypes") or [])}

    for key in to_create:
        tpl = by_key.get(key)
        if not tpl:
            print(f"  HATA: standart şablonlarda reviews.{key} (PRODUCT) bulunamadı — "
                  f"Shopify API sürümü değişmiş olabilir, elle Admin'den eklenmeli")
            continue
        res = S._graphql(client, _M_STANDARD_DEF_ENABLE, {"definition": {"id": tpl["id"]}})
        ue = (res.get("standardMetafieldDefinitionEnable") or {}).get("userErrors") or []
        if ue:
            print(f"  HATA (reviews.{key}): {ue}")
            continue
        created = res["standardMetafieldDefinitionEnable"]["createdDefinition"]
        print(f"  reviews.{key} oluşturuldu — id={created['id']}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--brand", default="Minerva", choices=list(S._BRANDS),
                    help="Hedef mağaza (varsayılan: Minerva — v1 kapsamı bu)")
    ap.add_argument("--commit", action="store_true",
                    help="Değişiklikleri uygula (varsayılan: dry-run)")
    args = ap.parse_args()

    cfg = S.store_config(args.brand)
    if not cfg:
        print(f"HATA: {args.brand} için Shopify env değişkenleri eksik "
              f"(SHOPIFY_{args.brand.upper()}_STORE/CLIENT_ID/CLIENT_SECRET/LOCATION)")
        sys.exit(1)

    print(f"Mağaza: {cfg['store']}  ·  mod: {'COMMIT' if args.commit else 'DRY-RUN'}")
    if not args.commit:
        print("(Hiçbir şey yazılmayacak — sadece plan gösteriliyor. --commit ile uygula.)")

    try:
        with S._client(cfg) as client:
            metaobject_def_id = ensure_metaobject_definition(client, args.commit)
            ensure_reviews_list_metafield(client, metaobject_def_id, args.commit)
            ensure_standard_aggregate_fields(client, args.commit)
    except S.ShopifyError as e:
        print(f"\nGraphQL hatası: {e}")
        print("Scope eksikse (write_metaobject_definitions / write_products) hata mesajı "
              "genelde erişimin engellendiğini söyler — dosya başındaki ÖN KOŞUL bölümüne bak.")
        sys.exit(1)

    print(f"\n{'─' * 70}")
    if args.commit:
        print("Tamamlandı.")
    else:
        print("Dry-run bitti. Uygulamak için --commit ekleyerek tekrar çalıştır.")


if __name__ == "__main__":
    main()
