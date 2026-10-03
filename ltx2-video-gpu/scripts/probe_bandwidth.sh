#!/bin/bash
# probe_bandwidth.sh — runs ON the rented instance, before deploy.
#
# Measures real throughput to the real source (the gated LTX-2 file on Hugging
# Face) with parallel ranged requests, so a host's advertised inet_down can't
# mislead us. The old probe fetched a Hetzner test file instead, and printed
# "0.0 Mbps" for ANY failure (DNS, 404, timeout), which looks like "slow host".
#
# Last stdout line: PROBE_MBPS=<n> BYTES=<got>/<want> SECS=<t> CODES=<http codes>
# PROBE_MBPS=0 means the probe itself failed (no token, bad HTTP code, no bytes).
set -u

PARTS="${PARTS:-4}"
# 4 x 128 MiB: enough to get past TCP slow start, short enough to finish in seconds on a good host.
PART_BYTES="${PART_BYTES:-134217728}"
URL="https://huggingface.co/Lightricks/LTX-2/resolve/main/ltx-2-19b-distilled.safetensors"

[ -s "$HOME/.hf_token" ] || { echo "PROBE_MBPS=0 REASON=no_token"; exit 2; }
TOKEN=$(cat "$HOME/.hf_token")

TMP=$(mktemp -d)
START=$(date +%s.%N)
for i in $(seq 0 $((PARTS - 1))); do
  # Offsets are 1 GiB apart so the parts hit different chunks of the file.
  OFF=$((i * 1073741824))
  curl -sL -m 60 -H "Authorization: Bearer $TOKEN" -r "${OFF}-$((OFF + PART_BYTES - 1))" \
    -o /dev/null -w '%{size_download} %{http_code}\n' "$URL" > "$TMP/$i" 2>/dev/null &
done
wait
END=$(date +%s.%N)

cat "$TMP"/? 2>/dev/null | awk -v s="$START" -v e="$END" -v want="$((PARTS * PART_BYTES))" '
  { got += $1; codes = codes (codes ? "," : "") $2; if ($2 != "206" && $2 != "200") bad = 1 }
  END {
    secs = e - s; if (secs < 0.001) secs = 0.001
    mbps = (bad || got == 0) ? 0 : got * 8 / 1e6 / secs
    printf "PROBE_MBPS=%d BYTES=%d/%d SECS=%.1f CODES=%s\n", mbps, got, want, secs, codes
  }'
rm -rf "$TMP"
