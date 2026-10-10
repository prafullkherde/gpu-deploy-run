#!/bin/bash
# start.sh — only run after bootstrap.sh has completed.
set -e
set -x   # trace every command — same reasoning as bootstrap.sh
export PYTHONUNBUFFERED=1   # same reasoning as bootstrap.sh — covers wgp.py's own startup output too

source "$HOME/.ltx2_env"
cd "$WAN2GP_DIR"

# The workflow polls deploy.log for "Starting Wan2GP". Print it on BOTH paths,
# otherwise an already-running app (e.g. started by the image itself) would
# leave the deploy step waiting for a marker that never comes.
if pgrep -f "wgp.py" > /dev/null; then
  echo "Starting Wan2GP — already running, not launching a second instance."
  exit 0
fi

echo "=================================================="
echo "Starting Wan2GP — will listen on port 7860"
echo "=================================================="

"$PYTHON_BIN" -u wgp.py --listen --server-port 7860
