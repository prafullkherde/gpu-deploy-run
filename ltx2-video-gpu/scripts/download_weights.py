#!/usr/bin/env python3
"""
Downloads the LTX-2 19B distilled checkpoint and the Gemma-3 12B text encoder
into /data (a plain directory on the instance's own disk).

Uses huggingface_hub's stable public functions (hf_hub_download,
snapshot_download) instead of its internal CLI module, which is not part of the
stable API and broke on this image's installed version.

Reads the HF token from the HF_TOKEN environment variable (set by the calling
shell command, which reads it from ~/.hf_token).

PROGRESS REPORTING: huggingface_hub's own progress bar disables itself when
stdout isn't a terminal (true here — output is redirected to deploy.log),
so without this, nothing is printed for the ENTIRE download duration. The
calling workflow's stall-detector then sees no log growth for 5 minutes and
kills a perfectly healthy download. Fixed by polling real bytes-on-disk in a
background thread every 5s and printing a flushed DL_PROGRESS line — this is
independent of huggingface_hub's internal behavior, so it can't go silent.

Prints one DL_STATS line (bytes, seconds, Mbps) so the workflow can log the
throughput this host actually delivered, next to what it advertised.
"""
import os
import sys
import threading
import time

from huggingface_hub import hf_hub_download, snapshot_download

# Belt-and-suspenders against output buffering hiding progress: the
# PYTHONUNBUFFERED env var set by bootstrap.sh/start.sh should already cover
# this, but reconfigure explicitly here too in case this script is ever run
# standalone without that env var set.
sys.stdout.reconfigure(line_buffering=True)

DATA_DIR = "/data"
PROGRESS_INTERVAL_S = 5

token = os.environ.get("HF_TOKEN")
if not token:
    print("HF_TOKEN not set in environment", file=sys.stderr)
    sys.exit(1)


def tree_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass
    return total


def progress_reporter(stop_event, baseline, label):
    """Runs in a background thread. Polls real bytes on disk every few
    seconds and prints a flushed line — ground truth, not dependent on
    huggingface_hub's own (silent-when-non-TTY) progress bar."""
    start = time.time()
    while not stop_event.is_set():
        time.sleep(PROGRESS_INTERVAL_S)
        now_bytes = tree_bytes(DATA_DIR) - baseline
        elapsed = max(time.time() - start, 0.001)
        mbps = int(now_bytes * 8 / 1e6 / elapsed)
        gb = now_bytes / 1e9
        print(f"DL_PROGRESS stage={label} gb={gb:.2f} elapsed_s={int(elapsed)} mbps={mbps}", flush=True)


def download_with_progress(label, fn, **kwargs):
    baseline = tree_bytes(DATA_DIR)
    stop_event = threading.Event()
    t = threading.Thread(target=progress_reporter, args=(stop_event, baseline, label), daemon=True)
    t.start()
    try:
        fn(**kwargs)
    finally:
        stop_event.set()
        t.join(timeout=PROGRESS_INTERVAL_S + 2)


before = tree_bytes(DATA_DIR)
start = time.time()

print("Downloading LTX-2 19B distilled checkpoint...", flush=True)
download_with_progress(
    "ltx2",
    hf_hub_download,
    repo_id="Lightricks/LTX-2",
    filename="ltx-2-19b-distilled.safetensors",
    local_dir=DATA_DIR,
    token=token,
)

print("Downloading Gemma-3 12B text encoder...", flush=True)
download_with_progress(
    "gemma3",
    snapshot_download,
    repo_id="google/gemma-3-12b-it",
    local_dir=f"{DATA_DIR}/gemma3",
    token=token,
)

secs = max(time.time() - start, 0.001)
got = tree_bytes(DATA_DIR) - before
print(f"DL_STATS bytes={got} secs={secs:.0f} mbps={int(got * 8 / 1e6 / secs)}", flush=True)
print("WEIGHTS_DOWNLOADED", flush=True)
