# JARVIS roadmap

Adjusted after the Phase 1 audit (AUDIT.md). Each phase should end with
something working and tested before the next phase starts. "Reuse" lists the
upstream parts to build on rather than rebuild.

## Phase 1: foundation (done in this branch)

* Audit of OpenJarvis, plus extension pattern (`src/jarvis_ext`, `[jarvis.*]` config).
* `media_workspace_inspect`: read-only, sandboxed listing and ffprobe metadata.
* Tests (mocked and real FFmpeg) and documentation.
* **Still to do on the Mac:** the checklist below.

## Phase 2: deterministic local video pipeline

Goal: "make a 45 s vertical cut from these clips" produces a versioned file
without touching the sources. No LLM creativity yet: a fixed, testable
pipeline.

* An `EditPlan` data model (JSON): source clip + in/out points, target aspect
  ratio, captions, branding overlay, output preset. The LLM will later
  *produce* plans; this phase *executes* them.
* `media_render` tool: validates a plan against `media_workspace_inspect`
  output, then runs FFmpeg with an argument list (no shell) into a separate,
  configured **output** folder (`[jarvis.media_workspace] output_root`). It
  writes versioned names (`reel_v001.mp4`), never overwrites, and refuses to
  write inside the source root.
* `media_validate` tool: ffprobe the result (duration within tolerance,
  1080×1920, codecs, audio present) and create a low-resolution preview and a
  thumbnail.
* Captions: local Whisper (`speech/faster_whisper.py` already exists) → SRT →
  burn-in. Branding: overlay a PNG from a configured assets folder.
* Reuse: `ToolExecutor` timeouts/capabilities, the `speech/` STT backends, and
  the Phase 1 path-safety code (extract a shared `jarvis_ext/_workspace.py`).
* Capability: `file:write` (output folder only); `requires_confirmation` for
  renders above a size or time threshold.

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
FFmpeg 9.0.2):

* 1–2 done. 116 of 117 tests pass. The one failure is the RBAC test, which
  needs the Rust extension (`rustup` + `maturin develop`, not installed).
  `test_unreadable_directory` ran and passed.
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
