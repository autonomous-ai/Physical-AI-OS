#!/usr/bin/env bash
# Shared GCS config sourced by the release scripts; env overrides win (`:=`).

RELEASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${RELEASE_DIR}/../.." && pwd)"

: "${GCS_BUCKET:=s3-autonomous-upgrade-3}"

: "${BUCKET_PREFIX:=os}"
