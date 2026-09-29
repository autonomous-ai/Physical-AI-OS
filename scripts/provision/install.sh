#!/bin/bash
# Run this on the Pi to download and execute the latest setup from CDN.
# Usage: curl -fsSL https://storage.googleapis.com/s3-autonomous-upgrade-3/os/install.sh | sudo bash
set -euo pipefail

# Per-deployment OTA metadata URL; override by exporting OTA_METADATA_URL.
OTA_METADATA_URL="${OTA_METADATA_URL:-https://storage.googleapis.com/s3-autonomous-upgrade-3/os/ota/metadata.json}"

# Required device class; set after sudo (sudo drops a leading env): curl … | sudo DEVICE_TYPE=lamp bash
DEVICE_TYPE="${DEVICE_TYPE:?DEVICE_TYPE must be set (e.g. DEVICE_TYPE=lamp) — no default}"

curl -fsSL -H "Cache-Control: no-cache" -H "Pragma: no-cache" \
  -o /tmp/setup.sh \
  "https://cdn.autonomous.ai/os/setup.sh"
chmod +x /tmp/setup.sh
OTA_METADATA_URL="$OTA_METADATA_URL" OTA_SIGNING_PUBLIC_KEY="${OTA_SIGNING_PUBLIC_KEY:-}" DEVICE_TYPE="$DEVICE_TYPE" bash /tmp/setup.sh
