#!/bin/bash
# Minerva108 — Günlük PostgreSQL yedek
# systemd timer (minerva-backup.timer) tarafından her gün 03:00 UTC'de çalıştırılır.
# Son 30 günü tutar, eskisini siler.  Hata olursa exit code != 0.
set -euo pipefail

BACKUP_DIR="/var/www/minerva/backups"
RETENTION_DAYS=30
mkdir -p "$BACKUP_DIR"

# .env'den DATABASE_URL yükle
set -a
source /var/www/minerva/.env
set +a

TS=$(date -u +'%Y%m%d-%H%M%S')
OUT="$BACKUP_DIR/minerva_auto_${TS}.dump"

echo "[$(date -u -Iseconds)] Yedek başlıyor: $OUT"
pg_dump "$DATABASE_URL" --format=custom --no-owner --no-acl -f "$OUT"
chmod 600 "$OUT"
SIZE=$(stat -c%s "$OUT")
echo "[$(date -u -Iseconds)] Tamam ($SIZE bayt)"

# 30 günden eski auto-yedekleri sil (manuel/upload/pre-restore yedekler kalır)
find "$BACKUP_DIR" -name 'minerva_auto_*.dump' -mtime +$RETENTION_DAYS -delete
echo "[$(date -u -Iseconds)] Eski yedekler temizlendi (>$RETENTION_DAYS gün)"
