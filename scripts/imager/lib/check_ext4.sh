#!/bin/bash
# Only accept a fully clean, unmounted filesystem. Never mask fsck failures.
prepare_ext4_for_resize() {
  local status=0
  # resize2fs needs a writable forced check, including updated check metadata.
  # Preen fixes only problems considered safe; unresolved errors must stop build.
  e2fsck -fp "$1" || status=$?
  case "$status" in
    0|1) return 0 ;;
    *) echo "ERROR: pre-resize e2fsck failed (status=$status)" >&2; return "$status" ;;
  esac
}

check_ext4() {
  e2fsck -fn "$1"
}
