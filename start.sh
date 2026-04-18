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
# Output goes to BOTH stdout (visible in Replit Deployment Logs panel) AND
# the log file (for historical grep on the VM). Using `tee -a` for both
# so developers can diagnose without shelling into the deploy machine.
(
  echo "[watcher-gate] waiting for port $HEALTHCHECK_PORT (max ${WATCHER_STARTUP_DELAY_MAX}s)"
  port_opened=false
  # Use curl (always present in Nix Node images) rather than bash /dev/tcp,
  # which isn't always compiled in on Replit's deployment containers.
  for ((i=0; i<WATCHER_STARTUP_DELAY_MAX; i++)); do
    if curl -s -o /dev/null --max-time 2 "http://127.0.0.1:$HEALTHCHECK_PORT/" 2>/dev/null; then
      echo "[watcher-gate] port $HEALTHCHECK_PORT is open (${i}s) — starting drive_watcher"
      port_opened=true
      break
    fi
    sleep 1
    # Progress heartbeat every 15 s so it's obvious we're still waiting
    if (( i > 0 && i % 15 == 0 )); then
      echo "[watcher-gate] still waiting for port $HEALTHCHECK_PORT (${i}s elapsed)"
    fi
  done
  if [ "$port_opened" != true ]; then
    echo "[watcher-gate] WARNING: port $HEALTHCHECK_PORT never opened within ${WATCHER_STARTUP_DELAY_MAX}s — starting drive_watcher anyway"
  fi

  cd "$DOORMAT_DIR"
  while true; do
    echo "[watcher-loop] $(date -Iseconds) — starting drive_watcher.py" | tee -a "$WATCHER_LOG"
    python -u drive_watcher.py 2>&1 | tee -a "$WATCHER_LOG"
    exit_code=${PIPESTATUS[0]}
    echo "[watcher-loop] $(date -Iseconds) — drive_watcher exited (code=$exit_code); restarting in 10s" | tee -a "$WATCHER_LOG"
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
