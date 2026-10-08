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
echo "STEP 2/4 — Pre-download weights into /data (OFF by default)"
echo "=================================================="
# Run 37297908189 proved Wan2GP downloads its OWN files (ltx-2.3-22b distilled int8 + gemma-3-12b-it-qat,
# 42 GB) into ckpts/ and never reads /data. The 67 GB pre-download cost ~9 min and was unused.
# Set PREDOWNLOAD=1 only if you deliberately want the old behaviour.
PREDOWNLOAD="${PREDOWNLOAD:-0}"
if [ "$PREDOWNLOAD" = "1" ]; then
  if [ -z "$HF_TOKEN" ]; then
    echo "FAIL: HF_TOKEN not set — cannot download gated LTX-2/Gemma weights."
    exit 1
  fi
  # Install only if missing: upgrading inside the app's own env can bump a
  # version that Wan2GP / gradio / transformers pin.
  "$PYTHON_BIN" -c "import huggingface_hub" 2>/dev/null || "$PYTHON_BIN" -m pip install -q huggingface_hub
  "$PYTHON_BIN" -u "$HOME/download_weights.py"
else
  echo "SKIPPED (PREDOWNLOAD=0): Wan2GP fetches its own weights on first use."
  echo "DL_STATS bytes=0 secs=0 mbps=0"
fi

echo ""
echo "=================================================="
echo "STEP 3/4 — Point Wan2GP's ckpts/ at /data via symlink"
echo "=================================================="
# UNVERIFIED: Wan2GP may expect different filenames or fetch its own quantized
# checkpoints. The first run with hold_min>0 shows what it actually does.
cd "$WAN2GP_DIR"
mkdir -p ckpts
if [ "$PREDOWNLOAD" = "1" ]; then
  ln -sfn /data/ltx-2-19b-distilled.safetensors ckpts/ltx-2-19b-distilled.safetensors
  ln -sfn /data/gemma3 ckpts/gemma3
fi
command -v ffmpeg > /dev/null || { echo "FAIL: ffmpeg not found in this image."; exit 1; }

echo ""
echo "=================================================="
echo "STEP 3a — Optional: update Wan2GP to the latest release (WANGP_UPDATE=1)"
echo "=================================================="
# Why: the pinned image carries an older Wan2GP (menus show "LTX-2.3 Distilled 1.0" and "Qwen Image 20B"). Upstream
# (v13.141, Sep 2026) lists LTX-2.5, Qwen Image 2.1, Krea 2 and Distilled 1.1. Off by default; a failed update rolls back.
WANGP_UPDATE="${WANGP_UPDATE:-0}"
WANGP_REPO="${WANGP_REPO:-https://github.com/deepbeepmeep/Wan2GP.git}"
if [ "$WANGP_UPDATE" = "1" ]; then
  cd "$WAN2GP_DIR"
  BEFORE=$(git rev-parse --short HEAD 2>/dev/null || echo "nogit")
  BACKUP=/tmp/wan2gp_backup.tgz
  tar -czf "$BACKUP" --exclude=./ckpts --exclude=./outputs --exclude=./loras --exclude=./venv --exclude=./.git . 2>/dev/null || true
  UPDATE_OK=0
  if [ -d .git ]; then
    git fetch --depth 1 "$WANGP_REPO" main 2>&1 | tail -2 && git reset --hard FETCH_HEAD 2>&1 | tail -1 && UPDATE_OK=1
  else
    TMPD=$(mktemp -d); git clone --depth 1 "$WANGP_REPO" "$TMPD/w" 2>&1 | tail -1 \
      && (cd "$TMPD/w" && tar -cf - --exclude=.git .) | tar -xf - -C "$WAN2GP_DIR" && UPDATE_OK=1
  fi
  AFTER=$(git rev-parse --short HEAD 2>/dev/null || echo "nogit")
  if [ "$UPDATE_OK" = "1" ]; then
    "$PYTHON_BIN" -m pip install -q -r requirements.txt 2>&1 | tail -3 || true
    # proof the update still starts: argparse runs before any model loads
    if timeout 240 "$PYTHON_BIN" wgp.py --help > /tmp/wgp_help.out 2>&1; then
      echo "UPDATE: wan2gp $BEFORE -> $AFTER, startup check OK"
    else
      echo "UPDATE: startup check FAILED ($(tail -1 /tmp/wgp_help.out | cut -c1-120)); rolling back"
      if [ -d .git ] && [ "$BEFORE" != "nogit" ]; then git reset --hard "$BEFORE" 2>&1 | tail -1; fi
      tar -xzf "$BACKUP" -C "$WAN2GP_DIR" 2>/dev/null || true
      echo "UPDATE: rolled back to $BEFORE"
    fi
  else
    echo "UPDATE: could not fetch $WANGP_REPO (continuing with the pinned version)"
  fi
else
  echo "SKIPPED (WANGP_UPDATE=0): running the Wan2GP that ships in the image."
fi

echo ""
echo "=================================================="
echo "STEP 3b — Known Wan2GP / Triton compatibility patch (LTX-2 RoPE kernel)"
echo "=================================================="
# Run 37297908189: every LTX-2.3 render died in models/ltx2/denoiser_triton.py::_split_rope with
#   TypeError: 'constexpr' object is not subscriptable
# A public report (LykosAI/StabilityMatrix #1756, RTX 5090, torch 2.7.1+cu128, Triton 3.3.1) traces the
# same error to indexing tl.constexpr objects GRID / AXIS_IDS and fixes it by indexing `.value`.
# UNVERIFIED here: single third-party source. Idempotent, scoped to _split_rope, original kept as .orig.
set +x
"$PYTHON_BIN" - "$WAN2GP_DIR/models/ltx2/denoiser_triton.py" <<'PY' || echo "PATCH denoiser_triton: patcher error (continuing unpatched)"
import re, shutil, sys

path = sys.argv[1]
try:
    src = open(path).read()
except FileNotFoundError:
    print("PATCH denoiser_triton: file not found (skipped)")
    sys.exit(0)

m = re.search(r"(@triton\.jit\s*\ndef _split_rope\(.*?)(?=\n@triton\.jit|\ndef |\Z)", src, re.S)
if not m:
    print("PATCH denoiser_triton: _split_rope not found (nothing changed)")
    sys.exit(0)

body, total = m.group(1), 0
for name in ("AXIS_IDS", "GRID"):
    body, n = re.subn(rf"\b{name}\[", f"{name}.value[", body)  # `\b` and `[` keep it from re-matching `.value[`
    total += n
if total == 0:
    print("PATCH denoiser_triton: already applied or no indexing found (nothing changed)")
    sys.exit(0)

shutil.copy(path, path + ".orig")
open(path, "w").write(src[:m.start(1)] + body + src[m.end(1):])
print(f"PATCH denoiser_triton: applied ({total} index sites in _split_rope); backup {path}.orig")
PY
set -x
rm -rf "$HOME/.triton/cache" 2>/dev/null || true   # stale compiled kernels must not mask the patch

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