#!/bin/bash
# Build a local development app; this does not launch it or request permission.
set -euo pipefail
umask 077
if [[ $# -lt 1 || $# -gt 2 || "$(uname -s)" != Darwin ]]; then
    echo 'usage (macOS): build.sh /absolute/NEW/LampRoomObserver.app [release|debug]' >&2
    exit 1
fi
observer_app="$1"
observer_profile="${2:-release}"
case "$observer_app" in /*.app) ;; *) echo 'output must be an absolute .app path' >&2; exit 1 ;; esac
if [[ -e "$observer_app" || -L "$observer_app" || -e "$observer_app.binary-sha256.txt" || -L "$observer_app.binary-sha256.txt" ]]; then
    echo 'output app already exists; choose a fresh path' >&2
    exit 1
fi
observer_script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
observer_root="$(cd "$observer_script_dir/../.." && pwd -P)"
observer_cargo="${CARGO:-cargo}"
observer_target="$(mktemp -d "${TMPDIR:-/private/tmp}/lamp-observer-build.XXXXXX")"
# This path is created above by mktemp and is never supplied by the caller.
observer_cleanup() {
    observer_status=$?
    trap - EXIT
    rm -rf "$observer_target"
    exit "$observer_status"
}
trap observer_cleanup EXIT
observer_flags=(--profile dev)
case "$observer_profile" in
    release) observer_flags=(--release) ;;
    debug) ;;
    *) echo 'profile must be release or debug' >&2; exit 1 ;;
esac
"$observer_cargo" build --manifest-path "$observer_root/Cargo.toml" --locked \
    --target-dir "$observer_target" -p lamp-observer "${observer_flags[@]}"
/usr/bin/plutil -lint "$observer_script_dir/Info.plist" "$observer_script_dir/Entitlements.plist"
/bin/mkdir -m 700 "$observer_app"
/bin/mkdir -m 700 "$observer_app/Contents" "$observer_app/Contents/MacOS" "$observer_app/Contents/Resources"
/bin/cp "$observer_target/$observer_profile/lamp-observer" "$observer_app/Contents/MacOS/lamp-observer"
/bin/chmod 755 "$observer_app/Contents/MacOS/lamp-observer"
/bin/cp "$observer_script_dir/Info.plist" "$observer_app/Contents/Info.plist"
/usr/bin/codesign --force --sign "${LAMP_OBSERVER_SIGN_IDENTITY:--}" --options runtime \
    --entitlements "$observer_script_dir/Entitlements.plist" "$observer_app"
/usr/bin/codesign --verify --strict --verbose=2 "$observer_app"
/usr/bin/shasum -a 256 "$observer_app/Contents/MacOS/lamp-observer" > "$observer_app.binary-sha256.txt"
printf 'Built %s\n' "$observer_app"
printf 'No app launch, permission request, or recording has been performed.\n'
