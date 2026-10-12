#!/usr/bin/env python3
"""
qa_review.py -- OPTIONAL automatic mistake check, run on the GitHub runner after the results are pulled (free CPU).

For every successful case it sends the review image (the picture itself, or 4 frames of the video side by side) plus the
prompt and the case's own checklist (e.g. "sword is in a scabbard, hilt up") to a vision model and asks for a verdict.
It catches what numbers cannot: two heads, a drawn sword, uncovered legs, merged characters, garbled text, wrong optics.
It cannot hear audio or judge motion between the 4 frames, so lip sync and timing stay a human check.

Needs the repository secret ANTHROPIC_API_KEY; without it the script prints a notice and exits 0.
  QA_MODEL    model id (default claude-sonnet-5-5)       QA_MAX    max cases to review (default 40)
  QA_API_URL  override the endpoint (used by the test)    RESULTS   results folder (default results)
Stdlib only.
"""
import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

RESULTS = Path(os.environ.get("RESULTS", "results"))
API_URL = os.environ.get("QA_API_URL", "https://api.anthropic.com/v1/messages")
MODEL = os.environ.get("QA_MODEL", "claude-sonnet-5-5")
MAX_CASES = int(os.environ.get("QA_MAX", "40"))
KEY = os.environ.get("ANTHROPIC_API_KEY", "")

GENERIC = [
    "Each person has exactly one head, two arms, two legs, and two hands with five fingers each (where visible).",
    "Faces are undistorted; eyes are sharp; teeth and mouths look natural.",
    "Objects and props keep the same size, shape and colour across the frames.",
    "No haze, fog or muddy low contrast; exposure is bright enough to see detail.",
    "No garbled or fake text, no watermark.",
    "Characters that should interact face each other and are clearly separate bodies.",
]
SYSTEM = ("You are a strict quality reviewer for AI-generated images and video frames. You are shown ONE image (for a video: four frames "
          "from the clip side by side, left to right in time). Check the image against the checklist and the prompt. Be specific and brief. "
          "Reply with ONLY a JSON object: {\"verdict\": \"pass\" | \"fail\" | \"unsure\", \"issues\": [short strings], \"fixes\": [short prompt fixes], "
          "\"checklist\": {\"<item>\": \"ok\" | \"problem\" | \"cannot tell\"}}.")


def call(image_path, user_text):
    body = {
        "model": MODEL, "max_tokens": 900, "system": SYSTEM,
        "messages": [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.b64encode(image_path.read_bytes()).decode()}},
            {"type": "text", "text": user_text}]}],
    }
    req = urllib.request.Request(API_URL, data=json.dumps(body).encode(), method="POST",
                                 headers={"content-type": "application/json", "x-api-key": KEY, "anthropic-version": "2023-06-01"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if e.code in (429, 500, 502, 503, 529) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise RuntimeError(f"HTTP {e.code}: {e.read()[:200]!r}") from e
        except urllib.error.URLError as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise RuntimeError(f"network: {e}") from e


def parse(resp):
    text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {"verdict": "unsure", "issues": [f"unparseable reply: {text[:120]}"], "fixes": [], "checklist": {}}
    try:
        return json.loads(m.group(0))
    except json.JSONDecodeError:
        return {"verdict": "unsure", "issues": [f"invalid JSON: {text[:120]}"], "fixes": [], "checklist": {}}


def main():
    if not KEY:
        print("qa_review: ANTHROPIC_API_KEY is not set; skipping the automatic review (add the secret to enable it).")
        return 0
    rj = RESULTS / "results.json"
    if not rj.exists():
        print("qa_review: no results.json; nothing to review")
        return 0
    cases = [c for c in json.loads(rj.read_text())["cases"] if c.get("status") == "SUCCESS" and c.get("qa_image")][:MAX_CASES]
    out, lines = [], ["### Automatic review (vision model; frames only, no audio)", "", "| ID | verdict | issues | suggested prompt fixes |", "|---|---|---|---|"]
    for c in cases:
        img = RESULTS / c["qa_image"]
        if not img.exists():
            continue
        checks = (c.get("checks") or []) + GENERIC
        text = (f"Case {c.get('id')} ({c.get('mode')}, {c.get('kind')}).\nPrompt used:\n{(c.get('prompt_full') or '')[:1500]}\n\n"
                "Checklist:\n" + "\n".join(f"- {x}" for x in checks))
        try:
            res = parse(call(img, text))
        except RuntimeError as e:
            res = {"verdict": "unsure", "issues": [str(e)[:120]], "fixes": [], "checklist": {}}
        res["id"], res["name"] = c.get("id"), c.get("name")
        out.append(res)
        esc = lambda xs: "; ".join(str(x) for x in xs)[:240].replace("|", "/")
        lines.append(f"| {c.get('id')} | {res.get('verdict')} | {esc(res.get('issues', []))} | {esc(res.get('fixes', []))} |")
        print(f"qa_review: {c.get('id')} -> {res.get('verdict')}", flush=True)
    (RESULTS / "qa_review.json").write_text(json.dumps(out, indent=1))
    (RESULTS / "qa_review.md").write_text("\n".join(lines) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())