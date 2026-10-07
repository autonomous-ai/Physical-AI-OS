#!/usr/bin/env bash
# runtime-hermes-presync: run by switch-runtime before hermes starts; owns config.yaml model wiring.
# Static structure is re-asserted every run so it self-heals after `hermes setup --reset`.
set -euo pipefail
CONFIG_JSON="${CONFIG_JSON:-/root/config/config.json}"
HERMES_DIR="${HERMES_DIR:-/root/.hermes}"
ENV_FILE="$HERMES_DIR/.env"
CONFIG_YAML="$HERMES_DIR/config.yaml"
log() { echo "[hermes-presync] $*"; }

LLM_CONFIG_MODE="$(jq -r '.llm_config_mode // empty' "$CONFIG_JSON" 2>/dev/null || true)"

# ── 0. SKILLS ──
# Restore OpenClaw-imported skills only when the dir is empty (factory reset wipes it; rerunning
# `claw migrate --skill-conflict rename` every switch would pile up duplicates).
HERMES_BIN="${HERMES_BIN:-/usr/local/bin/hermes}"
IMPORTS_DIR="$HERMES_DIR/skills/openclaw-imports"
if [ "$LLM_CONFIG_MODE" != runtime ] && { [ ! -d "$IMPORTS_DIR" ] || [ -z "$(ls -A "$IMPORTS_DIR" 2>/dev/null)" ]; }; then
  if [ -x "$HERMES_BIN" ] && [ -d /root/.openclaw ]; then
    log "openclaw-imported skills missing — restoring via claw migrate"
    "$HERMES_BIN" claw migrate --preset full --overwrite --skill-conflict rename --yes --migrate-secrets \
      || log "WARN: claw migrate failed (non-fatal)"
  fi
fi

if ! command -v yq >/dev/null 2>&1; then
  log "ERROR: yq not found — cannot ensure config.yaml structure" >&2
  exit 1
fi

touch "$CONFIG_YAML"

if [ "$LLM_CONFIG_MODE" != runtime ]; then
# `hermes setup --reset` leaves .model / .custom_providers as scalars; coerce before indexing.
[ "$(yq '.model | tag' "$CONFIG_YAML" 2>/dev/null)" = "!!map" ] || yq -i '.model = {}' "$CONFIG_YAML"
[ "$(yq '.custom_providers | tag' "$CONFIG_YAML" 2>/dev/null)" = "!!seq" ] || yq -i '.custom_providers = []' "$CONFIG_YAML"

# ── 1. STRUCTURE (idempotent) ──
log "ensure config.yaml model + custom_providers structure"
yq -i '
  .model.provider = "custom:autonomous"
  | .model.default = "Auto-AI"
  | .custom_providers[0].name     = "autonomous"
  | .custom_providers[0].key_env  = "AUTONOMOUS_API_KEY"
  | .custom_providers[0].api_mode = "anthropic_messages"
  | .custom_providers[0].base_url = (.custom_providers[0].base_url // "https://campaign-api.autonomous.ai/api/v1/ai")
' "$CONFIG_YAML"

# ── 1b. AUXILIARY VISION + AGENT IMAGE INPUT (always overwrite) ──
[ "$(yq '.auxiliary | tag' "$CONFIG_YAML" 2>/dev/null)" = "!!map" ] || yq -i '.auxiliary = {}' "$CONFIG_YAML"
[ "$(yq '.agent | tag' "$CONFIG_YAML" 2>/dev/null)" = "!!map" ] || yq -i '.agent = {}' "$CONFIG_YAML"
log "ensure config.yaml auxiliary.vision + agent.image_input_mode"
yq -i '
  .auxiliary.vision = {
    "provider": "custom:autonomous",
    "model": "qwen/qwen3.6-plus",
    "timeout": 120,
    "download_timeout": 30,
    "extra_body": {}
  }
  | .agent.image_input_mode = "auto"
' "$CONFIG_YAML"

fi

# ── 1b2. TERMINAL CWD ──
# Hermes finds AGENTS.md only by walking up from an absolute configured cwd; `.` never resolves.
log "ensure config.yaml terminal.cwd (makes AGENTS.md discoverable)"
[ "$(yq '.terminal | tag' "$CONFIG_YAML" 2>/dev/null)" = "!!map" ] || yq -i '.terminal = {}' "$CONFIG_YAML"
yq -i '.terminal.cwd = "'"$HERMES_DIR"'"' "$CONFIG_YAML"

# ── 1c. APPROVALS OFF ──
# style="double" is required: PyYAML (YAML 1.1) parses bare off as False, silently keeping prompts on.
[ "$(yq '.approvals | tag' "$CONFIG_YAML" 2>/dev/null)" = "!!map" ] || yq -i '.approvals = {}' "$CONFIG_YAML"
log "ensure config.yaml approvals.mode=off (no command-approval prompts)"
yq -i '.approvals.mode = "off" | .approvals.mode style="double"' "$CONFIG_YAML"

# ── 2. DYNAMIC (config.json wins) ──
# "Auto-AI" is a campaign-api alias; a custom base_url must use the operator's llm_model instead.
if [ "$LLM_CONFIG_MODE" != runtime ]; then
LLM_BASE_URL="$(jq -r '.llm_base_url // empty' "$CONFIG_JSON" 2>/dev/null || true)"
LLM_MODEL="$(jq -r '.llm_model // empty' "$CONFIG_JSON" 2>/dev/null || true)"

case "$LLM_BASE_URL" in
  ""|*campaign-api.autonomous.ai*)
    # Hermes emits cache_control for a custom provider only when the model declares prompt_caching.
    yq -i '.custom_providers[0].models["Auto-AI"].prompt_caching = true' "$CONFIG_YAML"
    log "custom_providers[0].models.Auto-AI.prompt_caching = true (cache markers on)"
    # 1h TTL outlives typical 10-20 min gaps between voice turns (Hermes accepts only "5m" | "1h").
    yq -i '.prompt_caching.cache_ttl = "1h"' "$CONFIG_YAML"
    log "prompt_caching.cache_ttl = 1h"
    ;;
  *)
    if [ -n "$LLM_MODEL" ]; then
      yq -i ".model.default = \"$LLM_MODEL\"" "$CONFIG_YAML"
      log "model.default = $LLM_MODEL (custom brain — Auto-AI only resolves at campaign-api)"
    else
      log "WARNING: custom base_url with no llm_model — leaving the Auto-AI alias, turns will 400"
    fi
    ;;
esac

# Explicit OS ownership must restore the selected model even on the campaign endpoint.
if [ "$LLM_CONFIG_MODE" = os ] && [ -n "$LLM_MODEL" ]; then
  MODEL="$LLM_MODEL" yq -i '.model.default = strenv(MODEL)' "$CONFIG_YAML"
fi

if [ -n "$LLM_BASE_URL" ]; then
  yq -i ".custom_providers[0].base_url = \"$LLM_BASE_URL\"" "$CONFIG_YAML"
  log "custom_providers[0].base_url = $LLM_BASE_URL"
fi

fi

# Upsert each non-empty config.json field into .env; other vars are left untouched.
sync_env() {
  local key="$1" var="$2" val
  val="$(jq -r ".${key} // empty" "$CONFIG_JSON" 2>/dev/null || true)"
  [ -n "$val" ] || return 0
  sed -i "/^${var}=/d" "$ENV_FILE"
  [ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >>"$ENV_FILE"
  echo "${var}=${val}" >>"$ENV_FILE"
  log "${var} synced"
}

if [ "$LLM_CONFIG_MODE" != runtime ]; then
  sync_env llm_api_key AUTONOMOUS_API_KEY
fi
sync_env telegram_bot_token TELEGRAM_BOT_TOKEN
sync_env telegram_user_id   TELEGRAM_ALLOWED_USERS
sync_env slack_bot_token    SLACK_BOT_TOKEN
sync_env slack_app_token    SLACK_APP_TOKEN
sync_env slack_user_id      SLACK_ALLOWED_USERS
sync_env discord_bot_token  DISCORD_BOT_TOKEN
sync_env discord_guild_id   DISCORD_GUILD_ID
sync_env discord_user_id    DISCORD_ALLOWED_USERS
sync_env whatsapp_user_id   WHATSAPP_ALLOWED_USERS
sync_env bluebubbles_server_url   BLUEBUBBLES_SERVER_URL
sync_env bluebubbles_password     BLUEBUBBLES_PASSWORD
sync_env bluebubbles_user_address BLUEBUBBLES_ALLOWED_USERS

# Multi-line caller context cannot live in .env (systemd EnvironmentFile); caller_context_file_fallback.py reads this file.
BB_CALLER_CTX_FILE="$HERMES_DIR/bluebubbles_caller_context.txt"
BB_CALLER_CTX_VAL="$(jq -r '.bluebubbles_caller_context // empty' "$CONFIG_JSON" 2>/dev/null || true)"
if [ -n "$BB_CALLER_CTX_VAL" ]; then
  mkdir -p "$HERMES_DIR"
  umask 077
  printf '%s' "$BB_CALLER_CTX_VAL" >"$BB_CALLER_CTX_FILE"
  chmod 600 "$BB_CALLER_CTX_FILE"
  log "bluebubbles_caller_context written to ${BB_CALLER_CTX_FILE}"
else
  rm -f "$BB_CALLER_CTX_FILE"
  log "bluebubbles_caller_context empty — removed ${BB_CALLER_CTX_FILE} if present"
fi

# The plugin binds 127.0.0.1 by default, which the Mac cannot reach; seed the LAN IP unless pinned.
if [ -n "$(jq -r '.bluebubbles_server_url // empty' "$CONFIG_JSON" 2>/dev/null || true)" ]; then
  LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i=1; i<=NF; i++) if ($i=="src") { print $(i+1); exit }}')"
  [ -z "$LAN_IP" ] && LAN_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

  if [ -n "$LAN_IP" ]; then
    # Refresh WEBHOOK_HOST when unset or a bare IPv4 no longer bound here; leave hostnames/tunnels alone.
    # `|| true`: grep's exit 1 would abort under set -e on a fresh .env.
    CURRENT_HOST="$(grep -E '^BLUEBUBBLES_WEBHOOK_HOST=' "$ENV_FILE" 2>/dev/null | tail -1 | cut -d= -f2- || true)"
    NEED_REFRESH=false
    REFRESH_REASON=""
    if [ -z "$CURRENT_HOST" ]; then
      NEED_REFRESH=true
      REFRESH_REASON="unset"
    elif printf '%s' "$CURRENT_HOST" | grep -qE '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$'; then
      if ! ip -4 -o addr show 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | grep -qFx "$CURRENT_HOST"; then
        NEED_REFRESH=true
        REFRESH_REASON="stale ${CURRENT_HOST}"
      fi
    fi
    if $NEED_REFRESH; then
      sed -i "/^BLUEBUBBLES_WEBHOOK_HOST=/d" "$ENV_FILE"
      [ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >>"$ENV_FILE"
      echo "BLUEBUBBLES_WEBHOOK_HOST=${LAN_IP}" >>"$ENV_FILE"
      log "BLUEBUBBLES_WEBHOOK_HOST set to ${LAN_IP} (${REFRESH_REASON})"
    fi
  else
    log "WARN: BlueBubbles configured but no LAN IP detected — webhook will bind loopback and Mac cannot reach it"
  fi

  if ! grep -q '^BLUEBUBBLES_WEBHOOK_PORT=' "$ENV_FILE" 2>/dev/null; then
    [ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >>"$ENV_FILE"
    echo "BLUEBUBBLES_WEBHOOK_PORT=8645" >>"$ENV_FILE"
    log "BLUEBUBBLES_WEBHOOK_PORT defaulted to 8645 (not pinned in .env)"
  fi

  # Customer-facing channel: allow all senders unless the operator pinned the flag.
  if ! grep -q '^BLUEBUBBLES_ALLOW_ALL_USERS=' "$ENV_FILE" 2>/dev/null; then
    [ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >>"$ENV_FILE"
    echo "BLUEBUBBLES_ALLOW_ALL_USERS=true" >>"$ENV_FILE"
    log "BLUEBUBBLES_ALLOW_ALL_USERS defaulted to true (customer-facing default; not pinned in .env)"
  fi

  # Without a home channel Hermes nags every customer with a /sethome prompt.
  if ! grep -q '^BLUEBUBBLES_HOME_CHANNEL=' "$ENV_FILE" 2>/dev/null; then
    OP_HANDLE="$(jq -r '.bluebubbles_user_address // empty' "$CONFIG_JSON" 2>/dev/null || true)"
    if [ -n "$OP_HANDLE" ]; then
      [ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >>"$ENV_FILE"
      echo "BLUEBUBBLES_HOME_CHANNEL=any;-;${OP_HANDLE}" >>"$ENV_FILE"
      log "BLUEBUBBLES_HOME_CHANNEL defaulted to any;-;${OP_HANDLE} (from bluebubbles_user_address)"
    else
      log "WARN: BlueBubbles configured but bluebubbles_user_address is empty — HOME_CHANNEL left unset (bot will nag customers with /sethome prompt)"
    fi
  fi
fi

# ── 3. API SERVER KEY ── must equal runtimes/hermes/constants.go APIKey.
EXPECTED_API_KEY="hermes-local-api-key"
sed -i "/^API_SERVER_KEY=/d" "$ENV_FILE"
[ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >>"$ENV_FILE"
echo "API_SERVER_KEY=${EXPECTED_API_KEY}" >>"$ENV_FILE"
log "API_SERVER_KEY enforced (${EXPECTED_API_KEY})"
