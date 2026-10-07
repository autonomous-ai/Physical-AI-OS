#!/usr/bin/env bash
# runtime-codex-presync — syncs codex config, env and one-time migration; never restarts the unit.
set -euo pipefail

CONFIG_JSON="${CONFIG_JSON:-/root/config/config.json}"   # device/project config (source of truth)
CODEX_DIR="${CODEX_DIR:-/root/.codex}"
WS_DIR="$CODEX_DIR/workspace"
SKILLS_DIR="$CODEX_DIR/skills"   # codex NATIVE skill discovery root ($CODEX_HOME/skills)
ENV_FILE="$CODEX_DIR/.env"
CODEX_CONFIG="$CODEX_DIR/config.toml"
AUTH_JSON="$CODEX_DIR/auth.json"

# Codex only speaks the Responses API and appends /responses, so the base needs the /v1 suffix.
DEFAULT_BASE_URL="https://campaign-api.autonomous.ai/api/v1/ai/v1"
DEFAULT_MODEL="Auto-AI"

log() { echo "[codex-presync] $*"; }

command -v jq >/dev/null 2>&1 || { log "ERROR: jq not found — cannot sync codex config" >&2; exit 1; }

mkdir -p "$WS_DIR" "$CODEX_DIR/attachments"

dev() { jq -r ".${1} // empty" "$CONFIG_JSON" 2>/dev/null | tr -d '\n\r' || true; }

# The marker is written ONLY after a clean copy, so a failed migrate is retried on the next run.
MIGRATE_MARKER="$CODEX_DIR/.openclaw-migrated"
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
    # skills/ → $CODEX_DIR/skills: codex never scans workspace/skills.
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

LLM_CONFIG_MODE="$(dev llm_config_mode)"

# auth.json present = ChatGPT subscription: use codex's built-in provider and omit OPENAI_API_KEY,
# which would conflict with ChatGPT auth.
if [ "$LLM_CONFIG_MODE" = runtime ] || { [ "$LLM_CONFIG_MODE" != os ] && [ -f "$AUTH_JSON" ]; }; then
  SUBSCRIPTION_MODE=1
  log "subscription mode (auth.json present) — omitting custom provider + OPENAI_API_KEY"
else
  SUBSCRIPTION_MODE=0
  log "api-key mode"
fi

if [ "$LLM_CONFIG_MODE" != runtime ]; then
# The head is regenerated each run; [mcp_servers.*] tables (owned by mcp.go) are preserved.
LLM_BASE_URL="$(dev llm_base_url)"; [ -n "$LLM_BASE_URL" ] || LLM_BASE_URL="$DEFAULT_BASE_URL"
LLM_BASE_URL="${LLM_BASE_URL%/}"
case "$LLM_BASE_URL" in
  */v1) ;;
  *) LLM_BASE_URL="$LLM_BASE_URL/v1" ;;
esac
LLM_MODEL="$(dev llm_model)"; [ -n "$LLM_MODEL" ] || LLM_MODEL="$DEFAULT_MODEL"

if [ "$SUBSCRIPTION_MODE" = 1 ]; then
  log "write $CODEX_CONFIG (subscription mode — built-in provider/model)"
  cat >"$CODEX_CONFIG.tmp" <<TOML
# Managed by runtime-codex-presync — head regenerated from /root/config/config.json.
# Subscription mode (auth.json present): no model / model_provider — codex uses
# its BUILT-IN default provider + model with ChatGPT auth.
# All [mcp_servers.*] tables are preserved wherever they appear
# (os-server runtimes/codex/mcp.go owns those entries).
approval_policy = "never"
sandbox_mode = "danger-full-access"
TOML
else
  log "write $CODEX_CONFIG (model=$LLM_MODEL base_url=$LLM_BASE_URL)"
  cat >"$CODEX_CONFIG.tmp" <<TOML
# Managed by runtime-codex-presync — head regenerated from /root/config/config.json.
# All [mcp_servers.*] tables are preserved wherever they appear
# (os-server runtimes/codex/mcp.go owns those entries).
model = "$LLM_MODEL"
model_provider = "autonomous"
approval_policy = "never"
sandbox_mode = "danger-full-access"

[model_providers.autonomous]
name = "Autonomous campaign-api"
# Codex appends /responses to base_url (Responses API only — the chat wire was
# removed upstream ~2/2026). ⚠️ VERIFY ON DEVICE: campaign-api must serve
# {base}/responses.
base_url = "$LLM_BASE_URL"
env_key = "OPENAI_API_KEY"
wire_api = "responses"
TOML
fi
# Position-independent: go-toml may sort [mcp_servers] before [model_providers], so capture only
# mcp_servers tables wherever they appear.
if [ -f "$CODEX_CONFIG" ]; then
  MCP_BLOCK="$(awk '
    /^\[mcp_servers[].]/ { capture=1; print; next }
    capture && /^\[/     { capture=0 }
    capture              { print }
  ' "$CODEX_CONFIG")"
  if [ -n "$MCP_BLOCK" ]; then
    log "preserving existing [mcp_servers] tables"
    printf '\n%s\n' "$MCP_BLOCK" >>"$CODEX_CONFIG.tmp"
  fi
fi
mv "$CODEX_CONFIG.tmp" "$CODEX_CONFIG"
fi

LLM_API_KEY="$(dev llm_api_key)"
if [ "$SUBSCRIPTION_MODE" = 1 ]; then
  log "write $ENV_FILE (OPENAI_API_KEY omitted — subscription mode)"
else
  log "write $ENV_FILE (key=$( [ -n "$LLM_API_KEY" ] && echo set || echo EMPTY ))"
fi
umask 077
{
  echo "# Managed by runtime-codex-presync — do not edit (synced from /root/config/config.json)."
  # CODEX_WS_TOKEN MUST match runtimes/codex/constants.go Token; read by `os-server codex-gatewayd`
  # (gatewayd package).
  echo "CODEX_WS_TOKEN=autonomous_codex_token"
  echo "CODEX_PORT=18792"
  echo "CODEX_APP_SERVER=1"
  echo "CODEX_HOME=$CODEX_DIR"
  echo "CODEX_WORKSPACE=$WS_DIR"
  if [ "$SUBSCRIPTION_MODE" != 1 ] && [ -n "$LLM_API_KEY" ]; then
    echo "OPENAI_API_KEY=$LLM_API_KEY"
  fi
} >"$ENV_FILE.tmp"
mv "$ENV_FILE.tmp" "$ENV_FILE"
umask 022

log "channels: none supported under codex — nothing to sync"

# Sources the active runtime's .env into interactive login shells only.
write_cli_login_env() {
  cat >"${CLI_PROFILE_PATH:-/etc/profile.d/agent-cli-env.sh}" <<'PROFILE'
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
esac
unset _rt
PROFILE
  chmod 0644 "${CLI_PROFILE_PATH:-/etc/profile.d/agent-cli-env.sh}"
}
write_cli_login_env && log "wrote /etc/profile.d/agent-cli-env.sh (interactive CLI auto-login)"

log "done — codex config.toml + env synced"
