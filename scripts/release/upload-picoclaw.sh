#!/usr/bin/env bash
set -e

# Publish a PicoClaw release TAG (e.g. v0.3.1-fixvision, not semver) to OTA metadata; roll out with `make promote-picoclaw`.
# Usage: ./scripts/release/upload-picoclaw.sh <release_tag>

if [[ -z "${1:-}" ]]; then
  echo "Usage: $0 <picoclaw-release-tag>" >&2
  echo "Example: $0 v0.3.1-fixvision   (the GitHub release TAG, not a bare semver)" >&2
  exit 1
fi
VERSION="$1"

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"
source "${RELEASE_DIR}/ota-metadata.sh"
METADATA_GCS="gs://${GCS_BUCKET}/${BUCKET_PREFIX}/ota/metadata.json"

METADATA_TMP=$(mktemp)
PAYLOAD_TMP=$(mktemp)
trap 'rm -f "$METADATA_TMP" "$PAYLOAD_TMP"' EXIT

# The tag becomes a device download URL; fail here rather than on every device.
PICO_REPO="${PICO_REPO:-autonomous-ai/picoclaw}"
if ! curl -fsSL -o /dev/null "https://github.com/${PICO_REPO}/releases/download/${VERSION}/picoclaw-linux-arm64"; then
  echo "ERROR: no picoclaw-linux-arm64 asset at release tag '${VERSION}' in ${PICO_REPO}." >&2
  exit 1
fi

if ! gsutil cp "$METADATA_GCS" "$METADATA_TMP" 2>/dev/null; then
  echo "Note: $METADATA_GCS not found — bootstrapping with empty object."
  printf '{}' > "$PAYLOAD_TMP"
else
  ota_metadata_unpack "$METADATA_TMP" "$PAYLOAD_TMP"
fi

python3 - "$PAYLOAD_TMP" "$VERSION" "$(date '+%Y-%m-%d %H:%M:%S %z')" <<'PY'
import json
import sys

path, version, updated_at = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(path))
pc = d.get("picoclaw") if isinstance(d.get("picoclaw"), dict) else {}
pc["version"] = version
pc["updated_at"] = updated_at
d["picoclaw"] = pc
json.dump(d, open(path, "w"), indent=4)
PY

ota_metadata_sign "$PAYLOAD_TMP" "$METADATA_TMP"

gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
       -h "Content-Type:application/json" \
       cp "$METADATA_TMP" "$METADATA_GCS"

echo "Updated $METADATA_GCS: picoclaw.version = ${VERSION}"
echo "Fleet is NOT updated yet — run 'make promote-picoclaw' to raise min_version."
