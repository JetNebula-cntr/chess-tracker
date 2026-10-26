#!/bin/bash
# Scheduled sync for the v2 tracker, run by launchd
# (~/Library/LaunchAgents/org.jetnebula.lichess-sync.plist).
#
#   load.py     fetch new rated rapid games into jetnebula.db
#   analyze.py  Stockfish over any games not yet analysed
#   rollup.py   rebuild out/docs.json for the tracker page
#
# Then runs the v1 sync (~/lichess/sync.sh) so games.ndjson stays current
# for the "Lichess tracker update" cloud routine that still reads it.
# Remove that last step once the v1 page is retired.

set -u
DIR="$HOME/chess"
LOG="$DIR/sync.log"
LOCK="$DIR/.sync.lock"
PY=/usr/bin/python3

ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "$(ts) $1" >> "$LOG"; }

# A catch-up analysis can run for a while; never let two runs overlap.
if ! mkdir "$LOCK" 2>/dev/null; then
  log "SKIP previous run still in progress"
  exit 0
fi
trap 'rmdir "$LOCK"' EXIT

cd "$DIR" || exit 1

step() {
  local name="$1"; shift
  local out
  if out=$("$PY" "$@" 2>&1); then
    log "OK $name: $(echo "$out" | tail -1)"
  else
    log "ERROR $name: $(echo "$out" | tail -3 | tr '\n' ' ')"
    return 1
  fi
}

step load load.py && step analyze analyze.py && step rollup rollup.py
V2=$?

/bin/bash "$HOME/lichess/sync.sh" || log "ERROR v1 sync failed, see ~/lichess/sync.log"

tail -n 300 "$LOG" > "$LOG.tmp" 2>/dev/null && mv "$LOG.tmp" "$LOG"
exit $V2
