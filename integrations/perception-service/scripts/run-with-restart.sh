#!/usr/bin/env bash
# Run a command in a restart loop with structured logging.
# Usage: run-with-restart.sh [OPTIONS] -- COMMAND [ARGS...]
#
# --pid-file PATH:         the inner process PID is written here on each start.
# --wrapper-pid-file PATH: this wrapper's own PID is written here once at startup.
# --cooldown SECONDS:      wait between restarts (default: 5).
# --probe-url URL:         liveness endpoint (/livez; must not check models, auth or
#                          downstream services). After PROBE_FAILURES consecutive failures
#                          the child gets SIGUSR1 + STACK_DUMP_WAIT s to dump stacks, then -9.
# --log-dir PATH:          stdout/stderr/watchdog logs as plain files (a pipe can block
#                          the writer forever and wedge the asyncio server).
# SIGTERM to the wrapper stops the inner process and exits.

set -euo pipefail

COOLDOWN=5
PROBE_URL=""
PID_FILE=""
WRAPPER_PID_FILE=""
LOG_DIR=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --cooldown) COOLDOWN="$2"; shift 2 ;;
        --probe-url) PROBE_URL="$2"; shift 2 ;;
        --pid-file) PID_FILE="$2"; shift 2 ;;
        --wrapper-pid-file) WRAPPER_PID_FILE="$2"; shift 2 ;;
        --log-dir) LOG_DIR="$2"; shift 2 ;;
        --) shift; break ;;
        *) break ;;
    esac
done

if [[ $# -eq 0 ]]; then
    echo "Usage: $0 [--cooldown N] [--pid-file PATH] [--wrapper-pid-file PATH] [--log-dir PATH] -- COMMAND [ARGS...]" >&2
    exit 1
fi

[[ -n "$WRAPPER_PID_FILE" ]] && echo "$$" > "$WRAPPER_PID_FILE"

MAX_LOG_BYTES=${MAX_LOG_BYTES:-8388608}   # 8 MiB
LOG_BACKUPS=3
GUARD_INTERVAL=${GUARD_INTERVAL:-60}     # seconds between size checks

# Conservative on purpose: an eager probe turns a slow start into a restart loop.
PROBE_INTERVAL=${PROBE_INTERVAL:-10}     # seconds between probes
PROBE_TIMEOUT=${PROBE_TIMEOUT:-5}        # per-probe curl timeout
PROBE_FAILURES=${PROBE_FAILURES:-6}      # consecutive failures before acting (~60s)
PROBE_GRACE=${PROBE_GRACE:-180}          # seconds after start before probing at all;
                                         # dlserver needs ~2-3 min to load its models
STACK_DUMP_WAIT=${STACK_DUMP_WAIT:-2}    # seconds between SIGUSR1 (stack dump) and SIGKILL

# Rename aside on startup; safe because no process holds these files open yet.
rotate_on_start() {
    local f=$1 i
    [[ -f "$f" ]] || return 0
    rm -f "$f.$LOG_BACKUPS"
    for ((i = LOG_BACKUPS - 1; i >= 1; i--)); do
        [[ -f "$f.$i" ]] && mv -f "$f.$i" "$f.$((i + 1))"
    done
    mv -f "$f" "$f.1"
}

# Must copy-then-truncate, never rename: the child holds an O_APPEND fd and would
# keep writing to the renamed inode.
guard_size() {
    local f=$1 i
    [[ -f "$f" ]] || return 0
    local sz
    sz=$(wc -c <"$f" 2>/dev/null || echo 0)
    (( sz < MAX_LOG_BYTES )) && return 0
    rm -f "$f.$LOG_BACKUPS"
    for ((i = LOG_BACKUPS - 1; i >= 1; i--)); do
        [[ -f "$f.$i" ]] && mv -f "$f.$i" "$f.$((i + 1))"
    done
    cp -f "$f" "$f.1" && : >"$f"
}

if [[ -n "$LOG_DIR" ]]; then
    mkdir -p "$LOG_DIR"
    for _f in stdout stderr watchdog; do
        rotate_on_start "$LOG_DIR/$_f.log"
    done
    exec >>"$LOG_DIR/watchdog.log" 2>&1
fi

CHILD_PID=""
GUARD_PID=""
PROBE_PID=""
RUNNING=true

start_size_guard() {
    [[ -n "$LOG_DIR" ]] || return 0
    (
        while :; do
            sleep "$GUARD_INTERVAL"
            guard_size "$LOG_DIR/stdout.log"
            guard_size "$LOG_DIR/stderr.log"
            guard_size "$LOG_DIR/watchdog.log"
        done
    ) &
    GUARD_PID=$!
}

# `wait` never fires for a frozen child. kill -9 is mandatory: a hung uvicorn
# absorbs SIGTERM because only the stuck event loop acts on it.
start_liveness_probe() {
    [[ -n "$PROBE_URL" ]] || return 0
    local target=$1
    (
        sleep "$PROBE_GRACE"
        local fails=0
        while :; do
            if curl -fsS -m "$PROBE_TIMEOUT" -o /dev/null "$PROBE_URL" 2>/dev/null; then
                if (( fails > 0 )); then
                    echo "[watchdog] probe recovered after $fails failure(s)"
                fi
                fails=0
            else
                fails=$(( fails + 1 ))
                echo "[watchdog] probe failed ($fails/$PROBE_FAILURES): $PROBE_URL"
                if (( fails >= PROBE_FAILURES )); then
                    echo "[watchdog] unresponsive after $fails probes; SIGUSR1 $target for a stack dump"
                    # A child with no SIGUSR1 handler just dies here, which is
                    # what the SIGKILL below was about to do anyway.
                    kill -USR1 "$target" 2>/dev/null || true
                    sleep "$STACK_DUMP_WAIT"
                    echo "[watchdog] SIGKILL $target"
                    kill -9 "$target" 2>/dev/null || true
                    return 0
                fi
            fi
            sleep "$PROBE_INTERVAL"
        done
    ) &
    PROBE_PID=$!
}

stop_liveness_probe() {
    [[ -n "$PROBE_PID" ]] && kill "$PROBE_PID" 2>/dev/null || true
    PROBE_PID=""
}

stop_size_guard() {
    [[ -n "$GUARD_PID" ]] && kill "$GUARD_PID" 2>/dev/null || true
    GUARD_PID=""
}

cleanup() {
    RUNNING=false
    stop_liveness_probe
    stop_size_guard
    if [[ -n "$CHILD_PID" ]] && kill -0 "$CHILD_PID" 2>/dev/null; then
        kill "$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
    fi
}

trap cleanup SIGTERM SIGINT

while $RUNNING; do
    if [[ -n "$LOG_DIR" ]]; then
        "$@" \
            >>"$LOG_DIR/stdout.log" \
            2>>"$LOG_DIR/stderr.log" &
    else
        "$@" &
    fi
    CHILD_PID=$!
    [[ -n "$PID_FILE" ]] && echo "$CHILD_PID" > "$PID_FILE"
    start_size_guard
    start_liveness_probe "$CHILD_PID"
    EXIT_CODE=0
    wait "$CHILD_PID" || EXIT_CODE=$?
    CHILD_PID=""
    stop_liveness_probe
    stop_size_guard

    if ! $RUNNING; then
        break
    fi

    echo "[watchdog] Process exited (code=$EXIT_CODE), restarting in ${COOLDOWN}s..."
    sleep "$COOLDOWN" &
    wait $! 2>/dev/null || true
done
