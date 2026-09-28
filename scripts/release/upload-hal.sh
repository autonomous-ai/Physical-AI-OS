#!/usr/bin/env bash
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"
source "${RELEASE_DIR}/ota-metadata.sh"

HAL_DIR="${ROOT_DIR}/hal"
VERSION_FILE="${ROOT_DIR}/hal/${VERSION_FILE:-VERSION_HAL}"

# Bucket and path: ${BUCKET_PREFIX}/ota/hal/[semver].zip

# Verify reproducible dependencies before bumping or publishing a release.
# Fresh resolution on a device can fail even when the release host's lock works.
command -v uv >/dev/null 2>&1 || { echo "Error: uv is required to validate the HAL lockfile" >&2; exit 1; }
uv lock --project "$HAL_DIR" --python 3.12 --check

# Auto-increment semver (patch) before upload
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

ZIP_NAME="hal-${new_version}.zip"
ZIP_PATH="${ROOT_DIR}/${ZIP_NAME}"
GCS_PATH="${GCS_PATH:-${BUCKET_PREFIX}/ota/hal/${new_version}.zip}"

if [[ ! -d "$HAL_DIR" ]]; then
  echo "Error: hal directory not found at $HAL_DIR"
  exit 1
fi

echo "========== Zipping hal to ${ZIP_NAME} =========="
rm -f "$ZIP_PATH"
(cd "$HAL_DIR" && zip -r "$ZIP_PATH" . \
  -x ".venv/*" "__pycache__/*" "*/__pycache__/*" ".git/*" "*.pyc" \
  ".env" ".python-version" "test/*")

echo "========== Upload ${ZIP_NAME} to Google Cloud Storage (no-cache) =========="
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$ZIP_PATH" "gs://${GCS_BUCKET}/${GCS_PATH}"
ZIP_SHA256=$(ota_artifact_sha256 "$ZIP_PATH")

# Update metadata.json (${BUCKET_PREFIX}/ota/metadata.json) - hal key
METADATA_PATH="${BUCKET_PREFIX}/ota/metadata.json"
METADATA_TMP=$(mktemp)
PAYLOAD_TMP=$(mktemp)
HAL_URL="${HAL_URL:-https://storage.googleapis.com/${GCS_BUCKET}/${GCS_PATH}}"

echo "========== Fetch metadata from gs://${GCS_BUCKET}/${METADATA_PATH} =========="
if gsutil cp "gs://${GCS_BUCKET}/${METADATA_PATH}" "$METADATA_TMP" 2>/dev/null; then
  ota_metadata_unpack "$METADATA_TMP" "$PAYLOAD_TMP"
else
  printf '{}' >"$PAYLOAD_TMP"
fi

updated_metadata=$(python3 - "$PAYLOAD_TMP" "$new_version" "$HAL_URL" "$ZIP_SHA256" "$(date '+%Y-%m-%d %H:%M:%S %z')" <<'PY'
import json, sys
data = json.load(open(sys.argv[1]))
entry = data.get('hal') if isinstance(data.get('hal'), dict) else {}
entry.update({'version': sys.argv[2], 'url': sys.argv[3], 'sha256': sys.argv[4], 'updated_at': sys.argv[5]})
# preserve existing min_version (staged-rollout floor); bump it via promote-ota.sh
data['hal'] = entry
print(json.dumps(data, indent=2))
PY
)

echo "$updated_metadata" > "$PAYLOAD_TMP"
ota_metadata_sign "$PAYLOAD_TMP" "$METADATA_TMP"
rm -f "$PAYLOAD_TMP"
echo "========== Upload metadata (hal: v${new_version}) =========="
gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$METADATA_TMP" "gs://${GCS_BUCKET}/${METADATA_PATH}"
rm -f "$METADATA_TMP"

rm -f "$ZIP_PATH"
echo "Done: gs://${GCS_BUCKET}/${GCS_PATH} (v${new_version})"
