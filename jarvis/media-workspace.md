# `media_workspace_inspect` — read-only media-workspace inspection

The first JARVIS capability. It lets an agent (or you, from the command line)
see which video and audio files are in one folder you have explicitly
authorised, along with their technical metadata. It never changes anything.
This is the foundation the Phase 2 editing workflow will plan from: "use the
strongest 45 seconds of the Skyline footage" starts with knowing which clips
exist, how long they are, and whether they are portrait or landscape.

Did OpenJarvis already have this? No. `file_read` reads text files of up to
1 MB, `audio_transcribe` sends audio to Whisper, and nothing in the codebase
called ffprobe or listed media. `file_read` + `shell_exec` (`ls`, `ffprobe`)
could approximate it, but only by granting unrestricted shell access.

## Configuration

Add to `~/.openjarvis/config.toml` (or whichever file `$OPENJARVIS_CONFIG`
points to):

```toml
[jarvis.media_workspace]
root = "/Users/you/Movies/JARVIS"   # required; absolute; ~ is expanded
# follow_symlinks = false           # true: follow links whose target stays inside root
# include_hidden = false            # dot-files, including macOS "._*" AppleDouble files
# probe_metadata = true             # false: list files only, never run ffprobe
# ffprobe_path = ""                 # default: "ffprobe" on PATH
# max_files = 100                   # per call, 1–1000
# max_depth = 8                     # folder depth below the inspected folder, 0–32
# ffprobe_timeout_seconds = 15      # per file, 1–120
# probe_budget_seconds = 60         # total ffprobe time per call, 1–600
# video_extensions = [".mp4", ".mov", ...]   # replaces the default list
# audio_extensions = [".wav", ".mp3", ...]
```

The core OpenJarvis loader ignores `[jarvis.*]` sections, so they do not
affect anything else. Invalid settings (relative root, root set to `/` or to
your entire home directory, unknown keys, wrong types, out-of-range numbers)
make the tool return a configuration error. They never silently widen access.

Default extensions:

* Video: `.3gp .avi .flv .m2ts .m4v .mkv .mov .mp4 .mpeg .mpg .mts .mxf .webm .wmv`
* Audio: `.aac .aif .aiff .caf .flac .m4a .mp3 .ogg .opus .wav .wma`

## Dependencies

* Python ≥ 3.10 and OpenJarvis' normal dependencies. No new packages.
* **Optional:** FFmpeg's `ffprobe` for metadata (`brew install ffmpeg` on the
  Mac). Without it the tool still lists files and adds a warning.

## Usage

**Agent:** enable the tool (`[tools] enabled = "media_workspace_inspect"` or
`--tools media_workspace_inspect`). Parameters, all optional:

| Parameter | Type | Default | Meaning |
|---|---|---|---|
| `subpath` | string | whole workspace | Folder or single file, relative to the root |
| `recursive` | bool | `true` | Include sub-folders |
| `media_type` | `all` / `video` / `audio` | `all` | Filter |
| `include_metadata` | bool | `true` | Run ffprobe |
| `max_files` | int | `max_files` setting | Lower the per-call cap |

**Command line (no LLM needed):**

```bash
uv run python -m jarvis_ext media-inspect                       # everything
uv run python -m jarvis_ext media-inspect "Skyline" --video     # one folder, video only
uv run python -m jarvis_ext media-inspect "Skyline/keynote.mp4" # one file
uv run python -m jarvis_ext media-inspect --no-metadata --config ./my.toml
```

**Python:**

```python
from jarvis_ext.tools.media_workspace import MediaWorkspaceInspectTool
result = MediaWorkspaceInspectTool().execute(subpath="Skyline", media_type="video")
```

## Output

`ToolResult.content` is compact JSON (shown expanded and trimmed here):

```json
{
  "workspace": "JARVIS",
  "inspected_path": "Skyline",
  "recursive": true,
  "media_type": "video",
  "summary": {"files_returned": 2, "video_files": 2, "audio_files": 0,
              "total_size_bytes": 1830042113, "truncated": false,
              "metadata_probed": true, "metadata_ok": 1, "metadata_failed": 1,
              "total_duration_seconds": 312.48},
  "files": [
    {"path": "Skyline/phone_clip.mov", "type": "video", "extension": ".mov",
     "size_bytes": 912004113, "modified": "2026-09-14T17:02:11+00:00",
     "metadata": {"status": "ok", "container": "mov,mp4,m4a,3gp,3g2,mj2",
                  "duration_seconds": 312.48, "bit_rate": 23348112,
                  "stream_counts": {"video": 1, "audio": 1, "subtitle": 0, "data": 0},
                  "video": {"codec": "hevc", "profile": "Main 10", "width": 3840,
                            "height": 2160, "frame_rate": 29.97, "pixel_format": "yuv420p10le",
                            "rotation": 90, "display_width": 2160, "display_height": 3840,
                            "orientation": "portrait"},
                  "audio": {"codec": "aac", "sample_rate": 48000, "channels": 2,
                            "channel_layout": "stereo"}}},
    {"path": "Skyline/broken.mp4", "type": "video", "extension": ".mp4",
     "size_bytes": 918038000, "modified": "2026-09-14T17:30:00+00:00",
     "metadata": {"status": "invalid_media", "error": "broken.mp4: Invalid data found when processing input"}}
  ],
  "skipped": {"hidden": 3, "unsupported_extension": 1, "symlink": 1},
  "warnings": []
}
```

`metadata.status` is one of `ok`, `invalid_media`, `timeout`, `probe_failed`,
`unreadable`, `skipped` (the file changed during inspection) or
`skipped_time_budget`. `orientation` takes the rotation flag that phones write
into account. One bad file never fails the whole call.

## Security model

| Threat | Mitigation |
|---|---|
| Reading outside the workspace via `../`, absolute paths, `~`, drive letters or NUL bytes | Rejected before touching the filesystem; every resolved path must stay inside the resolved root |
| Escaping via symbolic links | Links skipped by default, and a `subpath` through a link is refused. With `follow_symlinks = true`, only links whose fully resolved target is inside the root are used; directory loops are detected |
| Modifying source media | The tool never opens files itself. ffprobe only reads. Tests hash every file before and after a real ffprobe run |
| Shell injection | ffprobe runs from an argument list with no shell. The path is passed as `file:<absolute path>`, so names starting with `-` or containing `:` cannot be read as options or protocols |
| Crafted media making ffprobe fetch URLs or other files (for example an HLS playlist renamed `.mp4`) | `-protocol_whitelist file` and a demuxer `-format_whitelist` that excludes playlist/indirection formats (hls, concat, image2...). Verified against real ffprobe |
| Hangs (huge files, FIFOs, slow network drives) | Non-regular files are skipped. 15 s per-file timeout, 60 s total budget, and the OpenJarvis executor timeout as a backstop |
| Leaking paths or untrusted text to the model | Only workspace-relative paths are returned; ffprobe errors are trimmed and absolute paths stripped; config-file errors name only the file (`config.toml`). Free-text tags (title, comment) are never returned, and codec strings are reduced to a safe character set. **File names themselves are untrusted** and the tool description tells the model so |
| Permission control | `ToolSpec.required_capabilities = ["file:read"]`, enforced by OpenJarvis RBAC when a capability policy is active (tested) |

## Known limitations

* **macOS status.** The test suite passes on macOS 26.5.1 (Apple Silicon,
  Python 3.13 via uv, Homebrew FFmpeg 9.0.2), including real-ffprobe,
  FIFO, unreadable-directory and symlink tests. The exception is the RBAC
  test, which needs the Rust extension. FFmpeg 9 reports
  `-display_rotation 90` as `+90` (FFmpeg 6: `-90`); both are treated as
  portrait. Real iPhone/iPad footage matched QuickTime's Inspector (see
  [ROADMAP.md](ROADMAP.md)). The tested iPhone 17 Pro clip stores portrait
  natively (1080×1920, no rotation matrix), so the rotation-swap path has
  only been exercised with generated rotated files.
* **Frame rate is an average.** `frame_rate` is ffprobe's `avg_frame_rate`.
  For variable-frame-rate phone footage it can differ from QuickTime's
  "Encoded FPS". For example, a 60 fps iPhone clip with dropped frames shows
  59.891 here and 59.98 in QuickTime, and a 30 fps iPad clip shows 30.032
  versus 30.03. Phase 2 rendering should choose an explicit output rate
  rather than copying this value.
* **Case-insensitive subpaths echo the request.** On APFS,
  `subpath="CLIP.MOV"` finds `clip.mov` but reports the path as spelled in
  the request. Phase 2 should normalise to the on-disk name.
* Location and other QuickTime metadata tags (GPS, device model, creation
  date) are deliberately not returned.
* **Time-of-check/time-of-use.** A process that can write to the workspace
  could swap a file for an escaping symlink between the final check and the
  ffprobe call. The window is tiny and the worst outcome is ffprobe reading
  metadata of another file. The threat model assumes only you write to the
  workspace.
* **Case-insensitive filesystems (APFS default).** Containment checks compare
  paths as strings. A symlink whose target spells the root with different
  letter case is refused (fails closed). Ordinary paths are unaffected.
* **Network/cloud-synced folders** (iCloud Drive, Dropbox "online-only"):
  ffprobe may trigger a download or time out. Those files are reported as
  `timeout`, but probing can still be slow.
* **No content analysis.** No scene detection, transcription, thumbnails or
  "strongest moments" yet. That is Phase 2.
* Directory walk order is depth-first, with files listed before sub-folders.
  `max_files` truncates in that order.

## Tests

```bash
uv run pytest tests/jarvis_ext -v
```

* `tests/jarvis_ext/test_config.py`: config-file lookup, malformed TOML,
  setting validation.
* `tests/jarvis_ext/test_media_workspace.py`: registration and zero-argument
  construction, RBAC enforcement, an `OrchestratorAgent` calling the tool
  through a mocked engine, enumeration and filters, traversal and symlink
  attacks, FIFOs, ffprobe command construction, every failure status, the
  time budget, hostile ffprobe output, the CLI, and real-FFmpeg integration
  tests (rotated phone footage, broken files, a disguised HLS playlist, and
  byte-for-byte proof that source files are unchanged).
