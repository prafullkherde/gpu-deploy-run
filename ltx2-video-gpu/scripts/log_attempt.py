#!/usr/bin/env python3
"""Turn one workflow run into learnable rows.

attempts.csv  one row per BOX tried (host memory: pick_offers.py reads it)
runs.csv      one row per RUN (money + outcome: credit before/after)

The first 12 attempts.csv columns are unchanged; new columns are appended, and an old-format file
is migrated in place (old rows padded), so history is never lost.

In : $NEW_ATTEMPTS  rejected boxes, written by the rent loop (12 legacy columns + est_ready_min)
     $RUN_STATS     KEY=VALUE lines for the box that was accepted (may be empty if none was)
     results/results.json, results/deploy.log   (optional)
"""
import csv
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

COLUMNS = [
    "timestamp", "machine_id", "offer_id", "gpu", "dph", "adv_down_mbps", "boot_s", "probe_mbps",
    "dl_mbps", "ready_min", "data_gb", "result",
    "est_ready_min", "own_weights_mb", "render_status", "render_s", "render_s_per_video_s",
    "peak_vram_mb", "fail_note",
]
RUN_COLUMNS = ["timestamp", "run_id", "credit_before", "credit_after", "spent_usd", "attempts",
               "winner_machine", "time_to_up_min", "result"]
PASS_STATES = {"SUCCESS", "SKIPPED"}


def env_file(path):
    out = {}
    p = Path(path)
    if p.exists():
        for line in p.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip()
    return out


def read_csv(path, columns):
    """Return rows as dicts keyed by `columns`, whatever header the file currently has."""
    p = Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return []
    with open(p, newline="") as f:
        return [{c: (r.get(c) or "") for c in columns} for r in csv.DictReader(f)]


def write_csv(path, columns, rows):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def fnum(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def rejected_boxes(path):
    p = Path(path)
    if not p.exists():
        return []
    rows = []
    for vals in csv.reader(p.read_text().splitlines()):
        if vals:
            vals = (vals + [""] * len(COLUMNS))[:len(COLUMNS)]
            rows.append(dict(zip(COLUMNS, vals)))
    return rows


def render_facts(results_path):
    p = Path(results_path)
    if not p.exists():
        return {}
    data = json.loads(p.read_text())
    cases = {c["name"]: c for c in data.get("cases", [])}
    states = {c["status"] for c in cases.values()}
    ran = any(c["status"] == "SUCCESS" for c in cases.values())
    status = "ok" if ran and states <= PASS_STATES else ("skipped" if states <= {"SKIPPED"} else "failed")
    final = cases.get("video_30s") or {}
    ratio = final.get("render_s_per_video_s") or (cases.get("video_short") or {}).get("render_s_per_video_s")
    peaks = [c["peak_vram_mb"] for c in cases.values() if c.get("peak_vram_mb")]
    return {
        "render_status": status,
        "render_s": final.get("elapsed_s", ""),
        "render_s_per_video_s": ratio or "",
        "peak_vram_mb": max(peaks) if peaks else "",
        "own_weights_mb": data.get("env", {}).get("wan2gp_fetched_own_weights_mb", ""),
    }


def fail_note(log_path):
    """Last FAIL / Traceback / error line of deploy.log: the one-line reason a box was bad."""
    p = Path(log_path)
    if not p.exists():
        return ""
    hits = [ln for ln in p.read_text(errors="replace").splitlines()
            if re.search(r"^FAIL:|Traceback|Error|error:", ln)]
    return re.sub(r"[,\r\n]+", " ", hits[-1])[:140] if hits else ""


def main():
    attempts_file = os.environ["ATTEMPTS_FILE"]
    runs_file = os.environ.get("RUNS_FILE", str(Path(attempts_file).with_name("runs.csv")))
    stats = env_file(os.environ.get("RUN_STATS", "/tmp/run_stats.env"))
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    rows = read_csv(attempts_file, COLUMNS)
    rejected = rejected_boxes(os.environ.get("NEW_ATTEMPTS", "/tmp/attempts_new.csv"))
    rows += rejected

    healthy = bool(stats.get("T_HEALTHY"))
    ready = ""
    if stats.get("OFFER_ID"):
        if healthy:
            ready = (int(stats["T_HEALTHY"]) - int(stats["T_CREATE"])) // 60
        data_gb = int(stats.get("DATA_BYTES") or 0) // 1_000_000_000
        r = render_facts(os.environ.get("RESULTS_JSON", "results/results.json"))
        rows.append({
            "timestamp": now, "machine_id": stats.get("MACHINE_ID"), "offer_id": stats.get("OFFER_ID"),
            "gpu": stats.get("GPU"), "dph": stats.get("DPH"), "adv_down_mbps": stats.get("ADV_DOWN"),
            "boot_s": stats.get("BOOT_OBS"), "probe_mbps": stats.get("PROBE_MBPS"),
            "dl_mbps": stats.get("DL_MBPS", ""), "ready_min": ready, "data_gb": data_gb,
            "result": "ok" if healthy else "deploy_failed",
            "est_ready_min": stats.get("EST_READY_MIN", ""),
            "fail_note": "" if healthy else fail_note(os.environ.get("DEPLOY_LOG", "results/deploy.log")),
            **{k: v for k, v in r.items() if v != ""},
        })
    write_csv(attempts_file, COLUMNS, rows)

    before, after = fnum(stats.get("CREDIT_BEFORE")), fnum(os.environ.get("CREDIT_AFTER") or stats.get("CREDIT_AFTER"))
    spent = round(before - after, 4) if before is not None and after is not None else ""
    run_rows = read_csv(runs_file, RUN_COLUMNS)
    run_rows.append({
        "timestamp": now, "run_id": os.environ.get("RUN_ID", ""),
        "credit_before": "" if before is None else before, "credit_after": "" if after is None else after,
        "spent_usd": spent, "attempts": len(rejected) + (1 if stats.get("OFFER_ID") else 0),
        "winner_machine": stats.get("MACHINE_ID", "") if healthy else "",
        "time_to_up_min": ready, "result": "ok" if healthy else ("deploy_failed" if stats.get("OFFER_ID") else "no_box"),
    })
    write_csv(runs_file, RUN_COLUMNS, run_rows)
    print(f"attempts.csv: {len(rows)} rows ({len(rejected)} rejected + {1 if stats.get('OFFER_ID') else 0} accepted this run); "
          f"runs.csv: spent {spent or 'unknown'}")


if __name__ == "__main__":
    main()
