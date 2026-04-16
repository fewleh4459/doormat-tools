#!/bin/bash
# Orchestrator: runs drive_watcher.py in background and hands off to mousekeeping.
#
# Use this as your Replit run command when you want the doormat watcher running
# alongside mousekeeping on the same Reserved VM:
#
#   run = "bash /home/runner/workspace/doormat-tools/start.sh"
#
# Expectations on the VM layout (monorepo style):
#   /home/runner/workspace/doormat-tools/   (this repo)
#   /home/runner/workspace/mousekeeping/    (mousekeeping repo cloned alongside)
#
# If your layout is different, edit the MOUSEKEEPING_DIR path below.

set -e

DOORMAT_DIR="$(cd "$(dirname "$0")" && pwd)"
MOUSEKEEPING_DIR="${MOUSEKEEPING_DIR:-$DOORMAT_DIR/../mousekeeping}"

echo "[start.sh] doormat dir:     $DOORMAT_DIR"
echo "[start.sh] mousekeeping dir: $MOUSEKEEPING_DIR"

# Start the doormat Drive watcher in background
cd "$DOORMAT_DIR"
nohup python drive_watcher.py > "$DOORMAT_DIR/drive_watcher.log" 2>&1 &
DOORMAT_PID=$!
echo "[start.sh] drive_watcher started (PID $DOORMAT_PID)"

# Forward termination signals so the background process dies with us
trap "echo '[start.sh] shutting down'; kill $DOORMAT_PID 2>/dev/null; exit" SIGINT SIGTERM

# Hand off to mousekeeping in the foreground
if [ -d "$MOUSEKEEPING_DIR" ]; then
  cd "$MOUSEKEEPING_DIR"
  exec npm start
else
  echo "[start.sh] mousekeeping dir not found — running doormat watcher alone"
  wait $DOORMAT_PID
fi
