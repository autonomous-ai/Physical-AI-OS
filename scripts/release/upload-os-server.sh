#!/usr/bin/env bash
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"
source "${RELEASE_DIR}/ota-metadata.sh"

OS_BIN="${ROOT_DIR}/system/os-server"
VERSION_FILE="${ROOT_DIR}/system/${VERSION_FILE:-VERSION_OS_SERVER}"

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

ZIP_NAME="os-server-${new_version}.zip"
ZIP_PATH="${ROOT_DIR}/${ZIP_NAME}"
GCS_PATH="${GCS_PATH:-${BUCKET_PREFIX}/ota/os-server/${new_version}.zip}"

echo "========== Build os-server binary (VERSION=${new_version}) =========="
(cd "$ROOT_DIR" && make os-build VERSION="$new_version")

if [[ ! -f "$OS_BIN" ]]; then
  echo "Error: os-server binary not found at $OS_BIN after make os-build"
  exit 1
fi

echo "========== Zipping os-server binary to ${ZIP_NAME} =========="
rm -f "$ZIP_PATH"
(cd "$ROOT_DIR" && zip "$ZIP_PATH" "$OS_BIN")

echo "========== Upload ${ZIP_NAME} to Google Cloud Storage (no-cache) =========="
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$ZIP_PATH" "gs://${GCS_BUCKET}/${GCS_PATH}"
ZIP_SHA256=$(ota_artifact_sha256 "$ZIP_PATH")

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

python3 - "$PAYLOAD_TMP" "$new_version" "$BACKEND_URL" "$ZIP_SHA256" "$(date '+%Y-%m-%d %H:%M:%S %z')" <<'PY'
import json, sys
path, version, url, digest, updated_at = sys.argv[1:]
data = json.load(open(path))
entry = data.get('os-server') if isinstance(data.get('os-server'), dict) else {}
entry.update({'version': version, 'url': url, 'sha256': digest, 'updated_at': updated_at})
# preserve existing min_version (staged-rollout floor); bump it via promote-ota.sh
data['os-server'] = entry
json.dump(data, open(path, 'w'), separators=(',', ':'))
PY

ota_metadata_sign "$PAYLOAD_TMP" "$METADATA_TMP"
echo "========== Upload metadata (backend: v${new_version}) =========="
gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$METADATA_TMP" "gs://${GCS_BUCKET}/${METADATA_PATH}"
rm -f "$METADATA_TMP" "$PAYLOAD_TMP"

rm -f "$ZIP_PATH" "$OS_BIN"
echo "Done: gs://${GCS_BUCKET}/${GCS_PATH} (v${new_version})"
