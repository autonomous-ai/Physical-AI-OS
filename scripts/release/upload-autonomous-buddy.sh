#!/usr/bin/env bash
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"
source "${RELEASE_DIR}/ota-metadata.sh"

BUDDY_DIR="${ROOT_DIR}/integrations/companions/autonomous-buddy"
VERSION_FILE="${BUDDY_DIR}/VERSION_AUTONOMOUS_BUDDY"
DIST_DIR="${BUDDY_DIR}/dist"
DMG_TARGET="${BUDDY_DMG_TARGET:-dmg-signed}"
SKIP_BUILD="${BUDDY_SKIP_BUILD:-0}"
ARCHS="${BUDDY_ARCHS:-arm64 x64}"
case "$SKIP_BUILD" in
  0|1) ;;
  *) echo "Error: BUDDY_SKIP_BUILD must be 0 or 1" >&2; exit 1 ;;
esac
case "$DMG_TARGET" in
  dmg|dmg-signed) ;;
  *) echo "Error: BUDDY_DMG_TARGET must be dmg or dmg-signed" >&2; exit 1 ;;
esac
read -r -a architectures <<< "$ARCHS"
if [[ ${#architectures[@]} -eq 0 ]]; then
  echo "Error: BUDDY_ARCHS must contain arm64 and/or x64" >&2
  exit 1
fi
for arch in "${architectures[@]}"; do
  case "$arch" in
    arm64|x64) ;;
    *) echo "Error: unsupported architecture: $arch" >&2; exit 1 ;;
  esac
done
if [[ -n "${GCS_PATH:-}" && "$GCS_PATH" != *.dmg ]] || [[ -n "${BUDDY_URL:-}" && "$BUDDY_URL" != https://*.dmg ]]; then
  echo "Error: custom destinations must end in .dmg and BUDDY_URL must use HTTPS" >&2
  exit 1
fi
if [[ ${#architectures[@]} -gt 1 && ( -n "${GCS_PATH:-}" || -n "${BUDDY_URL:-}" ) ]]; then
  echo "Error: GCS_PATH/BUDDY_URL overrides require a single BUDDY_ARCHS value" >&2
  exit 1
fi
if [[ "$DMG_TARGET" == "dmg-signed" && "$SKIP_BUILD" == "0" ]]; then
  : "${NOTARY_PROFILE:?Set NOTARY_PROFILE to your notarytool Keychain profile}"
  xcrun notarytool history --keychain-profile "$NOTARY_PROFILE" --output-format json >/dev/null
fi

current_version=$(tr -d '[:space:]' < "$VERSION_FILE")
if [[ ! "$current_version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "Error: invalid Buddy version: $current_version" >&2
  exit 1
fi
new_version="$current_version"
if [[ "$SKIP_BUILD" == "0" ]]; then
  IFS='.' read -r major minor patch <<< "$current_version"
  new_version="${major}.${minor}.$((10#$patch + 1))"
  echo "$new_version" > "$VERSION_FILE"
  echo "========== Version bumped: ${current_version} -> ${new_version} =========="
  (cd "$BUDDY_DIR" && make "$DMG_TARGET" BUDDY_ARCHS="$ARCHS")
fi

for arch in "${architectures[@]}"; do
  dmg_path="${DIST_DIR}/Autonomous-Buddy-${new_version}-${arch}.dmg"
  if [[ ! -f "$dmg_path" ]]; then
    echo "Error: expected DMG not found at $dmg_path" >&2
    exit 1
  fi
  if [[ "$DMG_TARGET" == "dmg-signed" ]]; then
    xcrun stapler validate "$dmg_path"
    spctl --assess --type open --context context:primary-signature "$dmg_path"
    node "${BUDDY_DIR}/desktop/scripts/package-update.mjs" "$dmg_path" \
      "${DIST_DIR}/Autonomous-Buddy-${new_version}-${arch}.zip" "$new_version" "$arch"
  else
    node "${BUDDY_DIR}/desktop/scripts/package-update.mjs" "$dmg_path" \
      - "$new_version" "$arch" verify-only
  fi
done

METADATA_PATH="${BUCKET_PREFIX}/ota/metadata.json"
METADATA_TMP=$(mktemp)
PAYLOAD_TMP=$(mktemp)
ENTRIES_TMP=$(mktemp)
FEED_DIR=$(mktemp -d)
trap 'rm -f "$METADATA_TMP" "$PAYLOAD_TMP" "$ENTRIES_TMP"; rm -rf "$FEED_DIR"' EXIT
printf '{}' > "$ENTRIES_TMP"

for arch in "${architectures[@]}"; do
  dmg_path="${DIST_DIR}/Autonomous-Buddy-${new_version}-${arch}.dmg"
  gcs_path="${GCS_PATH:-${BUCKET_PREFIX}/ota/autonomous-buddy/${arch}/${new_version}.dmg}"
  buddy_url="${BUDDY_URL:-https://storage.googleapis.com/${GCS_BUCKET}/${gcs_path}}"
  checksum=$(ota_artifact_sha256 "$dmg_path")
  echo "========== Upload Buddy ${new_version} (${arch}) =========="
  gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
         -h "Content-Type:application/x-apple-diskimage" \
         cp "$dmg_path" "gs://${GCS_BUCKET}/${gcs_path}"
  if [[ "$DMG_TARGET" == "dmg-signed" ]]; then
    zip_path="${DIST_DIR}/Autonomous-Buddy-${new_version}-${arch}.zip"
    zip_gcs_path="${gcs_path%/*}/${new_version}.zip"
    zip_url="${buddy_url%/*}/${new_version}.zip"
    node "${BUDDY_DIR}/desktop/scripts/update-feed.mjs" "$new_version" "$zip_url" \
      "$zip_path" "${FEED_DIR}/${arch}.json"
    gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
           -h "Content-Type:application/zip" \
           cp "$zip_path" "gs://${GCS_BUCKET}/${zip_gcs_path}"
  fi
  entries=$(jq --arg arch "$arch" --arg version "$new_version" \
    --arg url "$buddy_url" --arg sha256 "$checksum" \
    --arg updated_at "$(date '+%Y-%m-%d %H:%M:%S %z')" \
    '.[$arch] = {version:$version,url:$url,sha256:$sha256,updated_at:$updated_at}' "$ENTRIES_TMP")
  printf '%s\n' "$entries" > "$ENTRIES_TMP"
  echo "URL (${arch}): ${buddy_url}"
done

# Fail closed: a fetch failure must never replace metadata with an incomplete document.
gsutil cp "gs://${GCS_BUCKET}/${METADATA_PATH}" "$METADATA_TMP"
ota_metadata_unpack "$METADATA_TMP" "$PAYLOAD_TMP"
if jq -e '.signed != null or .format == "autonomous-ota/v1"' "$METADATA_TMP" >/dev/null && [[ -z "${OTA_SIGNING_PRIVATE_KEY:-}" ]]; then
  echo "Error: signing key required to update signed OTA metadata" >&2
  exit 1
fi
updated_metadata=$(jq --slurpfile entries "$ENTRIES_TMP" \
  '."autonomous-buddy" = ((."autonomous-buddy" // {} |
    if .url != null and .arm64 == null then
      .arm64 = ({version,url,updated_at} + (if .sha256 != null then {sha256} else {} end))
    else . end |
    del(.version,.url,.sha256,.updated_at,.min_version)) + $entries[0])' \
  "$PAYLOAD_TMP")
printf '%s\n' "$updated_metadata" > "$PAYLOAD_TMP"
ota_metadata_sign "$PAYLOAD_TMP" "$METADATA_TMP"
gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" \
  cp "$METADATA_TMP" "gs://${GCS_BUCKET}/${METADATA_PATH}"
if [[ "$DMG_TARGET" == "dmg-signed" ]]; then
  for arch in "${architectures[@]}"; do
    gcs_path="${GCS_PATH:-${BUCKET_PREFIX}/ota/autonomous-buddy/${arch}/${new_version}.dmg}"
    gsutil -h "Content-Type:application/json" -h "Cache-Control:no-cache, no-store, must-revalidate" \
      cp "${FEED_DIR}/${arch}.json" "gs://${GCS_BUCKET}/${gcs_path%/*}/latest.json"
  done
fi
echo "Done: published Buddy ${new_version} for ${ARCHS}"
