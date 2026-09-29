#!/usr/bin/env bash
# Publish the aarch64 aec-audio-processing wheel as a GitHub release asset (tag wheels/aec-<version>).
# Usage: make upload-aec-wheel   (after scripts/release/build-aec-wheel.sh <device-ip>)
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"

WHEEL_DIR="${WHEEL_DIR:-${ROOT_DIR}/dist/aec}"
REPO="${GITHUB_REPO:-autonomous-ai/autonomous-os}"

if ! command -v gh >/dev/null 2>&1; then
  echo "Error: gh not found — brew install gh, then gh auth login"
  exit 1
fi

shopt -s nullglob
wheels=("${WHEEL_DIR}"/aec_audio_processing-*.whl)
shopt -u nullglob

if [[ ${#wheels[@]} -eq 0 ]]; then
  echo "Error: no wheel in ${WHEEL_DIR}"
  echo "Build one first: scripts/release/build-aec-wheel.sh <device-ip>"
  exit 1
fi

for wheel in "${wheels[@]}"; do
  name="$(basename "$wheel")"

    # Only the device ABI (cp312 aarch64) installs on lamps; reject anything else here.
  case "$name" in
    *cp312*aarch64*) ;;
    *)
      echo "Error: ${name} is not a cp312/aarch64 wheel — refusing to publish"
      exit 1
      ;;
  esac

  version="$(echo "$name" | cut -d- -f2)"
  tag="wheels/aec-${version}"
  sha256="$(shasum -a 256 "$wheel" | awk '{print $1}')"

  if gh release view "$tag" -R "$REPO" >/dev/null 2>&1; then
    echo "========== Release ${tag} exists — uploading ${name} =========="
      # --clobber replaces a same-version rebuild; normally bump the version instead.
    gh release upload "$tag" "$wheel" -R "$REPO" --clobber
  else
    echo "========== Creating release ${tag} =========="
      # Not an OS release: must not take the "Latest" badge.
    gh release create "$tag" "$wheel" -R "$REPO" \
      --title "aec-audio-processing ${version} (aarch64 wheel)" \
      --notes "Prebuilt \`cp312-cp312-linux_aarch64\` wheel for \`aec-audio-processing\` ${version}, used by HAL's \`aec\` extra (see docs/realtime-voice.md).

PyPI publishes Windows wheels only, so aarch64 otherwise compiles the vendored webrtc-audio-processing + abseil — 5m35s on a lamp (A523) against 2.98s to fetch this.

Built with \`scripts/release/build-aec-wheel.sh\` on Debian 12 / aarch64, requires glibc >= 2.34.

sha256: \`${sha256}\`" \
      --latest=false
  fi

  url="https://github.com/${REPO}/releases/download/${tag}/${name}"
  echo
  echo "  url:    ${url}"
  echo "  sha256: ${sha256}"
  echo
  echo "Pin it in hal/pyproject.toml:"
  echo
  echo "  [tool.uv.sources]"
  echo "  aec-audio-processing = { url = \"${url}\" }"
done
