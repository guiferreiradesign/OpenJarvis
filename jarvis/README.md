# JARVIS — personal extensions to OpenJarvis

JARVIS is a personal AI assistant built **on top of** [OpenJarvis](https://github.com/open-jarvis/OpenJarvis)
(Apache-2.0). This folder documents everything that is specific to the JARVIS
fork. The upstream OpenJarvis docs live in [`docs/`](../docs) and are left
untouched.

| Document | Contents |
|---|---|
| [AUDIT.md](AUDIT.md) | What OpenJarvis actually provides (from reading the source), what is missing, and the Claude authentication findings |
| [media-workspace.md](media-workspace.md) | The first JARVIS capability: read-only media-workspace inspection |
| [DECISIONS.md](DECISIONS.md) | Architecture decisions and assumptions |
| [ROADMAP.md](ROADMAP.md) | Phases 1–8 and the checklist of things to verify on the Mac |
| [config.example.toml](config.example.toml) | Example `[jarvis.*]` configuration |

---

## Where custom code lives

Everything custom is in three places, so upstream updates can be merged with
few conflicts:

```
src/jarvis_ext/            # JARVIS extension package (custom code only)
    __init__.py            #   registers extension tools; ensure_registered()
    __main__.py            #   `python -m jarvis_ext ...` runner (no LLM needed)
    config.py              #   reads [jarvis.<name>] sections from config.toml
    tools/
        __init__.py        #   tool table -> OpenJarvis ToolRegistry
        media_workspace.py #   media_workspace_inspect tool
tests/jarvis_ext/          # tests for the extension package
jarvis/                    # this documentation
```

The fork touches upstream files in exactly two places:

| File | Change |
|---|---|
| `src/openjarvis/tools/__init__.py` | One guarded `import jarvis_ext` at the end (the `try/except ImportError` pattern used for built-in tools). If the extension is present but broken it logs a warning; it never breaks the core |
| `pyproject.toml` | `src/jarvis_ext` added to the wheel `packages` list |

## Architecture (current and planned)

```
                ┌──────────────────────────── OpenJarvis core (upstream) ───────────────────────────┐
 you ── CLI /   │  agents (orchestrator, native_react, ...)  ─►  ToolExecutor  ─►  ToolRegistry      │
 desktop GUI ──►│     │        RBAC capabilities · taint tracking · confirmation · timeouts       │   │
 (later: your   │     ▼                                                                         │   │
  dashboard)    │  engines: Ollama / vLLM / llama.cpp / MLX ... / cloud (Anthropic, OpenAI ...)  │   │
                │  memory · scheduler · operators · channels · speech · server (FastAPI)        │   │
                └───────────────────────────────────────────────────────────────────────────────│───┘
                                                                                                ▼
                ┌──────────── JARVIS extensions (src/jarvis_ext) ────────────┐
                │  media_workspace_inspect   (Phase 1, implemented)           │
                │  media render / validate   (Phase 2, planned)               │
                │  job status, notifications (Phase 3, planned)               │
                └─────────────────────────────────────────────────────────────┘
   later:  Mac worker (Phase 4)  ◄── secure channel ──►  cloud orchestrator (Phase 5)
```

| Layer from the plan | Where it lives today |
|---|---|
| OpenJarvis core | `src/openjarvis` (upstream, unmodified apart from the hook above) |
| JARVIS extensions | `src/jarvis_ext` |
| JARVIS frontend | Not started. Upstream ships a React/Vite + Tauri app in `frontend/` that can be reused or replaced (Phase 7) |
| Local Mac worker | Not started (Phase 4). Today the whole stack runs on one machine |
| Cloud orchestration | Not started (Phase 5). Upstream has Docker, systemd, launchd and Render deployment files in `deploy/` |
| Persistent data | Upstream SQLite stores (memory, traces, scheduler, sessions) under `~/.openjarvis/` |

## Capability status

| Capability | Status | Notes |
|---|---|---|
| Media-workspace inspection (list + ffprobe metadata) | **Implemented** (JARVIS) | Tested with real FFmpeg 6.1 (Linux) and FFmpeg 9.0.2 on macOS 26.5 (Apple Silicon), including real iPhone/iPad footage checked against QuickTime |
| Text chat, multi-step tool use | **Available upstream** | Needs an inference engine (local Ollama or an API key) |
| Restricted file read/write | **Available upstream** | `file_read` / `file_write` + `[security] allowed_dirs` |
| Shell execution | **Available upstream, deliberately not enabled** | `shell_exec` has no allowlist; see AUDIT.md |
| Browser automation | **Available upstream** | Playwright tools; web pages only, not the desktop |
| Visual desktop control (Mac computer use) | **Missing** | No screenshot/click/keyboard tooling in the codebase |
| Remote access to a local Mac | **Missing** | No worker/agent protocol for this; Phase 4 |
| Scheduling / background jobs | **Available upstream** | `TaskScheduler` (cron/interval/once) + operators |
| Memory / preferences | **Available upstream** | Fact store + retrieval backends |
| Approvals | **Partial upstream** | Interactive confirmation + a proactive-agent approval queue with REST routes |
| Voice | **Available upstream** | Whisper STT and several TTS backends (some paid) |
| Email / notifications | **Available upstream** (channels) | Not configured; sending is out of scope for Phase 1 |
| Video rendering / editing | **Planned** | Phase 2 |
| Custom dashboard | **Planned** | Phase 7 |

## Quick start

```bash
brew install uv ffmpeg rustup             # macOS prerequisites
export PATH="/opt/homebrew/opt/rustup/bin:$PATH"   # rustup is keg-only
rustup default stable                     # Rust >= 1.89
uv sync --extra dev                       # install (Python 3.10–3.13)
uv run maturin develop --release --manifest-path rust/crates/openjarvis-python/Cargo.toml
cat >> ~/.openjarvis/config.toml <<'EOF'
[jarvis.media_workspace]
root = "/Users/you/Movies/JARVIS"         # a dedicated folder, not your home dir
EOF
uv run python -m jarvis_ext media-inspect                # whole workspace
uv run python -m jarvis_ext media-inspect "Skyline" --video
```

To let an agent use the tool, enable it like any other tool:

```toml
[tools]
enabled = "media_workspace_inspect,think"
```

```bash
uv run jarvis ask --agent orchestrator --tools media_workspace_inspect \
  "What footage is in my Skyline folder?"
```

(That last step needs an inference engine, which this phase did not set up. See AUDIT.md.)

## Running the tests

```bash
uv run pytest tests/jarvis_ext -v
```

The real-FFmpeg tests skip automatically if `ffmpeg`/`ffprobe` are missing.
The compiled Rust extension (`openjarvis_rust`, built by the `maturin
develop` step above) is **required**, not optional. It has no Python fallback.
Without it, `test_executor_enforces_file_read_capability` fails, and so
does a real agent run, because the RBAC policy, rate limiter,
injection/secret scanners, SSRF and file policies, `think` and `file_read`
all load it. The standalone `python -m jarvis_ext` runner does not need it.
Rebuild it after pulling upstream Rust changes. The wider OpenJarvis suite
also needs the server extras, as in CI:

```bash
uv sync --extra dev --extra framework-comparison --extra server --extra inference-cloud
uv run maturin develop --manifest-path rust/crates/openjarvis-python/Cargo.toml
uv run pytest tests/ -n auto -q -m "not live and not cloud and not hub"
```

## Adding a new JARVIS tool

1. Create `src/jarvis_ext/tools/<name>.py` with a `BaseTool` subclass
   (`spec` property + `execute(**params) -> ToolResult`). Use
   `media_workspace.py` as the template.
2. Give the constructor only keyword arguments with defaults. OpenJarvis builds
   tools with **no arguments**, so read settings with
   `load_extension_section("<name>")` from `jarvis_ext.config`.
3. Declare `required_capabilities` in the `ToolSpec` (`file:read`,
   `file:write`, `code:execute`, `network:fetch` ...). Tools outside
   `openjarvis.tools` are governed by what they declare.
4. Add it to `_TOOLS` in `src/jarvis_ext/tools/__init__.py`.
5. Add `tests/jarvis_ext/test_<name>.py`. Test isolation clears registries
   before every test, so call `jarvis_ext.ensure_registered()` when a test
   needs the registry.
6. Write errors as `ToolResult(success=False, ...)` rather than raising
   exceptions, and keep absolute paths out of what the model sees.

## Adding a skill

OpenJarvis skills are TOML step pipelines or `SKILL.md` documents
(agentskills.io format) that call existing tools. The loader is in
`src/openjarvis/skills/`; examples are in `src/openjarvis/skills/data/`.
Put JARVIS skills in `~/.openjarvis/skills/<skill-name>/`, the default
`[skills] skills_dir`. Some bundled skills call `shell_exec`
(for example `file-organizer` runs `mkdir`). Read a skill before enabling it,
and do not `jarvis skill install` third-party skills without reviewing them.
