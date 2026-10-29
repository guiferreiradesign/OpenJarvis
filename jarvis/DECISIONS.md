# Architecture decisions

Short records of decisions made during Phases 1–4.1, so they can be revisited
deliberately rather than by accident.

## D1. Custom code goes in a separate `jarvis_ext` package

**Decision:** JARVIS code lives in `src/jarvis_ext/`, not in `src/openjarvis/`.
It hooks in with one guarded import at the end of
`src/openjarvis/tools/__init__.py`.

**Why:** upstream merges stay almost conflict-free, and custom code is easy
to find. OpenJarvis has no entry-point plugin loader, so one import line is
the smallest possible hook. It also avoids editing the core security
inventory: `tests/security/test_capabilities.py` requires every tool under
`openjarvis/tools/` to be listed in `DEFAULT_TOOL_CAPABILITIES`, while tools
outside that package are governed by the capabilities they declare.

**Revisit if:** upstream adds a plugin/entry-point mechanism (it is on their
roadmap as "Plugin ecosystem"). The hook would then become a
`pyproject.toml` entry point.

## D2. Extension settings go in `[jarvis.*]` sections of the normal config file

**Decision:** settings live in `~/.openjarvis/config.toml` under
`[jarvis.<extension>]`, read by `jarvis_ext.config`.

**Why:** one config file for the user; no changes to the core config
dataclasses, because the core loader ignores unknown top-level sections
(tested). The file is located with the same precedence as the core loader
(`$OPENJARVIS_CONFIG`, then `$OPENJARVIS_HOME/config.toml`).

## D3. Tools load their own config and report config errors at call time

**Why:** OpenJarvis constructs tools with no arguments in five different call
sites. Raising in the constructor would break `jarvis tool list`, so problems
surface as a failed `ToolResult` with a clear message (the same approach
upstream `get_weather` uses).

## D4. The workspace is a separate allow-list from `[security] allowed_dirs`

**Why:** `allowed_dirs` governs `file_read`/`file_write`, and empty means
unrestricted. Media access should be narrower and must never default to "all".
An unset `root` means the tool refuses to run. Roots of `/` or the whole home
directory are rejected.

## D5. Paths are workspace-relative in both directions

The model never sees or supplies absolute paths. Future tools (render,
validate, export) should follow the same rule. This makes it straightforward
to move execution to a Mac worker later: the cloud side only ever talks in
workspace-relative paths, and the worker maps them to local folders.

## D6. Symbolic links are skipped unless explicitly enabled

**Why:** this is the simplest policy that cannot be escaped. When enabled,
links are accepted only if their fully resolved target is inside the root.

## D7. ffprobe is hardened, not just called

Argument list, no shell, `file:` URL, `-protocol_whitelist file`, a demuxer
whitelist, per-file timeout, total time budget and minimal environment. The
whitelist was added after confirming that a real ffprobe follows HLS
playlists disguised as `.mp4`.

## D8. No new dependencies

FFmpeg is an optional system tool. Without it, the tool lists files and adds
a warning. No Python packages were added.

## D9. Phase 2 reuses Phase 1 path safety and ffprobe through shared functions

**Decision:** the resolver moved from a method of `MediaWorkspaceInspectTool`
to `jarvis_ext/_workspace.py` (`resolve_relative`, `same_or_inside`), and
the hardened ffprobe call became the module function
`media_workspace.probe_media`. The inspect tool delegates to both; its
behaviour and tests are unchanged.

**Why:** one implementation of "is this path allowed" and "probe this file
safely", as the roadmap planned. `parse_ffprobe_output` also gained
per-stream `duration_seconds` (an additive field), which validation needs
(see D14).

## D10. Output settings get their own `[jarvis.media_render]` section

**Decision:** `output_root` lives in `[jarvis.media_render]`, not in
`[jarvis.media_workspace]` as the roadmap sketched.

**Why:** the workspace section is a read-only allow-list with strict
unknown-key validation. Keeping write settings in a separate section keeps
"what may be read" and "where may be written" separately reviewable, and
leaves Phase 1 configs valid unchanged.

## D11. The output folder must exist and be disjoint from the workspace, by inode

**Why:** auto-creating `output_root` would create a stray folder under
`/Volumes` when an external drive is unplugged. String comparisons are
fooled by APFS case-insensitivity and symlinked aliases, so overlap is
checked with `(st_dev, st_ino)` on every call, not just at config load.

## D12. Render in a private temp dir, check, then publish atomically

**Decision:** FFmpeg writes `render.mp4` (fixed name, `-n`) in
`<output>/.jarvis-tmp/job-*`. The file is probed and checked there, then
published with `link(2)` (fails on an existing name). There is an
`O_EXCL`-reservation fallback for filesystems without hard links. The next
version is "highest existing + 1"; a name taken in a race is skipped.

**Why:** no partially written or wrong file ever carries a final name; no
overwrite is possible even under concurrency; the temp dir is on the same
filesystem, so publishing is a metadata operation. Considered alternatives:
reserving a version before rendering (leaves empty placeholders on failure),
or `rename(2)` (silently overwrites on macOS).

## D13. Render success and validation are separate tools and statuses

**Decision:** `media_render` always returns `ready_for_review: false`, and
only `media_validate` can return `true`. Validation needs the
`.render.json` record and compares the file's SHA-256 with it.

**Why:** the brief requires that a failed validation never looks like a
ready video. The checksum ties validation to the exact bytes the render
produced, and detects truncation or replacement. The full decode pass is
in validation (not render) to keep renders fast; render still runs the
cheap probe-based checks before publishing.

## D14. Durations are checked per stream, not only per container

**Why:** found while testing. A truncated source with its index at the start
probes as 3.5 s, but only 1.3 s of video decodes. Audio is padded to keep
A/V sync, so the container still reported 3.5 s and the bad render passed
every check. Checking `video_duration` and `audio_duration` separately
catches it (regression test: `test_truncated_source_is_never_published`).

## D15. Clip order is list order; the plan carries no separate order field

**Why:** a second ordering field could disagree with the list, and an
ambiguous plan is worse than none. Reordering means reordering the list.

## D16. Subtitles are embedded by default; burn-in is optional and detected

**Why:** Homebrew FFmpeg 9.0.2 has no libass, so the `subtitles` filter
does not exist on this Mac. The tool asks FFmpeg which encoders and filters
it has, and refuses a plan it cannot render instead of failing midway.
Supplied SRT is re-parsed and rewritten (never passed through), so styling
codes and paths never reach the filter graph. Automatic transcription stays
an extension point until a local Whisper model is approved.

## D17. Never copy source metadata into renders

`-map_metadata -1 -map_chapters -1` on every output. iPhone clips carry GPS
location and device data, and renders are meant to be shared.

## D18. Convert colour in each clip's scale step; tone-map HDR or refuse it

**Decision:** every clip's `scale` converts to limited-range Rec.709
(`out_range=tv:out_color_matrix=bt709`, plus `out_primaries`/`out_transfer`
when swscale has colour management). HLG/PQ clips also get
`intent=perceptual`. Outputs are tagged Rec.709/tv, and validation checks
the range and tags. If FFmpeg cannot tone-map, HDR clips are refused.

**Why:** the first real-footage render failed its pre-publish check. Two
full-range (`yuvj420p`) iPhone clips made the output full range, because
`format=yuv420p` changes the pixel layout, not the range. Tagging alone
would be wrong: an HLG signal labelled Rec.709 shows flat, grey colours.
A test on the real HLG clip showed swscale's perceptual intent gives
natural skin tones where the clipping intent blows out highlights, and that
`-intent` on the command line is silently ignored for `-filter_complex`,
so the option is set inline. Regression tests check the generated command
and real pixel values (full-range black/white become 16/235). HLG was
verified on one real clip by eye; PQ only through the generated command.

## D19. Jobs run in a dedicated worker, not the upstream scheduler (Phase 3, approved)

**Decision:** long-running work is stored as jobs in a JARVIS SQLite store
(following the `scheduler/store.py` pattern) and executed by a dedicated
worker (Phase 3 step 3). `openjarvis.scheduler` is not used to run jobs.

**Why:** the upstream scheduler executes LLM prompts through
`JarvisSystem.ask`, polls every 60 s, runs tasks one after another in its
poll thread, and has no cancellation, stages or ownership. Deterministic
renders need none of the LLM path and all of the missing parts. The
roadmap's original "reuse `scheduler/`" line is superseded.

## D20. One active job at a time (Phase 3, approved)

**Decision:** the worker runs one job at a time. Parallel execution can be
added later as a setting.

**Why:** a render uses all CPU cores; two at once are slower in total and
harder to reason about. The store itself already supports concurrent
claimers safely (D26), so this is a worker policy, not a storage limit.

## D21. Notifications are local: macOS notifications and logging (Phase 3, approved)

**Decision:** final job states will be reported through the log and macOS
notifications (step 5). No paid service, external account or network
dependency. Channels such as Slack or Telegram are left for Phase 6.

## D22. Revisions are complete new plans, as child jobs (Phase 3, approved)

**Decision:** a revision is a new job with `parent_id` set and a complete
replacement payload (for video, a full `EditPlan`). JSON Merge Patch and
other partial edits are deferred until the basic flow works. Finished jobs
are never reopened, and a revision produces a new output version; earlier
outputs are kept (Phase 2 versioning already guarantees no overwrite).

## D23. At most two attempts, only for temporary failures (Phase 3, approved)

**Decision:** a job may run at most twice. Only failures the runner
explicitly classifies as temporary are retried; permanent failures,
validation failures and timeouts are not. `[jarvis.jobs] max_attempts`
may lower the limit to 1.

**How it is enforced:** the store refuses `retry` once
`attempts >= max_attempts` or after a cancellation request, so a runner
bug cannot cause a retry loop. Classification is the runner's job
(step 2), using the existing `render_status` / `validation_status` values.

## D24. Phase 2 integration changes are optional and later (Phase 3, approved)

**Decision:** a `job_id` in the render record and FFmpeg progress reporting
may be added in later steps as small, backward-compatible options. When
they are not used, rendering behaves exactly as in Phase 2. Step 1 does not
touch the Phase 2 pipeline.

## D25. The job core is skill-neutral: a `kind` plus an opaque JSON payload

**Decision:** a job has a dotted `kind` (`video.render` first; later for
example `research.report`) and a JSON object `payload` that only the
runner for that kind interprets. Results are a JSON object too. Status
names are generic (`succeeded`, not `ready_for_review`); skill-specific
meaning goes in the result. Workers claim only the kinds they handle.

**Why:** JARVIS will gain other long-running skills. Keeping FFmpeg, video
paths, HyperFrames and notification details out of `jarvis_ext/jobs`
means a new skill adds a runner, not a new store. No plugin system or
workflow engine is built until a second kind actually exists.

## D26. Job store integrity: write lock, guarded transitions, identified schema

**Decision:**

* Every write is a `BEGIN IMMEDIATE` transaction. The status is read,
  checked against the transition table, and updated (conditional on the
  status that was read) while holding SQLite's write lock. Claiming is
  therefore atomic across threads and processes (tested with 8 threads and
  4 processes).
* Only the worker that claimed a job may finish, fail, retry or confirm
  cancelling it.
* Every status change is appended to `job_events` in the same transaction.
* The file is marked with `PRAGMA application_id` and the schema version
  with `PRAGMA user_version`. A newer version, a foreign SQLite file or a
  non-database file is refused **before** anything is written (the check
  runs before switching to WAL). Upgrades will be explicit, numbered
  migrations.
* WAL journal mode, so status reads do not block the worker.
* The database is created with mode 0600 in `<OPENJARVIS_HOME>/jarvis/`
  (folder 0700), because payloads contain workspace paths. A custom
  `database_path` must be in an existing folder (as for `output_root`,
  D11) and must not be a symlink.
* Error messages never include the database path (D3).

**Not yet handled (step 3):** a job left `running` by a worker that died.
Recovery needs a heartbeat, which arrives with the worker.

## D27. Cancellation is a request; the real outcome wins (Phase 3, approved)

**Decision:**

* A **queued** job is cancelled immediately (`queued` → `cancelled`).
* Cancelling a **running** job only sets `cancel_requested`. The job stays
  `running` until its worker has stopped the work safely and calls
  `confirm_cancelled`, which is refused unless a cancellation was requested.
* If the work **succeeds or fails before the cancellation takes effect**,
  the job records that actual outcome (`succeeded` / `failed`), never a
  false `cancelled`. A cancellation request also blocks `retry`.
* The worker (step 3) must ensure a cancelled render never publishes
  partial output as a completed render. Phase 2 already gives the needed
  property: a render is produced in a private temp directory and only
  receives its final name after its checks pass (D12), and an interrupted
  render has its FFmpeg process stopped and temp directory removed. The
  worker must stop FFmpeg before publishing, then confirm the cancellation.

**Why:** reporting `cancelled` for a file that was in fact published, or
`succeeded` for work that was stopped halfway, would make the job history
untrustworthy. Implemented in step 1 (store rules, tested); the worker
side is step 3.

## D28. Single-job runner: generic outcome mapping, video handler on the existing tools

**Decision:**

* `jarvis_ext/jobs/runner.py` is skill-neutral. `run_next` claims one job
  of a kind it has a handler for, calls the handler, and maps its
  `JobOutcome` (`succeeded`, `failed`, `retry`, `cancelled`) onto the
  store's permitted transitions. An exception, a non-outcome return value,
  an unstorable result or an unrequested "cancelled" becomes a failure,
  never a success. `KeyboardInterrupt`/`SystemExit` are not caught, so an
  interrupted job stays `running` for the Step 3 worker to recover.
* `jarvis_ext/video/render_job.py` handles `video.render` with payload
  `{"plan": EditPlan}`. It calls the existing `media_render` and
  `media_validate` tools and adds no rendering or checking of its own.
  The plan is validated by `media_render` before anything is written.
* Success requires a successful render **and** a passed validation with
  `ready_for_review: true`, preview and thumbnail, for the same file.
* Retryable render statuses: `workspace_unavailable`,
  `output_unavailable`, `ffmpeg_failed`, `output_error`. Every other
  render status (including `timeout`, `failed_checks` and unknown
  statuses) is permanent. **Validation failures are never retried**: a
  retry would render a second version of a file that already exists. The
  job fails and its result names the unvalidated file.
* Error codes are `render_<status>` or `validate_<status>`; the job result
  keeps a compact summary of both stages (file, size, SHA-256, record,
  preview, thumbnail, failed check names, trimmed warnings).
* Cancellation is checked before rendering and between render and
  validation. A handler (or the Step 3 process control) can raise
  `JobCancelled` once the work has stopped. A render published before a
  cancellation took effect is recorded as "published but not validated".

**Store additions (backward-compatible):** `fail` and
`confirm_cancelled` accept an optional `result`, and `add_note` appends a
stage note (for example `rendered reel_v001.mp4`) to a running job's
history. No schema change.

**Not changed (D24):** the Phase 2 tools, render record and FFmpeg
handling are untouched.

## D29. Worker: flock, heartbeats, conservative recovery, stoppable FFmpeg

**Decision:**

* **Single instance:** an exclusive, non-blocking `flock` on
  `<database>.worker.lock`. The kernel releases it when the process dies,
  so there are no stale locks to clean up. The lock is the only way the
  worker runs jobs; jobs still run one at a time (D20).
* **Heartbeat:** schema version 2 adds `heartbeat_at` and `not_before`
  (an in-place `ALTER TABLE` migration; version 1 files upgrade on open).
  The runner sends a heartbeat every `heartbeat_seconds` from a background
  thread while a handler runs.
* **Recovery:** a running job is "abandoned" only if its heartbeat is older
  than `stale_after_seconds` (≥ 3 heartbeats), it is not this worker's,
  and this worker holds the lock. `JobStore.recover` re-checks the
  heartbeat age in the same write transaction. A cancellation request wins
  (→ `cancelled`). Otherwise the handler decides whether the attempt can be
  repeated: the video handler requeues only if no `rendered <file>` note
  exists, and otherwise fails the job with the file listed as unvalidated,
  so recovery never creates a second version from a known render. Attempt
  limits are enforced by the store.
* **Back-off:** `retry` and `recover` set `not_before = now +
  retry_delay_seconds`; `claim_next` skips such jobs until then. Only
  outcomes the handler already classifies as retryable are affected.
* **Cancellation and forced stop of running FFmpeg:** the tools'
  existing `process_runner` hook is given `stoppable_run_process`, which
  uses a new `video/ffmpeg.py` function, `run_process_until`. It is
  `run_process` plus a poll: every 0.25 s it asks the running job whether
  to stop (the store is read at most every 0.5 s), and it then stops the
  process group with the existing `_stop` (SIGTERM, then SIGKILL, reaped).
  `run_process` and the render/validate code are unchanged. The handler
  turns the resulting `JobCancelled` / `WorkerInterrupted` into outcomes
  by stage: during the render nothing was published (the tool removes its
  temp folder), so a shutdown requeues; after the render was published, a
  shutdown fails as `validate_interrupted` instead of re-rendering.
* **Graceful vs forced shutdown:** the first SIGINT/SIGTERM finishes the
  current job; a second one forces the stop above.
* **Temp cleanup:** only `<output_root>/.jarvis-tmp/job-*` directories
  (the `mkdtemp` prefix `OutputFolder` uses), only when they and all their
  contents are older than the longest a live tool call can take
  (`render_timeout + 4 × validate_timeout + 10 min`), deleted through
  `OutputFolder.remove_temp_dir`. Temp folders carry no job id, so age is
  the ownership evidence; it also protects folders used by a manual
  `media-render` run.
* **CLI:** `python -m jarvis_ext jobs ...` in the existing argparse CLI;
  JSON on stdout like the other commands.

**Why not resume interrupted jobs at validation, or match renders to jobs?**
That needs a job id in the render record (D24). It is optional and was not
approved for this step; the conservative policy above never duplicates a
known render, and the remaining window is documented in jobs.md.

## D30. Progress: FFmpeg's own report, one generic percentage, 100 only on success

**Decision:**

* **Source:** FFmpeg's `-progress` report (`out_time_us` per block),
  written to `progress.txt` in the render's private temp folder. This is
  the "FFmpeg progress reporting" option D24 anticipated: the render tool
  gains `report_progress` (default **off**) and `build_render_command`
  gains `progress` (default off); off, the command is byte-for-byte
  unchanged (tested). Only the job handler turns it on. No second render
  implementation, no new dependency.
* **Reading:** `stoppable_run_process` (D29) already polls every 0.25 s for
  cancellation; it now also reads new lines of the report with
  `ProgressReader` (complete blocks only, never decreasing, tolerant of
  junk) and hands the encoded time to the handler. Cancellation and
  cleanup are unchanged.
* **Model:** schema v3 adds `progress` (percent of the whole job) and
  `stage` (a short name; the store knows nothing about FFmpeg). The store
  enforces: monotonic per attempt, ≤ 99 while running, 100 only in
  `complete`, cleared when requeued. Stage changes go into the history.
* **Video mapping:** `render` 0–95 % by encoded time ÷ plan duration;
  `validate` holds at 95 % because validation has no meaningful measure;
  100 % only after the validator confirmed success.
* **Throttling:** stage changes at once; otherwise ≥ 1 point and ≥ 1 s
  apart (both), so a long render writes and publishes at most once a
  second.
* **Events:** custom string event `jarvis_job_progress` on the OpenJarvis
  event bus (no core enum change, like the scheduler's events), published
  at claim, on each write and at the next status. Publishing is wrapped so
  neither the bus nor a subscriber can affect the job (the core bus's own
  error logging assumes enum event types, so this wrapper matters).

**Why one percentage for the whole job:** a single number is what a list
or dashboard shows; the stage says what it means, and other skills can
use the same two fields with their own stages.

## D31. Notifications: a transactional outbox, delivered at least once

**Decision:**

* **When:** only final statuses (`succeeded`, `failed`, `cancelled`), for
  every job kind. No notification for progress or stages.
* **Outbox:** schema v4 table `job_notifications`, primary key job id,
  inserted by the store in the same transaction as the final status
  (every final change goes through one internal method). No backfill
  for jobs finished before the upgrade.
* **Delivery:** the worker claims due rows atomically (`sending`, with a
  lease of `max(stale_after_seconds, 60)` s), sends outside the
  transaction, then records `delivered` or a failed attempt (30 s
  back-off, 3 attempts; permanent errors give up at once).
* **Guarantee:** at least once. A crash between showing and recording
  can repeat one notification; recording first would lose it instead.
  Exactly-once is not claimed.
* **Text:** kind, short id, outcome, error code only; cleaned and bounded
  again by the notifier.
* **Backends:** `log` (default) and `macos` (`/usr/bin/osascript`, fixed
  script, values as `argv` after `--`, no shell, minimal env, timeout).
  `macos` on another system, or without osascript, stops `jobs worker`
  at start with a clear error, following the config convention that a
  setting which cannot be honoured is an error, not a silent no-op.
* **Isolation:** `jarvis_ext.notify` knows nothing about jobs;
  `jarvis_ext.jobs.notifications` knows nothing about video. Failures are
  logged by code and never change a job or stop the worker.

**Why an outbox:** sending straight from the runner would lose the
notification whenever the worker dies between finishing a job and
sending, and would need its own duplicate guard. A row written with the
final status survives crashes and is unique by construction.

## D32. Revisions, verified reuse and resume use render provenance, not a cache

**Decision:**

* **Revisions:** `jobs revise JOB_ID PLAN.json` queues a child job
  (`parent_id`) with a complete replacement plan (D22). The parent must be
  a finished `video.render` job; the `output_name` is kept (filled in or
  enforced) so the revision is the next version. Plans are checked before
  queueing (also for `jobs submit`).
* **Provenance (the D24 option):** the job handler's render tool adds an
  optional `provenance` block to the render record: job id, plan digest,
  and an input key = SHA-256 over the normalised plan, preset, FFmpeg
  version, exact FFmpeg command and full SHA-256 of every source,
  subtitle text and logo. Off for agents and the CLI, so their records
  are unchanged. No absolute paths. `record_version` stays 1 (validate
  ignores unknown fields).
* **Reuse:** an identical job succeeds with an earlier output only when
  the earlier job succeeded and was ready for review, the input keys
  match, the record names the publishing job with the same key and
  checksum, and `media_validate` passes again (fresh review files).
  Anything missing or different: render a new version. `--no-reuse`
  opts out.
* **Resume:** on a later attempt, a render whose record names this job
  and whose key still matches is validated instead of re-rendered; two
  such renders are ambiguous and fail the job; a `rendered` note without
  such a record fails as before. Interrupted validation of a render with
  provenance is retried (validation only) instead of failing.
* **No schema change:** provenance lives in the record; `plan_digest`,
  `input_key`, `reused_from` and `resumed` live in the job's JSON result.
  Policy lives in `video/render_job.py` and `video/provenance.py`; the job
  store is unchanged.

**Why full-file hashes, not size + mtime:** copies and restores can keep
both, and a wrong reuse would hand over a video of different footage.
The cost (reading the sources once more) is small next to a render.

**Why re-validate on reuse:** the earlier validation proved the file was
good then; re-running it proves the file is still complete and decodable
now and produces the review files a new job would have.

## D33. Agent job tools need the capabilities of the work they queue

**Decision:** `media_job_submit` and `media_job_revise` require
`file:read`, `file:write` and `schedule:create`, and follow
`[jarvis.media_render] require_confirmation`; `media_job_status`
requires `file:read`. The tools only validate and queue (or read); the
worker does the work. Plans are also checked for unsafe paths (absolute,
`..`) before queueing, in the tools and the CLI.

**Why:** a job is deferred execution of `media_render` +
`media_validate`. Requiring less to queue than to render would let a
restricted agent obtain a render indirectly. `schedule:create` is the
existing capability for deferred work. Status exposes only job metadata
and output-folder file names, never plans or paths.

## D34. Speech providers implement the upstream interfaces, outside the agent

**Decision:** speech input is `jarvis_ext.speech.MlxWhisperBackend`
(upstream `SpeechBackend`), speech output is `VoiceboxTTSBackend`
(upstream `TTSBackend`). They are registered as `mlx-whisper` and
`voicebox` in the existing `SpeechRegistry` / `TTSRegistry` from
`jarvis_ext.ensure_registered()`, as **lazy factories**: the provider
module is imported only when a backend is created. They are not tools,
get no capabilities, and are not added to upstream's `auto` discovery
order. No upstream file was changed.

**Why:** upstream already has exactly these two interfaces and callers
(`jarvis serve`, voice chat, the `text_to_speech` tool) that use them, so a
new framework would only duplicate them. Registering the classes directly
made every `openjarvis.tools` import about 0.5 s slower, because it pulls
in upstream `openjarvis.speech` and its optional backends; the factories
cost well under a millisecond. Keeping them out of `auto` means nothing
changes until you choose them.

## D35. MLX Whisper Base, loaded once, fed decoded audio, no transcript clean-up

**Decision:** `mlx-community/whisper-base-mlx`, English, with the
vocabulary prompt `JARVIS, Voicebox, Kokoro, George, Phase 4.1 roadmap`;
the raw transcript is returned. The model is loaded lazily or by `load()`
through `mlx_whisper`'s own process-wide `ModelHolder` (float16, as its
`transcribe` uses) and kept for the life of the process. WAV is decoded in
process and passed as a float32 array; inference holds a lock.

**Why:** the benchmark chose Base (Turbo caused memory pressure on 8 GB;
Voicebox's transcription took 3–4 s against about 0.3 s). Loading through
the same holder is what makes the first request not load twice and later
ones not at all. Passing a file path would make `mlx_whisper` start
`ffmpeg` for every utterance. One MLX model should not run concurrent
requests. Clean-up by an LLM would add latency and a second model that
interprets what you said; it can be an option later.

## D36. Voicebox: loopback only, stream endpoint, never its personality rewrite

**Decision:** `base_url` must be plain `http://` to `localhost` or a
loopback address with a port. Speech is generated with
`POST /generate/stream` (WAV back, nothing saved in Voicebox's history),
always with `personality: false`. The voice is chosen by profile id/name,
or by default as the first profile using Kokoro `bm_george` (the `JARVIS`
profile here). Proxy variables are ignored and redirects not followed.
JARVIS never starts, stops or reconfigures Voicebox.

**Why:** the brief is local speech with no external network access.
`personality: true` would let Voicebox's own model reword JARVIS's replies.
Choosing by preset voice rather than a profile UUID survives recreating the
profile and matches upstream's `speech.voice_id = "bm_george"`.

## D37. mlx-whisper is an optional `speech-mlx` extra; mlx is not pinned (approved)

**Decision:** `speech-mlx = ["mlx-whisper==0.4.3; sys_platform == 'darwin'
and platform_machine == 'arm64'"]` in `pyproject.toml`. It is not a
default dependency. `mlx` is left to the resolver, which keeps the 0.31.2
already locked for `inference-mlx`. The lock was regenerated with uv
0.13.1, so the diff adds only `mlx-whisper` and the extra; no other
package changed version.

**Why:** the benchmark environment (Python 3.9.6, mlx 0.29.3) cannot be
reproduced in the project: Python must be 3.10–3.13, and `mlx-lm` needs
`mlx>=0.31.2`, which uv locks together with every other extra. Pinning
0.29.3 would have needed a `[tool.uv] conflicts` entry and two MLX
versions in the lock. With 0.31.2 the transcript and timings matched the
benchmark version (measured on Python 3.13 and 3.10). `mlx-whisper` is
pinned exactly because 0.4.3 is the version that was benchmarked. Newer uv
releases (0.13.2+) rewrite three `onnxruntime` markers into an equivalent
form; regenerating with 0.13.1 avoids mixing that churn into this change.

## D38. Preflight: one registry of real checks, honest statuses, extend before adding

**Decision:** `python -m jarvis_ext preflight` is the standard verification
step. Checks register with a decorator and run in groups (app, media, jobs,
brain, future). Statuses are PASS, FAIL and WARN. A check that could not run
is WARN with "NOT RUN" and no timing, never PASS. An optional component that
is absent is WARN; an existing component that is broken, or a required one
that is missing, is FAIL and the exit status is 1. Every check has a latency
budget and a slow pass becomes WARN. Media and job checks use a generated
fixture and an isolated temp database. Secret checks plant canary keys and
scan for them and for the real configured values; findings never print a
value. The brain checks never change configuration; the request check
intercepts HTTP and blocks non-loopback sockets, so it proves request shape
only; the one live request is behind `--live-brain`. Starting the real
`jarvis serve` is behind `--spawn-server`. No new dependency.
Rule: a change to critical functionality is not reported complete until the
relevant tests and preflight checks have been run and reported; a subset is
never described as end-to-end verification.

**Why:** earlier verification was a set of unit suites plus manual Mac
runs, with no single command that exercised the real pipeline, job store,
secret handling and provider selection together. Statuses are strict so a
green run cannot hide a skipped or unimplemented part.

**Revisit if:** the checks become slow enough to discourage running them, or
a group needs a service that cannot be faked.

## D39. Voice output goes through a JARVIS adapter; no silent cloud fallback; confirm which "VoiceStudio"

**Decision (proposed, not yet implemented):** build a `jarvis_ext/voice/`
layer (adapter interface, profiles, cache, chunker, playback contract) in
front of the existing Voicebox backend first, then add a VoiceStudio adapter
once it is identified. Speech output never falls back to a cloud provider on
its own. Finding that motivates this: upstream's `_tts_discovery` tries
`kokoro`, `openai_tts`, `cartesia`, `elevenlabs` in that order and treats a
backend as usable when its health check passes, which for the cloud ones
depends only on their API key existing. With a key configured and the local
service down, `/v1/speech/synthesize` would send reply text to that provider.
`future.speech_cloud_fallback` flags that situation (WARN) today.

"VoiceStudio" is ambiguous: the repository contains the Voicebox integration,
and more than one unrelated public project uses the name VoiceStudio. No
integration contract is assumed until the user says which one is meant.

**Why:** the user's rule is local speech generation and no permanent cloud
audio service; the upstream default order breaks it quietly.

**Revisit if:** the user wants an optional, explicitly enabled cloud voice.

## D40. JARVIS speaks with Kokoro "George" through VoiceStudio, chosen explicitly on every request

**Decision (implemented 2026-10-29):** JARVIS's default voice is Kokoro 82M
(`bm_george`, British English) served by the local VoiceStudio v0.5.7 at
`http://127.0.0.1:3900` through MLX-Audio. `VoiceStudioTTSBackend`
(`TTSRegistry["voicestudio"]`) is the target backend and
`speech.tts_backend` defaults to it; Voicebox is no longer the target (its
code stays registered, unused). Every request to `POST /v1/audio/speech`
carries `model: "mlx-audio"`, `voice: "bm_george"`, `language: "British
English"` and `response_format: "wav"` explicitly. Model, voice, language,
speed and URL are configurable in `[jarvis.voicestudio]`; the URL must be
loopback. The sample rate is read from the WAV header, never assumed.

The spoken copy of the text writes the name as the word "Jarvis" (Kokoro
spelled `J.A.R.V.I.S.` letter by letter); displayed text is unchanged.

**Why:** VoiceStudio's "active engine" can change in its UI (and the
`tts-1` aliases follow it), so relying on it could change JARVIS's voice or
select a model with a licence JARVIS must not use. Listening comparison:
KittenTTS worked but sounded too robotic; George beat Daniel. OmniVoice is
non-commercial and its licence was not accepted; JARVIS never requests it.
No cloud TTS fallback: when VoiceStudio is down, speech fails with a clear
error and everything else keeps working (builds on D39).

**Not decided / still open:** profiles, caching, chunking, streaming and
cancellation of an in-flight request (the server's worker thread has no
cancel hook); a by-ear check that "Jarvis" is pronounced as the full name
(only durations were compared).

**Revisit if:** the voice changes, VoiceStudio gains a stable headless start
command, or a different local engine beats Kokoro on this 8 GB Mac.

## Assumptions

* Only you (and processes you trust) write to the media workspace. See the
  TOCTOU note in media-workspace.md.
* JARVIS runs as your user. OS-level isolation (a separate macOS user or a
  container for the worker) is a Phase 4 topic.
* Claude as the runtime model needs a separately billed API key. Until you
  approve that, local models through Ollama are the zero-cost path (see
  AUDIT.md).
