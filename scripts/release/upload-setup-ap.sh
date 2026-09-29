#!/usr/bin/env bash
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"

SETUP_FILE="${RELEASE_DIR}/../provision/setup-ap.sh"

GCS_PATH="${GCS_PATH:-${BUCKET_PREFIX}/setup-ap.sh}"

if [[ ! -f "$SETUP_FILE" ]]; then
  echo "Error: setup-ap.sh not found at $SETUP_FILE"
  exit 1
fi

echo "========== Upload setup-ap.sh to Google Cloud Storage (no-cache) =========="
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$SETUP_FILE" "gs://${GCS_BUCKET}/${GCS_PATH}"
echo "Done: gs://${GCS_BUCKET}/${GCS_PATH}"
