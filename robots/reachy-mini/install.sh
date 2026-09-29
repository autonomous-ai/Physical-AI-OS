#!/usr/bin/env bash
# install.sh — one-command Autonomous OS bring-up on a Reachy Mini (run on the robot).
# Usage: curl -fsSL https://raw.githubusercontent.com/autonomous-ai/autonomous-os/main/robots/reachy-mini/install.sh | sudo bash [-s -- SPIKE_ARGS|--force]
# Env: OTA_METADATA_URL, OTA_SIGNING_PUBLIC_KEY, DEVICE_TYPE.
set -euo pipefail

DEVICE_TYPE="${DEVICE_TYPE:-reachy-mini}"
OTA_METADATA_URL="${OTA_METADATA_URL:-https://cdn.autonomous.ai/os/ota/metadata.json}"

tag() { printf '[install] %s\n' "$1" >&2; }
fail() { printf '[install] ERROR: %s\n' "$1" >&2; exit 1; }

# Filter --force out (do not blank it) so spike.sh does not see an unknown flag.
FORCE=0
ARGS=()
for a in "$@"; do
  if [ "$a" = "--force" ]; then FORCE=1; else ARGS+=("$a"); fi
done
set -- ${ARGS[@]+"${ARGS[@]}"}

[ "$(id -u)" -eq 0 ] || fail "run as root — pipe to 'sudo bash', not 'bash'"

# Guard: refuse to modify a non-Reachy host (detected via the Pollen daemon unit or port).
if [ "$FORCE" = "0" ]; then
  if systemctl list-unit-files reachy-mini-daemon.service >/dev/null 2>&1 \
     && systemctl cat reachy-mini-daemon.service >/dev/null 2>&1; then
    :
  elif curl -sf -m 3 http://localhost:8000/api/media/status >/dev/null 2>&1; then
    :
  else
    fail "this does not look like a Reachy Mini.
No reachy-mini-daemon.service, and nothing answering on localhost:8000.

Run it ON the robot, over SSH — not on your laptop:
    ssh pollen@reachy-mini.local
    curl -fsSL …/install.sh | sudo bash

If you really mean to install here, re-run with --force."
  fi
fi

tag "device : $DEVICE_TYPE"
tag "feed   : $OTA_METADATA_URL"

missing=()
for t in curl unzip jq; do command -v "$t" >/dev/null || missing+=("$t"); done
if [ ${#missing[@]} -gt 0 ]; then
  tag "installing: ${missing[*]}"
  apt-get update -qq >&2
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}" >&2 \
    || fail "could not install ${missing[*]}"
fi

# With a pinned key, consume only the verified .signed payload.
BOOTSTRAP_JSON="/root/config/bootstrap.json"
OTA_SIGNING_PUBLIC_KEY="${OTA_SIGNING_PUBLIC_KEY:-$(jq -r '.signing_public_key // empty' "$BOOTSTRAP_JSON" 2>/dev/null || true)}"
verify_ota_metadata() {
  local envelope="$1" payload="$2" public_der signature
  command -v openssl >/dev/null || { apt-get update -qq >&2 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssl >&2; } \
    || fail "could not install openssl for OTA signature verification"
  public_der="$(mktemp)"
  signature="$(mktemp)"
  trap 'rm -f "$public_der" "$signature"' RETURN
  jq -er '(.signed // .) | .format == "autonomous-ota/v1" and .signature.algorithm == "ed25519" and (.payload | type == "string")' "$envelope" >/dev/null \
    || fail "OTA metadata is unsigned or uses an unsupported format"
  printf '\060\052\060\005\006\003\053\145\160\003\041\000' >"$public_der"
  printf '%s' "$OTA_SIGNING_PUBLIC_KEY" | (base64 --decode 2>/dev/null || base64 -D) >>"$public_der" \
    || fail "bootstrap signing_public_key is not base64"
  [ "$(wc -c <"$public_der" | tr -d ' ')" = "44" ] \
    || fail "bootstrap signing_public_key is not a 32-byte Ed25519 key"
  jq -r '(.signed // .).payload' "$envelope" | (base64 --decode 2>/dev/null || base64 -D) >"$payload" \
    || fail "OTA metadata payload is not valid base64"
  jq -r '(.signed // .).signature.value' "$envelope" | (base64 --decode 2>/dev/null || base64 -D) >"$signature" \
    || fail "OTA metadata signature is not valid base64"
  openssl pkeyutl -verify -pubin -keyform DER -inkey "$public_der" -rawin -in "$payload" -sigfile "$signature" >/dev/null \
    || fail "OTA metadata signature verification failed"
  jq -e . "$payload" >/dev/null 2>&1 || fail "verified OTA metadata payload is not JSON"
}

tag "reading the OTA feed"
META="$(mktemp)"
trap 'rm -f "$META"' EXIT
# no-cache: CDN edge caching can serve a stale build.
curl -fsSL -H 'Cache-Control: no-cache' -H 'Pragma: no-cache' -o "$META" "$OTA_METADATA_URL" \
  || fail "could not fetch $OTA_METADATA_URL"
jq -e . "$META" >/dev/null 2>&1 || fail "$OTA_METADATA_URL did not return JSON (captive portal?)"

if [ -n "$OTA_SIGNING_PUBLIC_KEY" ]; then
  META_PAYLOAD="$(mktemp)"
  trap 'rm -f "$META" "$META_PAYLOAD"' EXIT
  verify_ota_metadata "$META" "$META_PAYLOAD"
  mv "$META_PAYLOAD" "$META"
  tag "OTA metadata signature: verified"
else
  tag "WARN: OTA signing is not configured; using legacy unsigned metadata"
fi

PKG_URL="$(jq -r --arg t "$DEVICE_TYPE" '.devices[$t].url // empty' "$META")"
PKG_VER="$(jq -r --arg t "$DEVICE_TYPE" '.devices[$t].version // empty' "$META")"
PKG_SHA256="$(jq -r --arg t "$DEVICE_TYPE" '.devices[$t].sha256 // empty' "$META")"
[ -n "$PKG_URL" ] || fail "the feed has no device profile for '$DEVICE_TYPE'
Published profiles: $(jq -r '.devices | keys | join(", ")' "$META")"
[ -z "$OTA_SIGNING_PUBLIC_KEY" ] || [[ "$PKG_SHA256" =~ ^[a-fA-F0-9]{64}$ ]] \
  || fail "verified OTA metadata has no valid SHA-256 for device profile '$DEVICE_TYPE'"

tag "device package $DEVICE_TYPE $PKG_VER"
STAGING="$(mktemp -d)"
trap 'rm -f "$META"; rm -rf "$STAGING"' EXIT
curl -fsSL -H 'Cache-Control: no-cache' -o "$STAGING/pkg.zip" "$PKG_URL" \
  || fail "could not download the device package from $PKG_URL"
[ -z "$OTA_SIGNING_PUBLIC_KEY" ] || echo "$PKG_SHA256  $STAGING/pkg.zip" | sha256sum -c - >/dev/null \
  || fail "device package SHA-256 mismatch"
unzip -o -q "$STAGING/pkg.zip" -d "$STAGING" || fail "the device package is not a readable zip"

# spike-lib.sh marks the run-on-robot package generation; older ones ran from a Mac.
if [ ! -f "$STAGING/spike-lib.sh" ]; then
  [ -f "$STAGING/spike.sh" ] \
    && fail "device package $PKG_VER ships the older Mac-side scripts, which cannot run here.
Publish a current one:  make upload-device $DEVICE_TYPE" \
    || fail "device package $PKG_VER predates the installer scripts.
Publish a current one:  make upload-device $DEVICE_TYPE"
fi

tag "handing over to spike.sh"
echo >&2
OTA_METADATA_URL="$OTA_METADATA_URL" OTA_SIGNING_PUBLIC_KEY="$OTA_SIGNING_PUBLIC_KEY" DEVICE_TYPE="$DEVICE_TYPE" \
  bash "$STAGING/spike.sh" "$@"

cat >&2 <<EOF

[install] The scripts now live at /opt/devices/$DEVICE_TYPE/ — use them to
[install] stop or remove the stack later:
[install]     sudo bash /opt/devices/$DEVICE_TYPE/spike.sh --stop
[install]     sudo bash /opt/devices/$DEVICE_TYPE/spike.sh --uninstall
EOF
