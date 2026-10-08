#!/bin/bash
# run_remote_suite.sh -- runs on the GitHub RUNNER. Starts render_test.py on the box DETACHED (nohup) and polls its
# output, so a dropped SSH connection cannot kill an hour-long suite. Streams new lines to this step's log and
# exits with render_test.py's exit code (124 overall timeout, 125 box unreachable, 126 process died without a marker).
#
#   REMOTE   command prefix that runs a shell command on the box, e.g. "ssh -p 123 root@1.2.3.4"
#            (for a local test: REMOTE="bash -c")
#   TIER, DPH   passed to render_test.py;  POLL seconds between polls;  MAX_S overall limit
set -u
: "${REMOTE:?REMOTE not set}"
TIER="${TIER:-smoke}"; DPH="${DPH:-0}"; POLL="${POLL:-20}"; MAX_S="${MAX_S:-9000}"; MAX_FAILS="${MAX_FAILS:-30}"
WARN_FREE_GB="${WARN_FREE_GB:-10}"

$REMOTE "cd ~ && rm -f render_done render_test.out && (TIER=$TIER DPH=$DPH nohup bash -c 'python3 render_test.py; echo \$? > render_done' > render_test.out 2>&1 < /dev/null &) ; echo LAUNCHED" < /dev/null
OFF=0; FAILS=0; DEAD=0; T0=$(date +%s)

drain() {
  local sz
  sz=$($REMOTE 'cd ~ && wc -c < render_test.out' < /dev/null 2>/dev/null | tr -dc '0-9')
  [ "${sz:-0}" -gt "$OFF" ] && $REMOTE "cd ~ && tail -c +$((OFF + 1)) render_test.out" < /dev/null
}

while :; do
  sleep "$POLL"
  SZ=$($REMOTE 'cd ~ && wc -c < render_test.out 2>/dev/null || echo 0' < /dev/null 2>/dev/null | tr -dc '0-9')
  if [ -z "$SZ" ]; then
    FAILS=$((FAILS + 1)); echo "[poll] box not answering ($FAILS/$MAX_FAILS)"
    [ "$FAILS" -ge "$MAX_FAILS" ] && { echo "::error::lost contact with the box"; exit 125; }
    continue
  fi
  FAILS=0
  if [ "$SZ" -gt "$OFF" ]; then
    $REMOTE "cd ~ && tail -c +$((OFF + 1)) render_test.out | head -c $((SZ - OFF))" < /dev/null && OFF=$SZ
  fi

  DONE=$($REMOTE 'cd ~ && cat render_done 2>/dev/null' < /dev/null 2>/dev/null | tr -dc '0-9')
  if [ -n "$DONE" ]; then
    drain  # last bytes written between the size check and the marker
    echo "render_test exit $DONE after $(( $(date +%s) - T0 ))s"
    exit "$DONE"
  fi

  # A full disk also blocks the marker write, so "no marker" alone can mean "dead". Two polls in a row avoids a race at exit.
  ALIVE=$($REMOTE 'pgrep -f "[r]ender_test.py" > /dev/null && echo 1 || echo 0' < /dev/null 2>/dev/null | tr -dc '01')
  if [ "$ALIVE" = "0" ]; then DEAD=$((DEAD + 1)); else DEAD=0; fi
  if [ "$DEAD" -ge 2 ]; then
    drain
    echo "::error::render_test.py is gone and wrote no exit marker (disk full or crash) after $(( $(date +%s) - T0 ))s"
    $REMOTE 'df -h / | tail -1' < /dev/null 2>/dev/null
    exit 126
  fi

  FREE=$($REMOTE 'df --output=avail -BG / | tail -1' < /dev/null 2>/dev/null | tr -dc '0-9')
  [ -n "$FREE" ] && [ "$FREE" -lt "$WARN_FREE_GB" ] && echo "::warning::only ${FREE}GB free on the box"

  if [ $(( $(date +%s) - T0 )) -ge "$MAX_S" ]; then echo "::error::suite exceeded ${MAX_S}s"; exit 124; fi
done