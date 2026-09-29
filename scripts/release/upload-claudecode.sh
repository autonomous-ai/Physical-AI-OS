#!/usr/bin/env bash
set -e

# Publish a Claude Code CLI version (bare semver, e.g. 2.1.218) to OTA metadata; roll out with `make promote-claudecode`.
# Usage: ./scripts/release/upload-claudecode.sh <version_str>

if [[ -z "${1:-}" ]]; then
  echo "Usage: $0 <claudecode-version>" >&2
  echo "Example: $0 2.1.218   (bare semver — no leading 'v')" >&2
  exit 1
fi
VERSION="$1"

# Compared against `claude --version`; a "v" prefix would never match and loop updates.
if [[ "$VERSION" == v* ]]; then
  echo "ERROR: pass the bare semver (2.1.218), not a v-prefixed tag ($VERSION)." >&2
  exit 1
fi

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"
source "${RELEASE_DIR}/ota-metadata.sh"
METADATA_GCS="gs://${GCS_BUCKET}/${BUCKET_PREFIX}/ota/metadata.json"

METADATA_TMP=$(mktemp)
PAYLOAD_TMP=$(mktemp)
trap 'rm -f "$METADATA_TMP" "$PAYLOAD_TMP"' EXIT

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
cc = d.get("claudecode") if isinstance(d.get("claudecode"), dict) else {}
cc["version"] = version
cc["updated_at"] = updated_at
d["claudecode"] = cc
json.dump(d, open(path, "w"), indent=4)
PY

ota_metadata_sign "$PAYLOAD_TMP" "$METADATA_TMP"

gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
       -h "Content-Type:application/json" \
       cp "$METADATA_TMP" "$METADATA_GCS"

echo "Updated $METADATA_GCS: claudecode.version = ${VERSION}"
echo "Fleet is NOT updated yet — run 'make promote-claudecode' to raise min_version."
