# JARVIS roadmap

Adjusted after the Phase 1 audit (AUDIT.md). Each phase should end with
something working and tested before the next phase starts. "Reuse" lists the
upstream parts to build on rather than rebuild.

## Phase 1: foundation (done; verified on the Mac 2026-10-25)

* Audit of OpenJarvis, plus extension pattern (`src/jarvis_ext`, `[jarvis.*]` config).
* `media_workspace_inspect`: read-only, sandboxed listing and ffprobe metadata.
* Tests (mocked and real FFmpeg) and documentation.
* Mac verification: see the checklist status below. Items 4–6 are deferred,
  not blocking.

## Phase 2: deterministic local video pipeline (done 2026-10-25; real footage not yet tried)

Goal: "make a 45 s vertical cut from these clips" produces a versioned file
without touching the sources. No LLM creativity yet: a fixed, testable
pipeline. Details: [video-pipeline.md](video-pipeline.md).

Delivered:

* `EditPlan` (JSON, strictly validated): clips with in/out points in list
  order, preset (`instagram_reel`, 1080×1920 9:16 30 fps H.264/AAC), crop or
  pad, frame rate and encoder settings, optional `.srt` subtitles and PNG
  branding, duration tolerance.
* `media_render`: checks sources with the Phase 1 path rules and ffprobe
  (now shared via `jarvis_ext/_workspace.py` and `probe_media`). It renders
  in a private temp dir and checks the file before publishing it as
  `<name>_vNNN.mp4` plus a `.render.json` record in `[jarvis.media_render]
  output_root`. That folder is separate from the workspace (checked by
  inode); nothing is overwritten, and names are concurrency-safe.
* `media_validate`: checksum against the record, ffprobe checks (including
  per-stream durations), a full decode, then a 360×640 preview and a JPEG
  thumbnail in `review/`. Only this tool can report `ready_for_review`.
* Capabilities `file:read` + `file:write` (RBAC-tested); optional
  `require_confirmation`.
* 255 new tests, including real-FFmpeg integration on macOS.

Changed from the original plan:

* Output settings live in their own `[jarvis.media_render]` section (D10).
* Captions: supplied SRT is embedded as a track by default. Burn-in works
  only with an FFmpeg built with libass, which Homebrew's 9.0.2 is not.
  Local Whisper transcription is an extension point
  (`transcribe_to_srt`), not wired in: `faster-whisper` and a Whisper model
  are not installed and need your approval (D16).
* `requires_confirmation` is a single on/off setting, not a size/time
  threshold. OpenJarvis' executor only supports per-tool confirmation;
  per-job approvals belong with Phase 3 jobs.

### Mac verification checklist (Phase 2)

Done in this session (macOS 26.5.1 arm64, Python 3.13.17, FFmpeg 9.0.2):

* `uv run pytest tests/jarvis_ext`: 388 passed, 0 skipped (after the
  colour fix below; 372 before it).
* Wider suite (`uv run pytest tests/ -n auto -m "not live and not cloud and
  not hub"`, dev extras only): 9225 passed, 270 skipped, 9 failed, 5
  collection errors. The same 9 failures and 5 errors occur on the
  untouched Phase 1 commit `2ad2519c`. Eight failures and all 5 errors come
  from `fastapi` not being installed (server extras), and
  `test_convert_mlx_quantize` fails under `-n auto` only.
  `tests/tools/test_tool_timeout.py::test_repeated_timeouts_use_bounded_workers`
  failed in one of two full runs and passes alone and after the JARVIS
  tests. It is timing-sensitive under full parallel load.
* End-to-end CLI render + validate of generated test media (landscape +
  portrait clips, a file name with spaces, quotes, commas and brackets,
  a translucent PNG logo, embedded SRT). Frames were inspected visually
  (crop, logo position) and the subtitle track was extracted and checked
  for sanitised text.

First real-footage test (2026-10-25), with a temporary config pointing at
`~/Movies/JARVIS-Test-Media` (sources) and `~/Movies/JARVIS-Test-Renders`
(output):

* Sources: `IMG_1301.mov` (1920×1080, 30.03 fps, HEVC 8-bit full range),
  `vid.mov` (1920×1080, 60 fps, HEVC 8-bit full range), `demo-video.MOV`
  (1080×1920, 59.89 fps, HEVC 10-bit HLG/BT.2020).
* Plan: 5 s + 6 s + 4 s, crop, audio kept → 15 s `instagram_reel`.
* First attempt: **rejected before publishing** (output was `yuvj420p`, full
  range). Root cause and fix in DECISIONS.md D18. Nothing was published.
* After the fix: `phase2_test_reel_v001.mp4` rendered and validated. All
  18 checks passed, including 1080×1920, 30 fps, 15.0 s per stream, H.264
  High yuv420p, tv range, Rec.709 tags, AAC 48 kHz stereo, and full decode.
  The preview (360×640) and thumbnail were created. The HLG clip was
  tone-mapped (warning shown) and looked natural on inspection (a visual
  check of single frames, not a colour measurement). Source SHA-256, sizes
  and mtimes were unchanged.
* Not covered by this test: PQ HDR, HDR from other devices, pad framing,
  logo and subtitles on real footage, and playback on Instagram.

Still for you to do:

1. Watch `phase2_test_reel_v001.mp4` in QuickTime. Check framing, cuts at
   the requested times, audio sync across clips, and the HDR segment's
   colours.
2. Upload one render to Instagram as a private/draft Reel to confirm the
   format is accepted (manual; JARVIS does not post anything).
3. Try `fit: "pad"`, a logo and an `.srt` file on real footage.
4. Decide whether you want burned-in captions (needs an FFmpeg with
   libass) and/or local Whisper (`uv sync --extra speech` plus a one-off
   model download of about 75–145 MB).

## Phase 3: jobs, status, notifications, revisions (in progress)

Goal: renders (and later other long-running skills) run as **jobs** that
are tracked from submission to a final status, can be cancelled and
retried safely, notify you, and support revisions. Approved decisions:
D19–D24 and D27 in DECISIONS.md; design notes D25–D26, D28 and D29. Details: [jobs.md](jobs.md).

| Step | Scope | Status |
|---|---|---|
| 1 | Job store and status rules: SQLite (WAL), schema version, generic job model, allowed transitions, atomic claiming, `[jarvis.jobs]` config | **Done** |
| 2 | Single-job runner for `video.render`: calls `media_render` then `media_validate`, classifies failures, retries only temporary ones | **Done** |
| 3 | Worker and CLI: submit, list, show, cancel; single-worker lock; crash recovery (heartbeat); retry back-off; leftover temp-folder cleanup | **Done** |
| 4 | Progress from FFmpeg `-progress`, in job status and on the event bus | Planned |
| 5 | Notifications on final status: log + macOS | Planned |
| 6 | Revisions (complete new plan, child job) and safe reuse of identical finished work | Planned |
| 7 | Agent tools and documentation, with RBAC tests | Planned |

Out of scope for Phase 3: writing plans from a brief, the video-editing
skill itself, HyperFrames captions/graphics, external notification
channels, parallel jobs, per-clip caching.

## Phase 4: restricted Mac worker

* A small worker process on the Mac that exposes **only** registered JARVIS
  tools (media inspect/render/validate, later specific app actions). It never
  exposes a shell. It is authenticated and makes outbound connections only (no
  open ports on the Mac).
* Candidate transports already in the codebase: MCP (`mcp/`) or A2A (`a2a/`).
  Decide after a spike.
* Computer use (screenshots/clicks) is **new work** and the riskiest item.
  It should get its own design, an approval step for every action class, and
  ideally a separate macOS user.
* Test everything from Phases 1–3 on real footage here.

## Phase 5: affordable cloud orchestration

* A small VPS or container running `jarvis serve` (FastAPI, already present)
  plus the job store, behind authentication (`server/auth_middleware.py`
  exists) and a private network or tunnel (for example Tailscale) rather than
  public ports.
* Backups of the SQLite stores, health checks, log retention, and a monthly
  cost ceiling. Reuse the `deploy/` Docker/systemd files.
* Requires your approval: provider, monthly budget, and a domain if any.

## Phase 6: email, memory, voice, approvals

* Email drafting (not sending) through the existing channels/connectors.
  Sending stays behind approval.
* Personal memory: enable the existing fact store; seed preferences such as
  brand colours, caption style and reel length.
* Voice: local Whisper + local TTS (Kokoro) first; paid TTS only if approved.
* Approvals: generalise the existing proactive-agent approval queue
  (`tools/approval_store.py`, `server/approval_routes.py`).

## Phase 7: JARVIS dashboard

* Conversations, a job list with live status, logs, video previews,
  approve/deny buttons and feedback. It reads from the Phase 3 job store
  through the FastAPI server.
* Decide whether to extend upstream `frontend/` (React + Tauri) or build a
  separate small web app. Extending upstream is cheaper but conflicts more on
  merges.

## Phase 8: better editing and more specialists

* "Strongest moments": scene detection, audio energy, transcript keywords
  and face/speaker detection. The LLM ranks candidates and writes the
  `EditPlan`.
* An evaluation set of briefs → plans → human ratings, using OpenJarvis'
  `evals/` and trace-based learning.
* Further specialists (photo selection, document workflows) built with the
  same extension pattern.

---

## Mac verification checklist (Phase 1)

Nothing below could be run from the cloud container.

**Status 2026-10-25** (macOS 26.5.1, arm64, uv 0.13.5, Python 3.13.17,
FFmpeg 9.0.2, Rust 1.99.1 via Homebrew rustup):

* 1–2 done. All 117 `tests/jarvis_ext` tests pass once the Rust extension
  is built, including RBAC enforcement and `test_unreadable_directory`.
  `tests/security` gives 617 passed and 13 skipped; the skips are for
  missing server/crypto extras, not JARVIS.
* 3 done on three real HEVC clips in `~/Movies/JARVIS-Test-Media`: one
  iPhone 17 Pro portrait clip (Dolby Vision, 10-bit) and two iPad
  landscape clips. Resolution, orientation, codecs, audio format and
  duration match QuickTime's Inspector and AVFoundation. Frame rates differ
  slightly for variable-frame-rate clips (see media-workspace.md). The
  source files' SHA-256 hashes, sizes and mtimes were unchanged afterwards.
  No real audio-only file was tested.
* 4–5 not done (no external drive or iCloud folder used).
* 6 skipped: no local inference engine is installed. Apple FM needs a full
  Xcode for `apple-fm-sdk`.

1. `brew install ffmpeg uv`, clone the fork, `uv sync --extra dev`.
2. `uv run pytest tests/jarvis_ext -v`. Expect everything to pass,
   including `TestRealFfprobe` and `test_unreadable_directory`. The latter
   was skipped in the cloud because it ran as root.
3. Create a test workspace (copies of a few real clips, including an iPhone
   portrait video and an `.m4a`), configure `root`, and run
   `uv run python -m jarvis_ext media-inspect`. Check that durations,
   resolution, frame rate and **portrait orientation** match QuickTime's
   Inspector.
4. Check behaviour with the workspace on an external drive that is
   unplugged (expect a clean "does not exist" error) and on an exFAT drive
   (expect `._*` files to be skipped).
5. Check an iCloud Drive "optimised storage" folder: probing may download
   files or time out, so decide whether the workspace should be local-only.
6. Optional: install Ollama with a small tool-calling model and try
   `uv run jarvis ask --agent orchestrator --tools media_workspace_inspect
   "What footage do I have?"`.
