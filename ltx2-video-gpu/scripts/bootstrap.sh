#!/bin/bash
# bootstrap.sh — runs on an instance booted from the READYMADE
# robatvastai/wan2gp image. torch/CUDA/Wan2GP are already in the image.
# No persistent Volume anymore — /data is a plain ephemeral directory
# created fresh by ltx2-gpu.yml's deploy step; weights download every run.
set -e

WAN2GP_DIR="${WAN2GP_DIR:-/opt/workspace-internal/Wan2GP}"

echo "=================================================="
echo "STEP 0/4 — Re-verify preflight"
echo "=================================================="
bash preflight.sh

echo ""
echo "=================================================="
echo "STEP 1/4 — Find a python that actually has torch"
echo "=================================================="
# Non-interactive SSH skips .bashrc, so PATH here may not be the one
# the image's own docs assume. Try the sourced one first, then the
# known miniforge base as a fallback — NOT hardcoded blind, both are
# checked live every run.
source /root/.bashrc 2>/dev/null || true
source /etc/profile 2>/dev/null || true

PYTHON_BIN=""
if python3 -c "import torch" 2>/dev/null; then
  PYTHON_BIN="$(command -v python3)"
elif [ -x /opt/miniforge3/bin/python3 ] && /opt/miniforge3/bin/python3 -c "import torch" 2>/dev/null; then
  PYTHON_BIN="/opt/miniforge3/bin/python3"
fi

if [ -z "$PYTHON_BIN" ]; then
  echo "FAIL: no python with torch found (checked PATH python3 and /opt/miniforge3/bin/python3)."
  echo "Run the discovery steps manually on this host and update this script with the real path."
  exit 1
fi
echo "Using python: $PYTHON_BIN"
"$PYTHON_BIN" -c "import torch; print('torch:', torch.__version__, '| CUDA available:', torch.cuda.is_available())"

echo ""
echo "=================================================="
echo "STEP 2/4 — Download weights into /data (ephemeral — every run)"
echo "=================================================="
if [ -z "$HF_TOKEN" ]; then
  echo "FAIL: HF_TOKEN not set — cannot download gated LTX-2/Gemma weights."
  exit 1
fi
"$PYTHON_BIN" -m pip install -U huggingface_hub -q
"$PYTHON_BIN" "$HOME/download_weights.py"

echo ""
echo "=================================================="
echo "STEP 3/4 — Point Wan2GP's ckpts/ at /data via symlink"
echo "=================================================="
cd "$WAN2GP_DIR"
mkdir -p ckpts
ln -sfn /data/ltx-2-19b-distilled.safetensors ckpts/ltx-2-19b-distilled.safetensors
ln -sfn /data/gemma3 ckpts/gemma3
command -v ffmpeg > /dev/null || { echo "FAIL: ffmpeg not found in this image."; exit 1; }

echo ""
echo "=================================================="
echo "STEP 4/4 — Export PYTHON_BIN for start.sh"
echo "=================================================="
echo "export PYTHON_BIN=$PYTHON_BIN" > "$HOME/.ltx2_env"
echo "export WAN2GP_DIR=$WAN2GP_DIR" >> "$HOME/.ltx2_env"

echo ""
echo "=================================================="
echo "BOOTSTRAP COMPLETE — safe to run start.sh"
echo "=================================================="
