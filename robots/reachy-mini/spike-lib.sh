#!/usr/bin/env bash
# spike-lib.sh — shared helpers for the Reachy Mini spike scripts (sourced, not executed).
# Installs components from OTA metadata into the production layout.

DEVICE_TYPE="${DEVICE_TYPE:-reachy-mini}"
HAL_DIR="${HAL_DIR:-/opt/hal}"
DEVICES_DIR="${DEVICES_DIR:-/opt/devices}"
CONFIG_DIR="${CONFIG_DIR:-/root/config}"
# Must match openclaw's default home and Default() in system/server/config/config.go.
OPENCLAW_CONFIG_DIR="${OPENCLAW_CONFIG_DIR:-/root/.openclaw}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
WEB_ROOT="${WEB_ROOT:-/usr/share/nginx/html/setup}"

# Pollen daemon: owns motion, camera and ALSA PCMs until HAL claims them.
DAEMON_URL="${DAEMON_URL:-http://localhost:8000}"

DEFAULT_METADATA_URL="https://cdn.autonomous.ai/os/ota/metadata.json"

# All output goes to stderr: helpers run inside $( ) and stdout becomes the value.
say() { printf '\n========== %s ==========\n' "$1" >&2; }
info() { printf '[%s] %s\n' "${SPIKE_TAG:-spike}" "$1" >&2; }
die() { printf '[%s] ERROR: %s\n' "${SPIKE_TAG:-spike}" "$1" >&2; exit 1; }

ensure_root() {
  [ "$(id -u)" -eq 0 ] || die "run as root:  sudo bash $0 $*"
}

retry() {
  local cmd="$1" max="${2:-5}" delay="${3:-2}" n=0
  until [ "$n" -ge "$max" ]; do
    eval "$cmd" && return 0
    n=$((n + 1))
    info "retry $n/$max"
    sleep "$delay"
  done
  return 1
}

ensure_tools() {
  local missing=()
  for t in curl unzip jq; do command -v "$t" >/dev/null || missing+=("$t"); done
  [ ${#missing[@]} -eq 0 ] && return 0
  info "installing: ${missing[*]}"
  apt-get update -qq >&2
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}" >&2 \
    || die "could not install ${missing[*]}"
}

# Trust only a key pinned in bootstrap.json; metadata must never nominate its own key.
signing_public_key() {
  [ -f "$CONFIG_DIR/bootstrap.json" ] || return 0
  jq -r '.signing_public_key // empty' "$CONFIG_DIR/bootstrap.json" 2>/dev/null || true
}

ota_base64_decode() {
  base64 --decode 2>/dev/null || base64 -D
}

ensure_ota_verifier() {
  command -v openssl >/dev/null && return 0
  info "installing: openssl"
  apt-get update -qq >&2
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssl >&2 \
    || die "could not install openssl for OTA signature verification"
}

# verify_ota_metadata <envelope> <payload> <base64-ed25519-public-key>
verify_ota_metadata() {
  local envelope="$1" payload="$2" public_key="$3" public_der signature
  ensure_ota_verifier
  public_der="$(mktemp)"
  signature="$(mktemp)"
  trap 'rm -f "$public_der" "$signature"' RETURN
  jq -er '(.signed // .) | .format == "autonomous-ota/v1" and .signature.algorithm == "ed25519" and (.payload | type == "string")' "$envelope" >/dev/null \
    || { info "ERROR: OTA metadata is unsigned or uses an unsupported format"; return 1; }
  # SubjectPublicKeyInfo DER prefix for a raw 32-byte Ed25519 public key.
  printf '\060\052\060\005\006\003\053\145\160\003\041\000' >"$public_der"
  printf '%s' "$public_key" | ota_base64_decode >>"$public_der" \
    || { info "ERROR: bootstrap signing_public_key is not base64"; return 1; }
  [ "$(wc -c <"$public_der" | tr -d ' ')" = "44" ] \
    || { info "ERROR: bootstrap signing_public_key is not a 32-byte Ed25519 key"; return 1; }
  jq -r '(.signed // .).payload' "$envelope" | ota_base64_decode >"$payload" \
    || { info "ERROR: OTA metadata payload is not valid base64"; return 1; }
  jq -r '(.signed // .).signature.value' "$envelope" | ota_base64_decode >"$signature" \
    || { info "ERROR: OTA metadata signature is not valid base64"; return 1; }
  openssl pkeyutl -verify -pubin -keyform DER -inkey "$public_der" -rawin -in "$payload" -sigfile "$signature" >/dev/null \
    || { info "ERROR: OTA metadata signature verification failed"; return 1; }
  jq -e . "$payload" >/dev/null 2>&1 \
    || { info "ERROR: verified OTA metadata payload is not JSON"; return 1; }
}

# Cached in a file (not a variable) so subshell reads share one feed snapshot per run.
META_CACHE="${SPIKE_META_CACHE:-/tmp/.spike-ota-metadata.json}"

metadata_url() {
  if [ -n "${OTA_METADATA_URL:-}" ]; then
    echo "$OTA_METADATA_URL"
    return
  fi
  local from_bootstrap=""
  if [ -f "$CONFIG_DIR/bootstrap.json" ]; then
    from_bootstrap="$(jq -r '.metadata_url // empty' "$CONFIG_DIR/bootstrap.json" 2>/dev/null || true)"
  fi
  echo "${from_bootstrap:-$DEFAULT_METADATA_URL}"
}

fetch_metadata() {
  [ -s "$META_CACHE" ] && return 0
  ensure_tools
  local url tmp
  url="$(metadata_url)"
  tmp="$(mktemp)"
# no-cache: CDN edge caching can serve a stale build.
  retry "curl -fsSL -H 'Cache-Control: no-cache' -H 'Pragma: no-cache' -o '$tmp' '$url'" 5 \
    || { rm -f "$tmp"; die "could not fetch OTA metadata from $url"; }
# Validate before caching: captive portals return 200 with HTML.
  jq -e . "$tmp" >/dev/null 2>&1 || { rm -f "$tmp"; die "OTA metadata at $url is not valid JSON"; }
  local public_key payload
  public_key="$(signing_public_key)"
  if [ -n "$public_key" ]; then
    payload="$(mktemp)"
    verify_ota_metadata "$tmp" "$payload" "$public_key" || { rm -f "$tmp" "$payload"; die "could not verify OTA metadata from $url"; }
    rm -f "$tmp"
    mv "$payload" "$META_CACHE"
    info "OTA metadata signature: verified"
  else
    mv "$tmp" "$META_CACHE"
    info "WARN: OTA signing is not configured; using legacy unsigned metadata"
  fi
  info "OTA metadata: $url"
}

clear_metadata_cache() { rm -f "$META_CACHE"; }

# Seed bootstrap.json if missing or without metadata_url (os-server and skill watchers read it).
ensure_bootstrap_config() {
  ensure_tools
  local url key bs tmp
  url="$(metadata_url)"
  key="${OTA_SIGNING_PUBLIC_KEY:-}"
  bs="$CONFIG_DIR/bootstrap.json"
  mkdir -p "$CONFIG_DIR" /root/bootstrap

  if [ -f "$bs" ]; then
# Merge-if-empty: never move a staging-pointed robot back to production.
    tmp="$(mktemp)"
    if jq --arg url "$url" --arg key "$key" \
        'if (.metadata_url // "") == "" then .metadata_url = $url else . end | if (.signing_public_key // "") == "" and $key != "" then .signing_public_key = $key else . end' \
        "$bs" >"$tmp" 2>/dev/null; then
      mv "$tmp" "$bs"
      info "bootstrap.json: metadata_url=$(jq -r '.metadata_url // "(none)"' "$bs")"
    else
      rm -f "$tmp"
      info "WARN: $bs is not valid JSON — leaving it untouched"
    fi
    return 0
  fi

  jq -n --arg url "$url" --arg key "$key" \
    '{httpPort: 8080, metadata_url: $url, signing_public_key: $key, poll_interval: "5m", state_file: "/root/bootstrap/state.json"}' \
    >"$bs"
  info "seeded $bs with metadata_url=$url"
}

# ota_field <component> <field>  (component "device" reads devices.<DEVICE_TYPE>)
ota_field() {
  local component="$1" field="$2"
  fetch_metadata
  if [ "$component" = "device" ]; then
    jq -r --arg t "$DEVICE_TYPE" --arg f "$field" '.devices[$t][$f] // empty' "$META_CACHE"
  else
    jq -r --arg c "$component" --arg f "$field" '.[$c][$f] // empty' "$META_CACHE"
  fi
}

# ota_unpack <component> <dest_dir>
ota_unpack() {
  local component="$1" dest="$2"
  local url version sha256 zip_tmp public_key
  url="$(ota_field "$component" url)"
  version="$(ota_field "$component" version)"
  sha256="$(ota_field "$component" sha256)"
  [ -n "$url" ] || die "OTA metadata has no url for '$component' (device_type=$DEVICE_TYPE)"
  public_key="$(signing_public_key)"
  if [ -n "$public_key" ] && ! [[ "$sha256" =~ ^[a-fA-F0-9]{64}$ ]]; then
    die "OTA metadata has no valid SHA-256 for '$component'"
  fi

  zip_tmp="$(mktemp)"
  retry "curl -fsSL -H 'Cache-Control: no-cache' -H 'Pragma: no-cache' -o '$zip_tmp' '$url'" 5 \
    || die "download failed: $component from $url"
  if [ -n "$public_key" ]; then
    echo "$sha256  $zip_tmp" | sha256sum -c - >/dev/null \
      || { rm -f "$zip_tmp"; die "SHA-256 mismatch: $component"; }
  fi
  mkdir -p "$dest"
  unzip -o -q "$zip_tmp" -d "$dest" || die "unzip failed: $component"
  rm -f "$zip_tmp"
  info "$component $version -> $dest"
}

# ota_install_binary <component> <dest_path>
ota_install_binary() {
  local component="$1" dest="$2"
  local dir_tmp bin
  dir_tmp="$(mktemp -d)"
  ota_unpack "$component" "$dir_tmp"
  bin="$(find "$dir_tmp" -type f -perm -u+x 2>/dev/null | head -1)"
  [ -n "$bin" ] || bin="$(find "$dir_tmp" -type f 2>/dev/null | head -1)"
  [ -n "$bin" ] || die "no binary inside the $component zip"
# Install by rename: overwriting a running binary gives ETXTBSY.
  install -m 0755 "$bin" "$dest.new" && mv -f "$dest.new" "$dest"
  rm -rf "$dir_tmp"
}

# write_unit <name>  (unit body on stdin)
write_unit() {
  local name="$1"
  cat >"/etc/systemd/system/${name}.service"
  systemctl daemon-reload
  info "wrote /etc/systemd/system/${name}.service"
}

start_unit() {
  local name="$1"
  systemctl enable "$name" >/dev/null 2>&1
  systemctl restart "$name"
}

stop_unit() {
  local name="$1"
  systemctl stop "$name" 2>/dev/null || true
}

remove_unit() {
  local name="$1"
  systemctl disable --now "$name" 2>/dev/null || true
  rm -f "/etc/systemd/system/${name}.service"
  systemctl daemon-reload
}

# wait_http <url> <seconds> [label]
wait_http() {
  local url="$1" secs="${2:-60}" label="${3:-service}" i=0
  printf '[%s] waiting for %s ' "${SPIKE_TAG:-spike}" "$label"
  while [ "$i" -lt "$secs" ]; do
    if curl -sf -m 3 "$url" >/dev/null 2>&1; then echo " up"; return 0; fi
    printf '.'
    sleep 2
    i=$((i + 2))
  done
  echo " TIMEOUT"
  return 1
}
