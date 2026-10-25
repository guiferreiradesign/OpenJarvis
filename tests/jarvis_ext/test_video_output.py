"""Output folder safety, versioning, FFmpeg command construction and process control."""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from jarvis_ext.tools.media_workspace import FFPROBE_FORMAT_WHITELIST
from jarvis_ext.video import ffmpeg as ff
from jarvis_ext.video.ffmpeg import (
    ClipInput,
    FfmpegCapabilities,
    LogoInput,
    build_decode_check_command,
    build_filter_graph,
    build_preview_command,
    build_render_command,
    build_thumbnail_command,
    log_tail,
    logo_geometry,
    png_dimensions,
    query_capabilities,
    run_process,
)
from jarvis_ext.video.output import (
    OutputError,
    OutputFolder,
    parse_render_name,
    versioned_name,
)
from jarvis_ext.video.plan import parse_edit_plan

from .video_fakes import OLD_SCALE_HELP, FakeRunner

CAPS = query_capabilities("ffmpeg", FakeRunner())
OLD_CAPS = query_capabilities("ffmpeg", FakeRunner(scale_help=OLD_SCALE_HELP))

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="POSIX filesystem")


@pytest.fixture
def folder(tmp_path):
    root = tmp_path / "out"
    root.mkdir()
    return OutputFolder(root)


def _staged(folder: OutputFolder, content: bytes = b"video") -> Path:
    path = folder.make_temp_dir() / "render.mp4"
    path.write_bytes(content)
    return path


# ---------------------------------------------------------------------------
# Versioned names and publishing
# ---------------------------------------------------------------------------


class TestVersioning:
    def test_names(self):
        assert versioned_name("reel", 1, ".mp4") == "reel_v001.mp4"
        assert versioned_name("reel", 1234, ".mp4") == "reel_v1234.mp4"
        assert parse_render_name("reel_v001.mp4") == ("reel", 1)
        assert parse_render_name("my-reel_2_v012.mp4") == ("my-reel_2", 12)
        for bad in (
            "reel.mp4",
            "reel_v01.mp4",
            "reel_v001.mov",
            "../reel_v001.mp4",
            "Reel_v001.mp4",
            "reel_v001.render.json",
        ):
            assert parse_render_name(bad) is None, bad

    def test_first_and_next_versions(self, folder):
        first, v1 = folder.publish_versioned(
            _staged(folder), folder.root, "reel", ".mp4"
        )
        second, v2 = folder.publish_versioned(
            _staged(folder), folder.root, "reel", ".mp4"
        )
        assert (first.name, v1, second.name, v2) == (
            "reel_v001.mp4",
            1,
            "reel_v002.mp4",
            2,
        )

    def test_never_overwrites_existing_files(self, folder):
        existing = folder.root / "reel_v001.mp4"
        existing.write_bytes(b"precious user file")
        (folder.root / "reel_v003.render.json").write_text("{}")
        final, version = folder.publish_versioned(
            _staged(folder, b"new"), folder.root, "reel", ".mp4"
        )
        # Highest existing version (from any file of that stem) + 1.
        assert (final.name, version) == ("reel_v004.mp4", 4)
        assert existing.read_bytes() == b"precious user file"

    def test_other_stems_are_ignored(self, folder):
        (folder.root / "reel_extra_v009.mp4").write_bytes(b"x")
        (folder.root / "reelx_v005.mp4").write_bytes(b"x")
        _, version = folder.publish_versioned(
            _staged(folder), folder.root, "reel", ".mp4"
        )
        assert version == 1

    def test_name_taken_between_scan_and_claim(self, folder, monkeypatch):
        """A race: the scan says v001 is free, but another render takes it."""
        (folder.root / "reel_v001.mp4").write_bytes(b"other render")
        monkeypatch.setattr(OutputFolder, "next_version", staticmethod(lambda d, s: 1))
        final, version = folder.publish_versioned(
            _staged(folder, b"mine"), folder.root, "reel", ".mp4"
        )
        assert version == 2
        assert (folder.root / "reel_v001.mp4").read_bytes() == b"other render"
        assert final.read_bytes() == b"mine"

    def test_concurrent_allocation_gets_distinct_versions(self, folder):
        staged = [_staged(folder, f"render {i}".encode()) for i in range(24)]
        results = []
        barrier = threading.Barrier(len(staged))

        def publish(path):
            barrier.wait()
            results.append(
                folder.publish_versioned(path, folder.root, "reel", ".mp4")[1]
            )

        threads = [threading.Thread(target=publish, args=(p,)) for p in staged]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert sorted(results) == list(range(1, 25))
        contents = {p.read_bytes() for p in folder.root.glob("reel_v*.mp4")}
        assert len(contents) == 24

    def test_fallback_without_hard_links(self, folder, monkeypatch):
        def no_links(src, dst, **_):
            raise OSError(errno.EPERM, "Operation not permitted")

        monkeypatch.setattr(os, "link", no_links)
        (folder.root / "reel_v001.mp4").write_bytes(b"keep me")
        monkeypatch.setattr(OutputFolder, "next_version", staticmethod(lambda d, s: 1))
        final, version = folder.publish_versioned(
            _staged(folder, b"new"), folder.root, "reel", ".mp4"
        )
        assert version == 2 and final.read_bytes() == b"new"
        assert (folder.root / "reel_v001.mp4").read_bytes() == b"keep me"

    def test_unexpected_link_error_propagates(self, folder, monkeypatch):
        def broken(src, dst, **_):
            raise OSError(errno.EIO, "I/O error")

        monkeypatch.setattr(os, "link", broken)
        with pytest.raises(OSError):
            folder.publish_versioned(_staged(folder), folder.root, "reel", ".mp4")
        assert not list(folder.root.glob("reel_v*"))

    def test_write_record_is_exclusive(self, folder):
        temp = folder.make_temp_dir()
        target = folder.root / "reel_v001.render.json"
        target.write_text("original")
        with pytest.raises(FileExistsError):
            folder.write_record(temp, target, {"x": 1})
        assert target.read_text() == "original"


# ---------------------------------------------------------------------------
# Folder safety
# ---------------------------------------------------------------------------


class TestFolderSafety:
    def test_missing_root(self, tmp_path):
        with pytest.raises(OutputError, match="does not exist"):
            OutputFolder(tmp_path / "unplugged").check_ready()

    def test_root_is_file(self, tmp_path):
        (tmp_path / "file").write_text("x")
        with pytest.raises(OutputError, match="not a directory"):
            OutputFolder(tmp_path / "file").check_ready()

    @posix_only
    def test_symlinked_subdir_is_refused(self, folder, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (folder.root / ".jarvis-tmp").symlink_to(elsewhere)
        with pytest.raises(OutputError, match="plain directory"):
            folder.make_temp_dir()
        assert list(elsewhere.iterdir()) == []

    def test_temp_dirs_are_private_and_cleaned(self, folder):
        temp = folder.make_temp_dir()
        (temp / "partial.mp4").write_bytes(b"half")
        if sys.platform != "win32":
            assert temp.stat().st_mode & 0o077 == 0
        folder.remove_temp_dir(temp)
        assert not temp.exists()

    def test_remove_temp_dir_refuses_other_paths(self, folder):
        precious = folder.root / "keep"
        precious.mkdir()
        (precious / "a.mp4").write_bytes(b"x")
        folder.remove_temp_dir(precious)
        folder.remove_temp_dir(folder.root)
        folder.remove_temp_dir(None)
        assert (precious / "a.mp4").exists()

    @pytest.mark.parametrize(
        "name",
        ["../x.mp4", "/etc/passwd", "a/b/c.mp4", "other/x.mp4", ".hidden", "", "a\\b"],
    )
    def test_resolve_child_rejects(self, folder, name):
        with pytest.raises((OutputError, FileNotFoundError)):
            folder.resolve_child(name)

    @posix_only
    def test_resolve_child_rejects_symlink(self, folder, tmp_path):
        (tmp_path / "secret.mp4").write_bytes(b"x")
        (folder.root / "reel_v001.mp4").symlink_to(tmp_path / "secret.mp4")
        with pytest.raises(OutputError, match="regular file"):
            folder.resolve_child("reel_v001.mp4")


# ---------------------------------------------------------------------------
# FFmpeg commands
# ---------------------------------------------------------------------------

HOSTILE_NAMES = [
    "-y.mp4",
    "clip; rm -rf ~.mp4",
    "it's [0:v] a,b=c.mov",
    "$(touch pwned).mp4",
    "concat:a.mp4|b.mp4",
    "http:evil.mp4",
]


def _plan(**extra):
    data = {
        "output_name": "reel",
        "clips": [{"source": "a.mp4", "start": 1.5, "end": 4}] * 2,
    }
    data.update(extra)
    return parse_edit_plan(data)


def _clips(tmp_path, names):
    return [
        ClipInput(path=tmp_path / name, start=1.5, duration=2.5, has_audio=True)
        for name in names
    ]


class TestCommandConstruction:
    def test_hostile_file_names_stay_single_input_arguments(self, tmp_path):
        names = HOSTILE_NAMES[:2]
        clips = _clips(tmp_path, names)
        cmd = build_render_command("/usr/bin/ffmpeg", _plan(), clips)
        assert isinstance(cmd, list) and all(isinstance(a, str) for a in cmd)
        for clip in clips:
            index = cmd.index(f"file:{clip.path}")
            assert cmd[index - 1] == "-i"
        graph = cmd[cmd.index("-filter_complex") + 1]
        for name in names:
            assert name not in graph
        assert str(tmp_path) not in graph

    @pytest.mark.parametrize("name", HOSTILE_NAMES)
    def test_every_hostile_name(self, tmp_path, name):
        clips = _clips(tmp_path, [name, "ok.mp4"])
        cmd = build_render_command("ffmpeg", _plan(), clips)
        assert cmd.count(f"file:{tmp_path / name}") == 1
        assert name not in cmd[cmd.index("-filter_complex") + 1]

    def test_inputs_are_restricted_and_output_never_overwrites(self, tmp_path):
        cmd = build_render_command("ffmpeg", _plan(), _clips(tmp_path, ["a", "b"]))
        inputs = [i for i, arg in enumerate(cmd) if arg == "-i"]
        assert len(inputs) == 2
        for i in inputs:
            window = cmd[i - 6 : i]
            assert window[window.index("-protocol_whitelist") + 1] == "file"
            assert window[window.index("-format_whitelist") + 1] == (
                FFPROBE_FORMAT_WHITELIST
            )
        assert "-y" not in cmd and "-n" in cmd
        assert cmd[-1] == "file:render.mp4"
        assert cmd[cmd.index("-map_metadata") + 1] == "-1"
        assert cmd[cmd.index("-c:v") + 1] == "libx264"
        assert cmd[cmd.index("-r") + 1] == "30"
        assert cmd[cmd.index("-ss") + 1] == "1.5"
        assert cmd[cmd.index("-t") + 1] == "2.5"

    def test_filter_graph_crop_and_pad(self, tmp_path):
        clips = _clips(tmp_path, ["a", "b"])
        graph, video, audio = build_filter_graph(_plan(), clips)
        assert "force_original_aspect_ratio=increase:" in graph
        assert ",crop=1080:1920," in graph
        assert "concat=n=2:v=1:a=1" in graph
        assert (video, audio) == ("vcat", "acat")
        graph, _, _ = build_filter_graph(_plan(fit="pad"), clips)
        assert "pad=1080:1920" in graph and "crop=" not in graph

    @pytest.mark.parametrize("fit", ["crop", "pad"])
    @pytest.mark.parametrize("managed", [True, False])
    def test_every_clip_is_converted_to_limited_range(self, tmp_path, fit, managed):
        """Regression: full-range (yuvj420p) iPhone clips produced a
        full-range output because only the pixel layout was converted."""
        caps = CAPS if managed else OLD_CAPS
        clips = _clips(tmp_path, ["a", "b"])
        graph, _, _ = build_filter_graph(_plan(fit=fit), clips, capabilities=caps)
        chains = [part for part in graph.split(";") if ":v:0]scale=" in part]
        assert len(chains) == 2
        for chain in chains:
            scale = chain.split(",")[0]
            assert "out_range=tv" in scale and "out_color_matrix=bt709" in scale
            assert ("out_primaries=bt709" in scale) is managed
            assert ("out_transfer=bt709" in scale) is managed
            assert "intent=" not in scale
            assert chain.endswith("format=yuv420p[v0]") or chain.endswith(
                "format=yuv420p[v1]"
            )

    def test_only_hdr_clips_are_tone_mapped(self, tmp_path):
        clips = [
            ClipInput(tmp_path / "sdr", 0, 2.0, True, color_transfer="bt709"),
            ClipInput(tmp_path / "hlg", 0, 2.0, True, color_transfer="arib-std-b67"),
            ClipInput(tmp_path / "pq", 0, 2.0, True, color_transfer="smpte2084"),
        ]
        plan = parse_edit_plan(
            {
                "output_name": "reel",
                "clips": [{"source": "a.mp4", "start": 0, "end": 2}] * 3,
            }
        )
        graph, _, _ = build_filter_graph(plan, clips, capabilities=CAPS)
        scales = [p.split(",")[0] for p in graph.split(";") if ":v:0]scale=" in p]
        assert ["intent=perceptual" in s for s in scales] == [False, True, True]
        assert all("out_transfer=bt709" in s for s in scales)

    def test_output_is_tagged_rec709_limited(self, tmp_path):
        cmd = build_render_command("ffmpeg", _plan(), _clips(tmp_path, ["a", "b"]))
        tags = {cmd[i]: cmd[i + 1] for i in range(len(cmd) - 1)}
        assert tags["-color_range"] == "tv"
        assert tags["-color_primaries"] == "bt709"
        assert tags["-color_trc"] == "bt709"
        assert tags["-colorspace"] == "bt709"
        assert tags["-pix_fmt"] == "yuv420p"

    def test_silent_clip_gets_generated_silence(self, tmp_path):
        clips = [ClipInput(tmp_path / "a", 0, 2.0, has_audio=False)]
        graph, _, _ = build_filter_graph(_plan(), clips)
        assert "anullsrc" in graph and "[0:a:0]" not in graph

    def test_audio_none(self, tmp_path):
        plan = _plan(audio={"mode": "none"})
        cmd = build_render_command("ffmpeg", plan, _clips(tmp_path, ["a", "b"]))
        assert "-an" in cmd and "-c:a" not in cmd
        graph = cmd[cmd.index("-filter_complex") + 1]
        assert "concat=n=2:v=1:a=0" in graph and ":a:0]" not in graph

    def test_logo_and_embedded_subtitles(self, tmp_path):
        plan = _plan(
            branding={"asset": "logo.png", "opacity": 0.5},
            subtitles={"source": "c.srt", "language": "eng"},
        )
        logo = LogoInput(tmp_path / "it's [logo].png", 400, 200)
        cmd = build_render_command(
            "ffmpeg", plan, _clips(tmp_path, ["a", "b"]), logo=logo
        )
        assert cmd[
            cmd.index(f"file:{logo.path}") - 3 : cmd.index(f"file:{logo.path}")
        ] == ["-f", "png_pipe", "-i"]
        assert "file:subs.srt" in cmd
        assert cmd[cmd.index("-c:s") + 1] == "mov_text"
        assert "language=eng" in cmd
        graph = cmd[cmd.index("-filter_complex") + 1]
        assert "overlay=" in graph and "colorchannelmixer=aa=0.500" in graph
        assert "logo].png" not in graph
        maps = [cmd[i + 1] for i, a in enumerate(cmd) if a == "-map"]
        assert maps == ["[vbrand]", "[acat]", "3:s:0"]

    def test_burned_subtitles_use_fixed_relative_name(self, tmp_path):
        plan = _plan(subtitles={"source": "c.srt", "mode": "burn"})
        cmd = build_render_command("ffmpeg", plan, _clips(tmp_path, ["a", "b"]))
        graph = cmd[cmd.index("-filter_complex") + 1]
        assert graph.endswith("[vcat]subtitles=filename=subs.srt[vsub]")
        assert "file:subs.srt" not in cmd and "-c:s" not in cmd

    @pytest.mark.parametrize(
        "position, expected_xy",
        [
            ("top-left", (48, 48)),
            ("top-right", (1080 - 216 - 48, 48)),
            ("bottom-left", (48, 1920 - 108 - 48)),
            ("bottom-right", (1080 - 216 - 48, 1920 - 108 - 48)),
            ("center", ((1080 - 216) // 2, (1920 - 108) // 2)),
        ],
    )
    def test_logo_geometry(self, tmp_path, position, expected_xy):
        plan = _plan(branding={"asset": "l.png", "position": position})
        w, h, x, y = logo_geometry(plan, LogoInput(tmp_path / "l.png", 400, 200))
        assert (w, h) == (216, 108)
        assert (x, y) == expected_xy

    def test_mismatched_inputs_rejected(self, tmp_path):
        with pytest.raises(ValueError):
            build_render_command("ffmpeg", _plan(), _clips(tmp_path, ["a"]))
        with pytest.raises(ValueError):
            build_render_command(
                "ffmpeg",
                _plan(branding={"asset": "l.png"}),
                _clips(tmp_path, ["a", "b"]),
            )

    def test_review_commands(self, tmp_path):
        source = tmp_path / "-reel_v001.mp4"
        for cmd in (
            build_preview_command("ffmpeg", source, height=640, has_audio=True),
            build_thumbnail_command("ffmpeg", source, at_seconds=1.25, height=640),
            build_decode_check_command("ffmpeg", source),
        ):
            assert cmd[cmd.index("-i") + 1] == f"file:{source}"
            assert cmd[cmd.index("-protocol_whitelist") + 1] == "file"
            assert "-y" not in cmd
        preview = build_preview_command("ffmpeg", source, height=640, has_audio=False)
        assert "-an" in preview and preview[-1] == "file:preview.mp4"

    def test_png_dimensions(self):
        header = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR" + (
            (640).to_bytes(4, "big") + (480).to_bytes(4, "big")
        )
        assert png_dimensions(header) == (640, 480)
        assert png_dimensions(b"GIF89a" + b"\x00" * 30) is None
        assert png_dimensions(header[:20]) is None


class TestCapabilities:
    def test_parses_listings(self):
        caps = query_capabilities("ffmpeg", FakeRunner())
        assert caps.version == "9.9-test"
        assert {"libx264", "aac", "mov_text"} <= caps.encoders
        assert {"overlay", "scale", "concat"} <= caps.filters
        assert "subtitles" not in caps.filters

    def test_scale_colour_management(self):
        caps = query_capabilities("ffmpeg", FakeRunner())
        assert caps.color_management and caps.tone_mapping
        old = query_capabilities("ffmpeg", FakeRunner(scale_help=OLD_SCALE_HELP))
        assert not old.color_management and not old.tone_mapping
        assert not FfmpegCapabilities().tone_mapping

    def test_missing_features(self):
        caps = query_capabilities("ffmpeg", FakeRunner())
        assert caps.missing_for(_plan()) == []
        burn = _plan(subtitles={"source": "c.srt", "mode": "burn"})
        assert caps.missing_for(burn) == ["subtitles"]
        assert "libx264" in FfmpegCapabilities().missing_for(_plan())

    def test_broken_ffmpeg(self):
        def runner(cmd, **kw):
            return subprocess.CompletedProcess(cmd, 1, b"", b"")

        with pytest.raises(ff.FfmpegError):
            query_capabilities("ffmpeg", runner)

        def missing(cmd, **kw):
            raise FileNotFoundError(cmd[0])

        with pytest.raises(ff.FfmpegError):
            query_capabilities("ffmpeg", missing)


# ---------------------------------------------------------------------------
# Process control (real child processes, no FFmpeg needed)
# ---------------------------------------------------------------------------

SLEEPER = [
    sys.executable,
    "-c",
    "import os, time; open('pid', 'w').write(str(os.getpid())); time.sleep(60)",
]


def _wait_for_pid(directory: Path) -> int:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            return int((directory / "pid").read_text())
        except (FileNotFoundError, ValueError):
            time.sleep(0.05)
    raise AssertionError("child did not start")


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


@posix_only
class TestProcessControl:
    def test_success_and_log(self, tmp_path):
        code = run_process(
            [sys.executable, "-c", "import sys; sys.stderr.write('oops\\n'); exit(3)"],
            cwd=tmp_path,
            timeout=30,
            log_path=tmp_path / "log",
        )
        assert code == 3
        assert log_tail(tmp_path / "log", redact={}) == "oops"

    def test_timeout_stops_the_process(self, tmp_path):
        started = time.monotonic()
        with pytest.raises(subprocess.TimeoutExpired):
            run_process(SLEEPER, cwd=tmp_path, timeout=1.0, log_path=tmp_path / "log")
        assert time.monotonic() - started < 15
        assert not _is_running(_wait_for_pid(tmp_path))

    def test_interrupt_stops_the_process(self, tmp_path, monkeypatch):
        real_wait = subprocess.Popen.wait
        calls = []

        def wait(self, timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                _wait_for_pid(tmp_path)
                raise KeyboardInterrupt
            return real_wait(self, timeout)

        monkeypatch.setattr(subprocess.Popen, "wait", wait)
        with pytest.raises(KeyboardInterrupt):
            run_process(SLEEPER, cwd=tmp_path, timeout=60, log_path=tmp_path / "log")
        assert not _is_running(int((tmp_path / "pid").read_text()))

    def test_environment_is_minimal(self, tmp_path, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
        run_process(
            [sys.executable, "-c", "import os; print(sorted(os.environ))"],
            cwd=tmp_path,
            timeout=30,
            log_path=tmp_path / "log",
        )
        # stdout is discarded; check the env we would pass instead.
        assert "ANTHROPIC_API_KEY" not in ff.minimal_env()

    def test_log_tail_redacts_paths(self, tmp_path):
        log = tmp_path / "log"
        log.write_text(
            "noise\n"
            f"{tmp_path}/media/clip.mp4: Invalid data\n"
            f"Error opening {tmp_path}/out/.jarvis-tmp/job-1/render.mp4\n"
        )
        tail = log_tail(
            log,
            redact={
                str(tmp_path / "media"): "<workspace>",
                str(tmp_path / "out"): "<output>",
            },
        )
        assert str(tmp_path) not in tail
        assert "<workspace>/clip.mp4" in tail and "<output>/" in tail
        assert log_tail(tmp_path / "missing", redact={}) == ""
