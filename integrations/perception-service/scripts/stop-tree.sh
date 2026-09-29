#!/usr/bin/env bash
# Stop a perception-service process tree and verify it stopped (exits non-zero if not).
# Usage: stop-tree.sh NAME PORT WRAPPER_PID_FILE PID_FILE [LOG_DIR]
# Escalates TERM -> KILL: a hung uvicorn ignores SIGTERM. The listening port is
# treated as authoritative; pid files may hold stale PIDs from a failed start.
set -uo pipefail

NAME=${1:?usage: stop-tree.sh NAME PORT WRAPPER_PID_FILE PID_FILE}
PORT=${2:?}
WPID_FILE=${3:?}
PID_FILE=${4:?}
# LOG_DIR identifies ONE instance. Two dlserver slots run side by side with
# different log dirs (two-slot deploy); matching the name alone stopped both.
# Anchored with ( |$) so /workspace/logs/dlserver does not match dlserver-8002.
LOG_DIR=${5:-/workspace/logs/$NAME}

# A PID holding PORT must still look like this service: on a slave node dlserver
# binds LBSERVER_PORT, so stopping lbserver would otherwise kill dlserver.
owns_port_but_wrong_service() {
    local pid=$1 args
    args=$(ps -o args= -p "$pid" 2>/dev/null) || return 1
    case "$args" in *"$NAME"*) return 1 ;; esac
    echo "[$NAME] pid $pid holds port $PORT but is not $NAME; leaving it alone:" \
         "${args:0:80}" >&2
    return 0
}

# A pid file can outlive the process it named: a stale pid from a crashed
# instance, or -- inside this very script -- a pid that round 1's SIGTERM
# already reaped, since both files are only removed after the escalation
# loop below. `kill -0` counts a zombie as alive, so a bare existence check
# would keep re-adding that pid to `collect`'s output and hold `alive` true
# for the full TERM/TERM/KILL sequence even after the real process is gone
# -- and worst case re-signal a pid the kernel has since recycled. Check the
# process state instead: no entry, or a `Z` stat, means "already gone" to
# us. `ps -o stat=` works the same on BSD and procps `ps`.
pid_from_file() {
    local f=$1 p
    [[ -r "$f" ]] || return 0
    p=$(cat "$f" 2>/dev/null) || return 0
    [[ "$p" =~ ^[0-9]+$ ]] || return 0
    case "$(ps -o stat= -p "$p" 2>/dev/null)" in
        ''|Z*) return 0 ;;
    esac
    echo "$p"
}

collect() {
    {
        for _p in $(ss -lntpH "sport = :$PORT" 2>/dev/null \
                    | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -un); do
            owns_port_but_wrong_service "$_p" || echo "$_p"
        done
        pid_from_file "$WPID_FILE"
        pid_from_file "$PID_FILE"
        pgrep -f "run-with-restart.sh .*--log-dir $LOG_DIR( |\$)" 2>/dev/null
        pgrep -f "python -m $NAME .*--log-dir $LOG_DIR( |\$)" 2>/dev/null
    } 2>/dev/null | grep -E '^[0-9]+$' | sort -un
}

# Expand PIDs to process groups, never our own (killing it would take down make
# and this script mid-stop). Such PIDs are still killed individually.
expand_groups() {
    local pid pgid self
    self=$(ps -o pgid= -p $$ 2>/dev/null | tr -d ' ')
    for pid in "$@"; do
        pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
        [[ -z "$pgid" ]] && continue
        if [[ "$pgid" == "$self" ]]; then
            echo "[$NAME] pid $pid shares our process group ($pgid); killing it" \
                 "individually instead of by group" >&2
            continue
        fi
        (( pgid > 1 )) && echo "$pgid"
    done | sort -un
}

# Test collect's output, never its exit status: with pipefail, pgrep can make it
# exit 1 while live PIDs were printed.
alive() { [[ -n "$(collect | tr -d '[:space:]')" ]]; }

# No mapfile/readarray: must not assume bash >= 4.
for sig in TERM TERM KILL; do
    pids=$(collect | tr '\n' ' ')
    [[ -z "${pids// /}" ]] && break
    # shellcheck disable=SC2086
    pgids=$(expand_groups $pids | tr '\n' ' ')

    echo "[$NAME] SIG$sig -> pids: ${pids:-none} groups: ${pgids:-none}"
    for pgid in $pgids; do kill -"$sig" -- "-$pgid" 2>/dev/null || true; done
    for pid  in $pids;  do kill -"$sig" "$pid"       2>/dev/null || true; done

    for _ in $(seq 20); do          # up to 10s per round
        sleep 0.5
        alive || break 2
    done
done

rm -f "$WPID_FILE" "$PID_FILE"

leftover=$(collect | tr '\n' ' ')
port_held=$(ss -lntH "sport = :$PORT" 2>/dev/null | grep -c . || true)

if [[ -n "${leftover// /}" ]]; then
    echo "[$NAME] FAILED to stop cleanly -- our processes survived." >&2
    echo "[$NAME]   survivors: $leftover" >&2
    ss -lntp "sport = :$PORT" >&2 2>/dev/null || true
    exit 1
fi
if (( port_held > 0 )); then
    echo "[$NAME] stopped, but port $PORT is still held by another process." >&2
    echo "[$NAME]   not killed: it is not $NAME. Starting $NAME will fail." >&2
    ss -lntp "sport = :$PORT" >&2 2>/dev/null || true
    exit 1
fi

echo "[$NAME] stopped, port $PORT released"
