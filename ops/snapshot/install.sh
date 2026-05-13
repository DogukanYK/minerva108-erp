#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Snapshot timer kurulum scripti — prod sunucusunda BİR KEZ çalıştırılır.
#
#   sudo bash ops/snapshot/install.sh
#
# Yapacağı şeyler:
#   1. minerva-snapshot.sh → /usr/local/bin/minerva-snapshot.sh (chmod 755)
#   2. .service ve .timer → /etc/systemd/system/
#   3. systemctl daemon-reload
#   4. systemctl enable --now minerva-snapshot.timer
#   5. Test snapshot al (manuel start)
#   6. Durum çıktısı
#
# Idempotent — birden fazla çalıştırılabilir, sadece son durumu garantiler.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ "$EUID" -ne 0 ]]; then
  echo "Bu script root olarak çalıştırılmalı (sudo bash $0)" >&2
  exit 1
fi

echo "==> 1) Script /usr/local/bin'e kopyalanıyor"
install -m 0755 "$SRC_DIR/minerva-snapshot.sh" /usr/local/bin/minerva-snapshot.sh

echo "==> 2) systemd unit'leri /etc/systemd/system'a kopyalanıyor"
install -m 0644 "$SRC_DIR/minerva-snapshot.service" /etc/systemd/system/minerva-snapshot.service
install -m 0644 "$SRC_DIR/minerva-snapshot.timer"   /etc/systemd/system/minerva-snapshot.timer

echo "==> 3) systemctl daemon-reload"
systemctl daemon-reload

echo "==> 4) Timer aktifleştiriliyor"
systemctl enable --now minerva-snapshot.timer

echo "==> 5) Test snapshot (manuel start; başarısızsa kuruluma devam etme)"
if systemctl start minerva-snapshot.service; then
  echo "   ✓ test snapshot başarılı"
else
  echo "   ✗ test snapshot HATALI — journalctl -u minerva-snapshot inceleyin"
  exit 2
fi

echo "==> 6) Özet"
echo "----------------------------------------"
systemctl status minerva-snapshot.timer --no-pager -n 5 || true
echo
echo "Bir sonraki snapshot zamanı:"
systemctl list-timers minerva-snapshot.timer --no-pager
echo
echo "Son snapshot:"
ls -lh "${MINERVA_BACKUP_DIR:-/var/www/minerva/backups}"/minerva_auto_*.dump 2>/dev/null | tail -3 || echo "(henüz yok)"
echo "----------------------------------------"
echo "Kurulum tamam.  Snapshot'lar admin panelinde 'AUTO' rozetiyle görünür."
