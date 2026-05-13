#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Minerva108 — saatlik otomatik DB snapshot
#
# Bu script systemd timer tarafından her saat başı (07:00–19:00) tetiklenir.
# Var olan manuel backup endpoint'iyle (routers/backup.py) AYNI dizin ve
# AYNI pg_dump --format=custom çıktısını üretir; sadece otomatik olarak.
#
# Dosya adı pattern'ı: minerva_auto_YYYYMMDD-HHMM.dump
#   → admin paneli bunu "AUTO" etiketiyle gösterir, manuel'lerle karışmaz.
#
# Retention:
#   * Sadece minerva_auto_* dosyalarına dokunur — manuel backup'lar korunur.
#   * 30 günden eski auto-snapshot'lar silinir.
#
# Hata davranışı:
#   * pg_dump failure → systemd unit'i Failed olur, journalctl'de detay.
#   * Backup endpoint'i de bu dosyaları okur; restore aynı UI'dan yapılır.
#
# Kurulum: bkz. ops/snapshot/install.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

BACKUP_DIR="${MINERVA_BACKUP_DIR:-/var/www/minerva/backups}"
RETENTION_DAYS="${SNAPSHOT_RETENTION_DAYS:-30}"
ENV_FILE="${MINERVA_ENV_FILE:-/var/www/minerva/.env}"

# DATABASE_URL'i app'in env dosyasindan oku — systemd EnvironmentFile zaten
# yukleyebilir, bu manuel source idempotent (zaten varsa overwrite, sorun yok).
if [[ -f "$ENV_FILE" ]]; then
  # shellcheck disable=SC1090
  set -a; source "$ENV_FILE"; set +a
fi
if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "DATABASE_URL set edilmemis ($ENV_FILE veya systemd EnvironmentFile gerekli)" >&2
  exit 2
fi

# postgresql://user:pass@host:port/db formatını parçala
proto_removed="${DATABASE_URL#*://}"
userpass="${proto_removed%%@*}"
hostpath="${proto_removed#*@}"
PGUSER="${userpass%%:*}"
PGPASSWORD="${userpass#*:}"
hostport="${hostpath%%/*}"
PGDB="${hostpath#*/}"
PGDB="${PGDB%%\?*}"      # ?param=... varsa at
PGHOST="${hostport%%:*}"
PGPORT="${hostport#*:}"
[[ "$PGPORT" == "$hostport" ]] && PGPORT=5432
export PGUSER PGPASSWORD PGHOST PGPORT

# Backup dosyası
mkdir -p "$BACKUP_DIR"
TS="$(date -u +%Y%m%d-%H%M)"     # UTC — log timestamp'leriyle eşleşsin
OUT="$BACKUP_DIR/minerva_auto_${TS}.dump"

# pg_dump binary path'i (systemd PATH'i kısıtlı)
PG_DUMP=""
for cand in /usr/bin/pg_dump /usr/local/bin/pg_dump /opt/homebrew/bin/pg_dump; do
  [[ -x "$cand" ]] && PG_DUMP="$cand" && break
done
[[ -n "$PG_DUMP" ]] || { echo "pg_dump bulunamadı" >&2; exit 2; }

# Snapshot al — custom format (restore endpoint'iyle uyumlu)
"$PG_DUMP" --format=custom --no-owner --no-acl -d "$PGDB" -f "$OUT"
chmod 600 "$OUT"

SIZE_KB=$(du -k "$OUT" | cut -f1)
echo "[$(date -u -Iseconds)] snapshot ok: $OUT (${SIZE_KB} KB)"

# ─── Retention: 30 günden eski auto-snapshot'ları sil ──────────────────────
# -mtime +N → N gün önce değiştirilmiş (modify) dosyalar
DELETED=$(find "$BACKUP_DIR" -maxdepth 1 -type f -name 'minerva_auto_*.dump' -mtime +"$RETENTION_DAYS" -print -delete | wc -l | tr -d ' ')
[[ "$DELETED" -gt 0 ]] && echo "[$(date -u -Iseconds)] retention: $DELETED eski auto-snapshot silindi"

exit 0
