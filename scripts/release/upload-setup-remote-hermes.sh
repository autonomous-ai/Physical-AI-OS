#!/usr/bin/env bash
# Publish scripts/tools/setup-remote-hermes.sh to {CDN}/tools/ (unversioned, 5 min cache).
set -euo pipefail

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"

SCRIPT="${ROOT_DIR}/scripts/tools/setup-remote-hermes.sh"
DEST="gs://${GCS_BUCKET}/${BUCKET_PREFIX}/tools/setup-remote-hermes.sh"

[ -f "$SCRIPT" ] || { echo "Error: $SCRIPT not found" >&2; exit 1; }
bash -n "$SCRIPT" || { echo "Error: $SCRIPT failed syntax check" >&2; exit 1; }

echo "==> uploading $SCRIPT"
echo "    → $DEST"
gcloud storage cp --cache-control="public, max-age=300" \
  "$SCRIPT" "$DEST"

echo "==> public URL"
echo "    https://cdn.autonomous.ai/${BUCKET_PREFIX}/tools/setup-remote-hermes.sh"
echo
echo "Cloudflare edges may still hold an older copy; force a fresh fetch with:"
echo "  curl -fsSL \"https://cdn.autonomous.ai/${BUCKET_PREFIX}/tools/setup-remote-hermes.sh?v=\$(uuidgen)\""
