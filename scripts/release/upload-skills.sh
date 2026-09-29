#!/usr/bin/env bash
set -e

source "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/ota-config.sh"

SKILLS_DIR="${ROOT_DIR}/skills"
GCS_PREFIX="${GCS_PREFIX:-${BUCKET_PREFIX}/skills}"

if [[ ! -d "$SKILLS_DIR" ]]; then
  echo "Error: skills directory not found at $SKILLS_DIR"
  exit 1
fi

HASH_CACHE="${RELEASE_DIR}/.skill-hashes"
touch "$HASH_CACHE"

WORK_DIR="$(mktemp -d)"
ENTRIES_FILE="$(mktemp)"
trap 'rm -rf "$WORK_DIR" "$ENTRIES_FILE"' EXIT

count=0
skipped=0

for skill_dir in "$SKILLS_DIR"/*/; do
  [[ -d "$skill_dir" ]] || continue
  skill_name="$(basename "$skill_dir")"

  zip_path="${WORK_DIR}/${skill_name}.zip"
    # Deterministic zip so identical content always hashes the same.
  skill_hash="$(python3 - "$skill_dir" "$zip_path" <<'PY'
import hashlib
import os
import sys
import zipfile
from pathlib import Path

skill_dir = Path(sys.argv[1]).resolve()
out_path = Path(sys.argv[2])

EXCLUDE_NAMES = {
    ".DS_Store", "Thumbs.db",
    ".git", ".gitignore", ".gitattributes",
    "__pycache__", ".pytest_cache",
    ".idea", ".vscode",
    "node_modules",
}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".swp", ".swo", ".tmp", ".log")


def is_excluded(p: Path) -> bool:
    name = p.name
    if name in EXCLUDE_NAMES:
        return True
    if name.endswith(EXCLUDE_SUFFIXES):
        return True
    return False


def has_excluded_ancestor(file_path: Path, root: Path) -> bool:
    cur = file_path.parent
    while True:
        if cur == root or cur == cur.parent:
            return False
        if is_excluded(cur):
            return True
        cur = cur.parent


# Sorted recursive walk → stable ordering of zip entries.
files = []
for p in sorted(skill_dir.rglob("*")):
    if not p.is_file():
        continue
    if is_excluded(p) or has_excluded_ancestor(p, skill_dir):
        continue
    files.append(p)

# Deterministic zip:
# - fixed timestamp (1980-01-01 — earliest zip-allowed)
# - fixed unix mode 0644
# - sorted entries
# Same content → same zip bytes → same sha256.
with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
    for f in files:
        arcname = str(f.relative_to(skill_dir))
        info = zipfile.ZipInfo(arcname)
        info.date_time = (1980, 1, 1, 0, 0, 0)
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        with open(f, "rb") as src:
            zf.writestr(info, src.read())

h = hashlib.sha256()
with open(out_path, "rb") as fh:
    for chunk in iter(lambda: fh.read(65536), b""):
        h.update(chunk)
print(h.hexdigest()[:12])
PY
)"

  echo "${skill_name}|${skill_hash}" >> "$ENTRIES_FILE"

  cached_hash="$(grep "^${skill_name}:" "$HASH_CACHE" 2>/dev/null | cut -d: -f2 || true)"
  if [[ "$cached_hash" == "$skill_hash" ]]; then
    skipped=$((skipped + 1))
    continue
  fi

  gcs_path="${GCS_PREFIX}/${skill_name}.zip"
  echo "========== Upload ${skill_name}.zip (${skill_hash}) to gs://${GCS_BUCKET}/${gcs_path} =========="
  gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
         -h "Content-Type:application/zip" \
         cp "$zip_path" "gs://${GCS_BUCKET}/${gcs_path}"

  grep -v "^${skill_name}:" "$HASH_CACHE" > "${HASH_CACHE}.tmp" 2>/dev/null || true
  echo "${skill_name}:${skill_hash}" >> "${HASH_CACHE}.tmp"
  mv "${HASH_CACHE}.tmp" "$HASH_CACHE"
  count=$((count + 1))
done

echo "Done: uploaded ${count} skill zip(s), skipped ${skipped} unchanged. gs://${GCS_BUCKET}/${GCS_PREFIX}/"

METADATA_GCS="gs://${GCS_BUCKET}/${BUCKET_PREFIX}/ota/metadata.json"
METADATA_TMP=$(mktemp)
PAYLOAD_TMP=$(mktemp)
trap 'rm -rf "$WORK_DIR" "$ENTRIES_FILE" "$METADATA_TMP" "$PAYLOAD_TMP"' EXIT
source "${RELEASE_DIR}/ota-metadata.sh"
if gsutil cp "$METADATA_GCS" "$METADATA_TMP" 2>/dev/null; then
  ota_metadata_unpack "$METADATA_TMP" "$PAYLOAD_TMP"
  python3 - "$PAYLOAD_TMP" "$ENTRIES_FILE" "$(date '+%Y-%m-%d %H:%M:%S %z')" <<'PY'
import json
import sys

metadata_path, entries_path, updated_at = sys.argv[1], sys.argv[2], sys.argv[3]
d = json.load(open(metadata_path))
skills = {}
for line in open(entries_path):
    line = line.strip()
    if not line or "|" not in line:
        continue
    name, version = line.split("|", 1)
    skills[name] = {"version": version, "updated_at": updated_at}
d["skills"] = skills
json.dump(d, open(metadata_path, "w"), indent=4)
PY
  ota_metadata_sign "$PAYLOAD_TMP" "$METADATA_TMP"
  gsutil -h "Cache-Control:no-cache, no-store, must-revalidate" \
         -h "Content-Type:application/json" \
         cp "$METADATA_TMP" "$METADATA_GCS"
  echo "Updated metadata.json with per-skill versions"
else
  echo "Warning: could not update metadata.json"
fi
