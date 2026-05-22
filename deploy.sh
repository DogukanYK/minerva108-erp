#!/bin/bash
# ──────────────────────────────────────────────────────────────────────
# Minerva108 — robust deployment script
# Force-syncs the Turhost server with this Mac's main branch.
# Verbose, fail-fast, surfaces remote errors instead of swallowing them.
# ──────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── Config ────────────────────────────────────────────────────────────
SERVER_IP="136.144.251.26"
SERVER_PORT="23422"                          # ← Turhost custom SSH port (not 22)
SERVER_USER="root"
SERVER_PATH="/var/www/minerva"
SERVICE_NAME="minerva"
SSH_KEY="$HOME/.ssh/id_ed25519_minerva"      # Dedicated key created during setup
SSH_TIMEOUT=15
PROD_URL="https://srv.minervaims.com"        # Public URL — post-deploy HTTP doğrulaması

# Always operate from the repo root, regardless of where the script is invoked
cd "$(dirname "$0")"
PYTEST="./.venv/bin/pytest"

# ── Pretty output helpers ─────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; DIM='\033[2m'; NC='\033[0m'

step() { echo -e "\n${BLUE}${BOLD}━━━ $* ━━━${NC}"; }
ok()   { echo -e "${GREEN}✓${NC} $*"; }
warn() { echo -e "${YELLOW}⚠${NC} $*"; }
fail() { echo -e "${RED}✗ $*${NC}" >&2; exit 1; }
info() { echo -e "${DIM}  $*${NC}"; }

# Catch unexpected errors with line number
trap 'fail "Script line $LINENO sırasında hata oluştu — yukarıdaki çıktıyı kontrol edin."' ERR

# ── CLI args ──────────────────────────────────────────────────────────
# --skip-tests : SADECE acil hotfix için — pre-deploy test gate'ini atlar.
SKIP_TESTS=0
for arg in "$@"; do
  case "$arg" in
    --skip-tests) SKIP_TESTS=1 ;;
    -h|--help)
      echo "Kullanım: ./deploy.sh [--skip-tests]"
      echo "  (varsayılan)  Önce tüm pytest suite çalışır; geçmezse deploy İPTAL."
      echo "  --skip-tests  Test gate'ini atlar — yalnızca acil hotfix durumunda."
      exit 0 ;;
    *) fail "Bilinmeyen argüman: $arg  (bkz. ./deploy.sh --help)" ;;
  esac
done

# ──────────────────────────────────────────────────────────────────────
# STEP 1 — Pre-deploy test gate  (KURAL: testler geçmeden deploy yok)
# ──────────────────────────────────────────────────────────────────────
step "Step 1/5 — Pre-deploy test gate (pytest)"

if [ "$SKIP_TESTS" -eq 1 ]; then
  warn "TEST GATE ATLANIYOR (--skip-tests) — bu yalnızca acil hotfix içindir."
  warn "Deploy bittikten sonra prod'u MUTLAKA elle kontrol edin."
elif [ ! -x "$PYTEST" ]; then
  fail "$PYTEST bulunamadı. Önce 'make install' çalıştırın (acil durumda: ./deploy.sh --skip-tests)."
else
  info "Tüm pytest suite çalışıyor (auth + RBAC + backup + pages) — push'tan önce kapı kontrolü..."
  if ! "$PYTEST" tests/ -q; then
    fail "TESTLER BAŞARISIZ — deploy iptal edildi. Önce testleri düzeltin. (Acil hotfix: ./deploy.sh --skip-tests)"
  fi
  ok "Tüm testler geçti — deploy'a devam"
fi

# ──────────────────────────────────────────────────────────────────────
# STEP 2 — Local commit & push
# ──────────────────────────────────────────────────────────────────────
step "Step 2/5 — Yerel commit & GitHub push"

git add .

# Decide whether anything is staged worth committing
if git diff --cached --quiet; then
  warn "Yerelde commit edilecek değişiklik yok — mevcut HEAD push edilecek."
else
  read -p "Commit mesajını girin (boş bırakırsanız 'feat: auto-deploy update'): " commit_msg
  commit_msg="${commit_msg:-feat: auto-deploy update}"
  git commit -m "$commit_msg"
  ok "Local commit oluşturuldu — $(git rev-parse --short HEAD)"
fi

info "Local HEAD: $(git rev-parse --short HEAD) on $(git rev-parse --abbrev-ref HEAD)"

if ! git push origin main; then
  fail "GitHub push başarısız. Internet bağlantısı / SSH key / yetki kontrolü yapın."
fi
ok "GitHub'a push tamamlandı"

# ──────────────────────────────────────────────────────────────────────
# STEP 3 — Pre-flight: SSH connectivity check
# ──────────────────────────────────────────────────────────────────────
step "Step 3/5 — Sunucu erişim testi"

# Quick connection sanity-check — fails loud if SSH is broken
if ! ssh -p "$SERVER_PORT" \
       -i "$SSH_KEY" \
       -o ConnectTimeout=$SSH_TIMEOUT \
       -o StrictHostKeyChecking=accept-new \
       -o BatchMode=yes \
       "$SERVER_USER@$SERVER_IP" "echo READY" > /tmp/_minerva_ssh_test 2>&1; then
  echo -e "${DIM}-- SSH error output --${NC}"
  cat /tmp/_minerva_ssh_test
  rm -f /tmp/_minerva_ssh_test
  fail "$SERVER_USER@$SERVER_IP:$SERVER_PORT adresine bağlanılamadı. Sunucu / SSH key / firewall kontrol edin."
fi
rm -f /tmp/_minerva_ssh_test
ok "SSH bağlantısı çalışıyor — $SERVER_USER@$SERVER_IP:$SERVER_PORT"

# ──────────────────────────────────────────────────────────────────────
# STEP 4 — Remote force-sync + service restart (verbose, fail-fast)
# ──────────────────────────────────────────────────────────────────────
step "Step 4/5 — Sunucuda force-sync + servis restart"
echo -e "${DIM}-- Sunucudan canlı çıktı --${NC}"

# Heredoc with quoted EOF prevents local variable expansion;
# `bash -s` ensures we're in a real bash session on the remote.
# All remote commands run with `set -euo pipefail` so they fail loud and
# their non-zero exit code is propagated back through SSH to this script.
if ! ssh -p "$SERVER_PORT" \
         -i "$SSH_KEY" \
         -o ConnectTimeout=$SSH_TIMEOUT \
         -o ServerAliveInterval=30 \
         "$SERVER_USER@$SERVER_IP" \
         "REMOTE_PATH='$SERVER_PATH' SERVICE_NAME='$SERVICE_NAME' bash -s" <<'REMOTE_EOF'
set -euo pipefail

echo "→ Host: $(hostname) · User: $(whoami) · UTC: $(date -u +'%Y-%m-%dT%H:%M:%SZ')"
echo

# ── Directory check — explicit, friendly error ──
if [ ! -d "$REMOTE_PATH" ]; then
  echo "✗ HATA: Klasör bulunamadı: $REMOTE_PATH" >&2
  exit 2
fi
if [ ! -d "$REMOTE_PATH/.git" ]; then
  echo "✗ HATA: $REMOTE_PATH bir git deposu değil (.git klasörü yok)" >&2
  exit 3
fi

cd "$REMOTE_PATH"
echo "→ Working directory: $(pwd)"
echo "→ Eski commit:        $(git rev-parse --short HEAD) — $(git log -1 --format='%s' | cut -c1-60)"
echo

# ── FORCE SYNC: discards any local modifications on the server ──
# Equivalent to "make this server identical to origin/main".
# Local hand-edits to tracked files (e.g. database.py) are wiped.
# Untracked files (e.g. minerva108.db) are preserved.
echo "→ git fetch --all --prune"
git fetch --all --prune

echo
echo "→ git reset --hard origin/main"
git reset --hard origin/main

echo
echo "→ Yeni commit:        $(git rev-parse --short HEAD) — $(git log -1 --format='%s' | cut -c1-60)"
echo

# ── Dependency sync — always run on every deploy (idempotent, fast no-op) ──
if [ -f "$REMOTE_PATH/requirements.txt" ]; then
  echo "→ pip install -r requirements.txt (eksik paketler varsa kurar)"
  if   [ -f "$REMOTE_PATH/venv/bin/pip" ]; then
    "$REMOTE_PATH/venv/bin/pip"  install -q -r "$REMOTE_PATH/requirements.txt" && echo "  ✓ venv senkron"
  elif [ -f "$REMOTE_PATH/.venv/bin/pip" ]; then
    "$REMOTE_PATH/.venv/bin/pip" install -q -r "$REMOTE_PATH/requirements.txt" && echo "  ✓ .venv senkron"
  elif command -v pip3 >/dev/null 2>&1; then
    pip3 install -q -r "$REMOTE_PATH/requirements.txt" && echo "  ✓ pip3 senkron"
  else
    echo "  ⚠ pip bulunamadı, dependency install atlandı"
  fi
  echo
fi

# ── Service restart ──
echo "→ systemctl restart $SERVICE_NAME"
if ! systemctl restart "$SERVICE_NAME"; then
  echo "✗ HATA: systemctl restart başarısız" >&2
  echo "-- son 25 log satırı --" >&2
  journalctl -u "$SERVICE_NAME" -n 25 --no-pager >&2 || true
  exit 4
fi

# Brief settle period for the service to come back up
sleep 2

echo
echo "→ Servis durumu:"
if systemctl is-active --quiet "$SERVICE_NAME"; then
  echo "  ✓ active (running)"
else
  echo "  ✗ servis aktif değil!" >&2
  systemctl status "$SERVICE_NAME" --no-pager --lines=20 >&2 || true
  exit 5
fi
echo
systemctl status "$SERVICE_NAME" --no-pager --lines=5 || true

echo
echo "✓ Sunucu güncellemesi tamamlandı."
REMOTE_EOF
then
  echo -e "${DIM}-- Remote çıktısı sonu --${NC}"
  fail "Remote komutları başarısız oldu — yukarıdaki sunucu çıktısını inceleyin."
fi

echo -e "${DIM}-- Remote çıktısı sonu --${NC}"
ok "Remote force-sync ve servis restart başarılı"

# ──────────────────────────────────────────────────────────────────────
# STEP 5 — Final verification: service active + real HTTP response
# ──────────────────────────────────────────────────────────────────────
step "Step 5/5 — Bağımsız sağlık kontrolü + HTTP doğrulama"

# 5a — Servis gerçekten aktif mi? (remote step'ten bağımsız ikinci kontrol)
if ssh -p "$SERVER_PORT" -i "$SSH_KEY" -o ConnectTimeout=10 \
       "$SERVER_USER@$SERVER_IP" "systemctl is-active --quiet $SERVICE_NAME"; then
  ok "minerva servisi aktif çalışıyor"
else
  fail "Servis aktif DEĞİL! Loglar: ssh turhost 'journalctl -u $SERVICE_NAME -n 40 --no-pager'"
fi

# 5b — HTTP doğrulama: prod gerçekten istek karşılıyor mu?
# Servis yeni restart oldu (~3-4 sn) — 200 alana kadar birkaç deneme yapılır.
info "HTTP kontrolü: $PROD_URL/login"
http_code="000"
for attempt in 1 2 3 4 5; do
  http_code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 "$PROD_URL/login" 2>/dev/null || echo 000)"
  if [ "$http_code" = "200" ]; then
    ok "Prod /login sayfası HTTP 200 döndü (deneme $attempt) — uygulama gerçekten ayakta"
    break
  fi
  info "Deneme $attempt/5: HTTP $http_code — 3 sn sonra tekrar..."
  sleep 3
done
if [ "$http_code" != "200" ]; then
  fail "Prod DOĞRULANAMADI: $PROD_URL/login → HTTP $http_code. Servis 'active' ama uygulama cevap vermiyor — ssh turhost 'journalctl -u $SERVICE_NAME -n 40 --no-pager'"
fi

# ──────────────────────────────────────────────────────────────────────
# Summary
# ──────────────────────────────────────────────────────────────────────
echo
TEST_NOTE=$([ "$SKIP_TESTS" -eq 1 ] && echo "ATLANDI (--skip-tests)" || echo "geçti")
echo -e "${GREEN}${BOLD}🎉 Deployment tamamlandı ve doğrulandı.${NC}"
echo -e "   ${DIM}Server:    ${NC} $SERVER_USER@$SERVER_IP:$SERVER_PORT"
echo -e "   ${DIM}Path:      ${NC} $SERVER_PATH"
echo -e "   ${DIM}Service:   ${NC} $SERVICE_NAME"
echo -e "   ${DIM}Local:     ${NC} $(git rev-parse --short HEAD) on $(git rev-parse --abbrev-ref HEAD)"
echo -e "   ${DIM}Doğrulama: ${NC} testler $TEST_NOTE · servis active · $PROD_URL/login → HTTP 200"
echo
