# Preflight: the standard verification step

`python -m jarvis_ext preflight` checks the running application and its
essential parts and prints one `PASS`, `FAIL` or `WARN` line per check, with
the measured time of every check that actually ran. It exits with status 1 if
any check fails, so it can gate a commit or a report.

```bash
uv run python -m jarvis_ext preflight                 # everything, offline-safe
uv run python -m jarvis_ext preflight --list          # the checks and their budgets
uv run python -m jarvis_ext preflight --group jobs    # one group (app media jobs brain future)
uv run python -m jarvis_ext preflight --only media.render
uv run python -m jarvis_ext preflight --json          # machine-readable
uv run python -m jarvis_ext preflight --config ~/other/config.toml
# brain checks follow 'jarvis serve' precedence; pass the same options you serve with:
uv run python -m jarvis_ext preflight --group brain --engine cloud --model <model>
```

Code: `src/jarvis_ext/preflight/` (`core.py` runner and report, `fixtures.py`
synthetic media, `checks/<group>.py`). Tests: `tests/jarvis_ext/test_preflight.py`.
Decisions: D38 in [DECISIONS.md](DECISIONS.md).

## The rule

> A change to critical JARVIS functionality is not reported as complete until
> the relevant tests **and** the relevant preflight checks have been run and
> their real results reported.

"Critical" means anything the checks below cover: configuration loading, the
server and its secret handling, media inspection, the video pipeline and its
validation, the job store/runner/worker, the AI-provider selection and request
building, and (as they arrive) the voice, notes and brain-switching features.

In practice, before saying "done":

1. `uv run pytest tests/jarvis_ext -q` (plus any other suite your change
   touches).
2. `uv run python -m jarvis_ext preflight` (or `--group` for the area you
   changed, if you also report the full run separately).
3. Report the counts and name every `FAIL` and `WARN`. Never describe a
   preflight as "the whole application passes": it covers only the checks it
   lists, and says so at the end of its output.

A failing check is fixed or reported as failing. It is not skipped,
reclassified or deleted to obtain a green result. A new capability gets a real
check in the same change (see "Adding a check").

## What the statuses mean

| Status | Meaning |
|---|---|
| `PASS` | The component was exercised and behaved correctly. |
| `FAIL` | A component that exists is broken, or a required one is missing. Any `FAIL` makes the exit status 1. |
| `WARN` | Not a failure of anything that exists: an optional component is absent, the check could not run (`NOT RUN: ...`, shown without a timing), a roadmap feature is not implemented yet, or a check passed but took longer than its latency budget. |

A `WARN` is never evidence that something works. `NOT RUN` means exactly
that, and the opt-in checks below are reported this way until you ask for
them.

Every check has a latency budget (`--list` shows it). A passing check that
exceeds its budget is downgraded to `WARN`, so a slowdown is visible without
being called a failure.

## What it checks

**A. Application and configuration**

| Check | What it does |
|---|---|
| `app.rust_ext` | The compiled `openjarvis_rust` extension imports. It is required; there is no Python fallback. |
| `app.config` | `config.toml` and the five `[jarvis.*]` sections load and validate. A configured but missing media or output folder is a `WARN`. |
| `app.registration` | All six JARVIS tools are registered with OpenJarvis. |
| `app.server` | The real `create_app` factory starts in-process (with its lifespan) and answers `/health` and `/v1/info`. It uses a stub engine, so it proves the app and routes, **not** your configured brain or the `jarvis serve` command. |
| `app.secrets_served` | Plants canary API keys in the environment, builds the app, requests about 16 endpoints and secret-file paths, and fails if a canary or any real configured secret appears in a body or header. |
| `app.secrets_frontend` | Scans `frontend/src` and the built `server/static` for secret-shaped strings (OpenAI/Anthropic, NVIDIA, Google, GitHub, Slack keys, private keys) and for the exact values of keys configured on this machine. Findings name the file and pattern only, never the value. |
| `app.spawn_server` | **Opt-in** (`--spawn-server`). Starts the documented `jarvis serve` on a free local port, checks `/health`, stops it. It uses your configured brain and probes your engines (which can include a network health request). |

**B. Media inspection and video rendering** (synthetic fixture, never your media)

| Check | What it does |
|---|---|
| `media.ffmpeg` | `ffmpeg` and `ffprobe` (or the configured paths) run, and FFmpeg has `libx264` and `aac`. Missing binaries are a `FAIL`. |
| `media.fixture` | Generates two tiny clips, a logo and an SRT in a private temp folder. If FFmpeg cannot, this is a `WARN` and the checks below are `NOT RUN`; nothing is invented. |
| `media.inspect` | `media_workspace_inspect` finds both clips with the right size, orientation and duration, and leaves the sources unchanged (SHA-256). |
| `media.render` | A real FFmpeg render of an edit plan: file exists, 1080x1920, expected duration, sources unchanged. |
| `media.validate` | `media_validate` runs the project's own rules on that render (all 18 checks, full decode) and creates preview and thumbnail. |

**C. Job management** (an isolated temporary database; never `jobs.db`)

| Check | What it does |
|---|---|
| `jobs.create_retrieve` | Create, read back, list, find by id prefix, history starts at `queued`. |
| `jobs.validation` | Bad kind, non-object/oversize/non-JSON payload, bad `max_attempts`, unknown parent, and bad `video.render` payloads are refused and nothing is stored. |
| `jobs.transitions` | `queued -> running -> succeeded` is recorded; completing a queued job, a second claim, a non-owner, or changing a finished job is refused. |
| `jobs.failure_recorded` | A handler that fails, raises, returns a non-outcome, or returns an unstorable result ends `failed` with the right code. |
| `jobs.retry` | A temporary failure is re-queued once, then fails at the attempt limit. |
| `jobs.cancel` | Queued cancel is immediate; a running job that honours cancel ends `cancelled`; work that finished anyway stays `succeeded`; an unrequested "cancelled" becomes `failed`. |
| `jobs.no_false_success` | A render failure, an inconsistent render report, a failed validation, an invalid payload, and the real tools given a missing source file never end `succeeded`. |
| `jobs.real_pipeline` | A real `video.render` job through the production wiring (stoppable process runner, progress, provenance): renders, validates, output exists, progress 100, sources unchanged. |

**D. AI provider (brain)** — reads the configuration as `jarvis serve` does and never changes it

| Check | What it does |
|---|---|
| `brain.config` | Reports `[engine] default`, the model and agent, and that the engine is registered. No default model is a `WARN`. |
| `brain.credentials` | The provider's key variables are present (names only, never values); a hosted endpoint without its key is a `FAIL`. |
| `brain.request` | Builds the engine with the same factory, calls it with a one-line prompt while every HTTP request is intercepted and every non-loopback socket is blocked, and checks the method, endpoint, model, message and credential header. **Nothing is sent. This is not a connectivity test.** |
| `brain.live` | **Opt-in** (`--live-brain`). One real request (8 tokens max) that may use free-tier quota or bill. Without the flag it is `NOT RUN ... NOT TESTED`, never a pass. |

**E. Voice services and planned capabilities**

| Check | What it does |
|---|---|
| `future.speech_output` | Optional. Local speech service (currently Voicebox) health and voice-profile resolution on loopback. Not running is a `WARN`; no audio is generated. |
| `future.speech_input` | Optional. MLX Whisper is importable. The model is not loaded and nothing is transcribed. |
| `future.speech_cloud_fallback` | `WARN` if an OpenAI, Cartesia or ElevenLabs key is configured. Upstream's speech auto-discovery treats such a backend as healthy whenever its key exists, so if the local service is down, reply text would go to that cloud provider. Nothing is sent by the check. |
| `future.voice_adapter`, `future.whisper_loop`, `future.wake_word`, `future.notes`, `future.brain_switch`, `future.privacy_audit` | One placeholder per roadmap step. `NOT IMPLEMENTED` is a `WARN`. When the module appears the placeholder keeps warning ("no integration check is registered") until a real check replaces it. |

## What it does not do

* It does not test your real media, your real job database, or any real
  project, and it never writes outside its own temporary folder (deleted at
  the end).
* It does not call a paid or quota-limited API unless you pass `--live-brain`.
* The brain checks prove configuration and request shape. Only a
  `--live-brain` run, reported as such, shows the provider answering.
* `app.server` uses a stub engine. `--spawn-server` is the check that uses the
  documented start procedure.
* It does not verify the browser UI, microphone capture, playback, Gmail
  connectors, or anything in `frontend/` beyond the secret scan. Those have
  their own test suites (`npm test` in `frontend/`).
* A green run covers the listed checks only.

## Adding a check

Write a function in the module for its group (or a new module, listed in
`checks/__init__.py`):

```python
from jarvis_ext.preflight.core import Context, Outcome, check, failed, passed, warned

@check("voice.chunking", "future", "Sentences are chunked without breaking numbers", budget=2)
def chunking(ctx: Context) -> Outcome:
    ...                      # exercise the REAL code; raise Blocked / return not_run() if a prerequisite is missing
    return passed("12 sentences -> 4 chunks")
```

Rules: exercise the real component; return `failed(...)` for a component that
exists and is broken (an exception is also a `FAIL`); return `warned(...)` for
an optional component that is absent; use `not_run(...)` or raise `Blocked`
when a prerequisite is missing (it is reported, with no timing, and never
passes); set a `budget`; never print secret values; share setup through
`ctx.shared` / `ctx.workdir`. Then delete that feature's entry from
`PLANNED` in `checks/future.py` and add a test in `test_preflight.py`,
including one that breaks the component and requires the check to fail.

## Results recorded for this step

See the development report for the commands and results of the run that
accompanied the introduction of the preflight. Environment-specific results
(for example the Rust extension on a machine where it is not built) belong in
the report for that machine, not in this file.
