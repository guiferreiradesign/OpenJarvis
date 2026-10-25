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

* `uv run pytest tests/jarvis_ext`: 372 passed, 0 skipped.
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

Still for you to do (needs real footage, which tests must not use):

1. Create the output folder and config (see video-pipeline.md), copy two
   or three clips (one iPhone portrait clip) into a test workspace, and
   write a small plan with `media-inspect` durations.
2. `uv run python -m jarvis_ext media-render plan.json --validate`, then
   open the preview, thumbnail and full file in QuickTime. Check framing,
   cuts at the requested times, audio sync across clips, and the logo.
3. Check how the iPhone HDR (Dolby Vision, 10-bit) clip looks: no tone
   mapping is applied (see video-pipeline.md, limitations).
4. Upload one render to Instagram as a private/draft Reel to confirm the
   format is accepted (manual; JARVIS does not post anything).
5. Decide whether you want burned-in captions (needs an FFmpeg with
   libass) and/or local Whisper (`uv sync --extra speech` plus a one-off
   model download of about 75–145 MB).

## Phase 3: jobs, status, notifications, revisions

* Renders take minutes, so they run as **jobs**. Store a job record (id,
  brief, plan, status, outputs, logs, parent job for revisions) in SQLite,
  following the `scheduler/store.py` pattern.
* Reuse `scheduler/` for background execution and the event bus for progress.
  Send a completion notification through an existing channel (for example
  Slack, Telegram or email) once you choose one.
* Revisions: "make the intro shorter" creates a child job with a modified
  plan → `reel_v002.mp4`.

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
