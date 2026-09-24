#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Faz 1 — Hetzner sunucusu ilk kurulum (cloud-init'ten SONRA, elle çalıştırılır).
# root olarak, YENİ sunucuda:  bash ops/hetzner/setup.sh
#
# Yapar: PGDG repo + PostgreSQL 17 + client, cluster locale kontrolü, DB/rol
# oluşturma (minerva_db + minerva_test, owner minerva_user), postgresql.conf
# ayarları, nginx site conf'larının kopyalanması, systemd unit'lerin kurulumu,
# certbot snap.  Uygulama kodu/venv/rsync AYRI adımlar (RUNBOOK Faz 1).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../ops/hetzner (repo içinden çalıştırılmalı)

step() { echo -e "\n\033[1;34m━━━ $* ━━━\033[0m"; }

step "1/8 — PGDG apt deposu + PostgreSQL 17"
apt install -y postgresql-common
YES=yes /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh -y || true
apt update
apt install -y postgresql-17 postgresql-client-17

step "2/8 — Cluster locale kontrolü (C.UTF-8 bekleniyor)"
sudo -u postgres psql -tAc "SELECT datcollate FROM pg_database WHERE datname='template1';" | grep -qi "C.UTF-8\|C.utf8" \
  && echo "  ✓ lc_collate C.UTF-8" \
  || echo "  ⚠ lc_collate C.UTF-8 DEĞİL — initdb'yi kontrol et (RUNBOOK'ta not var)"

step "3/8 — postgresql.conf ayarları (RAM'e göre: shared_buffers %25, effective_cache_size %75)"
PGCONF=$(sudo -u postgres psql -tAc "SHOW config_file;")
cp "$PGCONF" "${PGCONF}.orig-$(date +%F)"
MEM_MB=$(awk '/MemTotal/{print int($2/1024)}' /proc/meminfo)
SB_MB=$(( MEM_MB / 4 )); EC_MB=$(( MEM_MB * 3 / 4 ))
echo "  RAM ${MEM_MB} MB → shared_buffers ${SB_MB}MB, effective_cache_size ${EC_MB}MB"
python3 - "$PGCONF" "$SB_MB" "$EC_MB" <<'PYEOF'
import re, sys
path, sb, ec = sys.argv[1], sys.argv[2], sys.argv[3]
settings = {
    "shared_buffers": f"{sb}MB",
    "effective_cache_size": f"{ec}MB",
    "work_mem": "16MB",
    "maintenance_work_mem": "256MB",
    "wal_compression": "on",
    "password_encryption": "scram-sha-256",
}
text = open(path).read()
for k, v in settings.items():
    pattern = re.compile(rf"^#?\s*{k}\s*=.*$", re.MULTILINE)
    line = f"{k} = {v}"
    if pattern.search(text):
        text = pattern.sub(line, text, count=1)
    else:
        text += f"\n{line}\n"
open(path, "w").write(text)
print("  ✓ postgresql.conf güncellendi:", ", ".join(settings))
PYEOF

step "4/8 — Rol + veritabanları"
if [ -z "${MINERVA_PW:-}" ]; then
  echo "  minerva_user parolasını gir (eski .env DATABASE_URL ile AYNI olmalı — SECRET_KEY gibi bu da değişmemeli):"
  read -rs MINERVA_PW; echo
fi
sudo -u postgres psql -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='minerva_user') THEN CREATE ROLE minerva_user LOGIN PASSWORD '${MINERVA_PW}'; END IF; END \$\$;"
sudo -u postgres psql -c "ALTER ROLE minerva_user PASSWORD '${MINERVA_PW}';"
for db in minerva_db minerva_test minerva_prova; do
  sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$db'" | grep -q 1 || \
    sudo -u postgres psql -c "CREATE DATABASE $db OWNER minerva_user LOCALE 'C.UTF-8' TEMPLATE template0;"
  echo "  ✓ $db hazır"
done
systemctl restart postgresql

step "5/8 — nginx site + rate-limit + realip conf"
cp "$D/nginx/minerva" /etc/nginx/sites-available/minerva
cp "$D/nginx/minerva-rate-limits.conf" /etc/nginx/conf.d/minerva-rate-limits.conf
cp "$D/nginx/minerva-realip-bridge.conf" /etc/nginx/conf.d/minerva-realip-bridge.conf.DISABLED
echo "  ⚠ minerva-realip-bridge.conf .DISABLED olarak kopyalandı — YALNIZ cutover köprüsü"
echo "    açıkken 'mv' ile .conf yapılıp etkinleştirilecek (RUNBOOK Faz 3)."
ln -sfn /etc/nginx/sites-available/minerva /etc/nginx/sites-enabled/minerva
echo "  ⚠ nginx -t şimdi BAŞARISIZ olacak — /etc/letsencrypt henüz kopyalanmadı (RUNBOOK Faz 1 rsync adımı)."

step "6/8 — systemd: minerva.service (sertleştirilmiş)"
cp "$D/systemd/minerva.service" /etc/systemd/system/minerva.service
mkdir -p /etc/systemd/system/minerva-snapshot.service.d /etc/systemd/system/minerva-backup.service.d
cp "$D/systemd/timer-user-override.conf" /etc/systemd/system/minerva-snapshot.service.d/user.conf
cp "$D/systemd/timer-user-override.conf" /etc/systemd/system/minerva-backup.service.d/user.conf
cp "$D/../snapshot/minerva-snapshot.service" /etc/systemd/system/minerva-snapshot.service
cp "$D/../snapshot/minerva-snapshot.timer" /etc/systemd/system/minerva-snapshot.timer
install -m 0755 "$D/../snapshot/minerva-snapshot.sh" /usr/local/bin/minerva-snapshot.sh
cp "$D/../backup/minerva-backup.service" /etc/systemd/system/minerva-backup.service
cp "$D/../backup/minerva-backup.timer" /etc/systemd/system/minerva-backup.timer
install -m 0755 "$D/../backup/minerva-backup.sh" /usr/local/bin/minerva-backup.sh
systemctl daemon-reload
echo "  ⚠ timer'lar ENABLE edilmedi — cutover'da 'systemctl enable --now minerva-snapshot.timer minerva-backup.timer'"

step "7/8 — certbot (snap)"
snap install core; snap refresh core
snap install --classic certbot || true
ln -sf /snap/bin/certbot /usr/bin/certbot
echo "  ✓ certbot snap kuruldu — sertifikalar rsync ile /etc/letsencrypt'e kopyalanacak (apt certbot KURULMADI)"

step "8/9 — sudoers: minerva kullanıcısına dar kapsamlı deploy izni"
cat > /etc/sudoers.d/minerva-deploy <<'SUDOEOF'
# deploy.sh, minerva kullanıcısı olarak SSH ile geldiğinde SADECE servisi
# yeniden başlatıp durumunu okuyabilsin — başka hiçbir komut için sudo yok.
minerva ALL=(root) NOPASSWD: /usr/bin/systemctl restart minerva, /usr/bin/systemctl status minerva, /usr/bin/journalctl -u minerva -n 25 --no-pager
SUDOEOF
chmod 0440 /etc/sudoers.d/minerva-deploy
visudo -cf /etc/sudoers.d/minerva-deploy && echo "  ✓ sudoers doğrulandı"

step "9/9 — Özet"
cat <<SUMMARY

Sıradaki elle adımlar (RUNBOOK Faz 1):
  1. minerva kullanıcısı için SSH deploy key üret, GitHub'a read-only deploy key ekle
  2. sudo -u minerva git clone git@github.com:DogukanYK/minerva108-erp.git /var/www/minerva  (veya mevcut boş dizine init+pull)
  3. Python 3.10 (canlıyla aynı) — uv ile, systemd ProtectHome yüzünden /home DEĞİL /opt altına:
       mkdir -p /opt/uv-python && chown minerva:minerva /opt/uv-python
       sudo -u minerva -H bash -c 'curl -LsSf https://astral.sh/uv/install.sh | sh'
       sudo -u minerva -H env UV_PYTHON_INSTALL_DIR=/opt/uv-python /home/minerva/.local/bin/uv python install 3.10
       sudo -u minerva -H env UV_PYTHON_INSTALL_DIR=/opt/uv-python /home/minerva/.local/bin/uv venv --seed -p 3.10 /var/www/minerva/venv
  4. sudo -u minerva /var/www/minerva/venv/bin/pip install -r /var/www/minerva/requirements.txt
  5. ops/hetzner/migrate/rsync_files.sh initial   (.env, /etc/letsencrypt dahil — ayrı, aşağıda)
  6. rsync -e "ssh -p 23422 -i \$OLD_SSH_KEY" -a root@136.144.251.26:/etc/letsencrypt/ /etc/letsencrypt/
  7. .env'e DISABLE_SCHEDULER=true ekle
  8. nginx -t && systemctl reload nginx
  9. systemctl enable --now minerva
  10. Test paketi burada KOŞULMAZ (conftest sabit yerel test şifresi kullanır) — smoke test: RUNBOOK Faz 2
SUMMARY
