#!/bin/bash
# run_remote_suite.sh -- runs on the GitHub RUNNER. Starts render_test.py on the box DETACHED (nohup) and polls its
# output, so a dropped SSH connection cannot kill an hour-long suite. Streams new lines to this step's log and
# exits with render_test.py's exit code (or 124 on overall timeout, 125 if the box stopped answering).
#
#   REMOTE   command prefix that runs a shell command on the box, e.g. "ssh -p 123 root@1.2.3.4"
#            (for a local test: REMOTE="bash -c")
#   TIER, DPH   passed to render_test.py;  POLL seconds between polls;  MAX_S overall limit
set -u
: "${REMOTE:?REMOTE not set}"
TIER="${TIER:-smoke}"; DPH="${DPH:-0}"; POLL="${POLL:-20}"; MAX_S="${MAX_S:-9000}"; MAX_FAILS="${MAX_FAILS:-30}"

$REMOTE "cd ~ && rm -f render_done render_test.out && (TIER=$TIER DPH=$DPH nohup bash -c 'python3 render_test.py; echo \$? > render_done' > render_test.out 2>&1 < /dev/null &) ; echo LAUNCHED" < /dev/null
OFF=0; FAILS=0; T0=$(date +%s)
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
    # last bytes written between the size check and the marker
    SZ2=$($REMOTE 'cd ~ && wc -c < render_test.out' < /dev/null | tr -dc '0-9')
    [ "${SZ2:-0}" -gt "$OFF" ] && $REMOTE "cd ~ && tail -c +$((OFF + 1)) render_test.out" < /dev/null
    echo "render_test exit $DONE after $(( $(date +%s) - T0 ))s"
    exit "$DONE"
  fi
  if [ $(( $(date +%s) - T0 )) -ge "$MAX_S" ]; then echo "::error::suite exceeded ${MAX_S}s"; exit 124; fi
done