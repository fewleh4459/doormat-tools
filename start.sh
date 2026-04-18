#!/bin/bash
# Orchestrator for the Replit Reserved VM — runs drive_watcher.py in a restart
# loop in the background, and hands off to mousekeeping (npm run dev) in the
# foreground.
#
# Use this as your Replit run command:
#     run = "bash doormat-tools/start.sh"
#
# If mousekeeping exits, this script exits (and Replit restarts the whole thing).
# If drive_watcher.py exits (crash or python error), it's auto-restarted after 10s.

set -u

# Paths — tweak if your layout differs
DOORMAT_DIR="${DOORMAT_DIR:-/home/runner/workspace/doormat-tools}"
MOUSEKEEPING_DIR="${MOUSEKEEPING_DIR:-/home/runner/workspace}"
MOUSEKEEPING_CMD="${MOUSEKEEPING_CMD:-npm run dev}"

LOG_DIR="${LOG_DIR:-$DOORMAT_DIR/logs}"
mkdir -p "$LOG_DIR"
WATCHER_LOG="$LOG_DIR/drive_watcher.log"

echo "[start.sh] $(date -Iseconds) — starting services"
echo "[start.sh]   DOORMAT_DIR       = $DOORMAT_DIR"
echo "[start.sh]   MOUSEKEEPING_DIR  = $MOUSEKEEPING_DIR"
echo "[start.sh]   MOUSEKEEPING_CMD  = $MOUSEKEEPING_CMD"
echo "[start.sh]   WATCHER_LOG       = $WATCHER_LOG"

# Fail fast if the directories don't exist
if [ ! -d "$DOORMAT_DIR" ]; then
  echo "[start.sh] FATAL: $DOORMAT_DIR does not exist"
  exit 1
fi
if [ ! -d "$MOUSEKEEPING_DIR" ]; then
  echo "[start.sh] FATAL: $MOUSEKEEPING_DIR does not exist"
  exit 1
fi

# ── Background: drive_watcher.py with restart loop ───────────────────────────
(
  cd "$DOORMAT_DIR"
  while true; do
    echo "[watcher-loop] $(date -Iseconds) — starting drive_watcher.py" >> "$WATCHER_LOG"
    python drive_watcher.py >> "$WATCHER_LOG" 2>&1
    exit_code=$?
    echo "[watcher-loop] $(date -Iseconds) — drive_watcher exited (code=$exit_code); restarting in 10s" >> "$WATCHER_LOG"
    sleep 10
  done
) &
WATCHER_LOOP_PID=$!
echo "[start.sh] drive_watcher loop started (pid=$WATCHER_LOOP_PID) → tail -f $WATCHER_LOG"

# Forward SIGINT/SIGTERM to the watcher loop so a clean Replit stop kills both
trap "echo '[start.sh] shutting down…'; kill $WATCHER_LOOP_PID 2>/dev/null; exit" SIGINT SIGTERM

# ── Foreground: mousekeeping ────────────────────────────────────────────────
cd "$MOUSEKEEPING_DIR"
echo "[start.sh] handing off to: $MOUSEKEEPING_CMD"
exec $MOUSEKEEPING_CMD
