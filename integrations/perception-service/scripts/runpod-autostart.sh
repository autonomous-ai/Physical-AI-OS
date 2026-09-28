#!/usr/bin/env bash
# Bring the perception-service master stack up after a RunPod container start.
# Invoked from /post_start.sh on every container start; idempotent.

set -uo pipefail

REPO_DIR=/workspace/autonomous-os/integrations/perception-service
LOG_DIR=/workspace/logs/autostart
LOCK=/tmp/perception-autostart.lock
DLSERVER_PID=/tmp/dlserver.pid
HEALTH_URL=http://127.0.0.1:8899/hal/api/dl/health

mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/autostart.log" 2>&1

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

exec 9>"$LOCK"
if ! flock -n 9; then
    log "another autostart is already running — exiting"
    exit 0
fi

log "=== autostart begin (pod=${RUNPOD_POD_ID:-unknown} container=$(hostname)) ==="

# /workspace is a MooseFS network volume and can lag behind container start.
for i in $(seq 1 60); do
    [[ -f "$REPO_DIR/Makefile" ]] && break
    log "waiting for /workspace volume ($i/60)"
    sleep 5
done
if [[ ! -f "$REPO_DIR/Makefile" ]]; then
    log "FATAL: $REPO_DIR/Makefile never appeared after 5min — giving up"
    exit 1
fi

for i in $(seq 1 30); do
    nvidia-smi >/dev/null 2>&1 && break
    log "waiting for GPU ($i/30)"
    sleep 5
done
nvidia-smi >/dev/null 2>&1 || log "WARNING: no GPU visible — continuing (CPU fallback)"

if [[ -f "$DLSERVER_PID" ]] && kill -0 "$(cat "$DLSERVER_PID")" 2>/dev/null; then
    log "dlserver already running (pid=$(cat "$DLSERVER_PID")) — nothing to do"
    exit 0
fi

# config.py reads a relative .env, so the stack must start from the repo root.
cd "$REPO_DIR" || { log "FATAL: cannot cd to $REPO_DIR"; exit 1; }

log "starting master stack (nginx + dlserver + lbserver)"
make start-runpod-master
log "make start-runpod-master exited with code $?"

# make returning 0 only means watchdogs were spawned; model load takes minutes.
API_KEY=$(grep -E '^DL_API_KEY=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d "\"'" | tr -d '[:space:]')
for i in $(seq 1 60); do
    code=$(curl -s -o /dev/null -w '%{http_code}' -m 10 \
        -H "X-API-Key: ${API_KEY}" "$HEALTH_URL" 2>/dev/null)
    if [[ "$code" == "200" ]]; then
        log "OK: stack healthy after $((i * 10))s"
        exit 0
    fi
    sleep 10
done

log "ERROR: stack did not become healthy within 10min (last HTTP code: ${code:-none})"
make info
exit 1
