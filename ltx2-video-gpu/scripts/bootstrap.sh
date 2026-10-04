#!/bin/bash
# bootstrap.sh — runs on an instance booted from the READYMADE
# robatvastai/wan2gp image. torch/CUDA/Wan2GP are already in the image.
# No persistent Volume — /data is a plain ephemeral directory created by
# ltx2-gpu.yml's deploy step; weights download every run.
# preflight.sh is run once by the workflow's deploy chain; not repeated here.
set -e
set -x   # trace every command as it runs — no more guessing what's "in progress"

# Without this, Python buffers stdout in large chunks when writing to a file
# (deploy.log) instead of a terminal — so even explicit print() calls can sit
# unflushed for a long time. This forces every python process launched from
# here (download_weights.py included) to flush output immediately.
export PYTHONUNBUFFERED=1

WAN2GP_DIR="${WAN2GP_DIR:-/opt/workspace-internal/Wan2GP}"

echo "=================================================="
echo "STEP 1/4 — Find a python that actually has torch"
echo "=================================================="
# Earlier discovery runs showed torch is NOT in /usr/bin/python3 or the empty
# miniforge base, even after sourcing .bashrc: Ubuntu's .bashrc returns
# immediately in non-interactive shells, so a venv it activates stays invisible.
# So probe likely venv locations directly instead of trusting PATH.
source /etc/profile 2>/dev/null || true

CANDIDATES=(
  "$(command -v python3 2>/dev/null || true)"
  /venv/main/bin/python
  /venv/*/bin/python
  "$WAN2GP_DIR/venv/bin/python"
  "$WAN2GP_DIR/.venv/bin/python"
  /opt/miniforge3/bin/python3
  /opt/conda/bin/python
)

PYTHON_BIN=""
for c in "${CANDIDATES[@]}"; do
  [ -n "$c" ] && [ -x "$c" ] || continue
  if "$c" -c "import torch" 2>/dev/null; then PYTHON_BIN="$c"; break; fi
done

if [ -z "$PYTHON_BIN" ]; then
  echo "FAIL: no python with torch found. Checked: ${CANDIDATES[*]}"
  echo "--- diagnostics (paste these back) ---"
  ls -d /venv/* 2>/dev/null || echo "no /venv"
  find / -maxdepth 7 -type d -path '*-packages/torch' 2>/dev/null | head -5
  grep -rIl 'wgp.py' /etc /opt/supervisor-scripts /opt/instance-tools 2>/dev/null | head -5
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
# Install only if missing: upgrading inside the app's own env can bump a
# version that Wan2GP / gradio / transformers pin.
"$PYTHON_BIN" -c "import huggingface_hub" 2>/dev/null || "$PYTHON_BIN" -m pip install -q huggingface_hub
"$PYTHON_BIN" -u "$HOME/download_weights.py"

echo ""
echo "=================================================="
echo "STEP 3/4 — Point Wan2GP's ckpts/ at /data via symlink"
echo "=================================================="
# UNVERIFIED: Wan2GP may expect different filenames or fetch its own quantized
# checkpoints. The first run with hold_min>0 shows what it actually does.
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
