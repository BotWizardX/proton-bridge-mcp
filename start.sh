#!/bin/bash
# Proton Mail Bridge runner for the MCP connector. Usage: start.sh start|stop|status|cli|guard
# Bridge stdout/stderr go to /dev/null; Bridge keeps its own logs in ~/.local/share/protonmail/bridge-v3/logs.
# Never add --log-smtp. PROTONMAIL_ENV is unset on purpose: Bridge runs in its normal mode, with its default
# crash reports to Proton left on (PROTONMAIL_ENV=dev would turn those off).
# disk_guard.sh runs once before Bridge starts (refuses on breach) and then watches every 60 s in its own tmux session.
set -u
DIR="$(cd "$(dirname "$0")" && pwd)"
SESSION=proton-bridge
GSESSION=proton-bridge-guard
LOG="$DIR/run/start.log"   # neutral: times and states only, no mail data
BIN=$(command -v protonmail-bridge || true)
unset PROTONMAIL_ENV
note() { mkdir -p "$(dirname "$LOG")"; echo "$(date '+%Y-%m-%d %H:%M:%S %z') $1" >> "$LOG"; echo "$1"; }
# IMAP port to wait for: the port Bridge is set to (Bridge's default 1143). Set PROTON_MCP_IMAP_PORT to the same
# value the MCP servers use if you changed it in Bridge. Digits only; anything else falls back to 1143.
IMAP_PORT="${PROTON_MCP_IMAP_PORT:-1143}"
[[ "$IMAP_PORT" =~ ^[0-9]{1,5}$ ]] || IMAP_PORT=1143
listening() { ss -ltn 2>/dev/null | grep -qF "127.0.0.1:$IMAP_PORT "; }
preflight() { "$DIR/disk_guard.sh" check >/dev/null || { note "REFUSED start: disk guard limit"; exit 3; }; }
start_guard() {
  tmux has-session -t "=$GSESSION" 2>/dev/null && return 0
  tmux new-session -d -s "$GSESSION" "exec $DIR/disk_guard.sh watch >/dev/null 2>&1"
  tmux has-session -t "=$GSESSION" 2>/dev/null && note "guard started" || { note "FAIL guard did not start"; return 1; }
}
stop_bridge() {
  # tmux session (service) and any foreground CLI started by 'start.sh cli'.
  tmux kill-session -t "=$SESSION" 2>/dev/null; local a=$?
  # Own user only, anchored. Matches the official Bridge 3.x .deb layout (checked with dpkg -L and ps -o args):
  # /usr/bin/protonmail-bridge is a symlink to the launcher /usr/lib/protonmail/bridge/proton-bridge, which
  # runs /usr/lib/protonmail/bridge/bridge. A downloaded update would run from the updates folder instead.
  pkill -TERM -u "$(id -u)" -f '^/usr/lib/protonmail/bridge/' 2>/dev/null
  pkill -TERM -u "$(id -u)" -f '^/usr/bin/protonmail-bridge( |$)' 2>/dev/null
  pkill -TERM -u "$(id -u)" -f "^$HOME/.local/share/protonmail/bridge-v3/updates/" 2>/dev/null
  [ $a -eq 0 ] && note "stopped" || note "bridge stop sent (no service session)"
}
case "${1:-status}" in
  start)
    [ -n "$BIN" ] || { note "FAIL bridge not installed"; exit 1; }
    preflight
    if tmux has-session -t "=$SESSION" 2>/dev/null; then start_guard; note "already running"; exit 0; fi
    start_guard || exit 1
    tmux new-session -d -s "$SESSION" "exec $BIN --noninteractive --log-level warn >/dev/null 2>&1"
    for i in $(seq 1 30); do listening && { note "started, IMAP on 127.0.0.1:$IMAP_PORT"; exit 0; }; sleep 2; done
    note "FAIL started but IMAP not listening after 60s (sign-in may be needed)"; exit 1 ;;
  stop)
    stop_bridge
    tmux kill-session -t "=$GSESSION" 2>/dev/null && note "guard stopped" ;;
  stop-bridge)
    # Called by disk_guard.sh on a breach; the guard exits by itself after this.
    stop_bridge ;;
  guard)
    preflight; start_guard ;;
  status)
    GUARD=0; tmux has-session -t "=$GSESSION" 2>/dev/null && GUARD=1
    [ $GUARD -eq 1 ] && note "GUARD UP" || note "GUARD DOWN"
    if tmux has-session -t "=$SESSION" 2>/dev/null && listening; then
      if [ $GUARD -eq 1 ]; then note "UP"; exit 0; fi
      note "UP but GUARD DOWN: run start.sh guard or stop Bridge"; exit 4
    elif tmux has-session -t "=$SESSION" 2>/dev/null; then note "RUNNING, IMAP not listening (sign-in needed?)"; exit 1
    else note "DOWN"; exit 1; fi ;;
  cli)
    # Sign-in only (the account owner types the Proton password here). Stops the background service first; Bridge allows one instance.
    # The guard is checked and started BEFORE sign-in, because the first sync starts right after login.
    [ -n "$BIN" ] || { note "FAIL bridge not installed"; exit 1; }
    preflight
    tmux kill-session -t "=$SESSION" 2>/dev/null
    start_guard || exit 1
    note "cli opened for sign-in"
    exec "$BIN" --cli --log-level warn ;;
  *) echo "usage: start.sh start|stop|status|cli|guard"; exit 2 ;;
esac
