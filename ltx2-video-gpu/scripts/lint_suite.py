#!/usr/bin/env python3
"""Free check of render_suite/cases.json, run on the GitHub runner BEFORE any GPU is rented.

Besides syntax it enforces the suite rules you set:
  * at most 10 image and 10 video DELIVERABLES in the smoke+standard tiers (sub-clips marked "internal" do not count)
  * every deliverable video is 10 to 15 s (frames at 24 fps; a stitch = sum of its parts)
  * every case has a unique id and a name that starts with it; image-to-video points at an EARLIER, same-or-lower-tier image case
"""
import json
import sys
from collections import Counter
from pathlib import Path

TIERS = {"smoke": 0, "standard": 1, "full": 2}
FPS, MIN_S, MAX_S, MAX_IMAGES, MAX_VIDEOS = 24, 10.0, 15.0, 10, 10
path = Path(sys.argv[1] if len(sys.argv) > 1 else "ltx2-video-gpu/render_suite/cases.json")
errors = []


def err(msg):
    errors.append(msg)
    print(f"::error file={path}::{msg}")


try:
    cases = json.loads(path.read_text())["cases"]
except (OSError, json.JSONDecodeError, KeyError) as e:
    print(f"::error file={path}::cannot read manifest: {e}")
    sys.exit(1)

by_name, ids, seconds = {}, set(), {}
for i, c in enumerate(cases):
    n = c.get("name", f"#{i}")
    kind = c.get("kind")
    if n in by_name:
        err(f"{n}: duplicate name")
    for k in ("id", "name", "kind", "tier"):
        if k not in c:
            err(f"{n}: missing '{k}'")
    if c.get("id") in ids:
        err(f"{n}: duplicate id {c.get('id')}")
    ids.add(c.get("id"))
    if c.get("id") and not str(n).startswith(str(c["id"])):
        err(f"{n}: name must start with its id '{c['id']}'")
    if kind not in ("image", "video", "stitch"):
        err(f"{n}: kind must be image, video or stitch")
    if c.get("tier") not in TIERS:
        err(f"{n}: tier must be one of {list(TIERS)}")
    s = c.get("settings", {})
    if kind in ("image", "video"):
        if not s.get("prompt"):
            err(f"{n}: settings.prompt is empty")
        if not (s.get("model_type") or c.get("model_candidates")):
            err(f"{n}: needs settings.model_type or model_candidates")
        res = s.get("resolution", "")
        if res and (res.count("x") != 1 or not all(p.isdigit() for p in res.split("x"))):
            err(f"{n}: resolution '{res}' is not WIDTHxHEIGHT")
        if res and any(int(p) % 32 for p in res.split("x") if p.isdigit()) and kind == "video":
            err(f"{n}: video resolution {res} should be a multiple of 32")
    if kind == "video":
        fl = s.get("video_length")
        if not isinstance(fl, int):
            err(f"{n}: video needs integer settings.video_length (frames)")
        else:
            seconds[n] = (fl - 1) / FPS
            if (fl - 1) % 8:
                err(f"{n}: video_length {fl} is not 8n+1 (LTX frame rule)")
    dep = c.get("needs")
    if dep:
        d = by_name.get(dep)
        if d is None:
            err(f"{n}: needs '{dep}', which must appear EARLIER in the manifest")
        else:
            if d.get("kind") != "image":
                err(f"{n}: needs '{dep}', which is not an image case")
            if TIERS.get(d.get("tier"), 9) > TIERS.get(c.get("tier"), 0):
                err(f"{n}: needs '{dep}' from a HIGHER tier ({d.get('tier')} > {c.get('tier')})")
    if kind == "stitch":
        parts = c.get("parts", [])
        if len(parts) < 2:
            err(f"{n}: stitch needs at least 2 parts")
        for p in parts:
            if p not in by_name:
                err(f"{n}: part '{p}' must appear EARLIER in the manifest")
            elif TIERS.get(by_name[p].get("tier"), 9) > TIERS.get(c.get("tier"), 0):
                err(f"{n}: part '{p}' is from a higher tier")
        seconds[n] = sum(seconds.get(p, 0) for p in parts)
    by_name[n] = c

print("deliverables per tier (internal sub-clips not counted):")
for t, rank in TIERS.items():
    inc = [c for c in cases if TIERS.get(c.get("tier"), 9) <= rank]
    imgs = [c for c in inc if c.get("kind") == "image"]
    vids = [c for c in inc if c.get("kind") in ("video", "stitch") and not c.get("internal")]
    gens = [c for c in inc if c.get("kind") in ("image", "video")]
    print(f"  {t:8s}: {len(imgs):2d} images, {len(vids):2d} videos, {len(gens)} generations, {sum(1 for c in inc if c.get('kind') == 'stitch')} stitch")
    if rank <= TIERS["standard"]:
        if len(imgs) > MAX_IMAGES:
            err(f"tier {t}: {len(imgs)} images > cap {MAX_IMAGES}")
        if len(vids) > MAX_VIDEOS:
            err(f"tier {t}: {len(vids)} videos > cap {MAX_VIDEOS}")
        for c in vids:
            sec = seconds.get(c["name"], 0)
            if rank == TIERS["standard"] and not (MIN_S <= sec <= MAX_S):
                err(f"{c['name']}: {sec:.1f} s is outside {MIN_S:g}-{MAX_S:g} s")
print(f"{len(cases)} cases;", dict(Counter(c.get("mode") for c in cases)))
sys.exit(1 if errors else 0)