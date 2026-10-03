#!/bin/bash
# preflight.sh — local sanity checks on the rented box, run before bootstrap.
# Bandwidth is NOT checked here any more: probe_bandwidth.sh does it inside the
# workflow's rent loop, against the real source, so a slow host is replaced by
# the next ranked offer instead of failing the whole run.
set -e

echo "=================================================="
echo "STEP 1/5 — GPU visible to the OS?"
echo "=================================================="
nvidia-smi || { echo "FAIL: no GPU detected. Wrong instance type rented?"; exit 1; }

echo ""
echo "=================================================="
echo "STEP 2/5 — VRAM check (need 20GB+ for 19B fp8)"
echo "=================================================="
VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits | head -1)
echo "Detected VRAM: ${VRAM} MB"
[ "$VRAM" -ge 20000 ] || { echo "FAIL: only ${VRAM}MB VRAM, need 20GB+"; exit 1; }

echo ""
echo "=================================================="
echo "STEP 3/5 — Disk space check on /data (need 90GB+ free)"
echo "=================================================="
# 90 = ~67GB weights + headroom for any checkpoints Wan2GP fetches itself.
TARGET=/data; [ -d "$TARGET" ] || TARGET=.
FREE=$(df --output=avail -BG "$TARGET" | tail -1 | tr -dc '0-9')
echo "Free disk on $TARGET: ${FREE}GB"
[ "$FREE" -ge 90 ] || { echo "FAIL: only ${FREE}GB free on $TARGET, need 90GB+."; exit 1; }

echo ""
echo "=================================================="
echo "STEP 4/5 — Driver + Python present?"
echo "=================================================="
nvidia-smi --query-gpu=driver_version --format=csv,noheader
python3 --version || { echo "FAIL: python3 not found"; exit 1; }

echo ""
echo "=================================================="
echo "STEP 5/5 — Hugging Face reachable?"
echo "=================================================="
curl -sSf -m 10 https://huggingface.co > /dev/null || { echo "FAIL: can't reach huggingface.co"; exit 1; }

echo ""
echo "=================================================="
echo "ALL CHECKS PASSED — safe to run bootstrap.sh"
echo "=================================================="
