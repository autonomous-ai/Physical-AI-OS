#!/usr/bin/env bash
# Hardware acceptance test (RAM, storage, thermal) run on the device before it ships.
# Usage: sudo ./burn-in.sh [--quick] [--ram MB] [--passes N]   Exit: 0 PASS, 1 FAIL, 2 could not run.

set -uo pipefail

VERSION="1.0"
RESULT_DIR="/var/lib/autonomous"
LOG="${RESULT_DIR}/burn-in.log"
JSON="${RESULT_DIR}/burn-in.json"

# Cap mismatch lines: a bad DIMM emits millions and can fill a RAM-backed /var/log.
MAX_FAIL_LINES=20

QUICK=0
RAM_MB=""
PASSES=""
SOAK_S=""
SKIP_STORAGE=0
SKIP_THERMAL=0
FAIL_FAST=1

if [ -t 1 ]; then
  C_RST=$'\033[0m'; C_DIM=$'\033[90m'; C_RED=$'\033[31m'
  C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_BLD=$'\033[1m'
else
  C_RST=""; C_DIM=""; C_RED=""; C_GRN=""; C_YEL=""; C_BLD=""
fi

ts()   { date '+%H:%M:%S'; }
say()  { printf '%s[%s]%s %s\n' "$C_DIM" "$(ts)" "$C_RST" "$*" | tee -a "$LOG"; }
head2(){ printf '\n%s══ %s %s\n' "$C_BLD" "$*" "$C_RST" | tee -a "$LOG"; }
ok()   { printf '%s[%s]%s   %sPASS%s  %s\n' "$C_DIM" "$(ts)" "$C_RST" "$C_GRN" "$C_RST" "$*" | tee -a "$LOG"; }
bad()  { printf '%s[%s]%s   %sFAIL%s  %s\n' "$C_DIM" "$(ts)" "$C_RST" "$C_RED" "$C_RST" "$*" | tee -a "$LOG"; }
warn() { printf '%s[%s]%s   %sWARN%s  %s\n' "$C_DIM" "$(ts)" "$C_RST" "$C_YEL" "$C_RST" "$*" | tee -a "$LOG"; }
fmt_status() {
  case "$1" in
    PASS) printf '%sPASS%s' "$C_GRN" "$C_RST" ;;
    FAIL) printf '%sFAIL%s' "$C_RED" "$C_RST" ;;
    *)    printf '%sSKIP%s' "$C_YEL" "$C_RST" ;;
  esac
}
die()  { printf '%sburn-in: %s%s\n' "$C_RED" "$*" "$C_RST" >&2; exit 2; }

usage() {
  sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --quick)        QUICK=1 ;;
    --ram)          RAM_MB="${2:-}"; shift ;;
    --passes)       PASSES="${2:-}"; shift ;;
    --soak)         SOAK_S="${2:-}"; shift ;;
    --skip-storage) SKIP_STORAGE=1 ;;
    --skip-thermal) SKIP_THERMAL=1 ;;
    --no-fail-fast) FAIL_FAST=0 ;;
    -h|--help)      usage ;;
    *)              die "unknown option: $1 (try --help)" ;;
  esac
  shift
done

[ "$(id -u)" -eq 0 ] || die "must run as root (memtester needs to lock memory)"
command -v memtester >/dev/null 2>&1 || die "memtester not installed"

mkdir -p "$RESULT_DIR" || die "cannot create $RESULT_DIR"
: > "$LOG"

# Leave ~1 GB free: an OOM kill mid-test would read as a hardware fault.
AVAIL_MB=$(awk '/MemAvailable/ {printf "%d", $2/1024}' /proc/meminfo)
if [ -z "$RAM_MB" ]; then
  if [ "$QUICK" -eq 1 ]; then
    RAM_MB=512
  else
    RAM_MB=$(( AVAIL_MB - 1024 ))
    [ "$RAM_MB" -lt 512 ] && RAM_MB=512
  fi
fi
[ -z "$PASSES" ] && { [ "$QUICK" -eq 1 ] && PASSES=1 || PASSES=1; }
[ -z "$SOAK_S" ] && { [ "$QUICK" -eq 1 ] && SOAK_S=30 || SOAK_S=120; }

DEVICE_ID=$(python3 -c "import json;print(json.load(open('/root/config/config.json')).get('device_id',''))" 2>/dev/null || true)
DEVICE_TYPE=$(grep -ho 'DEVICE_TYPE=[^ "]*' /etc/systemd/system/os-server.service /opt/hal/.env 2>/dev/null | head -1 | cut -d= -f2)
ROOT_DEV=$(findmnt -no SOURCE / 2>/dev/null)

head2 "AUTONOMOUS BURN-IN v${VERSION}"
say "host        $(hostname)"
say "device      ${DEVICE_TYPE:-unknown}  id=${DEVICE_ID:-unknown}"
say "kernel      $(uname -r)  $(uname -m)"
say "started     $(date '+%Y-%m-%d %H:%M:%S %Z')"
say "plan        RAM ${RAM_MB} MB x${PASSES} pass · soak ${SOAK_S}s · root ${ROOT_DEV}"
[ "$QUICK" -eq 1 ] && warn "quick mode — a screen, not an acceptance test"

FAIL_RAM=0; FAIL_STORAGE=0; FAIL_THERMAL=0
RAM_ERRORS=0; MAX_TEMP=0
STATUS_STORAGE="PASS"; STATUS_THERMAL="PASS"

# Persist the verdict early so an unstable board still leaves a record.
write_result() {
  local verdict
  verdict=$([ $((FAIL_RAM + FAIL_STORAGE + FAIL_THERMAL)) -eq 0 ] && echo PASS || echo FAIL)
  cat > "$JSON" <<EOF
{
  "version": "${VERSION}",
  "verdict": "${verdict}",
  "quick": $([ "$QUICK" -eq 1 ] && echo true || echo false),
  "finished_at": "$(date -Is)",
  "device_id": "${DEVICE_ID}",
  "device_type": "${DEVICE_TYPE}",
  "kernel": "$(uname -r)",
  "memory": { "status": "$([ $FAIL_RAM -eq 0 ] && echo PASS || echo FAIL)", "tested_mb": ${RAM_MB}, "passes": ${PASSES}, "mismatches": ${RAM_ERRORS} },
  "storage": { "status": "${STATUS_STORAGE}" },
  "thermal": { "status": "${STATUS_THERMAL}", "peak_c": ${MAX_TEMP}, "peak_skin_c": ${MAX_SKIN:-0} }
}
EOF
  sync 2>/dev/null || true
}

# 1. Memory
head2 "1/3  MEMORY"
say "memtester ${RAM_MB}M ${PASSES} — each line below appears as that subtest finishes"
say "this is the slow one; expect roughly 8 min per GB per pass"

FAILCOUNT_FILE=$(mktemp)
echo 0 > "$FAILCOUNT_FILE"

( while true; do sleep 60; printf '%s[%s]%s   %s…still running%s\n' \
    "$C_DIM" "$(ts)" "$C_RST" "$C_DIM" "$C_RST"; done ) &
HEARTBEAT=$!
trap 'kill "$HEARTBEAT" 2>/dev/null; rm -f "$FAILCOUNT_FILE"' EXIT INT TERM

set -o pipefail
# Collapse memtester's backspace-drawn spinners/counters to one line per subtest; FAILURE lines untouched.
stdbuf -oL memtester "${RAM_MB}M" "$PASSES" 2>&1 \
  | stdbuf -oL tr -d '\010' \
  | stdbuf -oL sed -E 's@[\|/\\-]{3,}@@g; s/( *(sett|test)ing +[0-9]+)+/ /g; s/ {2,}/ /g; s/ +$//' \
  | stdbuf -oL awk -v max="$MAX_FAIL_LINES" -v cf="$FAILCOUNT_FILE" '
      /FAILURE/ {
        n++
        if (n <= max) { print "      " $0; fflush() }
        else if (n == max + 1) { print "      … further mismatches suppressed (still counting)"; fflush() }
        next
      }
      /^[[:space:]]*$/ { next }
      { print; fflush() }
      END { print n+0 > cf }
    ' | tee -a "$LOG"
MEMTESTER_RC=${PIPESTATUS[0]}
kill "$HEARTBEAT" 2>/dev/null
RAM_ERRORS=$(cat "$FAILCOUNT_FILE" 2>/dev/null || echo 0)
[ -z "$RAM_ERRORS" ] && RAM_ERRORS=0

if [ "$MEMTESTER_RC" -ne 0 ] || [ "$RAM_ERRORS" -gt 0 ]; then
  FAIL_RAM=1
  bad "memory: ${RAM_ERRORS} mismatches (memtester exit ${MEMTESTER_RC})"
  bad "this board has bad DRAM — it cannot be fixed in software. Reject the unit."
else
  ok "memory: ${RAM_MB} MB x${PASSES} clean"
fi

write_result

# Stop on bad memory; later stages often crash the board. --no-fail-fast runs the full sweep.
if [ "$FAIL_RAM" -eq 1 ] && [ "$FAIL_FAST" -eq 1 ]; then
  warn "skipping storage and thermal — verdict already decided by memory"
  SKIP_STORAGE=1
  SKIP_THERMAL=1
fi

# 2. Storage
head2 "2/3  STORAGE"
if [ "$SKIP_STORAGE" -eq 1 ]; then
  warn "not run"; STATUS_STORAGE="SKIP"
else
  FS_STATE=$(dumpe2fs -h "$ROOT_DEV" 2>/dev/null | awk -F': *' '/Filesystem state/{print $2}')
  LIFETIME=$(dumpe2fs -h "$ROOT_DEV" 2>/dev/null | awk -F': *' '/Lifetime writes/{print $2}')
  say "filesystem  state=${FS_STATE:-unknown}  lifetime writes=${LIFETIME:-unknown}"

  case "$FS_STATE" in
    clean) ok "filesystem clean" ;;
    "")    warn "could not read filesystem state" ;;
    *)     STATUS_STORAGE="FAIL"; FAIL_STORAGE=1; bad "filesystem state '${FS_STATE}' — run fsck before shipping" ;;
  esac

  # Drop caches so the read-back hits the card, not RAM.
  SCRATCH="${RESULT_DIR}/.burnin-scratch"
  say "write/verify 256 MB to ${ROOT_DEV} (cache dropped between)"
  if dd if=/dev/urandom of="$SCRATCH" bs=1M count=256 conv=fsync status=none 2>/dev/null; then
    W_SUM=$(md5sum "$SCRATCH" | cut -d' ' -f1)
    sync; echo 3 > /proc/sys/vm/drop_caches 2>/dev/null || true
    R_SUM=$(md5sum "$SCRATCH" | cut -d' ' -f1)
    rm -f "$SCRATCH"
    if [ "$W_SUM" = "$R_SUM" ]; then
      ok "storage read-back matches (${W_SUM:0:12}…)"
    else
      FAIL_STORAGE=1
      bad "storage read-back MISMATCH — card is corrupting data"
    fi
  else
    STATUS_STORAGE="FAIL"; FAIL_STORAGE=1; bad "could not write scratch file — disk full or read-only?"
  fi

  DISK_PCT=$(df --output=pcent / | tail -1 | tr -dc '0-9')
  [ "${DISK_PCT:-0}" -gt 90 ] && { warn "root filesystem ${DISK_PCT}% full"; } || say "root usage ${DISK_PCT}%"
fi

# 3. Thermal + load
head2 "3/3  THERMAL SOAK"
if [ "$SKIP_THERMAL" -eq 1 ]; then
  warn "not run"; STATUS_THERMAL="SKIP"
else
  # Use each zone's own passive/critical trip points, not a hardcoded limit.
  zone_trip() {  # zone_dir, trip type → °C, empty if the zone declares none
    local d="$1" want="$2" t ty best=""
    for t in "$d"/trip_point_*_temp; do
      [ -f "$t" ] || continue
      ty=$(cat "${t%_temp}_type" 2>/dev/null)
      [ "$ty" = "$want" ] || continue
      v=$(awk '{printf "%d", $1/1000}' "$t")
      [ -z "$best" ] || [ "$v" -gt "$best" ] && best="$v"
    done
    printf '%s' "$best"
  }
  zone_by_type() {  # substring of the zone's type name → zone dir
    local z
    for z in /sys/class/thermal/thermal_zone*/; do
      case "$(cat "$z/type" 2>/dev/null)" in *"$1"*) printf '%s' "$z"; return ;; esac
    done
  }

  CPU_ZONE=$(zone_by_type cpub_thermal); [ -z "$CPU_ZONE" ] && CPU_ZONE=$(zone_by_type cpu)
  [ -z "$CPU_ZONE" ] && CPU_ZONE=$(ls -d /sys/class/thermal/thermal_zone0/ 2>/dev/null)
  SKIN_ZONE=$(zone_by_type skin)

  if [ -z "$CPU_ZONE" ] || [ ! -f "${CPU_ZONE}temp" ]; then
    warn "no thermal zone exposed — cannot measure"; STATUS_THERMAL="SKIP"
  else
    CPU_CRIT=$(zone_trip "${CPU_ZONE%/}" critical); [ -z "$CPU_CRIT" ] && CPU_CRIT=105
    CPU_PASS=$(zone_trip "${CPU_ZONE%/}" passive)
    CPU_FAIL_AT=$((CPU_CRIT - 5))
    SKIN_CRIT=""
    [ -n "$SKIN_ZONE" ] && SKIN_CRIT=$(zone_trip "${SKIN_ZONE%/}" critical)

    CORES=$(nproc)
    say "loading ${CORES} cores for ${SOAK_S}s, sampling every 10s"
    say "limits from silicon: throttle ${CPU_PASS:-?}°C · critical ${CPU_CRIT}°C · fail at ${CPU_FAIL_AT}°C${SKIN_CRIT:+ · skin critical ${SKIN_CRIT}°C}"
    if command -v stress >/dev/null 2>&1; then
      stress --cpu "$CORES" --timeout "${SOAK_S}s" >/dev/null 2>&1 &
      LOADPID=$!
    else
      warn "stress not installed — measuring idle temperature only"
      LOADPID=""
    fi
    MAX_SKIN=0; ELAPSED=0
    while [ "$ELAPSED" -lt "$SOAK_S" ]; do
      sleep 10; ELAPSED=$((ELAPSED + 10))
      T=$(awk '{printf "%d", $1/1000}' "${CPU_ZONE}temp" 2>/dev/null || echo 0)
      [ "$T" -gt "$MAX_TEMP" ] && MAX_TEMP=$T
      if [ -n "$SKIN_ZONE" ]; then
        S=$(awk '{printf "%d", $1/1000}' "${SKIN_ZONE}temp" 2>/dev/null || echo 0)
        [ "$S" -gt "$MAX_SKIN" ] && MAX_SKIN=$S
        say "  ${ELAPSED}s  cpu ${T}°C  skin ${S}°C"
      else
        say "  ${ELAPSED}s  cpu ${T}°C"
      fi
    done
    [ -n "$LOADPID" ] && wait "$LOADPID" 2>/dev/null

    if [ "$MAX_TEMP" -eq 0 ]; then
      warn "temperature never read"; STATUS_THERMAL="SKIP"
    elif [ "$MAX_TEMP" -ge "$CPU_FAIL_AT" ]; then
      STATUS_THERMAL="FAIL"; FAIL_THERMAL=1
      bad "cpu peak ${MAX_TEMP}°C — within 5°C of the ${CPU_CRIT}°C shutdown point. Check heatsink seating."
    elif [ -n "$SKIN_CRIT" ] && [ "$MAX_SKIN" -ge "$SKIN_CRIT" ]; then
      STATUS_THERMAL="FAIL"; FAIL_THERMAL=1
      bad "skin peak ${MAX_SKIN}°C ≥ ${SKIN_CRIT}°C — the enclosure gets too hot to hold."
    elif [ -n "$CPU_PASS" ] && [ "$MAX_TEMP" -ge "$CPU_PASS" ]; then
      # Throttling under an all-core soak is normal; record, don't reject.
      ok "cpu peak ${MAX_TEMP}°C — throttling above ${CPU_PASS}°C as designed, ${CPU_CRIT}°C is the limit"
    else
      ok "cpu peak ${MAX_TEMP}°C${SKIN_ZONE:+ · skin ${MAX_SKIN}°C}"
    fi
  fi
fi

# Verdict
TOTAL_FAIL=$((FAIL_RAM + FAIL_STORAGE + FAIL_THERMAL))
head2 "RESULT"
printf '  %-10s %s\n' "memory"  "$([ $FAIL_RAM     -eq 0 ] && echo "${C_GRN}PASS${C_RST}" || echo "${C_RED}FAIL${C_RST}  ${RAM_ERRORS} mismatches")" | tee -a "$LOG"
printf '  %-10s %s\n' "storage" "$(fmt_status "$STATUS_STORAGE")" | tee -a "$LOG"
printf '  %-10s %s\n' "thermal" "$(fmt_status "$STATUS_THERMAL")$([ "$STATUS_THERMAL" = "FAIL" ] && echo "  peak ${MAX_TEMP}°C")" | tee -a "$LOG"

write_result

echo | tee -a "$LOG"
if [ "$TOTAL_FAIL" -eq 0 ]; then
  printf '%s  ██  BURN-IN PASS  ██%s\n' "$C_GRN$C_BLD" "$C_RST" | tee -a "$LOG"
  [ "$QUICK" -eq 1 ] && say "quick mode: a full run is still required before shipping"
  say "log ${LOG} · result ${JSON}"
  exit 0
else
  printf '%s  ██  BURN-IN FAIL  ██%s\n' "$C_RED$C_BLD" "$C_RST" | tee -a "$LOG"
  [ "$FAIL_RAM" -eq 1 ] && say "DO NOT SHIP. Bad DRAM surfaces later as kernel panics, filesystem"
  [ "$FAIL_RAM" -eq 1 ] && say "corruption and random process crashes that look like software bugs."
  say "log ${LOG} · result ${JSON}"
  exit 1
fi
