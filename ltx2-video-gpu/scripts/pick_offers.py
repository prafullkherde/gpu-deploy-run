#!/usr/bin/env python3
"""Rank Vast.ai offers by the estimated cost of ONE daily run; keep the best N.

run cost = GPU $/hr x (boot + weights download + app start + render hours)
         + bandwidth $ for the weights download

Advertised bandwidth is a claim, not a measurement. The workflow measures the
real thing on every rented box and appends it to attempts.csv, and this script
learns from that file:
  - per machine : the last measured/advertised ratio replaces the default EFF
  - globally    : median ratio and median boot time replace the defaults once
                  >= 3 samples exist
  - cooldown    : a machine whose latest attempt failed within COOLDOWN_DAYS is skipped

In : /tmp/offers.json            (vastai search offers --raw)
     $ATTEMPTS_FILE              (optional, written by the workflow)
Env: WEIGHTS_GB RENDER_HOURS EFF MAX_READY_MIN MAX_DPH BOOT_S APP_S REF_DLPERF
     TOP_N COOLDOWN_DAYS ATTEMPTS_FILE
Out: /tmp/candidates.json        (ranked, one per machine)
     markdown table on stdout and appended to $GITHUB_STEP_SUMMARY
Exit 1 if nothing passes the gates.
"""
import csv
import json
import os
import statistics
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

OFFERS = "/tmp/offers.json"
OUT = "/tmp/candidates.json"
INF = float("inf")
BAD_RESULTS = {"never_running", "no_ssh", "slow_probe", "deploy_failed"}


def num(name, default):
    return float(os.environ.get(name) or default)


WEIGHTS_GB = num("WEIGHTS_GB", 67)
RENDER_H = num("RENDER_HOURS", 2)
# Default achieved/advertised ratio, used until history has enough samples.
EFF = num("EFF", 0.7)
MAX_READY_MIN = num("MAX_READY_MIN", 15)
MAX_DPH = num("MAX_DPH", 0.60)
BOOT_S = num("BOOT_S", 120)
APP_S = num("APP_S", 90)
# DLPerf is only a proxy for render speed until a real render is timed.
REF_DLPERF = num("REF_DLPERF", 98.5)
TOP_N = int(num("TOP_N", 5))
COOLDOWN_DAYS = num("COOLDOWN_DAYS", 7)


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def clamp(x):
    return min(1.0, max(0.02, x))


def measured_ratio(row):
    adv = fnum(row.get("adv_down_mbps"))
    real = fnum(row.get("dl_mbps")) or fnum(row.get("probe_mbps"))
    return clamp(real / adv) if adv > 0 and real > 0 else None


def load_history():
    path = os.environ.get("ATTEMPTS_FILE", "")
    if not path or not os.path.exists(path):
        return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))


def calibrate(rows):
    ratios = [r for r in (measured_ratio(x) for x in rows) if r is not None]
    boots = [fnum(x.get("boot_s")) for x in rows if fnum(x.get("boot_s")) > 0]
    eff = statistics.median(ratios) if len(ratios) >= 3 else EFF
    boot = statistics.median(boots) if len(boots) >= 3 else BOOT_S
    last = {}
    for r in rows:  # file order is chronological; later rows win
        last[str(r.get("machine_id"))] = r
    return eff, boot, last, len(ratios), len(boots)


def recent_failure(row):
    if row.get("result") not in BAD_RESULTS:
        return None
    try:
        ts = datetime.strptime(row["timestamp"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (KeyError, ValueError):
        return None
    if datetime.now(timezone.utc) - ts <= timedelta(days=COOLDOWN_DAYS):
        return row["result"]
    return None


def fmt(x, spec):
    return "inf" if x == INF else format(x, spec)


def evaluate(o, eff_default, boot_s, last):
    down = o.get("inet_down") or 0
    dlperf = o.get("dlperf") or 0
    dph = o["dph_total"]
    prev = last.get(str(o.get("machine_id")))
    eff = (measured_ratio(prev) if prev else None) or eff_default

    reasons = []
    if not down:
        reasons.append("no inet_down")
    if not dlperf:
        reasons.append("no dlperf")
    failure = recent_failure(prev) if prev else None
    if failure:
        reasons.append(f"failed recently ({failure})")

    dl_s = WEIGHTS_GB * 8000 / (down * eff) if down else INF
    ready_min = (boot_s + dl_s + APP_S) / 60
    render_h = RENDER_H * REF_DLPERF / dlperf if dlperf else INF
    # inet_down_cost is $/GB in the raw JSON (every row seen had raw == $/GB).
    bw = WEIGHTS_GB * (o.get("inet_down_cost") or 0.0)
    cost = dph * (ready_min / 60 + render_h) + bw

    if ready_min > MAX_READY_MIN:
        reasons.append(f"ready {fmt(ready_min, '.0f')}m > {MAX_READY_MIN:.0f}m")
    if dph > MAX_DPH:
        reasons.append(f"${dph:.3f}/hr > ${MAX_DPH:.2f}")
    return {
        "o": o, "ready_min": ready_min, "cost": cost, "reasons": reasons,
        "eff": eff, "seen": prev.get("result") if prev else "-",
    }


def table(rows):
    lines = [
        "| # | offer | GPU | where | $/hr | down Mbps | eff | history | bw $/GB | ready min | run $ | dlperf | rel |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for i, e in enumerate(rows, 1):
        o = e["o"]
        rel = o.get("reliability2") or o.get("reliability") or 0
        lines.append(
            f"| {i} | {o['id']} | {o.get('gpu_name')} | {o.get('geolocation')} | {o['dph_total']:.3f} "
            f"| {o.get('inet_down')} | {e['eff']:.2f} | {e['seen']} | {(o.get('inet_down_cost') or 0):.4f} "
            f"| {fmt(e['ready_min'], '.1f')} | {fmt(e['cost'], '.2f')} | {o.get('dlperf')} | {rel:.3f} |"
        )
    return "\n".join(lines)


def main():
    with open(OFFERS) as f:
        offers = json.load(f)

    eff, boot_s, last, n_ratio, n_boot = calibrate(load_history())
    evals = [evaluate(o, eff, boot_s, last) for o in offers]
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
        f"(weights {WEIGHTS_GB:.0f}GB, ready <= {MAX_READY_MIN:.0f}m, <= ${MAX_DPH:.2f}/hr, render {RENDER_H}h)\n"
        f"Calibration: eff {eff:.2f} from {n_ratio} measured run(s) (default {EFF}), "
        f"boot {boot_s:.0f}s from {n_boot} run(s) (default {BOOT_S:.0f}s)\n"
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
