# Architecture decisions

Short records of decisions made during Phases 1 and 2, so they can be revisited
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

## Assumptions

* Only you (and processes you trust) write to the media workspace. See the
  TOCTOU note in media-workspace.md.
* JARVIS runs as your user. OS-level isolation (a separate macOS user or a
  container for the worker) is a Phase 4 topic.
* Claude as the runtime model needs a separately billed API key. Until you
  approve that, local models through Ollama are the zero-cost path (see
  AUDIT.md).
