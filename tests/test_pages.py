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
import pathlib
import re

from fastapi.testclient import TestClient


# Her korumalı sayfa + minimum gerekli izin (SuperAdmin hepsine erişebilir)
PAGES = [
    "/sahit-numune",
    "/urun-gorselleri",
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
    "/system",
    "/influencer",
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


def test_recipes_page_template_vars_declared(authed_client: TestClient):
    """
    /recipes sayfasının inline JS'inde template literal kullanılan ${var}
    pattern'larında, kısa adlı (1-2 harfli) değişkenler için tanım kontrolü.
    Geçmiş bug: refactor sırasında 'const u = ...' tanımı silindi ama
    template'de '${u}' kaldı → 'u is not defined' runtime hatası.
    """
    import re
    r = authed_client.get("/recipes")
    assert r.status_code == 200
    html = r.text

    # Tek harfli template-literal kullanımları bul
    short_vars = set(re.findall(r"\$\{([a-zA-Z_])\}", html))
    # Her birinin 'const X' / 'let X' / 'var X' / parametre olarak tanımı var mı?
    missing = []
    for v in short_vars:
        # Tanımları regex'le ara: var, let, const, function param
        declared = (
            re.search(rf"\b(?:const|let|var)\s+{v}\b", html) is not None
            or re.search(rf"\.forEach\(\s*\(?\s*{v}\b", html) is not None
            or re.search(rf"function[^(]*\([^)]*\b{v}\b", html) is not None
            or re.search(rf"=>\s*{v}\b|\(\s*{v}\b\s*\)\s*=>", html) is not None
        )
        if not declared:
            missing.append(v)
    assert not missing, (
        f"/recipes inline JS'inde tanımsız ${{{','.join(missing)}}} değişken(ler)i. "
        f"Bu 'undefined' runtime hatası verir (geçmişte ${{u}} sorunu)."
    )


def test_admin_matrix_shows_all_permissions(authed_client: TestClient):
    """
    Admin sayfasındaki permission matrix'inde core/permissions.py'deki TÜM
    action'lar render edilmeli.  Geçmiş bug: actionOrder array'inde 'adjust'
    ve 'backup' eksikti — bu yüzden matrix'te görünmüyorlardı.  Bu test
    actionOrder'da tüm action'ların var olduğunu doğrular.
    """
    from core.permissions import PERMISSION_CATEGORIES
    r = authed_client.get("/admin")
    assert r.status_code == 200
    html = r.text

    # actionOrder array'i admin.html içinde inline JS — string olarak yakala
    # ve tüm bilinen action'ların orada listelendiğini doğrula.
    all_actions = set()
    for actions in PERMISSION_CATEGORIES.values():
        all_actions.update(actions)

    missing = []
    for action in all_actions:
        # actionOrder içinde 'view' veya 'backup' gibi quote'lu olmalı
        if f"'{action}'" not in html:
            missing.append(action)
    assert not missing, (
        f"Bu action'lar admin matrix actionOrder/labels'da yok: {missing}. "
        f"templates/admin.html'de actionOrder ve _ACTION_LABELS'a ekle."
    )


# ─── Sistem sağlık / canlılık ───────────────────────────────────────────────

def test_health_endpoint_public(client: TestClient):
    """Hafif /health endpoint kimliksiz erişilebilir ve 'ok' döner."""
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json().get("status") == "ok"


# NOT: authed_client ve labtech_client aynı 'client' fixture'ını paylaşır —
# tek testte ikisini birden kullanmak cookie çakışmasına yol açar.  Bu yüzden
# her rol ayrı test fonksiyonunda doğrulanır.

def test_system_page_renders_for_superadmin(authed_client: TestClient):
    """/system sayfası SuperAdmin'e render olmalı."""
    r = authed_client.get("/system")
    assert r.status_code == 200
    assert "Sistem Durumu" in r.text


def test_system_page_blocked_for_labtech(labtech_client: TestClient):
    """/system sayfası SuperAdmin olmayan kullanıcıya kapalı — dashboard'a redirect."""
    r = labtech_client.get("/system", follow_redirects=False)
    assert r.status_code == 302


def test_system_health_api_for_superadmin(authed_client: TestClient):
    """/api/system/health SuperAdmin'e zengin sağlık raporu döner."""
    r = authed_client.get("/api/system/health")
    assert r.status_code == 200
    body = r.json()
    assert body.get("status") in ("ok", "warn", "down")
    assert "checks" in body and isinstance(body["checks"], dict)


def test_system_health_api_blocked_for_labtech(labtech_client: TestClient):
    """/api/system/health SuperAdmin olmayan kullanıcıya 403."""
    r = labtech_client.get("/api/system/health")
    assert r.status_code == 403


# ─── Aylık detaylı sistem raporu ────────────────────────────────────────────

def test_monthly_report_pdf_for_superadmin(authed_client: TestClient):
    """Aylık rapor PDF olarak üretilip indirilebilmeli."""
    r = authed_client.get("/api/system/report?year=2026&month=5&format=pdf")
    assert r.status_code == 200
    assert r.headers.get("content-type") == "application/pdf"
    assert r.content[:4] == b"%PDF"


def test_monthly_report_excel_for_superadmin(authed_client: TestClient):
    """Aylık rapor Excel (xlsx) olarak üretilip indirilebilmeli."""
    r = authed_client.get("/api/system/report?year=2026&month=5&format=excel")
    assert r.status_code == 200
    assert r.content[:2] == b"PK"          # xlsx = zip arşivi


def test_monthly_report_blocked_for_labtech(labtech_client: TestClient):
    """Aylık rapor SuperAdmin olmayan kullanıcıya 403."""
    r = labtech_client.get("/api/system/report?year=2026&month=5&format=pdf")
    assert r.status_code == 403


def test_monthly_report_list_for_superadmin(authed_client: TestClient):
    """Kayıtlı raporlar listesi SuperAdmin'e açık ve 'reports' anahtarı döner."""
    r = authed_client.get("/api/system/reports")
    assert r.status_code == 200
    assert "reports" in r.json()


# ─── Bootstrap sınıf çakışması (canlıda yaşandı) ────────────────────────────

# Bootstrap'in kendi kuralları aynı adlı yerel sınıfı GÖRÜNMEZ yapabilir.
# .btn-check{position:absolute;clip:rect(0,0,0,0);pointer-events:none}
# Yerel kural bu üç property'yi tanımlamazsa cascade onları korur → element
# ekranda yok, tıklanamaz.  PDKS "GİRİŞ/ÇIKIŞ YAP" butonu tam olarak böyle
# kayboldu; personel modülü haftalarca kullanamadı.
_BOOTSTRAP_SHADOWING = ("btn-check",)


def test_no_bootstrap_shadowed_class_names():
    for f in sorted(pathlib.Path("templates").glob("*.html")):
        src = f.read_text(encoding="utf-8")
        if "bootstrap" not in src:
            continue                      # Bootstrap yüklenmiyorsa çakışma yok
        code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)     # CSS yorumlarını at
        for cls in _BOOTSTRAP_SHADOWING:
            assert cls not in code, (
                f"{f.name}: '{cls}' Bootstrap'te gizleyici bir sınıf — "
                f"başka bir ad kullan (ör. 'check-action')")
