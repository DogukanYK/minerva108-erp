"""
Minerva Asistan widget'ı — şablon enjeksiyonu + statik dosya testleri.

Kilitlenenler:
  • idle-watch.js yükleyen her iç şablon (bayi portalı hariç) /static/assistant.js
    de yükler — ve tam idle-watch satırının ALTINDA, aynı girintiyle.
  • Public / kiosk / portal şablonlarında (login, share, yorum, başvuru,
    pdks_qr*, distributor_portal) widget YOK.
  • static/assistant.js `node --check`ten geçer, XSS-safe kalıp (createElement
    + textContent, innerHTML ataması yok), `btn-*` sınıf adı yok (Bootstrap
    gölgeleme tuzağı), public yol koruması var.
  • sw.js CACHE_NAME bump'landı (v37 → v38+): önbellekli sayfalar yeni
    script'i görsün.
  • scripts/add_assistant_widget.py idempotent ve bayi portalını atlar
    (geçici kopya üzerinde çalıştırılır).
  • Render edilen sayfalarda script var; login'de yok; sohbet uçları ağa çıkmaz.
"""
import pathlib
import re
import shutil
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

ROOT = pathlib.Path(__file__).resolve().parent.parent
TPL = ROOT / "templates"
STATIC = ROOT / "static"
SCRIPT = ROOT / "scripts" / "add_assistant_widget.py"

IDLE = '<script src="/static/idle-watch.js" defer></script>'
WIDGET_RE = re.compile(r'<script src="/static/assistant\.js(\?v=\d+)?" defer></script>')
PORTAL = "distributor_portal.html"
PUBLIC_TEMPLATES = ["login.html", "share.html", "yorum.html", "basvuru.html",
                    "basvuru_claim.html", "pdks_qr.html", "pdks_qr_yazdir.html", PORTAL]
EXPECTED_INTERNAL = 28


def _templates_with_idle():
    return sorted(f for f in TPL.glob("*.html") if IDLE in f.read_text(encoding="utf-8"))


# ─── Şablon enjeksiyonu ──────────────────────────────────────────────────────

def test_every_internal_template_loads_widget():
    """idle-watch yükleyen her şablon (bayi portalı hariç) widget'ı da yükler."""
    internal = [f for f in _templates_with_idle() if f.name != PORTAL]
    assert len(internal) == EXPECTED_INTERNAL, (
        f"idle-watch yükleyen iç şablon sayısı {len(internal)} (beklenen {EXPECTED_INTERNAL}); "
        f"yeni sayfa eklendiyse EXPECTED_INTERNAL'ı güncelle ve enjeksiyon script'ini çalıştır")
    missing = [f.name for f in internal if not WIDGET_RE.search(f.read_text(encoding="utf-8"))]
    assert not missing, "assistant.js eksik: " + ", ".join(missing)


def test_widget_line_follows_idle_watch_with_same_indent():
    """Script sırası: idle-watch → assistant (aynı girinti, tam altında); tek kopya."""
    bad = []
    for f in _templates_with_idle():
        if f.name == PORTAL:
            continue
        src = f.read_text(encoding="utf-8")
        if len(WIDGET_RE.findall(src)) != 1:
            bad.append(f"{f.name}: {len(WIDGET_RE.findall(src))} kopya")
            continue
        pat = re.compile(r'^([ \t]*)' + re.escape(IDLE) + r'[ \t]*\n\1<script src="/static/assistant\.js', re.M)
        if not pat.search(src):
            bad.append(f"{f.name}: assistant.js idle-watch'ın hemen altında değil")
    assert not bad, "\n".join(bad)


def test_public_kiosk_portal_templates_have_no_widget():
    """Oturumsuz / kiosk / bayi sayfalarında widget yüklenmez."""
    offenders = []
    for name in PUBLIC_TEMPLATES:
        p = TPL / name
        assert p.exists(), f"{name} yok — PUBLIC_TEMPLATES listesini güncelle"
        if "/static/assistant.js" in p.read_text(encoding="utf-8"):
            offenders.append(name)
    assert not offenders, "widget public/kiosk/portal şablonunda: " + ", ".join(offenders)


def test_only_idle_watch_templates_have_widget():
    """Widget yalnız idle-watch yükleyen (oturumlu) şablonlarda — başka yere sızmamış."""
    stray = [f.name for f in TPL.glob("*.html")
             if "/static/assistant.js" in f.read_text(encoding="utf-8")
             and IDLE not in f.read_text(encoding="utf-8")]
    assert not stray, "idle-watch'sız şablonda widget: " + ", ".join(stray)


# ─── scripts/add_assistant_widget.py ─────────────────────────────────────────

def test_injection_script_is_idempotent_and_skips_portal(tmp_path):
    """Geçici bir kopyada: ilk koşu ekler, ikinci koşu hiçbir şeyi değiştirmez;
    bayi portalı ve idle-watch'sız şablon dokunulmadan kalır."""
    (tmp_path / "scripts").mkdir()
    (tmp_path / "templates").mkdir()
    shutil.copy(SCRIPT, tmp_path / "scripts" / SCRIPT.name)
    page = "<html><head>\n  " + IDLE + "\n  <script src=\"/static/undo-watch.js\" defer></script>\n</head></html>\n"
    (tmp_path / "templates" / "items.html").write_text(page, encoding="utf-8")
    (tmp_path / "templates" / PORTAL).write_text(page, encoding="utf-8")
    (tmp_path / "templates" / "login.html").write_text("<html><head></head></html>\n", encoding="utf-8")

    r1 = subprocess.run([sys.executable, str(tmp_path / "scripts" / SCRIPT.name)],
                        capture_output=True, text=True, timeout=30)
    assert r1.returncode == 0, r1.stderr
    assert "items.html" in r1.stdout.split("Atlandı")[0]
    after1 = (tmp_path / "templates" / "items.html").read_text(encoding="utf-8")
    assert after1.count("/static/assistant.js") == 1
    assert re.search(r'^  ' + re.escape(IDLE) + r'\n  <script src="/static/assistant\.js', after1, re.M)
    assert "/static/assistant.js" not in (tmp_path / "templates" / PORTAL).read_text(encoding="utf-8")
    assert "/static/assistant.js" not in (tmp_path / "templates" / "login.html").read_text(encoding="utf-8")

    r2 = subprocess.run([sys.executable, str(tmp_path / "scripts" / SCRIPT.name)],
                        capture_output=True, text=True, timeout=30)
    assert r2.returncode == 0, r2.stderr
    assert (tmp_path / "templates" / "items.html").read_text(encoding="utf-8") == after1
    assert "Toplam  : 0" in r2.stdout


# ─── static/assistant.js ─────────────────────────────────────────────────────

def test_assistant_js_passes_node_check():
    node = shutil.which("node")
    if not node:
        pytest.skip("node yok")
    r = subprocess.run([node, "--check", str(STATIC / "assistant.js")], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


def test_assistant_js_is_xss_safe_and_bootstrap_clean():
    src = (STATIC / "assistant.js").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)           # blok yorumları at
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)         # satır yorumları at
    assert "createElement" in code and "textContent" in code
    assert ".innerHTML =" not in code and ".innerHTML=" not in code
    assert "insertAdjacentHTML" not in code and "outerHTML" not in code
    assert "document.write" not in code
    # Bootstrap gölgeleme tuzağı: btn-* sınıf adı YOK; her sınıf m108-ai- önekli
    assert not re.search(r"['\"][^'\"]*\bbtn-", code), "btn-* sınıf adı yasak"
    for cls in re.findall(r"\.([a-zA-Z][\w-]*)\{", code):
        assert cls.startswith("m108-ai-"), f"öneksiz CSS sınıfı: {cls}"
    # IIFE + 'use strict' + public yol koruması
    assert code.lstrip().startswith("(function ()")
    assert "'use strict'" in code
    for path in ("login", "s\\/", "yorum", "basvuru", "pdks-qr", "portal"):
        assert path in code, f"public yol koruması eksik: {path}"
    # Yalnız iç bağlantı: "/" ile başlayan, ikinci karakter "/" ya da "\\" olmayan
    # (WHATWG: "/\\evil.com" → "https://evil.com"), origin denetimi tek yardımcıda
    assert "/^\\/(?![\\/\\\\])/" in code and "function isInternalHref(href)" in code
    assert "new URL(href, location.origin).origin === location.origin" in code
    assert code.count("isInternalHref(") >= 4                 # tanım + 3 kullanım (markdown, çip, kaynaklar)
    assert "/^\\/(?!\\/)/" not in code
    # Akış sözleşmesi
    for ev in ("'meta'", "'ping'", "'tool'", "'token'", "'done'", "'error'"):
        assert "case " + ev in code, f"NDJSON olayı işlenmiyor: {ev}"
    assert "AbortController" in code and "getReader" in code
    assert "sessionStorage" in code
    assert "/api/assistant/bootstrap" in code or "API + '/bootstrap'" in code


def test_assistant_js_served(client: TestClient):
    r = client.get("/static/assistant.js")
    assert r.status_code == 200
    assert "m108-ai-" in r.text
    assert "javascript" in (r.headers.get("content-type") or "")


def test_sw_cache_name_bumped():
    """Widget eklendiğinde CACHE_NAME artmalı — önbellekteki eski sayfalar
    assistant.js'i görmeden kalmasın."""
    src = (STATIC / "sw.js").read_text(encoding="utf-8")
    m = re.search(r"const CACHE_NAME = 'minerva108-v(\d+)'", src)
    assert m, "sw.js CACHE_NAME biçimi değişmiş"
    assert int(m.group(1)) >= 38, f"CACHE_NAME v{m.group(1)} — widget için en az v38 olmalı"


# ─── Render edilen sayfalar ──────────────────────────────────────────────────

def test_rendered_pages_include_widget(authed_client: TestClient):
    """Gerçek render: iç sayfalarda script etiketi var, script dengesi bozulmadı."""
    for path in ("/", "/items", "/pdks", "/drive", "/crm"):
        r = authed_client.get(path)
        assert r.status_code == 200, f"{path}: HTTP {r.status_code}"
        assert WIDGET_RE.search(r.text), f"{path}: assistant.js yok"
        assert r.text.count("<script") == r.text.count("</script>"), f"{path}: script dengesi"


def test_login_page_has_no_widget(client: TestClient):
    r = client.get("/login")
    assert r.status_code == 200
    assert "/static/assistant.js" not in r.text
