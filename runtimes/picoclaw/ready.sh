#!/usr/bin/env bash
# A successful HTTP 101 verifies that PicoClaw has bound its WebSocket endpoint and accepts the
# device-local bearer credential.
set -euo pipefail

headers="$(curl --http1.1 --silent --max-time 2 -D - -o /dev/null \
  -H 'Connection: Upgrade' \
  -H 'Upgrade: websocket' \
  -H 'Sec-WebSocket-Version: 13' \
  -H 'Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==' \
  -H 'Authorization: Bearer darren_pico_token' \
  http://127.0.0.1:18790/pico/ws/ 2>/dev/null || true)"
grep -q '^HTTP/1.1 101 ' <<<"$headers"
