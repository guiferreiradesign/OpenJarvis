# OpenJarvis audit (Phase 1)

Audited on 2026-10-24 against commit `4741d5d` (upstream `main` as forked),
by reading source in `src/openjarvis/`, the tests and the docs, and by running
the test suite. The checkout is **shallow** (88 commits of history), so
history-based questions (when or why something changed) were not
investigated.

## Summary

OpenJarvis is a large, mature Python framework (Apache-2.0, Python
3.10–3.13, an optional Rust/PyO3 extension, and a React/Tauri desktop app).
It already provides most of the "agent OS" plumbing JARVIS needs: agents,
tool calling, a security layer, memory, a scheduler, channels, speech, an
OpenAI-compatible server and a GUI. What it does **not** provide is anything
specific to the JARVIS vision: media processing, control of a remote Mac,
desktop computer use, or a task/job dashboard.

The right strategy is to **extend, not fork-and-modify**. New capabilities
plug into the existing registries from a separate package
(`src/jarvis_ext`).

## Component findings

| Area | What exists (verified in source) | Reuse for JARVIS |
|---|---|---|
| **Extension mechanism** | Decorator registries in `core/registry.py` (`ToolRegistry`, `AgentRegistry`, `EngineRegistry`, `MemoryRegistry`, `ChannelRegistry`, `SkillRegistry`, ...). No entry-point plugin loading; built-ins register by being imported from each package's `__init__.py` | **Yes**: the JARVIS hook is one guarded import |
| **Agents** | `simple`, `orchestrator` (function calling), `native_react`, `native_openhands` (CodeAct), `rlm`, `operative` / `monitor_operative` (persistent), `deep_research`, `morning_digest`, `proactive_agent`, `claude_code` (Claude Agent SDK via Node), `opencode` | `orchestrator` is the natural driver for tool workflows |
| **Tools** | `BaseTool` + `ToolSpec` (`tools/_stubs.py`). `ToolExecutor` adds rate limiting, RBAC capabilities, taint tracking (blocks "read secret → send over network"), confirmation prompts, prompt-injection scanning of non-local tool output, timeouts on a bounded worker pool, and event-bus tracing. About 60 built-in tools | **Yes**: the JARVIS tool uses all of this unchanged |
| **Tool construction** | Every call site (`cli/ask.py`, `system/orchestrator.py`, `agents/tool_resolver.py`, `cli/serve.py`, `agents/executor.py`) creates tools with **no constructor arguments**, apart from memory/channel/llm/file tools | Extension tools must load their own config |
| **Security** | `security/capabilities.py`: RBAC with a canonical capability table that every in-tree tool must appear in (enforced by a test). Also `file_policy` (blocks secret files), `ssrf`, taint, an injection scanner, an audit log, and Docker/WASM sandboxes (`sandbox/`) | Extension tools declare capabilities in `ToolSpec` |
| **Skills** | `skills/`: TOML step pipelines and agentskills.io `SKILL.md`; importers for Hermes/OpenClaw/GitHub; signature verification and injection scanning available; a skill optimiser in `learning/` | Use later for reusable "edit a reel" procedures; review third-party skills first |
| **Engines / models** | Local: Ollama, vLLM, SGLang, llama.cpp, MLX, LM Studio, Exo, Nexa, Uzu, Apple FM, Gemma.cpp, Lemonade. Cloud (`engine/cloud.py`): OpenAI, Anthropic, Gemini, OpenRouter, MiniMax, DeepSeek, Atlas, Codex; LiteLLM and NIM. Routing/learning layer | See the Claude section below |
| **Memory** | `memory/`: a persistent fact store with background extraction and trust levels. `tools/storage/`: SQLite FTS5, BM25, FAISS, ColBERT, hybrid, knowledge graph. `memory_files` (markdown memory) | Covers "remember preferences" in Phase 6 |
| **Scheduling** | `scheduler/`: cron/interval/once with SQLite persistence and agent tools (`schedule_task`, ...). `operators/`: persistent tick-based agents defined by TOML manifests. `daemon/` | Phase 3 job runner can build on this |
| **Server / API** | FastAPI (`server/`): OpenAI-compatible `/v1/chat/completions`, agent manager, approvals queue, dashboard and analytics routes, WebSocket bridge, API-key auth middleware. Defaults to `127.0.0.1:8000` | Phase 5/7 backend |
| **Frontend** | `frontend/`: React 19 + Vite + Tailwind, packaged as a Tauri 2 desktop app (`desktop/`) | Starting point for the Phase 7 dashboard |
| **Channels** | About 35 channel adapters (Slack, Telegram, email, Signal, iMessage via AppleScript, webhook, ...) | Completion notifications (Phase 3/6) |
| **Speech** | Whisper (local faster-whisper and OpenAI), Deepgram; TTS: Kokoro (local), OpenAI, ElevenLabs, Cartesia | Voice (Phase 6) |
| **Deployment** | `deploy/`: Docker, systemd, launchd, Windows service; `render.yaml` | Phase 5 options |
| **Tests** | About 9,600 pytest tests, ruff lint/format, pre-commit; `conftest.py` isolates `OPENJARVIS_HOME` and clears registries before each test. CI: `uv sync --extra dev --extra server ...`, `maturin develop`, `pytest -n auto -m "not live and not cloud and not hub"` | Followed for `tests/jarvis_ext` |
| **Roadmap** (`docs/development/roadmap.md`) | Operator hardening, mobile/messaging clients, a unified notification system ("Design Needed"), a plugin ecosystem, and voice ("Research-Stage") | Notifications/plugins align with JARVIS Phases 3 and 6 |

## The capabilities you asked about specifically

| Capability | Status | Detail |
|---|---|---|
| **File access** | Exists, restricted | `file_read` (text, ≤ 1 MB) and `file_write`, limited to `[security] allowed_dirs` when set (**unrestricted when empty**), and blocks sensitive files. No media awareness |
| **Shell / command execution** | Exists, **unrestricted** | `shell_exec` runs any command as your user, with no allowlist (stated in `docs/user-guide/system-access.md`). `docker_shell_exec` and the sandbox runner isolate it in Docker. **Not enabled for JARVIS** |
| **Browser automation** | Exists | Playwright tools (`browser_navigate/click/type/screenshot/extract/axtree`). Drives a headless browser, not your desktop or your logged-in browser |
| **Visual desktop control** | **Does not exist** | No screenshot/mouse/keyboard tooling for the OS. `osascript` is used only to send iMessages. Computer use needs new work (Phase 4) |
| **Remote access to a local Mac** | **Does not exist** | Nothing lets a cloud instance drive tools on another machine. The A2A protocol (`a2a/`) and the MCP client (`mcp/`, `tools/mcp_adapter.py`) are possible transports, but no worker, authentication model or tunnelling exists. Phase 4/5 work |

## Claude: subscription versus API (important for cost)

What the code supports:

* The `cloud` engine's Anthropic path and the `claude_code` agent both
  authenticate **only** with `ANTHROPIC_API_KEY` (`engine/cloud.py`,
  `agents/claude_code.py`, `claude_code_runner/index.mjs`). That is an
  Anthropic API (Console) key, which is **billed per token, separately from a
  Claude Pro subscription**.
* There is no code path that uses a Claude.ai login or Pro subscription.

What this means:

* **Developing JARVIS** with Claude Code (as in this session) is covered by
  your Claude Pro plan, within its usage limits.
* **Running JARVIS** with Claude as its runtime model needs an API key. That
  is a new, separately billed service, so it was **not set up** in this phase
  and needs your explicit approval.
* I did not find, and do not assume, any officially supported way to use a
  Pro subscription as a permanent, unattended inference backend for your own
  agent. Anthropic's Agent SDK documentation says third-party products should
  authenticate with API keys rather than claude.ai login. Check Anthropic's
  current terms before relying on any subscription-based workaround.
* **Zero-cost option:** OpenJarvis' default is local models through Ollama.
  On the Mac (Apple Silicon) a small local model can drive tool calls such as
  `media_workspace_inspect` for free. Quality for complex planning will be
  below Claude's. A hybrid (local by default, Claude via API only for hard
  planning steps, with a spend cap) fits the existing routing layer.

## Verification limits in this environment

* The cloud container is Linux, not macOS. Nothing touching your Mac, its
  files, apps or footage was attempted or simulated.
* No inference engine (Ollama or API key) was available, so the agent
  integration was tested with a mocked engine, which is the upstream test
  pattern.
* Network: four upstream tests that make real HTTP/DNS calls
  (`tests/tools/test_web_search.py` ×2, `tests/server/test_connectors_router.py`
  ×2) fail here **with and without** the JARVIS changes (checked by stashing
  the changes).
* The Rust extension had to be built locally (`maturin develop`), as CI does.
  Without it about 26 security tests fail on import.
