#!/usr/bin/env bash
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"
source "${RELEASE_DIR}/ota-metadata.sh"

TWITCH_IRC_BIN="${ROOT_DIR}/integrations/chat-bridges/twitch-chat-hook/twitch-irc"
VERSION_FILE="${ROOT_DIR}/integrations/chat-bridges/twitch-chat-hook/${VERSION_FILE:-VERSION_TWITCH_IRC}"

if [[ -f "$VERSION_FILE" ]]; then
  version=$(cat "$VERSION_FILE" | tr -d '[:space:]')
  IFS='.' read -r major minor patch <<< "$version"
  patch=$((patch + 1))
  new_version="${major}.${minor}.${patch}"
  echo "$new_version" > "$VERSION_FILE"
  echo "========== Version bumped: ${version} -> ${new_version} =========="
else
  echo "1.0.0" > "$VERSION_FILE"
  new_version="1.0.0"
  echo "========== Version initialized: ${new_version} =========="
fi

ZIP_NAME="twitch-irc-${new_version}.zip"
ZIP_PATH="${ROOT_DIR}/${ZIP_NAME}"
GCS_PATH="${GCS_PATH:-${BUCKET_PREFIX}/ota/twitch-irc/${new_version}.zip}"

echo "========== Build twitch-irc binary (VERSION=${new_version}) =========="
(cd "$ROOT_DIR" && make twitch-build-irc VERSION="$new_version")

if [[ ! -f "$TWITCH_IRC_BIN" ]]; then
  echo "Error: twitch-irc binary not found at $TWITCH_IRC_BIN after make twitch-build-irc"
  exit 1
fi

echo "========== Zipping twitch-irc binary to ${ZIP_NAME} =========="
rm -f "$ZIP_PATH"
(cd "$ROOT_DIR" && zip "$ZIP_PATH" "$TWITCH_IRC_BIN")

echo "========== Upload ${ZIP_NAME} to Google Cloud Storage (no-cache) =========="
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$ZIP_PATH" "gs://${GCS_BUCKET}/${GCS_PATH}"

METADATA_PATH="${BUCKET_PREFIX}/ota/metadata.json"
METADATA_TMP=$(mktemp)
PAYLOAD_TMP=$(mktemp)
BACKEND_URL="${BACKEND_URL:-https://storage.googleapis.com/${GCS_BUCKET}/${GCS_PATH}}"

echo "========== Fetch metadata from gs://${GCS_BUCKET}/${METADATA_PATH} =========="
if gsutil cp "gs://${GCS_BUCKET}/${METADATA_PATH}" "$METADATA_TMP" 2>/dev/null; then
  ota_metadata_unpack "$METADATA_TMP" "$PAYLOAD_TMP"
else
  printf '{}' >"$PAYLOAD_TMP"
fi

updated_metadata=$(python3 - "$PAYLOAD_TMP" "$new_version" "$BACKEND_URL" "$(date '+%Y-%m-%d %H:%M:%S %z')" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
data['twitch-irc'] = {'version': sys.argv[2], 'url': sys.argv[3], 'updated_at': sys.argv[4]}
print(json.dumps(data, indent=2))
PY
)

echo "$updated_metadata" > "$PAYLOAD_TMP"
ota_metadata_sign "$PAYLOAD_TMP" "$METADATA_TMP"
echo "========== Upload metadata (backend: v${new_version}) =========="
gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$METADATA_TMP" "gs://${GCS_BUCKET}/${METADATA_PATH}"
rm -f "$METADATA_TMP" "$PAYLOAD_TMP"

rm -f "$ZIP_PATH" "$TWITCH_IRC_BIN"
echo "Done: gs://${GCS_BUCKET}/${GCS_PATH} (v${new_version})"
