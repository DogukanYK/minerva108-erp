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
  • 13.10.2026 yeniden tasarım: sağ alttaki yuvarlak düğme (FAB) YOK; üst
    çubukta ARIA combobox arama (`/api/assistant/search`), yan cevap paneli,
    /crm'de Ctrl+K CRM aramasına bırakılır.  Sol menüde `assistant.use` kapılı
    "Yapay Zekâ" bölümü her iç şablonda, hiçbir yetki bloğunun İÇİNDE değil
    (scripts/add_ai_nav.py idempotent).  /asistan sayfası static/asistan-page.js
    ile aynı güvenlik kurallarına uyar.
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
EXPECTED_INTERNAL = 30          # 29 + templates/asistan.html (13.10.2026)
NAV_SCRIPT = ROOT / "scripts" / "add_ai_nav.py"


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
    for path in ("/", "/items", "/pdks", "/drive", "/crm", "/asistan"):
        r = authed_client.get(path)
        assert r.status_code == 200, f"{path}: HTTP {r.status_code}"
        assert WIDGET_RE.search(r.text), f"{path}: assistant.js yok"
        assert r.text.count("<script") == r.text.count("</script>"), f"{path}: script dengesi"


def test_login_page_has_no_widget(client: TestClient):
    r = client.get("/login")
    assert r.status_code == 200
    assert "/static/assistant.js" not in r.text


# ─── Yeniden tasarım (13.10.2026): üst arama + yan panel, FAB yok ────────────

def _code(path):
    src = path.read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", code, flags=re.M)


def test_widget_is_top_search_and_side_panel_without_fab():
    code = _code(STATIC / "assistant.js")
    assert "'fab'" not in code and "spark" not in code and "m108-ai-fab" not in code
    assert "'combobox'" in code and "aria-activedescendant" in code and "'listbox'" in code
    assert "API + '/search?q='" in code and "'aria-modal'" in code
    assert "!ON_CRM" in code                                      # Ctrl+K /crm'de CRM aramasının
    assert "window.M108AI" in code and "createThread" in code
    assert "Günlük kalan" not in code                             # son kullanıcıya dolar sayacı yok
    assert "bi-stars" not in code


def test_asistan_page_js_is_safe():
    path = STATIC / "asistan-page.js"
    node = shutil.which("node")
    if node:
        r = subprocess.run([node, "--check", str(path)], capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr
    code = _code(path)
    assert code.lstrip().startswith("(function ()") and "'use strict'" in code
    assert "createElement" in code and "textContent" in code
    assert ".innerHTML" not in code and "insertAdjacentHTML" not in code and "document.write" not in code
    assert not re.search(r"['\"][^'\"]*\bbtn-", code), "btn-* sınıf adı yasak"


def _jinja_stack_at(text, pos):
    """`pos` noktasında açık kalan {% if %} koşulları (sidebar başından)."""
    stack = []
    for m in re.finditer(r"\{%-?\s*(if\b[^%]*|endif)\s*-?%\}", text[text.index('class="sidebar'):pos]):
        if m.group(1).startswith("if"):
            stack.append(m.group(1))
        elif stack:
            stack.pop()
    return stack


def test_ai_nav_section_in_every_internal_template_outside_other_guards():
    head = "{% if can('assistant','use') %}<div class=\"nav-section-label\""
    bad = []
    for f in sorted(TPL.glob("*.html")):
        src = f.read_text(encoding="utf-8")
        if not WIDGET_RE.search(src) or f.name == "asistan.html":
            continue
        i = src.find(head)
        if i == -1 or ">Yapay Zekâ</div>" not in src[i:i + 300]:
            bad.append(f"{f.name}: bölüm yok")
            continue
        block = src[i:src.index("{% endif %}", i)]
        for href in ('href="/asistan"', 'href="/asistan/belge"', 'href="/asistan/yaz"'):
            if href not in block:
                bad.append(f"{f.name}: {href} yok")
        if _jinja_stack_at(src, i):
            bad.append(f"{f.name}: bölüm başka bir yetki bloğunun içinde: {_jinja_stack_at(src, i)}")
        n = src.count('href="/asistan"')        # f-string içinde \" Python 3.9'da SyntaxError
        if n != 1:
            bad.append(f"{f.name}: /asistan bağlantısı {n} kez")
    assert not bad, "\n".join(bad)


def test_ai_nav_script_is_idempotent_and_skips_admin_block(tmp_path):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "templates").mkdir()
    shutil.copy(NAV_SCRIPT, tmp_path / "scripts" / NAV_SCRIPT.name)
    widget = '  <script src="/static/assistant.js?v=4" defer></script>\n'
    guarded = ("<style>.nav-section-label{}</style>\n" + widget + '<aside class="sidebar"><nav>\n'
               '      <a href="/items" class="nav-link-item">Ürünler</a>\n'
               "      {% if can('admin', 'view') %}\n"
               '      <div class="nav-section-label" style="margin-top:1rem;">Sistem</div>\n'
               '      <a href="/admin" class="nav-link-item">Yönetim</a>\n'
               "      {% endif %}\n    </nav></aside>\n")
    bare = (widget + '<aside class="sidebar"><nav>\n      <a href="/items" class="nav-link-item">Ürünler</a>\n'
            "    </nav></aside>\n")
    (tmp_path / "templates" / "a.html").write_text(guarded, encoding="utf-8")
    (tmp_path / "templates" / "b.html").write_text(bare, encoding="utf-8")
    (tmp_path / "templates" / "c.html").write_text(bare.replace(widget, ""), encoding="utf-8")   # widget'sız
    r1 = subprocess.run([sys.executable, str(tmp_path / "scripts" / NAV_SCRIPT.name)],
                        capture_output=True, text=True, timeout=30)
    assert r1.returncode == 0, r1.stderr
    a = (tmp_path / "templates" / "a.html").read_text(encoding="utf-8")
    b = (tmp_path / "templates" / "b.html").read_text(encoding="utf-8")
    opener = a.index("{% if can('assistant','use') %}")
    assert opener < a.index("{% if can('admin', 'view') %}") and not _jinja_stack_at(a, opener)
    assert 'style="margin-top:1rem;">Yapay Zekâ' in a                    # şablon CSS'i var → kısa stil
    assert b.index("Yapay Zekâ") < b.index("</nav>") and "text-transform:uppercase" in b
    assert "Yapay Zekâ" not in (tmp_path / "templates" / "c.html").read_text(encoding="utf-8")
    r2 = subprocess.run([sys.executable, str(tmp_path / "scripts" / NAV_SCRIPT.name)],
                        capture_output=True, text=True, timeout=30)
    assert r2.returncode == 0 and "Toplam: 0" in r2.stdout
    assert (tmp_path / "templates" / "a.html").read_text(encoding="utf-8") == a
