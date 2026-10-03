#!/bin/bash
# start.sh — only run after bootstrap.sh has completed.
set -e

source "$HOME/.ltx2_env"
cd "$WAN2GP_DIR"

if pgrep -f "wgp.py" > /dev/null; then
  echo "wgp.py already running — not launching a second instance."
  exit 0
fi

echo "=================================================="
echo "Starting Wan2GP — will listen on port 7860"
echo "=================================================="

"$PYTHON_BIN" wgp.py --listen --port 7860
