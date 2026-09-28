#!/usr/bin/env bash
# Zero-downtime dlserver deploy on one GPU: two slots, switch when the new one is ready.
#
#   git pull && make deploy-dlserver
#
# Starts the new code on the idle slot, waits until it serves every model the
# active slot serves, points lbserver at it (state file + SIGHUP, confirmed by the
# ack lbserver writes), drains, then stops the old slot. Every refusal or failure
# before the switch leaves traffic where it was.
#
# Not for dependency changes: if pyproject.toml changed since the active slot
# started, pip would rewrite the venv the old slot serves from, and a TensorRT /
# ONNX Runtime bump rebuilds engines that may not fit next to two loaded copies.
# Use the in-place restart (make start-runpod-master) for those.
#
# Run it under nohup or tmux: an SSH drop sends SIGHUP, which aborts the deploy.
# Env knobs: DEPLOY_READY_TIMEOUT (900 s), DEPLOY_DRAIN_SECONDS (35 s, must exceed
# lb.http_timeout 30 s), DEPLOY_MIN_FREE_GPU_MB (12000; one loaded copy is ~9.7 GB).
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

RUN_DIR=${RUN_DIR:-/tmp}
LOG_ROOT=${LOG_ROOT:-/workspace/logs}
DLSERVER_STATE_FILE=${DLSERVER_STATE_FILE:-$RUN_DIR/dlserver-active}
LBSERVER_PORT=${LBSERVER_PORT:-7999}
DLSERVER_SLOT_A=${DLSERVER_SLOT_A:-8001}
DLSERVER_SLOT_B=${DLSERVER_SLOT_B:-8002}
export RUN_DIR LOG_ROOT DLSERVER_STATE_FILE LBSERVER_PORT DLSERVER_SLOT_A DLSERVER_SLOT_B
STATE=$DLSERVER_STATE_FILE
READY_TIMEOUT=${DEPLOY_READY_TIMEOUT:-900}
DRAIN=${DEPLOY_DRAIN_SECONDS:-35}
MIN_FREE_MB=${DEPLOY_MIN_FREE_GPU_MB:-12000}
LOG="$LOG_ROOT/deploy/deploy.log"
mkdir -p "$(dirname "$LOG")" "$RUN_DIR"

log() { echo "[deploy $(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" | tee -a "$LOG"; }
abort() { log "ABORT: $* -- traffic unchanged"; exit 1; }
gpu() { nvidia-smi --query-gpu=memory.used,memory.free,memory.total --format=csv,noheader,nounits 2>/dev/null | head -1; }
listener() { ss -lntpH "sport = :$1" 2>/dev/null | grep -oE 'pid=[0-9]+' | head -1 | cut -d= -f2; }
slot_of() { sed -n 's|^http://127.0.0.1:\([0-9][0-9]*\)$|\1|p' "$1" 2>/dev/null | grep .; }
rev_file() { if [ "$1" = "$DLSERVER_SLOT_A" ]; then echo "$RUN_DIR/dlserver.rev"; else echo "$RUN_DIR/dlserver-$1.rev"; fi; }
# Close fd 9 (the deploy lock) for make and everything it execs: the dlserver
# start target detaches a long-lived wrapper process (nohup setsid ... &), and an
# inherited fd 9 would keep that process holding the lock forever after this
# script exits, permanently blocking every later deploy.
mk() { make --no-print-directory "$@" 9>&- >>"$LOG" 2>&1; }
KEY=${DL_API_KEY:-$(grep -E '^DL_API_KEY=' .env 2>/dev/null | head -1 | cut -d= -f2- | tr -d "\"'" | tr -d '[:space:]')}
health() { curl -sf -m 10 -H "X-API-Key: $KEY" "http://127.0.0.1:$1/hal/api/dl/health"; }
# Models ready on the active slot but not on the new one, space-separated.
missing_models() {
    python - "$1" "$2" <<'EOF'
import json, sys
def ready(raw):
    m = json.loads(raw)["models"]
    out = {k for k, v in m.items() if v is True}
    out |= {f"object_detectors.{k}" for k, v in (m.get("object_detectors") or {}).items() if v is True}
    return out
print(" ".join(sorted(ready(sys.argv[1]) - ready(sys.argv[2]))))
EOF
}
# lbserver can be restarted by its watchdog at any moment, so always ask who
# listens on the LB port now instead of reusing a pid read earlier.
lb_pid() { listener "$LBSERVER_PORT"; }
acked() { local p; p=$(lb_pid); [ -n "$p" ] && grep -q "\"pid\": $p," "$STATE.applied" 2>/dev/null; }
acked_on() { acked && grep -q "127.0.0.1:$1\"" "$STATE.applied"; }

exec 9>"$RUN_DIR/dlserver-deploy.lock"
flock -n 9 || abort "another deploy is running"

active=$(slot_of "$STATE"); active=${active:-$DLSERVER_SLOT_A}
if [ "$active" = "$DLSERVER_SLOT_A" ]; then idle=$DLSERVER_SLOT_B; else idle=$DLSERVER_SLOT_A; fi
log "=== deploy $(git rev-parse --short HEAD 2>/dev/null): active=$active idle=$idle gpu(used,free,total MB)=$(gpu)"

# --- preflight: refuse before touching anything --------------------------------
[ -n "$(lb_pid)" ] || abort "nothing listens on :$LBSERVER_PORT (lbserver down)"
acked || abort "lbserver (pid $(lb_pid)) has not acked $STATE: it predates the switch or runs without LB__STATE_FILE; restart it with make start-runpod-lbserver"
[ -n "$(listener "$active")" ] || abort "active slot $active is not running"
[ -z "$(listener "$idle")" ] || abort "idle port $idle is in use; stop it first: make stop-runpod-dlserver DLSERVER_PORT=$idle"
rev=$(cat "$(rev_file "$active")" 2>/dev/null)
[ -n "$rev" ] || abort "no $(rev_file "$active"): unknown commit on slot $active; do one in-place restart (make start-runpod-master) first"
git diff --quiet "$rev" HEAD -- pyproject.toml 2>/dev/null \
    || abort "pyproject.toml changed since slot $active started ($rev): use the in-place restart (make start-runpod-master)"
free=$(nvidia-smi --query-gpu=memory.free --format=csv,noheader,nounits 2>/dev/null | head -1 | tr -d ' ')
if [ -z "$free" ]; then
    log "WARNING: nvidia-smi unavailable, skipping the GPU memory check"
elif [ "$free" -lt "$MIN_FREE_MB" ]; then
    abort "only ${free} MB GPU free, a second copy needs $MIN_FREE_MB"
fi
want=$(health "$active") || abort "cannot read /hal/api/dl/health on active slot $active"

# --- start the new slot ------------------------------------------------------------
switched=0
# 1 from just before the state file first names $idle until the ack confirms it
# (or a rollback finishes): the window where a signal must undo the switch, not
# just stop $idle -- lbserver may already be sending it traffic.
switching=0
fail() { log "ABORT: $* -- stopping slot $idle, traffic unchanged"; mk stop-runpod-dlserver DLSERVER_PORT="$idle" || log "WARNING: slot $idle did not stop cleanly"; exit 1; }

# Undo a switch to $idle: point the state file back at $active, SIGHUP the current
# lbserver, and wait for it to ack $active (same protocol as the forward switch).
# Confirmed -> traffic is really back on $active, so stopping $idle (via fail) is
# safe. NOT confirmed -> lbserver may still be sending traffic to $idle, so $idle
# must stay up; report the exact state for a human instead of guessing.
rollback_switch() {
    trap '' INT TERM HUP
    local reason=$1 wrote=1 i
    printf 'http://127.0.0.1:%s\n' "$active" >"$STATE.tmp" && mv "$STATE.tmp" "$STATE" && wrote=0
    kill -HUP "$(lb_pid)" 2>/dev/null
    for i in $(seq 1 20); do acked_on "$active" && break; sleep 0.25; done
    if [ "$wrote" = 0 ] && acked_on "$active"; then
        fail "$reason; state file restored to $active"
    fi
    log "ABORT: $reason -- rollback NOT confirmed: state file names $(cat "$STATE" 2>/dev/null || echo unreadable), lbserver ack $(cat "$STATE.applied" 2>/dev/null || echo none). lbserver may still be sending traffic to $idle: NOT stopping it. Check by hand which slot ($active or $idle) actually answers, fix the state file if needed (printf 'http://127.0.0.1:<port>\n' >$STATE && kill -HUP <lbserver pid>), then stop the other slot with: make stop-runpod-dlserver DLSERVER_PORT=<port>"
    exit 1
}

on_signal() {
    trap '' INT TERM HUP
    if [ "$switched" = 1 ]; then
        log "interrupted after the switch: traffic is on $idle; stop the old slot with: make stop-runpod-dlserver DLSERVER_PORT=$active"
        exit 1
    fi
    if [ "$switching" = 1 ]; then
        rollback_switch "interrupted during the switch"
    fi
    fail "interrupted"
}
trap on_signal INT TERM HUP

mk start-runpod-dlserver-slot DLSERVER_PORT="$idle" || fail "make start-runpod-dlserver-slot failed"
log "slot $idle starting; waiting up to ${READY_TIMEOUT}s for its models"
deadline=$((SECONDS + READY_TIMEOUT))
while :; do
    # dlserver binds its port only after the lifespan loaded every model, so the
    # first answer is final: a model missing now failed to load and will not appear.
    if got=$(health "$idle"); then
        missing=$(missing_models "$want" "$got") || fail "cannot parse health of slot $idle"
        [ -z "$missing" ] || fail "slot $idle is up but missing models: $missing"
        break
    fi
    [ "$SECONDS" -lt "$deadline" ] || fail "slot $idle not ready after ${READY_TIMEOUT}s"
    sleep 5
done
log "slot $idle ready gpu=$(gpu)"

# --- switch --------------------------------------------------------------------------
switching=1
printf 'http://127.0.0.1:%s\n' "$idle" >"$STATE.tmp" && mv "$STATE.tmp" "$STATE" || fail "cannot write $STATE"
kill -HUP "$(lb_pid)" 2>/dev/null
for _ in $(seq 1 20); do acked_on "$idle" && break; sleep 0.25; done
if ! acked_on "$idle"; then
    rollback_switch "lbserver did not confirm the switch"
fi
switched=1
log "switched lbserver -> $idle; draining ${DRAIN}s"
sleep "$DRAIN"

# --- stop the old slot -------------------------------------------------------------
if ! mk stop-runpod-dlserver DLSERVER_PORT="$active"; then
    log "ERROR: old slot $active did not stop cleanly. Traffic is on $idle. Stop it by hand: make stop-runpod-dlserver DLSERVER_PORT=$active"
    exit 1
fi
trap - INT TERM HUP
log "done: serving from $idle gpu=$(gpu)"
