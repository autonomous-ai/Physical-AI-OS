#!/usr/bin/env bash
# Raise an OTA component's min_version (default: its current version) so bootstrap auto-rolls it out.
# Usage: ./scripts/release/promote-ota.sh <component|device:<type>> [min_version]
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"
source "${RELEASE_DIR}/ota-metadata.sh"

component="${1:-}"
override_min="${2:-}"
if [[ "$component" == "device" ]]; then
  dtype="${2:-}"
  if [[ -z "$dtype" ]]; then
    echo "Usage: $0 device <type> [min_version]   (e.g. device lamp)" >&2
    exit 1
  fi
  component="device:${dtype}"
  override_min="${3:-}"
fi
if [[ -z "$component" ]]; then
  echo "Usage: $0 <component> [min_version]   (flat key | device <type> | device:<type>)" >&2
  exit 1
fi

METADATA_PATH="${BUCKET_PREFIX}/ota/metadata.json"
METADATA_GCS="gs://${GCS_BUCKET}/${METADATA_PATH}"
METADATA_TMP=$(mktemp)
PAYLOAD_TMP=$(mktemp)
trap 'rm -f "$METADATA_TMP" "$PAYLOAD_TMP"' EXIT

echo "========== Fetch metadata from ${METADATA_GCS} =========="
gsutil cp "$METADATA_GCS" "$METADATA_TMP"
ota_metadata_unpack "$METADATA_TMP" "$PAYLOAD_TMP"

python3 - "$PAYLOAD_TMP" "$component" "$override_min" <<'PY'
import json, sys

path, comp, override = sys.argv[1], sys.argv[2], sys.argv[3]
data = json.load(open(path))

if comp.startswith("device:"):
    dtype = comp.split(":", 1)[1]
    entry = data.get("devices", {}).get(dtype)
    where = "devices.%s" % dtype
else:
    entry = data.get(comp)
    where = comp

if not isinstance(entry, dict):
    sys.exit("no metadata entry for %s" % where)

target = override or entry.get("version")
if not target:
    sys.exit("%s has no version to promote to" % where)

old = entry.get("min_version", "(unset -> version)")
entry["min_version"] = target
json.dump(data, open(path, "w"), indent=2)
print("%s: min_version %s -> %s" % (where, old, target))
PY

echo "========== Upload metadata to ${METADATA_GCS} =========="
ota_metadata_sign "$PAYLOAD_TMP" "$METADATA_TMP"
gsutil -h "Content-Type:application/json" \
       -h "Cache-Control:no-cache, no-store, must-revalidate" \
       cp "$METADATA_TMP" "$METADATA_GCS"

echo "Promoted. Devices with current version below min_version will now auto-update."
