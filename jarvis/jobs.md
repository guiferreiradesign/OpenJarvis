# Jobs: tracking long-running work (Phase 3)

Phase 3 turns long-running work into **jobs**, starting with renders. A job
is tracked from submission to a final status, can be cancelled, and is
retried only when that is safe.

**What exists today (steps 1–6):** the job model, its status rules, the
SQLite job store, a runner that executes one `video.render` job through
the existing render and validate tools, a worker with a `jobs` CLI
(submit, revise, list, show, cancel, worker), progress reporting from
FFmpeg's real encoding position, a notification for every finished job
(log, or macOS), revisions as child jobs, verified reuse of identical
finished outputs, and resuming a job's own published render. Agent tools
are step 7 in [ROADMAP.md](ROADMAP.md). The Phase 2 render pipeline is
unchanged apart from three additive, opt-in hooks: a stoppable process
runner (D29), FFmpeg's `-progress` report (D30) and a `provenance` block
in the render record (D32).

The job layer is shared by every JARVIS skill. It knows nothing about video,
FFmpeg or HyperFrames (DECISIONS.md D25); notifications are generic too:

```
skill (later: video-editing skill, research, ...)
   │  submits kind + JSON payload, reads status/result
   ▼
jarvis_ext/jobs  ── store.py: SQLite, status rules, claiming, history
   ▲
   │  claims jobs of the kinds it handles, reports the outcome
runner.run_next + a handler per kind  (first kind: "video.render")
   │
   ▼
existing tools (media_render, media_validate)
```

## Job model

| Field | Meaning |
|---|---|
| `id` | 32 hex characters (random UUID) |
| `kind` | Dotted lower-case name, e.g. `video.render`, `research.report` (≤ 64 characters) |
| `payload` | JSON object the runner for that kind interprets (for video, an `EditPlan`). ≤ 1 MB. Only plain JSON types: tuples, non-string keys, NaN and infinity are refused rather than silently converted |
| `status` | `queued`, `running`, `succeeded`, `failed`, `cancelled` |
| `parent_id` | The job this one revises, if any (D22). Must exist |
| `attempts`, `max_attempts` | Runs started so far; limit 1–2 (D23) |
| `cancel_requested` | Set when you cancel a running job |
| `stage`, `progress` | The running stage (e.g. `render`, `validate`) and the percentage of the whole job, 0–100; see "Progress" |
| `claimed_by` | The worker running it |
| `result` | JSON object from the runner when it succeeds (for video: output file, checksum, preview…) |
| `error_code`, `error_message` | Last failure, e.g. `ffmpeg_failed`; message truncated to 4000 characters |
| `created_at`, `updated_at`, `started_at`, `finished_at` | UTC ISO 8601 timestamps |

Every status change is also written to an append-only history
(`JobStore.events(job_id)`): time, from, to and a short note such as
`claimed by worker-1` or `retry after ffmpeg_failed`.

## Status rules

```
queued ──claim──► running ──► succeeded
   │                 │  ├───► failed
   │                 │  └───► cancelled   (after a cancel request)
   │                 └──retry──► queued   (only while attempts < max_attempts)
   └──cancel──► cancelled
```

| Operation | From → to | Refused when |
|---|---|---|
| `submit(kind, payload, parent_id=None, max_attempts=None)` | → `queued` | Invalid kind/payload, unknown parent, `max_attempts` not 1–2 |
| `claim_next(worker_id, kinds=None)` | oldest `queued` → `running`, `attempts + 1` | Returns `None` when nothing is queued |
| `complete(id, worker_id, result=None)` | `running` → `succeeded` | Not running, or claimed by another worker |
| `fail(id, worker_id, error_code, message)` | `running` → `failed` | As above |
| `retry(id, worker_id, error_code, message)` | `running` → `queued` | As above, or all attempts used, or a cancellation was requested |
| `cancel(id)` | `queued` → `cancelled` at once; `running` stays `running` with `cancel_requested` set | Job already final |
| `confirm_cancelled(id, worker_id)` | `running` → `cancelled` | No cancellation requested, or another worker's job |

`succeeded`, `failed` and `cancelled` are final: nothing changes them, and
a revision is a new job.

**Cancellation (D27):** cancelling a running job is a request. The job
becomes `cancelled` only when its worker confirms the work has stopped
safely. If the work succeeded or failed before that, the job keeps the
real outcome rather than a false `cancelled`. The worker (step 3) must
never let a cancelled render publish partial output; Phase 2 already only
names a render after its checks pass.

Whether a failure is temporary (retry) or permanent (fail) is decided by
the runner for each kind (step 2), not by the store. The store only
guarantees the limits.

## Running a job (step 2)

`jarvis_ext.jobs.runner.run_next(store, worker_id, handlers)` claims the
oldest queued job whose kind has a handler, runs it, and records the
outcome. It runs one job and returns it (or `None` if nothing was queued).
The worker (below) calls it repeatedly.

A handler returns a `JobOutcome`, which the runner maps onto the store:

| Outcome | Recorded as |
|---|---|
| `succeeded(result)` | `succeeded` with the result |
| `failed(code, message, result)` | `failed` |
| `retry(code, message, result)` | `queued` again while attempts remain and no cancellation was requested; otherwise `failed` with the same code |
| `cancelled(result)` | `cancelled` if a cancellation was requested; otherwise `failed` (`unexpected_result`) |
| exception in the handler | `failed` (`runner_error`, exception type only) |
| `JobCancelled` raised in the handler | as `cancelled` |
| anything else returned | `failed` (`unexpected_result`) |
| result that cannot be stored | `failed` (`invalid_result`) |

Handlers can call `context.cancel_requested()` between stages and
`context.note("...")` to add a stage to the job's history.

### `video.render`

Payload: `{"plan": {...}}`, a complete `EditPlan` (see
[video-pipeline.md](video-pipeline.md)), optionally with `"reuse": false`
(always render; see step 6). Any other key, or a missing or empty plan,
fails the job (`invalid_payload`) before anything runs. `jobs submit` and
`jobs revise` also check the plan's shape and values before queueing.

```python
from jarvis_ext.jobs import open_job_store
from jarvis_ext.jobs.runner import run_next
from jarvis_ext.video.render_job import KIND, VideoRenderHandler

with open_job_store() as store:
    store.submit(KIND, {"plan": plan})
    job = run_next(store, "mac-worker", {KIND: VideoRenderHandler()})
    print(job.status, job.result["output"], job.result["ready_for_review"])
```

The handler calls `media_render` and then `media_validate`, exactly as
the CLI does. The job **succeeds only** when the render succeeded and
validation passed with `ready_for_review: true` (preview and thumbnail
made) for that same file.

| What happened | Job | Error code |
|---|---|---|
| Render failed with `workspace_unavailable`, `output_unavailable`, `ffmpeg_failed` or `output_error` | Retried once (2 attempts in total), then `failed` | `render_<status>` |
| Render failed with any other status (`invalid_plan`, `access_denied`, `invalid_media`, `unsupported`, `ffmpeg_missing`, `timeout`, `failed_checks`, …) | `failed` | `render_<status>` |
| Render succeeded, validation did not pass | `failed`, **not retried** (a retry would render a second version). The result names the unvalidated file | `validate_<status>` |
| Render published, then validation interrupted (forced stop) | Retried once as *validation only* (step 6), then `failed` | `validate_interrupted` |
| A tool's report is unreadable or contradicts itself | `failed` | `unexpected_result` |
| Cancellation requested before the render | `cancelled`; nothing rendered | |
| Cancellation requested after the render, before validation | `cancelled`; the result lists the file as published but **not validated** | |
| Work finished before the cancellation took effect | The real outcome (`succeeded` / `failed`) | |

The result keeps `output`, `ready_for_review`, and a compact `render` and
`validate` summary (version, size, SHA-256, record, preview, thumbnail,
names of failed checks, up to 50 warnings).

All Phase 2 output guarantees still apply, because the tools do the work:
nothing is overwritten (a second job for the same plan produces `_v002`,
unless it reuses `_v001`, step 6), and a render is only named after its
checks pass. A render whose FFmpeg is
stopped publishes nothing and leaves no temp folder (tested with real
FFmpeg).

## The worker and the `jobs` CLI (step 3)

```bash
uv run python -m jarvis_ext jobs submit plan.json      # queue a video.render job (or - for stdin)
uv run python -m jarvis_ext jobs revise 3f2a9c plan.json  # queue a revision (step 6)
uv run python -m jarvis_ext jobs list [--status failed] [--kind video.render] [--limit 20]
uv run python -m jarvis_ext jobs show 3f2a9c           # full id or its first 6+ characters
uv run python -m jarvis_ext jobs cancel 3f2a9c
uv run python -m jarvis_ext jobs worker --once         # run at most one job, then exit
uv run python -m jarvis_ext jobs worker                # keep running jobs until stopped
```

Every command accepts `--config PATH` and prints JSON on stdout. Errors go
to stderr: exit status 1 for an error (unknown job, bad plan, config
error, another worker running), 2 for invalid arguments. Output contains
job ids, workspace-relative plan paths and output file names only, never
absolute paths. The worker logs one line per job to stderr and prints a
report (`processed`, `recovered`, `cleaned_temp_dirs`, `stopped`; each job
with its `stage` and `progress`) when it
exits.

**One worker, one job at a time.** The worker takes an exclusive `flock`
on `<database>.worker.lock` for as long as it runs; a second worker exits
with "Another job worker is already running". The kernel drops the lock
when the process ends for any reason, so a crashed worker never blocks the
next. Jobs run strictly one after another (D20).

**Stopping.** The first Ctrl-C or SIGTERM lets the current job finish and
then exits. A second one stops the current job now: FFmpeg is stopped and
the job is recorded as interrupted: requeued if it was still rendering;
if its render was already published, requeued for **validation only**
when the render record identifies the job (step 6), otherwise failed with
`validate_interrupted`, because re-running would create a second version.
A forced stop uses up one of the job's attempts.

**Cancelling a running job.** `jobs cancel` sets `cancel_requested`. While
FFmpeg runs, the worker checks for this about twice a second; it then
stops FFmpeg's whole process group (SIGTERM, then SIGKILL), the render
tool removes its temp folder, and only then is the job marked
`cancelled`. A cancelled render publishes nothing. If the render was
already published, the job is cancelled with the file listed as not
validated and not ready for review. Work that finished first keeps its
real outcome.

**Heartbeat and crash recovery.** While a job runs, the worker records a
heartbeat every `heartbeat_seconds`. When the worker starts, and whenever
it is idle, it looks for `running` jobs of *other* workers whose heartbeat
is older than `stale_after_seconds`. Because it holds the lock, no other
worker can be alive, and the heartbeat age is re-checked inside the same
database transaction that changes the job. Such a job is:

| Situation | Result |
|---|---|
| A cancellation was requested | `cancelled` |
| Its history shows a published render (`rendered <file>`) | requeued (`validate_interrupted`) while attempts remain; the next attempt validates that file only if its record identifies this job, and never re-renders (step 6). With no attempts left: `failed`, the file listed as not validated |
| No published render recorded, attempts left | requeued (`interrupted`) after `retry_delay_seconds` |
| No attempts left | `failed`, `interrupted` |
| A kind this worker has no handler for | `failed`, `interrupted` |

**Retry back-off.** A job requeued after a temporary failure (or an
interruption) is not claimed again for `retry_delay_seconds` (default
60 s), so an unplugged drive does not use up both attempts at once. Other
queued jobs still run in the meantime. Which failures are temporary is
unchanged (D28); the worker never makes a permanent failure retryable.

**Temp-folder cleanup.** At start and then hourly while idle, the worker
removes abandoned render/validate temp folders: only plain directories
named `job-*` directly inside `<output_root>/.jarvis-tmp/`, and only when
the folder and everything in it are older than `render_timeout_seconds +
4 × validate_timeout_seconds + 10 min` (45 min with the defaults). No
running tool call can be that old (FFmpeg is stopped at its timeouts), and
a folder an orphaned FFmpeg is still writing to stays because its files
are recent. Symlinks are never followed, and renders, records and
`review/` files are never candidates.

## Progress (step 4)

A running job has a **stage** and a **progress** percentage for the whole
job. For `video.render`:

| Stage | Progress | Where it comes from |
|---|---|---|
| (claimed) | 0 | the claim |
| `render` | 0 → 95 | FFmpeg's own `-progress` report: the encoded output time (`out_time_us`) divided by the plan's total duration (sum of clip lengths), × 95 |
| `validate` | 95 | held while `media_validate` runs; validation has no measurable progress, so none is invented |
| (succeeded) | 100 | recorded by the store only when the job succeeds |

Rules (generic, for every job kind):

* **Never decreases** within an attempt; a lower report keeps the current
  value. A job requeued for another attempt starts again from 0 (its
  previous value is cleared when it is requeued).
* **Below 100 while running**: anything ≥ 99 is stored as 99. Only a
  successful completion records 100, so a failed, cancelled or
  interrupted job never shows 100; it keeps the stage and percentage it
  reached, which is useful diagnostics.
* **Throttled**: a stage change is written at once; otherwise at most one
  write per second, and only after a gain of at least one point. FFmpeg
  writes its report about twice a second; the worker reads it while it
  checks for cancellation (every 0.25 s).
* **FFmpeg's report is evidence of encoding speed only.** Reaching 95 %
  does not mean the file is good; the render's own checks and validation
  still decide success.
* Missing, malformed, partial, `N/A`, duplicated or out-of-order lines in
  the report are ignored (`video.ffmpeg.ProgressReader`). If the plan's
  duration cannot be worked out, the render stage stays at 0 until it
  finishes.
* A stage change is also written to the job's history (`stage render`,
  `stage validate`); individual percentages are not.

**Watching progress:** `jobs list` and `jobs show` include `stage` and
`progress`, for example `"stage": "render", "progress": 47.5`.

**Event bus:** the runner publishes a custom event,
`jarvis_job_progress` (a plain string, like the upstream scheduler's
`scheduler_task_start`), when a job is claimed, on each progress write and
when the job reaches its next status. The data is
`{job_id, kind, status, stage, progress, attempt}`: no paths, no payload.
A failing bus or subscriber is logged and never affects the job. The
OpenJarvis event bus is in-process, so subscribers must run inside the
worker process; other processes (the CLI) read progress from the job
database.

```python
from openjarvis.core.events import get_event_bus
from jarvis_ext.jobs.runner import JOB_PROGRESS_EVENT

get_event_bus().subscribe(JOB_PROGRESS_EVENT, lambda e: print(e.data))
```

## Notifications (step 5)

When a job reaches a final status (`succeeded`, `failed`, `cancelled`)
the worker sends one notification. Progress and stage changes are never
notified; watch them with `jobs list`/`jobs show` or the event bus.

| Outcome | Title | Message |
|---|---|---|
| succeeded | `JARVIS: job succeeded` | `video.render job 1a2b3c4d succeeded.` |
| failed | `JARVIS: job failed` | `video.render job 1a2b3c4d failed (ffmpeg_failed).` |
| cancelled | `JARVIS: job cancelled` | `video.render job 1a2b3c4d was cancelled.` |

Messages are built only from validated identifiers: the kind, the first
8 characters of the id, the outcome and the error **code**. The error
message, payload (plan) and result are never used, so no path, file name,
plan content or credential can appear. As a second guard, the text is
cleaned (control characters removed, anything path-like replaced by
`[path]`, title ≤ 80 and message ≤ 240 characters).

**Backends** (`[jarvis.jobs] notifications`):

* `log` (default): one INFO line on the `jarvis_ext.notify` logger
  (the worker logs to stderr). Works everywhere.
* `macos`: a Notification Center banner via `/usr/bin/osascript`, plus
  the same log line. The AppleScript is a fixed constant; title and
  message are passed as arguments to its `run` handler after `--`, so
  notification text is never parsed as AppleScript. No shell, a minimal
  environment (`PATH`, `HOME`), no stdin, a 10 s timeout; osascript's
  output is not logged. If you are not on macOS, or `osascript` is
  missing, `jobs worker` refuses to start with a clear message instead
  of silently not notifying. macOS may ask once whether "Script Editor"
  (osascript) may show notifications; allow it in System Settings →
  Notifications if no banner appears.

**Delivery (outbox).** Schema v4 adds `job_notifications`, one row per
finished job (primary key: the job id). The row is inserted in the
**same transaction** as the final status, so the two commit or roll back
together: a job cannot finish without owing its notification, and
cannot owe two. Rows move `pending` → `sending` → `delivered` (or
`failed`):

1. The worker claims due rows in one `BEGIN IMMEDIATE` transaction
   (`pending`, or `sending` with an expired lease). Concurrent claimers,
   in any thread or process, never receive the same row.
2. It calls the notifier outside any transaction.
3. It records `delivered`, or a failed attempt: retried after 30 s, at
   most 3 attempts; errors that cannot get better (not macOS, no
   osascript) give up at once. Only the claiming worker can record the
   result.

The worker delivers on start (so a restart sends what a crashed worker
left behind), after every job, and on each idle poll. Jobs finished by
recovery or cancelled from the CLI while queued are notified the next
time a worker polls.

**Guarantee: at least once, not exactly once.** Showing a banner and
committing to SQLite cannot be one atomic step. If the worker dies after
claiming a row and before recording the result, the row stays `sending`;
once its lease (`max(stale_after_seconds, 60)` seconds) expires it is
sent again. So a crash in the instant between showing a notification and
recording it gives one duplicate; a crash at any other moment loses
nothing and duplicates nothing. Recording `delivered` *before* sending
would turn that duplicate into a lost notification, which is worse for
"your render is done". Ordinary polling never repeats a notification.

**A notification never affects its job.** Notifier errors, notifier bugs
and database errors during delivery are logged with a short code (the
notification text and osascript output are not logged) and the job's
status, result and history are unchanged. `jobs show` includes the
notification's state, attempts and last error code.

Other skills get notifications for free by finishing jobs through the
store. `jarvis_ext.notify` (`Notification`, `make_notifier`) can also be
used directly; it knows nothing about jobs.

## Revisions and reuse (step 6)

### Revisions

```bash
uv run python -m jarvis_ext jobs revise JOB_ID PLAN.json [--no-reuse] [--config PATH]
```

`JOB_ID` (full or first 6+ characters) is the job to revise; `PLAN.json`
(or `-`) is a **complete replacement** `EditPlan`; partial edits (JSON
Patch / Merge Patch) are not supported (D22). Before anything is queued:

* the parent must exist, be a `video.render` job and be finished
  (`succeeded`, `failed` or `cancelled`); a queued or running job is
  refused;
* the plan keeps the parent's `output_name`: it is filled in if left out,
  and a different name is refused (submit a new job instead), so the
  revision is published as the next version (`reel_v002.mp4`);
* the plan's shape and values are checked (`Invalid plan: …`).

The child job has `parent_id` set; the output is the same JSON as
`jobs submit` (now with `parent_id`). The parent, its result, its history
and every existing file are left as they are, whether the revision
succeeds or fails. `jobs show`/`list` include `parent_id` and, for a
reused result, `reused_from`.

### Provenance in the render record

Renders made by the job handler carry an optional `provenance` block in
`<name>_vNNN.render.json` (D24, D32). Renders from an agent or
`media-render` are unchanged.

```json
"provenance": {
  "key_version": 1,
  "job_id": "3f2a9c…",              // the job that published this file
  "plan_digest": "…",               // SHA-256 of the normalised plan
  "input_key": "…",                 // SHA-256 over everything below, or null
  "sources": [{"source": "Shoot/a.mp4", "size_bytes": 123, "sha256": "…"}],
  "subtitles_sha256": "…", "logo_sha256": null
}
```

The **input key** covers the normalised plan, its preset, the FFmpeg
version, the exact FFmpeg command (every option, the filter graph, the
colour handling chosen for this FFmpeg's capabilities, and the source,
logo and FFmpeg paths) and the SHA-256 of every source file, the
subtitle text and the logo. Sources are hashed in full before FFmpeg
starts; if one changes during the render (size or modification time),
`input_key` is `null` and that file is never reused or resumed. No
absolute path is stored. The job's result keeps `plan_digest` and
`input_key` too.

### Reuse

Before rendering, a job looks for an earlier **succeeded** `video.render`
job with the same plan digest (cheap). Only then does it compute its own
input key (probes and hashes the sources). It reuses an earlier output
only if **all** of these hold:

1. the earlier job is `succeeded` with `ready_for_review: true` (failed,
   cancelled and unvalidated jobs are never candidates) and its result has
   an input key equal to this job's;
2. the render record on disk names the job that published the file (by
   id, not by file name), with the same input key and the SHA-256 the
   earlier job recorded;
3. `media_validate` passes on the file **again**: size and SHA-256 against
   the record, probe against the plan, a full decode, and a fresh preview
   and thumbnail. So a reused result meets exactly the readiness rules of
   a new render.

The job then succeeds with `output` = the earlier file, `reused_from` =
the job that rendered it, the earlier render summary and the new
validation summary; its history shows `reusing <file> from job <id>` and
`validated <file>`; its stage is `validate` from 0 % (so a fallback
render still starts from 0 %), and it reaches 100 % only after validation. If any
check fails, the history says why (`not reusing <file>: …`) and the job
renders a new version as usual. `--no-reuse` (payload `"reuse": false`)
always renders. Reuse adds the time to hash the sources and to validate
the earlier file; it saves the render.

### Resuming a job's own render

A worker can die (or be force-stopped) after a render was published but
before it was recorded or validated. On a later attempt the job first
looks for renders whose record names **this job's id** (the file name only
narrows the search):

| Found | What happens |
|---|---|
| One, with the input key the plan resolves to now | `resuming <file>`: validated without rendering; `succeeded` (with `resumed: true`) only if validation passes; a failed validation fails the job without rendering again |
| One, but the inputs changed since (or a source cannot be read) | `not resuming <file>: …`; renders a new version |
| More than one | `failed`, `resume_ambiguous`; nothing is used or rendered |
| None, but the history says `rendered <file>` (a record without provenance, e.g. from before step 6) | `failed`, `validate_interrupted`, as before; never re-rendered |
| None and no `rendered` note | renders as usual |

So the crash window between publishing and recording `rendered <file>`
no longer produces a second version, and an interrupted validation is
finished instead of failing the job. No database change was needed:
provenance lives in the render record, the rest in the job's result.

## Storage and safety

* **Location:** `<OPENJARVIS_HOME>/jarvis/jobs.db` (normally
  `~/.openjarvis/jarvis/jobs.db`), created with mode 0600 in a 0700 folder.
  Payloads contain workspace paths, so the database is not put in a media
  or output folder.
* **Concurrency:** every write runs in a `BEGIN IMMEDIATE` transaction, so
  the status check and the update happen under SQLite's write lock. Two
  claimers, in any threads or processes, never get the same job (tested
  with 8 threads and 4 processes on 40–60 jobs). WAL mode lets status reads
  run while a worker writes. A store object may be shared between threads.
* **Schema version:** the file is marked as a JARVIS job database
  (`PRAGMA application_id`) with its schema version (`PRAGMA user_version`,
  currently 4; older files are upgraded in place when opened, in one
  transaction. The v3 → v4 upgrade adds the notification outbox and
  creates no rows for jobs that had already finished, so upgrading never
  announces old work).
  A database from a newer JARVIS, another program's SQLite file, or a file
  that is not a database is refused, and the file is left byte-for-byte
  unchanged. Future upgrades will be numbered migrations applied in one
  transaction.
* **Errors** never include the database path, because they can reach a
  model through tool results (D3).

## Configuration

Optional. Add to `~/.openjarvis/config.toml` (see
[config.example.toml](config.example.toml)):

```toml
[jarvis.jobs]
# database_path = ""          # default: <OPENJARVIS_HOME>/jarvis/jobs.db
# max_attempts = 2            # 1 or 2
# retry_delay_seconds = 60    # back-off before a temporary failure is retried (0–3600)
# poll_seconds = 2            # idle worker: how often it looks for work
# heartbeat_seconds = 10      # running job: how often the worker reports in
# stale_after_seconds = 120   # no heartbeat this long: worker presumed dead (≥ 3 × heartbeat)
# notifications = "log"       # "log", or "macos" (log + Notification Center)
```

A custom `database_path` must be absolute, its folder must already exist
(so an unplugged drive fails cleanly instead of creating folders), and it
must not be a symbolic link. Unknown keys and out-of-range values are
configuration errors.

## Python

```python
from jarvis_ext.jobs import open_job_store

with open_job_store() as store:                  # reads [jarvis.jobs]
    job = store.submit("video.render", {"plan": {...}})
    running = store.claim_next("worker-1", kinds=["video.render"])
    store.complete(running.id, "worker-1", {"output": "reel_v001.mp4"})
    print(store.get(job.id).status, store.events(job.id))
```

## Tests

```bash
uv run pytest tests/jarvis_ext/test_job_store.py tests/jarvis_ext/test_job_runner.py \
    tests/jarvis_ext/test_job_worker.py tests/jarvis_ext/test_job_progress.py \
    tests/jarvis_ext/test_job_notifications.py tests/jarvis_ext/test_job_revisions.py -v
```

`test_job_revisions.py` covers revision payloads and the `revise` CLI
(child job and `parent_id`, output name kept or filled in, unfinished,
missing, other-kind parents and invalid plans refused before queueing,
`--no-reuse`, parent untouched, next version rendered, a failed revision
leaving earlier files unchanged), the provenance record (contents, no
absolute paths, unchanged without provenance, a source changing during
the render), reuse (no render, re-validation, history, notifications,
equivalent plan spellings, reuse of a reuse) and every refusal falling
back to a fresh version (plan, preset, FFmpeg version, source content,
modified or deleted output, deleted or altered record, wrong checksum,
`--no-reuse`, failed validation, missing review files, failed,
cancelled, unvalidated and legacy jobs), and resuming (with and without
the `rendered` note, records without provenance, same name but another
job, changed inputs, a corrupted file, ambiguity, interrupted validation,
last attempt, cancellation). These use the real render tool with fake
FFmpeg. Its real-FFmpeg tests run submit → revise (v002, v001 unchanged)
→ identical revise reused → v002 corrupted → v003 rendered, and a
published-but-unrecorded render resumed by a restarted worker with real
validation and exactly one version.

`test_job_notifications.py` covers message text for each outcome (and
that error messages, paths and payloads never appear), text cleaning of
hostile input, the outbox (one row per final status, none for
non-final changes, once-only claiming during polling, lease recovery,
back-off, giving up, ownership), 6 threads with separate connections
delivering 30 notifications exactly once, the v3 → v4 upgrade, the
worker (after a job, cancelled jobs, restart recovery, a failing
notifier never failing a job or stopping the worker), the macOS command
(fixed script, `--`, argument list, no shell, minimal environment),
osascript failures, timeouts, a missing or non-executable osascript,
other platforms, settings and the CLI. An autouse fixture makes any
attempt to run the real osascript fail the test, so no banner is shown.

`test_job_progress.py` covers parsing FFmpeg's progress report (partial,
malformed, `N/A`, duplicated, out-of-order and replaced files), the
unchanged default render command, store rules (monotonic, capped, 100
only on success, reset on requeue), throttling, events, a broken bus or
subscriber, the handler's stages, and the CLI. Its real-FFmpeg tests
check that a real render reports intermediate progress and only reaches
100 after validation, and that a cancellation after real progress stops
FFmpeg and publishes nothing.

`test_job_worker.py` covers schema upgrade, heartbeat, back-off,
recovery, the stoppable process runner, temp-folder cleanup, the worker
(once, loop, graceful and forced stop, locking, recovery on start) and
every CLI command. Its real-FFmpeg tests submit and run jobs through the
CLI (v001, an identical job reusing v001, then `--no-reuse` giving v002
with v001 unchanged), cancel a render mid-encode and
check FFmpeg is gone and nothing is published, force a worker shutdown
mid-encode, and SIGKILL a worker mid-render, then recover the job and
finish it with exactly one published version.

`test_job_runner.py` has three groups: fake handlers and fake tools (no
FFmpeg); the real `media_render` tool with fake FFmpeg; and two real-FFmpeg
tests on generated media (render + validate a job, a second job giving
`_v002`, and a real FFmpeg stopped by a cancellation publishing nothing).

`test_job_store.py` covers the transition table (every status pair),
submission validation, FIFO claiming and kind filters, ownership, retry
limits, cancellation, history, persistence across reopening, schema and
foreign-file refusal (file unchanged), file permissions, concurrent
claiming across threads and processes, and `[jarvis.jobs]` settings.

## Known limitations

* **A SIGKILLed (or crashed) worker leaves FFmpeg running.** FFmpeg runs
  in its own process session, so it finishes its encode on its own. It only
  writes inside its temp folder; nothing is published without the worker,
  and the folder is cleaned up once it is old. Stopping the worker with
  Ctrl-C/SIGTERM does stop FFmpeg.
* **A much smaller window can still produce an extra version.** The
  render and its record are published one after the other; if the worker
  dies between the two (or the record cannot be written), nothing ties
  the file to the job and the next attempt renders the next version.
  Nothing is overwritten.
* **Older renders cannot be resumed or reused.** Records without a
  `provenance` block (Phase 2, steps 1–5, or `media-render`) are never
  associated with a job; such a job interrupted after publishing still
  fails with `validate_interrupted`.
* **Reuse and provenance hash the sources.** Every job render reads each
  source once more to checksum it (and reuse checks do the same); for
  very large footage on a slow drive this adds noticeable time. A
  cancellation is only noticed once hashing is done and FFmpeg starts.
* **Reuse re-validates.** A reused output gets a new preview and
  thumbnail version in `review/` (never overwritten).
* **The input key is conservative.** Moving the workspace, the FFmpeg
  binary or a source (even unchanged), or upgrading FFmpeg, prevents reuse
  even if the output would be identical. JARVIS's own code version is
  not part of the key: a change to how commands are built changes the
  command and so the key, but a change to the pre-publish checks alone
  does not.
* **A forced stop or a crash uses an attempt.** With `max_attempts = 2`,
  a job interrupted twice fails.
* **macOS and Linux only.** The worker lock uses `fcntl.flock`.
* **Progress events are in-process.** Only code running in the worker
  process sees `jarvis_job_progress`; the CLI reads the database.
* **The render percentage assumes encoding speed is even.** FFmpeg reports
  the time encoded so far; scenes that encode slower or faster make the
  percentage move unevenly. Probing before FFmpeg starts and publishing
  after it ends are not measured.
* **Python 3.10 is not verified.** The project supports Python 3.10–3.13;
  the job code was only tested on Python 3.13 (3.10 is not installed here).
* **Run jobs through the worker.** Calling `run_next` directly, without the
  worker's lock and heartbeat, is for tests: a worker would treat such a
  long-running job as abandoned after `stale_after_seconds`.
* **Notifications are at least once.** A worker that dies in the instant
  between showing a notification and recording it shows it again after
  restart (once the lease expires). A notification is sent only while a
  worker runs.
* **macOS delivery is not confirmed.** osascript exiting 0 means the
  request was accepted; if notifications are turned off for it in System
  Settings, nothing appears and JARVIS cannot tell. The log line is
  always written.
* **No pruning.** Finished jobs, their history and notification rows are
  kept indefinitely.
* **WAL needs a local filesystem.** On a network share SQLite may fall back
  to its rollback journal (logged as a warning); keep the database local.
