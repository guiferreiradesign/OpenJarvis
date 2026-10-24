"""Tests for reading ``[jarvis.*]`` extension sections from config.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis_ext.config import (
    ExtensionConfigError,
    load_extension_section,
    resolve_config_path,
)
from jarvis_ext.tools.media_workspace import (
    DEFAULT_VIDEO_EXTENSIONS,
    MediaWorkspaceSettings,
)


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestLoadExtensionSection:
    def test_missing_file_is_unconfigured(self, tmp_path):
        assert load_extension_section("media_workspace", tmp_path / "nope.toml") == {}

    def test_reads_named_section_only(self, tmp_path):
        path = _write(
            tmp_path,
            '[engine]\ndefault = "ollama"\n\n'
            '[jarvis.media_workspace]\nroot = "/srv/media"\n\n'
            "[jarvis.other]\nx = 1\n",
        )
        assert load_extension_section("media_workspace", path) == {"root": "/srv/media"}

    def test_absent_section_is_empty(self, tmp_path):
        path = _write(tmp_path, '[engine]\ndefault = "ollama"\n')
        assert load_extension_section("media_workspace", path) == {}

    def test_malformed_toml_raises(self, tmp_path):
        path = _write(tmp_path, "[jarvis.media_workspace\nroot = ")
        with pytest.raises(ExtensionConfigError):
            load_extension_section("media_workspace", path)

    @pytest.mark.parametrize(
        "text",
        ['jarvis = "oops"\n', '[jarvis]\nmedia_workspace = "oops"\n'],
    )
    def test_non_table_raises(self, tmp_path, text):
        path = _write(tmp_path, text)
        with pytest.raises(ExtensionConfigError):
            load_extension_section("media_workspace", path)

    def test_env_override_matches_core_loader(self, tmp_path, monkeypatch):
        path = _write(tmp_path, "")
        monkeypatch.setenv("OPENJARVIS_CONFIG", str(path))
        assert resolve_config_path() == path.resolve()

    def test_default_path_uses_openjarvis_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OPENJARVIS_CONFIG", raising=False)
        monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path))
        assert resolve_config_path() == tmp_path / "config.toml"

    def test_core_loader_ignores_jarvis_section(self, tmp_path):
        """Adding [jarvis.*] must not break or alter the core config."""
        from openjarvis.core.config import load_config

        path = _write(tmp_path, '[jarvis.media_workspace]\nroot = "/srv/media"\n')
        load_config.cache_clear()
        try:
            cfg = load_config(path)
        finally:
            load_config.cache_clear()
        assert not hasattr(cfg, "jarvis")


class TestMediaWorkspaceSettings:
    def test_defaults_are_unconfigured(self):
        settings = MediaWorkspaceSettings.from_mapping({})
        assert settings.root is None
        assert settings.follow_symlinks is False
        assert settings.include_hidden is False
        assert settings.video_extensions == DEFAULT_VIDEO_EXTENSIONS

    def test_root_is_resolved(self, tmp_path):
        settings = MediaWorkspaceSettings.from_mapping({"root": str(tmp_path)})
        assert settings.root == tmp_path.resolve()

    def test_root_expands_user(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / "Movies").mkdir()
        settings = MediaWorkspaceSettings.from_mapping({"root": "~/Movies"})
        assert settings.root == (tmp_path / "Movies").resolve()

    @pytest.mark.parametrize("root", ["relative/media", "./media"])
    def test_relative_root_rejected(self, root):
        with pytest.raises(ExtensionConfigError, match="absolute"):
            MediaWorkspaceSettings.from_mapping({"root": root})

    def test_filesystem_root_rejected(self):
        with pytest.raises(ExtensionConfigError, match="filesystem root"):
            MediaWorkspaceSettings.from_mapping({"root": "/"})

    def test_whole_home_rejected(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        with pytest.raises(ExtensionConfigError, match="home directory"):
            MediaWorkspaceSettings.from_mapping({"root": str(tmp_path)})

    @pytest.mark.parametrize(
        "section",
        [
            {"root": 42},
            {"follow_symlinks": "yes"},
            {"include_hidden": 1},
            {"ffprobe_path": ["ffprobe"]},
            {"max_files": "10"},
            {"max_files": True},
            {"max_files": 0},
            {"max_files": 1001},
            {"max_files": 2.5},
            {"max_depth": -1},
            {"ffprobe_timeout_seconds": 0},
            {"probe_budget_seconds": float("inf")},
            {"video_extensions": []},
            {"video_extensions": ".mp4"},
            {"video_extensions": [3]},
            {"audio_extensions": ["../etc"]},
            {"video_extensions": [".wav"]},  # overlaps default audio
            {"rooot": "/typo"},
        ],
    )
    def test_invalid_values_rejected(self, section):
        with pytest.raises(ExtensionConfigError):
            MediaWorkspaceSettings.from_mapping(section)

    def test_extensions_are_normalised(self):
        settings = MediaWorkspaceSettings.from_mapping(
            {"video_extensions": ["MP4", ".Mov"], "audio_extensions": ["wav"]}
        )
        assert settings.video_extensions == {".mp4", ".mov"}
        assert settings.audio_extensions == {".wav"}

    def test_numeric_limits_accepted(self):
        settings = MediaWorkspaceSettings.from_mapping(
            {"max_files": 5, "max_depth": 0, "ffprobe_timeout_seconds": 3}
        )
        assert settings.max_files == 5
        assert settings.max_depth == 0
        assert settings.ffprobe_timeout_seconds == 3.0
