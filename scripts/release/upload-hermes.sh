#!/usr/bin/env bash
set -e

# Publish a Hermes CLI version pinned to an upstream commit to OTA metadata; roll out with `make promote-hermes`.
# Usage: ./scripts/release/upload-hermes.sh <version_str> <upstream-tag|commit-sha>

if [[ -z "${1:-}" || -z "${2:-}" ]]; then
  echo "Usage: $0 <hermes-version> <upstream-tag|commit-sha>" >&2
  echo "Example: $0 0.21.1 v2026.9.7   (bare semver — no leading 'v'; tag as published upstream)" >&2
  exit 1
fi
VERSION="$1"
REF="$2"
HERMES_REPO="https://github.com/NousResearch/hermes-agent"

# Compared against `hermes --version`; a "v" prefix would never match and loop updates.
if [[ "$VERSION" == v* ]]; then
  echo "ERROR: pass the bare semver (0.21.1), not a v-prefixed tag ($VERSION)." >&2
  exit 1
fi

# Installer needs a full 40-char SHA; tags must be peeled (^{}) to the commit.
if [[ "$REF" =~ ^[0-9a-fA-F]{40}$ ]]; then
  COMMIT="$REF"
else
  COMMIT="$(git ls-remote --tags "$HERMES_REPO" "refs/tags/${REF}^{}" | awk '{print $1}' | head -1)"
  [[ -n "$COMMIT" ]] || COMMIT="$(git ls-remote --tags "$HERMES_REPO" "refs/tags/${REF}" | awk '{print $1}' | head -1)"
  if [[ ! "$COMMIT" =~ ^[0-9a-fA-F]{40}$ ]]; then
    echo "ERROR: could not resolve '$REF' to a commit on $HERMES_REPO (try: git ls-remote --tags $HERMES_REPO)" >&2
    exit 1
  fi
fi
echo "hermes $VERSION → $REF = $COMMIT"

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

python3 - "$PAYLOAD_TMP" "$VERSION" "$COMMIT" "$(date '+%Y-%m-%d %H:%M:%S %z')" <<'PY'
import json
import sys

path, version, commit, updated_at = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
d = json.load(open(path))
hm = d.get("hermes") if isinstance(d.get("hermes"), dict) else {}
hm["version"] = version
hm["commit"] = commit
hm["updated_at"] = updated_at
d["hermes"] = hm
json.dump(d, open(path, "w"), indent=4)
PY

ota_metadata_sign "$PAYLOAD_TMP" "$METADATA_TMP"

gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
       -h "Content-Type:application/json" \
       cp "$METADATA_TMP" "$METADATA_GCS"

echo "Updated $METADATA_GCS: hermes.version = ${VERSION}, hermes.commit = ${COMMIT}"
echo "Fleet is NOT updated yet — run 'make promote-hermes' to raise min_version."
