# Jobs: tracking long-running work (Phase 3)

Phase 3 turns long-running work into **jobs**, starting with renders. A job
is tracked from submission to a final status, can be cancelled, and is
retried only when that is safe.

**What exists today (steps 1–3):** the job model, its status rules, the
SQLite job store, a runner that executes one `video.render` job through
the existing render and validate tools, and a worker with a `jobs` CLI
(submit, list, show, cancel, worker). There is no progress reporting,
notification or agent tool yet; those are steps 4–7 in
[ROADMAP.md](ROADMAP.md). The Phase 2 render pipeline is unchanged apart
from one additive process-control function (D29).

The job layer is shared by every JARVIS skill. It knows nothing about video,
FFmpeg, HyperFrames or notifications (DECISIONS.md D25):

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
[video-pipeline.md](video-pipeline.md)). Any other key, or a missing or
empty plan, fails the job (`invalid_payload`) before anything runs.

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
| A tool's report is unreadable or contradicts itself | `failed` | `unexpected_result` |
| Cancellation requested before the render | `cancelled`; nothing rendered | |
| Cancellation requested after the render, before validation | `cancelled`; the result lists the file as published but **not validated** | |
| Work finished before the cancellation took effect | The real outcome (`succeeded` / `failed`) | |

The result keeps `output`, `ready_for_review`, and a compact `render` and
`validate` summary (version, size, SHA-256, record, preview, thumbnail,
names of failed checks, up to 50 warnings).

All Phase 2 output guarantees still apply, because the tools do the work:
nothing is overwritten (a second job for the same plan produces `_v002`),
and a render is only named after its checks pass. A render whose FFmpeg is
stopped publishes nothing and leaves no temp folder (tested with real
FFmpeg).

## The worker and the `jobs` CLI (step 3)

```bash
uv run python -m jarvis_ext jobs submit plan.json      # queue a video.render job (or - for stdin)
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
report (`processed`, `recovered`, `cleaned_temp_dirs`, `stopped`) when it
exits.

**One worker, one job at a time.** The worker takes an exclusive `flock`
on `<database>.worker.lock` for as long as it runs; a second worker exits
with "Another job worker is already running". The kernel drops the lock
when the process ends for any reason, so a crashed worker never blocks the
next. Jobs run strictly one after another (D20).

**Stopping.** The first Ctrl-C or SIGTERM lets the current job finish and
then exits. A second one stops the current job now: FFmpeg is stopped and
the job is recorded as interrupted (requeued if it was still rendering,
failed with `validate_interrupted` if its render was already published,
because re-running would create a second version). A forced stop uses up
one of the job's attempts.

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
| Its history shows a published render (`rendered <file>`) | `failed`, `validate_interrupted`; the file is listed as not validated. It is not re-rendered, so no second version is created |
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
  currently 2; version 1 files are upgraded in place when opened).
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
    tests/jarvis_ext/test_job_worker.py -v
```

`test_job_worker.py` covers schema upgrade, heartbeat, back-off,
recovery, the stoppable process runner, temp-folder cleanup, the worker
(once, loop, graceful and forced stop, locking, recovery on start) and
every CLI command. Its real-FFmpeg tests submit and run jobs through the
CLI (v001, then v002 with v001 unchanged), cancel a render mid-encode and
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
* **A tiny window can still produce an extra version.** If the worker dies
  in the milliseconds between publishing a render and recording
  `rendered <file>`, recovery cannot know the file exists and re-renders,
  publishing the next version (nothing is overwritten). Closing this needs
  a job id in the render record (D24), not done without your approval.
* **Interrupted jobs are not resumed at validation.** A job interrupted
  after its render was published fails with the file listed; validate it
  with `media-validate` and re-submit if needed. Resuming needs the same
  D24 change.
* **A forced stop or a crash uses an attempt.** With `max_attempts = 2`,
  a job interrupted twice fails.
* **macOS and Linux only.** The worker lock uses `fcntl.flock`.
* **Python 3.10 is not verified.** The project supports Python 3.10–3.13;
  the job code was only tested on Python 3.13 (3.10 is not installed here).
* **Run jobs through the worker.** Calling `run_next` directly, without the
  worker's lock and heartbeat, is for tests: a worker would treat such a
  long-running job as abandoned after `stale_after_seconds`.
* **No pruning.** Finished jobs and their history are kept indefinitely.
* **WAL needs a local filesystem.** On a network share SQLite may fall back
  to its rollback journal (logged as a warning); keep the database local.
