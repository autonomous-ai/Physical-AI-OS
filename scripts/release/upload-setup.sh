#!/usr/bin/env bash
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"

SETUP_FILE="${RELEASE_DIR}/../provision/setup.sh"

GCS_PATH="${GCS_PATH:-${BUCKET_PREFIX}/setup.sh}"

SWUPDATE_FILE="${RELEASE_DIR}/../provision/software-update"

if [[ ! -f "$SETUP_FILE" ]]; then
  echo "Error: setup.sh not found at $SETUP_FILE"
  exit 1
fi
if [[ ! -f "$SWUPDATE_FILE" ]]; then
  echo "Error: software-update not found at $SWUPDATE_FILE"
  exit 1
fi

# Inline the canonical updater between setup.sh's markers at release time, never in git.
BEGIN_MARK='  # >>> BEGIN software-update (generated)'
END_MARK='  # <<< END software-update (generated)'
UPLOAD_FILE="$(mktemp)"
trap 'rm -f "$UPLOAD_FILE"' EXIT

grep -qF "$BEGIN_MARK" "$SETUP_FILE" && grep -qF "$END_MARK" "$SETUP_FILE" || {
  echo "Error: software-update markers missing in $SETUP_FILE — did the block get hand-edited?"
  exit 1
}

awk -v begin="$BEGIN_MARK" -v end="$END_MARK" -v body="$SWUPDATE_FILE" '
  $0 == begin { print; print "  cat >/usr/local/bin/software-update <<'\''SOFTWAREUPDATE'\''";
                while ((getline line < body) > 0) print line;
                close(body); print "SOFTWAREUPDATE"; skip = 1; next }
  $0 == end   { skip = 0 }
  !skip       { print }
' "$SETUP_FILE" > "$UPLOAD_FILE"

bash -n "$UPLOAD_FILE" || { echo "Error: assembled setup.sh is not valid bash"; exit 1; }
grep -q "placeholder body" "$UPLOAD_FILE" && { echo "Error: placeholder survived inlining"; exit 1; }

echo "========== Upload setup.sh to Google Cloud Storage (no-cache) =========="
echo "  (software-update inlined from $SWUPDATE_FILE)"
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" cp "$UPLOAD_FILE" "gs://${GCS_BUCKET}/${GCS_PATH}"
echo "Done: gs://${GCS_BUCKET}/${GCS_PATH}"

# Also publish the raw updater standalone so field devices can fetch a newer one.
SWUPDATE_GCS_PATH="${SWUPDATE_GCS_PATH:-${BUCKET_PREFIX}/software-update}"
echo "========== Upload software-update to Google Cloud Storage (no-cache) =========="
gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
       -h "Content-Type:text/x-shellscript" \
       cp "$SWUPDATE_FILE" "gs://${GCS_BUCKET}/${SWUPDATE_GCS_PATH}"
echo "Done: gs://${GCS_BUCKET}/${SWUPDATE_GCS_PATH}"
