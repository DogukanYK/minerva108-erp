"""
Sayfa render / template syntax sanity testleri.

Auto-refactor (örn. toast.js refactor) script'leri bazen yanlışlıkla
closing </script> veya kritik init bloklarını yiyor.  Bu testler her
template'in:
  • HTTP 200 dönmesini
  • <script> ve </script> tag sayısının eşit olmasını
  • Render edilmiş HTML'in /static/toast.js içerdiğini
doğrular.  Auto-refactor regresyonlarını erken yakalar.
"""
from fastapi.testclient import TestClient


# Her korumalı sayfa + minimum gerekli izin (SuperAdmin hepsine erişebilir)
PAGES = [
    "/",
    "/items",
    "/suppliers",
    "/receiving",
    "/recipes",
    "/production",
    "/qc",
    "/stocks",
    "/ledger",
    "/reports",
    "/traceability",
    "/quotations",
    "/admin",
]


def test_login_page_renders(client: TestClient):
    """Login sayfası unauth görünür."""
    r = client.get("/login")
    assert r.status_code == 200
    assert "Sisteme Giriş Yap" in r.text or "Giriş" in r.text


def test_all_protected_pages_render_after_login(authed_client: TestClient):
    """SuperAdmin tüm sayfaları render edebilmeli."""
    failures = []
    for path in PAGES:
        r = authed_client.get(path)
        if r.status_code != 200:
            failures.append(f"{path}: HTTP {r.status_code}")
    assert not failures, "Sayfa render hataları:\n" + "\n".join(failures)


def test_script_tag_balance_on_every_page(authed_client: TestClient):
    """
    Auto-refactor regresyon koruması: render edilmiş HTML'de <script ve
    </script> sayısı eşit olmalı (admin.html'deki R7 hatasını yakalar).
    """
    failures = []
    for path in ["/login"] + PAGES:
        r = authed_client.get(path) if path != "/login" else authed_client.get(path)
        html = r.text
        open_count  = html.count("<script")
        close_count = html.count("</script>")
        if open_count != close_count:
            failures.append(
                f"{path}: <script {open_count} != </script> {close_count}"
            )
    assert not failures, "Tag dengesi bozuk sayfalar:\n" + "\n".join(failures)


def test_shared_toast_js_loaded_everywhere(authed_client: TestClient):
    """Her sayfa /static/toast.js'i include etmeli (R7 sonrası)."""
    failures = []
    for path in ["/login"] + PAGES:
        r = authed_client.get(path)
        if "/static/toast.js" not in r.text:
            failures.append(path)
    assert not failures, "toast.js include eksik: " + ", ".join(failures)


def test_static_toast_js_served(client: TestClient):
    """/static/toast.js dosyası serve ediliyor mu?"""
    r = client.get("/static/toast.js")
    assert r.status_code == 200
    assert "window.showToast" in r.text
    # XSS-safe pattern doğrulaması: createElement + textContent kullanmalı
    assert "createElement" in r.text
    assert "textContent" in r.text
    # Eski insecure pattern olmamalı — .innerHTML = ataması arıyoruz
    # (yorum içinde geçen kelime sayılmasın)
    assert ".innerHTML =" not in r.text
    assert ".innerHTML=" not in r.text


def test_no_local_showToast_left(authed_client: TestClient):
    """Hiçbir template'te local 'function showToast' kalmamalı (paylaşılana göre)."""
    failures = []
    for path in PAGES + ["/login"]:
        r = authed_client.get(path)
        if "function showToast" in r.text:
            failures.append(path)
    assert not failures, "Hâlâ local showToast bulunan sayfalar: " + ", ".join(failures)
