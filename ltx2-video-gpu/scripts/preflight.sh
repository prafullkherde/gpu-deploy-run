#!/bin/bash
# preflight.sh — run this FIRST, right after SSH login.
set -e

echo "=================================================="
echo "STEP 0/6 — Real bandwidth check (advertised speed is not trustworthy)"
echo "=================================================="
SPEED_MBPS=$(curl -o /dev/null -s -w '%{speed_download}' -m 20 https://speed.hetzner.de/100MB.bin | awk '{printf "%.1f", $1/125000}')
echo "Measured download: ${SPEED_MBPS} Mbps"
awk -v s="$SPEED_MBPS" 'BEGIN{exit !(s<100)}' && { echo "FAIL: measured ${SPEED_MBPS} Mbps, below 100 Mbps floor. Likely repeats the earlier slow-download failures — destroy and create again for a different host."; exit 1; }

echo ""
echo "=================================================="
echo "STEP 1/6 — GPU visible to the OS?"
echo "=================================================="
nvidia-smi || { echo "FAIL: no GPU detected. Wrong instance type rented?"; exit 1; }

echo ""
echo "=================================================="
echo "STEP 2/6 — VRAM check (need 20GB+ for 19B fp8)"
echo "=================================================="
VRAM=$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits)
echo "Detected VRAM: ${VRAM} MB"
[ "$VRAM" -ge 20000 ] || { echo "FAIL: only ${VRAM}MB VRAM, need 20GB+"; exit 1; }

echo ""
echo "=================================================="
echo "STEP 3/6 — Disk space check (need 60GB+ free)"
echo "=================================================="
FREE=$(df --output=avail -BG . | tail -1 | tr -dc '0-9')
echo "Free disk: ${FREE}GB"
[ "$FREE" -ge 60 ] || { echo "FAIL: only ${FREE}GB free, need 60GB+. Resize volume."; exit 1; }

echo ""
echo "=================================================="
echo "STEP 4/6 — Driver + Python present?"
echo "=================================================="
nvidia-smi --query-gpu=driver_version --format=csv,noheader
python3 --version || { echo "FAIL: python3 not found"; exit 1; }

echo ""
echo "=================================================="
echo "STEP 5/6 — Internet reachable (for model download)?"
echo "=================================================="
curl -sSf -m 10 https://huggingface.co > /dev/null || { echo "FAIL: can't reach huggingface.co"; exit 1; }

echo ""
echo "=================================================="
echo "ALL CHECKS PASSED — safe to run bootstrap.sh"
echo "=================================================="
