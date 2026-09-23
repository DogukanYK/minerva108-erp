#!/usr/bin/env bash
# Köprü/dondurma conf'larını üretir (Mac'te çalıştır):
#   ops/hetzner/old-server/render.sh <YENI_IP>
# Çıktı: ops/hetzner/old-server/out/  → eski sunucuya scp ile kopyalanır
#   scp -P 23422 out/minerva-freeze            turhost:/etc/nginx/sites-available/minerva-freeze
#   scp -P 23422 minerva-freeze-map.conf       turhost:/etc/nginx/conf.d/
#   scp -P 23422 out/minerva-bridge            turhost:/etc/nginx/sites-available/minerva-bridge
#   scp -P 23422 out/minerva-bridge-proxy.conf turhost:/etc/nginx/snippets/
set -euo pipefail
NEW_IP="${1:?kullanım: render.sh <YENI_IP>}"
[[ "$NEW_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || { echo "geçersiz IP: $NEW_IP" >&2; exit 1; }
D="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
mkdir -p "$D/out"
sed "s/__NEW_IP__/$NEW_IP/g" "$D/minerva-bridge.template"       > "$D/out/minerva-bridge"
sed "s/__NEW_IP__/$NEW_IP/g" "$D/minerva-bridge-proxy.template" > "$D/out/minerva-bridge-proxy.conf"
cp "$D/minerva-freeze.template" "$D/out/minerva-freeze"
echo "üretildi: $D/out/ (NEW_IP=$NEW_IP)"
