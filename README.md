# ltx2-video-gpu: full picture

Goal: rent a Vast.ai GPU box that is **ready fast**, run image + video generation on it, bring the
results back, **destroy the box**, and remember which hosts were good.

Contents: 1 Files · 2 Flow · 3 Inputs · 4 State · 5 Host memory · 6 render_test.py · 7 Libraries ·
8 Failure reasons · 9 Spend log · 10 Runbook · 11 Questions and answers · 12 UNVERIFIED · 13 Files to add or edit

## Project overview

This project automates the full lifecycle of renting a temporary GPU box on Vast.ai, deploying **Wan2GP** (for image and video generation with models such as Qwen-Image and LTX-2), optionally running a structured test suite, pulling results back, and destroying the box.

**Key design principles:**
- The GitHub Actions runner is the control plane; the GPU box is disposable.
- Everything worth keeping (results, logs, host performance data, cost) is pulled back *before* the box is destroyed.
- Host quality is remembered across runs via `attempts.csv` so future rentals prefer proven machines.
- The test suite (`render_suite/cases.json`) is tiered (`smoke` → `standard` → `lab`) and covers both production-style generations and controlled quality experiments.

The main workflow is triggered manually via GitHub Actions and supports search-only, full run, or destroy-all modes.

## 1. Files in this project

```
.github/workflows/ltx2-gpu.yml          the whole pipeline (manual dispatch)
README.md                               pointer to this file
ltx2-video-gpu/
├── ltx2-video-gpu-readme.md            this file
├── attempts.csv                        host memory, one row per box tried (auto-written)
├── runs.csv                            money + outcome, one row per run (auto-written)
├── render_runs/<ts>_m<machine>.json    render summary per run (auto-written)
├── render_suite/
│   ├── cases.json                      main test file: smoke / standard (4 images + 5 videos) / lab (quality sweeps)
│   └── cases-pratical-utube.json       practical file: 12 videos x 15 s (8 text-to-video, 4 image-to-video), talking characters
└── scripts/
    ├── pick_offers.py                  runner: rank live offers, learn from attempts.csv
    ├── probe_bandwidth.sh              box: real throughput to Hugging Face
    ├── preflight.sh                    box: GPU, VRAM, disk, python, HF reachable
    ├── bootstrap.sh                    box: find python+torch, (optional) pre-download, Triton patch
    ├── download_weights.py             box: OPTIONAL pre-download (off by default, PREDOWNLOAD=1)
    ├── start.sh                        box: start Wan2GP on :7860
    ├── render_test.py                  box: run the suite, collect stats, thumbnails, report
    ├── run_remote_suite.sh             runner: start render_test.py detached on the box, stream its output
    ├── lint_suite.py                   runner: free check of the chosen suite file before any GPU is rented
    ├── qa_review.py                    runner: OPTIONAL vision-model review of every result against its checklist
    └── log_attempt.py                  runner: write attempts.csv + runs.csv
```

## 2. End-to-end flow (action = `run`)

```
GitHub runner (free)                                Vast.ai box (billed per second)
────────────────────                                ───────────────────────────────
Guard: account must have 0 instances;  record credit BEFORE
Preflight: HF token can read Gemma-3 + LTX-2 repos
Search offers ─► pick_offers.py ranks (reads attempts.csv)
Rent #1 ──────────────────────────────────────────► image pull, boot, SSH
   probe_bandwidth.sh runs ON the box ◄──────────── real HF throughput
   probe < bar or box never ready? destroy, rent #2 … #5
Deploy: preflight.sh → bootstrap.sh → download_weights.py → start.sh   (log: ~/deploy.log)
Health: curl :7860 until HTTP 200
[render_test=true] render_test.py ───────────────► stop UI, wgp.py --process × 3 cases
   scp results/ back ◄───────────────────────────── images, video, results.json, logs
Pull deploy.log ◄────────────────────────────────── whole file, before destroy
Destroy everything + verify 0 left          (always runs);  record credit AFTER
Upload artifact run-<run_id>
Append attempts.csv + runs.csv, commit render_runs/*.json  (always runs)
```
Design rule: the runner is the control plane, the box is disposable, and everything worth keeping is
pulled back **before** destroy.

## 3. Inputs (short list; everything else is fixed and printed in the run log)

| Input | Default | Meaning |
|---|---|---|
| action | run | `run` = rent, render the suite, always destroy. `search` = rank only (free). `destroy_all` |
| suite_file | cases.json | any file in `render_suite/` (`cases.json`, `cases-pratical-utube.json`) |
| suite_tier | standard | `smoke` 2 cases, `standard` the whole file, `lab` quality sweeps (`cases.json`) |
| min_real_mbps | 600 | probe bar, measured on the box against Hugging Face |
| gpu_names | 4090,5090,3090,A100 | GPUs to consider |
| ui_first_min | 0 | minutes the web UI stays usable before the suite starts (the suite stops the UI to free VRAM) |
| hold_min | 0 | minutes to keep the box after the suite (UI restarted) |
| wan2gp_image | pinned tag | change only on purpose |
| fixed_settings | read-only | a one-option list that shows the fixed values below |

**Fixed in the YAML** (printed by the "Settings in effect" step in the log and the run Summary): max estimated ready time 30 min,
max price $0.60/h, max download price $0.004/GB, about 73 GB pulled per box, render budget 2 h, Wan2GP auto-update ON (rolls back if
it will not start), old pre-download OFF. The render suite always runs on `action=run`.

The web UI URL is printed in the log at HTTP 200 (a `::notice` and a banner). The suite stops the UI when it starts (VRAM); use
`ui_first_min` for a window before it, `hold_min` for one after.

The **model is not an input**: it comes from `model_type` inside each case. A cron `schedule` event carries no inputs, so scheduling
would need defaults-fallback changes.

## 4. Where every piece of state lives

| What | Where | Written by | Used by |
|---|---|---|---|
| Host memory, one row per box: boot s, advertised / probed / real-download Mbps, est vs actual ready min, result, own-weights MB, render status, render s, s-per-video-s, peak VRAM, fail note | `ltx2-video-gpu/attempts.csv` in git | "Log attempts" step: commit + push to the dispatched branch (`always()`, never blocks cleanup) | `pick_offers.py` on the next run |
| Money + outcome, one row per run: credit before / after, spent, attempts, winner, time to up | `ltx2-video-gpu/runs.csv` in git | `log_attempt.py` | you |
| Render summary per run | `ltx2-video-gpu/render_runs/<ts>_m<machine>.json` in git | same step | you |
| Images, video, full logs, GPU csv, `results.json`, `deploy.log` | Actions artifact `run-<run_id>`, 30 days | Upload artifact step | you |
| Live progress | Actions log | deploy polling (every 15 s) and render heartbeat (every 30 s) | you |
| Time-to-up and spend summary | run Summary page | Health check and Credit steps | you |

Git gets only small JSON/CSV. Videos live in the artifact. Artifact storage is an account-wide pool
(500 MB on Free for private repos, shared with caches and packages); retention defaults to 90 days
and is set to 30 here.

## 5. How host memory works (pick_offers.py)
- Reads `attempts.csv`, keyed by `machine_id` (offer and instance IDs die on destroy).
- **Per machine**: last measured/advertised speed ratio replaces the default 0.7.
- **Global**: median ratio and median boot time replace defaults after 3+ samples.
- **Cooldown**: a machine whose latest row is a failure is skipped for 7 days.
- **Proven preference**: a machine with any row where `probe_mbps > 0` ranks as 25% cheaper
  (`PROVEN_DISCOUNT`). Ranking only; `est_run_usd` stays honest.
- Output: top 5, one per machine, in `/tmp/candidates.json`; the rent loop walks them in order.
- Net speed history: `adv_down_mbps`, `probe_mbps`, `dl_mbps` columns. Only machines that got far enough to be probed have numbers.

"Good bandwidth" = sustained throughput measured on the box against Hugging Face, not the advertised
figure. Hosts advertising 4-8 Gbps probed 1-1.4 Gbps in your history; the default bar is 800 Mbps.

## 6. render_test.py (runs on the box)

It reads `render_suite/cases.json`, keeps the cases of the chosen tier, and runs them one at a time with
`wgp.py --process <settings.json> --output-dir <dir>`, each as its own process group. The runner starts it
detached (`run_remote_suite.sh`), so a dropped SSH connection cannot kill an hour-long suite.

| Part | Does | If it were missing |
|---|---|---|
| tier filter, `needs` | picks cases by tier; an image-to-video case gets its start image from an earlier case's output | no comparison between a prompt and its image-to-video |
| `model_catalog`, `model_candidates` | lists `defaults/*.json` stems on the box; a case with candidates uses the first one that exists, else it is SKIPPED with the catalog hint | a wrong model name wastes a case; now it is a one-line SKIPPED |
| cold bonus | the first case of each model gets +1500 s timeout (its own download) | the cold download would trip the timeout |
| `stop_app` | kills the Gradio UI, waits for VRAM < 1.5 GB | the UI holds the model; headless run OOMs |
| `GpuSampler` | every 2 s: VRAM, util, temp (summary in results.json, raw in `<case>_gpu.csv`) | no idea if the GPU is enough or idle |
| `heartbeat` | every 30 s: elapsed, GPU, last wgp line | the Actions log is silent during a render |
| `probe_media` | ffprobe: size, resolution, fps, duration, audio, bitrate | "a file exists" passes even if it is 3 s long |
| `quality_checks` | decodes the file; sums black, frozen and silent seconds | all-black, frozen or silent output would pass |
| seed check | compares the requested seed with the one in the output file name (`seed_honored`) | silent loss of reproducibility |
| partial cleanup | sliding windows leave a partial (20 s) next to the final (30 s); partials are deleted unless `KEEP_PARTIALS=1` | double the artifact size |
| delivery re-encode | H.264 CRF 21 copy (`delivery.mp4`), size and time recorded; `DELIVERY=0` skips it | no data on the "20 to 40 MB" question |
| thumbnails | image: 640 px; video: a 6-frame strip across the clip (look for seams and drift) | judging 31 cases means opening 31 files |
| failure policy | OOM and other errors fail only their case; a Triton or download failure skips the remaining cases of that model | 20 cases fail one by one at 30 s each |
| budget | stops starting new cases after `SUITE_BUDGET_MIN` (40 / 90 / 150 min by tier) | a surprise 4-hour bill |
| reports | `results.json`, `report.md` (also appended to the run Summary), `index.html` with thumbnails and settings | no overview |
| exit code | 0 if the 3 smoke cases pass; 1 if one fails or nothing succeeded; 3 if nothing ran | exploratory failures at 2K or on other models do not turn the run red |

**Reading results.json**: `env` (gpu, packages, `triton_patch`, `model_catalog`, `model_files`,
`wan2gp_fetched_own_weights_mb`, `suite_elapsed_s`, `results_dir_mb`), then per case: `status`, `elapsed_s`, `cost_usd`,
`warm`, `ckpts_downloaded_mb`, `peak_vram_mb`, `mean_util_pct`, `render_s_per_video_s` (warm video cases only),
`seed_honored`, `failure {kind, exception, where}`, `hf_access_errors`, and `output {width, height, fps, duration_s,
bitrate_mbps, has_audio, decode_ok, black_s, frozen_s, silent_s, delivery_mb, delivery_mbps}`.

## 7. Libraries and tools: why each, what fails without it

| Tool | Used for | Without it |
|---|---|---|
| Python stdlib only in `render_test.py`, `pick_offers.py`, `log_attempt.py` | subprocess, threading, json, re, signal, pathlib; csv, statistics, datetime | n/a: no installs, so nothing in the image can break them |
| `vastai==1.8.3` (runner) | search, create, show, destroy instances, credit | nothing can be rented or destroyed |
| `ssh` / `scp` | run and copy on the box | no deploy, no results |
| `curl` | probe, HF check, health check | no bandwidth gate |
| `huggingface_hub` | gated weight download | no weights |
| `nvidia-smi` | GPU facts and stats | no VRAM check, no sampler |
| `ffmpeg` / `ffprobe` | media facts and quality checks | quality checks degrade to "ffmpeg_missing" |

## 8. Failure reasons the system records (`result` column)

| result | Meaning | Typical cause |
|---|---|---|
| `create_failed` | offer gone before we could rent it | someone else took it |
| `never_running` | not `running` within 300 s (image pull counts) | slow or broken host, stuck image pull |
| `no_ssh` | running, but SSH not answering after ~2 min | host networking / sshd |
| `slow_probe` | probe below the bar (default 800 Mbps) | advertised speed was a claim; real HF throughput was lower |
| `deploy_failed` | box passed the probe, but deploy or health failed. `fail_note` holds the last FAIL / Traceback line | script bug, disk, download stall, app crash |
| `ok` | HTTP 200 reached | n/a |

Claim vs actual is visible per row: `adv_down_mbps` vs `probe_mbps` vs `dl_mbps`, and
`est_ready_min` vs `ready_min`. `pick_offers.py` already learns the speed ratio from them. It does
**not yet** learn from est-vs-actual ready time; the data is logged so it can.
Not classified separately yet: image-pull failure vs host failure inside `never_running`, and render
failures (`render_status` column, not `result`).

Boot data (15 boxes): every box that came up did so in 40-185 s. The 8 that did not were killed at the
300 s timeout, so we never learned whether they would have come up at 6-10 min. 300 s is supported by
the data; stretching to 10 min is not (no evidence they recover, and each wait is billed).

## 9. Spend log
`runs.csv` and the run Summary page show `credit before -> after` and the difference. `credit` is the
field the Vast docs show for `vastai show user`. Billing may lag a few minutes after destroy, so judge
cost by the trend over several runs.

## 10. Runbook
1. **First run of the suite**: `action=run`, `render_test=true`, `suite_tier=smoke` (about the same as run 2), then `standard`, then `full`. Read `report.md` in the run Summary, then `index.html` and `results.json` from the artifact:
   - `model_files` and `wan2gp_fetched_own_weights_mb`: is the pre-downloaded 19B file what the 22B
     `model_type` uses? If Wan2GP fetched its own, drop `download_weights.py` from the chain (about 7 min saved) or align `model_type`.
   - Did the 30 s case produce 30 s? Check `duration_s`, `decode_ok`, `black_s`, `frozen_s`.
2. If a case fails on schema: run with `render_test=false`, `hold_min=30`, open the URL, set the model in the UI,
   click **Export Settings**, replace the JSON, commit.
3. **Daily**: dispatch about 40 min before you need it. Worst case is 5 attempts × ~5.4 min + ~11 min.
4. After any run: Vast console should show 0 instances. If not, `action=destroy_all`.

## 11. Questions and answers

### Reliability and cost
| Question | Answer |
|---|---|
| Is the probe the risk? | No. All 7 boxes that reached it passed (955-1445 Mbps). The risk is boot: 8 of 15 never became usable. Top-5 candidates plus destroy-always handle it. |
| Can it be "sure shot"? | No. It can only make a failed attempt cheap and automatic: probe before any download, 5 candidates, destroy always, verify zero instances. Start about 40 min early. |
| Stop vs destroy? | Always destroy. Stopped instances still bill storage and lose their GPU. |
| Does `pick_offers.py` need inputs? | No. Defaults run it; only `action` is chosen. The model is not an input. |
| Is a 5 min boot threshold right? | Supported by the data (all successes <= 185 s). See section 8 for the survivorship caveat. |
| Is `max_ready_min` 15 too low? | It was. It is now a wide 30 min pre-filter; the probe and the 300 s boot timeout are the real gates. |

### Models and Wan2GP
| Question | Answer |
|---|---|
| Why Wan2GP, and does it hurt quality? | Wan2GP is the runner and memory manager. Quality comes from the model, its precision (int8/fp8 vs full), steps and resolution. Whether its quantised files visibly change LTX output is unverified. |
| Does Wan2GP have its own models? | Yes: Wan, Hunyuan, Flux, Qwen, Z-Image, LTX and others. It auto-downloads the files suited to the GPU generation. |
| 19B vs 22B? | The workflow pre-downloads `ltx-2-19b-distilled`; Wan2GP's documented example is `ltx2_22B_distilled`. They may be different files, so the 67 GB pre-download may be unused. `render_test.py` reports it (`model_files`, `wan2gp_fetched_own_weights_mb`). |
| HF agreement? | The preflight proves access to the two gated repos. If Wan2GP fetches its own mirrors they are probably ungated; any 401/403 is now flagged as `hf_access_errors`. |
| Gemma to LTX-2 handshake? | Wan2GP wires them together. The risk is ours: the files must sit where it looks. `bootstrap.sh` symlinks `ckpts/gemma3` to `/data/gemma3` and its own comment calls that assumption unverified. |
| Does ckpts growth mean our output? | No. `ckpts/` holds model weights. Outputs go to `--output-dir`. |
| Model reasoning? | LTX-2 is a video diffusion model and does not reason; Gemma is only its text encoder. What can be tested is prompt adherence: `image.json` carries left/right/front spatial constraints, judge by eye. |

### Settings and rendering
| Question | Answer |
|---|---|
| Export settings every install? | No. The settings live in `render_suite/cases.json` in git; `render_test.py` writes the exact settings it ran to `results/<case>/settings.json`. They feed headless mode (`wgp.py --process file.json`). |
| What is inside? | Keys such as `model_type`, `prompt`, `resolution`, `num_inference_steps`, `video_length` (frames). The three provided files follow Wan2GP's docs examples, not a UI export. |
| Risk of docs-based settings? | Low. A bad file fails in milliseconds, a wrong model fails fast, the image case runs first. Worst case is a 60 min timeout at about $0.59/hr. |
| Missing settings file? | SKIPPED, not FAILED: a config gap is not a host fault. With all 3 committed nothing is skipped. |
| Video length limit? | No documented cap found. Length is `video_length` in the JSON; VRAM does not grow with length but RAM does. |
| 30 min video in practice | Do not ask one call for 43,000 frames. Pattern: storyboard, N shots of 5-10 s (image-to-video from the previous shot's last frame for continuity), render each, ffmpeg concat (stream copy if codecs match, else re-encode), mix audio. Not built here; each shot would be one more settings JSON. |
| Render ratio output? | `render_s_per_video_s` and `predicted_elapsed_s_from_short` in `results.json`, plus a `ratio:` line in the Actions log. The short clip is cold (includes model load), so it over-predicts; the real 30 s run is the truth. |
| Media checks quick? | Yes. ffprobe is under a second; the decode for a 30 s clip takes seconds (10 min timeout). |
| Can output come back over HTTP? | Gradio is the web UI on :7860; Wan2GP's docs show a Python API that can target the running Gradio queue, but I found no plain REST route for generation. Production pattern: queue, worker, object storage, status polling. |

### Logs, results and learning
| Question | Answer |
|---|---|
| Live logs during render? | Yes: heartbeat every 30 s with elapsed, GPU and the last wgp line. Full case logs are in the artifact. |
| Deploy-chain logs? | Visible as the last 8 lines per 15 s poll; the full `deploy.log` is pulled before destroy and saved in the artifact. |
| Where is GpuSampler output? | Per-case summary in `results.json`; raw samples in `<case>_gpu.csv` in the artifact. |
| `results.json` location? | Artifact, `render_runs/<ts>_m<machine>.json` in git, and `~/results/` on the box until destroy. |
| Results back ASAP? | Copied right after the render and before destroy; uploaded as an artifact a few steps later. Videos never go to git. |
| Does the system self-learn? | `attempts.csv` gets render, weights, est-vs-actual and fail-note columns every run; `pick_offers.py` learns host speed ratio, boot time, cooldown and proven hosts from it. |
| Is `attempts.csv` logged automatically? | Yes, for `run` actions, pushed to the dispatched branch. A failed push only warns. |
| Is net speed stored per machine? | Yes: advertised, probed and real-download Mbps. |
| Credit before/after? | Recorded per run in `runs.csv` and the Summary page. |

### Access and operations
| Question | Answer |
|---|---|
| Gradio from my laptop? | Yes. The URL (`http://<host>:<mapped port>/`) is in the run Summary. Use `render_test=false` and `hold_min=30`; the render test stops the UI. Plain HTTP, no login, so anyone with the IP:port can use it while it is up. |
| Repo visibility and quota? | Visibility: the Public/Private label by the repo name (change under Settings, Danger Zone). Quota: your account's Billing and plans, Actions usage. Each run's artifact: the run page, Artifacts section. |
| What was fixed along the way? | The render step now runs with `set +e`, because GitHub's default `bash -e` would abort a failed render before its results were pulled. |

## 11b. First real run (run 37297908189, RTX 5090, machine 74248): what worked and what remains

Timeline (from the logs and artifact): created 10:39:12, running in 116 s, probe 767 Mbps (samples 756 / 774 / 767,
bar 600), deploy 67.7 GB at 1031 Mbps (525 s), HTTP 200 at 10:51:05 = **11 min 53 s** after create (estimate was 21.9).
Spend: credit $4.8795 -> $4.4773 = **$0.4023**.

| Area | Result | Evidence |
|---|---|---|
| Guard, preflight, search, rent, probe sampling | **worked** | first offer accepted, median of 3 samples |
| Deploy chain, health check | **worked** | HTTP 200 on poll 4 |
| Pull logs, cleanup + verify, artifact | **worked** | 6 files, `deploy.log` and GPU csvs present |
| `attempts.csv` | **updated, but the file on GitHub was corrupted** | two CSV versions concatenated (two header lines, 4 foreign old-format rows). `log_attempt.py` now heals this on the next run |
| `runs.csv` | **worked** | one row per run since the new workflow; the 08:50, 09:14 and 09:39 runs are in it. Two runs at 10:22 and 10:37 used an older workflow, so they only appear in `attempts.csv` |
| `render_runs/*.json` | **worked** | `20261005T105820Z_m74248.json` committed and pushed (d809016) |
| Spend line in the log | **bug, fixed** | printed `spent $unknown` (variables not exported); `runs.csv` had the right 0.4023 |
| Image case | **skipped: `image.json` is invalid JSON** | missing closing quote on the prompt; reproduced the identical error message. A free lint step now catches it before renting |
| Video cases | **FAILED: Wan2GP's own Triton kernel** | `models/ltx2/denoiser_triton.py`, `split_rope`: `TypeError: 'constexpr' object is not subscriptable`. Same error in both cases |
| Pre-downloaded weights | **unused: 67 GB, 525 s, about $0.08 per run** | `ckpts/` held only an empty symlink; Wan2GP then fetched its own 42 GB (LTX-2.3 22B int8, Gemma-3 QAT, VAEs, upscalers) |

**Do we need to download models in bootstrap? No.** `bootstrap.sh` now skips the pre-download by default
(`PREDOWNLOAD=1` brings it back). Wan2GP downloads what it needs at the first render; that cost about 6 min
at 1 Gbps and is inside the first case, which is why the case timeouts are now 1500 s.

**The Triton failure is a software mismatch, not a host problem.** A public report (LykosAI/StabilityMatrix #1756:
RTX 5090, torch 2.7.1+cu128, Triton 3.3.1) shows the same error in the same function, and fixes it by indexing
`.value` on two `tl.constexpr` objects. Your box has torch 2.7.1+cu128; the Triton version was not captured,
so `render_test.py` now records package versions. `bootstrap.sh` applies that workaround to `_split_rope` only
(idempotent, backup `.orig`, prints a `PATCH denoiser_triton:` line in `deploy.log`). Treat it as an experiment.

Still open after this run: the patch working on a real file, the image case (second model download), whether the
30 s clip completes, whether a 4090 hits the same error, quality and `render_s_per_video_s`.

## 11c. Second run (first success): every task, timed

Box: machine 143802, RTX 5090, $0.5556/hr. Probe 833 Mbps (samples 844 / 803 / 833, bar 600). Spend **$0.2901**
(credit $4.4555 -> $4.1653). All times are from the Actions log (UTC, 11:33 to 11:57).

| Stage | Time | What it was |
|---|---|---|
| Rent: create, boot, SSH, 3-sample probe | 2 min 47 s | first offer accepted |
| Deploy (no pre-download any more) | 21 s | was 525 s in run 1 |
| App answering | 12 s | HTTP 200 on poll 2 |
| **Create to usable** | **about 3.5 min** | run 1: 11 min 53 s; estimate was 20.1 min |
| Image, 1024x1024 (Qwen-Image 20B) | 291 s | mostly Wan2GP's own 30.7 GB download and load; the render itself was the last part |
| Video 5 s, 1280x704 (LTX-2.3 22B distilled) | 416 s | mostly its own 42.1 GB download (about 6 min at 1 Gbps); the render after load was about 55 s |
| **Video 30 s, 1280x704, warm** | **186 s** | 2 sliding windows: about 120 s + 60 s; **6.2 s of wall time per video second**, about $0.03 |
| Render step total | 896 s | 15 min, of which about 10 min was model download |
| Hold | 5 min | wasted: the app had been stopped (see below) |

Answers drawn from this run:
- **How long a video, in what time?** A 30.04 s clip at 1280x704 in 186 s on an RTX 5090, once the model is on the box.
  A 30 min video at that speed is about 3.1 hours of rendering, ignoring stitching and continuity problems.
- **The cold prediction was wrong by 13x.** The old script predicted 2477 s for the 30 s clip from the 5 s clip; the
  5 s clip carried the download. `render_test.py` now reports a ratio only for warm cases (nothing downloaded inside them).
- **Why two video files?** Sliding-window renders save each window: the 20.04 s file is a partial, the 30.04 s file is final.
  `results.json` now marks the longest as `final_output`.
- **Why 20 to 40 MB?** File size is bitrate times duration. 20 MB over 20 s is about 8 Mbps and 40 MB over 30 s is about 10.7 Mbps
  (your sizes; `bitrate_mbps` is now logged). That is a high bitrate for 720p, which is typically a few Mbps. It reflects the
  encoder quality setting and the fine grain in generated video, not better content. A smaller file is possible with a
  re-encode (for example H.264 at CRF 20 to 23), but that is a delivery choice, not a quality gain.
- **The Hold URL refused connections** because `render_test.py` stops the Gradio app to free VRAM and nothing started it again.
  The Hold step now restarts it (`start.sh`) when `render_test=true` and waits for HTTP 200 before printing the URL.
- **The Triton workaround held.** The same image (torch 2.7.1+cu128, Triton 3.3.1, mmgp 3.8.2) failed run 1 at the first
  denoise step and passed here. `render_test.py` now prints the `PATCH denoiser_triton:` line from `deploy.log` as `env.triton_patch`.
  The patch line itself was not in the logs you sent (the Actions log shows only the last 8 lines per poll), so confirm it in the artifact.

### What the Triton fix is (bird's eye)

```
 your prompt ─► text encoder ─► denoising loop (8 steps) ──► video
                                      │
                      every attention layer applies "RoPE"
                      (rotary position embedding: tells the model
                       where / when each pixel-patch is)
                                      │
                      Wan2GP does it with a GPU kernel written in Triton
                                      │
        kernel code:   AXIS_IDS[a]  ,  GRID[j]      <- indexes two tl.constexpr objects
                                      │
        Triton 3.3.1 (on our image):  "constexpr object is not subscriptable"  -> crash at step 1
                                      │
        our fix (bootstrap.sh):   AXIS_IDS.value[a] , GRID.value[j]   (that one function only)
                                      │
                                      ▼
                                   renders
```
- **What:** a compile error in one small GPU function, hit on the very first denoising step.
- **Why:** the function indexes a Triton wrapper object directly; this Triton version wants `.value`.
- **How:** `bootstrap.sh` rewrites those two spots after install (idempotent, `.orig` backup, a `PATCH denoiser_triton:` line in `deploy.log`).
- **One line:** a version mismatch between Wan2GP's kernel and the Triton in the image, not a model or host problem.

### PREDOWNLOAD
It is read **on the box, after the instance is rented and SSH works** (inside `bootstrap.sh`), never during offer selection. The runner only forwards the workflow input in the Deploy ssh command. Default is set in `bootstrap.sh`: `PREDOWNLOAD="${PREDOWNLOAD:-0}"`, so the pre-download is off. To override, run the workflow
with input **predownload = true**; the Deploy step passes `PREDOWNLOAD=1` to the box. (Before this change there was no way to
override it without editing the script.)

### Selection and probe
- Instance selection: **fine**. Both successful runs were the first-ranked offer on an RTX 5090 advertising 850 to 950 Mbps.
- Probe: **fine**. It reads about 20 to 25% below the real download (767 vs 1031 Mbps in run 1), so a pass is conservative.
  The 3-sample median is the right size; more samples add seconds, not accuracy.
- `weights_gb` default is now 73 (measured: 42.1 GB LTX-2.3 + 30.7 GB Qwen-Image). It only feeds the readiness estimate.

## 11d. What worked, what still needs work

| Worked | Still open |
|---|---|
| Rent, probe, deploy, health, render, artifact, credit, git ledger | Prompts are basic, so output is basic (see 11e) |
| Image, 5 s and 30 s video all rendered | No visual quality check beyond decode, black and frozen frames |
| 30 s in 186 s warm | Weights re-download on every fresh box (about 6 min for LTX alone) |
| Triton workaround | Image case pulls a second 31 GB model for one still |
| | 30 s clip is 2 sliding windows: continuity at the seam is unchecked |
| | Output bitrate is high; no delivery re-encode step |

"Load the model upfront" was wrong in *which* files (Wan2GP uses its own), not in principle: the weights must be on the box
before the render either way. The only real ways to remove that 6 min are persistent storage, a pre-baked image, or a
host that already holds the files. None is built.

## 11e. Missing parts of image and video generation, and what Seedance uses

Public facts about Seedance (ByteDance Seed; the weights are not released):
- Seedance 1.0 is a diffusion transformer on VAE latents with decoupled spatial and temporal layers, text conditioning from a
  fine-tuned decoder-only LLM, and multi-shot and image-to-video handled in one model.
- Post-training uses supervised fine-tuning plus video-specific RLHF.
- Inference is sped up about 10x with multi-stage distillation; the report cites a 5 s 1080p clip in 41.4 s on an NVIDIA L20.
- Later versions add joint audio-video generation (1.5 Pro) and reference-image control (2.0, per a third-party wiki).

Where our setup differs and what to add, in order of likely payoff. Items marked (general) come from common practice, not from these logs.

| Gap | Today | Candidate change |
|---|---|---|
| Prompt detail | one short sentence | structured prompts: subject, setting, camera move, lighting, style, audio. Biggest lever for "detail" (general) |
| Resolution | 1280x704 | try 1920x1088 and check VRAM; the log shows a 2-phase pipeline with spatial and temporal x2 upscaler files already downloaded |
| Model variant | distilled, 8 steps | Wan2GP lists LTX2 DEV presets (Vanilla Dev, HQ mode) with tunable settings; slower, likely higher quality |
| Multi-shot / continuity | single prompt | one prompt per shot; start each shot from the previous last frame (image-to-video) |
| Subject consistency | none | generate a reference image first (the image case), then use it as the first frame |
| Candidates | one seed | 3 to 4 seeds, pick the best (general) |
| Audio | model default | an explicit audio description in the prompt; check `has_audio` |
| Delivery | raw encoder output | re-encode to a sane bitrate; optional frame interpolation |
| Quality gate | decode, black, frozen | add a vision-model or manual review step; prompt adherence is not measured |

## 11f. Run 3 (full tier, 27 cases, 27/27 succeeded): every task timed

RTX 5090, 40.2 min wall, GPU cost about $0.384, artifact 224 MB, 72.8 GB downloaded by Wan2GP itself.

| Task | Models and settings used in run 3 | Time | Note |
|---|---|---|---|
| Image, first (`I01`) | Qwen-Image 20B int8, 4 steps, 1664x928 | **303.5 s** | cold: +30.7 GB download and load |
| Image, warm (12 cases) | same | **28 to 42 s**, typically 30 s | VRAM peak only 3.5 to 5.6 GB, GPU util 15 to 29 % |
| Video, first (`V01`, 10 s) | LTX-2.3 Distilled 1.0 22B int8, 8 + 3 steps, 1280x704 | **457.1 s** | cold: +42.1 GB |
| Video 10 s, warm (`V02`,`V03`,`V04`,`V06`) | same | **92 to 99 s** | about 9.2 to 9.8 s per video second |
| Video 12 s (`V05`,`V07`,`V09`) | same | **105 to 108 s** | about 9 s per video second |
| Video 15 s, 704x1280 (`V08`, avatar) | same | **136 s** | 9.0 s per video second; VRAM peak 7.7 GB |
| Video 5 s clips (`V10a-c`) | same | **65 to 78 s** | short clips pay the same fixed load, so more seconds per second |
| Stitch to 15.12 s (`V10`) | ffmpeg concat | **1.7 s** | seams at 5.04 s and 10.08 s |
| Video 10 s at 1920x1088 (`V11`) | same | **192.7 s** | about 19 s per video second; VRAM peak 8.1 GB |

Takeaways: warm work is cheap (an image about 30 s, a 10 s video about 95 s, about $0.015 each); the cold downloads (about 12 min
together) dominate a short run. The GPU is mostly idle (images 15 to 29 %, VRAM use 3.5 to 8 GB of 32 GB): Wan2GP runs heavily offloaded.

## 11g. Why the quality was poor, and what was changed (evidence-ranked)

You saw: washed-out, mushy images (about 0.2 MB); dark or hazy videos; image-to-video inheriting the bad start frame.

| # | Cause | Evidence | Confidence | Fix in this version |
|---|---|---|---|---|
| 1 | **Qwen-Image was sampled with 4 steps and no guidance.** Qwen-Image is not a distilled model: the vendor default is 50 steps with true CFG 4 and a negative prompt; 4 to 8 steps only work with a Lightning LoRA, which we never activated. Wan2GP's own changelog says Qwen is "very picky", needs "lots of steps (50?)" and was "dying for a negative prompt". | `steps=4` in all 12 image cases; the log loads `qwen_image_20B_quanto_bf16_int8.safetensors` and no LoRA | high | standard images use 30 steps, CFG 4, a negative prompt; `L03` tests the official 50 steps; `L01` repeats the old 4-step config as a control |
| 2 | **Our prompts asked for darkness and haze.** The avatar prompt said "dark-themed studio"; others said golden-hour backlight, volumetric light, mist, soft focus, "warm lamp lighting". The models obey. | the prompt texts in the old manifest | high | all prompts rewritten: bright key light, evenly lit, crisp focus, "no haze or mist"; avatar studio is now bright |
| 3 | **Image-to-video inherits the start frame.** Soft, flat start images give soft, flat video. | V01, V05, V06, V08 were all image-to-video from the 4-step images | high | fix 1 and 2 first; `L10`-`L15` then vary the video side |
| 4 | **The box runs an old Wan2GP.** The menu showed "LTX-2.3 Distilled 1.0" and "Qwen Image 20B". Upstream (v13.141, 29 Sep 2026) lists Qwen Image 2.1, Krea 2 (with Real/HD VAE choice), LTX-2.5, Distilled 1.1 ("better audio and visuals"). | env.packages and the loaded model names | high that it is old; unknown how much better | `wan2gp_update=true` + lab cases `L04`, `L05`, `L11`, `L12` |
| 5 | **LTX defaults to a 2-phase pipeline** (8 steps at low resolution, then 3 refine steps). A single high-resolution phase is "slower, more VRAM, potentially higher quality" per the changelog. | log shows "First Phase" / "Second Phase" | medium | `L10` tries `guidance_phases: 1` (key UNVERIFIED; `phases_seen` shows if it worked) |
| 6 | **Dog prompt asked for a human-like body** ("wearing a shirt, paws on the wheel"), and the 4-step image could not hold it. | your "dog face on a human body" | medium | prompt now says natural dog anatomy; sampling fixed |
| 7 | **The JPEG is not the blur, but it throws detail away.** Wan2GP saves images as JPEG by default; PNG and lossless WebP exist as a config option. File size follows detail: the 3.1 MB SDXL PNG has more fine detail and is lossless, the 0.2 MB image is smooth and compressed. | your two reference images: Sobel edge density 18.6 (SDXL, 1536x1536) vs 11.6 (Wan2GP JPEG, 1024x1024) | medium | `env.wgp_config` now prints the box's real config keys so the codec key can be set exactly next run |
| 8 | int8 quantization and heavy offload (3.5 to 8 GB of 32 GB used) may cost a little quality and a lot of time. | log: `quanto_bf16_int8`; VRAM and util numbers | low (no direct evidence of a quality loss) | not changed; `env.wgp_config` and `model_defaults` show what is available |
| 9 | The Triton RoPE workaround altering results. | videos are coherent and the log says the Triton kernel is in use | low | not changed |

About the "4 MB image" target: size is a symptom, not a goal. A detailed 1664x928 PNG is naturally 2 to 4 MB; the same
picture as a lightly compressed JPEG is a few hundred KB. Judge by the new `edges` (detail) and `luma` (brightness) numbers in `report.md`,
and by eye.

The Prompt Enhancer you saw in the UI (a Qwen3.x GGUF language model, several GB) rewrites short prompts into detailed ones. It
downloads on first use, so the 5 min hold is too short: use `hold_min` 30 or more if you want to try it.

## 11h. The suites now

**English only.** All speech and singing in the videos is English (a Hindi variant was dropped; it can be added as one more case).
No children and no blood in the suites. The notes' studio and character names are not used: the clay, cartoon and presenter characters are original.

### `cases.json` (main file)
| ID | mode | size | what it is |
|---|---|---|---|
| `I01` | t2i | 1664x928 | Keyframe for V01. Rule sword_scabbard fixes the sword above the shoulder / upside-down hilt. |
| `I06` | t2i | 1664x928 | Keyframe for V06. Rule animal_anatomy keeps dogs as dogs. |
| `I08` | t2i | 928x1664 | smoke, THE PRESENTER. Fashion-model look, adult, bright studio. Member of the 'presenter image' sweep. |
| `I20` | t2i | 928x1664 | Keyframe for V20 (wide start; the video zooms in). |
| `V01` | i2v | 10 s, 1280x704 | Image-to-video from I01. |
| `V06` | i2v | 10 s, 1280x704 | Image-to-video from I06. |
| `V08` | i2v | 15 s, 704x1280 | smoke, THE PRESENTER: image-to-video from I08, 15 s of English speech. Listen for lip sync and clarity. |
| `V20` | i2v | 15 s, 704x1280 | THE SINGER: image-to-video from I20. Camera: wide, slow zoom-in to full body. LYRICS ARE ORIGINAL (the requested song's lyrics are copyrighted and are not reproduced); replace with your own or licensed text. |
| `V30` | t2v | 15 s, 1280x704 | THE OBJECTS-AND-MOTION EXAMPLE (replaces the blood story): text-to-video; checks optics and proportions. |

`lab` adds sweeps (same prompt and seed, one change each), ranked by detail and brightness in `report.md`:

| ID | model | steps | what it tests |
|---|---|---|---|
| `L01` | qwen_image_20B | 4 | Old config (4 steps, CFG default): the control that produced the poor images. |
| `L02` | qwen_image_20B | model default | Only model, prompt, size, seed: Wan2GP's own defaults. |
| `L03` | qwen_image_20B | 50 | Official Qwen-Image recipe: 50 steps, CFG 4. |
| `L05` | krea2_turbo | model default | Krea 2 Turbo (8 steps). |
| `L06` | krea2_raw | model default | Krea 2 RAW. |
| `L07` | flux2_klein_9b | model default | Flux 2 Klein 9B. |
| `L08` | Qwen.*Image.*2\.1 | model default | Qwen Image 2.1 if the box has it (matched by name). |
| `L11` | ltx2_22B_distilled_1_1 | 8 | LTX-2.3 Distilled 1.1. |
| `L12` | ltx2_25_22B_distilled | 8 | LTX-2.5 Distilled. |
| `L13` | ltx2_22B_1_1 | model default | LTX-2.3 Dev 1.1 with its own defaults (slower). |
| `L14` | ltx2_25_22B | model default | LTX-2.5 Dev with its own defaults. |
| `L15` | ltx2_22B_distilled | 8 | Single high-res phase; key guidance_phases=1 UNVERIFIED (phases_seen shows if it took effect). |
| `L16` | ltx2_22B_distilled | 8 | 1920x1088. |
| `L17` | ltx2_22B_distilled | 8 | Text-to-video baseline for comparing against the image-to-video cases. |

### `cases-pratical-utube.json` (practical file): 12 videos x 15 s
| ID | mode | size | what it is |
|---|---|---|---|
| `PI01` | t2i | 1664x928 | Keyframe for P01 (two original clay characters, facing each other). |
| `PI02` | t2i | 928x1664 | Keyframe for P07. |
| `PI03` | t2i | 928x1664 | Keyframe for P08. |
| `PI04` | t2i | 1664x928 | smoke, Keyframe for P09. |
| `P01` | i2v | 15 s, 1280x704 | TALKING TO EACH OTHER (two characters, clay, i2v from PI01). |
| `P02` | t2v | 15 s, 1280x704 | T2V stylised clay character; squash-and-stretch. |
| `P03` | t2v | 15 s, 1280x704 | T2V procedural objects-and-motion example. |
| `P04` | t2v | 15 s, 1280x704 | T2V physics explainer (optics must be correct). |
| `P05` | t2v | 15 s, 1280x704 | T2V landscape with a timeline. |
| `P06` | t2v | 15 s, 704x1280 | T2V adult walking on a beach (no singing). |
| `P07` | i2v | 15 s, 704x1280 | I2V singer; original lyrics (the named song's lyrics are copyrighted and not reproduced). |
| `P08` | i2v | 15 s, 704x1280 | I2V talking presenter (English). |
| `P09` | i2v | 15 s, 1280x704 | smoke, TALKING TO EACH OTHER (two real-looking adults, i2v from PI04). |
| `P10` | t2v | 15 s, 1280x704 | T2V product shot. |
| `P11` | t2v | 15 s, 1280x704 | T2V reflections and several moving subjects. |
| `P12` | t2v | 15 s, 1280x704 | T2V hands, liquids, steam, food. |

The geography map explainer from the notes is not included: a video model cannot draw accurate maps; make it with map tools plus a voice-over.

### The base prompt and the per-object rules (fixes for the common mistakes)
Every prompt is composed at run time: the case's own text + the file's `base` quality text + the text of each rule the case lists.
Images always get the negative prompt; video gets it only when a case sets `"negative": true` (distilled LTX runs at CFG 1 and ignores it
unless NAG is on). The full composed prompt is in each `<ID>.txt` and in `results.json`.

| rule | what the QA reviewer checks |
|---|---|
| `sword_scabbard` | Sword is in a scabbard on the back, hilt up above the shoulder, no bare blade. |
| `bed_covers` | Blanket covers the legs. |
| `animal_anatomy` | Animals have real animal bodies (no human torso or hands). |
| `two_person_dialogue` | Two distinct characters face each other, speak in turns, mouths match the speaker. |
| `talking_head` | Eyes on camera, lip movement matches speech, hands correct. |
| `singer_on_beach` | Eyes on camera, singing mouth motion, full body visible at the end, hands natural. |
| `physics_light` | Ray bends toward the normal entering glass and away leaving; spectrum order red to violet; no garbled text. |
| `object_motion` | Objects keep size and shape; no clipping or morphing. |
(`cases-pratical-utube.json` adds `clay_style`, `cafe_dialogue`, `product_shot`, `nature_doc`.)

### Checking for mistakes: four layers
1. **Prompt side**: base text + rules + negative prompt (above).
2. **Numbers**: brightness, contrast, detail, black / frozen / silent seconds, loudness in LUFS (target about -16; below -24 sounds quiet). The delivery copy is loudness-normalised to -16 LUFS.
3. **Vision model (optional)**: `qa_review.py` sends each case's review image (the picture, or 4 frames of the video) and its checklist to a vision model
   and writes `qa_review.md` into the run Summary. Add the repository secret `LTX2_GPU_ANTHROPIC_API_KEY` to enable it; without it the step prints a notice and skips. It catches two heads, a drawn sword,
   uncovered legs, merged characters, garbled text, wrong optics. It cannot hear audio or see motion between its 4 frames.
4. **You**: audio, lip sync, and motion (a second head appearing for a few frames).

## 11i. Run 4 (RTX 4090 24 GB, standard tier of the previous manifest, 13/13): rates and what mattered

| Task | Warm time (average) | Cold first use |
|---|---|---|
| Image, 1664x928 or 928x1664, Qwen-Image 20B, 30 steps, CFG 4 | **60.8 s** (59.8 to 61.5, n=4); was 30 s at 4 steps | 357 s (+30.7 GB download) |
| Video 5 s clip | 60.5 s (12.1 s per video-second) | |
| Video 10 s | 89.9 s (9.0 s per video-second) | |
| Video 12 s | 105.2 s (8.8 s per video-second) | |
| Video 15 s, 704x1280 | 126.6 s (8.4 s per video-second) | 484 s for the first video (+42.1 GB) |

Suite 27.3 min and about $0.22 of GPU time; the whole job (rent, deploy, update, suite, hold, cleanup) cost $0.52. The 4090 is as fast as the 5090 here
because Wan2GP runs heavily offloaded (VRAM peak 1.6 to 3 GB of 24 GB).

**What changed to get acceptable quality (in order of importance):** (1) Qwen-Image sampling: 4 steps with no guidance became 30 steps, CFG 4 and a
negative prompt; (2) prompts rewritten to ask for bright, crisp, evenly lit scenes (the old ones asked for dark studios, haze and backlight); (3) image-to-video from the better
start frames. The Wan2GP update ran, but the same models were used (Qwen Image 20B, LTX Distilled 1.0), so it was not the cause.

**`negative_prompt` and CFG** matter for Qwen-Image (true CFG: the negative prompt is the "do not" branch; Wan2GP's changelog says Qwen was "dying for a negative prompt").
They do not matter for the distilled LTX video model (CFG 1), which ignores the negative prompt unless NAG is enabled. So: always on for images, off by default for video, on for the Dev models.

**Prompt Enhancer:** not used. It is built into Wan2GP (a local Qwen language model, several GB, downloaded to the box; `enhancer_enabled` and `prompt_enhancer_quantization` appear in the box's config), but our settings never request it
and the log shows no enhancer model loading. We author long structured prompts instead (case text + base + rules).

**Other settings found in the box's `wgp_config`** (printed in the log; none changed yet): `transformer_quantization` int8 and `text_encoder_quantization` int8 (a possible small quality cost);
`profile` 4 (low-VRAM mode: speed, not quality); `image_output_codec` jpeg_95 (lossy: use PNG in the UI Config tab when judging detail);
`video_output_codec` libx264_8 (8-bit H.264; a 10-bit option may reduce banding); `audio_output_codec` aac_128. Quiet sound is a level problem, not a codec one: loudness is now measured and the delivery copy normalised.

## 12. UNVERIFIED (do not rely on these until the first real run confirms them)

| # | Claim | Confidence | How it gets verified |
|---|---|---|---|
| 1 | ~~The 19B LTX-2 pre-download is what Wan2GP uses~~ **REFUTED by run 37297908189**: it fetched its own 42 GB | resolved | `ckpts_downloaded_mb` = 42130 |
| 2 | Wan2GP auto-downloads its own files: **confirmed**. That its mirrors are ungated is still unproven (no `hf_access_errors`, but `HF_TOKEN` was not checked in that shell) | medium | repeat with the token unset |
| 3 | Settings JSON values (`model_type` names, 1280x704, 8 steps, 121 / 721 frames, 24 fps) work as written | medium | first render run; otherwise export from the UI |
| 4 | LTX-2 accepts a 721-frame (30 s) clip in one call | low | `video_30s` status and `duration_s` |
| 5 | `qwen_image_20B` for the image case may trigger its own large download | medium | `ckpts_downloaded_mb` on the `image` case |
| 6 | Wan2GP's quantised files do not visibly degrade LTX output | low | compare outputs by eye |
| 7 | `credit` is the right field in `vastai show user --raw`; billing lag is small | medium | `runs.csv` `credit_before` / `credit_after` not blank |
| 8 | Artifact per-file limit (a single third-party page says 500 MB); account quota depends on plan and repo visibility | low | your account's Actions usage view |
| 9 | ffmpeg log format `black_duration` / `freeze_duration` | medium | `black_s` / `frozen_s` on a real file |
| 10 | Beyond 5 min boot means a corrupt host (our data cannot show it: boxes were killed at 300 s) | low | only by letting one run longer |
| 11 | ~~`bootstrap.sh` symlink layout matches what Wan2GP expects~~ **moot**: symlinks are no longer created | resolved | n/a |
| 12 | Wan2GP sliding-window continuation limits for long single outputs | low | not tested |
| 13 | The 7-day cooldown on 6 machines is unwarranted (their `deploy_failed` rows look like script bugs, not host faults) | judgement | drop `deploy_failed` from `BAD_RESULTS` if you agree |
| 14 | GitHub menu paths for visibility and quota | medium | menu names may differ by plan |
| 15 | The workflow has been executed end to end | **partly done**: first run reached the render step | the render, the Triton patch and the new `log_attempt.py` healing have not run on a real box yet; the scripts were tested on stubs and on your real logs |
| 16 | The Triton workaround (`.value` indexing in `_split_rope`) fixes the crash | medium-high: run 2 passed on the same image after failing in run 1; the `PATCH` line itself was not in the logs sent | `env.triton_patch` in `results.json` (now printed) |
| 17 | ~~Triton 3.3.1 is the version on the box~~ **confirmed**: torch 2.7.1+cu128, triton 3.3.1, mmgp 3.8.2, gradio 5.29.0 | resolved | `env.packages` |
| 18 | The patch also works on an RTX 4090 and A100 | unknown (both successes so far were RTX 5090) | one run on a 4090 |
| 19 | `image.json` failed because of a missing closing quote | medium (the lint reproduces the identical message by truncating the string) | the lint step on your repo copy |
| 20 | Case timeouts of 1500 / 1500 / 3000 s cover the first-use downloads on slow hosts | medium (run 2 used 291 / 416 / 186 s at about 1 Gbps; a 400 Mbps host would take roughly 2.5x on the download part) | `elapsed_s` vs timeout |
| 21 | The 30 s clip is visually continuous across its two sliding windows | unknown | watch the seam near 20 s; `frozen_s`, `black_s` do not measure continuity |
| 22 | Seedance 2.0 capabilities (a third-party wiki, not the technical report) | low | primary source: seed.bytedance.com |
| 23 | General-practice items in 11e (structured prompts, seeds, CRF re-encode) improve quality here | medium, untested on this stack | `standard` tier comparisons (section 11f) |
| 24 | Settings keys `seed`, `image_start`, `image_prompt_type: "S"` are honored by `--process` JSON files | medium (docs name `image_start` + `S`; `seed` is inferred from the output file names) | `seed_honored`; `vid_05` result |
| 25 | Wan2GP accepts `1664x928` (Qwen), `2048x1152` (Qwen) and `1920x1088` (LTX) | low | the corresponding cases; an OOM or error there is a finding |
| 26 | Model names `z_image*`, `ltx2_22B*` (DEV), `t2v_2_2*` exist in `defaults/` | low | `env.model_catalog`; those cases are SKIPPED otherwise |
| 27 | A newline-separated prompt maps to one prompt per sliding window (`vid_16`) | low | the result; the case is exploratory |
| 28 | Time and cost estimates for `standard` and `full` | low (only smoke is calibrated) | `suite_elapsed_s`, `cost_usd` |
| 29 | `libx264` and `aac` exist in the box's ffmpeg for the delivery re-encode | medium | `output.delivery` field; `DELIVERY=0` skips it |
| 30 | Run-1 and run-2 weights (LTX 42 GB, Qwen 31 GB) are all that `smoke` and `standard` download | medium | `wan2gp_fetched_own_weights_mb` |
| 31 | `928x1664` / `1664x928` (Qwen) and `704x1280` (LTX) are accepted | medium (standard aspect-ratio sizes, multiples of 32) | the cases; an error names the problem |
| 32 | Image-to-video from `I10` can show the inside of a blood vessel (`V10b`, `V10c`) | low | the clips; likely partial at best |
| 33 | LTX speaks the quoted English lines (`V07`, `V08`, `V10b-c`) with usable lip sync | low-medium | listen |
| 34 | A 704x1280 vertical 15 s clip stays within VRAM and time (`V08`) | medium | `peak_vram_mb`, `elapsed_s` |
| 35 | The case-name label burns into `delivery.mp4` | medium (needs `drawtext`/freetype in the box's ffmpeg; skipped silently otherwise) | `delivery_labelled` |
| 36 | Hard cuts in `V10` look acceptable; `.concat` re-encode keeps audio in sync | medium | the seam strip; listen at 5 s and 10 s |
| 37 | Qwen-Image at 30 steps / CFG 4 / negative prompt looks good; `guidance_scale` and `negative_prompt` are honoured by the headless JSON | medium | `I01`..`I10` vs `L01`; `L02` shows Wan2GP's own defaults |
| 38 | Wan2GP's own defaults for `qwen_image_20B` are not already 30 steps / CFG 4 (we may be changing little) | unknown | `env.model_defaults` in `results.json` |
| 39 | `guidance_phases: 1` selects LTX single-phase; `NAG_scale` enables NAG for the distilled model | low | `phases_seen`; a visible difference in `L15` |
| 40 | `wan2gp_update=true` works on this image (git or tarball overlay, requirements, startup check) | low-medium | the `UPDATE:` line in `deploy.log`; rolls back if the startup check fails |
| 41 | Newer Wan2GP needs a newer torch/CUDA than the image's 2.7.1/cu128 | unknown (upstream recommends torch 2.10/cu130) | the `UPDATE:` startup check |
| 42 | Model names in the lab (Qwen Image 2.1, Krea 2, Z-Image Turbo, Flux 2 Klein 9B, Distilled 1.1, LTX 2.5) match by name regex | medium | `MODELS:` line in the log lists what exists; unmatched cases are SKIPPED with a hint |
| 43 | Quality improvement from the rewritten prompts alone | medium-high | `I01` vs old run; `L16` vs old waterfall |
| 45 | The vision-model review (qa_review.py) works against the real API with model `claude-sonnet-5-5`; tested only against a local mock | medium | a run with the secret set |
| 46 | LTX produces two-character dialogue with correct turn-taking and lip sync (`P01`, `P09`) | low | listen |
| 47 | Singing with clear lyrics and a slow zoom-in from wide to full body (`V20`, `P07`) | low | watch and listen; lyrics are original (the named song's lyrics are copyrighted and not reproduced) |
| 48 | `loudnorm` to -16 LUFS makes the delivery copy sound right | high (standard EBU R128 filter) | listen |
| 49 | Changing `image_output_codec` / `video_output_codec` in the UI config improves detail | medium | the UI Config tab; the key values then show in `env.wgp_config` |
| 44 | The blood-biology narration is accurate and what the model draws matches it | low | a teacher or doctor's review; the clips |

## 13. Files to add or edit (one checklist)

Copy into the repo and commit to the branch you dispatch from. Do not copy `attempts.csv` (your live file is migrated in place).

| Path | Action |
|---|---|
| `.github/workflows/ltx2-gpu.yml` | **replace** (short inputs, fixed settings, UI URL at HTTP 200, suite file input, QA step) |
| `ltx2-video-gpu/scripts/render_test.py` | **replace** |
| `ltx2-video-gpu/scripts/bootstrap.sh` | **replace** |
| `ltx2-video-gpu/scripts/run_remote_suite.sh` | **replace** |
| `ltx2-video-gpu/scripts/lint_suite.py` | **replace** |
| `ltx2-video-gpu/scripts/log_attempt.py` | **replace** |
| `ltx2-video-gpu/scripts/pick_offers.py` | **replace** |
| `ltx2-video-gpu/scripts/qa_review.py` | **add** |
| `ltx2-video-gpu/render_suite/cases.json` | **replace** |
| `ltx2-video-gpu/render_suite/cases-pratical-utube.json` | **add** |
| `ltx2-video-gpu/ltx2-video-gpu-readme.md`, `README.md` | **replace** |

Unchanged: `preflight.sh`, `probe_bandwidth.sh`, `start.sh`, `download_weights.py`. Delete the old `ltx2-video-gpu/render_settings/` if it still exists.
Optional secret: `LTX2_GPU_ANTHROPIC_API_KEY` (enables the automatic mistake review).

Your repo's workflow differs from this one in small ways (for example the probe sampling). Diff before replacing, or take the whole file and re-apply your probe edits.