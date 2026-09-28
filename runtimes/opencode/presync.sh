#!/usr/bin/env bash
# runtime-opencode-presync — run by switch-runtime right before opencode starts, once at the end of
# install.sh, and by EnsureOnboarding on every os-server boot / config change (hermes-style).
set -euo pipefail

CONFIG_JSON="${CONFIG_JSON:-/root/config/config.json}"   # device/project config (source of truth)
OPENCODE_DIR="${OPENCODE_DIR:-/root/.opencode}"
WS_DIR="$OPENCODE_DIR/workspace"
ENV_FILE="$OPENCODE_DIR/.env"
OPENCODE_XDG_DIR="${OPENCODE_XDG_DIR:-/root/.config/opencode}"
OPENCODE_CONFIG="$OPENCODE_XDG_DIR/opencode.json"
SKILLS_DIR="$OPENCODE_XDG_DIR/skills"   # opencode global skill discovery root

# The campaign provider speaks the OpenAI RESPONSES API (not chat-completions — device-verified:
# {base}/chat/completions 404s, {base}/responses works).
DEFAULT_BASE_URL="https://campaign-api.autonomous.ai/api/v1/ai/v1"
DEFAULT_MODEL="Auto-AI"

log() { echo "[opencode-presync] $*"; }

command -v jq >/dev/null 2>&1 || { log "ERROR: jq not found — cannot sync opencode config" >&2; exit 1; }

mkdir -p "$WS_DIR" "$OPENCODE_DIR/attachments" "$OPENCODE_XDG_DIR"

dev() { jq -r ".${1} // empty" "$CONFIG_JSON" 2>/dev/null | tr -d '\n\r' || true; }

# The marker is written ONLY after a clean copy, so a failed migrate is retried on the next run.
MIGRATE_MARKER="$OPENCODE_DIR/.openclaw-migrated"
OC_WS="/root/.openclaw/workspace"
if [ ! -f "$MIGRATE_MARKER" ] && [ -d "$OC_WS" ]; then
  log "no migration marker — migrating persona/memory/skills from openclaw"
  # stop openclaw first so the copy doesn't race its live on-disk state (retry 3x, proceed
  # regardless — migrate is non-fatal).
  for attempt in 1 2 3; do
    systemctl stop openclaw 2>/dev/null || true
    if ! systemctl is-active --quiet openclaw; then
      log "openclaw stopped (attempt ${attempt}/3)"; break
    fi
    [ "$attempt" -eq 3 ] && log "WARN: openclaw still active after 3 attempts — continuing anyway"
    sleep 1
  done
  if (
    set -e
    for f in IDENTITY.md SOUL.md KNOWLEDGE.md HEARTBEAT.md MEMORY.md USER.md AGENTS.md; do
      if [ -f "$OC_WS/$f" ]; then
        cp -f "$OC_WS/$f" "$WS_DIR/$f"
        log "copied $f from openclaw"
      fi
    done
    # memory/ → workspace/memory — only when absent, so a re-run never clobbers local edits.
    if [ -d "$OC_WS/memory" ] && [ ! -d "$WS_DIR/memory" ]; then
      cp -a "$OC_WS/memory" "$WS_DIR/memory" && log "copied memory/ from openclaw"
    fi
    # Only when the destination is absent, so a re-run never clobbers local edits.
    if [ -d "$OC_WS/skills" ] && [ ! -d "$SKILLS_DIR" ]; then
      cp -a "$OC_WS/skills" "$SKILLS_DIR" && log "copied skills/ from openclaw → $SKILLS_DIR"
    fi
  ); then
    touch "$MIGRATE_MARKER"
    log "openclaw migration complete (marker written)"
  else
    log "WARN: openclaw migration failed — will retry on next presync run"
  fi
fi

LLM_BASE_URL="$(dev llm_base_url)"; [ -n "$LLM_BASE_URL" ] || LLM_BASE_URL="$DEFAULT_BASE_URL"
LLM_BASE_URL="${LLM_BASE_URL%/}"   # strip trailing slash
LLM_MODEL="$(dev llm_model)"; [ -n "$LLM_MODEL" ] || LLM_MODEL="$DEFAULT_MODEL"

EXISTING_MCP="$(jq -c '.mcp // {}' "$OPENCODE_CONFIG" 2>/dev/null || echo '{}')"
[ -n "$EXISTING_MCP" ] || EXISTING_MCP='{}'

log "write $OPENCODE_CONFIG (model=campaign/$LLM_MODEL base=$LLM_BASE_URL)"
jq -n \
  --arg base "$LLM_BASE_URL" \
  --arg model "$LLM_MODEL" \
  --argjson mcp "$EXISTING_MCP" '
{
  "$schema": "https://opencode.ai/config.json",
  "model": ("campaign/" + $model),
  "provider": {
    "campaign": {
      "npm": "@ai-sdk/openai",
      "name": "Autonomous campaign-api",
      "options": { "baseURL": $base, "apiKey": "{env:LLM_API_KEY}" },
      "models": { ($model): { "name": $model } }
    }
  }
}
+ (if ($mcp | length) > 0 then { "mcp": $mcp } else {} end)
' >"$OPENCODE_CONFIG.tmp"
mv "$OPENCODE_CONFIG.tmp" "$OPENCODE_CONFIG"

LLM_API_KEY="$(dev llm_api_key)"
log "write $ENV_FILE (key=$( [ -n "$LLM_API_KEY" ] && echo set || echo EMPTY ))"
umask 077
{
  echo "# Managed by runtime-opencode-presync — do not edit (synced from /root/config/config.json)."
  # OPENCODE_WS_TOKEN MUST match runtimes/opencode/constants.go Token; read by `os-server
  # opencode-gatewayd` (gatewayd package).
  echo "OPENCODE_WS_TOKEN=autonomous_opencode_token"
  echo "OPENCODE_PORT=18793"
  echo "OPENCODE_WORKSPACE=$WS_DIR"
  [ -n "$LLM_API_KEY" ] && echo "LLM_API_KEY=$LLM_API_KEY"
} >"$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
umask 022

log "channels: device-owned (telegram/slack/discord) — nothing to sync runtime-side"

# Guarded to interactive shells only (no leak into scripts/cron).
write_cli_login_env() {
  cat >/etc/profile.d/agent-cli-env.sh <<'PROFILE'
# Managed by os-server runtime presync — do not edit.
case "$-" in *i*) ;; *) return 2>/dev/null || exit 0 ;; esac
_rt="$(sed -n 's/.*"agent_runtime"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' /root/config/config.json 2>/dev/null | head -1)"
case "$_rt" in
  claudecode)
    if [ -f /root/.claudecode/.env ]; then set -a; . /root/.claudecode/.env; set +a; fi
    export IS_SANDBOX=1
    ;;
  codex)
    if [ -f /root/.codex/.env ]; then set -a; . /root/.codex/.env; set +a; fi
    export CODEX_HOME=/root/.codex
    ;;
  opencode)
    if [ -f /root/.opencode/.env ]; then set -a; . /root/.opencode/.env; set +a; fi
    ;;
esac
unset _rt
PROFILE
  chmod 0644 /etc/profile.d/agent-cli-env.sh
}
write_cli_login_env && log "wrote /etc/profile.d/agent-cli-env.sh (interactive CLI auto-login)"

log "done — opencode.json + env synced"
