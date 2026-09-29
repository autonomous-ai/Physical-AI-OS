#!/usr/bin/env bash
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"
source "${RELEASE_DIR}/ota-metadata.sh"

DIST_DIR="${ROOT_DIR}/system/web/dist"
ZIP_NAME="setup-web.zip"
ZIP_PATH="${ROOT_DIR}/${ZIP_NAME}"
VERSION_FILE="${ROOT_DIR}/system/VERSION_WEB"

echo "========== npm install =========="
(cd "$ROOT_DIR/system/web" && npm install)

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

GCS_PATH="${GCS_PATH:-${BUCKET_PREFIX}/ota/web/${new_version}.zip}"

echo "========== npm run build =========="
(cd "$ROOT_DIR/system/web" && npm run build)

if [[ ! -d "$DIST_DIR" ]]; then
  echo "Error: dist not found at $DIST_DIR"
  exit 1
fi

cp "$VERSION_FILE" "$DIST_DIR/VERSION"

echo "========== Zipping dist contents to ${ZIP_NAME} =========="
rm -f "$ZIP_PATH"
(cd "$DIST_DIR" && zip -r "$ZIP_PATH" .)

echo "========== Upload ${ZIP_NAME} to Google Cloud Storage (no-cache) =========="
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$ZIP_PATH" "gs://${GCS_BUCKET}/${GCS_PATH}"
ZIP_SHA256=$(ota_artifact_sha256 "$ZIP_PATH")

METADATA_PATH="${BUCKET_PREFIX}/ota/metadata.json"
METADATA_TMP=$(mktemp)
PAYLOAD_TMP=$(mktemp)
WEB_URL="${WEB_URL:-https://storage.googleapis.com/${GCS_BUCKET}/${GCS_PATH}}"

echo "========== Fetch metadata from gs://${GCS_BUCKET}/${METADATA_PATH} =========="
if gsutil cp "gs://${GCS_BUCKET}/${METADATA_PATH}" "$METADATA_TMP" 2>/dev/null; then
  ota_metadata_unpack "$METADATA_TMP" "$PAYLOAD_TMP"
else
  printf '{}' >"$PAYLOAD_TMP"
fi

updated_metadata=$(python3 - "$PAYLOAD_TMP" "$new_version" "$WEB_URL" "$ZIP_SHA256" "$(date '+%Y-%m-%d %H:%M:%S %z')" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
entry = data.get('web') if isinstance(data.get('web'), dict) else {}
entry.update({'version': sys.argv[2], 'url': sys.argv[3], 'sha256': sys.argv[4], 'updated_at': sys.argv[5]})
# preserve existing min_version (staged-rollout floor); bump it via promote-ota.sh
data['web'] = entry
print(json.dumps(data, indent=2))
PY
)

echo "$updated_metadata" > "$PAYLOAD_TMP"
ota_metadata_sign "$PAYLOAD_TMP" "$METADATA_TMP"
rm -f "$PAYLOAD_TMP"
echo "========== Upload metadata (web: v${new_version}) =========="
gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$METADATA_TMP" "gs://${GCS_BUCKET}/${METADATA_PATH}"
rm -f "$METADATA_TMP"

rm -f "$ZIP_PATH"
echo "Done: gs://${GCS_BUCKET}/${GCS_PATH} (v${new_version})"
