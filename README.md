# ltx2-video-gpu: full picture

Goal: rent a Vast.ai GPU box that is **ready fast**, run image + video generation on it, bring the
results back, **destroy the box**, and remember which hosts were good.

Contents: 1 Files · 2 Flow · 3 Inputs · 4 State · 5 Host memory · 6 render_test.py · 7 Libraries ·
8 Failure reasons · 9 Spend log · 10 Runbook · 11 Questions and answers · 12 UNVERIFIED · 13 Files to add or edit

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
│   └── cases.json                      THE test manifest: 10 images + 10 videos (+ 3 sub-clips, 1 stitch) and 4 optional extras
└── scripts/
    ├── pick_offers.py                  runner: rank live offers, learn from attempts.csv
    ├── probe_bandwidth.sh              box: real throughput to Hugging Face
    ├── preflight.sh                    box: GPU, VRAM, disk, python, HF reachable
    ├── bootstrap.sh                    box: find python+torch, (optional) pre-download, Triton patch
    ├── download_weights.py             box: OPTIONAL pre-download (off by default, PREDOWNLOAD=1)
    ├── start.sh                        box: start Wan2GP on :7860
    ├── render_test.py                  box: run the suite, collect stats, thumbnails, report
    ├── run_remote_suite.sh             runner: start render_test.py detached on the box, stream its output
    ├── lint_suite.py                   runner: free check of cases.json before any GPU is rented
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

## 3. Inputs (all have defaults; for a normal run only choose `action`)

| Input | Default | Meaning |
|---|---|---|
| action | search | `search` ranks only (free). `run` does everything. `destroy_all` kills all instances |
| gpu_names | 4090,5090,3090,A100 | candidate GPUs; ranking picks among them |
| max_ready_min | 30 | wide pre-filter on the ESTIMATED ready time. Estimates are rough; the real gates are the 5 min boot timeout and the probe |
| min_real_mbps | 800 | bar for the on-box probe. `auto` derives it from max_ready_min |
| max_dph | 0.60 | $/hr ceiling incl. disk |
| render_test | false | run the render suite after HTTP 200 |
| suite_tier | smoke | `smoke` 3 cases, `standard` your 10 images + 10 videos (23 cases), `full` + 4 extras (see 11f) |
| predownload | false | also pre-download the old 67 GB set (never used by Wan2GP) |
| hold_min | 0 | keep box up N min for manual use (the render test stops the UI, so use one or the other) |
| wan2gp_image | pinned tag | change only deliberately |

The **model is not an input**: it comes from `model_type` inside each case of `render_suite/cases.json`.
The workflow runs on manual dispatch only. A cron `schedule` event carries no inputs, so every
`if: inputs.action == ...` step would be skipped; scheduling needs a fallback-to-defaults change.

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

## 11f. The test suite: your 10 images + 10 videos

Goal: see what this pipeline produces at its best, how long each thing takes, and what fails, using real briefs.
Everything is in `render_suite/cases.json`. Sub-clips (`V10a-c`) are internal steps of the stitched `V10`.

### Rules the lint enforces before any GPU is rented
At most 10 image and 10 video deliverables in `smoke` + `standard`; every deliverable video is 10 to 15 s (frames at 24 fps,
`8n+1`); every case has a unique ID and a name that starts with it; an image-to-video case points at an earlier image case.

### Tiers (estimates; only the download and the 30 s speed are measured)

| Tier | Cases | What it is | Est. wall time | Est. GPU cost |
|---|---|---|---|---|
| `smoke` | 3 | `I10` toddler image, `V10a` 5 s image-to-video, `V04` 10 s text-to-video | about 15 min (mostly the two model downloads) | about $0.3 |
| `standard` | 23 = 10 images + 12 clips + 1 stitch | your set | about 40 to 55 min | about $0.4 to $0.6 |
| `full` | 27 | `standard` + 4 optional extras beyond your cap (`I11` Ghibli quote text, `I12` start-up infographic, `I13` second seed, `V11` 1080p) | about 60 to 75 min | about $0.6 to $0.8 |

How the `standard` estimate is made: two cold downloads (Qwen-Image 31 GB + LTX 42 GB) about 10 min; 10 images at about 40 s;
116 video-seconds at the measured warm 6.2 s per video second about 12 min; re-encodes and stitch about 3 min; setup about 4 min.

### Concept to image to video (the matching table)

| concept | origin | image | video(s) | tier |
|---|---|---|---|---|
| C01 Ghibli traveler | mine | **I01** 1664x928 | **V01** i2v 10 s 1280x704<br>**V11** i2v 10 s 1920x1088 | standard |
| C02 Sneaker product | mine | **I02** 1664x928 | **V02** i2v 10 s 1280x704 | standard |
| C03 Neon city night | mine | **I03** 1664x928 | **V03** i2v 10 s 1280x704 | standard |
| C04 Portrait | mine | **I04** 928x1664 | - | standard |
| C05 Father braids hair | pasted | **I05** 1664x928 | **V05** i2v 12 s 1280x704 | standard |
| C06 Dogs in a taxi | pasted | **I06** 1664x928 | **V06** i2v 10 s 1280x704 | standard |
| C07 Baby lawyer in court | pasted | **I07** 1664x928 | **V07** i2v 12 s 1280x704 | standard |
| C08 AI avatar presenter | pasted | **I08** 928x1664 | **V08** i2v 15 s 704x1280 | standard |
| C09 Ganesha and mouse army | pasted | **I09** 928x1664 | **V09** i2v 12 s 704x1280 | standard |
| C10 Toddler and blood story | pasted | **I10** 928x1664 | ***V10a** i2v 5 s 704x1280*<br>***V10b** i2v 5 s 704x1280*<br>***V10c** i2v 5 s 704x1280*<br>**V10** stitch of V10a, V10b, V10c = 15 s | standard |
| C11 Waterfall | mine | - | **V04** t2v 10 s 1280x704 | standard |
| X1 Ghibli quote poster | pasted | **I11** 928x1664 | - | full (extra) |
| X2 Infographic | pasted | **I12** 1664x928 | - | full (extra) |

Only in `full` (beyond your cap): `I11`, `I12`, `I13` (second seed of `I05`) and `V11` (the 1080p version of `V01`).

`*italic*` clips are internal sub-clips. Resolutions follow the pasted prompts: `--ar 16:9` becomes 1664x928 (images) and 1280x704
(videos); `--ar 9:16` becomes 928x1664 and 704x1280. Midjourney flags (`--v 6.0`) are dropped; Qwen-Image does not use them.

### How a prompt is matched to its output
- Every case has an ID (`I05`, `V05`) and every file is named `<ID>_<slug>.<ext>`.
- `<case>/<ID>_<slug>.txt` holds the full prompt, settings, mode and (for image-to-video) the source image ID.
- `delivery.mp4` has the case name burned into its top-left corner (when the box's ffmpeg has `drawtext`; otherwise it is skipped silently).
- `id_map.md`: one row per concept, image to video. `index.html`: grouped by concept, prompt inside each card.
- `results.json` has `id`, `concept`, `mode`, `from_case`, `origin` per case.

### The two special examples
- **AI digital avatar (`I08` then `V08`)**: a fictional presenter is generated first (`I08`, 9:16), then `V08` is image-to-video with 15 s of
  spoken script, lip sync and gestures. The script is a neutral "try open models" tip (the pasted reel's product claims were not repeated).
  What you check by ear: clarity, lip sync, language. `silent_s` flags dead audio but cannot judge speech.
- **Stitched 15 s from three 5 s clips (`V10`)**: `V10a` (the pasted toddler yawn prompt), `V10b` (red blood cells) and `V10c` (white
  blood cells and platelets) all start from the SAME image `I10`, then ffmpeg joins them with hard cuts (re-encoded, CRF 17). The
  result records the seams (5 s, 10 s) and a strip of the frame before and after each cut.
  Whether image-to-video can go "inside the body" from a bedroom still is exactly what this case tests; it may well not.

### What to look at after a run
1. Run Summary: `report.md` (ID, mode, source, time, $, VRAM, output, quality flags).
2. Artifact: `index.html` by concept; `id_map.md`; `results.json`.
3. By eye and ear (nothing automatic judges these): `I03` sign spelling, `I04` skin and hands, `V07` and `V08` speech and lip sync,
   `V05` hands and hair, `V06` props staying on the dogs, `V09` crowd consistency, `V10` seams, and image-to-video vs text-to-video (`V04`).
4. Numbers for real jobs: warm `render_s_per_video_s`, `peak_vram_mb`, cold download MB per model, `delivery_mbps` vs original.

### Changing the suite
Edit `cases.json` (`id`, `name` = id + slug, `kind` image / video / stitch, `tier`, `settings`, optional `needs`, `parts`, `model_candidates`,
`timeout_s`, `internal`). The lint runs first and rejects anything outside your 10 + 10 and 10 to 15 s rules.

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

## 13. Files to add or edit (one checklist)

Copy these into the repo, commit to the branch you dispatch the workflow from.

| Path | Action | What it is |
|---|---|---|
| `.github/workflows/ltx2-gpu.yml` | **EDIT (replace)** | adds `render_test` and `predownload` inputs, credit before/after, pull-logs, artifact upload, `log_attempt.py` call, lint step, hold restarts the app; `weights_gb` 73, `min_real_mbps` 800, `max_ready_min` 30 |
| `ltx2-video-gpu/scripts/pick_offers.py` | **EDIT (replace)** | adds proven-host ranking and the proven count in the summary |
| `README.md` | **EDIT (replace)** | one-line pointer to this file |
| `ltx2-video-gpu/ltx2-video-gpu-readme.md` | **ADD** | this file |
| `ltx2-video-gpu/scripts/render_test.py` | **ADD / replace** | the suite runner (tiers, thumbnails, delivery re-encode, reports, failure policy) |
| `ltx2-video-gpu/scripts/run_remote_suite.sh` | **ADD** | starts the suite detached on the box and streams its output |
| `ltx2-video-gpu/scripts/lint_suite.py` | **ADD / replace** | enforces 10 + 10, 10 to 15 s, IDs, stitch parts before renting |
| `ltx2-video-gpu/scripts/log_attempt.py` | **ADD / replace** | writes `attempts.csv` + `runs.csv`; heals duplicated files; stores the render root cause in `fail_note` |
| `ltx2-video-gpu/scripts/bootstrap.sh` | **EDIT (replace)** | pre-download off by default; idempotent Triton workaround |
| `ltx2-video-gpu/render_suite/cases.json` | **ADD / replace** | 10 images + 10 videos with IDs, avatar, stitched example, 4 optional extras |
| `ltx2-video-gpu/render_settings/` | **DELETE** | replaced by `render_suite/`; the old 3 JSON files are cases `img_01`, `vid_01`, `vid_02` now |

Do **not** overwrite your live `ltx2-video-gpu/attempts.csv` with the copy in the download: it is an
unchanged snapshot of the zip, and the workflow migrates your real file in place on the next run.

Unchanged, leave alone: `scripts/download_weights.py` (kept for `PREDOWNLOAD=1`), `scripts/preflight.sh`,
`scripts/probe_bandwidth.sh`, `scripts/start.sh`.

**Workflow caution:** your repo's workflow and `probe_bandwidth.sh` already differ from the ones I delivered (the logs show
`SAMPLES=` and a 600 Mbps bar), so do not blindly replace them. Apply these changes to your copy, or diff against
the delivered `ltx2-gpu.yml`:
1. Step "Credit after + spend": `source /tmp/run_stats.env` becomes `set -a; source /tmp/run_stats.env; set +a`.
2. New step before "Credit before": "Lint render suite (free)" (runs `scripts/lint_suite.py`), copied from the delivered file.
3. Timeouts: render step `timeout-minutes: 170`, job `timeout-minutes: 260`; artifact `retention-days: 14`.
4. Hold step: restart the app (`ssh ... 'cd ~ && nohup bash start.sh ...'`) when `render_test` is true, then wait for HTTP 200.
5. New inputs `predownload` (+ `export ... PREDOWNLOAD=$PREDL` in the Deploy ssh command) and `suite_tier`; `weights_gb` default 73.
6. The render step is replaced: it copies `render_test.py` + `render_suite/`, calls `scripts/run_remote_suite.sh`, pulls `results/`, appends `report.md` to the run Summary.

Created automatically by the workflow (do not add by hand): `ltx2-video-gpu/runs.csv`,
`ltx2-video-gpu/render_runs/*.json`, and the new `attempts.csv` columns.