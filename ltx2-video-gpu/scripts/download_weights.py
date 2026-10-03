#!/usr/bin/env python3
"""
Downloads the LTX-2 19B distilled checkpoint and the Gemma-3 12B text encoder
into /data (a plain directory on the instance's own disk).

Uses huggingface_hub's stable public functions (hf_hub_download,
snapshot_download) instead of its internal CLI module, which is not part of the
stable API and broke on this image's installed version.

Reads the HF token from the HF_TOKEN environment variable (set by the calling
shell command, which reads it from ~/.hf_token).

Prints one DL_STATS line (bytes, seconds, Mbps) so the workflow can log the
throughput this host actually delivered, next to what it advertised.
"""
import os
import sys
import time

from huggingface_hub import hf_hub_download, snapshot_download

DATA_DIR = "/data"

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


before = tree_bytes(DATA_DIR)
start = time.time()

print("Downloading LTX-2 19B distilled checkpoint...")
hf_hub_download(
    repo_id="Lightricks/LTX-2",
    filename="ltx-2-19b-distilled.safetensors",
    local_dir=DATA_DIR,
    token=token,
)

print("Downloading Gemma-3 12B text encoder...")
snapshot_download(
    repo_id="google/gemma-3-12b-it",
    local_dir=f"{DATA_DIR}/gemma3",
    token=token,
)

secs = max(time.time() - start, 0.001)
got = tree_bytes(DATA_DIR) - before
print(f"DL_STATS bytes={got} secs={secs:.0f} mbps={int(got * 8 / 1e6 / secs)}")
print("WEIGHTS_DOWNLOADED")
