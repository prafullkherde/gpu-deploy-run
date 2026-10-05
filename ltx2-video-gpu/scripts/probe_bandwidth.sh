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
#!/bin/bash
# Runs ON the rented box. Median of N rounds against the real HF file.
# Last line: PROBE_MBPS=<median> SAMPLES=<a,b,c> CODES=<http codes>  (0 = probe failed)
set -u
PARTS="${PARTS:-4}"
PART_BYTES="${PART_BYTES:-134217728}"
SAMPLES="${SAMPLES:-3}"
GAP="${GAP:-3}"
URL="https://huggingface.co/Lightricks/LTX-2/resolve/main/ltx-2-19b-distilled.safetensors"

[ -s "$HOME/.hf_token" ] || { echo "PROBE_MBPS=0 REASON=no_token"; exit 2; }
TOKEN=$(cat "$HOME/.hf_token")
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT

sample() {
  local r=$1 s e i off
  s=$(date +%s.%N)
  for i in $(seq 0 $((PARTS - 1))); do
    # Fresh offsets every round so a CDN cache can't flatter later rounds.
    off=$(( (r * PARTS + i) * 1073741824 ))
    curl -sL -m 60 -H "Authorization: Bearer $TOKEN" -r "${off}-$((off + PART_BYTES - 1))" \
      -o /dev/null -w '%{size_download} %{http_code}\n' "$URL" > "$TMP/$r.$i" 2>/dev/null &
  done
  wait
  e=$(date +%s.%N)
  cat "$TMP/$r".? | awk -v s="$s" -v e="$e" '
    { got += $1; codes = codes (codes ? "," : "") $2; if ($2 != "206" && $2 != "200") bad = 1 }
    END { t = e - s; if (t < 0.001) t = 0.001
          printf "%d %s\n", (bad || got == 0) ? 0 : got * 8 / 1e6 / t, codes }'
}

VALS=""; CODES=""
for r in $(seq 0 $((SAMPLES - 1))); do
  read -r mbps codes < <(sample "$r")
  VALS="$VALS $mbps"; CODES="$codes"
  [ "$r" -lt $((SAMPLES - 1)) ] && sleep "$GAP"
done
MEDIAN=$(printf '%s\n' $VALS | sort -n | sed -n "$(( (SAMPLES + 1) / 2 ))p")
printf 'PROBE_MBPS=%d SAMPLES=%s CODES=%s\n' "$MEDIAN" "$(echo $VALS | tr ' ' ',')" "$CODES"
