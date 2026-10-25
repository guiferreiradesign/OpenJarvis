# Deterministic video pipeline: `media_render` and `media_validate`

Phase 2 adds two tools that turn an explicit **edit plan** into a finished,
checked vertical video:

* **`media_render`** executes an `EditPlan` with FFmpeg. It trims clips from
  the media workspace, joins them in order, fits them to the output preset,
  encodes H.264/AAC MP4 and can add subtitles and a PNG logo. It writes a new
  versioned file (`reel_v001.mp4`) to a separate output folder.
* **`media_validate`** proves the result is usable. It compares the file
  with the render record, probes it, decodes every frame, and only then
  creates a low-resolution preview and a thumbnail for review.

Neither tool makes creative decisions. Choosing clips and in/out points is
the caller's job (you today; an LLM in Phase 8). A plan always renders the
same way.

```
EditPlan JSON ──► media_render ──► <output>/reel_v001.mp4 + reel_v001.render.json
                    │ validates plan, paths, sources (Phase 1 path rules + ffprobe)
                    │ renders in <output>/.jarvis-tmp/job-*/  (private, removed after)
                    │ probes + checks the result BEFORE giving it a name
                    ▼
               media_validate ──► checksum, probe, per-stream durations, full decode
                    │ only if everything passed:
                    ▼
               <output>/review/reel_v001_preview_v001.mp4  (360×640)
               <output>/review/reel_v001_thumb_v001.jpg
               ready_for_review: true
```

## Configuration

Add a `[jarvis.media_render]` section next to the Phase 1 workspace section
in `~/.openjarvis/config.toml` (or the file `$OPENJARVIS_CONFIG` points to).
See [config.example.toml](config.example.toml).

```toml
[jarvis.media_workspace]               # Phase 1: where source footage is (read-only)
root = "/Users/you/Movies/JARVIS"

[jarvis.media_render]
output_root = "/Users/you/Movies/JARVIS-Renders"   # required; must already exist
# assets_root = "/Users/you/Movies/JARVIS-Assets"  # optional; branding PNGs
# ffmpeg_path = ""                     # default: "ffmpeg" on PATH
# ffprobe_path = ""                    # default: media_workspace.ffprobe_path, then "ffprobe"
# default_preset = "instagram_reel"
# require_confirmation = false         # true: the agent must ask before every render
# render_timeout_seconds = 900         # 10–7200; FFmpeg is stopped after this
# validate_timeout_seconds = 300       # 10–3600; per validation step
# max_clips = 20                       # 1–100 clips per plan
# max_output_seconds = 600             # 1–3600; presets may set a lower cap
# duration_tolerance_seconds = 0.25    # 0.01–5; default for plans
# preview_height = 640                 # 144–1080

# Optional extra presets (built-in "instagram_reel" cannot be replaced):
# [jarvis.media_render.presets.square_post]
# width = 1080
# height = 1080
# frame_rate = 30                      # 24, 25, 30, 50 or 60
# crf = 20                             # 14–32
# x264_preset = "medium"
# audio_bitrate_kbps = 128             # 64–320
# max_duration_seconds = 60
```

Rules (checked when the config loads, and again on every call):

* `output_root` must be an absolute path to an **existing** folder. It is
  not created automatically, so a path on an unplugged drive fails cleanly
  instead of creating a stray folder under `/Volumes`. It cannot be `/` or
  your home directory.
* `output_root` and the media workspace may not be the same folder or contain
  each other. This is compared by device and inode, so `~/movies/jarvis` and
  `~/Movies/JARVIS` (same folder on case-insensitive APFS), or a symlinked
  alias, are recognised as the same place.
* `assets_root` and `output_root` must be separate.
* Unknown keys, wrong types and out-of-range values are configuration errors.
  The tools then refuse to run; access is never silently widened.

### Built-in preset

| Preset | Resolution | Aspect | FPS | Video | Audio | Max length |
|---|---|---|---|---|---|---|
| `instagram_reel` | 1080×1920 | 9:16 | 30 | H.264 High, CRF 20, x264 `medium`, yuv420p | AAC 128 kb/s, 48 kHz stereo | 180 s |

### Dependencies

* FFmpeg and ffprobe (Homebrew FFmpeg 9.0.2 was used for testing). The
  pipeline needs the `libx264`, `aac`, `mjpeg` and `mov_text` encoders. The
  tool checks which encoders and filters the installed FFmpeg has, and
  refuses a plan it cannot render, naming what is missing.
* No new Python packages. Python standard library plus what OpenJarvis
  already depends on.

## The `EditPlan`

A plan is a JSON object. Unknown keys are errors at every level, so a typo
fails instead of being ignored. A complete example is in
[examples/edit-plan.example.json](examples/edit-plan.example.json).

```json
{
  "version": 1,
  "output_name": "skyline_reel",
  "preset": "instagram_reel",
  "fit": "crop",
  "clips": [
    {"source": "Skyline/keynote.mp4", "start": "00:00:05.0", "end": "00:00:12.5"},
    {"source": "Skyline/crowd.mov",   "start": 30,           "end": 41.2}
  ],
  "video": {"frame_rate": 30, "crf": 20, "x264_preset": "medium"},
  "audio": {"mode": "keep", "bitrate_kbps": 128},
  "subtitles": {"source": "Skyline/captions.srt", "mode": "embed", "language": "eng"},
  "branding": {"asset": "logo.png", "position": "top-right",
               "width_fraction": 0.2, "margin_px": 48, "opacity": 0.9},
  "duration_tolerance_seconds": 0.25
}
```

| Field | Required | Values | Meaning |
|---|---|---|---|
| `version` | no | `1` | Plan format version |
| `output_name` | **yes** | 1–48 chars, `a-z 0-9 _ -`, starting with a letter or digit | File stem; the tool adds `_vNNN.mp4` |
| `preset` | no | a configured preset | Default: `default_preset` |
| `clips` | **yes** | 1–`max_clips` objects | Rendered **in list order** |
| `clips[].source` | **yes** | workspace-relative path to a video file | Same path rules as `media_workspace_inspect` |
| `clips[].start`, `end` | **yes** | seconds (number) or `[[HH:]MM:]SS[.ffffff]` | In/out points in the source. `end > start`, at least 0.1 s, and `end` no later than the source's probed duration |
| `fit` | no | `crop` (default), `pad` | `crop` fills the frame and centre-crops. `pad` fits the whole picture and adds black bars |
| `video.frame_rate` | no | 24, 25, 30, 50, 60 | Constant output rate (VFR phone footage is converted) |
| `video.crf` | no | 14–32 | x264 quality (lower = better, larger) |
| `video.x264_preset` | no | `ultrafast` … `veryslow` | Speed/size trade-off |
| `audio.mode` | no | `keep` (default), `none` | `keep`: each clip's first audio track, resampled to 48 kHz stereo. Clips without audio get silence (with a warning). `none`: no audio track |
| `audio.bitrate_kbps` | no | 64–320 | AAC bitrate |
| `subtitles.source` | no | workspace-relative `.srt` (≤ 1 MB, UTF-8) | Cue times are relative to the **output**, not to a source clip |
| `subtitles.mode` | no | `embed` (default), `burn` | `embed`: a selectable `mov_text` track. `burn`: drawn into the picture (needs FFmpeg with libass; see limitations) |
| `subtitles.language` | no | ISO 639-2 code, e.g. `eng`, `por` | Track language tag |
| `branding.asset` | no | `.png` relative to `assets_root` (≤ 10 MB, ≤ 8192 px) | Overlay for the whole video |
| `branding.position` | no | `top-left`, `top-right` (default), `bottom-left`, `bottom-right`, `center` | |
| `branding.width_fraction` | no | 0.05–0.5 (default 0.2) | Logo width as a fraction of the output width; aspect ratio kept |
| `branding.margin_px` | no | 0–400 (default 48) | Distance from the edges |
| `branding.opacity` | no | 0.1–1.0 (default 1.0) | |
| `duration_tolerance_seconds` | no | 0.01–5 | Allowed difference between expected and actual duration |

The total duration (the sum of clip lengths) must not exceed the preset's
`max_duration_seconds` or `max_output_seconds`.

## Colour

Every render is SDR **Rec.709, limited ("TV") range, 8-bit 4:2:0**, and is
tagged that way. The tags are accurate because each clip is converted in
its own `scale` step, not just relabelled:

| Source | What happens |
|---|---|
| SDR, limited range (most cameras) | Scaled; colour unchanged |
| SDR, **full range** (`yuvj420p`, many iPhone/iPad clips) | Pixel values rescaled to limited range (black 0→16, white 255→235). Before this fix such clips produced a full-range output, which the pre-publish check rejected |
| SDR with other primaries/matrix | Converted to Rec.709 when the scale filter supports colour management; otherwise only the matrix and range are converted |
| **HDR** (HLG `arib-std-b67` or PQ `smpte2084`, e.g. iPhone HDR video) | Tone-mapped to SDR Rec.709 with swscale's **perceptual** intent (`scale=…:intent=perceptual`). The render adds a warning asking you to review its colours. Tested on real HLG footage only; see limitations |
| HDR, but FFmpeg cannot tone-map | **Refused** (`unsupported`), never relabelled as SDR |

The tool decides from `ffmpeg -h filter=scale`. Colour management needs
`out_primaries`/`out_transfer`; tone mapping also needs the `intent`
option and the HLG/PQ transfers. Detection is by these options, not by
version number. Homebrew FFmpeg 9.0.2 has all of them.
`zscale` is not needed. Note that in a `-filter_complex` graph the
command-line `-intent` option is ignored, so the intent is set inside the
`scale` filter.

## Rendering a video

**Command line (no LLM needed):**

```bash
uv run python -m jarvis_ext media-render plan.json            # render only
uv run python -m jarvis_ext media-render plan.json --validate # render, then validate
cat plan.json | uv run python -m jarvis_ext media-render -    # plan from stdin
uv run python -m jarvis_ext media-validate skyline_reel_v001.mp4
uv run python -m jarvis_ext media-validate skyline_reel_v001.mp4 --thumbnail-at 2.5
```

Exit status is 0 only for a successful render (and, with `--validate`, a
passed validation). Results are JSON on stdout (success) or stderr (failure).

**Agent:** enable the tools, for example
`[tools] enabled = "media_workspace_inspect,media_render,media_validate,think"`.
A typical flow: inspect, write a plan from the reported durations, call
`media_render`, then `media_validate`, and only then present the preview.

**Python:**

```python
from jarvis_ext.tools.media_render import MediaRenderTool
from jarvis_ext.tools.media_validate import MediaValidateTool

render = MediaRenderTool().execute(plan={...})          # or plan='{"output_name": ...}'
check = MediaValidateTool().execute(output="skyline_reel_v001.mp4")
```

`MediaRenderTool(report_progress=True)` additionally asks FFmpeg for its
`-progress` report in the render's temp folder (used by the Phase 3 job
runner; see jobs.md). It is off by default, and the render command is then
unchanged.

### `media_render` result

```json
{
  "render_status": "succeeded",
  "validation_status": "not_validated",
  "ready_for_review": false,
  "next_step": "Call media_validate with output='skyline_reel_v001.mp4'.",
  "output": {"file": "skyline_reel_v001.mp4", "version": 1, "size_bytes": 1580478,
             "sha256": "45f3…", "duration_seconds": 6.0, "width": 1080,
             "height": 1920, "frame_rate": 30.0, "has_audio": true},
  "record": "skyline_reel_v001.render.json",
  "settings": { "...normalised plan, times in seconds..." },
  "prepublish_checks": [{"check": "dimensions", "passed": true,
                         "expected": "1080x1920", "actual": "1080x1920"}, "..."],
  "ffmpeg_version": "9.0.2",
  "elapsed_seconds": 1.97,
  "warnings": ["Skyline/crowd.mov has no audio; silence is used."]
}
```

A successful render always says `ready_for_review: false`. Failures have
`success = false` and a `render_status` of:

| `render_status` | Meaning | Anything written? |
|---|---|---|
| `config_error`, `not_configured` | Config missing or invalid, or output folder overlaps the workspace | No |
| `workspace_unavailable`, `output_unavailable` | Folder missing (drive unplugged?), not a directory, or not writable | No |
| `invalid_plan` | Malformed plan, unknown keys, bad times, missing files, out-point past the end of a clip, bad subtitles/logo | No |
| `access_denied` | Path outside the workspace/assets folder, via `..`, absolute, `~`, symlink escape, or hidden file | No |
| `invalid_media` | A source cannot be probed or has no video | No |
| `ffmpeg_missing`, `ffmpeg_unusable`, `unsupported` | FFmpeg not found, broken, or lacking a needed encoder/filter | No |
| `ffmpeg_failed` | FFmpeg exited with an error (last log lines included, paths redacted) | Temp dir only, removed |
| `timeout` | Exceeded `render_timeout_seconds`; FFmpeg's process group was stopped | Temp dir only, removed |
| `failed_checks` | FFmpeg finished but the file does not match the plan; **not published** | Temp dir only, removed |
| `output_error` | Filesystem error while publishing | Temp dir removed |

If the render is interrupted (Ctrl-C, or the process being stopped), FFmpeg
is terminated and the temp directory removed before the interruption
propagates.

### `media_validate` result

```json
{
  "validation_status": "passed",
  "ready_for_review": true,
  "output": "skyline_reel_v001.mp4",
  "checks": [
    {"check": "matches_render_record", "passed": true, "actual": "match", "...": ""},
    {"check": "non_empty"}, {"check": "probe"}, {"check": "container"},
    {"check": "duration"}, {"check": "video_duration"}, {"check": "audio_duration"},
    {"check": "video_stream"}, {"check": "dimensions"}, {"check": "frame_rate"},
    {"check": "video_codec"}, {"check": "color_range"}, {"check": "color_metadata"},
    {"check": "pixel_format"}, {"check": "audio_stream"},
    {"check": "audio_codec"}, {"check": "audio_sample_rate"},
    {"check": "subtitle_track"}, {"check": "decodes_cleanly"}
  ],
  "preview": "review/skyline_reel_v001_preview_v001.mp4",
  "thumbnail": "review/skyline_reel_v001_thumb_v001.jpg",
  "expected": {"width": 1080, "height": 1920, "frame_rate": 30.0, "duration": 6.0, "...": ""},
  "warnings": []
}
```

| Check | Passes when |
|---|---|
| `matches_render_record` | Size and SHA-256 equal those recorded at render time, so the file is complete and has not been replaced |
| `non_empty`, `probe`, `container` | Non-empty, ffprobe reads it (same hardened ffprobe as Phase 1), MP4 family |
| `duration` | Container duration within tolerance of the plan |
| `video_duration`, `audio_duration` | **Each stream** within tolerance (catches a video track that stops early while padded audio keeps the container long) |
| `dimensions` | Exactly the preset size, no rotation flag |
| `frame_rate` | Within 0.05 fps of the plan |
| `video_codec`, `pixel_format` | `h264`, `yuv420p` |
| `color_range`, `color_metadata` | Limited (`tv`) range; primaries, transfer and matrix all `bt709` |
| `audio_stream`, `audio_codec`, `audio_sample_rate` | AAC 48 kHz present when `audio.mode = keep`; absent when `none` |
| `subtitle_track` | A subtitle stream exists when `subtitles.mode = embed` |
| `decodes_cleanly` | `ffmpeg -xerror` decodes every frame (only run when the checks above pass) |

`ready_for_review` is `true` only when every check passed **and** (unless
`preview: false`) both the preview and the thumbnail were created and
verified (preview probed at the expected height, thumbnail starts with a
JPEG header). `validation_status` is `passed`, `failed`,
`passed_without_review_files`, or an argument/config error such as
`not_found`, `no_record` (the file was not produced by `media_render`) or
`invalid_arguments`. The tool's `success` flag equals `ready_for_review`.

## Reviewing the result

Everything is in the output folder:

```bash
open ~/Movies/JARVIS-Renders/review/skyline_reel_v001_preview_v001.mp4   # QuickTime
open ~/Movies/JARVIS-Renders/review/skyline_reel_v001_thumb_v001.jpg     # Preview
open ~/Movies/JARVIS-Renders/skyline_reel_v001.mp4                       # full quality
cat  ~/Movies/JARVIS-Renders/skyline_reel_v001.render.json               # what produced it
```

The preview is 360×640 (CRF 30) so it is quick to share or scrub. The
thumbnail is taken from the middle of the video unless `--thumbnail-at` /
`thumbnail_at_seconds` says otherwise.

## Output folder, versioning and filesystem restrictions

```
<output_root>/
    skyline_reel_v001.mp4
    skyline_reel_v001.render.json     plan, preset, checks, size, SHA-256, FFmpeg version
                                      (+ job provenance for job renders, jobs.md step 6)
    skyline_reel_v002.mp4             a later render of the same output_name
    review/skyline_reel_v001_preview_v001.mp4
    review/skyline_reel_v001_thumb_v001.jpg
    .jarvis-tmp/                      in-progress work only; emptied after every call
```

* **Writes happen only under `output_root`**, in `.jarvis-tmp/job-*`
  (created with `mkdtemp`, mode 0700), `review/`, and the root itself. Both
  sub-folders are refused if they are symlinks or not directories.
* **Nothing is overwritten.** FFmpeg runs with `-n` and writes a fixed name
  inside its private temp directory. A finished file gets its final name
  through `link(2)`, which fails if the name exists. On filesystems without
  hard links (for example exFAT) the name is reserved with
  `O_CREAT | O_EXCL` first. When a name is taken, the next version is tried.
* **Versions:** the next number is one more than the highest existing
  `<output_name>_vNNN.*` (any extension, so a leftover record also reserves
  its number). Up to `v9999`. Concurrent renders always get distinct
  numbers; this is tested with 24 threads, and with 3 real parallel FFmpeg
  renders.
* **Publish only after checks.** The render is probed and checked in the
  temp directory; a file that fails any check never receives a final name.
* **Cleanup:** the temp directory is removed after every call, whether it
  succeeded, failed, timed out or was interrupted. Only directories this code
  created (`.jarvis-tmp/job-*`) are ever deleted. Existing files are never
  deleted. If the process is killed hard (SIGKILL, power loss), a
  `job-*` folder can be left behind; it is safe to delete by hand.
* **Source footage is never modified.** Sources are only ever FFmpeg/ffprobe
  *inputs*, opened read-only. Tests hash every workspace file before and
  after real renders.
* **Narrow write permission:** both tools declare
  `required_capabilities = ["file:read", "file:write"]`. Under an OpenJarvis
  RBAC policy, an agent without `file:write` cannot render (tested). Set
  `require_confirmation = true` to make the executor ask before each render.

## Security model

| Threat | Mitigation |
|---|---|
| Reading outside the workspace (`..`, absolute, `~`, drive letters, NUL, symlinks, hidden files) | Phase 1's resolver, now shared in `jarvis_ext/_workspace.py`, is used for clips and subtitles. The assets folder uses the same rules (symlinks never followed) |
| Writing outside the output folder, or into the workspace | Fixed layout under `output_root`. Output and workspace must be disjoint, checked by inode on every call |
| Overwriting files | `-n`, private temp dir, `link(2)` / `O_EXCL` publishing |
| Shell or filter injection through file names or plan values | Argument lists, no shell. Paths are passed only as `file:<abs path>` input arguments. The filter graph contains only numbers and fixed keywords. Subtitles are rewritten to a fixed `subs.srt` and FFmpeg runs with the temp dir as cwd, so no path ever appears in a filter. Tested with names like `-y.mp4`, `$(touch pwned).mp4`, `it's [0:v] a,b=c.mov`, `concat:a|b` |
| Crafted media making FFmpeg open URLs or other files | Every input uses `-protocol_whitelist file` and Phase 1's demuxer whitelist (no HLS/concat/image sequences). The logo is forced through the `png_pipe` demuxer, subtitles through `srt` |
| Malicious subtitle files | Strict SRT parser, UTF-8 only, ≤ 1 MB, ≤ 5000 cues. Override blocks `{…}`, tags `<…>` and control characters are stripped. FFmpeg reads only the sanitised copy |
| Leaking location/device metadata into a shared video | `-map_metadata -1 -map_chapters -1` on every output |
| Hangs and runaway processes | Timeouts for every step. FFmpeg runs in its own process group and is stopped with SIGTERM, then SIGKILL, on timeout or interruption |
| Leaking secrets into FFmpeg | It runs with only `PATH`, `LC_ALL=C` and `HOME` (needed by fontconfig for burn-in) |
| Leaking absolute paths to the model | Results contain only workspace/output-relative names. FFmpeg log tails have workspace, output, assets and temp paths replaced with placeholders. Config errors name only the file |

## Captions and speech-to-text

* **Supplied subtitles** (`.srt`) are supported, embedded as a selectable
  track (works with any FFmpeg) or burned in (needs libass).
* **Automatic transcription is not wired into rendering.** OpenJarvis
  already ships a local `faster-whisper` speech backend
  (`src/openjarvis/speech/faster_whisper.py`), but the `faster-whisper`
  package is not installed in this environment (it is in the optional
  `speech` extra) and its first use downloads a Whisper model (about 75 MB
  for `tiny`, 145 MB for `base`). Both need your approval, so neither was
  done.
* **Extension point:** `jarvis_ext.video.subtitles.transcribe_to_srt(backend,
  audio_bytes)` turns any OpenJarvis `SpeechBackend` result into clean SRT
  (tested with a fake backend). Once approved, the flow would be: extract
  the audio of the rendered video (an FFmpeg step), transcribe it locally,
  write the SRT into the workspace, then render again with
  `subtitles.source`. No paid transcription service is used anywhere.

## Tests

```bash
uv run pytest tests/jarvis_ext -v                 # everything (needs FFmpeg for the real tests)
uv run pytest tests/jarvis_ext -k "Real" -v       # only the real-FFmpeg integration tests
```

| File | Covers |
|---|---|
| `test_edit_plan.py` | Timestamps, valid/invalid plans (35 rejection cases), presets, `[jarvis.media_render]` settings, separation (symlink alias, letter case on APFS), SRT parsing/sanitising, transcription hook, output checks including per-stream durations |
| `test_video_output.py` | Version naming, no-overwrite publishing, the scan/claim race, 24-thread concurrent allocation, the no-hard-link fallback, temp-dir privacy and cleanup, symlinked sub-folders, FFmpeg command construction with hostile file names, filter graphs, logo geometry, capability parsing, real process timeout/interrupt (child really stopped), log redaction |
| `test_media_render.py` | Tool registration, zero-argument construction, RBAC `file:write` enforcement, executor confirmation, mocked render success/failure/timeout/interrupt, traversal and symlink escapes (clips, subtitles, logo), output overlapping the workspace, missing FFmpeg, burn-in availability, sanitised subtitles, bad outputs never published, validation (tampered, truncated, wrong metadata, decode failure, preview failure, bad names, symlinked output), CLI. **Real FFmpeg:** full plan (crop, logo, embedded subtitles) rendered and validated, then checked independently with ffprobe; pad + no audio + 25 fps + versioning; 3 parallel renders; a real timeout; a corrupted render caught by checksum and, separately, by the decode pass; truncated sources never published. Source files hashed before/after |

**Results on this Mac** (macOS 26.5.1 arm64, Python 3.13.17, FFmpeg 9.0.2,
2026-10-25): `uv run pytest tests/jarvis_ext` gives **388 passed, 0 skipped**
(117 Phase 1 + 255 Phase 2). See [ROADMAP.md](ROADMAP.md) for the wider
suite.

## Known limitations

* **Real footage: one technical test so far.** A 15 s reel from three real
  iPhone/iPad clips (two full-range SDR landscape, one HLG HDR portrait)
  rendered and validated; see [ROADMAP.md](ROADMAP.md). Cut accuracy and
  A/V sync were not measured frame by frame, and Instagram's acceptance of
  the output is unverified.
* **External drives are untested.** An output folder on an external or
  exFAT drive (no hard links, so the `O_EXCL` reservation fallback is
  used) has only been exercised with mocked `link(2)` failures. A
  missing/unplugged output folder is tested and fails cleanly.
* **No subtitle burn-in with Homebrew's FFmpeg 9.0.2.** It is built without
  libass and freetype, so the `subtitles` filter (and `drawtext`) are
  missing. `mode: "burn"` is refused with a clear message, and `embed` is
  the default. Instagram ignores embedded tracks, so burned captions need an
  FFmpeg with libass. Getting one means installing different software
  (not done; needs your approval). The burn-in path is covered only by
  mocked tests.
* **HDR tone mapping: what is and is not verified.** HLG and PQ sources
  are tone-mapped by FFmpeg's swscale perceptual intent (see "Colour"
  above); the quality is FFmpeg's, not JARVIS's.
  * Verified: one real iPhone HLG/BT.2020 clip, checked by eye only (it
    looked natural); a generated HLG clip in the automated tests, which
    only proves the pixels were converted, not that they look right.
  * Not verified: PQ (`smpte2084`) footage (only the generated command is
    tested), HDR from other phones or cameras, other HLG scenes (very
    bright highlights, night shots), and how Instagram or other platforms
    display the result. There is no objective colour-accuracy test.
  * Dolby Vision dynamic metadata, if a file carries it, is ignored; only
    the HLG/PQ signal ffprobe reports is used.
  * With an FFmpeg whose scale filter lacks these options, HDR clips are
    refused rather than mislabelled.
* **Short audio is padded with silence** to each clip's length (so A/V stays
  in sync across clips); this is reported only when a clip has no audio at
  all. A truncated *video* track is caught by the per-stream duration check.
* **One audio track per clip** (the first). No music bed, ducking, fades or
  transitions. Clips are hard cuts.
* **Times are relative to each source** as decoded by FFmpeg (after any
  container edit list). For variable-frame-rate phone clips, cuts land on
  the nearest frame of the constant-rate output.
* **Case-insensitive paths echo the request**, as in Phase 1: on APFS,
  `Skyline/CLIP.MOV` finds `clip.mov`, and the plan/record keep the spelling
  you used.
* **Leftover temp folders after a hard kill** (see above) are not cleaned
  by the tools themselves. The Phase 3 job worker removes `job-*` folders
  once they are older than any live call could be (see jobs.md).
* **Validation needs the render record.** Files not produced by
  `media_render` (or whose `.render.json` was deleted) are reported as
  `no_record` rather than guessed at.
* Rendering runs synchronously inside the tool call. Long renders block the
  agent until done (bounded by `render_timeout_seconds`); background jobs
  with progress are Phase 3.
