#!/usr/bin/env bash
# runtime-claudecode-presync — run by switch-runtime right before claudecode starts, once at the end
# of install.sh, and by EnsureOnboarding on every os-server boot / config change (hermes-style).
set -euo pipefail

CONFIG_JSON="/root/config/config.json"          # device/project config (source of truth)
CC_DIR="/root/.claudecode"
WS_DIR="$CC_DIR/workspace"
ENV_FILE="$CC_DIR/.env"
CLAUDE_HOME="/root/.claude"

DEFAULT_BASE_URL="https://campaign-api.autonomous.ai/api/v1/ai"
DEFAULT_MODEL="Auto-AI"

log() { echo "[claudecode-presync] $*"; }

command -v jq >/dev/null 2>&1 || { log "ERROR: jq not found — cannot sync claudecode config" >&2; exit 1; }

# Skills are USER-scoped (~/.claude/skills), not project-scoped: Claude Code resolves project skills
# from the session cwd, so a workspace-only install is invisible to the coding sessions the device
# spawns in other folders.
mkdir -p "$CLAUDE_HOME/skills" "$WS_DIR/.claude" "$WS_DIR/memory"

dev() { jq -r ".${1} // empty" "$CONFIG_JSON" 2>/dev/null || true; }
jq_edit() { local f="$1"; shift; local tmp; tmp="$(mktemp)"; jq "$@" "$f" >"$tmp" && mv "$tmp" "$f"; }

# Headless device has no TTY: skip first-run onboarding and accept the bypass-permissions warning.
log "seed headless flags in ~/.claude.json"
CLAUDE_JSON="/root/.claude.json"
[ -f "$CLAUDE_JSON" ] || echo '{}' >"$CLAUDE_JSON"
jq_edit "$CLAUDE_JSON" '
    .hasCompletedOnboarding          = true
  | .bypassPermissionsModeAccepted   = true
'

log "seed workspace .claude/settings.json"
SETTINGS="$WS_DIR/.claude/settings.json"
mkdir -p "$WS_DIR/.claude"
[ -f "$SETTINGS" ] || echo '{}' >"$SETTINGS"
jq_edit "$SETTINGS" '.enableAllProjectMcpServers = true'

# Telegram and discord are device-owned; the native channel plugins are not used.
rm -rf "$CLAUDE_HOME/channels"

# Subscription mode omits every ANTHROPIC_* var: API-key vars outrank OAuth in Claude Code.
OAUTH_TOKEN="$(dev claude_code_oauth_token)"
umask 077
if [ -n "$OAUTH_TOKEN" ] || [ -s "$CLAUDE_HOME/.credentials.json" ]; then
  log "write $ENV_FILE (auth=claude.ai subscription, token=$( [ -n "$OAUTH_TOKEN" ] && echo config || echo credentials.json ))"
  {
    echo "# Managed by runtime-claudecode-presync — do not edit (synced from /root/config/config.json)."
    echo "# Subscription auth: ANTHROPIC_* omitted on purpose (they outrank the OAuth login)."
    if [ -n "$OAUTH_TOKEN" ]; then
      echo "CLAUDE_CODE_OAUTH_TOKEN=$OAUTH_TOKEN"
    fi
    echo "DISABLE_AUTOUPDATER=1"
    echo "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1"
  } >"$ENV_FILE.tmp"
else
  LLM_BASE_URL="$(dev llm_base_url)"; [ -n "$LLM_BASE_URL" ] || LLM_BASE_URL="$DEFAULT_BASE_URL"
  LLM_BASE_URL="${LLM_BASE_URL%/v1}"
  LLM_API_KEY="$(dev llm_api_key)"
  LLM_MODEL="$(dev llm_model)"; [ -n "$LLM_MODEL" ] || LLM_MODEL="$DEFAULT_MODEL"
  log "write $ENV_FILE (auth=api-key, base_url=$LLM_BASE_URL model=$LLM_MODEL key=$( [ -n "$LLM_API_KEY" ] && echo set || echo EMPTY ))"
  cat >"$ENV_FILE.tmp" <<ENV
# Managed by runtime-claudecode-presync — do not edit (synced from /root/config/config.json).
ANTHROPIC_BASE_URL=$LLM_BASE_URL
# x-api-key ONLY: campaign-api 401s the Authorization: Bearer form, and claude
# prefers ANTHROPIC_AUTH_TOKEN (bearer) over ANTHROPIC_API_KEY when both are
# set — so the bearer var must stay unset (device-verified).
ANTHROPIC_API_KEY=$LLM_API_KEY
ANTHROPIC_MODEL=$LLM_MODEL
ANTHROPIC_SMALL_FAST_MODEL=$LLM_MODEL
DISABLE_AUTOUPDATER=1
CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1
ENV
  # Pre-approve the key (last 20 chars) in customApiKeyResponses, or interactive claude refuses it.
  if [ -n "$LLM_API_KEY" ]; then
    KEYID="${LLM_API_KEY: -20}"
    jq_edit "$CLAUDE_JSON" --arg k "$KEYID" '
        .customApiKeyResponses.approved = (((.customApiKeyResponses.approved // []) + [$k]) | unique)
      | .customApiKeyResponses.rejected = ((.customApiKeyResponses.rejected // []) | map(select(. != $k)))
    '
    log "pre-approved interactive API key (…$KEYID) in ~/.claude.json"
  fi
fi
mv "$ENV_FILE.tmp" "$ENV_FILE"
umask 022

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
esac
unset _rt
PROFILE
  chmod 0644 /etc/profile.d/agent-cli-env.sh
}
write_cli_login_env && log "wrote /etc/profile.d/agent-cli-env.sh (interactive CLI auto-login)"

# claude-sessions picker: Claude's /resume picker hides headless (--print) sessions.
write_session_picker() {
  cat >/usr/local/bin/claude-sessions <<'PICKER'
#!/bin/sh
# Managed by os-server runtime presync — do not edit.
# Unified claude coding-session picker: `claude-sessions` in a folder lists its
# sessions (terminal- AND Telegram-created) and resumes the one you pick.
[ "$(id -u)" -eq 0 ] || exec sudo /usr/local/bin/claude-sessions "$@"
exec /usr/local/bin/os-server claude-sessions "$@"
PICKER
  chmod 0755 /usr/local/bin/claude-sessions
  # Remove the picker's earlier `cc` name — only if it is OUR managed wrapper (never clobber a real
  # C-compiler cc that may sit there on other systems).
  if grep -q "Managed by os-server runtime presync" /usr/local/bin/cc 2>/dev/null; then
    rm -f /usr/local/bin/cc
  fi
}
write_session_picker && log "wrote /usr/local/bin/claude-sessions (unified session picker)"

log "done — claudecode env + channel config synced"
