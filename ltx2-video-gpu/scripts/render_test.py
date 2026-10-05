#!/usr/bin/env python3
"""
render_test.py -- runs ON the rented box after the health check returns HTTP 200.

Runs each settings JSON in render_settings/ headlessly (`wgp.py --process <json> --output-dir <dir>`),
one case at a time, and writes results/results.json with timings, GPU stats and media facts.

Why the app is stopped first: the Gradio server already holds the model in VRAM; a second
process on a 24 GB card would fight it for memory.
Why SKIPPED instead of FAILED for a missing file: a missing case is a config gap, not a host fault.
"""
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

HOME = Path.home()
SETTINGS_DIR = Path(os.environ.get("SETTINGS_DIR", HOME / "render_settings"))
OUT_DIR = Path(os.environ.get("OUT_DIR", HOME / "results"))
FPS = float(os.environ.get("ASSUMED_FPS", "24"))  # only used to predict duration from video_length
CASES = [  # (name, settings file, timeout seconds)
    ("image", "image.json", 1500),        # first case pays Wan2GP's own model download (qwen_image_20B is a second one)
    ("video_short", "video_short.json", 1500),  # 42 GB LTX-2.3 + Gemma download took ~6 min at 1 Gbps
    ("video_30s", "video_30s.json", 3000),
]
MEDIA_EXT = {".mp4", ".mkv", ".webm", ".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXT = {".mp4", ".mkv", ".webm"}
HEARTBEAT_S = 30
OWN_WEIGHTS_WARN_MB = 2000  # more than this fetched during a case => pre-downloaded weights probably unused
REQUIRED_KEYS = ("model_type", "prompt")
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
APP_PATTERN = "wgp[.]py"  # bracket trick: pattern can't match pkill/pgrep's own command line


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
                pass  # file vanished mid-walk (partial download renamed) -- skip, not fatal
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


def probe_media(path):
    raw = sh(f'ffprobe -v error -print_format json -show_streams -show_format "{path}"', 30)
    try:
        info = json.loads(raw)
    except json.JSONDecodeError:
        return {"file": path.name, "size_bytes": path.stat().st_size, "probe": "ffprobe_failed"}
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), {})
    num, _, den = (v.get("r_frame_rate") or "0/1").partition("/")
    return {
        "file": path.name,
        "size_bytes": path.stat().st_size,
        "width": v.get("width"), "height": v.get("height"),
        "fps": round(float(num) / float(den), 2) if float(den or 0) else None,
        "duration_s": round(float(info.get("format", {}).get("duration", 0)), 2),
        "has_audio": any(s.get("codec_type") == "audio" for s in info.get("streams", [])),
    }


def quality_checks(path):
    """Decode the whole file once and look for black / frozen stretches. ffprobe only reads headers,
    so a file that is the right size and length but all-black or corrupt would pass probe_media."""
    try:
        p = subprocess.run(
            ["ffmpeg", "-v", "info", "-i", str(path), "-vf", "blackdetect=d=1:pix_th=0.10,freezedetect=n=-60dB:d=2",
             "-an", "-f", "null", "-"], capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        return {"quality": "ffmpeg_missing"}
    except subprocess.TimeoutExpired:
        return {"quality": "timeout"}
    black = sum(float(x) for x in re.findall(r"black_duration:\s*([\d.]+)", p.stderr))
    frozen = sum(float(x) for x in re.findall(r"freeze_duration:\s*([\d.]+)", p.stderr))
    return {"decode_ok": p.returncode == 0, "black_s": round(black, 2), "frozen_s": round(frozen, 2)}


def model_file_report(settings_paths):
    """Best effort: for each model_type find Wan2GP's own model definition and list the files it wants,
    and whether they are already in ckpts/. Answers 'is our pre-download actually the file it uses?'"""
    report = {}
    for path in settings_paths:
        mt = json.loads(path.read_text()).get("model_type")
        if not mt or mt in report:
            continue
        found = next((c for c in (WAN2GP_DIR / "defaults" / f"{mt}.json", WAN2GP_DIR / "finetunes" / f"{mt}.json")
                      if c.exists()), None)
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


def failure_summary(log_text):
    """One-line root cause. The last 600 chars of a Python traceback are mid-stack noise; the useful facts are
    the final exception line and the innermost Wan2GP frame."""
    lines = [ln.strip() for ln in log_text.replace("\r", "\n").splitlines() if ln.strip()]
    exc = next((ln for ln in reversed(lines) if re.match(r"^[\w.]*(Error|Exception|Exit)\b", ln)), "")
    frames = [ln for ln in lines if ln.startswith('File "') and "/Wan2GP/" in ln]
    where = re.sub(r'^File "[^"]*/Wan2GP/', "", frames[-1]) if frames else ""
    kind = ("triton_kernel" if re.search(r"triton|CompilationError", log_text) else
            "oom" if re.search(r"out of memory|OutOfMemory", log_text, re.I) else
            "download" if re.search(r"HTTPError|ConnectionError|No space left", log_text) else "other")
    return {"kind": kind, "exception": exc[:160], "where": where[:120]}


def heartbeat(name, start, log_path, sampler, stop_evt):
    """wgp.py writes to a file, not to our stdout, so without this the Actions log is silent for
    the whole render. Prints elapsed, GPU, and the last line wgp wrote (progress bars use \\r)."""
    while not stop_evt.wait(HEARTBEAT_S):
        try:
            tail = log_path.read_bytes()[-2000:].decode(errors="replace")
            last = re.split(r"[\r\n]+", tail.strip())[-1][:160]
        except OSError:
            last = ""
        g = sampler.latest()
        gpu = f"vram={int(g[0])}MB util={int(g[1])}% temp={int(g[2])}C" if g else "gpu=?"
        print(f"[{name}] +{int(time.time() - start)}s | {gpu} | {last}", flush=True)


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


def validate_settings(path):
    """Fail in milliseconds on a bad file instead of after a 60 s model load."""
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        return f"invalid JSON: {e}"
    if not isinstance(data, dict):
        return "top level must be an object"
    missing = [k for k in REQUIRED_KEYS if k not in data]
    return f"missing keys {missing}" if missing else None


def run_case(name, settings, timeout_s):
    out = OUT_DIR / name
    out.mkdir(parents=True, exist_ok=True)
    log_path = OUT_DIR / f"{name}.log"
    ckpts = WAN2GP_DIR / "ckpts"
    ckpt_before = tree_bytes(ckpts)
    frames = json.loads(settings.read_text()).get("video_length")
    sampler = GpuSampler()
    sampler.start()
    start = time.time()
    status, rc = "FAILED", None
    cmd = [PYTHON_BIN, "-u", "wgp.py", "--process", str(settings), "--output-dir", str(out)]
    print(f"[{name}] START (timeout {timeout_s}s): {' '.join(cmd)}", flush=True)
    hb_stop = threading.Event()
    log_path.write_text("")
    threading.Thread(target=heartbeat, args=(name, start, log_path, sampler, hb_stop), daemon=True).start()
    with open(log_path, "w") as log:
        proc = subprocess.Popen(cmd, cwd=WAN2GP_DIR, env=ENV, stdout=log, stderr=subprocess.STDOUT,
                                start_new_session=True)
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

    media = []
    for p in sorted(out.rglob("*")):
        if p.suffix.lower() in MEDIA_EXT and p.stat().st_size > 0:
            m = probe_media(p)
            if p.suffix.lower() in VIDEO_EXT:
                m.update(quality_checks(p))
            media.append(m)
    if status != "TIMEOUT":
        status = "SUCCESS" if rc == 0 and media else "FAILED"
    rec = {
        "name": name, "status": status, "exit_code": rc, "elapsed_s": elapsed,
        "requested_frames": frames,
        "ckpts_downloaded_mb": round((tree_bytes(ckpts) - ckpt_before) / 1e6),
        "outputs": media, **sampler.summary(),
    }
    # Wan2GP may fetch its own (possibly gated) files; a 401/403 there must be visible, not buried in a log.
    hits = [ln.strip()[:160] for ln in log_path.read_text(errors="replace").splitlines()
            if re.search(r"\b(401|403)\b|gated|Access to model|Repository Not Found", ln)]
    if hits:
        rec["hf_access_errors"] = hits[:5]
        print(f"[{name}] HF ACCESS PROBLEM in log: {hits[0]}", flush=True)
    if status != "SUCCESS":
        text = log_path.read_text(errors="replace")
        rec["failure"] = failure_summary(text)
        rec["log_tail"] = text[-600:]
    results.append(rec)
    print(f"[{name}] {status} in {elapsed}s | vram_peak={rec['peak_vram_mb']}MB util={rec['mean_util_pct']}% "
          f"temp={rec['max_temp_c']}C | ckpts +{rec['ckpts_downloaded_mb']}MB | "
          f"{[(m['file'], m.get('width'), m.get('height'), m.get('duration_s')) for m in media]}", flush=True)
    if status != "SUCCESS":
        print(f"[{name}] ROOT CAUSE: {rec['failure']}", flush=True)
        print(f"[{name}] LOG TAIL:\n{rec['log_tail']}", flush=True)


def add_render_ratio():
    """seconds of wall time per second of video, from the cases that produced video."""
    for r in results:
        durs = [m.get("duration_s") or 0 for m in r.get("outputs", [])]
        if r["status"] == "SUCCESS" and durs and max(durs) > 1:
            r["render_s_per_video_s"] = round(r["elapsed_s"] / max(durs), 2)
    short = next((r for r in results if r["name"] == "video_short" and "render_s_per_video_s" in r), None)
    full = next((r for r in results if r["name"] == "video_30s"), None)
    if short and full is not None:
        # video_short is cold (includes model load) so this over-predicts; compare with the real 30 s run
        full["predicted_elapsed_s_from_short"] = round(short["render_s_per_video_s"] * 30, 1)
        print(f"ratio: short = {short['render_s_per_video_s']} s/video-s -> predicted 30 s clip "
              f"{full['predicted_elapsed_s_from_short']}s; actual {full.get('elapsed_s')}s", flush=True)


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    env_snap = {
        "gpu": sh("nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader"),
        "disk_free": sh("df -h / | tail -1"),
        "ckpts_before": sh(f"du -sh {WAN2GP_DIR}/ckpts/* 2>/dev/null | head -20"),
        "data_before": sh("du -sh /data 2>/dev/null"),
    }
    env_snap["packages"] = sh(
        f"{PYTHON_BIN} -c \"import importlib.metadata as m; "
        "print({n: m.version(n) for n in ('torch','triton','sageattention','flash_attn','mmgp','gradio') "
        "if any(d.metadata['Name'].lower().replace('-','_')==n for d in m.distributions())})\" 2>&1 | tail -1")
    print(json.dumps(env_snap, indent=2), flush=True)

    runnable = []
    for name, fname, timeout_s in CASES:
        path = SETTINGS_DIR / fname
        err = "missing" if not path.exists() else validate_settings(path)
        if err:
            results.append({"name": name, "status": "SKIPPED", "detail": f"{path}: {err}"})
            print(f"[{name}] SKIPPED -- {path}: {err}", flush=True)
        else:
            runnable.append((name, path, timeout_s))

    if runnable:
        env_snap["model_files"] = model_file_report([p for _, p, _ in runnable])
        print("model files per model_type:", json.dumps(env_snap["model_files"], indent=2), flush=True)
        print(f"app stopped, VRAM used now: {stop_app()} MB", flush=True)
        for name, path, timeout_s in runnable:
            run_case(name, path, timeout_s)
            if name == "image" and results[-1]["status"] != "SUCCESS":
                print("image case failed -- skipping long cases to save GPU hours", flush=True)
                break

    add_render_ratio()
    own_mb = sum(r.get("ckpts_downloaded_mb", 0) for r in results)
    env_snap["wan2gp_fetched_own_weights_mb"] = own_mb
    if own_mb > OWN_WEIGHTS_WARN_MB:
        print(f"WARNING: Wan2GP downloaded {own_mb} MB itself during the render. The pre-downloaded weights "
              f"are probably NOT what it uses -- compare model_files above with /data.", flush=True)
    env_snap["ckpts_after"] = sh(f"du -sh {WAN2GP_DIR}/ckpts/* 2>/dev/null | head -20")
    (OUT_DIR / "results.json").write_text(json.dumps({"env": env_snap, "cases": results}, indent=2))

    print("\n--- SUMMARY ---")
    for r in results:
        print(f"{r['name']}: {r['status']}" + (f" ({r['elapsed_s']}s)" if "elapsed_s" in r else ""))
    if all(r["status"] == "SKIPPED" for r in results):
        sys.exit(3)
    sys.exit(0 if all(r["status"] in ("SUCCESS", "SKIPPED") for r in results) else 1)


if __name__ == "__main__":
    main()
