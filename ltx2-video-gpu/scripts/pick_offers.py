#!/usr/bin/env python3
"""Rank Vast.ai offers by the estimated cost of ONE daily run; keep the best N.

run cost = GPU $/hr x (boot + weights download + app start + render hours)
         + bandwidth $ for the weights download

The server-side query already enforces VRAM, CUDA, arch, disk, reliability and
bandwidth price. This script adds what a query can't express: time-to-ready and
total cost per run.

In : /tmp/offers.json       (vastai search offers --raw)
Env: WEIGHTS_GB RENDER_HOURS EFF MAX_READY_MIN MAX_DPH BOOT_S APP_S REF_DLPERF TOP_N
Out: /tmp/candidates.json   (ranked, one per machine)
     markdown table on stdout and appended to $GITHUB_STEP_SUMMARY
Exit 1 if nothing passes the gates.
"""
import json
import os
import sys
from collections import Counter

OFFERS = "/tmp/offers.json"
OUT = "/tmp/candidates.json"
INF = float("inf")


def num(name, default):
    return float(os.environ.get(name) or default)


WEIGHTS_GB = num("WEIGHTS_GB", 67)
RENDER_H = num("RENDER_HOURS", 2)
# Achieved / advertised throughput. Recalibrate from the first real run.
EFF = num("EFF", 0.7)
MAX_READY_MIN = num("MAX_READY_MIN", 15)
MAX_DPH = num("MAX_DPH", 0.60)
BOOT_S = num("BOOT_S", 120)
APP_S = num("APP_S", 90)
# DLPerf is only a proxy for render speed until a real render is timed.
REF_DLPERF = num("REF_DLPERF", 98.5)
TOP_N = int(num("TOP_N", 3))


def bw_per_gb(o):
    raw = o.get("inet_down_cost") or 0.0
    # Queries take $/GB but the raw JSON unit is unconfirmed (looks like $/MiB),
    # so tiny values are rescaled. The table prints raw and used values to check.
    return raw * 1024 if raw < 1e-4 else raw


def fmt(x, spec):
    return "inf" if x == INF else format(x, spec)


def evaluate(o):
    down = o.get("inet_down") or 0
    dlperf = o.get("dlperf") or 0
    dph = o["dph_total"]
    reasons = []
    if not down:
        reasons.append("no inet_down")
    if not dlperf:
        reasons.append("no dlperf")

    dl_s = WEIGHTS_GB * 8000 / (down * EFF) if down else INF
    ready_min = (BOOT_S + dl_s + APP_S) / 60
    render_h = RENDER_H * REF_DLPERF / dlperf if dlperf else INF
    bw = WEIGHTS_GB * bw_per_gb(o)
    cost = dph * (ready_min / 60 + render_h) + bw

    if ready_min > MAX_READY_MIN:
        reasons.append(f"ready {fmt(ready_min, '.0f')}m > {MAX_READY_MIN:.0f}m")
    if dph > MAX_DPH:
        reasons.append(f"${dph:.3f}/hr > ${MAX_DPH:.2f}")
    return {"o": o, "ready_min": ready_min, "cost": cost, "reasons": reasons}


def table(rows):
    lines = [
        "| # | offer | GPU | where | $/hr | down Mbps | bw $/GB (raw>used) | ready min | run $ | dlperf | rel |",
        "|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, e in enumerate(rows, 1):
        o = e["o"]
        rel = o.get("reliability2") or o.get("reliability") or 0
        lines.append(
            f"| {i} | {o['id']} | {o.get('gpu_name')} | {o.get('geolocation')} | {o['dph_total']:.3f} "
            f"| {o.get('inet_down')} | {(o.get('inet_down_cost') or 0):.3g}>{bw_per_gb(o):.4f} "
            f"| {fmt(e['ready_min'], '.1f')} | {fmt(e['cost'], '.2f')} | {o.get('dlperf')} | {rel:.3f} |"
        )
    return "\n".join(lines)


def main():
    with open(OFFERS) as f:
        offers = json.load(f)
    evals = [evaluate(o) for o in offers]
    ok = sorted((e for e in evals if not e["reasons"]), key=lambda e: (e["cost"], e["ready_min"]))

    # One candidate per machine: retrying a host that just failed wastes time.
    seen, picks = set(), []
    for e in ok:
        m = e["o"].get("machine_id")
        if m in seen:
            continue
        seen.add(m)
        picks.append(e)

    header = (
        f"### Offer ranking: {len(offers)} offers, {len(ok)} pass gates "
        f"(weights {WEIGHTS_GB:.0f}GB, eff {EFF}, ready <= {MAX_READY_MIN:.0f}m, <= ${MAX_DPH:.2f}/hr, render {RENDER_H}h)\n"
    )

    if not picks:
        why = Counter(r.split(" ")[0] for e in evals for r in e["reasons"])
        nearest = sorted(evals, key=lambda e: e["cost"])[:5]
        out = header + f"\n**No offer passes.** Rejections by gate: {dict(why)}\n\nNearest misses:\n\n" + table(nearest)
        out += "\n\n" + "\n".join(f"- {e['o']['id']}: {', '.join(e['reasons'])}" for e in nearest)
        print(out)
        _summary(out)
        sys.exit(1)

    out = header + "\n" + table(picks[:10])
    print(out)
    _summary(out)

    top = [
        {
            "id": e["o"]["id"],
            "machine_id": e["o"].get("machine_id"),
            "gpu_name": e["o"].get("gpu_name"),
            "location": e["o"].get("geolocation") or "unknown",
            "dph_total": round(e["o"]["dph_total"], 4),
            "inet_down": e["o"].get("inet_down"),
            "ready_min": round(e["ready_min"], 1),
            "est_run_usd": round(e["cost"], 2),
        }
        for e in picks[:TOP_N]
    ]
    with open(OUT, "w") as f:
        json.dump(top, f)


def _summary(text):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(text + "\n\n")


if __name__ == "__main__":
    main()
