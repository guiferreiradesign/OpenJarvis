"""``media_render`` and ``media_validate`` tools: mocked FFmpeg, then real FFmpeg."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict

import pytest

import jarvis_ext
from jarvis_ext.tools.media_render import TOOL_NAME as RENDER
from jarvis_ext.tools.media_render import MediaRenderTool
from jarvis_ext.tools.media_validate import TOOL_NAME as VALIDATE
from jarvis_ext.tools.media_validate import MediaValidateTool
from jarvis_ext.video.output import TMP_DIRNAME
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolCall
from openjarvis.tools._stubs import ToolExecutor

from .video_fakes import (
    FILTERS,
    HAVE_FFMPEG,
    OLD_SCALE_HELP,
    FakeProcess,
    FakeRunner,
    digest_tree,
    make_render_tool,
    make_test_media,
    make_validate_tool,
    probe_payload,
)
from .video_fakes import ffmpeg as run_ffmpeg

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX filesystem")
needs_ffmpeg = pytest.mark.skipif(
    not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed"
)

PNG_HEADER = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    + (400).to_bytes(4, "big")
    + (200).to_bytes(4, "big")
    + b"\x08\x06\x00\x00\x00"
)


@pytest.fixture
def dirs(tmp_path):
    workspace = tmp_path / "media"
    (workspace / "Shoot").mkdir(parents=True)
    (workspace / "Shoot" / "a.mp4").write_bytes(b"source a")
    (workspace / "Shoot" / "b.mov").write_bytes(b"source b")
    (workspace / "Shoot" / "caps.srt").write_text(
        "1\n00:00:00,500 --> 00:00:01,500\nHi\n\n"
        "2\n00:01:00,000 --> 00:01:01,000\nLate\n"
    )
    (workspace / "Shoot" / "notes.txt").write_text("not media")
    output = tmp_path / "renders"
    output.mkdir()
    assets = tmp_path / "assets"
    assets.mkdir()
    (assets / "logo.png").write_bytes(PNG_HEADER + b"\x00" * 64)
    return workspace, output, assets


def _plan(**extra) -> Dict[str, Any]:
    plan = {
        "output_name": "reel",
        "clips": [
            {"source": "Shoot/a.mp4", "start": 1, "end": 4},
            {"source": "Shoot/b.mov", "start": "00:00:00.5", "end": 2.5},
        ],
    }
    plan.update(extra)
    return plan


def _runner(**output_kw) -> FakeRunner:
    output = {"width": 1080, "height": 1920, "duration": 5.0, **output_kw}
    return FakeRunner(
        {
            "a.mp4": probe_payload(duration=10.0),
            "b.mov": probe_payload(width=1080, height=1920, duration=3.0, audio=False),
            "render.mp4": probe_payload(**output),
        }
    )


def _render(dirs, plan=None, *, runner=None, process=None, **kw):
    workspace, output, assets = dirs
    tool = make_render_tool(
        workspace,
        output,
        assets=assets,
        runner=runner or _runner(),
        process_runner=process or FakeProcess(),
        **kw,
    )
    result = tool.execute(plan=plan if plan is not None else _plan())
    return result, json.loads(result.content)


def _no_temp_left(output: Path) -> bool:
    tmp = output / TMP_DIRNAME
    return not tmp.exists() or not any(tmp.iterdir())


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


class TestRegistration:
    def test_registered_by_openjarvis_tools_import(self):
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                "import openjarvis.tools; "
                "from openjarvis.core.registry import ToolRegistry as R; "
                f"assert R.contains({RENDER!r}) and R.contains({VALIDATE!r})",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def test_zero_arg_construction_reads_config(self, dirs, tmp_path, monkeypatch):
        workspace, output, _ = dirs
        config = tmp_path / "config.toml"
        config.write_text(
            f'[jarvis.media_workspace]\nroot = "{workspace.as_posix()}"\n'
            f'[jarvis.media_render]\noutput_root = "{output.as_posix()}"\n'
            "render_timeout_seconds = 60\n"
        )
        monkeypatch.setenv("OPENJARVIS_CONFIG", str(config))
        jarvis_ext.ensure_registered()
        tool = ToolRegistry.create(RENDER)
        assert tool._settings.output_root == output.resolve()
        assert tool.spec.timeout_seconds > 60
        assert isinstance(ToolRegistry.create(VALIDATE), MediaValidateTool)

    def test_specs(self, dirs):
        workspace, output, _ = dirs
        for tool in (
            make_render_tool(workspace, output),
            make_validate_tool(workspace, output),
        ):
            fn = tool.to_openai_function()["function"]
            assert fn["parameters"]["type"] == "object"
            assert tool.spec.required_capabilities == ["file:read", "file:write"]
            assert tool.spec.requires_confirmation is False
        tool = make_render_tool(workspace, output, require_confirmation=True)
        assert tool.spec.requires_confirmation is True

    def test_executor_enforces_write_capability(self, dirs):
        from openjarvis.security.capabilities import CapabilityPolicy

        workspace, output, assets = dirs
        tool = make_render_tool(
            workspace,
            output,
            assets=assets,
            runner=_runner(),
            process_runner=FakeProcess(),
        )
        call = ToolCall(id="1", name=RENDER, arguments=json.dumps({"plan": _plan()}))
        policy = CapabilityPolicy(default_deny=True)
        policy.grant("jarvis", "file:read")
        denied = ToolExecutor([tool], capability_policy=policy, agent_id="jarvis")
        result = denied.execute(call)
        assert not result.success and "file:write" in result.content
        assert not list(output.glob("*.mp4"))

        policy.grant("jarvis", "file:write")
        allowed = ToolExecutor([tool], capability_policy=policy, agent_id="jarvis")
        result = allowed.execute(call)
        assert result.success, result.content

    def test_confirmation_is_enforced_by_executor(self, dirs):
        workspace, output, _ = dirs
        tool = make_render_tool(
            workspace, output, runner=_runner(), process_runner=FakeProcess(),
            require_confirmation=True,
        )  # fmt: skip
        call = ToolCall(id="1", name=RENDER, arguments=json.dumps({"plan": _plan()}))
        result = ToolExecutor([tool]).execute(call)
        assert not result.success and "confirmation" in result.content
        result = ToolExecutor(
            [tool], interactive=True, confirm_callback=lambda prompt: False
        ).execute(call)
        assert not result.success and "denied" in result.content
        assert not list(output.glob("*.mp4"))


# ---------------------------------------------------------------------------
# Mocked rendering
# ---------------------------------------------------------------------------


class TestRenderMocked:
    def test_success(self, dirs):
        workspace, output, _ = dirs
        before = digest_tree(workspace)
        process = FakeProcess()
        result, report = _render(dirs, process=process)
        assert result.success, report
        assert report["render_status"] == "succeeded"
        assert report["ready_for_review"] is False
        assert report["validation_status"] == "not_validated"
        assert report["output"]["file"] == "reel_v001.mp4"
        assert report["record"] == "reel_v001.render.json"
        assert (output / "reel_v001.mp4").read_bytes() == b"mp4"
        record = json.loads((output / "reel_v001.render.json").read_text())
        assert record["output"]["sha256"] == report["output"]["sha256"]
        assert record["plan"]["clips"][1]["start"] == 0.5
        assert "b.mov has no audio" in " ".join(report["warnings"])
        assert str(workspace) not in result.content
        assert str(output) not in result.content
        assert _no_temp_left(output)
        assert digest_tree(workspace) == before
        # FFmpeg ran in the private temp dir with an argument list.
        assert process.cwds[0].parent == output / TMP_DIRNAME
        assert f"file:{workspace / 'Shoot' / 'a.mp4'}" in process.calls[0]["cmd"]
        assert "PATH" in process.calls[0]["env"]
        assert set(process.calls[0]["env"]) <= {"PATH", "LC_ALL", "HOME"}

    def test_versions_increase_and_nothing_is_overwritten(self, dirs):
        _, output, _ = dirs
        (output / "reel_v001.mp4").write_bytes(b"user's earlier export")
        _, first = _render(dirs)
        _, second = _render(dirs)
        assert first["output"]["file"] == "reel_v002.mp4"
        assert second["output"]["file"] == "reel_v003.mp4"
        assert (output / "reel_v001.mp4").read_bytes() == b"user's earlier export"

    def test_plan_may_be_json_string(self, dirs):
        result, report = _render(dirs, json.dumps(_plan()))
        assert result.success, report

    @pytest.mark.parametrize(
        "plan, status, message",
        [
            ("{not json", "invalid_plan", "not valid JSON"),
            ({"output_name": "reel"}, "invalid_plan", "clips"),
            (
                {"output_name": "reel", "clips": [{"source": "Shoot/a.mp4",
                 "start": 2, "end": 1}]},
                "invalid_plan", "after start",
            ),
            (
                {"output_name": "reel", "clips": [{"source": "Shoot/a.mp4",
                 "start": 0, "end": 11}]},
                "invalid_plan", "beyond the end",
            ),
            (
                {"output_name": "reel", "clips": [{"source": "Shoot/missing.mp4",
                 "start": 0, "end": 1}]},
                "invalid_plan", "not found",
            ),
            (
                {"output_name": "reel", "clips": [{"source": "Shoot/notes.txt",
                 "start": 0, "end": 1}]},
                "invalid_plan", "not a video",
            ),
            (
                {"output_name": "reel", "clips": [{"source": "Shoot",
                 "start": 0, "end": 1}]},
                "invalid_plan", "regular file",
            ),
        ],
    )  # fmt: skip
    def test_invalid_plans(self, dirs, plan, status, message):
        result, report = _render(dirs, plan)
        assert not result.success
        assert report["render_status"] == status
        assert message in report["error"]
        assert not list(dirs[1].glob("*.mp4"))

    def test_missing_plan(self, dirs):
        workspace, output, _ = dirs
        result = make_render_tool(workspace, output).execute()
        assert json.loads(result.content)["render_status"] == "invalid_plan"

    @pytest.mark.parametrize(
        "source",
        [
            "../outside.mp4",
            "Shoot/../../outside.mp4",
            "/etc/hosts",
            "~/outside.mp4",
            "C:\\outside.mp4",
            "Shoot/a.mp4\x00.txt",
        ],
    )
    def test_source_traversal_denied(self, dirs, source):
        (dirs[0].parent / "outside.mp4").write_bytes(b"x")
        plan = {
            "output_name": "reel",
            "clips": [{"source": source, "start": 0, "end": 1}],
        }
        result, report = _render(dirs, plan)
        assert not result.success
        assert report["render_status"] in ("access_denied", "invalid_plan")
        assert "Access denied" in report["error"] or "control" in report["error"]

    @posix_only
    def test_symlink_escape_denied(self, dirs, tmp_path):
        workspace = dirs[0]
        outside = tmp_path / "private.mp4"
        outside.write_bytes(b"private")
        (workspace / "Shoot" / "link.mp4").symlink_to(outside)
        (workspace / "linkdir").symlink_to(tmp_path)
        for source in ("Shoot/link.mp4", "linkdir/private.mp4"):
            plan = {
                "output_name": "r",
                "clips": [{"source": source, "start": 0, "end": 1}],
            }
            for follow in (False, True):
                result, report = _render(
                    dirs, plan, workspace_overrides={"follow_symlinks": follow}
                )
                assert report["render_status"] == "access_denied", (source, follow)

    @posix_only
    def test_symlink_inside_workspace_allowed_only_when_enabled(self, dirs):
        workspace = dirs[0]
        (workspace / "Shoot" / "alias.mp4").symlink_to(workspace / "Shoot" / "a.mp4")
        plan = {
            "output_name": "r",
            "clips": [{"source": "Shoot/alias.mp4", "start": 0, "end": 1}],
        }
        _, report = _render(dirs, plan)
        assert report["render_status"] == "access_denied"
        result, report = _render(
            dirs,
            plan,
            runner=_runner(duration=1.0),
            workspace_overrides={"follow_symlinks": True},
        )
        assert result.success, report

    def test_hidden_sources_refused(self, dirs):
        (dirs[0] / ".private").mkdir()
        (dirs[0] / ".private" / "a.mp4").write_bytes(b"x")
        plan = {
            "output_name": "r",
            "clips": [{"source": ".private/a.mp4", "start": 0, "end": 1}],
        }
        _, report = _render(dirs, plan)
        assert report["render_status"] == "access_denied"

    def test_unreadable_media(self, dirs):
        runner = _runner()
        del runner.payloads["a.mp4"]
        result, report = _render(dirs, runner=runner)
        assert report["render_status"] == "invalid_media"
        assert str(dirs[0]) not in result.content

    def test_audio_only_source(self, dirs):
        runner = _runner()
        runner.payloads["a.mp4"] = {
            "format": {"duration": "5"},
            "streams": [{"codec_type": "audio", "codec_name": "aac"}],
        }
        _, report = _render(dirs, runner=runner)
        assert report["render_status"] == "invalid_media"
        assert "no video" in report["error"]

    def test_hlg_source_is_tone_mapped(self, dirs):
        runner = _runner()
        runner.payloads["a.mp4"] = probe_payload(
            duration=10,
            pix_fmt="yuv420p10le",
            color_transfer="arib-std-b67",
            color_primaries="bt2020",
            color_space="bt2020nc",
        )
        process = FakeProcess()
        result, report = _render(dirs, runner=runner, process=process)
        assert result.success, report
        assert any("HLG HDR source tone-mapped" in w for w in report["warnings"])
        graph = process.calls[0]["cmd"][
            process.calls[0]["cmd"].index("-filter_complex") + 1
        ]
        scales = [p.split(",")[0] for p in graph.split(";") if ":v:0]scale=" in p]
        assert ["intent=perceptual" in s for s in scales] == [True, False]

    def test_hdr_refused_without_tone_mapping(self, dirs):
        """Never relabel an HDR signal as Rec.709 without converting it."""
        runner = _runner()
        runner.scale_help = OLD_SCALE_HELP
        runner.payloads["a.mp4"] = probe_payload(
            duration=10, color_transfer="arib-std-b67", color_primaries="bt2020"
        )
        result, report = _render(dirs, runner=runner)
        assert not result.success
        assert report["render_status"] == "unsupported"
        assert "HLG HDR" in report["error"] and "tone-map" in report["error"]
        assert not list(dirs[1].glob("*.mp4"))

    def test_sdr_sources_render_with_old_ffmpeg(self, dirs):
        runner = _runner()
        runner.scale_help = OLD_SCALE_HELP
        result, report = _render(dirs, runner=runner)
        assert result.success, report

    def test_full_range_output_is_not_published(self, dirs):
        result, report = _render(dirs, runner=_runner(color_range="pc"))
        assert report["render_status"] == "failed_checks"
        assert "color_range" in report["error"]
        assert not list(dirs[1].glob("*.mp4"))

    # -- configuration and folders ------------------------------------------

    def test_unconfigured(self, dirs, tmp_path):
        from jarvis_ext.tools.media_workspace import MediaWorkspaceSettings
        from jarvis_ext.video.settings import MediaRenderSettings

        tool = MediaRenderTool(
            settings=MediaRenderSettings(),
            workspace_settings=MediaWorkspaceSettings.from_mapping(
                {"root": str(dirs[0])}
            ),
        )
        report = json.loads(tool.execute(plan=_plan()).content)
        assert report["render_status"] == "not_configured"
        assert "output_root" in report["error"]

    def test_config_error_reported_without_path(self, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text('[jarvis.media_render]\noutput_root = "relative"\n')
        result = MediaRenderTool(config_path=config).execute(plan=_plan())
        report = json.loads(result.content)
        assert report["render_status"] == "config_error"
        assert str(tmp_path) not in result.content

    @pytest.mark.parametrize("where", ["inside", "same", "parent"])
    def test_output_overlapping_workspace_refused(self, dirs, where):
        workspace, output, assets = dirs
        target = {
            "inside": workspace / "Exports",
            "same": workspace,
            "parent": workspace.parent,
        }[where]
        target.mkdir(exist_ok=True)
        before = digest_tree(workspace)
        tool = make_render_tool(
            workspace, target, runner=_runner(), process_runner=FakeProcess()
        )
        report = json.loads(tool.execute(plan=_plan()).content)
        assert report["render_status"] == "config_error"
        assert "outside the media workspace" in report["error"]
        assert digest_tree(workspace) == before
        assert not (target / TMP_DIRNAME).exists()

    def test_output_folder_missing(self, dirs, tmp_path):
        workspace = dirs[0]
        tool = make_render_tool(
            workspace,
            tmp_path / "unplugged-drive",
            runner=_runner(),
            process_runner=FakeProcess(),
        )
        report = json.loads(tool.execute(plan=_plan()).content)
        assert report["render_status"] == "output_unavailable"
        assert not (tmp_path / "unplugged-drive").exists()

    def test_ffmpeg_missing(self, dirs):
        _, report = _render(dirs, which=lambda name: None)
        assert report["render_status"] == "ffmpeg_missing"
        assert "brew install ffmpeg" in report["error"]

    def test_burn_in_unavailable(self, dirs):
        plan = _plan(subtitles={"source": "Shoot/caps.srt", "mode": "burn"})
        _, report = _render(dirs, plan)
        assert report["render_status"] == "unsupported"
        assert "libass" in report["error"]

    def test_burn_in_when_available(self, dirs):
        runner = _runner()
        runner.filters = FILTERS + b" ... subtitles  V->V  Render text subtitles.\n"
        process = FakeProcess()
        plan = _plan(subtitles={"source": "Shoot/caps.srt", "mode": "burn"})
        result, report = _render(dirs, plan, runner=runner, process=process)
        assert result.success, report
        graph = process.calls[0]["cmd"][
            process.calls[0]["cmd"].index("-filter_complex") + 1
        ]
        assert "subtitles=filename=subs.srt" in graph

    # -- subtitles and branding ---------------------------------------------

    def test_subtitles_are_sanitised_copies(self, dirs):
        seen = {}

        def render(cmd, cwd):
            seen["srt"] = (cwd / "subs.srt").read_text()
            (cwd / "render.mp4").write_bytes(b"mp4")
            return 0

        runner = _runner(subtitles=1)
        plan = _plan(subtitles={"source": "Shoot/caps.srt"})
        result, report = _render(dirs, plan, runner=runner, process=FakeProcess(render))
        assert result.success, report
        assert seen["srt"] == "1\n00:00:00,500 --> 00:00:01,500\nHi\n"
        assert any("dropped" in w for w in report["warnings"])

    @pytest.mark.parametrize(
        "content, message",
        [(b"garbage", "timing"), (b"\xff\xfe", "UTF-8")],
    )
    def test_bad_subtitles(self, dirs, content, message):
        (dirs[0] / "Shoot" / "bad.srt").write_bytes(content)
        _, report = _render(dirs, _plan(subtitles={"source": "Shoot/bad.srt"}))
        assert report["render_status"] == "invalid_plan"
        assert message in report["error"]

    def test_subtitles_outside_workspace(self, dirs):
        _, report = _render(dirs, _plan(subtitles={"source": "../x.srt"}))
        assert report["render_status"] == "access_denied"

    def test_branding(self, dirs):
        process = FakeProcess()
        plan = _plan(branding={"asset": "logo.png", "position": "bottom-right"})
        result, report = _render(dirs, plan, process=process)
        assert result.success, report
        cmd = process.calls[0]["cmd"]
        assert f"file:{dirs[2] / 'logo.png'}" in cmd

    @pytest.mark.parametrize("asset", ["../media/Shoot/a.png", "/etc/x.png", "~/x.png"])
    def test_branding_traversal_denied(self, dirs, asset):
        _, report = _render(dirs, _plan(branding={"asset": asset}))
        assert report["render_status"] == "access_denied"

    @posix_only
    def test_branding_symlink_denied(self, dirs, tmp_path):
        (tmp_path / "elsewhere.png").write_bytes(PNG_HEADER)
        (dirs[2] / "link.png").symlink_to(tmp_path / "elsewhere.png")
        _, report = _render(dirs, _plan(branding={"asset": "link.png"}))
        assert report["render_status"] == "access_denied"

    def test_branding_not_png(self, dirs):
        (dirs[2] / "fake.png").write_bytes(b"GIF89a" + b"\x00" * 40)
        _, report = _render(dirs, _plan(branding={"asset": "fake.png"}))
        assert report["render_status"] == "invalid_plan"
        assert "not a PNG" in report["error"]

    def test_branding_needs_assets_root(self, dirs):
        workspace, output, _ = dirs
        tool = make_render_tool(
            workspace, output, runner=_runner(), process_runner=FakeProcess()
        )
        report = json.loads(
            tool.execute(plan=_plan(branding={"asset": "logo.png"})).content
        )
        assert report["render_status"] == "not_configured"

    # -- FFmpeg failures ----------------------------------------------------

    def test_ffmpeg_failure(self, dirs):
        workspace, output, _ = dirs
        log = f"{workspace}/Shoot/a.mp4: Invalid data\nConversion failed!\n".encode()
        result, report = _render(dirs, process=FakeProcess(1, log=log))
        assert report["render_status"] == "ffmpeg_failed"
        assert "Conversion failed" in report["error"]
        assert str(workspace) not in result.content
        assert not list(output.glob("*.mp4")) and _no_temp_left(output)

    def test_timeout(self, dirs):
        exc = subprocess.TimeoutExpired(["ffmpeg"], 10)
        _, report = _render(dirs, process=FakeProcess(exc))
        assert report["render_status"] == "timeout"
        assert not list(dirs[1].glob("*.mp4")) and _no_temp_left(dirs[1])

    def test_ffmpeg_cannot_start(self, dirs):
        _, report = _render(dirs, process=FakeProcess(PermissionError("nope")))
        assert report["render_status"] == "ffmpeg_failed"

    def test_interrupted_render_cleans_up(self, dirs):
        def interrupted(cmd, cwd):
            (cwd / "render.mp4").write_bytes(b"half a vid")
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            _render(dirs, process=FakeProcess(interrupted))
        assert not list(dirs[1].glob("*.mp4")) and _no_temp_left(dirs[1])

    def test_success_without_file(self, dirs):
        _, report = _render(dirs, process=FakeProcess(lambda cmd, cwd: 0))
        assert report["render_status"] == "ffmpeg_failed"
        assert "wrote no file" in report["error"]

    @pytest.mark.parametrize(
        "output_kw, failed",
        [
            ({"width": 1920, "height": 1080}, "dimensions"),
            ({"duration": 9.0}, "duration"),
            ({"audio": False}, "audio_stream"),
            ({"video_duration": 1.3}, "video_duration"),
        ],
    )
    def test_bad_output_is_not_published(self, dirs, output_kw, failed):
        result, report = _render(dirs, runner=_runner(**output_kw))
        assert not result.success
        assert report["render_status"] == "failed_checks"
        assert failed in report["error"]
        assert not list(dirs[1].glob("*.mp4")) and _no_temp_left(dirs[1])

    def test_unprobeable_output_is_not_published(self, dirs):
        runner = _runner()
        del runner.payloads["render.mp4"]
        _, report = _render(dirs, runner=runner)
        assert report["render_status"] == "failed_checks"
        assert not list(dirs[1].glob("*.mp4"))


# ---------------------------------------------------------------------------
# Mocked validation
# ---------------------------------------------------------------------------


def _validate(dirs, output="reel_v001.mp4", *, runner=None, process=None, **params):
    workspace, out, _ = dirs
    preview = probe_payload(width=360, height=640, duration=5.0)
    runner = runner or _runner()
    runner.payloads.setdefault(
        "reel_v001.mp4", probe_payload(width=1080, height=1920, duration=5.0)
    )
    runner.payloads.setdefault("preview.mp4", preview)
    tool = make_validate_tool(
        workspace, out, runner=runner, process_runner=process or FakeProcess()
    )
    result = tool.execute(output=output, **params)
    return result, json.loads(result.content)  # fmt: skip


class TestValidateMocked:
    def test_passes_and_creates_review_files(self, dirs):
        _render(dirs)
        result, report = _validate(dirs)
        assert result.success, report
        assert report["validation_status"] == "passed"
        assert report["ready_for_review"] is True
        assert report["preview"] == "review/reel_v001_preview_v001.mp4"
        assert report["thumbnail"] == "review/reel_v001_thumb_v001.jpg"
        assert (dirs[1] / report["preview"]).exists()
        assert "decodes_cleanly" in [c["check"] for c in report["checks"]]
        assert _no_temp_left(dirs[1])

        # Validating again never overwrites earlier review files.
        _, again = _validate(dirs)
        assert again["preview"] == "review/reel_v001_preview_v002.mp4"

    def test_tampered_file_fails(self, dirs):
        _render(dirs)
        (dirs[1] / "reel_v001.mp4").write_bytes(b"mp5")
        result, report = _validate(dirs)
        assert not result.success
        assert report["ready_for_review"] is False
        failed = [c for c in report["checks"] if not c["passed"]]
        assert failed[0]["check"] == "matches_render_record"
        assert report["preview"] is None
        assert not (dirs[1] / "review").exists()

    def test_truncated_file_fails(self, dirs):
        _render(dirs)
        (dirs[1] / "reel_v001.mp4").write_bytes(b"mp")
        _, report = _validate(dirs)
        assert report["validation_status"] == "failed"

    def test_wrong_metadata_fails(self, dirs):
        _render(dirs)
        runner = _runner()
        runner.payloads["reel_v001.mp4"] = probe_payload(width=720, height=1280)
        _, report = _validate(dirs, runner=runner)
        assert report["ready_for_review"] is False
        assert "Full decode skipped" in report["warnings"][0]

    def test_decode_failure_fails(self, dirs):
        _render(dirs)

        def decode_fails(cmd, cwd):
            return 1 if cmd[-1] == "-" else 0

        result, report = _validate(dirs, process=FakeProcess(decode_fails))
        assert not result.success
        assert report["validation_status"] == "failed"
        assert report["preview"] is None

    def test_preview_failure_is_not_ready(self, dirs):
        _render(dirs)

        def preview_fails(cmd, cwd):
            if cmd[-1] == "file:preview.mp4":
                return 1
            if cmd[-1].startswith("file:"):
                (cwd / cmd[-1][5:]).write_bytes(b"\xff\xd8\xff jpeg")
            return 0

        result, report = _validate(dirs, process=FakeProcess(preview_fails))
        assert not result.success
        assert report["validation_status"] == "passed_without_review_files"
        assert report["ready_for_review"] is False
        assert report["thumbnail"] is not None and report["preview"] is None

    def test_preview_can_be_skipped(self, dirs):
        _render(dirs)
        process = FakeProcess()
        result, report = _validate(dirs, process=process, preview=False)
        assert result.success and report["ready_for_review"] is True
        assert report["preview"] is None and len(process.calls) == 1

    @pytest.mark.parametrize(
        "output",
        [
            "../reel_v001.mp4",
            "/abs/reel_v001.mp4",
            "review/reel_v001.mp4",
            "reel.mp4",
            "reel_v001.render.json",
            "",
            5,
        ],
    )
    def test_invalid_output_names(self, dirs, output):
        _, report = _validate(dirs, output=output)
        assert report["validation_status"] == "invalid_arguments"

    def test_not_found_and_no_record(self, dirs):
        _, report = _validate(dirs, output="reel_v009.mp4")
        assert report["validation_status"] == "not_found"
        (dirs[1] / "manual_v001.mp4").write_bytes(b"made by hand")
        _, report = _validate(dirs, output="manual_v001.mp4")
        assert report["validation_status"] == "no_record"

    def test_malformed_record(self, dirs):
        (dirs[1] / "x_v001.mp4").write_bytes(b"x")
        (dirs[1] / "x_v001.render.json").write_text('{"plan": {"video": 1}}')
        _, report = _validate(dirs, output="x_v001.mp4")
        assert report["validation_status"] == "no_record"

    @posix_only
    def test_symlinked_output_refused(self, dirs, tmp_path):
        (tmp_path / "elsewhere.mp4").write_bytes(b"x")
        (dirs[1] / "evil_v001.mp4").symlink_to(tmp_path / "elsewhere.mp4")
        _, report = _validate(dirs, output="evil_v001.mp4")
        assert report["validation_status"] == "invalid_arguments"


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


class TestCommandLine:
    def test_render_errors_exit_nonzero(self, tmp_path, capsys):
        from jarvis_ext.__main__ import main

        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps(_plan()))
        assert main(["media-render", str(plan), "--config", str(tmp_path / "x")]) == 1
        assert "not_configured" in capsys.readouterr().err
        assert main(["media-render", str(tmp_path / "missing.json")]) == 1

    def test_validate_errors_exit_nonzero(self, tmp_path, capsys):
        from jarvis_ext.__main__ import main

        code = main(
            ["media-validate", "reel_v001.mp4", "--config", str(tmp_path / "x")]
        )
        assert code == 1
        assert "not_configured" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Real FFmpeg
# ---------------------------------------------------------------------------


@needs_ffmpeg
class TestRealFfmpeg:
    @pytest.fixture
    def real(self, tmp_path):
        workspace = tmp_path / "workspace"
        output = tmp_path / "renders"
        assets = tmp_path / "assets"
        output.mkdir()
        make_test_media(workspace, assets)
        return workspace, output, assets

    def _tools(self, real, **settings):
        workspace, output, assets = real
        render = make_render_tool(
            workspace,
            output,
            assets=assets,
            runner=subprocess.run,
            which=None,
            **settings,
        )
        render._which = shutil.which
        validate = make_validate_tool(workspace, output, runner=subprocess.run)
        validate._which = shutil.which
        return render, validate

    def _probe(self, path: Path) -> dict:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, check=True,
        ).stdout  # fmt: skip
        return json.loads(out)

    def test_render_and_validate_full_plan(self, real):
        workspace, output, _ = real
        before = digest_tree(workspace)
        render, validate = self._tools(real)
        plan = {
            "output_name": "test_reel",
            "clips": [
                {"source": "Shoot/landscape.mp4", "start": 0.5, "end": "00:00:02.5"},
                {"source": "Shoot/portrait silent.mov", "start": 1, "end": 2.5},
            ],
            "video": {"x264_preset": "ultrafast"},
            "subtitles": {"source": "Shoot/captions.srt", "language": "eng"},
            "branding": {"asset": "logo.png", "position": "top-left", "opacity": 0.7},
        }
        result = render.execute(plan=plan)
        report = json.loads(result.content)
        assert result.success, report
        assert report["output"]["file"] == "test_reel_v001.mp4"
        assert report["ready_for_review"] is False

        probe = self._probe(output / "test_reel_v001.mp4")
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        audio = next(s for s in probe["streams"] if s["codec_type"] == "audio")
        subs = [s for s in probe["streams"] if s["codec_type"] == "subtitle"]
        assert (video["width"], video["height"]) == (1080, 1920)
        assert video["codec_name"] == "h264" and video["pix_fmt"] == "yuv420p"
        assert video["avg_frame_rate"] == "30/1"
        assert audio["codec_name"] == "aac" and audio["sample_rate"] == "48000"
        assert subs and subs[0]["codec_name"] == "mov_text"
        assert subs[0]["tags"]["language"] == "eng"
        assert float(probe["format"]["duration"]) == pytest.approx(3.5, abs=0.1)
        # No source metadata copied into the shareable file.
        assert "location" not in json.dumps(probe["format"].get("tags", {}))

        result = validate.execute(output="test_reel_v001.mp4")
        checked = json.loads(result.content)
        assert result.success, checked
        assert checked["ready_for_review"] is True
        preview = self._probe(output / checked["preview"])
        pv = next(s for s in preview["streams"] if s["codec_type"] == "video")
        assert (pv["width"], pv["height"]) == (360, 640)
        thumb = output / checked["thumbnail"]
        assert thumb.read_bytes()[:3] == b"\xff\xd8\xff"

        assert digest_tree(workspace) == before
        assert _no_temp_left(output)

    def test_pad_without_audio_and_versioning(self, real):
        _, output, _ = real
        render, validate = self._tools(real)
        plan = {
            "output_name": "padded",
            "fit": "pad",
            "audio": {"mode": "none"},
            "video": {"frame_rate": 25, "x264_preset": "ultrafast"},
            "clips": [{"source": "Shoot/landscape.mp4", "start": 0, "end": 1}],
        }
        first = json.loads(render.execute(plan=plan).content)
        second = json.loads(render.execute(plan=plan).content)
        assert (first["output"]["file"], second["output"]["file"]) == (
            "padded_v001.mp4",
            "padded_v002.mp4",
        )
        assert first["output"]["sha256"] != "" and first["output"]["has_audio"] is False
        probe = self._probe(output / "padded_v002.mp4")
        assert [s["codec_type"] for s in probe["streams"]] == ["video"]
        assert probe["streams"][0]["avg_frame_rate"] == "25/1"
        result = validate.execute(output="padded_v002.mp4")
        assert result.success, result.content

    def test_concurrent_renders_get_distinct_versions(self, real):
        _, output, _ = real
        plan = {
            "output_name": "parallel",
            "video": {"x264_preset": "ultrafast"},
            "clips": [{"source": "Shoot/landscape.mp4", "start": 0, "end": 0.5}],
        }
        results = []

        def go():
            render, _ = self._tools(real)
            results.append(json.loads(render.execute(plan=plan).content))

        threads = [threading.Thread(target=go) for _ in range(3)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        names = sorted(r["output"]["file"] for r in results)
        assert names == [f"parallel_v00{i}.mp4" for i in (1, 2, 3)]
        assert _no_temp_left(output)

    def test_real_timeout_is_cleaned_up(self, real):
        from jarvis_ext.video.settings import MediaRenderSettings

        workspace, output, _ = real
        render, _ = self._tools(real)
        render._settings = MediaRenderSettings(
            output_root=output.resolve(), render_timeout_seconds=0.05
        )
        plan = {
            "output_name": "slow",
            "clips": [{"source": "Shoot/landscape.mp4", "start": 0, "end": 4}],
        }
        report = json.loads(render.execute(plan=plan).content)
        assert report["render_status"] == "timeout"
        assert not list(output.glob("*.mp4")) and _no_temp_left(output)

    def test_real_decode_failure_detected(self, real):
        """A file corrupted after rendering fails validation."""
        _, output, _ = real
        render, validate = self._tools(real)
        plan = {
            "output_name": "corrupt",
            "video": {"x264_preset": "ultrafast"},
            "clips": [{"source": "Shoot/landscape.mp4", "start": 0, "end": 2}],
        }
        assert render.execute(plan=plan).success
        target = output / "corrupt_v001.mp4"
        data = bytearray(target.read_bytes())
        data[len(data) // 2 : len(data) // 2 + 4096] = os.urandom(4096)
        target.write_bytes(bytes(data))
        result = validate.execute(output="corrupt_v001.mp4")
        report = json.loads(result.content)
        assert not result.success and report["ready_for_review"] is False
        assert report["checks"][0]["check"] == "matches_render_record"
        assert report["checks"][0]["passed"] is False
        assert report["preview"] is None

        # Even if the record were updated to match (corruption that happened
        # during the render itself), the full decode pass catches it.
        record_path = output / "corrupt_v001.render.json"
        record = json.loads(record_path.read_text())
        record["output"]["sha256"] = hashlib.sha256(bytes(data)).hexdigest()
        record_path.write_text(json.dumps(record))
        result = validate.execute(output="corrupt_v001.mp4")
        report = json.loads(result.content)
        failed = [c["check"] for c in report["checks"] if not c["passed"]]
        assert not result.success and report["ready_for_review"] is False
        assert failed == ["decodes_cleanly"], report["checks"]
        assert report["preview"] is None

    def _signal_stats(self, path: Path, at: float) -> Dict[str, float]:
        out = subprocess.run(
            ["ffmpeg", "-v", "error", "-ss", str(at), "-i", str(path),
             "-frames:v", "1", "-vf", "signalstats,metadata=print:file=-",
             "-f", "null", "-"],
            capture_output=True, text=True, check=True,
        ).stdout  # fmt: skip
        stats = {}
        for key in ("YMIN", "YMAX", "YAVG"):
            stats[key] = float(out.split(f"lavfi.signalstats.{key}=")[1].split()[0])
        return stats

    @pytest.mark.parametrize("fit", ["crop", "pad"])
    def test_full_range_source_becomes_limited_range(self, real, fit):
        """Regression for real iPhone footage: yuvj420p in, yuv420p/tv out,
        with pixel values actually rescaled (black 0->16, white 255->235)."""
        workspace, output, _ = real
        for name, colour in (("black", "black"), ("white", "white")):
            run_ffmpeg(
                "-f", "lavfi", "-i", f"color=c={colour}:size=640x360:rate=30",
                "-t", "1", "-c:v", "libx264", "-preset", "ultrafast",
                "-pix_fmt", "yuvj420p", "-color_range", "pc",
                str(workspace / "Shoot" / f"full_{name}.mp4"),
            )  # fmt: skip
            assert (
                self._probe(workspace / "Shoot" / f"full_{name}.mp4")["streams"][0][
                    "color_range"
                ]
                == "pc"
            )
        render, validate = self._tools(real)
        plan = {
            "output_name": f"range_{fit}",
            "fit": fit,
            "audio": {"mode": "none"},
            "video": {"x264_preset": "ultrafast"},
            "clips": [
                {"source": "Shoot/full_black.mp4", "start": 0, "end": 1},
                {"source": "Shoot/full_white.mp4", "start": 0, "end": 1},
            ],
        }
        result = render.execute(plan=plan)
        assert result.success, result.content
        target = output / f"range_{fit}_v001.mp4"
        stream = self._probe(target)["streams"][0]
        assert stream["pix_fmt"] == "yuv420p"
        assert stream["color_range"] == "tv"
        assert stream["color_primaries"] == "bt709"
        assert stream["color_transfer"] == "bt709"
        assert stream["color_space"] == "bt709"
        black = self._signal_stats(target, 0.5)
        white = self._signal_stats(target, 1.5)
        assert black["YAVG"] == pytest.approx(16, abs=2)
        if fit == "crop":  # pad adds black bars to the white frame
            assert white["YAVG"] == pytest.approx(235, abs=2)
        assert white["YMAX"] <= 236
        checked = json.loads(validate.execute(output=target.name).content)
        assert checked["ready_for_review"] is True, checked["checks"]

    def test_hlg_source_is_tone_mapped_not_relabelled(self, real, tmp_path):
        """An HLG/BT.2020 10-bit clip renders as genuinely converted Rec.709."""
        workspace, output, _ = real
        source = workspace / "Shoot" / "landscape.mp4"
        hlg = workspace / "Shoot" / "hlg.mkv"
        run_ffmpeg(
            "-i", str(source), "-t", "1", "-an",
            # setparams tags the frames; FFmpeg 9 lets frame properties
            # override -color_trc/-color_primaries output options.
            "-vf", "scale=out_color_matrix=bt2020:out_range=tv,"
            "format=yuv420p10le,setparams=color_primaries=bt2020:"
            "color_trc=arib-std-b67:colorspace=bt2020nc:range=tv",
            "-c:v", "ffv1",
            str(hlg),
        )  # fmt: skip
        stream = self._probe(hlg)["streams"][0]
        assert stream["color_transfer"] == "arib-std-b67"
        render, validate = self._tools(real)
        caps = render._ffmpeg_capabilities(shutil.which("ffmpeg"))
        if not caps.tone_mapping:
            pytest.skip("this FFmpeg cannot tone-map; refusal is tested with mocks")
        result = render.execute(
            plan={
                "output_name": "hlg",
                "audio": {"mode": "none"},
                "video": {"x264_preset": "ultrafast"},
                "clips": [{"source": "Shoot/hlg.mkv", "start": 0, "end": 1}],
            }
        )
        report = json.loads(result.content)
        assert result.success, report
        assert any("HLG HDR source tone-mapped" in w for w in report["warnings"])
        stream = self._probe(output / "hlg_v001.mp4")["streams"][0]
        assert (stream["color_transfer"], stream["color_primaries"]) == (
            "bt709",
            "bt709",
        )
        # Same frame rendered as plain SDR: tone mapping must change pixels.
        sdr = json.loads(
            render.execute(
                plan={
                    "output_name": "sdr",
                    "audio": {"mode": "none"},
                    "video": {"x264_preset": "ultrafast"},
                    "clips": [{"source": "Shoot/landscape.mp4", "start": 0, "end": 1}],
                }
            ).content
        )
        assert sdr["render_status"] == "succeeded"
        hdr_stats = self._signal_stats(output / "hlg_v001.mp4", 0.5)
        sdr_stats = self._signal_stats(output / "sdr_v001.mp4", 0.5)
        assert abs(hdr_stats["YAVG"] - sdr_stats["YAVG"]) > 3
        assert validate.execute(output="hlg_v001.mp4").success

    @pytest.mark.parametrize("faststart", [False, True])
    def test_truncated_source_is_never_published(self, real, tmp_path, faststart):
        """Index at the end: probe fails.  Index first: the file probes as
        3.5 s but only ~1.3 s of video decodes; padded audio keeps the
        container at 3.5 s, so only the per-stream duration check catches it.
        """
        workspace, output, _ = real
        source = workspace / "Shoot" / "landscape.mp4"
        if faststart:
            source = tmp_path / "faststart.mp4"
            run_ffmpeg(
                "-i", str(workspace / "Shoot" / "landscape.mp4"),
                "-c", "copy", "-movflags", "+faststart", str(source),
            )  # fmt: skip
        broken = workspace / "Shoot" / "broken.mp4"
        data = source.read_bytes()
        broken.write_bytes(data[: len(data) // 3])
        render, _ = self._tools(real)
        plan = {
            "output_name": "broken",
            "clips": [{"source": "Shoot/broken.mp4", "start": 0, "end": 3.5}],
        }
        result = render.execute(plan=plan)
        report = json.loads(result.content)
        assert not result.success
        expected = "failed_checks" if faststart else "invalid_media"
        assert report["render_status"] == expected, report
        if faststart:
            assert "video_duration" in report["error"]
        assert str(workspace) not in result.content
        assert not list(output.glob("*.mp4")) and _no_temp_left(output)
