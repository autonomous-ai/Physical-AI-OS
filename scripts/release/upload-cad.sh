#!/usr/bin/env bash
# Upload a CAD file to Mega.nz and print a public link (needs MEGAcmd and a prior mega-login).
# Usage: scripts/release/upload-cad.sh <local-file> [remote-dir]
set -euo pipefail

LOCAL_FILE="${1:-}"
REMOTE_DIR="${2:-/lamp-cad}"

if [[ -z "$LOCAL_FILE" ]]; then
  echo "usage: $(basename "$0") <local-file> [remote-dir]" >&2
  exit 2
fi

if [[ ! -f "$LOCAL_FILE" ]]; then
  echo "error: file not found: $LOCAL_FILE" >&2
  exit 1
fi

if ! command -v mega-put >/dev/null 2>&1; then
  echo "error: MEGAcmd not installed. Run: brew install --cask megacmd" >&2
  exit 1
fi

if ! mega-whoami >/dev/null 2>&1; then
  echo "error: not logged in to Mega. Run: mega-login <email> <password>" >&2
  exit 1
fi

FILENAME="$(basename "$LOCAL_FILE")"
REMOTE_PATH="${REMOTE_DIR%/}/${FILENAME}"

echo "[upload-cad] account: $(mega-whoami)"
echo "[upload-cad] ensuring remote dir: ${REMOTE_DIR}"
mega-mkdir -p "$REMOTE_DIR" >/dev/null 2>&1 || true

echo "[upload-cad] uploading ${LOCAL_FILE} -> mega:${REMOTE_PATH}"
mega-put -c "$LOCAL_FILE" "$REMOTE_DIR/"

echo "[upload-cad] creating public share link"
LINK="$(mega-export -a -f "$REMOTE_PATH" 2>/dev/null | grep -Eo 'https://mega\.nz/[^[:space:]]+' | head -n1 || true)"

if [[ -z "$LINK" ]]; then
  echo "[upload-cad] warning: could not parse share link; run manually:" >&2
  echo "  mega-export -a $REMOTE_PATH" >&2
  exit 1
fi

echo ""
echo "Public link: $LINK"
echo ""
echo "Paste this into robots/lamp/hardware/cad/README.md."
