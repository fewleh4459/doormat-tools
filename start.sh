#!/bin/bash
# Orchestrator for the Replit Reserved VM.
#
# Runs mousekeeping in the foreground IMMEDIATELY (so port 5000 opens
# quickly and Replit's healthcheck passes). Only after the port is
# responding does it kick off drive_watcher.py in the background — with
# a restart loop that re-launches it 10 s after any exit.
#
# Why the ordering matters: on a 0.5 vCPU VM, Node's startup is CPU-bound
# (font scanning, Vite bundle load, etc.). If we launch Python
# simultaneously, it steals enough CPU that Node takes >60 s to reach
# `server.listen()` and Replit terminates the process for "port never
# opened". Serial start fixes this.
#
# If mousekeeping exits → this script exits → Replit restarts the Repl.
# If drive_watcher.py exits → it's auto-restarted after 10 s.

set -u

DOORMAT_DIR="${DOORMAT_DIR:-/home/runner/workspace/doormat-tools}"
MOUSEKEEPING_DIR="${MOUSEKEEPING_DIR:-/home/runner/workspace}"
MOUSEKEEPING_CMD="${MOUSEKEEPING_CMD:-npm run dev}"
HEALTHCHECK_PORT="${HEALTHCHECK_PORT:-5000}"
WATCHER_STARTUP_DELAY_MAX="${WATCHER_STARTUP_DELAY_MAX:-180}"    # seconds

LOG_DIR="${LOG_DIR:-$DOORMAT_DIR/logs}"
mkdir -p "$LOG_DIR"
WATCHER_LOG="$LOG_DIR/drive_watcher.log"

echo "[start.sh] $(date -Iseconds) — starting services"
echo "[start.sh]   DOORMAT_DIR       = $DOORMAT_DIR"
echo "[start.sh]   MOUSEKEEPING_DIR  = $MOUSEKEEPING_DIR"
echo "[start.sh]   MOUSEKEEPING_CMD  = $MOUSEKEEPING_CMD"
echo "[start.sh]   HEALTHCHECK_PORT  = $HEALTHCHECK_PORT"
echo "[start.sh]   WATCHER_LOG       = $WATCHER_LOG"

if [ ! -d "$DOORMAT_DIR" ]; then
  echo "[start.sh] FATAL: $DOORMAT_DIR does not exist"
  exit 1
fi
if [ ! -d "$MOUSEKEEPING_DIR" ]; then
  echo "[start.sh] FATAL: $MOUSEKEEPING_DIR does not exist"
  exit 1
fi

# ── Background: wait for mousekeeping's port, then start drive_watcher ──────
(
  echo "[watcher-gate] waiting for port $HEALTHCHECK_PORT (max ${WATCHER_STARTUP_DELAY_MAX}s)"
  for ((i=0; i<WATCHER_STARTUP_DELAY_MAX; i++)); do
    if (exec 3<>/dev/tcp/127.0.0.1/$HEALTHCHECK_PORT) 2>/dev/null; then
      exec 3>&- 2>/dev/null
      echo "[watcher-gate] port $HEALTHCHECK_PORT is open — starting drive_watcher"
      break
    fi
    sleep 1
  done

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
echo "[start.sh] watcher-gate launched (pid=$WATCHER_LOOP_PID) — will start drive_watcher once port $HEALTHCHECK_PORT is open"

# Forward signals so a clean Replit stop kills the background loop too
trap "echo '[start.sh] shutting down…'; kill $WATCHER_LOOP_PID 2>/dev/null; exit" SIGINT SIGTERM

# ── Foreground: mousekeeping ────────────────────────────────────────────────
cd "$MOUSEKEEPING_DIR"
echo "[start.sh] handing off to: $MOUSEKEEPING_CMD"
exec $MOUSEKEEPING_CMD
