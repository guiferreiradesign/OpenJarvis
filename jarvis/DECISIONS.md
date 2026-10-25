# Architecture decisions

Short records of decisions made during Phases 1–3, so they can be revisited
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

## Assumptions

* Only you (and processes you trust) write to the media workspace. See the
  TOCTOU note in media-workspace.md.
* JARVIS runs as your user. OS-level isolation (a separate macOS user or a
  container for the worker) is a Phase 4 topic.
* Claude as the runtime model needs a separately billed API key. Until you
  approve that, local models through Ollama are the zero-cost path (see
  AUDIT.md).
