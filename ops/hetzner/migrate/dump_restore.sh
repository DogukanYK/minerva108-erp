#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# PostgreSQL 14 (Turhost) → PostgreSQL 17 (Hetzner) — dump/restore.
# YENİ sunucuda, root olarak çalıştırılır (postgresql-client-17 buradadır).
#
# Kullanım:
#   ops/hetzner/migrate/dump_restore.sh prova     # T-3 prova: prova_db'ye restore eder
#   ops/hetzner/migrate/dump_restore.sh cutover   # gerçek: minerva_db'ye restore eder
#
# Önkoşul: bu makineden Turhost'a SSH tüneli açılabilmeli (id_ed25519_minerva).
# pg_dump BURADAKİ (PG17) binary'yle, SSH tünel üzerinden ESKİ (PG14) sunucuya
# bağlanır — resmi öneri: her zaman hedef/yeni majörün pg_dump'ı kullanılır.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

MODE="${1:?kullanım: dump_restore.sh prova|cutover}"
[[ "$MODE" == "prova" || "$MODE" == "cutover" ]] || { echo "MODE prova ya da cutover olmalı" >&2; exit 1; }

OLD_HOST="136.144.251.26"
OLD_PORT="23422"
OLD_SSH_KEY="${OLD_SSH_KEY:-/root/.ssh/id_ed25519_minerva_deploy}"   # yeni sunucuda ayrı, salt-okunur deploy key
TUNNEL_LOCAL_PORT=15432
OLD_PG_PORT=5432
OLD_PG_DB="minerva_db"
OLD_PG_USER="minerva_user"        # eski .env'deki DATABASE_URL kullanıcısı

TARGET_DB="minerva_db"
[[ "$MODE" == "prova" ]] && TARGET_DB="minerva_prova"

DUMP_FILE="/root/migrate-$(date -u +%Y%m%dT%H%M%SZ).dump"

echo "→ [$MODE] SSH tüneli açılıyor ($OLD_HOST:$OLD_PORT → localhost:$TUNNEL_LOCAL_PORT)"
ssh -f -N -o ExitOnForwardFailure=yes -i "$OLD_SSH_KEY" -p "$OLD_PORT" \
    -L "${TUNNEL_LOCAL_PORT}:127.0.0.1:${OLD_PG_PORT}" "root@${OLD_HOST}"
TUNNEL_PID=$(pgrep -f "L ${TUNNEL_LOCAL_PORT}:127.0.0.1:${OLD_PG_PORT}" | head -1)
trap '[[ -n "${TUNNEL_PID:-}" ]] && kill "$TUNNEL_PID" 2>/dev/null || true' EXIT

echo "→ pg_dump (PG17 client, eski PG14 sunucudan) → $DUMP_FILE"
PGPASSWORD="${OLD_PG_PASSWORD:?OLD_PG_PASSWORD env değişkeni gerekli (eski .env DATABASE_URL şifresi)}" \
  pg_dump -h 127.0.0.1 -p "$TUNNEL_LOCAL_PORT" -U "$OLD_PG_USER" -d "$OLD_PG_DB" \
    --format=custom --no-owner --no-acl -f "$DUMP_FILE"
echo "  ✓ dump: $(du -h "$DUMP_FILE" | cut -f1)"

if [[ "$MODE" == "cutover" ]]; then
  echo "→ [cutover] $TARGET_DB DROP + yeniden oluşturuluyor (C.UTF-8, minerva_user owner)"
  sudo -u postgres psql -tAc "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$TARGET_DB' AND pid <> pg_backend_pid();" || true
  sudo -u postgres psql -c "DROP DATABASE IF EXISTS $TARGET_DB;"
  sudo -u postgres psql -c "CREATE DATABASE $TARGET_DB OWNER minerva_user LOCALE 'C.UTF-8' TEMPLATE template0;"
else
  echo "→ [prova] $TARGET_DB (varsa) DROP + yeniden oluşturuluyor"
  sudo -u postgres psql -tAc "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname='$TARGET_DB' AND pid <> pg_backend_pid();" || true
  sudo -u postgres psql -c "DROP DATABASE IF EXISTS $TARGET_DB;"
  sudo -u postgres psql -c "CREATE DATABASE $TARGET_DB OWNER minerva_user LOCALE 'C.UTF-8' TEMPLATE template0;"
fi

echo "→ pg_restore → $TARGET_DB"
PGPASSWORD="${NEW_PG_PASSWORD:?NEW_PG_PASSWORD env değişkeni gerekli (yeni sunucu minerva_user şifresi)}" \
  pg_restore -h 127.0.0.1 -U minerva_user -d "$TARGET_DB" --no-owner --no-acl -j 4 "$DUMP_FILE"

echo "→ ANALYZE"
PGPASSWORD="${NEW_PG_PASSWORD}" psql -h 127.0.0.1 -U minerva_user -d "$TARGET_DB" -c "ANALYZE;"

echo "✓ Tamam. Dump dosyası: $DUMP_FILE (elle sil: rm $DUMP_FILE)"
echo "  Sayım doğrulaması için: ops/hetzner/migrate/verify_counts.sh $TARGET_DB"
