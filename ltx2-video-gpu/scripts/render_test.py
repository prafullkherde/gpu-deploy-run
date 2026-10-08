#!/usr/bin/env python3
"""
render_test.py -- runs ON the rented box after the health check returns HTTP 200.

Reads render_suite/cases.json, runs every case of the chosen TIER (smoke < standard < lab) one at a time with
`wgp.py --process <settings.json> --output-dir <dir>`, and writes into ~/results/:
  results.json   everything, machine-readable (timings, GPU, files, quality flags, failure root cause)
  report.md      the same as tables, for the Actions run summary
  index.html     tables + thumbnails + prompts, open it from the downloaded artifact
  <case>.log, <case>_gpu.csv, <case>/settings.json (exactly what ran), <case>/out/*, thumbs/*

Environment: TIER, SUITE_DIR, OUT_DIR, DPH ($/hr, for cost), SUITE_BUDGET_MIN, COLD_BONUS_S,
             KEEP_PARTIALS=1 keeps sliding-window partials, DELIVERY=0 skips the re-encode.
Stdlib only on purpose: nothing in the rented image can break this script.
"""
import copy
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path.home()
TIER_ORDER = {"smoke": 0, "standard": 1, "lab": 2}
TIER = os.environ.get("TIER", "smoke")
SUITE_DIR = Path(os.environ.get("SUITE_DIR", HOME / "render_suite"))
OUT_DIR = Path(os.environ.get("OUT_DIR", HOME / "results"))
DPH = float(os.environ.get("DPH") or 0)
BUDGET_MIN = float(os.environ.get("SUITE_BUDGET_MIN") or {"smoke": 40, "standard": 120, "lab": 220}.get(TIER, 120))
COLD_BONUS_S = int(os.environ.get("COLD_BONUS_S") or 1500)  # extra timeout for the first case of each model (its download)
KEEP_PARTIALS = os.environ.get("KEEP_PARTIALS") == "1"
DELIVERY = os.environ.get("DELIVERY", "1") != "0"
GROUPS = os.environ.get("GH_GROUPS", "1") == "1"
MEDIA_EXT = {".mp4", ".mkv", ".webm", ".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXT = {".mp4", ".mkv", ".webm"}
HEARTBEAT_S = 30
SKIP_MODEL_ON = {"triton_kernel", "download"}  # failure kinds that will repeat for every case of the same model
results = []


def load_env():
    """bootstrap.sh wrote `export K=V` lines to ~/.ltx2_env; reuse them."""
    env = dict(os.environ)
    path = HOME / ".ltx2_env"
    if path.exists():
        for line in path.read_text().splitlines():
            if line.startswith("export ") and "=" in line:
                k, v = line[7:].split("=", 1)
                env.setdefault(k.strip(), v.strip())
    env["PYTHONUNBUFFERED"] = "1"
    return env


ENV = load_env()
WAN2GP_DIR = Path(ENV.get("WAN2GP_DIR", "/opt/workspace-internal/Wan2GP"))
PYTHON_BIN = ENV.get("PYTHON_BIN", "python3")
APP_PATTERN = "wgp[.]py"  # bracket trick: the pattern cannot match pkill/pgrep's own command line


def sh(cmd, timeout=30):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except subprocess.TimeoutExpired:
        return "TIMEOUT"


def tree_bytes(path):
    total = 0
    for root, _, files in os.walk(path):
        for name in files:
            try:
                total += os.path.getsize(os.path.join(root, name))
            except OSError:
                pass  # file vanished mid-walk (partial download renamed): skip, not fatal
    return total


class GpuSampler(threading.Thread):
    QUERY = "nvidia-smi --query-gpu=memory.used,utilization.gpu,temperature.gpu --format=csv,noheader,nounits"

    def __init__(self):
        super().__init__(daemon=True)
        self.stop_evt = threading.Event()
        self.samples = []

    def run(self):
        while not self.stop_evt.wait(2):
            try:
                mem, util, temp = (float(x) for x in sh(self.QUERY, 5).splitlines()[0].split(","))
                self.samples.append((mem, util, temp))
            except (ValueError, IndexError):
                continue  # transient nvidia-smi hiccup

    def latest(self):
        return self.samples[-1] if self.samples else None

    def write_csv(self, path):
        with open(path, "w") as f:
            f.write("t_s,vram_mb,util_pct,temp_c\n")
            for i, (m, u, t) in enumerate(self.samples):
                f.write(f"{i * 2},{int(m)},{int(u)},{int(t)}\n")

    def summary(self):
        if not self.samples:
            return {"peak_vram_mb": None, "mean_util_pct": None, "max_temp_c": None}
        return {
            "peak_vram_mb": int(max(s[0] for s in self.samples)),
            "mean_util_pct": round(sum(s[1] for s in self.samples) / len(self.samples)),
            "max_temp_c": int(max(s[2] for s in self.samples)),
        }


# ---------------------------------------------------------------- media facts and checks
def probe_media(path):
    raw = sh(f'ffprobe -v error -print_format json -show_streams -show_format "{path}"', 30)
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return {"file": path.name, "size_bytes": path.stat().st_size, "probe": "ffprobe_failed"}
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    num, _, den = (v.get("r_frame_rate") or "0/1").partition("/")
    duration = round(float(info.get("format", {}).get("duration") or 0), 2)
    size = path.stat().st_size
    return {
        "file": path.name,
        "size_bytes": size,
        "width": v.get("width"), "height": v.get("height"),
        "fps": round(float(num) / float(den), 2) if float(den or 0) else None,
        "duration_s": duration,
        # total bitrate (video + audio): why a clip is "20 MB" or "40 MB", not a quality measure
        "bitrate_mbps": round(size * 8 / duration / 1e6, 2) if duration > 1 else None,
        "has_audio": any(s.get("codec_type") == "audio" for s in info.get("streams", [])),
        "codec": v.get("codec_name"),
    }


def quality_checks(path, has_audio):
    """Decode the whole file once; sum black, frozen and silent seconds. ffprobe reads headers only, so a file of the
    right size and length that is all black, frozen or silent would pass probe_media."""
    cmd = ["ffmpeg", "-v", "info", "-i", str(path), "-vf", "blackdetect=d=1:pix_th=0.10,freezedetect=n=-60dB:d=2"]
    if has_audio:
        cmd += ["-af", "silencedetect=n=-50dB:d=1"]
    cmd += ["-f", "null", "-"]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        return {"quality": "ffmpeg_missing"}
    except subprocess.TimeoutExpired:
        return {"quality": "timeout"}
    total = lambda pat: round(sum(float(x) for x in re.findall(pat, p.stderr)), 2)
    out = {"decode_ok": p.returncode == 0, "black_s": total(r"black_duration:\s*([\d.]+)"),
           "frozen_s": total(r"freeze_duration:\s*([\d.]+)")}
    if has_audio:
        out["silent_s"] = total(r"silence_duration:\s*([\d.]+)")
    return out


def make_thumb(media, kind, thumbs_dir, name):
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    if kind == "image":
        dst = thumbs_dir / f"{name}.jpg"
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(media), "-vf", "scale=640:-2", "-frames:v", "1", str(dst)]
    else:
        dst = thumbs_dir / f"{name}_strip.jpg"
        rate = 6 / max(media_duration(media), 1)  # 6 frames spread over the clip: look for seams and drift
        cmd = ["ffmpeg", "-y", "-v", "error", "-i", str(media), "-vf", f"fps={rate:.5f},scale=320:-2,tile=6x1",
               "-frames:v", "1", "-q:v", "4", str(dst)]
    try:
        subprocess.run(cmd, capture_output=True, timeout=180)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return None
    return f"thumbs/{dst.name}" if dst.exists() else None


def media_duration(path):
    try:
        return float(sh(f'ffprobe -v error -show_entries format=duration -of csv=p=0 "{path}"', 30) or 0)
    except ValueError:
        return 0.0


def make_delivery(src, dst, label=None):
    """Re-encode to a sane bitrate (a delivery copy) and, if drawtext exists, burn the case ID into the corner so a file is
    identifiable even after it is renamed or shared. Measures what the 'high bitrate' output costs us."""
    t0 = time.time()
    base = ["ffmpeg", "-y", "-v", "error", "-i", str(src)]
    tail = ["-c:v", "libx264", "-crf", "21", "-preset", "medium", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
            "-movflags", "+faststart", str(dst)]
    attempts = []
    if label:
        safe = re.sub(r"[^A-Za-z0-9_ .-]", "", label)
        attempts.append(base + ["-vf", f"drawtext=text='{safe}':x=14:y=14:fontsize=h/36:fontcolor=white:box=1:boxcolor=black@0.55:boxborderw=6"] + tail)
    attempts.append(base + tail)
    p, labelled = None, False
    for i, cmd in enumerate(attempts):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return {"delivery": f"failed: {type(e).__name__}"}
        if p.returncode == 0 and dst.exists():
            labelled = bool(label) and i == 0
            break
    if p is None or p.returncode != 0 or not dst.exists():
        return {"delivery": f"failed: {(p.stderr if p else '').strip()[:120]}"}
    dur = media_duration(dst)
    size = dst.stat().st_size
    return {"delivery_file": dst.name, "delivery_mb": round(size / 1e6, 1), "delivery_s": round(time.time() - t0, 1),
            "delivery_mbps": round(size * 8 / dur / 1e6, 2) if dur > 1 else None, "delivery_labelled": labelled}


def make_seam_strip(media, seams, thumbs_dir, name):
    """For a stitched video: the frame just before and just after every cut, side by side."""
    thumbs_dir.mkdir(parents=True, exist_ok=True)
    frames = []
    for i, t in enumerate(seams):
        for j, ts in enumerate((max(t - 0.05, 0), t + 0.05)):
            f = thumbs_dir / f"_{name}_{i}_{j}.jpg"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{ts:.3f}", "-i", str(media), "-frames:v", "1",
                            "-vf", "scale=240:-2", str(f)], capture_output=True, timeout=120)
            if f.exists():
                frames.append(f)
    if not frames:
        return None
    dst = thumbs_dir / f"{name}_seams.jpg"
    cmd = ["ffmpeg", "-y", "-v", "error"] + sum([["-i", str(f)] for f in frames], []) + \
          ["-filter_complex", f"hstack=inputs={len(frames)}", str(dst)]
    subprocess.run(cmd, capture_output=True, timeout=120)
    for f in frames:
        f.unlink(missing_ok=True)
    return f"thumbs/{dst.name}" if dst.exists() else None


def frame_stats(path):
    """Brightness / contrast / sharpness proxies of ONE image via ffmpeg (stdlib only on the box).
    luma = mean brightness 0-255; low/high = 10th/90th percentile (YLOW/YHIGH); edges = mean Sobel magnitude (higher = more fine detail)."""
    out = {}
    for key, vf in (("luma", "signalstats,metadata=print:file=-"), ("edges", "format=gray,sobel,signalstats,metadata=print:file=-")):
        try:
            p = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", vf, "-frames:v", "1", "-f", "null", "-"],
                               capture_output=True, text=True, timeout=120)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return {}
        out[key] = {k: float(v) for k, v in re.findall(r"lavfi\.signalstats\.(\w+)=([\d.]+)", p.stdout + p.stderr)}
    L, E = out.get("luma", {}), out.get("edges", {})
    if "YAVG" not in L or "YAVG" not in E:
        return {}
    return {"luma": round(L["YAVG"]), "low": round(L["YLOW"]), "high": round(L["YHIGH"]), "sat": round(L.get("SATAVG", 0)),
            "edges": round(E["YAVG"], 1)}


def quality_metrics(path, kind, duration):
    """Image: one measurement. Video: three frames (20/50/80 %) averaged. Flags are heuristics, not verdicts."""
    if kind == "image":
        m = frame_stats(path)
    else:
        tmp = OUT_DIR / "thumbs"
        tmp.mkdir(parents=True, exist_ok=True)
        rows = []
        for i, frac in enumerate((0.2, 0.5, 0.8)):
            f = tmp / f"_m{i}.jpg"
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{duration * frac:.2f}", "-i", str(path), "-frames:v", "1", str(f)],
                           capture_output=True, timeout=120)
            if f.exists():
                rows.append(frame_stats(f))
                f.unlink()
        rows = [r for r in rows if r]
        m = {k: round(sum(r[k] for r in rows) / len(rows), 1) for k in rows[0]} if rows else {}
    if not m:
        return {}
    flags = []
    if m["luma"] < 70:
        flags.append("DARK")
    if m["high"] - m["low"] < 60:
        flags.append("LOW_CONTRAST")
    m["flags"] = flags
    return m


# ---------------------------------------------------------------- app control, diagnostics
def app_running():
    return subprocess.run(["pgrep", "-f", APP_PATTERN], capture_output=True).returncode == 0


def stop_app():
    subprocess.run(["pkill", "-f", APP_PATTERN])
    for _ in range(30):
        if not app_running():
            break
        time.sleep(2)
    else:
        subprocess.run(["pkill", "-9", "-f", APP_PATTERN])
    for _ in range(30):  # wait until VRAM is actually released
        used = sh("nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits")
        if used.isdigit() and int(used) < 1500:
            return int(used)
        time.sleep(2)
    return None


def failure_summary(log_text):
    """One-line root cause: the final exception line and the innermost Wan2GP frame, not mid-stack noise."""
    lines = [ln.strip() for ln in log_text.replace("\r", "\n").splitlines() if ln.strip()]
    exc = next((ln for ln in reversed(lines) if re.match(r"^[\w.]*(Error|Exception|Exit)\b", ln)), "")
    frames = [ln for ln in lines if ln.startswith('File "') and "/Wan2GP/" in ln]
    where = re.sub(r'^File "[^"]*/Wan2GP/', "", frames[-1]) if frames else ""
    kind = ("triton_kernel" if re.search(r"triton|CompilationError", log_text) else
            "oom" if re.search(r"out of memory|OutOfMemory", log_text, re.I) else
            "download" if re.search(r"HTTPError|ConnectionError|No space left", log_text) else "other")
    return {"kind": kind, "exception": exc[:160], "where": where[:120]}


def model_catalog():
    """stem -> display name, from Wan2GP's defaults/ and finetunes/ JSON files. The stem is the model_type."""
    cat = {}
    for sub in ("defaults", "finetunes"):
        d = WAN2GP_DIR / sub
        if not d.is_dir():
            continue
        for p in d.glob("*.json"):
            try:
                name = (json.loads(p.read_text()).get("model") or {}).get("name", "")
            except (json.JSONDecodeError, OSError, AttributeError):
                name = ""
            cat[p.stem] = name
    return dict(sorted(cat.items()))


def model_defaults(stems):
    """What Wan2GP itself recommends for each model (steps, guidance, loras ...): everything outside the 'model' block."""
    out = {}
    for stem in stems:
        for sub in ("defaults", "finetunes"):
            f = WAN2GP_DIR / sub / f"{stem}.json"
            if f.exists():
                try:
                    d = json.loads(f.read_text())
                except json.JSONDecodeError:
                    continue
                m = d.get("model") or {}
                out[stem] = {"name": m.get("name"), "architecture": m.get("architecture"),
                             "settings": {k: (v[:80] if isinstance(v, str) else v) for k, v in d.items() if k != "model"}}
                break
    return out


def wgp_config_subset():
    """The keys of wgp_config.json that decide memory profile, quantization, attention and output formats."""
    f = WAN2GP_DIR / "wgp_config.json"
    if not f.exists():
        return {"wgp_config.json": "missing"}
    try:
        cfg = json.loads(f.read_text())
    except json.JSONDecodeError:
        return {"wgp_config.json": "invalid json"}
    pat = re.compile(r"profile|quant|attention|codec|output|compile|vae|preload|enhanc|fps|quality|upsampl|sage|tea|mag", re.I)
    return {k: v for k, v in cfg.items() if pat.search(k) and not isinstance(v, (dict, list))}


def model_file_report(model_types):
    """For each model_type: the files Wan2GP's own definition wants and whether they are already in ckpts/."""
    report = {}
    for mt in model_types:
        found = next((c for c in (WAN2GP_DIR / "defaults" / f"{mt}.json", WAN2GP_DIR / "finetunes" / f"{mt}.json") if c.exists()), None)
        if not found:
            report[mt] = {"definition": None}
            continue
        try:
            d = json.loads(found.read_text())
        except json.JSONDecodeError:
            report[mt] = {"definition": str(found), "error": "invalid json"}
            continue
        urls = d.get("URLs") or (d.get("model") or {}).get("URLs") or []
        names = [u.rsplit("/", 1)[-1] for u in urls if isinstance(u, str)]
        report[mt] = {"definition": str(found), "files": names,
                      "in_ckpts": {n: (WAN2GP_DIR / "ckpts" / n).exists() for n in names}}
    return report


def heartbeat(name, start, log_path, sampler, stop_evt):
    """wgp.py writes to a file, not to our stdout; without this the Actions log is silent for the whole render."""
    while not stop_evt.wait(HEARTBEAT_S):
        try:
            tail = log_path.read_bytes()[-2000:].decode(errors="replace")
            last = re.split(r"[\r\n]+", tail.strip())[-1][:160]
        except OSError:
            last = ""
        g = sampler.latest()
        gpu = f"vram={int(g[0])}MB util={int(g[1])}% temp={int(g[2])}C" if g else "gpu=?"
        print(f"[{name}] +{int(time.time() - start)}s | {gpu} | {last}", flush=True)


# ---------------------------------------------------------------- one case
def resolve_model(case, catalog):
    """model_type to use. Order: explicit settings.model_type; then the first regex in model_match that matches a catalog
    entry's 'stem name' text (so a case can say 'LTX-2.5 distilled' without knowing the stem); then model_candidates."""
    s = case["settings"]
    if s.get("model_type"):
        return s["model_type"], catalog.get(s["model_type"], "")
    for pat in case.get("model_match") or []:
        rx = re.compile(pat, re.I)
        hits = [(stem, name) for stem, name in catalog.items() if rx.search(f"{stem} {name}")]
        if hits:
            return hits[0]
    cands = case.get("model_candidates") or []
    present = [c for c in cands if c in catalog]
    if present:
        return present[0], catalog.get(present[0], "")
    return (cands[0], "") if cands and not catalog else (None, "")


def final_media(media, kind):
    if not media:
        return None
    if kind == "video":
        vids = [m for m in media if (m.get("duration_s") or 0) > 1]
        return max(vids, key=lambda m: m["duration_s"]) if vids else None
    return media[0]


def identity(case):
    """Fields that make every result traceable: ID, concept, mode, and which image a video was made from."""
    return {"id": case.get("id"), "concept": case.get("concept"), "mode": case.get("mode"), "origin": case.get("origin"),
            "internal": bool(case.get("internal")), "from_case": case.get("needs"), "note": case.get("note"),
            "sweep": case.get("sweep")}


def finalize_output(case, rec, out_dir, final, settings):
    """Rename the final file to <ID>_<slug><ext> (Wan2GP's own names are long prompt excerpts) and write a sidecar
    <ID>_<slug>.txt holding the full prompt and settings, so prompt-to-output matching never depends on a file name."""
    src = out_dir / final["file"]
    dst = out_dir / (case["name"] + src.suffix.lower())
    if src != dst:
        src.replace(dst)
    final["file"] = dst.name
    side = out_dir.parent / f"{case['name']}.txt"
    side.write_text("\n".join([
        f"ID:        {case.get('id')}", f"concept:   {case.get('concept')}", f"mode:      {case.get('mode')}"
        + (f"  (image-to-video from {case['needs']})" if case.get("needs") else ""),
        f"origin:    {case.get('origin')}", f"model:     {rec.get('model_type')}", f"note:      {case.get('note')}", "",
        "SETTINGS (prompt omitted, see below):", json.dumps({k: v for k, v in settings.items() if k != "prompt"}, indent=1), "",
        "PROMPT:", settings.get("prompt", ""), ""]))
    return dst


def run_stitch(case, finals):
    """Join the final outputs of case['parts'] into one video with ffmpeg (hard cuts) and measure the result."""
    name = case["name"]
    case_dir = OUT_DIR / name
    out = case_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    paths = [finals.get(p) for p in case["parts"]]
    if not all(paths):
        missing = [p for p, f in zip(case["parts"], paths) if not f]
        skipped(case, f"parts without output: {missing}")
        return None
    infos = [probe_media(p) for p in paths]
    audio = all(i.get("has_audio") for i in infos)
    n = len(paths)
    t0 = time.time()
    dst = out / f"{name}.mp4"
    inputs = sum([["-i", str(p)] for p in paths], [])
    v = "".join(f"[{i}:v]" for i in range(n))
    if audio:
        a = "".join(f"[{i}:a]" for i in range(n))
        fc = f"{v}concat=n={n}:v=1:a=0[v];{a}concat=n={n}:v=0:a=1[a]"
        maps = ["-map", "[v]", "-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
    else:
        fc, maps = f"{v}concat=n={n}:v=1:a=0[v]", ["-map", "[v]"]
    cmd = ["ffmpeg", "-y", "-v", "error"] + inputs + ["-filter_complex", fc] + maps + \
          ["-c:v", "libx264", "-crf", "17", "-preset", "medium", "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(dst)]
    print(f"[{name}] STITCH {n} clips ({'with' if audio else 'WITHOUT'} audio): {[p.name for p in paths]}", flush=True)
    p = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = round(time.time() - t0, 1)
    rec = {"name": name, "group": case.get("group"), "kind": "stitch", "tier": case["tier"], **identity(case), "elapsed_s": elapsed,
           "cost_usd": round(elapsed * DPH / 3600, 4) if DPH else None, "parts": case["parts"], "warm": True}
    if p.returncode != 0 or not dst.exists():
        rec.update(status="FAILED", failure={"kind": "ffmpeg", "exception": p.stderr.strip()[:160], "where": "stitch"})
        results.append(rec)
        print(f"[{name}] FAILED stitch: {rec['failure']['exception']}", flush=True)
        return None
    final = probe_media(dst)
    expected = round(sum(i.get("duration_s") or 0 for i in infos), 2)
    seams, acc = [], 0.0
    for i in infos[:-1]:
        acc += i.get("duration_s") or 0
        seams.append(round(acc, 2))
    final.update(quality_checks(dst, final.get("has_audio", False)))
    final["expected_s"], final["duration_ok"] = expected, abs((final.get("duration_s") or 0) - expected) < 0.6
    rec.update(status="SUCCESS", final_output=dst.name, output=final, seams_s=seams, settings=case.get("settings", {}))
    if DELIVERY:
        rec["output"].update(make_delivery(dst, case_dir / "delivery.mp4", label=case["name"][:40]))
    rec["thumb"] = make_thumb(dst, "video", OUT_DIR / "thumbs", name)
    rec["seam_thumb"] = make_seam_strip(dst, seams, OUT_DIR / "thumbs", name)
    (case_dir / f"{name}.txt").write_text(
        f"ID:        {case.get('id')}\nconcept:   {case.get('concept')}\nmode:      stitch of {case['parts']}\nseams at:  {seams} s\n"
        f"duration:  {final.get('duration_s')} s (expected {expected})\nnote:      {case.get('note')}\n")
    results.append(rec)
    print(f"[{name}] SUCCESS stitch in {elapsed}s -> {final.get('duration_s')}s (expected {expected}), seams at {seams}", flush=True)
    return dst


def run_case(case, model_type, settings, timeout_s):
    name, kind = case["name"], case["kind"]
    case_dir = OUT_DIR / name
    out = case_dir / "out"
    out.mkdir(parents=True, exist_ok=True)
    settings_path = case_dir / "settings.json"
    settings_path.write_text(json.dumps(settings, indent=1, ensure_ascii=False))
    log_path = OUT_DIR / f"{name}.log"
    ckpts = WAN2GP_DIR / "ckpts"
    ckpt_before = tree_bytes(ckpts)
    sampler = GpuSampler()
    sampler.start()
    start = time.time()
    status, rc = "FAILED", None
    cmd = [PYTHON_BIN, "-u", "wgp.py", *case.get("cli_args", []), "--process", str(settings_path), "--output-dir", str(out)]
    if GROUPS:
        print(f"::group::[{name}] {case.get('group', '')}", flush=True)
    print(f"[{name}] START model={model_type} ({case.get('_model_name', '')}) timeout={timeout_s}s res={settings.get('resolution')} "
          f"frames={settings.get('video_length', '-')} steps={settings.get('num_inference_steps', 'default')}", flush=True)
    hb_stop = threading.Event()
    log_path.write_text("")
    threading.Thread(target=heartbeat, args=(name, start, log_path, sampler, hb_stop), daemon=True).start()
    with open(log_path, "w") as log:
        proc = subprocess.Popen(cmd, cwd=WAN2GP_DIR, env=ENV, stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            rc = proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)  # kill the group: wgp spawns children
            status = "TIMEOUT"
    elapsed = round(time.time() - start, 1)
    hb_stop.set()
    sampler.stop_evt.set()
    sampler.join(timeout=5)
    sampler.write_csv(OUT_DIR / f"{name}_gpu.csv")

    media = [probe_media(p) for p in sorted(out.rglob("*")) if p.suffix.lower() in MEDIA_EXT and p.stat().st_size > 0]
    final = final_media(media, kind)
    if status != "TIMEOUT":
        status = "SUCCESS" if rc == 0 and final else "FAILED"
    rec = {
        "name": name, "group": case.get("group"), "kind": kind, "model_type": model_type, "model_name": case.get("_model_name"), "tier": case["tier"],
        **identity(case), "status": status, "exit_code": rc, "elapsed_s": elapsed,
        "cost_usd": round(elapsed * DPH / 3600, 4) if DPH else None,
        "settings": {k: (v if k != "prompt" else v[:200]) for k, v in settings.items()},
        "ckpts_downloaded_mb": round((tree_bytes(ckpts) - ckpt_before) / 1e6),
        **sampler.summary(),
    }
    rec["warm"] = rec["ckpts_downloaded_mb"] == 0
    if final:
        rec["final_output"] = final["file"]
        rec["output"] = final
        req_seed = settings.get("seed")
        m = re.search(r"_seed(\d+)_", final["file"])
        if m and req_seed is not None:
            rec["seed_honored"] = str(req_seed) == m.group(1)
        partials = [x for x in media if x["file"] != final["file"] and (x.get("duration_s") or 0) > 1]
        finalize_output(case, rec, out, final, settings)  # after the seed check: the seed lives in Wan2GP's file name
        rec["final_output"] = final["file"]
        if kind == "video":
            rec["output"].update(quality_checks(out / final["file"], final.get("has_audio", False)))
            rec["partial_files"] = len(partials)
            if partials and not KEEP_PARTIALS:
                for x in partials:
                    (out / x["file"]).unlink(missing_ok=True)
            if DELIVERY:
                rec["output"].update(make_delivery(out / final["file"], case_dir / "delivery.mp4", label=case["name"][:40]))
        rec["thumb"] = make_thumb(out / final["file"], kind, OUT_DIR / "thumbs", name)
        rec["metrics"] = quality_metrics(out / final["file"], kind, final.get("duration_s") or 0)
        if status == "SUCCESS" and kind == "video" and rec["warm"] and final["duration_s"] > 1:
            rec["render_s_per_video_s"] = round(elapsed / final["duration_s"], 2)
    if status != "SUCCESS":
        text = log_path.read_text(errors="replace")
        rec["failure"] = failure_summary(text)
        rec["log_tail"] = text[-600:]
    log_text = log_path.read_text(errors="replace")
    rec["phases_seen"] = int("First Phase" in log_text) + int("Second Phase" in log_text) if "Denoising" in log_text else 0
    hits = [ln.strip()[:160] for ln in log_text.splitlines()
            if re.search(r"\b(401|403)\b|gated|Access to model|Repository Not Found", ln)]
    if hits:
        rec["hf_access_errors"] = hits[:5]
        print(f"[{name}] HF ACCESS PROBLEM in log: {hits[0]}", flush=True)
    results.append(rec)
    o = rec.get("output") or {}
    print(f"[{name}] {status} in {elapsed}s ({'warm' if rec['warm'] else 'cold +' + str(rec['ckpts_downloaded_mb']) + ' MB'}) | "
          f"vram_peak={rec['peak_vram_mb']}MB util={rec['mean_util_pct']}% temp={rec['max_temp_c']}C | "
          f"{o.get('width')}x{o.get('height')}{' ' + str(o.get('duration_s')) + 's' if kind == 'video' else ''} "
          f"{round((o.get('size_bytes') or 0) / 1e6, 1)}MB"
          f"{' seed_ok=' + str(rec['seed_honored']) if 'seed_honored' in rec else ''}", flush=True)
    if status != "SUCCESS":
        print(f"[{name}] ROOT CAUSE: {rec['failure']}\n[{name}] LOG TAIL:\n{rec['log_tail']}", flush=True)
    if GROUPS:
        print("::endgroup::", flush=True)
    return rec


def skipped(case, why):
    results.append({"name": case["name"], "group": case.get("group"), "kind": case["kind"], "tier": case["tier"], **identity(case),
                    "status": "SKIPPED", "detail": why})
    print(f"[{case['name']}] SKIPPED -- {why}", flush=True)


# ---------------------------------------------------------------- reports
def write_reports(env_snap, suite_elapsed):
    ok = [r for r in results if r["status"] == "SUCCESS"]
    cost = round(sum(r.get("cost_usd") or 0 for r in results), 3)
    dl = sum(r.get("ckpts_downloaded_mb", 0) for r in results)
    head = [f"### Render suite: tier `{TIER}`, {len(ok)}/{len(results)} succeeded, "
            f"{sum(r['status'] in ('FAILED', 'TIMEOUT') for r in results)} failed, "
            f"{sum(r['status'] == 'SKIPPED' for r in results)} skipped",
            f"GPU {env_snap.get('gpu')} | suite wall time {round(suite_elapsed / 60, 1)} min | GPU-time cost about ${cost} | "
            f"Wan2GP downloaded {round(dl / 1000, 1)} GB itself | Triton patch: {env_snap.get('triton_patch')}", ""]

    def out_text(r):
        o = r.get("output") or {}
        if not o:
            return ""
        return (f"{o.get('width')}x{o.get('height')}{' ' + str(o.get('duration_s')) + 's' if r.get('kind') in ('video', 'stitch') else ''} "
                f"{round((o.get('size_bytes') or 0) / 1e6, 1)}MB")

    rows = ["| ID | case | mode | from | status | time | $ | VRAM MB | output | brightness / contrast / detail | quality | notes |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in results:
        o = r.get("output") or {}
        q = " ".join(filter(None, [
            None if "decode_ok" not in o else ("decode ok" if o["decode_ok"] else "DECODE ERRORS"),
            f"black {o['black_s']}s" if o.get("black_s") else None, f"frozen {o['frozen_s']}s" if o.get("frozen_s") else None,
            f"silent {o['silent_s']}s" if o.get("silent_s") else None,
            None if o.get("duration_ok", True) else f"LENGTH {o.get('duration_s')}s != {o.get('expected_s')}s"]))
        notes = "; ".join(filter(None, [
            r.get("detail"), "sub-clip" if r.get("internal") else None,
            ("cold +%d MB" % r["ckpts_downloaded_mb"]) if r.get("ckpts_downloaded_mb") else None,
            ("%.1f s/video-s" % r["render_s_per_video_s"]) if r.get("render_s_per_video_s") else None,
            None if r.get("seed_honored", True) else "SEED NOT HONORED",
            ("%s: %s" % (r["failure"]["kind"], r["failure"]["exception"][:70])) if r.get("failure") else None,
            ("delivery %.1f MB" % o["delivery_mb"]) if o.get("delivery_mb") else None]))
        mt = r.get("metrics") or {}
        mtxt = (f"luma {mt['luma']} · spread {mt['high'] - mt['low']} · edges {mt['edges']}" + (" " + ",".join(mt["flags"]) if mt.get("flags") else "")) if mt else ""
        rows.append(f"| {r.get('id') or ''} | {r['name']} | {r.get('mode') or ''} | {(r.get('from_case') or '').split('_')[0]} | {r['status']} | "
                    f"{r.get('elapsed_s', '')} | {r.get('cost_usd') or ''} | {r.get('peak_vram_mb') or ''} | {out_text(r)} | {mtxt} | {q} | {notes} |")
    sweeps = {}
    for r in results:
        if r.get("sweep") and r["status"] == "SUCCESS":
            sweeps.setdefault(r["sweep"], []).append(r)
    sweep_md = []
    for sw, rs in sweeps.items():
        sweep_md += ["", f"#### Sweep: {sw} (same prompt and seed; only the config differs; look at them side by side)", "",
                     "| case | model | steps / guidance | time s | luma | edges (detail) | flags |", "|---|---|---|---|---|---|---|"]
        for r in sorted(rs, key=lambda x: -((x.get("metrics") or {}).get("edges") or 0)):
            st, mt = r.get("settings") or {}, r.get("metrics") or {}
            sweep_md.append(f"| {r['name']} | {r.get('model_name') or r.get('model_type')} | {st.get('num_inference_steps', 'default')} / "
                            f"{st.get('guidance_scale', 'default')} | {r.get('elapsed_s')} | {mt.get('luma', '')} | {mt.get('edges', '')} | "
                            f"{','.join(mt.get('flags', []))} |")
    (OUT_DIR / "report.md").write_text("\n".join(head + rows + sweep_md) + "\n")

    # id_map.md: one line per concept, image -> video, the matching you asked for
    by_concept = {}
    for r in results:
        by_concept.setdefault(r.get("concept") or "-", []).append(r)
    m = ["# ID map: concept -> image -> video", "", "| concept | image (ID, status) | video (ID, mode, status) | prompt starts with |", "|---|---|---|---|"]
    for concept, rs in by_concept.items():
        ims = "<br>".join(f"{x['id']} {x['status']}" for x in rs if x["kind"] == "image") or "-"
        vds = "<br>".join(f"{x['id']} {x.get('mode')}" + (f" from {(x.get('from_case') or '').split('_')[0]}" if x.get("from_case") else "") + f" {x['status']}"
                          for x in rs if x["kind"] in ("video", "stitch")) or "-"
        first = next((x for x in rs if (x.get("settings") or {}).get("prompt")), None)
        m.append(f"| {concept} | {ims} | {vds} | {html.escape(((first or {}).get('settings') or {}).get('prompt', '')[:70])} |")
    m += ["", "Per case: `<name>/<name>.txt` holds the full prompt and settings; `<name>/out/<name>.<ext>` is the file; "
          "`delivery.mp4` has the ID burned into its corner (when ffmpeg has drawtext)."]
    (OUT_DIR / "id_map.md").write_text("\n".join(m) + "\n")

    def card(r):
        thumb = f'<img src="{html.escape(r["thumb"])}" loading="lazy">' if r.get("thumb") else ""
        seams = f'<img src="{html.escape(r["seam_thumb"])}" loading="lazy"><small>frame before / after each cut</small>' if r.get("seam_thumb") else ""
        link = ""
        if r.get("final_output"):
            link = (f'<a href="{html.escape(r["name"])}/out/{html.escape(r["final_output"])}">original</a>'
                    + (f' · <a href="{html.escape(r["name"])}/delivery.mp4">delivery</a>' if (r.get("output") or {}).get("delivery_mb") else "")
                    + f' · <a href="{html.escape(r["name"])}/{html.escape(r["name"])}.txt">prompt+settings</a>')
        prompt = (r.get("settings") or {}).get("prompt", "")
        return (f'<div class="c {r["status"]}"><h3>{html.escape(str(r.get("id") or ""))} <small>{html.escape(r["name"])}</small></h3>'
                f'<p><b>{r["status"]}</b> {html.escape(str(r.get("mode") or ""))}'
                f'{" from " + html.escape((r.get("from_case") or "").split("_")[0]) if r.get("from_case") else ""} · {r.get("elapsed_s", "")}s · {html.escape(out_text(r))}</p>'
                f'{thumb}{seams}<p>{link}</p><details><summary>prompt</summary><p class="s">{html.escape(prompt)}</p></details>'
                f'<p class="s">{html.escape(r.get("detail") or (r.get("failure") or {}).get("exception", ""))}</p></div>')

    sections = "".join(f'<h2>{html.escape(c)}</h2><div class="g">{"".join(card(r) for r in rs)}</div>' for c, rs in by_concept.items())
    (OUT_DIR / "index.html").write_text(
        "<!doctype html><meta charset=utf-8><title>render suite</title><style>body{font:14px system-ui;margin:20px}"
        ".g{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px;margin-bottom:18px}.c{border:1px solid #ccc;border-radius:8px;padding:10px}"
        ".SUCCESS{border-color:#2a7}.FAILED,.TIMEOUT{border-color:#c33}.SKIPPED{border-color:#aaa;opacity:.7}img{width:100%}.s{font:11px monospace;word-break:break-word}"
        "h3{margin:0 0 6px}h2{margin:22px 0 8px;border-bottom:2px solid #eee}small{color:#666;font-weight:400}</style>"
        f"<h1>Render suite ({html.escape(TIER)})</h1><pre>{html.escape(chr(10).join(head))}</pre>{sections}")
    return head


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    manifest = json.loads((SUITE_DIR / "cases.json").read_text())
    cases = [c for c in manifest["cases"] if TIER_ORDER[c["tier"]] <= TIER_ORDER[TIER]]
    catalog = model_catalog()
    env_snap = {
        "tier": TIER, "cases_planned": len(cases), "budget_min": BUDGET_MIN,
        "gpu": sh("nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader"),
        "disk_free": sh("df -h / | tail -1"),
        "ckpts_before": sh(f"du -sh {WAN2GP_DIR}/ckpts/* 2>/dev/null | head -20"),
        "triton_patch": sh("grep -h 'PATCH denoiser_triton' ~/deploy.log | tail -1") or "no PATCH line in deploy.log",
        "packages": sh(f"{PYTHON_BIN} -c \"import importlib.metadata as m; print({{n: m.version(n) for n in ('torch','triton','sageattention','flash_attn','mmgp','gradio') "
                       "if any(d.metadata['Name'].lower().replace('-','_')==n for d in m.distributions())})\" 2>&1 | tail -1"),
        "model_catalog": {k: v for k, v in catalog.items()},
        "wgp_config": wgp_config_subset(),
    }
    print(json.dumps({k: v for k, v in env_snap.items() if k != "model_catalog"}, indent=2), flush=True)
    print(f"model_type catalog on this box: {len(catalog)} entries", flush=True)
    # The names Wan2GP shows in its menu, one line per image/video model: lets the next run pick exact model_types.
    interesting = re.compile(r"qwen|krea|z.?image|flux|ltx|wan|hunyuan|h3|minimax|hidream|ideogram|sensenova", re.I)
    print("MODELS: " + " | ".join(f"{k}={v}" for k, v in catalog.items() if interesting.search(f"{k} {v}"))[:6000], flush=True)

    print(f"app stopped, VRAM used now: {stop_app()} MB", flush=True)
    suite_start = time.time()
    seen_models, dead_models, finals = set(), {}, {}
    for case in cases:
        if time.time() - suite_start > BUDGET_MIN * 60:
            skipped(case, f"suite budget of {BUDGET_MIN:g} min used up")
            continue
        if case["kind"] == "stitch":
            dst = run_stitch(case, finals)
            if dst:
                finals[case["name"]] = dst
            continue
        mt, mname = resolve_model(case, catalog)
        if not mt:
            skipped(case, f"no model matched {case.get('model_match') or case.get('model_candidates')} on this box (see env.model_catalog; "
                          f"a newer Wan2GP may be needed: wan2gp_update=true)")
            continue
        case["_model_name"] = mname
        if mt in dead_models:
            skipped(case, f"model {mt} already failed with {dead_models[mt]}")
            continue
        settings = copy.deepcopy(case["settings"])
        settings["model_type"] = mt
        if case.get("needs"):
            dep = finals.get(case["needs"])
            if not dep:
                skipped(case, f"needs {case['needs']}, which produced no output")
                continue
            settings["image_start"] = str(dep)  # absolute path: Wan2GP resolves relative paths against its own cwd
        timeout_s = case.get("timeout_s", 900) + (COLD_BONUS_S if mt not in seen_models else 0)
        seen_models.add(mt)
        rec = run_case(case, mt, settings, timeout_s)
        if rec["status"] == "SUCCESS":
            finals[case["name"]] = OUT_DIR / case["name"] / "out" / rec["final_output"]
        elif rec.get("failure", {}).get("kind") in SKIP_MODEL_ON:
            dead_models[mt] = rec["failure"]["kind"]

    suite_elapsed = time.time() - suite_start
    own_mb = sum(r.get("ckpts_downloaded_mb", 0) for r in results)
    env_snap["wan2gp_fetched_own_weights_mb"] = own_mb
    env_snap["ckpts_after"] = sh(f"du -sh {WAN2GP_DIR}/ckpts/* 2>/dev/null | head -30")
    used = sorted({r["model_type"] for r in results if r.get("model_type")})
    env_snap["model_files"] = model_file_report(used)
    env_snap["model_defaults"] = model_defaults(used)
    env_snap["suite_elapsed_s"] = round(suite_elapsed)
    env_snap["results_dir_mb"] = round(tree_bytes(OUT_DIR) / 1e6)
    head = write_reports(env_snap, suite_elapsed)
    (OUT_DIR / "results.json").write_text(json.dumps({"env": env_snap, "cases": results}, indent=2))

    print("\n--- SUMMARY ---\n" + "\n".join(head))
    for r in results:
        print(f"{r['name']}: {r['status']}" + (f" ({r['elapsed_s']}s)" if "elapsed_s" in r else ""))
    print(f"artifact size: {env_snap['results_dir_mb']} MB (GitHub Free private-repo storage pool is 500 MB)", flush=True)

    ran = [r for r in results if r["status"] != "SKIPPED"]
    if not ran:
        sys.exit(3)
    core_bad = [r for r in results if r["tier"] == "smoke" and r["status"] in ("FAILED", "TIMEOUT")]
    sys.exit(1 if core_bad or not any(r["status"] == "SUCCESS" for r in results) else 0)


if __name__ == "__main__":
    main()