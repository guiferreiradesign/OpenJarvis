"""Test doubles and generated media for the video-pipeline tests.

Nothing here touches real footage: every media file is synthesised with
FFmpeg's ``lavfi`` test sources inside pytest's ``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from jarvis_ext.tools.media_render import MediaRenderTool
from jarvis_ext.tools.media_validate import MediaValidateTool
from jarvis_ext.tools.media_workspace import MediaWorkspaceSettings
from jarvis_ext.video.settings import MediaRenderSettings

HAVE_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))

ENCODERS = b"""Encoders:
 V..... = Video
 ------
 V....D libx264              libx264 H.264
 V....D mjpeg                MJPEG
 A....D aac                  AAC
 S..... mov_text             3GPP Timed Text subtitle
"""
FILTERS = b"""Filters:
  T.. = Timeline support
 ... anullsrc          |->A       Null audio source.
 ... aformat           A->A       Convert audio format.
 ... apad              A->A       Pad audio with silence.
 ... aresample         A->A       Resample audio data.
 T.. atrim             A->A       Pick one continuous section from the input.
 ... colorchannelmixer V->V       Adjust colors by mixing color channels.
 ... concat            N->N       Concatenate audio and video streams.
 ... crop              V->V       Crop the input video.
 ... format            V->V       Convert the input video to one of the formats.
 ... fps               V->V       Force constant framerate.
 TS. overlay           VV->V      Overlay a video source on top of the input.
 ... pad               V->V       Pad the input video.
 ... scale             V->V       Scale the input video size.
 ... setsar            V->V       Set the pixel sample aspect ratio.
 T.. trim              V->V       Pick one continuous section from the input.
"""


def probe_payload(
    *,
    width: int = 1920,
    height: int = 1080,
    rate: str = "30/1",
    duration: float = 10.0,
    audio: bool = True,
    subtitles: int = 0,
    codec: str = "h264",
    pix_fmt: str = "yuv420p",
    container: str = "mov,mp4,m4a,3gp,3g2,mj2",
    video_duration: Optional[float] = None,
) -> Dict[str, Any]:
    streams: List[Dict[str, Any]] = [
        {
            "codec_type": "video",
            "codec_name": codec,
            "width": width,
            "height": height,
            "avg_frame_rate": rate,
            "pix_fmt": pix_fmt,
            "duration": str(video_duration if video_duration is not None else duration),
        }
    ]
    if audio:
        streams.append(
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "sample_rate": "48000",
                "channels": 2,
                "duration": str(duration),
            }
        )
    streams += [{"codec_type": "subtitle", "codec_name": "mov_text"}] * subtitles
    return {
        "format": {"format_name": container, "duration": str(duration)},
        "streams": streams,
    }


class FakeRunner:
    """Stands in for ``subprocess.run`` (ffprobe and capability queries).

    ``payloads`` maps a file *name* to the ffprobe JSON returned for it.
    """

    def __init__(
        self,
        payloads: Optional[Dict[str, Any]] = None,
        *,
        encoders: bytes = ENCODERS,
        filters: bytes = FILTERS,
    ) -> None:
        self.payloads = dict(payloads or {})
        self.encoders = encoders
        self.filters = filters
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, cmd, **kwargs):
        self.calls.append({"cmd": list(cmd), **kwargs})
        assert isinstance(cmd, list), "commands must be argument lists"
        assert not kwargs.get("shell"), "shell=True is never allowed"
        if "-version" in cmd:
            out = b"ffmpeg version 9.9-test Copyright\n"
        elif "-encoders" in cmd:
            out = self.encoders
        elif "-filters" in cmd:
            out = self.filters
        else:
            name = Path(cmd[-1].removeprefix("file:")).name
            if name not in self.payloads:
                return subprocess.CompletedProcess(cmd, 1, b"", b"Invalid data")
            out = json.dumps(self.payloads[name]).encode()
        return subprocess.CompletedProcess(cmd, 0, out, b"")


class FakeProcess:
    """Stands in for ``run_process`` (long FFmpeg jobs).

    By default it "renders" by writing bytes to the command's output file in
    *cwd*.  ``behaviour`` can be a return code, an exception instance to
    raise, or a callable ``(cmd, cwd) -> int``.
    """

    def __init__(self, behaviour: Any = 0, *, log: bytes = b"") -> None:
        self.behaviour = behaviour
        self.log = log
        self.calls: List[Dict[str, Any]] = []
        self.cwds: List[Path] = []

    def __call__(self, cmd, *, cwd, timeout, log_path, env):
        self.calls.append({"cmd": list(cmd), "timeout": timeout, "env": env})
        self.cwds.append(Path(cwd))
        Path(log_path).write_bytes(self.log)
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        if callable(self.behaviour):
            return self.behaviour(cmd, Path(cwd))
        target = cmd[-1]
        if self.behaviour == 0 and target.startswith("file:"):
            data = b"\xff\xd8\xff fake jpeg" if target.endswith(".jpg") else b"mp4"
            (Path(cwd) / target.removeprefix("file:")).write_bytes(data)
        return self.behaviour


def make_settings(
    output_root: Path, assets_root: Optional[Path] = None, **overrides: Any
) -> MediaRenderSettings:
    section: Dict[str, Any] = {"output_root": str(output_root)}
    if assets_root is not None:
        section["assets_root"] = str(assets_root)
    section.update(overrides)
    return MediaRenderSettings.from_mapping(section)


def make_render_tool(
    workspace: Path,
    output: Path,
    *,
    assets: Optional[Path] = None,
    runner: Optional[Callable] = None,
    process_runner: Optional[Callable] = None,
    workspace_overrides: Optional[Dict[str, Any]] = None,
    which: Optional[Callable] = None,
    **settings: Any,
) -> MediaRenderTool:
    return MediaRenderTool(
        settings=make_settings(output, assets, **settings),
        workspace_settings=MediaWorkspaceSettings.from_mapping(
            {"root": str(workspace), **(workspace_overrides or {})}
        ),
        runner=runner,
        process_runner=process_runner,
        which=which or (lambda name: f"/usr/bin/{Path(name).name}"),
    )


def make_validate_tool(
    workspace: Path,
    output: Path,
    *,
    runner: Optional[Callable] = None,
    process_runner: Optional[Callable] = None,
    which: Optional[Callable] = None,
    **settings: Any,
) -> MediaValidateTool:
    return MediaValidateTool(
        settings=make_settings(output, **settings),
        workspace_settings=MediaWorkspaceSettings.from_mapping(
            {"root": str(workspace)}
        ),
        runner=runner,
        process_runner=process_runner,
        which=which or (lambda name: f"/usr/bin/{Path(name).name}"),
    )


def digest_tree(root: Path) -> Dict[str, tuple]:
    """``{relative path: (sha256, size, mtime_ns)}`` for every file below root."""
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            info = path.stat()
            result[path.relative_to(root).as_posix()] = (
                hashlib.sha256(path.read_bytes()).hexdigest(),
                info.st_size,
                info.st_mtime_ns,
            )
    return result


def ffmpeg(*args: str) -> None:
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-v", "error", "-y", *args],
        check=True,
        capture_output=True,
        timeout=120,
    )


def make_test_media(workspace: Path, assets: Path) -> None:
    """Small synthetic clips, a logo and captions (a few hundred KB total)."""
    (workspace / "Shoot").mkdir(parents=True, exist_ok=True)
    assets.mkdir(parents=True, exist_ok=True)
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", "4", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(workspace / "Shoot" / "landscape.mp4"),
    )  # fmt: skip
    ffmpeg(
        "-f", "lavfi", "-i", "testsrc=size=360x640:rate=25",
        "-t", "3", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        str(workspace / "Shoot" / "portrait silent.mov"),
    )  # fmt: skip
    ffmpeg(
        "-f", "lavfi", "-i", "color=c=red:size=120x60,format=rgba",
        "-frames:v", "1", str(assets / "logo.png"),
    )  # fmt: skip
    (workspace / "Shoot" / "captions.srt").write_text(
        "1\n00:00:00,200 --> 00:00:01,500\nHello {\\an8}world\n\n"
        "2\n00:00:02,000 --> 00:00:03,500\n<b>Second</b> cue\n",
        encoding="utf-8",
    )


__all__ = [
    "FakeProcess",
    "FakeRunner",
    "HAVE_FFMPEG",
    "digest_tree",
    "ffmpeg",
    "make_render_tool",
    "make_settings",
    "make_test_media",
    "make_validate_tool",
    "probe_payload",
]
