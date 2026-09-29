#!/usr/bin/env bash
# Stricter than systemctl is-active: systemd reports active before the listener binds.
set -euo pipefail

curl --fail --silent --show-error --max-time 5 \
  -H 'Authorization: Bearer hermes-local-api-key' \
  http://127.0.0.1:8642/health >/dev/null
