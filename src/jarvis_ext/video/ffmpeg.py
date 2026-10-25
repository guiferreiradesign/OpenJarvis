"""FFmpeg argument construction and process control.

Every command is an argument **list** executed without a shell.  Nothing from
a plan is ever interpolated into a filter graph as free text:

* Source files are passed as ``file:<absolute path>`` *inputs*, so names that
  start with ``-`` or contain ``:``, ``,``, ``'`` or ``[`` cannot become
  options, protocols or filter syntax.
* The filter graph contains only numbers computed here and fixed keywords.
* Sanitised subtitles are written to a fixed file name (``subs.srt``) inside
  the private render directory, and FFmpeg runs with that directory as its
  working directory, so the ``subtitles`` filter never sees a user path.
* Inputs are restricted with ``-protocol_whitelist file`` and the same demuxer
  whitelist ``media_workspace_inspect`` uses (no playlists, no concat lists).
* ``-n`` makes FFmpeg refuse to overwrite anything, and the only output is a
  fixed name in the private render directory.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, FrozenSet, List, Mapping, Optional, Sequence

from jarvis_ext.tools.media_workspace import FFPROBE_FORMAT_WHITELIST
from jarvis_ext.video.plan import EditPlan

RENDER_FILENAME = "render.mp4"
SUBTITLE_FILENAME = "subs.srt"
LOG_FILENAME = "ffmpeg.log"
PREVIEW_FILENAME = "preview.mp4"
THUMBNAIL_FILENAME = "thumbnail.jpg"

OUTPUT_FORMAT_WHITELIST = "mov,mp4,m4a,3gp,3g2,mj2"

# Every render is SDR Rec.709, limited ("TV") range, 8-bit 4:2:0.
OUTPUT_COLOR = {
    "range": "tv",
    "primaries": "bt709",
    "transfer": "bt709",
    "space": "bt709",
}
# ffprobe ``color_transfer`` values that mean the source is HDR.
HDR_TRANSFERS = frozenset({"smpte2084", "arib-std-b67"})
_HDR_NAMES = {"smpte2084": "PQ", "arib-std-b67": "HLG"}

# scale-filter options needed for real colour conversion (swscale colour
# management, detected by option name rather than version) and for HDR->SDR
# tone mapping.
_COLOR_MANAGEMENT_TOKENS = frozenset(
    {"out_range", "out_color_matrix", "out_primaries", "out_transfer"}
)
_TONE_MAPPING_TOKENS = frozenset({"intent", "perceptual", "arib-std-b67", "smpte2084"})

_MAX_LOG_TAIL_BYTES = 8192
_MAX_ERROR_CHARS = 300
_TERMINATE_GRACE_SECONDS = 5.0
_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]
# (cmd, *, cwd, timeout, log_path, env) -> return code.  Raises
# subprocess.TimeoutExpired after the process has been stopped.
ProcessRunner = Callable[..., int]


class FfmpegError(RuntimeError):
    """FFmpeg is missing, too limited, or failed."""


# ---------------------------------------------------------------------------
# Environment and capabilities
# ---------------------------------------------------------------------------


def minimal_env() -> dict:
    """The environment FFmpeg runs with: no secrets, deterministic locale."""
    env = {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"}
    # libass/fontconfig (subtitle burn-in) look for caches under HOME.
    if os.environ.get("HOME"):
        env["HOME"] = os.environ["HOME"]
    return env


@dataclass(frozen=True)
class FfmpegCapabilities:
    version: str = ""
    encoders: FrozenSet[str] = field(default_factory=frozenset)
    filters: FrozenSet[str] = field(default_factory=frozenset)
    # Option names and enum values listed by ``ffmpeg -h filter=scale``.
    scale_options: FrozenSet[str] = field(default_factory=frozenset)

    @property
    def color_management(self) -> bool:
        """The scale filter can convert primaries and transfer, not just tag."""
        return _COLOR_MANAGEMENT_TOKENS <= self.scale_options

    @property
    def tone_mapping(self) -> bool:
        """The scale filter can tone-map HLG/PQ HDR to SDR (perceptual intent)."""
        return self.color_management and _TONE_MAPPING_TOKENS <= self.scale_options

    def missing_for(self, plan: EditPlan) -> List[str]:
        """Names of encoders/filters *plan* needs that this build lacks."""
        need_encoders = {"libx264"}
        if plan.audio_mode == "keep":
            need_encoders.add("aac")
        need_filters = {"scale", "fps", "concat", "setsar", "format", "trim"}
        need_filters.add("crop" if plan.fit == "crop" else "pad")
        if plan.audio_mode == "keep":
            need_filters.update({"aresample", "aformat", "apad", "atrim", "anullsrc"})
        if plan.branding is not None:
            need_filters.add("overlay")
            if plan.branding.opacity < 1.0:
                need_filters.add("colorchannelmixer")
        if plan.subtitles is not None:
            if plan.subtitles.mode == "embed":
                need_encoders.add("mov_text")
            else:
                need_filters.add("subtitles")
        missing = sorted(need_encoders - self.encoders)
        missing += sorted(need_filters - self.filters)
        return missing


def _listing_names(text: str) -> FrozenSet[str]:
    """Second column of ``ffmpeg -encoders`` / ``-filters`` rows."""
    names = set()
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 2 and re.fullmatch(r"[A-Z.|]{2,8}", parts[0]):
            names.add(parts[1])
    return frozenset(names)


def _option_tokens(text: str) -> FrozenSet[str]:
    """First word of each row of ``ffmpeg -h filter=...`` (options and values)."""
    tokens = set()
    for line in text.splitlines():
        parts = line.split()
        if parts:
            tokens.add(parts[0].lstrip("-"))
    return frozenset(tokens)


def query_capabilities(
    ffmpeg: str, runner: Runner = subprocess.run, timeout: float = 20.0
) -> FfmpegCapabilities:
    """Ask the installed FFmpeg which encoders and filters it has."""

    def run(*args: str) -> str:
        try:
            proc = runner(
                [ffmpeg, "-hide_banner", *args],
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
                check=False,
                env=minimal_env(),
            )
        except subprocess.TimeoutExpired:
            raise FfmpegError("ffmpeg did not respond") from None
        except OSError as exc:
            raise FfmpegError(f"ffmpeg could not be started ({type(exc).__name__})")
        if proc.returncode != 0:
            raise FfmpegError("ffmpeg could not list its capabilities")
        out = proc.stdout or b""
        return out.decode("utf-8", errors="replace")

    version_line = run("-version").splitlines()[:1]
    version = ""
    if version_line:
        match = re.match(r"ffmpeg version (\S+)", version_line[0])
        if match:
            version = re.sub(r"[^A-Za-z0-9_.+-]", "", match.group(1))[:40]
    return FfmpegCapabilities(
        version=version,
        encoders=_listing_names(run("-encoders")),
        filters=_listing_names(run("-filters")),
        scale_options=_option_tokens(run("-h", "filter=scale")),
    )


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipInput:
    """A plan clip resolved to a real file and checked against its metadata."""

    path: Path
    start: float
    duration: float
    has_audio: bool
    # ffprobe colour_transfer of the source (e.g. "arib-std-b67" for HLG).
    color_transfer: Optional[str] = None

    @property
    def hdr(self) -> bool:
        return self.color_transfer in HDR_TRANSFERS


def hdr_name(transfer: Optional[str]) -> str:
    return _HDR_NAMES.get(transfer or "", "HDR")


def color_conversion(clip: ClipInput, capabilities: "FfmpegCapabilities") -> str:
    """scale-filter options that convert *clip* to the output colour space.

    Full-range ("JPEG", ``yuvj420p``) sources are converted to limited range
    in every case.  With swscale colour management the primaries and
    transfer are converted too, and HDR (HLG/PQ) sources are tone-mapped
    with the perceptual intent.  The tool refuses HDR clips when tone
    mapping is unavailable (see ``media_render``), so an HDR signal is never
    merely relabelled as Rec.709.
    """
    options = (
        f"out_range={OUTPUT_COLOR['range']}:out_color_matrix={OUTPUT_COLOR['space']}"
    )
    if capabilities.color_management:
        options += (
            f":out_primaries={OUTPUT_COLOR['primaries']}"
            f":out_transfer={OUTPUT_COLOR['transfer']}"
        )
        if clip.hdr:
            options += ":intent=perceptual"
    return options


@dataclass(frozen=True)
class LogoInput:
    path: Path
    width: int
    height: int


def png_dimensions(data: bytes) -> Optional[tuple]:
    """``(width, height)`` from a PNG header, or ``None`` if not a PNG."""
    if len(data) < 24 or not data.startswith(_PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    if width < 1 or height < 1:
        return None
    return width, height


def _even(value: float) -> int:
    return max(2, int(round(value / 2.0)) * 2)


def logo_geometry(plan: EditPlan, logo: LogoInput) -> tuple:
    """``(logo_w, logo_h, x, y)`` in output pixels for the branding overlay."""
    branding = plan.branding
    assert branding is not None
    width, height = plan.width, plan.height
    logo_w = min(_even(width * branding.width_fraction), width)
    logo_h = min(_even(logo_w * logo.height / logo.width), height)
    margin = branding.margin_px
    left, right = margin, width - logo_w - margin
    top, bottom = margin, height - logo_h - margin
    x, y = {
        "top-left": (left, top),
        "top-right": (right, top),
        "bottom-left": (left, bottom),
        "bottom-right": (right, bottom),
        "center": ((width - logo_w) // 2, (height - logo_h) // 2),
    }[branding.position]
    return logo_w, logo_h, max(0, x), max(0, y)


def _seconds(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def _geometry(plan: EditPlan, color: str) -> str:
    width, height = plan.width, plan.height
    if plan.fit == "crop":
        return (
            f"scale={width}:{height}:force_original_aspect_ratio=increase:{color},"
            f"crop={width}:{height}"
        )
    return (
        f"scale={width}:{height}:force_original_aspect_ratio=decrease:"
        f"force_divisible_by=2:{color},"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black"
    )


def build_filter_graph(
    plan: EditPlan,
    clips: Sequence[ClipInput],
    logo: Optional[LogoInput] = None,
    capabilities: Optional[FfmpegCapabilities] = None,
) -> tuple:
    """Return ``(filter_complex, video_label, audio_label_or_None)``.

    Inputs are numbered clips first, then the logo.  Only numbers and fixed
    keywords appear in the graph.
    """
    capabilities = capabilities or FfmpegCapabilities()
    fps = plan.frame_rate
    keep_audio = plan.audio_mode == "keep"
    parts: List[str] = []
    concat_inputs = ""
    for index, clip in enumerate(clips):
        duration = _seconds(clip.duration)
        geometry = _geometry(plan, color_conversion(clip, capabilities))
        parts.append(
            f"[{index}:v:0]{geometry},setsar=1,fps={fps},"
            f"trim=duration={duration},setpts=PTS-STARTPTS,format=yuv420p[v{index}]"
        )
        concat_inputs += f"[v{index}]"
        if keep_audio:
            if clip.has_audio:
                parts.append(
                    f"[{index}:a:0]aresample=48000,"
                    "aformat=sample_fmts=fltp:channel_layouts=stereo,"
                    f"apad,atrim=duration={duration},asetpts=PTS-STARTPTS[a{index}]"
                )
            else:
                parts.append(
                    "anullsrc=channel_layout=stereo:sample_rate=48000,"
                    f"atrim=duration={duration}[a{index}]"
                )
            concat_inputs += f"[a{index}]"

    count = len(clips)
    if keep_audio:
        parts.append(f"{concat_inputs}concat=n={count}:v=1:a=1[vcat][acat]")
    else:
        parts.append(f"{concat_inputs}concat=n={count}:v=1:a=0[vcat]")
    video = "vcat"

    if plan.branding is not None and logo is not None:
        logo_w, logo_h, x, y = logo_geometry(plan, logo)
        chain = f"[{count}:v:0]scale={logo_w}:{logo_h},format=rgba"
        if plan.branding.opacity < 1.0:
            chain += f",colorchannelmixer=aa={plan.branding.opacity:.3f}"
        parts.append(chain + "[logo]")
        parts.append(
            f"[{video}][logo]overlay={x}:{y}:eof_action=repeat,format=yuv420p[vbrand]"
        )
        video = "vbrand"

    if plan.subtitles is not None and plan.subtitles.mode == "burn":
        parts.append(f"[{video}]subtitles=filename={SUBTITLE_FILENAME}[vsub]")
        video = "vsub"

    return ";".join(parts), video, ("acat" if keep_audio else None)


def _input_args(path: Path, *, start: Optional[float] = None, duration=None) -> list:
    args: List[str] = []
    if start is not None:
        args += ["-ss", _seconds(start)]
    if duration is not None:
        args += ["-t", _seconds(duration)]
    return args + [
        "-protocol_whitelist",
        "file",
        "-format_whitelist",
        FFPROBE_FORMAT_WHITELIST,
        "-i",
        f"file:{path}",
    ]


def build_render_command(
    ffmpeg: str,
    plan: EditPlan,
    clips: Sequence[ClipInput],
    *,
    logo: Optional[LogoInput] = None,
    capabilities: Optional[FfmpegCapabilities] = None,
) -> List[str]:
    """Complete argument list for one render, run with cwd = render dir."""
    if len(clips) != len(plan.clips):
        raise ValueError("every plan clip needs a resolved input")
    if plan.branding is not None and logo is None:
        raise ValueError("branding requested without a logo input")

    cmd: List[str] = [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error"]
    for clip in clips:
        cmd += _input_args(clip.path, start=clip.start, duration=clip.duration)
    next_input = len(clips)
    if logo is not None:
        cmd += [
            "-protocol_whitelist",
            "file",
            "-f",
            "png_pipe",
            "-i",
            f"file:{logo.path}",
        ]
        next_input += 1
    subtitle_input = None
    if plan.subtitles is not None and plan.subtitles.mode == "embed":
        subtitle_input = next_input
        cmd += [
            "-protocol_whitelist",
            "file",
            "-f",
            "srt",
            "-i",
            f"file:{SUBTITLE_FILENAME}",
        ]

    graph, video, audio = build_filter_graph(plan, clips, logo, capabilities)
    cmd += ["-filter_complex", graph, "-map", f"[{video}]"]
    if audio:
        cmd += ["-map", f"[{audio}]"]
    if subtitle_input is not None:
        cmd += ["-map", f"{subtitle_input}:s:0"]

    fps = plan.frame_rate
    cmd += [
        "-c:v", "libx264",
        "-preset", plan.x264_preset,
        "-crf", str(plan.crf),
        "-profile:v", "high",
        "-pix_fmt", "yuv420p",
        "-r", str(fps),
        "-g", str(fps * 2),
        # Accurate because every clip was converted in its scale step.
        "-color_range", OUTPUT_COLOR["range"],
        "-color_primaries", OUTPUT_COLOR["primaries"],
        "-color_trc", OUTPUT_COLOR["transfer"],
        "-colorspace", OUTPUT_COLOR["space"],
    ]  # fmt: skip
    if audio:
        cmd += [
            "-c:a", "aac",
            "-b:a", f"{plan.audio_bitrate_kbps}k",
            "-ar", str(plan.preset.audio_sample_rate),
            "-ac", "2",
        ]  # fmt: skip
    else:
        cmd += ["-an"]
    if subtitle_input is not None:
        cmd += [
            "-c:s", "mov_text",
            "-metadata:s:s:0", f"language={plan.subtitles.language}",
        ]  # fmt: skip
    cmd += [
        # Never copy source metadata (GPS location, device, dates) into a
        # file that is meant to be shared.
        "-map_metadata", "-1",
        "-map_chapters", "-1",
        "-movflags", "+faststart",
        "-f", "mp4",
        "-n",
        f"file:{RENDER_FILENAME}",
    ]  # fmt: skip
    return cmd


def build_preview_command(
    ffmpeg: str, source: Path, *, height: int, has_audio: bool
) -> List[str]:
    """Low-resolution review copy of a rendered file."""
    cmd = [
        ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error",
        "-protocol_whitelist", "file",
        "-format_whitelist", OUTPUT_FORMAT_WHITELIST,
        "-i", f"file:{source}",
        "-map", "0:v:0",
        "-vf", f"scale=-2:{height},format=yuv420p",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
    ]  # fmt: skip
    if has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "64k", "-ac", "2"]
    else:
        cmd += ["-an"]
    cmd += [
        "-sn", "-map_metadata", "-1", "-movflags", "+faststart",
        "-f", "mp4", "-n", f"file:{PREVIEW_FILENAME}",
    ]  # fmt: skip
    return cmd


def build_thumbnail_command(
    ffmpeg: str, source: Path, *, at_seconds: float, height: int
) -> List[str]:
    """A single JPEG frame from a rendered file."""
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error",
        "-ss", _seconds(at_seconds),
        "-protocol_whitelist", "file",
        "-format_whitelist", OUTPUT_FORMAT_WHITELIST,
        "-i", f"file:{source}",
        "-map", "0:v:0", "-frames:v", "1",
        "-vf", f"scale=-2:{height}",
        "-c:v", "mjpeg", "-q:v", "3",
        "-map_metadata", "-1",
        "-f", "image2", "-update", "1", "-n", f"file:{THUMBNAIL_FILENAME}",
    ]  # fmt: skip


def build_decode_check_command(ffmpeg: str, source: Path) -> List[str]:
    """Decode every frame and stop at the first error (no output file)."""
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error", "-xerror",
        "-protocol_whitelist", "file",
        "-format_whitelist", OUTPUT_FORMAT_WHITELIST,
        "-i", f"file:{source}",
        "-map", "0:v", "-map", "0:a?",
        "-f", "null", "-",
    ]  # fmt: skip


# ---------------------------------------------------------------------------
# Process control
# ---------------------------------------------------------------------------


def _signal(proc: "subprocess.Popen[bytes]", force: bool) -> None:
    try:
        if hasattr(os, "killpg"):
            # FFmpeg runs in its own session, so its pid is its group id.
            os.killpg(proc.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif force:
            proc.kill()
        else:
            proc.terminate()
    except OSError:
        pass  # already gone


def _stop(proc: "subprocess.Popen[bytes]") -> None:
    """Terminate FFmpeg and anything it started, then reap it."""
    _signal(proc, force=False)
    try:
        proc.wait(timeout=_TERMINATE_GRACE_SECONDS)
        return
    except subprocess.TimeoutExpired:
        pass
    _signal(proc, force=True)
    proc.wait()


def run_process(
    cmd: List[str],
    *,
    cwd: Path,
    timeout: float,
    log_path: Path,
    env: Optional[Mapping[str, str]] = None,
) -> int:
    """Run *cmd* to completion; stderr goes to *log_path*.

    On timeout the process group is stopped and ``TimeoutExpired`` raised.
    On any other interruption (``KeyboardInterrupt``, a signal turned into an
    exception) the process is stopped before the exception propagates, so no
    orphaned FFmpeg keeps writing.
    """
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            env=dict(env) if env is not None else minimal_env(),
            start_new_session=True,
        )
        try:
            return proc.wait(timeout=timeout)
        except BaseException:
            _stop(proc)
            raise


class ProcessStopped(Exception):
    """Raised by :func:`run_process_until` after it stopped the process."""

    def __init__(self, reason: str) -> None:
        super().__init__(f"process stopped: {reason}")
        self.reason = reason


def run_process_until(
    cmd: List[str],
    *,
    cwd: Path,
    timeout: float,
    log_path: Path,
    env: Optional[Mapping[str, str]] = None,
    should_stop: Callable[[], Optional[str]],
    poll_seconds: float = 0.25,
) -> int:
    """Like :func:`run_process`, but also stops early when asked.

    Every *poll_seconds* it calls *should_stop*; a non-empty reason stops the
    process group exactly as a timeout does (SIGTERM, then SIGKILL, then
    reaped) and raises :class:`ProcessStopped`.  Used by the job worker to
    honour cancellation and shutdown; ``run_process`` itself is unchanged.
    """
    deadline = time.monotonic() + timeout
    with open(log_path, "wb") as log:
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=log,
            env=dict(env) if env is not None else minimal_env(),
            start_new_session=True,
        )
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _stop(proc)
                    raise subprocess.TimeoutExpired(cmd, timeout)
                try:
                    return proc.wait(timeout=min(poll_seconds, remaining))
                except subprocess.TimeoutExpired:
                    pass
                reason = should_stop()
                if reason:
                    _stop(proc)
                    raise ProcessStopped(reason)
        except BaseException:
            if proc.poll() is None:
                _stop(proc)
            raise


def log_tail(log_path: Path, *, redact: Mapping[str, str]) -> str:
    """Last meaningful line(s) of an FFmpeg log, with paths redacted."""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - _MAX_LOG_TAIL_BYTES))
            data = fh.read()
    except OSError:
        return ""
    text = data.decode("utf-8", errors="replace")
    # Longest first so a nested folder is replaced before its parent.
    for needle in sorted(redact, key=len, reverse=True):
        if needle:
            text = text.replace(needle, redact[needle])
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return " | ".join(lines[-3:])[-_MAX_ERROR_CHARS:]


__all__ = [
    "HDR_TRANSFERS",
    "OUTPUT_COLOR",
    "ClipInput",
    "FfmpegCapabilities",
    "FfmpegError",
    "LOG_FILENAME",
    "LogoInput",
    "PREVIEW_FILENAME",
    "ProcessRunner",
    "ProcessStopped",
    "RENDER_FILENAME",
    "SUBTITLE_FILENAME",
    "THUMBNAIL_FILENAME",
    "build_decode_check_command",
    "build_filter_graph",
    "build_preview_command",
    "build_render_command",
    "build_thumbnail_command",
    "color_conversion",
    "hdr_name",
    "log_tail",
    "logo_geometry",
    "minimal_env",
    "png_dimensions",
    "query_capabilities",
    "run_process",
    "run_process_until",
]
