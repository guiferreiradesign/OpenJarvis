# Architecture decisions

Short records of decisions made during Phase 1, so they can be revisited
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

## Assumptions

* Only you (and processes you trust) write to the media workspace. See the
  TOCTOU note in media-workspace.md.
* JARVIS runs as your user. OS-level isolation (a separate macOS user or a
  container for the worker) is a Phase 4 topic.
* Claude as the runtime model needs a separately billed API key. Until you
  approve that, local models through Ollama are the zero-cost path (see
  AUDIT.md).
