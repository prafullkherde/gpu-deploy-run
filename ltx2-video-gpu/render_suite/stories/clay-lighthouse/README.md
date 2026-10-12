# Story: clay-lighthouse

Comic 30 s clay stop-motion short: lighthouse keeper + terrier argue with the tide.

## Folder map (maps to cases-real-format.json)

| Path | Role | cases-real-format field |
|------|------|-------------------------|
| `bible.json` | Style, characters, locations, locks | `bible` |
| `assets/characters/` | Profile locks (front/side) | `bible.characters.*.ref_imgs` |
| `assets/keyframes/` | Start frame per shot | `shot.start_frame` |
| `shots/S0N_*.json` | One shot = one 10 s clip | `shot` object |
| `stitch.json` | Order + final delivery name | (production pack) |
| `logs/` | Suite logs only (not inside every shot) | — |

## Characters

- **Keeper** — tall lanky clay, ginger moustache, wire glasses, yellow raincoat. Deadpan.
- **Terrier** — short round clay dog on two legs, green hat, red scarf. High energy.

Existing seed image in repo: `render_suite/video-assets/uncle_dog_clay_pair.jpg`
Copy the best approved still to `assets/keyframes/S01_kitchen_start.jpg`.

## Shots

1. **S01 kitchen** — tide is late; terrier proposes telling the sea; they leave.
2. **S02 doorway** — outside; terrier thinks a wave waved; keeper corrects.
3. **S03 clifftop** — they shout at the sea; a wave actually rolls in.

## Artifact policy

- **Ship:** `delivery/` + top-level `logs/` + locked assets + JSON.
- **Do not ship:** raw `out/` (high bitrate model output).
- On the GPU box: generate `out` → QA → encode `delivery` → delete or leave `out` on the ephemeral box (never pull back).

## Character images: git vs on-the-fly

- **Approved locks** (front/side, and approved keyframes) → **commit to git** under `assets/`.
- Generate on the box when missing; after QA, pull only the approved stills back into git.
- Do not commit every failed attempt.

## How to run (immediate path, no base-code change)

1. Pull latest branch.
2. Copy this `stories/clay-lighthouse/` tree into the repo.
3. Seed S01 keyframe from existing asset or one suite run of PI01.
4. Run practical suite (or single i2v cases) using the compiled prompts in each shot file.
5. After run: keep only delivery mp4s + logs; copy approved keyframes into `assets/keyframes/`.
6. Later: optional code to load story folders automatically.
