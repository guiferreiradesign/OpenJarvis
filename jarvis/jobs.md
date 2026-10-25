# Jobs: tracking long-running work (Phase 3)

Phase 3 turns long-running work into **jobs**, starting with renders. A job
is tracked from submission to a final status, can be cancelled, and is
retried only when that is safe.

**What exists today (step 1):** the job model, its status rules and the
SQLite job store. Nothing executes jobs yet: there is no worker, runner,
CLI, progress, notification or agent tool. Those are steps 2–7 in
[ROADMAP.md](ROADMAP.md). The Phase 2 render pipeline is unchanged.

The job layer is shared by every JARVIS skill. It knows nothing about video,
FFmpeg, HyperFrames or notifications (DECISIONS.md D25):

```
skill (later: video-editing skill, research, ...)
   │  submits kind + JSON payload, reads status/result
   ▼
jarvis_ext/jobs  ── store.py: SQLite, status rules, claiming, history
   ▲
   │  claims jobs of the kinds it handles, reports the outcome
worker + runner per kind  (step 2–3; first kind: "video.render")
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
  (`PRAGMA application_id`) with schema version 1 (`PRAGMA user_version`).
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
# database_path = ""   # default: <OPENJARVIS_HOME>/jarvis/jobs.db
# max_attempts = 2     # 1 or 2
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
uv run pytest tests/jarvis_ext/test_job_store.py -v
```

`test_job_store.py` covers the transition table (every status pair),
submission validation, FIFO claiming and kind filters, ownership, retry
limits, cancellation, history, persistence across reopening, schema and
foreign-file refusal (file unchanged), file permissions, concurrent
claiming across threads and processes, and `[jarvis.jobs]` settings.

## Known limitations (step 1)

* **No execution yet.** Jobs can be submitted and moved through their
  states only by code calling the store.
* **A job left `running` by a worker that died stays `running`.** Recovery
  needs a worker heartbeat, planned for step 3.
* **No pruning.** Finished jobs and their history are kept indefinitely.
* **WAL needs a local filesystem.** On a network share SQLite may fall back
  to its rollback journal (logged as a warning); keep the database local.
