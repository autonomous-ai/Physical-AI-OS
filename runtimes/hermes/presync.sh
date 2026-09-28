#!/usr/bin/env bash
# runtime-hermes-presync — run by switch-runtime right before hermes starts (and
# once at the end of install.sh). It OWNS the device-side Hermes model config in
# config.yaml, in two layers:
#
#   1. STRUCTURE (static) — the provider wiring that routes Hermes at the device's
#      campaign-api brain via anthropic_messages. `hermes claw migrate` does NOT
#      carry it across, and a factory reset (`hermes setup --reset`) WIPES it back
#      to defaults. We assert it idempotently here so it self-heals on the next
#      switch — without this, only the first install.sh run ever wrote it, and
#      switch-runtime skips install.sh once hermes is installed, so a reset left
#      hermes pointed at a broken/default provider with no path to recover. The
#      same layer also asserts two os-server-owned knobs: auxiliary.vision (the
#      image-understanding model) and agent.image_input_mode.
#
#   2. DYNAMIC (per-device) — the real llm_model / llm_base_url / llm_api_key and
#      channel tokens from /root/config/config.json, which override the defaults.
#
# This file is EMBEDDED IN os-server (runtimes/hermes/presync.sh) and materialized
# to /usr/local/bin/runtime-hermes-presync on every switch, so a plain os-server
# OTA refreshes it on disk — unlike a copy written by install.sh, which only re-runs
# on a first install / failed verify.
set -euo pipefail
CONFIG_JSON="/root/config/config.json"
HERMES_DIR="/root/.hermes"
ENV_FILE="$HERMES_DIR/.env"
CONFIG_YAML="$HERMES_DIR/config.yaml"
log() { echo "[hermes-presync] $*"; }

# ── 0. SKILLS (restore OpenClaw-imported skills if missing) ─────────────────────
# `hermes claw migrate` imports the device's OpenClaw skills into
# ~/.hermes/skills/openclaw-imports. It runs from install.sh on first install, but
# a factory reset (wipeHermesState) wipes that dir while leaving the OpenClaw
# workspace intact — and install.sh does NOT re-run on a later switch (verify
# passes), so the skills would never come back. Restore them here when the dir is
# empty/absent. GUARDED so a normal switch is a no-op (re-running every switch with
# --skill-conflict rename would pile up renamed duplicates). claw migrate has no
# skills-only preset (it also touches SOUL/MEMORY), but that is harmless: the Go
# persona migration runs afterwards (os-server boot) and re-writes SOUL/MEMORY
# cleanly, so only the skills it imported actually persist.
HERMES_BIN="${HERMES_BIN:-/usr/local/bin/hermes}"
IMPORTS_DIR="$HERMES_DIR/skills/openclaw-imports"
if [ ! -d "$IMPORTS_DIR" ] || [ -z "$(ls -A "$IMPORTS_DIR" 2>/dev/null)" ]; then
  if [ -x "$HERMES_BIN" ] && [ -d /root/.openclaw ]; then
    log "openclaw-imported skills missing — restoring via claw migrate"
    "$HERMES_BIN" claw migrate --preset full --overwrite --skill-conflict rename --yes --migrate-secrets \
      || log "WARN: claw migrate failed (non-fatal)"
  fi
fi

# yq is required for the structured config.yaml edits. install.sh installs it; if
# it is somehow absent we cannot safely patch — bail loudly (non-fatal to the
# switch: the caller treats presync failure as a warning).
if ! command -v yq >/dev/null 2>&1; then
  log "ERROR: yq not found — cannot ensure config.yaml structure" >&2
  exit 1
fi

# touch first so yq has a doc to edit on a fresh/absent config.yaml.
touch "$CONFIG_YAML"

# `hermes setup --reset` leaves top-level `.model` as an EMPTY STRING ('') and may
# leave `.custom_providers` as a scalar too — yq cannot index `.model.provider` /
# `.custom_providers[0]` into a scalar, so the structure assignment below would
# no-op and the config stays broken (`model: ''`). Coerce each to its expected
# container type FIRST, but only when it isn't already one, so a populated config
# is preserved.
[ "$(yq '.model | tag' "$CONFIG_YAML" 2>/dev/null)" = "!!map" ] || yq -i '.model = {}' "$CONFIG_YAML"
[ "$(yq '.custom_providers | tag' "$CONFIG_YAML" 2>/dev/null)" = "!!seq" ] || yq -i '.custom_providers = []' "$CONFIG_YAML"

# ── 1. STRUCTURE (idempotent) ──────────────────────────────────────────────────
# Assert the static provider wiring. `// default` keeps any existing value (so a
# real llm_* already synced below is not stomped) and only fills it when missing.
# os-server sends a fixed request model ("hermes-agent", constants.go); .model
# .provider = custom:autonomous is what routes that bare model at the campaign-api
# custom provider, so this is the field that actually matters (the .default value
# is only a never-used fallback once a per-request model is sent).
log "ensure config.yaml model + custom_providers structure"
yq -i '
  .model.provider = "custom:autonomous"
  | .model.default = "Auto-AI"
  | .custom_providers[0].name     = "autonomous"
  | .custom_providers[0].key_env  = "AUTONOMOUS_API_KEY"
  | .custom_providers[0].api_mode = "anthropic_messages"
  | .custom_providers[0].base_url = (.custom_providers[0].base_url // "https://campaign-api.autonomous.ai/api/v1/ai")
' "$CONFIG_YAML"

# ── 1b. AUXILIARY VISION + AGENT IMAGE INPUT (always overwrite) ─────────────────
# os-server-owned knobs, asserted unconditionally (self-heal after reset). Coerce
# .auxiliary/.agent to maps first (reset can leave scalars, like .model). Only
# .auxiliary.vision (overwritten whole) and .agent.image_input_mode are touched —
# other keys under both are preserved.
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

# ── 1b2. TERMINAL CWD (always overwrite) ──────────────────────────────────────
# Hermes discovers project-context files (AGENTS.md, .cursorrules) by walking up
# from the CONFIGURED cwd — `resolve_context_cwd` in agent/runtime_cwd.py, which
# returns None rather than falling back to the launch dir (a guard against an
# agent self-spawned inside the Hermes source tree swallowing that repo's own
# AGENTS.md). The stock `terminal.cwd: .` is relative, never bridges to
# TERMINAL_CWD, and leaves that lookup empty — so an AGENTS.md we write is never
# read. Pinning the absolute Hermes home is what makes the OS block reachable.
#
# The process already runs there (`WorkingDirectory=/root/.hermes` in the unit
# Hermes installs), so this only states an existing fact; the agent's shell cwd
# does not move. Verified on-device: with `.` a codeword planted in AGENTS.md was
# absent from the prompt; with the absolute path the agent returned it.
log "ensure config.yaml terminal.cwd (makes AGENTS.md discoverable)"
[ "$(yq '.terminal | tag' "$CONFIG_YAML" 2>/dev/null)" = "!!map" ] || yq -i '.terminal = {}' "$CONFIG_YAML"
yq -i '.terminal.cwd = "'"$HERMES_DIR"'"' "$CONFIG_YAML"

# ── 1c. APPROVALS OFF (always overwrite) ───────────────────────────────────────
# The device runs unattended (voice + chat channels) — a "Command Approval
# Required" card is a dead end for a voice user and stalls the turn, so command
# approval prompts are disabled entirely (product decision). Hermes' hardline
# blocklist still applies; approvals.mode is the documented master switch for
# the tirith/dangerous-command prompt flow. style="double" is REQUIRED: yq v4
# (YAML 1.2) writes a bare `off`, but Hermes reads config.yaml with PyYAML
# (YAML 1.1) where unquoted off parses as boolean False — the mode string is
# never matched and prompts silently stay ON. Verified on-device: bare off →
# parsed False; "off" → parsed 'off'.
[ "$(yq '.approvals | tag' "$CONFIG_YAML" 2>/dev/null)" = "!!map" ] || yq -i '.approvals = {}' "$CONFIG_YAML"
log "ensure config.yaml approvals.mode=off (no command-approval prompts)"
yq -i '.approvals.mode = "off" | .approvals.mode style="double"' "$CONFIG_YAML"

# ── 2. DYNAMIC (config.json wins) ──────────────────────────────────────────────
# NOTE on .model.default: "Auto-AI" is an alias only the campaign-api proxy
# understands — it resolves it to whatever model it picks. While the device is on
# that proxy the alias is exactly right, and llm_model is left alone (it may hold
# an OpenClaw primary model like claude-opus-4-6, which means nothing here).
#
# Point the device at another host — an operator supplying their own brain — and
# the alias becomes an unknown model id. Measured on intern-v2-d16f against
# openrouter: every turn came back `400 Auto-AI is not a valid model ID`, and
# because this script self-heals on each boot, editing config.yaml by hand did
# not survive a restart. So the alias is kept for the proxy and the operator's
# llm_model is used everywhere else.
LLM_BASE_URL="$(jq -r '.llm_base_url // empty' "$CONFIG_JSON" 2>/dev/null || true)"
LLM_MODEL="$(jq -r '.llm_model // empty' "$CONFIG_JSON" 2>/dev/null || true)"

# Custom brain → the alias cannot resolve; use the operator's model. Empty base
# URL means the device is on the default proxy, so that keeps the alias too.
case "$LLM_BASE_URL" in
  ""|*campaign-api.autonomous.ai*)
    # Prompt-cache markers for the Auto-AI alias. Hermes only emits Anthropic
    # `cache_control` breakpoints for a custom provider when that model declares
    # `prompt_caching: true`; without it every turn re-sends the whole ~18k-token
    # floor (system + 25 tool schemas) uncached. Measured on lamp-0c4e 2026-09-16,
    # same "hello" in one session: no markers 18.3s → 12.6s, cache_read 0;
    # markers 13.8s → 9.2s steady, cache_read ~85%. Scoped to the campaign-api
    # proxy (known to honour the markers); a BYO brain keeps Hermes' own
    # per-provider caching policy.
    yq -i '.custom_providers[0].models["Auto-AI"].prompt_caching = true' "$CONFIG_YAML"
    log "custom_providers[0].models.Auto-AI.prompt_caching = true (cache markers on)"
    # 1h cache TTL: a lamp user typically speaks, then goes quiet for 10-20 min,
    # which outlives the 5m default and re-bills the full prefill on the next
    # turn (measured 2026-09-16: 3.6s with cache vs 11.8s after an 8 min gap).
    # Hermes accepts only "5m" | "1h"; it sends `ttl: "1h"` on every marker. If
    # the gateway ignores the field the cache silently stays at 5m - harmless.
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

if [ -n "$LLM_BASE_URL" ]; then
  yq -i ".custom_providers[0].base_url = \"$LLM_BASE_URL\"" "$CONFIG_YAML"
  log "custom_providers[0].base_url = $LLM_BASE_URL"
fi

# .env secrets/IDs: config.json wins — UPSERT each non-empty config.json field
# into its Hermes env var (replace the existing line, or append if absent). Other
# variables are never touched, so whatever `hermes claw migrate` carried over
# from OpenClaw is kept as the fallback. Empty/missing config fields are skipped.
sync_env() {
  local key="$1" var="$2" val
  val="$(jq -r ".${key} // empty" "$CONFIG_JSON" 2>/dev/null || true)"
  [ -n "$val" ] || return 0
  sed -i "/^${var}=/d" "$ENV_FILE"
  # guard against gluing onto a previous line that lacks a trailing newline
  [ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >>"$ENV_FILE"
  echo "${var}=${val}" >>"$ENV_FILE"
  log "${var} synced"
}

sync_env llm_api_key        AUTONOMOUS_API_KEY
sync_env telegram_bot_token TELEGRAM_BOT_TOKEN
sync_env telegram_user_id   TELEGRAM_ALLOWED_USERS
sync_env slack_bot_token    SLACK_BOT_TOKEN
sync_env slack_app_token    SLACK_APP_TOKEN
sync_env slack_user_id      SLACK_ALLOWED_USERS
sync_env discord_bot_token  DISCORD_BOT_TOKEN
sync_env discord_guild_id   DISCORD_GUILD_ID
sync_env discord_user_id    DISCORD_ALLOWED_USERS
sync_env whatsapp_user_id   WHATSAPP_ALLOWED_USERS

# ── 3. API SERVER KEY (must match constants.go APIKey) ────────────────────────
# Enforce on every presync so a key bump in os-server self-heals on the next
# switch — install.sh only runs on first-install and cannot fix devices that
# already have hermes installed with an older key.
# API_SERVER_KEY MUST equal runtimes/hermes/constants.go APIKey.
EXPECTED_API_KEY="hermes-local-api-key"
sed -i "/^API_SERVER_KEY=/d" "$ENV_FILE"
[ -s "$ENV_FILE" ] && [ -n "$(tail -c1 "$ENV_FILE")" ] && printf '\n' >>"$ENV_FILE"
echo "API_SERVER_KEY=${EXPECTED_API_KEY}" >>"$ENV_FILE"
log "API_SERVER_KEY enforced (${EXPECTED_API_KEY})"
