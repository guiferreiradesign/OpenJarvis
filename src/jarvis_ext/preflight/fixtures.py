"""A tiny synthetic media fixture for the preflight, generated with FFmpeg.

The same recipe the test-suite uses (``tests/jarvis_ext/video_fakes.py``):
a 640x360 landscape clip with audio, a silent 360x640 portrait clip, a
translucent PNG logo and an SRT file, a few hundred KB in total.  It lives in
the preflight's private temporary folder, so no personal media is read,
changed or needed.
"""

from __future__ import annotations

import hashlib
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional

PLAN = {
    "output_name": "preflight_reel",
    "clips": [
        {"source": "Shoot/landscape.mp4", "start": 0.5, "end": 2.0},
        {"source": "Shoot/portrait silent.mov", "start": 0, "end": 1.0},
    ],
    "video": {"x264_preset": "ultrafast"},
}
EXPECTED_DURATION = 2.5  # 1.5 s + 1.0 s


class FixtureUnavailable(RuntimeError):
    """FFmpeg could not generate the fixture (missing binary or encoder)."""


@dataclass(frozen=True)
class Fixture:
    workspace: Path
    assets: Path
    output: Path


def _ffmpeg(ffmpeg: str, *args: str) -> None:
    try:
        subprocess.run(
            [ffmpeg, "-hide_banner", "-v", "error", "-y", *args],
            check=True,
            capture_output=True,
            timeout=120,
        )
    except FileNotFoundError:
        raise FixtureUnavailable("ffmpeg was not found") from None
    except subprocess.TimeoutExpired:
        raise FixtureUnavailable("ffmpeg timed out generating the fixture") from None
    except subprocess.CalledProcessError as exc:
        tail = (exc.stderr or b"").decode("utf-8", "replace").strip().splitlines()
        raise FixtureUnavailable(
            "ffmpeg could not generate the fixture"
            + (f": {tail[-1][:200]}" if tail else "")
        ) from None


def build_fixture(root: Path, ffmpeg: str = "ffmpeg") -> Fixture:
    """Create the fixture under *root* (which must be a private temp folder)."""
    workspace, assets, output = root / "workspace", root / "assets", root / "renders"
    (workspace / "Shoot").mkdir(parents=True)
    assets.mkdir()
    output.mkdir()
    _ffmpeg(
        ffmpeg,
        "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100",
        "-t", "4", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
        str(workspace / "Shoot" / "landscape.mp4"),
    )  # fmt: skip
    _ffmpeg(
        ffmpeg,
        "-f", "lavfi", "-i", "testsrc=size=360x640:rate=25",
        "-t", "3", "-c:v", "libx264", "-preset", "ultrafast",
        "-pix_fmt", "yuv420p",
        str(workspace / "Shoot" / "portrait silent.mov"),
    )  # fmt: skip
    _ffmpeg(
        ffmpeg,
        "-f", "lavfi", "-i", "color=c=red:size=120x60,format=rgba",
        "-frames:v", "1", str(assets / "logo.png"),
    )  # fmt: skip
    (workspace / "Shoot" / "captions.srt").write_text(
        "1\n00:00:00,200 --> 00:00:01,500\nHello world\n\n"
        "2\n00:00:02,000 --> 00:00:03,500\nSecond cue\n",
        encoding="utf-8",
    )
    return Fixture(workspace, assets, output)


def digest_tree(root: Path) -> Dict[str, str]:
    """SHA-256 of every file under *root*, to prove sources were not changed."""
    result: Dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    return result


def first_line(text: Optional[str]) -> str:
    return (text or "").strip().splitlines()[0] if (text or "").strip() else ""
