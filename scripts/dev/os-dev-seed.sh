#!/usr/bin/env bash
# Prepare the off-device state dir for `make os-dev`: seed config/config.json once, never overwrite.
set -euo pipefail

STATE_DIR="${1:?usage: os-dev-seed.sh <state-dir> <device-type> <agent-runtime> <codex-home>}"
DEVICE_TYPE="${2:?}"
AGENT_RUNTIME="${3:?}"
CODEX_HOME="${4:?}"

log() { echo "[os-dev-seed] $*"; }

mkdir -p "$STATE_DIR/config"
CONFIG_JSON="$STATE_DIR/config/config.json"
CONFIG_EXAMPLE="$(dirname "$0")/config.example.json"

if [ ! -f "$CONFIG_JSON" ]; then
  cp "$CONFIG_EXAMPLE" "$CONFIG_JSON"
  chmod 600 "$CONFIG_JSON"
  log "created $CONFIG_JSON from $(basename "$CONFIG_EXAMPLE")"
fi

# set_up_completed gates presync + EnsureOnboarding (server/config_watch.go).
python3 - "$CONFIG_JSON" "$DEVICE_TYPE" "$AGENT_RUNTIME" <<'PY'
import json, os, sys
path, device_type, runtime = sys.argv[1:4]
PLACEHOLDER_KEY = "autonomous_api_key"
cfg = {}
if os.path.exists(path):
    with open(path) as f:
        cfg = json.load(f)
cfg["device_type"] = device_type
cfg["agent_runtime"] = runtime
cfg["set_up_completed"] = True
# Off-device only: log into the web UI without hand-generating a bcrypt hash.
# The hash below is bcrypt(cost 10) of "autonomous" and is public by definition
# — a real device gets its hash from setup, and this script never runs there.
# An existing hash (a config copied from a provisioned device) is left alone.
if not str(cfg.get("admin_password_hash", "")).strip():
    cfg["admin_password_hash"] = "$2a$10$nfmV4leY9FjNIS44X8/97OobRW6VWOvyhKYxAvLPAWTVKmCMWeOH6"
    print("[os-dev-seed] admin_password_hash was empty — defaulted to password 'autonomous'")
# The shared backend every dev key is issued against. Only filled when blank —
# a config pointing at another gateway keeps its own URL.
if not str(cfg.get("llm_base_url", "")).strip():
    cfg["llm_base_url"] = "https://campaign-api.autonomous.ai/api/v1/ai/v1"
    print("[os-dev-seed] llm_base_url was empty — defaulted to https://campaign-api.autonomous.ai/api/v1/ai/v1")
# A placeholder rather than "": an empty key makes adminAuthMiddleware answer
# 503 and the web UI bounce to /setup, which reads as a broken build instead of
# a missing credential. The placeholder keeps the UI reachable and names what to
# ask the team for; the NOTE below still fires until a real key replaces it.
if not str(cfg.get("llm_api_key", "")).strip():
    cfg["llm_api_key"] = PLACEHOLDER_KEY
    print(f"[os-dev-seed] llm_api_key was empty — set to placeholder '{PLACEHOLDER_KEY}' (ask the team for a real key)")
if not str(cfg.get("llm_model", "")).strip():
    cfg["llm_model"] = "Auto-AI"
    print("[os-dev-seed] llm_model was empty — defaulted to Auto-AI")
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
print(f"[os-dev-seed] {path}: device_type={device_type} agent_runtime={runtime} set_up_completed=true")

# Name what each missing credential costs, at the moment the dev can act on it.
# A silent empty key surfaces much later as "the device never speaks" or a turn
# that answers blind, and neither points back here.
missing = []
if str(cfg.get("llm_api_key", "")).strip() in ("", PLACEHOLDER_KEY):
    missing.append("llm_api_key is still the placeholder — ask the team for a real key. "
                   "Until then: no TTS (silent replies), no STT, "
                   f"no Gemini Live, no image description. Fill it in: nano {path}")
for m in missing:
    print(f"[os-dev-seed] NOTE: {m}")
PY

# presync.sh regenerates config.toml on every boot; back up a hand-written one once.
if [ "$AGENT_RUNTIME" = "codex" ] && [ -f "$CODEX_HOME/config.toml" ] && [ ! -f "$CODEX_HOME/config.toml.pre-os-dev" ]; then
  cp "$CODEX_HOME/config.toml" "$CODEX_HOME/config.toml.pre-os-dev"
  log "backed up $CODEX_HOME/config.toml → config.toml.pre-os-dev (presync rewrites it)"
fi

BOOTSTRAP_JSON="$STATE_DIR/config/bootstrap.json"
if [ ! -f "$BOOTSTRAP_JSON" ]; then
  # shellcheck source=/dev/null
  . "$(dirname "$0")/../release/ota-config.sh"
  URL="${OTA_METADATA_URL:-https://storage.googleapis.com/${GCS_BUCKET}/${BUCKET_PREFIX}/ota/metadata.json}"
  printf '{\n  "metadata_url": "%s"\n}\n' "$URL" > "$BOOTSTRAP_JSON"
  log "seeded $BOOTSTRAP_JSON (metadata_url=$URL)"
fi

log "state dir ready: $STATE_DIR"
