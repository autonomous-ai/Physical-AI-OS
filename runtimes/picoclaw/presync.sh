#!/usr/bin/env bash
# runtime-picoclaw-presync — run by switch-runtime right before picoclaw starts (and once at the end
# of install.sh).
set -euo pipefail

CONFIG_JSON="${CONFIG_JSON:-/root/config/config.json}"          # device/project config (secret source of truth)
PICO_DIR="${PICO_DIR:-/root/.picoclaw}"
PICO_CONFIG="$PICO_DIR/config.json"             # picoclaw's own config (structure)
PICO_SECURITY="$PICO_DIR/.security.yml"         # picoclaw's secrets
PICO_BIN="${PICO_BIN:-/usr/local/bin/picoclaw}"

# MUST equal runtimes/picoclaw/constants.go Token — os-server connects to the pico gateway with this
# bearer token, so the gateway must be seeded with the same value.
PICO_TOKEN="darren_pico_token"
DEFAULT_API_BASE="https://campaign-api.autonomous.ai/api/v1/ai/v1"

log() { echo "[picoclaw-presync] $*"; }

for tool in jq yq; do
  command -v "$tool" >/dev/null 2>&1 || { log "ERROR: $tool not found — cannot patch picoclaw config" >&2; exit 1; }
done

# config.json must exist (install.sh runs `picoclaw onboard` first).
[ -f "$PICO_CONFIG" ] || { log "ERROR: $PICO_CONFIG missing (onboard not run?)" >&2; exit 1; }
touch "$PICO_SECURITY"

jq_edit() { local f="$1"; shift; local tmp; tmp="$(mktemp)"; jq "$@" "$f" >"$tmp" && mv "$tmp" "$f"; }
dev() { jq -r ".${1} // empty" "$CONFIG_JSON" 2>/dev/null || true; }

# Gate migrate on a marker, not skills emptiness: onboard always seeds built-in skills.
MIGRATE_MARKER="$PICO_DIR/.openclaw-migrated"
if [ ! -f "$MIGRATE_MARKER" ]; then
  if [ -x "$PICO_BIN" ] && [ -d /root/.openclaw ]; then
    log "no migration marker — migrating persona/memory/skills from openclaw"
    # stop openclaw first so migrate doesn't race its live on-disk state (retry 3x, proceed
    # regardless — migrate is non-fatal).
    for attempt in 1 2 3; do
      systemctl stop openclaw 2>/dev/null || true
      if ! systemctl is-active --quiet openclaw; then
        log "openclaw stopped (attempt ${attempt}/3)"; break
      fi
      [ "$attempt" -eq 3 ] && log "WARN: openclaw still active after 3 attempts — continuing anyway"
      sleep 1
    done
    # --workspace-only: migrate ONLY the workspace (persona/memory/skills), NOT config.json —
    # converting openclaw.json into a picoclaw config produces a broken config (wrong
    # model/channel/gateway shape).
    if HOME=/root "$PICO_BIN" migrate --workspace-only --force; then
      WS="$PICO_DIR/workspace"
      OC_WS="/root/.openclaw/workspace"
      if [ -f "$OC_WS/HEARTBEAT.md" ]; then
        cp -f "$OC_WS/HEARTBEAT.md" "$WS/HEARTBEAT.md"
        log "copied HEARTBEAT.md from openclaw"
      fi
      if [ -f "$OC_WS/KNOWLEDGE.md" ]; then
        cp -f "$OC_WS/KNOWLEDGE.md" "$WS/KNOWLEDGE.md"
        log "copied KNOWLEDGE.md from openclaw"
      fi
      # `picoclaw migrate` does NOT carry IDENTITY.md over, so copy openclaw's in manually.
      rm -f "$WS/AGENT.md"
      if [ -f "$OC_WS/IDENTITY.md" ]; then
        cp -f "$OC_WS/IDENTITY.md" "$WS/IDENTITY.md"
        log "copied IDENTITY.md from openclaw"
      fi
      touch "$MIGRATE_MARKER"
      log "migration complete — marker written ($MIGRATE_MARKER)"
    else
      log "WARN: picoclaw migrate failed (non-fatal) — will retry next switch"
    fi
  else
    log "no migration marker but openclaw absent or picoclaw binary missing — skipping migrate"
  fi
fi

LLM_CONFIG_MODE="$(dev llm_config_mode)"
jq_edit "$PICO_CONFIG" '.agents.defaults.restrict_to_workspace = false | .agents.defaults.allow_read_outside_workspace = true'
if [ "$LLM_CONFIG_MODE" != runtime ]; then
# Route the default agent at the autonomous (campaign-api) provider.
log "ensure agents.defaults model wiring"
jq_edit "$PICO_CONFIG" '
    .agents.defaults.restrict_to_workspace        = false
  | .agents.defaults.allow_read_outside_workspace = true
  | .agents.defaults.provider                     = "anthropic-messages"
  | .agents.defaults.model_name                   = "autonomous"
  | .agents.defaults.image_model                  = "autonomous_vision"
'

log "ensure model_list autonomous entry"
jq_edit "$PICO_CONFIG" --arg ab "$DEFAULT_API_BASE" '
  ( [ (.model_list // [])[] | select(.model_name == "autonomous") | .api_base ]
    | map(select(. != null and . != "")) | .[0] ) as $existing
  | .model_list = ( (.model_list // []) | map(select(.model_name != "autonomous")) )
      + [ { model_name: "autonomous", provider: "anthropic-messages",
            model: "Auto-AI", api_base: ($existing // $ab) } ]
'

log "ensure model_list autonomous_vision entry"
jq_edit "$PICO_CONFIG" --arg ab "$DEFAULT_API_BASE" '
  ( [ (.model_list // [])[] | select(.model_name == "autonomous_vision") | .api_base ]
    | map(select(. != null and . != "")) | .[0] ) as $existing
  | .model_list = ( (.model_list // []) | map(select(.model_name != "autonomous_vision")) )
      + [ { model_name: "autonomous_vision", provider: "anthropic-messages",
            model: "qwen/qwen3.6-plus", api_base: ($existing // $ab) } ]
'

fi

log "ensure gateway server block"
jq_edit "$PICO_CONFIG" '
  .gateway = { host: "localhost", port: 18790, hot_reload: false, log_level: "warn" }
'

log "ensure channel_list.pico structure (always enabled)"
jq_edit "$PICO_CONFIG" '
    .channel_list.pico.enabled              = true
  | .channel_list.pico.type                 = "pico"
  | .channel_list.pico.reasoning_channel_id = (.channel_list.pico.reasoning_channel_id // "")
  | .channel_list.pico.group_trigger        = (.channel_list.pico.group_trigger // {})
  | .channel_list.pico.typing               = (.channel_list.pico.typing // {})
  | .channel_list.pico.placeholder          = (.channel_list.pico.placeholder // {enabled: true})
  | .channel_list.pico.settings             = (.channel_list.pico.settings // {
        max_connections: 100, ping_interval: 30, read_timeout: 60,
        streaming: {enabled: false}, write_timeout: 10, allow_token_query: true })
'

# Other channels: assert structure but DEFAULT enabled=false; §2 flips enabled=true only when the
# credentials exist in config.json.
ensure_channel_struct() {
  local ch="$1" type="$2"
  jq_edit "$PICO_CONFIG" --arg ch "$ch" --arg ty "$type" '
      .channel_list[$ch].type                 = $ty
    | .channel_list[$ch].enabled              = (.channel_list[$ch].enabled // false)
    | .channel_list[$ch].reasoning_channel_id = (.channel_list[$ch].reasoning_channel_id // "")
    | .channel_list[$ch].group_trigger        = (.channel_list[$ch].group_trigger // {})
    | .channel_list[$ch].typing               = (.channel_list[$ch].typing // {})
    | .channel_list[$ch].placeholder          = (.channel_list[$ch].placeholder // {enabled: false})
  '
}
log "ensure channel_list structure (telegram/discord/slack/whatsapp)"
ensure_channel_struct telegram telegram
ensure_channel_struct discord  discord
ensure_channel_struct slack    slack
ensure_channel_struct whatsapp whatsapp

# enable_channel flips config.json; sec_* write to .security.yml via yq's strenv() so values are
# passed through the environment (no shell-quoting / yaml- injection risk).
enable_channel() { jq_edit "$PICO_CONFIG" --arg ch "$1" '.channel_list[$ch].enabled = true'; }
sec_set() {
  CH="$1" K="$2" V="$3" yq -i \
    '.channel_list[strenv(CH)].settings[strenv(K)] = strenv(V) | .channel_list[strenv(CH)].settings style="flow"' \
    "$PICO_SECURITY"
}
sec_allow_from() {
  local ch="$1" id="$2"
  [ -n "$id" ] || return 0
  CH="$ch" ID="$id" yq -i \
    '.channel_list[strenv(CH)].settings.allow_from = [strenv(ID)] | .channel_list[strenv(CH)].settings style="flow"' \
    "$PICO_SECURITY"
}

if [ "$LLM_CONFIG_MODE" != runtime ]; then
LLM_BASE_URL="$(dev llm_base_url)"
if [ -n "$LLM_BASE_URL" ]; then
  base="${LLM_BASE_URL%/}"
  case "$base" in */v1) : ;; *) base="${base}/v1" ;; esac
  jq_edit "$PICO_CONFIG" --arg ab "$base" '
    .model_list = ((.model_list // []) | map(if (.model_name == "autonomous" or .model_name == "autonomous_vision") then .api_base = $ab else . end))
  '
  log "model_list[autonomous,autonomous_vision].api_base = $base"

  # "Auto-AI" is a campaign-api alias; any other host rejects it, so use the operator's model.
  case "$LLM_BASE_URL" in
    *campaign-api.autonomous.ai*) ;;
    *)
      LLM_MODEL="$(dev llm_model)"
      if [ -n "$LLM_MODEL" ]; then
        jq_edit "$PICO_CONFIG" --arg m "$LLM_MODEL" '
          .model_list = ((.model_list // []) | map(if .model_name == "autonomous" then .model = $m else . end))
        '
        log "model_list[autonomous].model = $LLM_MODEL (custom brain — Auto-AI only resolves at campaign-api)"
      else
        log "WARNING: custom llm_base_url with no llm_model — leaving the Auto-AI alias, turns will fail"
      fi
      ;;
  esac
fi

# Explicit OS ownership restores the selected model on every provider endpoint.
LLM_MODEL="$(dev llm_model)"
if [ "$LLM_CONFIG_MODE" = os ] && [ -n "$LLM_MODEL" ]; then
  jq_edit "$PICO_CONFIG" --arg m "$LLM_MODEL" '
    .model_list |= map(if .model_name == "autonomous" then .model = $m else . end)
  '
fi

# LLM api key → .security.yml model_list."autonomous:0".api_keys.
LLM_API_KEY="$(dev llm_api_key)"
if [ -n "$LLM_API_KEY" ]; then
  KEY="$LLM_API_KEY" yq -i '.model_list["autonomous:0"].api_keys = [strenv(KEY)] | .model_list["autonomous_vision:0"].api_keys = [strenv(KEY)]' "$PICO_SECURITY"
  log "security model_list autonomous:0 + autonomous_vision:0 api_keys synced"
fi

fi

# pico bearer token (always) — must match constants.go Token.
PT="$PICO_TOKEN" yq -i '.channel_list.pico.settings.token = strenv(PT) | .channel_list.pico.settings style="flow"' "$PICO_SECURITY"
log "security channel_list.pico.settings.token synced"

TG_TOKEN="$(dev telegram_bot_token)"; TG_USER="$(dev telegram_user_id)"
if [ -n "$TG_TOKEN" ]; then
  enable_channel telegram
  sec_set telegram token "$TG_TOKEN"
  sec_allow_from telegram "$TG_USER"
  log "telegram enabled + token synced"
else
  log "telegram: no telegram_bot_token in config.json — left disabled"
fi

# Discord — enable when bot token present (pico discord format: token + allow_from; discord_guild_id
# from config.json is not used by this channel).
DC_TOKEN="$(dev discord_bot_token)"; DC_USER="$(dev discord_user_id)"
if [ -n "$DC_TOKEN" ]; then
  enable_channel discord
  sec_set discord token "$DC_TOKEN"
  sec_allow_from discord "$DC_USER"
  log "discord enabled + token synced"
else
  log "discord: no discord_bot_token in config.json — left disabled"
fi

SL_BOT="$(dev slack_bot_token)"; SL_APP="$(dev slack_app_token)"; SL_USER="$(dev slack_user_id)"
if [ -n "$SL_BOT" ] && [ -n "$SL_APP" ]; then
  enable_channel slack
  sec_set slack bot_token "$SL_BOT"
  sec_set slack app_token "$SL_APP"
  sec_allow_from slack "$SL_USER"
  log "slack enabled + tokens synced"
else
  log "slack: missing slack_bot_token/slack_app_token in config.json — left disabled"
fi

WA_USER="$(dev whatsapp_user_id)"
if [ -n "$WA_USER" ]; then
  enable_channel whatsapp
  jq_edit "$PICO_CONFIG" '
      .channel_list.whatsapp.settings.use_native         = true
    | .channel_list.whatsapp.settings.session_store_path = "/root/.picoclaw/workspace/whatsapp"
  '
  sec_allow_from whatsapp "$WA_USER"
  log "whatsapp enabled (native; QR pairing required on first run)"
else
  log "whatsapp: no whatsapp_user_id in config.json — left disabled"
fi

log "done — picoclaw model + channel config synced"
