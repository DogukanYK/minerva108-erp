#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Dump/restore sonrası satır-sayımı doğrulaması.
# YENİ sunucuda çalıştırılır; eski sunucuya SSH tüneliyle bağlanıp her tabloda
# count(*) karşılaştırır + alembic_version + en yüksek sequence değerlerini
# raporlar.  Fark varsa exit 1 ile başarısız olur (cutover script'i buna göre durur).
#
# Kullanım: verify_counts.sh <target_db> [old_ssh_key]
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

TARGET_DB="${1:?kullanım: verify_counts.sh <target_db>}"
OLD_HOST="136.144.251.26"
OLD_PORT="23422"
OLD_SSH_KEY="${2:-/root/.ssh/id_ed25519_minerva_deploy}"
TUNNEL_LOCAL_PORT=15433   # dump_restore.sh'ın tünelinden (15432) ayrı port

ssh -f -N -o ExitOnForwardFailure=yes -i "$OLD_SSH_KEY" -p "$OLD_PORT" \
    -L "${TUNNEL_LOCAL_PORT}:127.0.0.1:5432" "root@${OLD_HOST}"
TUNNEL_PID=$(pgrep -f "L ${TUNNEL_LOCAL_PORT}:127.0.0.1:5432" | head -1)
trap '[[ -n "${TUNNEL_PID:-}" ]] && kill "$TUNNEL_PID" 2>/dev/null || true' EXIT

TABLES=(
  "admin_audit_log"
  "alembic_version"
  "app_setting"
  "crm_activity"
  "crm_attachment"
  "crm_company"
  "crm_contact"
  "crm_deal"
  "crm_entity_tag"
  "crm_field_def"
  "crm_field_value"
  "crm_integration_state"
  "crm_saved_view"
  "crm_stage"
  "crm_tag"
  "crm_task"
  "crm_wa_template"
  "deliveries"
  "delivery_items"
  "distributor_prices"
  "distributors"
  "drive_collection"
  "drive_collection_file"
  "drive_collection_folder"
  "drive_file"
  "drive_folder"
  "duplicate_item_decisions"
  "influencer_account"
  "influencer_activity"
  "influencer_address"
  "influencer_affiliate"
  "influencer_application"
  "influencer_benchmark"
  "influencer_campaign"
  "influencer_collab"
  "influencer_collab_stage_log"
  "influencer_content"
  "influencer_creator"
  "influencer_file"
  "influencer_metric_snapshot"
  "influencer_payout"
  "influencer_referral"
  "influencer_shipment"
  "influencer_tier"
  "influencer_token"
  "inventory"
  "items"
  "pdks_employees"
  "pdks_events"
  "pdks_holidays"
  "pdks_leave_requests"
  "pdks_leaves"
  "pdks_requests"
  "pdks_schedules"
  "product_return_items"
  "product_returns"
  "product_reviews"
  "production_history"
  "push_subscriptions"
  "quotation_items"
  "quotations"
  "recipe_ingredients"
  "recipes"
  "retention_sample_checks"
  "retention_sample_movements"
  "retention_samples"
  "review_invites"
  "sample_analyses"
  "sample_analysis_ingredients"
  "shopify_orders"
  "shopify_sync_state"
  "stock_order_flags"
  "stock_snapshot"
  "supplier_prices"
  "suppliers"
  "system_event"
  "transactions"
  "undo_log"
  "users"
)

FAIL=0
printf "%-30s %10s %10s\n" "tablo" "eski" "yeni"
printf "%-30s %10s %10s\n" "-----" "----" "----"
for t in "${TABLES[@]}"; do
  OLD_N=$(PGPASSWORD="${OLD_PG_PASSWORD:?OLD_PG_PASSWORD gerekli}" psql -h 127.0.0.1 -p "$TUNNEL_LOCAL_PORT" -U minerva_user -d minerva_db -tAc "SELECT count(*) FROM \"$t\";" 2>/dev/null || echo "ERR")
  NEW_N=$(PGPASSWORD="${NEW_PG_PASSWORD:?NEW_PG_PASSWORD gerekli}" psql -h 127.0.0.1 -U minerva_user -d "$TARGET_DB" -tAc "SELECT count(*) FROM \"$t\";" 2>/dev/null || echo "ERR")
  MARK=""
  if [[ "$OLD_N" != "$NEW_N" ]]; then MARK=" ← FARK"; FAIL=1; fi
  printf "%-30s %10s %10s%s\n" "$t" "$OLD_N" "$NEW_N" "$MARK"
done

echo
echo "→ alembic_version:"
OLD_AV=$(PGPASSWORD="$OLD_PG_PASSWORD" psql -h 127.0.0.1 -p "$TUNNEL_LOCAL_PORT" -U minerva_user -d minerva_db -tAc "SELECT version_num FROM alembic_version;" | tr -d ' ')
NEW_AV=$(PGPASSWORD="$NEW_PG_PASSWORD" psql -h 127.0.0.1 -U minerva_user -d "$TARGET_DB" -tAc "SELECT version_num FROM alembic_version;" | tr -d ' ')
echo "  eski=$OLD_AV yeni=$NEW_AV"
[[ "$OLD_AV" == "$NEW_AV" ]] || { echo "  ✗ alembic_version FARKLI"; FAIL=1; }

echo
echo "→ sequence son değerleri (yeni ≥ eski olmalı, restore sırasında setval ile taşınır):"
PGPASSWORD="$NEW_PG_PASSWORD" psql -h 127.0.0.1 -U minerva_user -d "$TARGET_DB" -tAc \
  "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname='public' ORDER BY 1;" | head -5
echo "  (tam liste için: psql -d $TARGET_DB -c '\\x' -c 'SELECT * FROM pg_sequences;')"

if [[ "$FAIL" -eq 1 ]]; then
  echo; echo "✗ DOĞRULAMA BAŞARISIZ — farkları incele, cutover'ı durdur."; exit 1
else
  echo; echo "✓ Tüm tablo sayımları ve alembic_version eşleşiyor."
fi
