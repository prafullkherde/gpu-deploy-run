#!/usr/bin/env python3
"""
Downloads the LTX-2 19B distilled checkpoint and the Gemma-3 12B text
encoder onto the mounted volume (/data).

Uses huggingface_hub's stable public functions (hf_hub_download,
snapshot_download) instead of invoking its internal CLI module
(huggingface_hub.commands.huggingface_cli) — that internal path is not
part of the stable API and broke on this image's installed version
(ModuleNotFoundError: No module named 'huggingface_hub.commands').

Reads the HF token from the HF_TOKEN environment variable (set by the
calling shell command, which reads it from ~/.hf_token).
"""
import os
import sys

from huggingface_hub import hf_hub_download, snapshot_download

token = os.environ.get("HF_TOKEN")
if not token:
    print("HF_TOKEN not set in environment", file=sys.stderr)
    sys.exit(1)

print("Downloading LTX-2 19B distilled checkpoint...")
hf_hub_download(
    repo_id="Lightricks/LTX-2",
    filename="ltx-2-19b-distilled.safetensors",
    local_dir="/data",
    token=token,
)

print("Downloading Gemma-3 12B text encoder...")
snapshot_download(
    repo_id="google/gemma-3-12b-it",
    local_dir="/data/gemma3",
    token=token,
)

print("WEIGHTS_DOWNLOADED")
