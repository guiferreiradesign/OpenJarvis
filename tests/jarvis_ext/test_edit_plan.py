"""EditPlan parsing, presets, render settings, subtitles and output checks.

Pure logic: no FFmpeg and (apart from settings) no filesystem.
"""

from __future__ import annotations

import copy
import math
from pathlib import Path

import pytest

from jarvis_ext.config import ExtensionConfigError
from jarvis_ext.video.checks import Expectations, all_passed, check_media
from jarvis_ext.video.plan import (
    BUILTIN_PRESETS,
    PlanError,
    PlanLimits,
    parse_edit_plan,
    parse_timestamp,
    preset_from_mapping,
)
from jarvis_ext.video.settings import MediaRenderSettings, check_separation
from jarvis_ext.video.subtitles import (
    SubtitleError,
    fit_to_duration,
    parse_srt,
    segments_to_cues,
    to_srt,
    transcribe_to_srt,
)
from openjarvis.speech._stubs import Segment, TranscriptionResult

from .video_fakes import probe_payload

VALID = {
    "output_name": "skyline_reel",
    "clips": [
        {"source": "Skyline/a.mp4", "start": "00:00:05.5", "end": 12},
        {"source": "Skyline/b.mov", "start": 0, "end": "1:02.25"},
    ],
}


def _plan(**changes):
    data = copy.deepcopy(VALID)
    data.update(changes)
    return data


# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------


class TestTimestamps:
    @pytest.mark.parametrize(
        "value, seconds",
        [
            (0, 0.0),
            (12.5, 12.5),
            ("7", 7.0),
            ("7.125", 7.125),
            ("1:02.25", 62.25),
            ("01:02:03.5", 3723.5),
            ("00:00:00.000001", 0.000001),
        ],
    )
    def test_valid(self, value, seconds):
        assert parse_timestamp(value) == pytest.approx(seconds)

    @pytest.mark.parametrize(
        "value",
        [
            -1,
            "-1",
            "",
            "abc",
            "1:60",
            "1:60:00",
            "1:2:3:4",
            "1:02.5.5",
            " 1 : 2 ",
            "1e3",
            "nan",
            math.nan,
            math.inf,
            True,
            None,
            [1],
            25 * 3600,
            "0.1234567",
        ],
    )
    def test_invalid(self, value):
        with pytest.raises(PlanError):
            parse_timestamp(value)


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


class TestEditPlan:
    def test_valid_plan_defaults(self):
        plan = parse_edit_plan(_plan())
        assert plan.preset.name == "instagram_reel"
        assert (plan.width, plan.height, plan.frame_rate) == (1080, 1920, 30)
        assert plan.preset.aspect_ratio == "9:16"
        assert [c.source for c in plan.clips] == ["Skyline/a.mp4", "Skyline/b.mov"]
        assert plan.clips[0].start == 5.5 and plan.clips[1].end == 62.25
        assert plan.expected_duration == pytest.approx(6.5 + 62.25)
        assert plan.fit == "crop" and plan.audio_mode == "keep"
        assert plan.subtitles is None and plan.branding is None

    def test_order_is_list_order(self):
        data = _plan()
        data["clips"].reverse()
        plan = parse_edit_plan(data)
        assert [c.source for c in plan.clips] == ["Skyline/b.mov", "Skyline/a.mp4"]

    def test_full_plan_round_trip(self):
        plan = parse_edit_plan(
            _plan(
                fit="pad",
                video={"frame_rate": 60, "crf": 18, "x264_preset": "slow"},
                audio={"mode": "none", "bitrate_kbps": 192},
                subtitles={"source": "caps.srt", "mode": "burn", "language": "por"},
                branding={
                    "asset": "brand/logo.png",
                    "position": "bottom-left",
                    "width_fraction": 0.3,
                    "margin_px": 0,
                    "opacity": 0.5,
                },
                duration_tolerance_seconds=0.1,
            )
        )
        data = plan.to_dict()
        assert data["video"]["frame_rate"] == 60
        assert data["video"]["crf"] == 18
        assert data["audio"]["mode"] == "none"
        assert data["subtitles"] == {
            "source": "caps.srt",
            "mode": "burn",
            "language": "por",
        }
        assert data["branding"]["position"] == "bottom-left"
        assert data["expected_duration_seconds"] == pytest.approx(68.75)
        assert Expectations.from_plan_dict(data).audio is False

    @pytest.mark.parametrize(
        "changes, message",
        [
            ({"output_name": "Bad Name"}, "output_name"),
            ({"output_name": "../escape"}, "output_name"),
            ({"output_name": ""}, "output_name"),
            ({"output_name": "a" * 49}, "output_name"),
            ({"version": 2}, "version"),
            ({"preset": "tiktok"}, "preset"),
            ({"clips": []}, "clips"),
            ({"clips": "a.mp4"}, "clips"),
            ({"clips": [{"source": "a.mp4", "start": 3, "end": 3}]}, "after start"),
            ({"clips": [{"source": "a.mp4", "start": 5, "end": 2}]}, "after start"),
            ({"clips": [{"source": "a.mp4", "start": 0, "end": 0.05}]}, "shorter"),
            ({"clips": [{"source": "a.mp4", "start": 0}]}, "start and end"),
            ({"clips": [{"source": "", "start": 0, "end": 1}]}, "source"),
            ({"clips": [{"source": 5, "start": 0, "end": 1}]}, "source"),
            ({"clips": [{"source": "a\nb.mp4", "start": 0, "end": 1}]}, "control"),
            (
                {"clips": [{"source": "a.mp4", "start": 0, "end": 1, "speed": 2}]},
                "Unknown keys",
            ),
            ({"fit": "stretch"}, "fit"),
            ({"video": {"frame_rate": 29.97}}, "frame_rate"),
            ({"video": {"crf": 51}}, "crf"),
            ({"video": {"crf": 20.5}}, "crf"),
            ({"video": {"x264_preset": "placebo"}}, "x264_preset"),
            ({"video": {"codec": "hevc"}}, "Unknown keys"),
            ({"audio": {"mode": "music"}}, "audio.mode"),
            ({"audio": {"bitrate_kbps": 8}}, "bitrate"),
            ({"subtitles": {"source": "caps.ass"}}, ".srt"),
            ({"subtitles": {"source": "c.srt", "mode": "open"}}, "subtitles.mode"),
            ({"subtitles": {"source": "c.srt", "language": "en"}}, "language"),
            ({"branding": {"asset": "logo.svg"}}, ".png"),
            ({"branding": {"asset": "l.png", "position": "middle"}}, "position"),
            ({"branding": {"asset": "l.png", "width_fraction": 0.9}}, "width"),
            ({"branding": {"asset": "l.png", "opacity": 0}}, "opacity"),
            ({"branding": {"asset": "l.png", "margin_px": -1}}, "margin"),
            ({"duration_tolerance_seconds": 0}, "tolerance"),
            ({"output_path": "/tmp/x.mp4"}, "Unknown keys"),
            (
                {"clips": [{"source": "a.mp4", "start": 0, "end": 181}]},
                "exceeds",
            ),
        ],
    )
    def test_rejected(self, changes, message):
        with pytest.raises(PlanError, match=message):
            parse_edit_plan(_plan(**changes))

    @pytest.mark.parametrize("data", [None, [], "plan", 42])
    def test_not_an_object(self, data):
        with pytest.raises(PlanError):
            parse_edit_plan(data)

    def test_limits(self):
        limits = PlanLimits(max_clips=1)
        with pytest.raises(PlanError, match="At most 1"):
            parse_edit_plan(_plan(), limits)
        limits = PlanLimits(max_output_seconds=10)
        with pytest.raises(PlanError, match="10s limit"):
            parse_edit_plan(_plan(), limits)


class TestPresets:
    def test_builtin_reel(self):
        reel = BUILTIN_PRESETS["instagram_reel"]
        assert (reel.width, reel.height, reel.frame_rate) == (1080, 1920, 30)
        assert reel.to_dict()["video_codec"] == "h264"

    def test_custom_preset(self):
        preset = preset_from_mapping(
            "square", {"width": 1080, "height": 1080, "frame_rate": 25}
        )
        assert preset.aspect_ratio == "1:1"

    @pytest.mark.parametrize(
        "raw",
        [
            {"width": 1081, "height": 1920, "frame_rate": 30},
            {"width": 1080, "height": 1920, "frame_rate": 29},
            {"width": 99999, "height": 1920, "frame_rate": 30},
            {"width": 1080, "height": 1920},
            {"width": 1080, "height": 1920, "frame_rate": 30, "codec": "vp9"},
        ],
    )
    def test_invalid_custom_preset(self, raw):
        with pytest.raises(PlanError):
            preset_from_mapping("custom", raw)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


class TestRenderSettings:
    def test_defaults_unconfigured(self):
        settings = MediaRenderSettings.from_mapping({})
        assert settings.output_root is None
        assert settings.default_preset == "instagram_reel"

    def test_valid(self, tmp_path):
        settings = MediaRenderSettings.from_mapping(
            {
                "output_root": str(tmp_path / "out"),
                "assets_root": str(tmp_path / "assets"),
                "render_timeout_seconds": 60,
                "presets": {
                    "square": {"width": 1080, "height": 1080, "frame_rate": 30}
                },
                "default_preset": "square",
            }
        )
        assert settings.output_root == (tmp_path / "out").resolve()
        assert settings.plan_limits.default_preset == "square"
        assert parse_edit_plan(_plan(), settings.plan_limits).width == 1080

    @pytest.mark.parametrize(
        "section, message",
        [
            ({"output_root": "relative/out"}, "absolute"),
            ({"output_root": "/"}, "filesystem root"),
            ({"output_root": "~"}, "home directory"),
            ({"output_root": 5}, "string"),
            ({"bogus": 1}, "Unknown"),
            ({"render_timeout_seconds": 0}, "between"),
            ({"max_clips": 2.5}, "integer"),
            ({"require_confirmation": "yes"}, "true or false"),
            ({"default_preset": "nope"}, "default_preset"),
            ({"presets": {"instagram_reel": {}}}, "built-in"),
            ({"presets": {"x": {"width": 3}}}, "media_render"),
        ],
    )
    def test_invalid(self, section, message):
        with pytest.raises(ExtensionConfigError, match=message):
            MediaRenderSettings.from_mapping(section)

    def test_assets_and_output_must_be_separate(self, tmp_path):
        with pytest.raises(ExtensionConfigError, match="separate"):
            MediaRenderSettings.from_mapping(
                {
                    "output_root": str(tmp_path / "out"),
                    "assets_root": str(tmp_path / "out" / "logos"),
                }
            )

    def test_separation(self, tmp_path):
        workspace = tmp_path / "media"
        (workspace / "renders").mkdir(parents=True)
        sibling = tmp_path / "renders"
        sibling.mkdir()
        check_separation(sibling, workspace)
        for bad in (workspace, workspace / "renders", tmp_path):
            with pytest.raises(ExtensionConfigError, match="outside the media"):
                check_separation(bad, workspace)

    def test_separation_sees_through_symlinks(self, tmp_path):
        workspace = tmp_path / "media"
        workspace.mkdir()
        alias = tmp_path / "alias"
        alias.symlink_to(workspace)
        with pytest.raises(ExtensionConfigError):
            check_separation(alias / "out", workspace)

    def test_separation_ignores_letter_case(self, tmp_path):
        workspace = tmp_path / "Media"
        workspace.mkdir()
        other_case = tmp_path / "media"
        if not other_case.exists():
            pytest.skip("case-sensitive filesystem")
        with pytest.raises(ExtensionConfigError):
            check_separation(other_case, workspace)


# ---------------------------------------------------------------------------
# Subtitles
# ---------------------------------------------------------------------------

SRT = (
    "﻿1\r\n00:00:01,000 --> 00:00:02,500\r\nHello {\\an8}<i>world</i>\r\n\r\n"
    "2\r\n00:00:03.000 --> 00:00:04.000 X1:10\r\nSecond\r\nline\r\n\r\n"
    "3\r\n00:00:05,000 --> 00:00:06,000\r\n{\\fs99}\r\n"
)


class TestSubtitles:
    def test_parse_and_sanitise(self):
        cues = parse_srt(SRT.encode("utf-8"))
        assert [(c.start, c.end, c.text) for c in cues] == [
            (1.0, 2.5, "Hello world"),
            (3.0, 4.0, "Second\nline"),
        ]

    def test_round_trip(self):
        text = to_srt(parse_srt(SRT.encode()))
        assert text.startswith("1\n00:00:01,000 --> 00:00:02,500\nHello world\n")
        assert parse_srt(text.encode())[1].text == "Second\nline"

    @pytest.mark.parametrize(
        "data, message",
        [
            (b"\xff\xfe\x00", "UTF-8"),
            (b"", "no cues"),
            (b"1\nnot a time\nHi\n", "timing"),
            (b"1\n00:00:02,000 --> 00:00:01,000\nHi\n", "ends before"),
            (b"1\n00:61:00,000 --> 00:62:00,000\nHi\n", "invalid time"),
            (b"x" * (1024 * 1024 + 1), "1 MB"),
        ],
    )
    def test_invalid(self, data, message):
        with pytest.raises(SubtitleError, match=message):
            parse_srt(data)

    def test_fit_to_duration(self):
        cues = parse_srt(SRT.encode())
        kept, dropped = fit_to_duration(cues, 3.5)
        assert dropped == 0 and kept[-1].end == 3.5
        kept, dropped = fit_to_duration(cues, 2.0)
        assert dropped == 1 and kept[0].end == 2.0

    def test_transcription_extension_point(self):
        class Backend:
            def transcribe(self, audio, *, format="wav", language=None):
                assert audio == b"pcm" and language == "en"
                return TranscriptionResult(
                    text="hi there",
                    segments=[
                        Segment("hi", 0.0, 0.8),
                        Segment("{\\b1}there", 0.8, 1.6),
                        Segment("", 2.0, 3.0),
                    ],
                )

        srt = transcribe_to_srt(Backend(), b"pcm", language="en")
        assert [c.text for c in parse_srt(srt.encode())] == ["hi", "there"]
        assert segments_to_cues([Segment("x", 2.0, 1.0)]) == []

    def test_transcription_without_segments(self):
        class Backend:
            def transcribe(self, audio, **_):
                return TranscriptionResult(text="untimed")

        with pytest.raises(SubtitleError):
            transcribe_to_srt(Backend(), b"")


# ---------------------------------------------------------------------------
# Output checks
# ---------------------------------------------------------------------------


def _meta(**kw):
    from jarvis_ext.tools.media_workspace import parse_ffprobe_output

    return parse_ffprobe_output(probe_payload(**kw))


EXPECT = Expectations(
    width=1080, height=1920, frame_rate=30, duration=10.0, tolerance=0.25, audio=True
)


class TestChecks:
    def test_matching_output_passes(self):
        checks = check_media(_meta(width=1080, height=1920), 100, EXPECT)
        assert all_passed(checks), checks

    @pytest.mark.parametrize(
        "meta_kw, size, failed",
        [
            ({"width": 1920, "height": 1080}, 100, "dimensions"),
            ({"width": 1080, "height": 1920, "duration": 10.3}, 100, "duration"),
            ({"width": 1080, "height": 1920, "rate": "25/1"}, 100, "frame_rate"),
            ({"width": 1080, "height": 1920, "audio": False}, 100, "audio_stream"),
            ({"width": 1080, "height": 1920, "codec": "hevc"}, 100, "video_codec"),
            (
                {"width": 1080, "height": 1920, "pix_fmt": "yuv420p10le"},
                100,
                "pixel_format",
            ),
            (
                {"width": 1080, "height": 1920, "color_range": "pc"},
                100,
                "color_range",
            ),
            (
                {"width": 1080, "height": 1920, "color_range": None},
                100,
                "color_range",
            ),
            (
                {
                    "width": 1080,
                    "height": 1920,
                    "color_transfer": "arib-std-b67",
                    "color_primaries": "bt2020",
                },
                100,
                "color_metadata",
            ),
            (
                {"width": 1080, "height": 1920, "container": "matroska,webm"},
                100,
                "container",
            ),
            ({"width": 1080, "height": 1920}, 0, "non_empty"),
            (
                {"width": 1080, "height": 1920, "video_duration": 4.0},
                100,
                "video_duration",
            ),
        ],
    )
    def test_mismatch_fails(self, meta_kw, size, failed):
        checks = check_media(_meta(**meta_kw), size, EXPECT)
        assert not all_passed(checks)
        assert failed in [c["check"] for c in checks if not c["passed"]]

    def test_unprobeable_output_fails(self):
        checks = check_media({"status": "invalid_media"}, 100, EXPECT)
        assert not all_passed(checks)
        assert [c["check"] for c in checks] == ["non_empty", "probe"]

    def test_audio_must_be_absent_when_not_requested(self):
        expected = Expectations(1080, 1920, 30, 10.0, 0.25, audio=False)
        checks = check_media(_meta(width=1080, height=1920), 100, expected)
        assert not all_passed(checks)

    def test_subtitle_track_required_for_embed(self):
        expected = Expectations(1080, 1920, 30, 10.0, 0.25, True, subtitle_track=True)
        meta = _meta(width=1080, height=1920)
        assert not all_passed(check_media(meta, 1, expected))
        meta = _meta(width=1080, height=1920, subtitles=1)
        assert all_passed(check_media(meta, 1, expected))

    @pytest.mark.parametrize(
        "plan",
        [
            {},
            {"video": {}, "audio": {}},
            {
                "video": {"width": 1080, "height": 1920, "frame_rate": 30},
                "audio": {"mode": "keep"},
                "expected_duration_seconds": -1,
                "duration_tolerance_seconds": 0.25,
            },
        ],
    )
    def test_malformed_record(self, plan):
        with pytest.raises(ValueError):
            Expectations.from_plan_dict(plan)


def test_settings_path_is_resolved(tmp_path):
    link = tmp_path / "link"
    real = tmp_path / "real"
    real.mkdir()
    link.symlink_to(real)
    settings = MediaRenderSettings.from_mapping({"output_root": str(link)})
    assert settings.output_root == Path(real).resolve()
