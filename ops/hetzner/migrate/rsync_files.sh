#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Disk üzerindeki state'i eski sunucudan yeni sunucuya kopyalar (DB dışı).
# YENİ sunucuda root olarak çalıştırılır; kaynak eski sunucu salt-okunur.
#
# Kullanım:
#   ops/hetzner/migrate/rsync_files.sh dry     # -n ile prova (hiçbir şey yazmaz)
#   ops/hetzner/migrate/rsync_files.sh initial  # Faz 1: ilk tam kopya (--delete YOK)
#   ops/hetzner/migrate/rsync_files.sh final    # Faz 3: son delta (--delete VAR)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

MODE="${1:?kullanım: rsync_files.sh dry|initial|final}"
OLD_HOST="136.144.251.26"
OLD_PORT="23422"
OLD_SSH_KEY="${OLD_SSH_KEY:-/root/.ssh/id_ed25519_minerva_deploy}"
SRC="root@${OLD_HOST}:/var/www/minerva"
DST="/var/www/minerva"

FLAGS=(-aHAX --numeric-ids --info=progress2 -e "ssh -p $OLD_PORT -i $OLD_SSH_KEY")
[[ "$MODE" == "dry" ]] && FLAGS+=(-n -v)
[[ "$MODE" == "final" ]] && FLAGS+=(--delete)

# Taşınacak dizin/dosyalar — kod REPO değil (git clone ile geliyor), sadece
# git-ignore'lu / untracked runtime state.
PATHS=(
  drive_files
  backups
  product_images
  _sozlesmeler
  data
  system_reports
  review_audio
  minerva108.db
  .env
)

for p in "${PATHS[@]}"; do
  echo "→ [$MODE] $p"
  rsync "${FLAGS[@]}" "$SRC/$p" "$DST/" || {
    rc=$?
    # kaynak yoksa (örn. review_audio boşsa rsync bazen 23 döner) — devam et, uyar
    echo "  ⚠ rsync exit=$rc ($p) — kaynakta yoksa/boşsa normal olabilir, kontrol et"
  }
done

echo
echo "✓ rsync [$MODE] tamamlandı."
[[ "$MODE" != "dry" ]] && chown -R minerva:minerva "$DST" && echo "  chown minerva:minerva uygulandı"
