"""Group B: FFmpeg, media inspection and the real video pipeline.

Everything runs on a synthetic fixture generated into the preflight's private
temporary folder.  The user's media workspace is never read, and the fixture's
source files are checked for changes (SHA-256) after each stage.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Tuple

from jarvis_ext.preflight.core import (
    Blocked,
    Context,
    Outcome,
    check,
    failed,
    passed,
    warned,
)
from jarvis_ext.preflight.fixtures import (
    EXPECTED_DURATION,
    PLAN,
    FixtureUnavailable,
    build_fixture,
    digest_tree,
)


def _configured_paths(ctx: Context) -> Tuple[str, str]:
    render = ctx.shared.get("settings.media_render")
    workspace = ctx.shared.get("settings.media_workspace")
    ffmpeg = getattr(render, "ffmpeg_path", "") or "ffmpeg"
    ffprobe = (
        getattr(render, "ffprobe_path", "")
        or getattr(workspace, "ffprobe_path", "")
        or "ffprobe"
    )
    return ffmpeg, ffprobe


def _version_line(binary: str) -> str:
    proc = subprocess.run(  # noqa: S603 - fixed argument list
        [binary, "-version"], capture_output=True, timeout=15, check=False
    )
    if proc.returncode != 0:
        raise RuntimeError(f"'{Path(binary).name} -version' exited {proc.returncode}")
    return (proc.stdout.decode("utf-8", "replace").splitlines() or [""])[0]


@check(
    "media.ffmpeg", "media", "FFmpeg and ffprobe are available and capable", budget=15
)
def ffmpeg_available(ctx: Context) -> Outcome:
    from jarvis_ext.video.ffmpeg import FfmpegError, query_capabilities

    want_ffmpeg, want_ffprobe = _configured_paths(ctx)
    ffmpeg, ffprobe = shutil.which(want_ffmpeg), shutil.which(want_ffprobe)
    missing = [
        name for name, found in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)) if not found
    ]
    if missing:
        return failed(
            f"{' and '.join(missing)} not found. The video pipeline needs both "
            "('brew install ffmpeg', or set ffmpeg_path/ffprobe_path in "
            "[jarvis.media_render])."
        )
    assert ffmpeg and ffprobe
    try:
        ffmpeg_line, ffprobe_line = _version_line(ffmpeg), _version_line(ffprobe)
        caps = query_capabilities(ffmpeg)
    except (RuntimeError, OSError, subprocess.TimeoutExpired, FfmpegError) as exc:
        return failed(f"FFmpeg is present but unusable: {exc}")
    ctx.shared["ffmpeg"], ctx.shared["ffprobe"] = ffmpeg, ffprobe
    missing_enc = [e for e in ("libx264", "aac") if e not in caps.encoders]
    if missing_enc:
        return failed(
            "this FFmpeg lacks required encoder(s): " + ", ".join(missing_enc)
        )
    extras = []
    if not caps.tone_mapping:
        extras.append("cannot tone-map HDR (HDR sources are refused, not mangled)")
    return passed(
        f"{ffmpeg_line.split(' Copyright')[0]}; {ffprobe_line.split(' Copyright')[0]}"
        + ("; " + "; ".join(extras) if extras else "")
    )


@check("media.fixture", "media", "Synthetic media fixture generates", budget=20)
def make_fixture(ctx: Context) -> Outcome:
    ffmpeg = ctx.need("ffmpeg", "FFmpeg is not available (see media.ffmpeg)")
    try:
        fixture = build_fixture(ctx.workdir / "fixture", ffmpeg)
    except FixtureUnavailable as exc:
        return warned(
            f"fixture unavailable ({exc}); inspection and render checks were "
            "NOT RUN rather than assumed to work"
        )
    ctx.shared["fixture"] = fixture
    ctx.shared["fixture_digest"] = digest_tree(fixture.workspace)
    return passed("2 clips, a logo and an SRT generated in a temporary folder")


def ensure_fixture(ctx: Context):
    """The fixture, generating it first if the media checks were not selected."""
    if "fixture" in ctx.shared:
        return ctx.shared["fixture"]
    if "ffmpeg" not in ctx.shared and ffmpeg_available(ctx).status == "FAIL":
        raise Blocked("FFmpeg is not available (see media.ffmpeg)")
    outcome = make_fixture(ctx)
    if "fixture" not in ctx.shared:
        raise Blocked(outcome.detail)
    return ctx.shared["fixture"]


_fixture = ensure_fixture


def _parse(result: Any) -> Dict[str, Any]:
    try:
        data = json.loads(result.content)
    except ValueError:
        raise AssertionError(f"tool returned non-JSON: {result.content[:160]}")
    return data if isinstance(data, dict) else {}


@check("media.inspect", "media", "Read-only media inspection of the fixture", budget=10)
def inspect_fixture(ctx: Context) -> Outcome:
    from jarvis_ext.tools.media_workspace import (
        MediaWorkspaceInspectTool,
        MediaWorkspaceSettings,
    )

    fixture = _fixture(ctx)
    _, ffprobe = _configured_paths(ctx)
    tool = MediaWorkspaceInspectTool(
        settings=MediaWorkspaceSettings.from_mapping(
            {"root": str(fixture.workspace), "ffprobe_path": ctx.shared["ffprobe"]}
        ),
        runner=subprocess.run,
    )
    result = tool.execute(media_type="video")
    data = _parse(result)
    if not result.success:
        return failed(f"media_workspace_inspect failed: {str(data)[:200]}")
    files = {f["path"]: f for f in data.get("files", [])}
    expected = {
        "Shoot/landscape.mp4": (640, 360, 4.0),
        "Shoot/portrait silent.mov": (360, 640, 3.0),
    }
    problems = []
    for rel, (width, height, duration) in expected.items():
        meta = (files.get(rel) or {}).get("metadata") or {}
        video = meta.get("video") or {}
        if meta.get("status") != "ok":
            problems.append(f"{rel}: metadata status {meta.get('status')!r}")
        elif (video.get("width"), video.get("height")) != (width, height):
            problems.append(
                f"{rel}: got {video.get('width')}x{video.get('height')}, "
                f"expected {width}x{height}"
            )
        elif abs(float(meta.get("duration_seconds", -1)) - duration) > 0.3:
            problems.append(f"{rel}: duration {meta.get('duration_seconds')}")
    if problems:
        return failed("; ".join(problems))
    if digest_tree(fixture.workspace) != ctx.shared["fixture_digest"]:
        return failed("inspection changed the fixture's source files")
    return passed(
        "2 videos found with correct size, orientation and duration; sources unchanged"
    )


def _render_tools(ctx: Context):
    from jarvis_ext.tools.media_render import MediaRenderTool
    from jarvis_ext.tools.media_validate import MediaValidateTool
    from jarvis_ext.tools.media_workspace import MediaWorkspaceSettings
    from jarvis_ext.video.settings import MediaRenderSettings

    fixture = _fixture(ctx)
    workspace = MediaWorkspaceSettings.from_mapping(
        {"root": str(fixture.workspace), "ffprobe_path": ctx.shared["ffprobe"]}
    )
    settings = MediaRenderSettings.from_mapping(
        {
            "output_root": str(fixture.output),
            "assets_root": str(fixture.assets),
            "ffmpeg_path": ctx.shared["ffmpeg"],
            "ffprobe_path": ctx.shared["ffprobe"],
        }
    )
    kwargs = {"settings": settings, "workspace_settings": workspace}
    return MediaRenderTool(**kwargs), MediaValidateTool(**kwargs)


@check("media.render", "media", "Real FFmpeg render of an edit plan", budget=60)
def render_fixture(ctx: Context) -> Outcome:
    fixture = _fixture(ctx)
    render, _ = _render_tools(ctx)
    result = render.execute(plan=json.dumps(PLAN))
    data = _parse(result)
    if not result.success or data.get("render_status") != "succeeded":
        return failed(
            f"media_render reported {data.get('render_status')!r}: "
            f"{data.get('error', '')}"[:300]
        )
    out = data["output"]
    published = fixture.output / out["file"]
    if not published.is_file() or published.stat().st_size == 0:
        return failed("render reported success but the output file is missing/empty")
    if (out.get("width"), out.get("height")) != (1080, 1920):
        return failed(
            f"output is {out.get('width')}x{out.get('height')}, not 1080x1920"
        )
    if abs(float(out.get("duration_seconds", -1)) - EXPECTED_DURATION) > 0.3:
        return failed(f"output duration {out.get('duration_seconds')} s")
    if digest_tree(fixture.workspace) != ctx.shared["fixture_digest"]:
        return failed("rendering changed the fixture's source files")
    ctx.shared["rendered"] = out["file"]
    return passed(
        f"{out['file']} {out['width']}x{out['height']} {out['duration_seconds']} s; "
        "sources unchanged"
    )


@check(
    "media.validate", "media", "Render passes the project's validation rules", budget=30
)
def validate_fixture(ctx: Context) -> Outcome:
    fixture = _fixture(ctx)
    rendered = ctx.need("rendered", "no rendered output (see media.render)")
    _, validate = _render_tools(ctx)
    result = validate.execute(output=rendered)
    data = _parse(result)
    checks = data.get("checks", [])
    bad = [c for c in checks if not c.get("passed")]
    if data.get("validation_status") != "passed" or not data.get("ready_for_review"):
        names = ", ".join(c.get("check", "?") for c in bad) or data.get("error", "")
        return failed(
            f"media_validate status {data.get('validation_status')!r}; failing: {names}"
        )
    for rel in (data.get("preview"), data.get("thumbnail")):
        if not rel or not (fixture.output / rel).is_file():
            return failed("validation passed but preview/thumbnail were not created")
    if digest_tree(fixture.workspace) != ctx.shared["fixture_digest"]:
        return failed("validation changed the fixture's source files")
    return passed(
        f"{len(checks)}/{len(checks)} checks passed (dimensions, fps, codecs, "
        "colour range, audio, full decode); preview and thumbnail created"
    )
