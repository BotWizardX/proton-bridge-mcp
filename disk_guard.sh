#!/bin/bash
# Disk guard for Proton Mail Bridge. Bridge itself does not check free space.
# After the first sync you create run/synced (touch run/synced); from then on a missing gluon folder or a du of 0
# counts as a measurement failure (a breach), because the cache cannot really be empty.
# Usage: disk_guard.sh check   -> one check; exit 0 ok, 3 limit breached, 4 could not measure (fail closed)
#        disk_guard.sh watch   -> check every 60 s; on breach stop Bridge via start.sh stop, log, exit 3
# Limits are constants on purpose; no env value changes them.
set -u
readonly MAX_CACHE_BYTES=$((5 * 1024 * 1024 * 1024))    # gluon cache over 5 GB -> stop
readonly MIN_FREE_BYTES=$((60 * 1024 * 1024 * 1024))    # free space on / under 60 GB -> stop
readonly INTERVAL=60
DIR="$(cd "$(dirname "$0")" && pwd)"
LOG="$DIR/run/start.log"            # neutral: fixed state lines only, no mail data
EXTRA="$DIR/run/gluon-extra-dirs"   # one absolute path per line if Bridge's 'change location' is ever used
GLUON="$HOME/.local/share/protonmail/bridge-v3/gluon"
SYNCED="$DIR/run/synced"            # AFTER_FIRST_SYNC: create it once du shows the cache > 0
note() { mkdir -p "$DIR/run"; echo "$(date '+%Y-%m-%d %H:%M:%S %z') $1" >> "$LOG"; echo "$1"; }

cache_dirs() {
  echo "$GLUON"
  if [ -f "$EXTRA" ]; then grep -E '^/' "$EXTRA"; fi
}

# Prints a reason word and returns non-zero on breach.
check_once() {
  local total=0 d n free after=0
  [ -e "$SYNCED" ] && after=1                     # AFTER_FIRST_SYNC state
  if [ $after -eq 1 ] && [ ! -e "$GLUON" ]; then echo "measure"; return 4; fi
  while IFS= read -r d; do
    [ -e "$d" ] || continue                       # before first sync, not created yet counts as 0
    n=$(du -sb -- "$d" 2>/dev/null | awk 'NR==1{print $1}')
    [[ "$n" =~ ^[0-9]+$ ]] || { echo "measure"; return 4; }
    total=$((total + n))
  done < <(cache_dirs)
  # After the first sync an empty or vanished cache means we are not measuring the real cache.
  if [ $after -eq 1 ] && [ "$total" -eq 0 ]; then echo "measure"; return 4; fi
  free=$(df -B1 --output=avail / 2>/dev/null | awk 'NR==2{print $1}')
  [[ "$free" =~ ^[0-9]+$ ]] || { echo "measure"; return 4; }
  if [ "$total" -gt "$MAX_CACHE_BYTES" ]; then echo "cache"; return 3; fi
  if [ "$free" -lt "$MIN_FREE_BYTES" ]; then echo "free"; return 3; fi
  echo "ok"; return 0
}

line_for() {
  case "$1" in
    cache) echo "cache over 5 GB" ;;
    free) echo "free space on / under 60 GB" ;;
    *) echo "disk could not be measured" ;;
  esac
}

case "${1:-check}" in
  check)
    why=$(check_once); rc=$?
    if [ $rc -ne 0 ]; then note "GUARD BREACH $(line_for "$why")"; fi
    exit $rc ;;
  watch)
    note "GUARD watching every 60s"
    while true; do
      why=$(check_once); rc=$?
      if [ $rc -ne 0 ]; then
        note "GUARD STOP $(line_for "$why"); stopping Bridge"
        "$DIR/start.sh" stop-bridge
        exit 3
      fi
      sleep "$INTERVAL" || { note "GUARD ended"; exit 0; }
    done ;;
  *) sed -n 2,7p "$0"; exit 2 ;;
esac
