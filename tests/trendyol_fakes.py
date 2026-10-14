"""
Trendyol testleri için sahte API + örnek yükler — AĞ YOK.

`FakeApi` core.trendyol.Api'nin `get`/`close`/`seller_id`/`calls` sözleşmesini
taklit eder; yanıtlar yol parçasına göre (en uzun eşleşen anahtar önce)
sözlükten ya da çağrılabilirden gelir.  Örnek değerler Trendyol dokümanından:
paket/satır alanları (Nisan 2026 adları), "Örnek 6" indirim gösterimi
(2 adet × 350, birim satıcı indirimi 35 → net 630) ve hakediş örneği
(credit 449,99 · oran 15 · komisyon 67,4985 · hakediş 382,4915).
"""
from typing import Callable, Dict, Optional, Union

import pytest

SELLER = "123456"


@pytest.fixture(autouse=True)
def trendyol_stop_reset():
    """api_main shutdown'ı her TestClient kapanışında `request_stop()` çağırır;
    bayrak açık kalırsa sonraki (client'sız) testte senkron hemen "durdu" olur.
    Trendyol test modülleri bunu import eder (autouse — modül içinde geçerli)."""
    from core import trendyol as T
    T.reset_stop()
    yield
    T.reset_stop()

Route = Union[dict, list, Exception, Callable[[str, dict], object]]


class FakeApi:
    def __init__(self, routes: Optional[Dict[str, Route]] = None, seller_id: str = SELLER):
        self.seller_id = seller_id
        self.calls = 0
        self.log = []
        self.closed = False
        self.routes = dict(routes or {})

    def get(self, group, path, params=None):
        from core import trendyol as T
        if T._STOP.is_set():                   # gerçek Api gibi: kapanışta istek gitmez
            raise T.TrendyolStopped(T.STOP_MSG)
        params = dict(params or {})
        self.calls += 1
        self.log.append((group, path, params))
        for key in sorted(self.routes, key=len, reverse=True):
            if key in path:
                r = self.routes[key]
                if callable(r) and not isinstance(r, Exception):
                    r = r(path, params)
                if isinstance(r, Exception):
                    raise r
                return r
        return {}

    def query(self, group, path, body):
        """Gerçek Api.query gibi: yalnız izin listeli yol (buybox), aksi ValueError."""
        from core import trendyol as T
        if T._STOP.is_set():
            raise T.TrendyolStopped(T.STOP_MSG)
        if not any(rx.match(path) for rx in T.QUERY_PATHS):
            raise ValueError(f"izin listesinde olmayan sorgu: {path}")
        self.calls += 1
        self.log.append((group, path, {"body": body}))
        for key in sorted(self.routes, key=len, reverse=True):
            if key in path:
                r = self.routes[key]
                if callable(r) and not isinstance(r, Exception):
                    r = r(path, body)
                if isinstance(r, Exception):
                    raise r
                return r
        return {}

    def close(self):
        self.closed = True

    def calls_to(self, fragment):
        return [entry for entry in self.log if fragment in entry[1]]


def line(line_id=4765000001, barcode="8680000000011", qty=2, gross=350.0, seller=35.0, ty=0.0,
         vat=20, commission=15.0, name="Kırmızı Yonca Krem 50 ML", details=True, status="Delivered"):
    unit = gross - seller - ty
    d = {"lineId": line_id, "barcode": barcode, "stockCode": "SKU-1", "productName": name,
         "contentId": 111, "quantity": qty, "lineGrossAmount": gross, "lineSellerDiscount": seller,
         "lineTyDiscount": ty, "lineTotalDiscount": seller + ty, "lineUnitPrice": unit,
         "vatRate": vat, "commission": commission, "orderLineItemStatusName": status,
         "currencyCode": "TRY"}
    if details:
        d["discountDetails"] = [{"lineItemPrice": unit, "lineItemSellerDiscount": seller,
                                 "lineItemTyDiscount": ty} for _ in range(qty)]
    return d


def package(pid=3330000001, order="1065440001", status="Delivered", lines=None,
            lm=1762865408581, od=1762253333685, tracking="7260001151141191"):
    return {
        "orderNumber": order, "shipmentPackageId": pid, "shipmentPackageStatus": status,
        "status": status, "orderDate": od, "lastModifiedDate": lm, "currencyCode": "TRY",
        "packageGrossAmount": 700.0, "packageSellerDiscount": 70.0, "packageTyDiscount": 0.0,
        "packageTotalDiscount": 70.0, "packageTotalPrice": 630.0,
        "cargoProviderName": "Trendyol Express", "cargoTrackingNumber": tracking,
        # Kişisel veri — parser'ın ALMAMASI gerekenler:
        "customerFirstName": "Ayşe", "customerLastName": "Yılmaz", "customerEmail": "ayse@example.com",
        "customerId": 998877, "taxNumber": "12345678901", "identityNumber": "11111111111",
        "shipmentAddress": {"fullName": "Ayşe Yılmaz", "address1": "Gizli Mah. 1", "phone": "05550000000"},
        "invoiceAddress": {"fullName": "Ayşe Yılmaz", "address1": "Gizli Mah. 1"},
        "lines": lines if lines is not None else [line()],
    }


def stream(content, has_more=False, cursor=None):
    return {"hasMore": has_more, "nextCursor": cursor, "size": len(content), "content": content}


def paged(content, total_pages=1, page=0):
    return {"page": page, "size": len(content), "totalPages": total_pages,
            "totalElements": len(content), "content": content}


def settlement(row_id="725041340", order="1065440001", barcode="8680000000011", credit=449.99,
               debt=0.0, rate=15.0, comm=67.4985, rev=382.4915, pkg=3330000001, label="Satış"):
    return {"id": row_id, "transactionDate": 1762865408581, "barcode": barcode,
            "transactionType": label, "receiptId": 48376618, "description": label, "debt": debt,
            "credit": credit, "paymentPeriod": 30, "commissionRate": rate, "commissionAmount": comm,
            "commissionInvoiceSerialNumber": None, "sellerRevenue": rev, "orderNumber": order,
            "paymentOrderId": 112360, "paymentDate": 1765000000000, "sellerId": 123456,
            "storeId": None, "storeName": None, "storeAddress": None, "country": "Türkiye",
            "orderDate": 1762253333685, "affiliate": "TRENDYOLTR", "shipmentPackageId": pkg}


def deduction(row_id="TYC2026000001", label="Kargo Faturası", debt=82.18):
    return {"id": row_id, "transactionDate": 1762865408581, "transactionType": label,
            "description": label, "debt": debt, "credit": 0.0, "paymentOrderId": 112360}


def cargo_item(order="1065440001", amount=34.24, desi=1, kind="Gönderi Kargo Bedeli",
               parcel=7260001151141191):
    return {"shipmentPackageType": kind, "parcelUniqueId": parcel, "orderNumber": order,
            "amount": amount, "desi": desi}


def approved(barcode="8680000000011", title="Kırmızı Yonca Krem 50 ML", brand="Minerva 108",
             sale=315.0, lst=350.0, on_sale=True, vat=20):
    return {"contentId": 111, "productMainId": "PM-1", "brand": {"id": 1, "name": brand},
            "category": {"id": 2, "name": "Yüz Kremi"}, "title": title,
            "variants": [{"variantId": 9, "barcode": barcode, "stockCode": "SKU-1", "onSale": on_sale,
                          "archived": False, "locked": False, "blacklisted": False, "vatRate": vat,
                          "price": {"salePrice": sale, "listPrice": lst},
                          "productUrl": "https://www.trendyol.com/minerva/krem-p-111"}]}


def inventory(barcode="8680000000011", qty=12, sale=315.0, lst=350.0):
    return {"contentId": 111, "productMainId": "PM-1",
            "variants": [{"variantId": 9, "barcode": barcode, "salePrice": sale, "listPrice": lst,
                          "quantity": qty, "stockCode": "SKU-1", "stockLastModifiedDate": None}]}


def content(barcode="8680000000011", title="Kırmızı Yonca Krem 50 ML", description="<p>Nemlendirici krem.</p>",
            attributes=None, images=3, category_id=2, delivery=1, sale=315.0, lst=350.0):
    """İçerikli onaylı ürün (açıklama, özellikler, görseller, kategori, teslimat)."""
    d = approved(barcode=barcode, title=title, sale=sale, lst=lst)
    d["category"] = {"id": category_id, "name": "Yüz Kremi"}
    d["description"] = description
    d["images"] = [{"url": f"https://cdn.dsmcdn.com/{i}.jpg"} for i in range(images)]
    d["attributes"] = attributes if attributes is not None else [
        {"attributeId": 47, "attributeName": "Hacim", "attributeValues": [{"attributeValueId": 501, "attributeValue": "50 ml"}]}]
    d["variants"][0]["deliveryOptions"] = {"deliveryDuration": delivery, "isRushDelivery": False}
    return d


def category_attrs(cid=2, attrs=None):
    """GET /product/categories/{id}/attributes yanıtı."""
    attrs = attrs if attrs is not None else [
        (47, "Hacim", True, False, False), (338, "Cilt Tipi", True, True, False), (60, "Menşei", False, False, False)]
    return {"id": cid, "name": "Yüz Kremi", "displayName": "Yüz Kremi", "categoryAttributes": [
        {"attribute": {"id": aid, "name": name}, "required": req, "slicer": slicer, "varianter": False,
         "allowCustom": custom, "allowMultipleAttributeValues": False}
        for aid, name, req, slicer, custom in attrs]}


def buybox(barcode="8680000000011", order=1, price=315.0, second=None, third=None, multiple=False):
    return {"barcode": barcode, "buyboxOrder": order, "buyboxPrice": price, "secondBuyboxPrice": second,
            "thirdBuyboxPrice": third, "hasMultipleSeller": multiple}


def question(qid=5001, text="Bu krem yağlı cilde uygun mu?", status="WAITING_FOR_ANSWER", pmid="PM-1",
             created=1762865408581, answer=None):
    d = {"id": qid, "text": text, "status": status, "productMainId": pmid, "productName": "Kırmızı Yonca Krem 50 ML",
         "creationDate": created, "webUrl": f"https://www.trendyol.com/soru/{qid}",
         # Kişisel veri — ALINMAMALI:
         "customerId": 998877, "userName": "Ayşe Y.", "showUserName": True}
    if answer:
        d["answer"] = {"text": answer, "creationDate": created + 3_600_000}
    return d
