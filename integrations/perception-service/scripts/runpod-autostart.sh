#!/usr/bin/env bash
# Bring the perception-service master stack up after a RunPod container start.
#
# RunPod rents pods on shared hosts and recreates the container without notice
# (host maintenance, migration). The stock runpod/pytorch template only starts
# nginx, SSH and Jupyter, so dlserver and lbserver stay down until someone runs
# make by hand — the pod reports RUNNING and keeps billing while serving 502s.
#
# Invoked from /post_start.sh, which /start.sh runs on every container start.
# Safe to run repeatedly: it exits early if dlserver is already up.

set -uo pipefail

REPO_DIR=${AUTOSTART_REPO_DIR:-/workspace/autonomous-os/integrations/perception-service}
LOG_DIR=${AUTOSTART_LOG_DIR:-/workspace/logs/autostart}
LOCK=${AUTOSTART_LOCK:-/tmp/perception-autostart.lock}
HEALTH_URL=${AUTOSTART_HEALTH_URL:-http://127.0.0.1:8899/hal/api/dl/health}

mkdir -p "$LOG_DIR"
exec >>"$LOG_DIR/autostart.log" 2>&1

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*"; }

# Only one autostart at a time — a manual run must not race the boot hook.
exec 9>"$LOCK"
if ! flock -n 9; then
    log "another autostart is already running — exiting"
    exit 0
fi

log "=== autostart begin (pod=${RUNPOD_POD_ID:-unknown} container=$(hostname)) ==="

# /workspace is a MooseFS network volume and can lag behind container start,
# so the repo is not necessarily visible yet.
for i in $(seq 1 60); do
    [[ -f "$REPO_DIR/Makefile" ]] && break
    log "waiting for /workspace volume ($i/60)"
    sleep 5
done
if [[ ! -f "$REPO_DIR/Makefile" ]]; then
    log "FATAL: $REPO_DIR/Makefile never appeared after 5min — giving up"
    exit 1
fi

# The GPU device can take a moment to become usable after container start.
for i in $(seq 1 30); do
    nvidia-smi >/dev/null 2>&1 && break
    log "waiting for GPU ($i/30)"
    sleep 5
done
nvidia-smi >/dev/null 2>&1 || log "WARNING: no GPU visible — continuing (CPU fallback)"

# After a two-slot deploy the serving dlserver may be slot 8002; the Makefile owns
# the slot -> pid-file mapping, so ask it instead of assuming /tmp/dlserver.pid.
# fd 9 (the autostart lock) is closed for the make call so a detached daemon it
# might spawn can't inherit and hold it.
DLSERVER_PID=$(make -s --no-print-directory -C "$REPO_DIR" print-dlserver-pid 9>&- 2>/dev/null)
DLSERVER_PID=${DLSERVER_PID:-/tmp/dlserver.pid}

# Already serving? Nothing to do. This is the common case for a manual re-run.
if [[ -f "$DLSERVER_PID" ]] && kill -0 "$(cat "$DLSERVER_PID")" 2>/dev/null; then
    log "dlserver already running (pid=$(cat "$DLSERVER_PID")) — nothing to do"
    exit 0
fi

# config.py declares env_file=".env" as a RELATIVE path, so the stack must be
# started from the repo root. Started from anywhere else, every setting
# silently falls back to its default and the server comes up misconfigured.
cd "$REPO_DIR" || { log "FATAL: cannot cd to $REPO_DIR"; exit 1; }

log "starting master stack (nginx + dlserver + lbserver)"
# Close fd 9 (the autostart lock) for make: its recipes start detached daemons
# (nohup setsid ... &) that would otherwise inherit the fd and hold the lock
# forever, making every later autostart run see "already running" and exit.
make start-runpod-master 9>&-
log "make start-runpod-master exited with code $?"

# Verify rather than assume: make returning 0 only means the watchdogs were
# spawned, not that a model is loaded and serving. Model load takes minutes.
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
make info 9>&-
exit 1
