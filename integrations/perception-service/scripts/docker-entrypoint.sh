#!/usr/bin/env bash
# Entrypoint for the perception-service Docker container.
#
# ROLE=master (default): nginx :8899 → lbserver :7999 → dlserver :8001
# ROLE=slave:            nginx :8899 → dlserver :7999 (no LB)
set -euo pipefail

ROLE="${ROLE:-master}"
HOST="${HOST:-127.0.0.1}"

mkdir -p /var/log/nginx /workspace/logs/dlserver /workspace/logs/lbserver "${MODEL_CACHE_DIR:-/workspace/models}"

cp /app/nginx.conf /etc/nginx/perception-service-nginx.conf
nginx -c /etc/nginx/perception-service-nginx.conf

if [ "$ROLE" = "master" ]; then
    echo "[entrypoint] Starting as MASTER (nginx:8899 → lbserver:7999 → dlserver:8001)"

    python -m lbserver --host "$HOST" --port 7999 \
        --log-dir /workspace/logs/lbserver &

    exec python -m dlserver --host "$HOST" --port 8001 \
        --log-dir /workspace/logs/dlserver

elif [ "$ROLE" = "slave" ]; then
    echo "[entrypoint] Starting as SLAVE (nginx:8899 → dlserver:7999, no LB)"

    exec python -m dlserver --host "$HOST" --port 7999 \
        --log-dir /workspace/logs/dlserver

else
    echo "[entrypoint] Unknown ROLE=$ROLE (expected: master, slave)"
    exit 1
fi
