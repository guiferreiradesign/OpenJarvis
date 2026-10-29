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

## Phase 3: jobs, status, notifications, revisions (done)

Goal: renders (and later other long-running skills) run as **jobs** that
are tracked from submission to a final status, can be cancelled and
retried safely, notify you, and support revisions. Approved decisions:
D19–D24 and D27 in DECISIONS.md; design notes D25–D26 and D28–D33. Details: [jobs.md](jobs.md).

| Step | Scope | Status |
|---|---|---|
| 1 | Job store and status rules: SQLite (WAL), schema version, generic job model, allowed transitions, atomic claiming, `[jarvis.jobs]` config | **Done** |
| 2 | Single-job runner for `video.render`: calls `media_render` then `media_validate`, classifies failures, retries only temporary ones | **Done** |
| 3 | Worker and CLI: submit, list, show, cancel; single-worker lock; crash recovery (heartbeat); retry back-off; leftover temp-folder cleanup | **Done** |
| 4 | Progress from FFmpeg `-progress`, in job status and on the event bus | **Done** |
| 5 | Notifications on final status: log + macOS, from a crash-safe outbox | **Done** |
| 6 | Revisions (complete new plan, child job), verified reuse of identical finished outputs, resuming a job's own published render | **Done** |
| 7 | Agent tools `media_job_submit`, `media_job_status`, `media_job_revise`, with RBAC tests | **Done** |

Out of scope for Phase 3: writing plans from a brief, the video-editing
skill itself, HyperFrames captions/graphics, external notification
channels, parallel jobs, per-clip caching.

## Phase 4.1: speech providers (done 2026-10-26; foundation only)

Goal: replaceable speech input and output for the coming interface,
without touching the agent or Phase 3. Details: [speech.md](speech.md);
decisions D34–D37.

Delivered:

* Speech input: `MlxWhisperBackend` (upstream `SpeechBackend`, registry key
  `mlx-whisper`), `mlx-community/whisper-base-mlx`, English, vocabulary
  prompt `JARVIS, Voicebox, Kokoro, George, Phase 4.1 roadmap`, raw
  transcript (no LLM clean-up). Lazy, explicit `load()`; the model stays
  loaded; in-process WAV decoding; serialised inference; cold and warm
  timings reported separately.
* Speech output: `VoiceboxTTSBackend` (upstream `TTSBackend`, key
  `voicebox`) for the local Voicebox service, Kokoro engine, George voice
  (`JARVIS` profile), loopback URLs only, `personality` always off.
* `[jarvis.speech_input]` / `[jarvis.speech_output]` settings;
  `python -m jarvis_ext speech-check | speech-transcribe | speech-say`.
* 125 mocked tests (no model, MLX, microphone or Voicebox needed). Text-only
  JARVIS imports no speech code.
* Measured on this Mac (Python 3.13, mlx-whisper 0.4.3): model load
  ≈ 2.5 s, first transcription ≈ 0.5 s, then ≈ 0.21 s for 16.8 s of
  speech; Voicebox ≈ 2.4 s per sentence once warm.

Update 2026-10-29 (D40): speech output now targets VoiceStudio with Kokoro
George (`VoiceStudioTTSBackend`, `[jarvis.voicestudio]`); see
[voicestudio-plan.md](voicestudio-plan.md).

Not chosen: Whisper Large-v3-Turbo (memory pressure on 8 GB) and Voicebox's
own transcription (≈ 3–4 s versus ≈ 0.2 s).

Dependency (approved, D37): optional `speech-mlx` extra,
`mlx-whisper==0.4.3` for Apple Silicon only, sharing the locked mlx
0.31.2 with `inference-mlx`. Install with `uv sync --extra dev --extra
speech-mlx`; the model weights download separately on first use.

## Voice and local-first track (current order of work)

Status note: this file's "Next milestone" section below was written before
the interface work. The branch `jarvis/phase-4.2-interface` already has
commits "Build Phase 4.2 JARVIS interface" and "Fix cloud provider tool-call
normalization", and `frontend/src/lib/tts.ts` plus `/v1/speech/synthesize`
already give one-utterance read-aloud. Those sections and the README
capability table have not been brought up to date; do that when the interface
is next documented. Nothing below depends on it.

Principles: the brain may be a hosted API (configured, switchable, never
required to be local); listening, wake word and speech generation are local
and are never permanent cloud services; Ollama is not a prerequisite and is
never started automatically; nothing records or streams audio to a cloud
service in the background.

**Done: preflight foundation.** `python -m jarvis_ext preflight` and the
verification rule ([preflight.md](preflight.md), D38). Every step below
replaces its `WARN: NOT IMPLEMENTED` placeholder in
`preflight/checks/future.py` with real checks in the same change.

| Order | Step | Status | Done when (verified by) |
|---|---|---|---|
| 1 | VoiceStudio speech output: voice adapter, profiles, audio cache, sentence chunking, interruption, graceful failure ([voicestudio-plan.md](voicestudio-plan.md)) | **In progress:** adapter done 2026-10-29 (VoiceStudio, Kokoro George `bm_george`, British English, sent on every request; D40). Profiles, cache, chunking, streaming and interruption still planned | Unit tests with a fake service, `future.voice_adapter` and `future.speech_output` pass, JARVIS answers in text with the service stopped, no cloud TTS key is consulted |
| 2 | Local Whisper in the voice loop (MLX Whisper provider exists; the loop does not) | Planned | Model loaded once, transcript reaches the agent, measured latency inside a budget, no audio leaves the machine |
| 3 | Wake word, push-to-talk and interruption | Planned | Push-to-talk first; wake word only if it runs fully offline; speaking stops when the user talks or presses a key |
| 4 | Persistent Markdown notes with immediate indexing | Planned | A note written is found by search in the same session without a restart |
| 5 | Explicit local/cloud brain switching with a visible indicator | Planned | The active brain is always shown; switching is a user action, never automatic; the preflight `brain.*` checks cover both |
| 6 | Local-mode privacy verification and network auditing | Planned | In local mode a network guard shows no outbound traffic except loopback; the check fails if any appears |

Constraints for all steps: no Ollama auto-start, no always-on cloud audio,
no secrets in logs/frontend/docs, no paid or quota-consuming calls to test.

## Next milestone: JARVIS First Usable Interface — Web, Text and Voice

Not started. This is the first version meant for everyday use: talk to
JARVIS by text or voice in a browser, watch and review its work, and
approve what it may do. It brings forward the voice part of Phase 6 and
the dashboard of Phase 7; the rest of those phases, and Phases 4–5, keep
their place.

**Scope**

* A polished, responsive web interface (desktop and phone browsers).
* Text chat and voice from the start: **push-to-talk** input and spoken
  replies, with the text always shown.
* A task area: jobs with status, stage, progress, history and finished
  results (preview, thumbnail, output version, `reused_from`, revisions
  as a parent/child chain), fed by the job store.
* Review and approval controls: play a preview, accept it, or ask for a
  revision; approve or deny actions that need confirmation.
* A skills area that grows as skills are added (video first), listing what
  each skill can do and the permissions it needs.
* Clear permissions, an activity history (who asked for what, which tools
  ran, what was approved) and safe approvals.

**Architecture (direction, not yet a commitment)**

* **Local-first.** The Mac is the first and only worker; the interface
  talks to a backend on the Mac over localhost (or a private tunnel later,
  Phase 5). No cloud service is required.
* **Backend:** the existing OpenJarvis FastAPI server (`jarvis serve`,
  `src/openjarvis/server/`) with its auth middleware, WebSocket/stream
  bridge and approval routes. Agents use the same tools, `ToolExecutor`,
  RBAC policy and confirmation rules as everywhere else; the job tools
  (`media_job_*`) are the interface's way into jobs, so no separate path
  bypasses validation, permissions or the worker. Job progress reaches the
  browser by reading the job store (and, in the worker process, the
  `jarvis_job_progress` event).
* **Frontend: to evaluate first.** Option A: extend upstream `frontend/`
  (React + Vite + Tailwind, already Tauri-ready); cheaper, but merges with
  upstream and its telemetry (`posthog-js`) must be reviewed and turned
  off. Option B: a small separate web app in the JARVIS tree talking to
  the same API; cleaner ownership, more work. Decide after a short spike.
* **Speech: interchangeable providers.** Chosen in Phase 4.1: MLX Whisper
  Base for input and the local Voicebox (Kokoro, George) for output, both
  behind upstream's `SpeechBackend` / `TTSBackend` interfaces (speech.md).
  Still to do here: microphone capture and push-to-talk, playback, loading
  the model once at start-up, selecting the providers in `jarvis serve`,
  and the transcript → agent → spoken reply loop. Paid providers only with
  your approval (D21's no-paid-API rule still applies).
* **Security:** the browser is just another client: authenticated,
  bound to localhost by default, and subject to the same per-tool
  capabilities, confirmation prompts and path-free results.

**Later extensions (recorded, not part of this milestone)**

* Packaging the web interface as a native Mac app, possibly with Tauri.
* Phone-call notifications for genuinely urgent email, finished tasks and
  other events you explicitly approve. This needs explicit permission,
  configurable categories, severity thresholds, quiet hours, duplicate
  prevention (the Step 5 outbox is a model), and a decision on what must
  keep running when the Mac is asleep or offline (likely the Phase 5
  cloud side).
* More independent skills beyond video editing, using the same job,
  tool and permission model.

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
* Voice: providers done in Phase 4.1 (MLX Whisper Base + Voicebox/Kokoro);
  the voice interface comes with the First Usable Interface milestone.
  Paid TTS only if approved.
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
