#!/bin/bash
# Watch MaaNikki's maafw.log during a supervised macOS device run (developer tool, never shipped):
#   scripts/macos_watch_log.sh "$HOME/Library/Application Support/MaaNikki/debug/maafw.log" [HEARTBEAT_SECONDS]
# One line per event: TASK START/OK/FAILED <entry>, PAUSE / RESUME (the game left or came back to the
# front), scene-press, abandoned, not-sending and [ERR] lines, and WATCH ALIVE every HEARTBEAT seconds
# (default 300) so that silence can be told apart from a dead watcher. VERBOSE=1 also reports every UI
# click, release and scroll.
LOG="$1"; HEARTBEAT="${2:-300}"
[ -f "$LOG" ] || { echo "WATCH ERROR: no file $LOG"; exit 1; }
# Read from the size measured before WATCH START: lines written while tail is still opening are not skipped.
start=$(( $(stat -f %z "$LOG") + 1 ))
echo "WATCH START $(date +%T) $LOG"
( while sleep "$HEARTBEAT"; do echo "WATCH ALIVE $(date +%T)"; done ) &
hb=$!
trap 'kill $hb 2>/dev/null' EXIT
pattern='msg=Tasker\.Task\.(Starting|Succeeded|Failed)|MaaContextRunTask.*游戏已(离开|回到)前台|scene press|abandoned|not sending|\]\[ERR\]|^tail:'
[ -n "$VERBOSE" ] && pattern="$pattern|press accepted|release posted|scroll posted"
# -F follows the name across maafw.log rotation (maafw.bak.*.log).
tail -c "+$start" -F "$LOG" 2>&1 | grep --line-buffered -E "$pattern" | awk '
{
  ts = substr($0, 13, 12)
  entry = ""
  if (match($0, /"entry":"[^"]*"/)) entry = substr($0, RSTART + 9, RLENGTH - 10)
  if ($0 ~ /Tasker\.Task\.Starting/) print ts " TASK START " entry
  else if ($0 ~ /Tasker\.Task\.Succeeded/) print ts " TASK OK " entry
  else if ($0 ~ /Tasker\.Task\.Failed/) print ts " TASK FAILED " entry
  else if ($0 ~ /游戏已离开前台/) print ts " PAUSE (game left the front)"
  else if ($0 ~ /游戏已回到前台/) print ts " RESUME (game back in front)"
  else { line = $0; sub(/^\[[^]]*\]\[/, "[", line); print ts " " substr(line, 1, 260) }
  fflush()
}'
echo "WATCH END $(date +%T): tail exited"
