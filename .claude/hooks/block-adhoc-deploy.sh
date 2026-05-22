#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────
# Claude Code PreToolUse hook — Bash matcher.
#
# KURAL: Prod'a deploy YALNIZCA ./deploy.sh (veya make deploy) ile yapilir.
# deploy.sh test gate (pytest) + HTTP dogrulama (curl /login -> 200) icerir.
#
# Bu hook, deploy.sh'i atlayan ad-hoc komutlari engeller: prod sunucusuna
# (turhost / 136.144.251.26) ssh ile servisi restart eden, repoyu force-sync
# eden (git reset --hard) veya git pull ceken komutlar.
#
# Salt-okunur ssh (journalctl, systemctl status/is-active) SERBESTTIR.
# ./deploy.sh'in kendi ic ssh'i bu hook'a gorunmez (script alt-sureci),
# bu yuzden mesru deploy yolu engellenmez.
# ─────────────────────────────────────────────────────────────────────
set -euo pipefail

cmd="$(jq -r '.tool_input.command // ""' 2>/dev/null || echo '')"

# 1) Komut prod sunucusuna mi gidiyor? (ssh alias veya IP)
if printf '%s' "$cmd" | grep -Eq '(turhost|136\.144\.251\.26)'; then
  # 2) Deploy-niteliginde mutasyon komutu iceriyor mu?
  if printf '%s' "$cmd" | grep -Eq '(systemctl[[:space:]]+restart|git[[:space:]]+reset[[:space:]]+--hard|git[[:space:]]+pull)'; then
    jq -n '{
      hookSpecificOutput: {
        hookEventName: "PreToolUse",
        permissionDecision: "deny",
        permissionDecisionReason: "KURAL: Prod deploy yalnizca ./deploy.sh (veya make deploy) ile yapilir — test gate + HTTP dogrulama icerir. Ad-hoc ssh ile systemctl restart / git reset --hard / git pull engellendi. Mesru yol: ./deploy.sh   Acil hotfix: ./deploy.sh --skip-tests"
      }
    }'
    exit 0
  fi
fi

# Eslesme yok — komuta dokunma (cikti yok = normal akis)
exit 0
