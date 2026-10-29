"""The preflight harness: its core rules, and that its checks can really fail.

Core tests use fake checks.  Check tests run the built-in checks against
temporary data; "mutation" tests break the thing under test on purpose and
require the matching check to FAIL, so a green preflight cannot be vacuous.
Real-FFmpeg tests skip when FFmpeg is not installed.
"""

from __future__ import annotations

import json
import os
import socket
import threading
from pathlib import Path
from typing import Any, List

import pytest

import jarvis_ext
from jarvis_ext.__main__ import main as cli_main
from jarvis_ext.jobs import JobStatus, JobStore
from jarvis_ext.jobs.runner import JobOutcome
from jarvis_ext.preflight import core
from jarvis_ext.preflight.checks import app as app_checks
from jarvis_ext.preflight.checks import brain as brain_checks
from jarvis_ext.preflight.checks import future as future_checks
from jarvis_ext.preflight.checks import jobs as job_checks
from jarvis_ext.preflight.checks import media as media_checks
from jarvis_ext.preflight.core import (
    FAIL,
    PASS,
    WARN,
    Blocked,
    Check,
    Context,
    Outcome,
    check,
    failed,
    known_secret_values,
    not_run,
    passed,
    redact,
    registered_checks,
    run_check,
    run_preflight,
    temporary_env,
    warned,
)
from jarvis_ext.preflight.fixtures import FixtureUnavailable
from openjarvis.core.registry import EngineRegistry

from .video_fakes import HAVE_FFMPEG

needs_ffmpeg = pytest.mark.skipif(not HAVE_FFMPEG, reason="needs ffmpeg and ffprobe")


@pytest.fixture
def ctx(tmp_path: Path) -> Context:
    return Context(workdir=tmp_path)


def run_one(fn: Any, context: Context, budget: Any = None, secrets: Any = None):
    item = Check("t.one", "app", "a test check", fn, budget)
    return run_check(item, context, secrets or {})


def by_id(check_id: str) -> Check:
    return next(c for c in registered_checks() if c.id == check_id)


def run_builtin(check_id: str, context: Context):
    return run_check(by_id(check_id), context, {})


# -- core ----------------------------------------------------------------------


class TestCore:
    def test_unknown_status_is_refused(self):
        with pytest.raises(ValueError):
            Outcome("OK")

    def test_pass_fail_warn(self, ctx):
        assert run_one(lambda c: passed("fine"), ctx).status == PASS
        assert run_one(lambda c: failed("broken"), ctx).status == FAIL
        assert run_one(lambda c: warned("meh"), ctx).status == WARN

    def test_exception_is_a_failure_with_its_cause(self, ctx):
        def boom(c):
            raise KeyError("missing thing")

        result = run_one(boom, ctx)
        assert result.status == FAIL
        assert "KeyError" in result.detail and "missing thing" in result.detail

    def test_non_outcome_return_is_a_failure(self, ctx):
        assert run_one(lambda c: True, ctx).status == FAIL

    def test_blocked_check_is_not_run_and_has_no_timing(self, ctx):
        def blocked(c):
            raise Blocked("the fixture is missing")

        result = run_one(blocked, ctx)
        assert result.status == WARN
        assert result.detail == "NOT RUN: the fixture is missing"
        assert result.seconds is None

    def test_not_run_outcome_has_no_timing(self, ctx):
        result = run_one(lambda c: not_run("opt-in"), ctx)
        assert (result.status, result.seconds) == (WARN, None)

    def test_executed_checks_are_timed(self, ctx):
        assert run_one(lambda c: passed(), ctx).seconds >= 0

    def test_pass_over_budget_becomes_warn_but_fail_stays_fail(self, ctx):
        import time

        def slow_pass(c):
            time.sleep(0.03)
            return passed("done")

        def slow_fail(c):
            time.sleep(0.03)
            return failed("bad")

        over = run_one(slow_pass, ctx, budget=0.001)
        assert over.status == WARN and "latency budget" in over.detail
        assert run_one(slow_pass, ctx, budget=5).status == PASS
        assert run_one(slow_fail, ctx, budget=0.001).status == FAIL

    def test_secrets_are_redacted_from_details(self, ctx):
        secret = "nvapi-super-secret-value-123"
        result = run_one(
            lambda c: failed(f"call failed with key {secret}"),
            ctx,
            secrets={"NIM_API_KEY": secret},
        )
        assert secret not in result.detail and "***" in result.detail

    def test_duplicate_and_unknown_group_registration_refused(self):
        snapshot = list(core._REGISTRY)
        try:
            with pytest.raises(ValueError):
                check("dup.one", "nope", "x")(lambda c: passed())
            check("dup.two", "app", "x")(lambda c: passed())
            with pytest.raises(ValueError):
                check("dup.two", "app", "x")(lambda c: passed())
        finally:
            core._REGISTRY[:] = snapshot


class TestReport:
    def _report(self, *outcomes: Outcome):
        checks = [
            Check(f"t.{i}", "app", f"check {i}", (lambda c, o=o: o))
            for i, o in enumerate(outcomes)
        ]
        return run_preflight(checks=checks)

    def test_warnings_alone_do_not_fail_the_run(self):
        report = self._report(passed(), warned("x"), not_run("y"))
        assert report.counts() == {PASS: 1, FAIL: 0, WARN: 2}
        assert report.exit_code == 0
        assert "WARN items were not verified" in report.render()

    def test_any_failure_exits_one(self):
        report = self._report(passed(), failed("nope"))
        assert report.exit_code == 1
        text = report.render()
        assert "FAILED (exit 1)" in text and "Failed: t.1" in text

    def test_all_pass(self):
        assert "all checks passed" in self._report(passed()).render()

    def test_render_always_states_its_limits(self):
        assert "not the whole of JARVIS" in self._report(passed()).render()

    def test_json_is_complete(self):
        data = self._report(passed("ok"), failed("bad")).to_dict()
        assert data["exit_code"] == 1
        assert data["counts"] == {PASS: 1, FAIL: 1, WARN: 0}
        assert [r["status"] for r in data["results"]] == [PASS, FAIL]
        json.dumps(data)

    def test_filters_by_group_and_id(self):
        checks = [
            Check("a.one", "app", "x", lambda c: passed()),
            Check("m.one", "media", "x", lambda c: passed()),
        ]
        assert [
            r.id for r in run_preflight(checks=checks, groups=["media"]).results
        ] == ["m.one"]
        assert [r.id for r in run_preflight(checks=checks, ids=["a.one"]).results] == [
            "a.one"
        ]
        with pytest.raises(ValueError):
            run_preflight(checks=checks, groups=["bogus"])
        with pytest.raises(ValueError):
            run_preflight(checks=checks, ids=["bogus"])

    def test_workdir_is_removed_afterwards(self):
        seen: List[Path] = []

        def remember(c):
            seen.append(c.workdir)
            (c.workdir / "scratch").write_text("x")
            return passed()

        run_preflight(checks=[Check("t.w", "app", "x", remember)])
        assert seen and not seen[0].exists()


class TestSecrets:
    def test_known_values_come_from_env_and_credential_files_without_changes(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setenv("SOME_SERVICE_API_KEY", "env-secret-value-1")
        monkeypatch.setenv("HARMLESS_SETTING", "not-a-secret-at-all")
        creds = tmp_path / "credentials.toml"
        creds.write_text('[web_search]\nTAVILY_API_KEY = "file-secret-value-2"\n')
        (tmp_path / "cloud-keys.env").write_text("OPENAI_API_KEY='file-secret-3'\n")
        before = creds.read_bytes()
        found = known_secret_values(tmp_path)
        assert found["SOME_SERVICE_API_KEY"] == "env-secret-value-1"
        assert found["TAVILY_API_KEY"] == "file-secret-value-2"
        assert found["OPENAI_API_KEY"] == "file-secret-3"
        assert "HARMLESS_SETTING" not in found
        assert creds.read_bytes() == before
        assert sorted(p.name for p in tmp_path.iterdir()) == [
            "cloud-keys.env",
            "credentials.toml",
        ]

    def test_malformed_credentials_are_not_moved_or_raised(self, tmp_path):
        bad = tmp_path / "credentials.toml"
        bad.write_text("this is [not toml")
        assert isinstance(known_secret_values(tmp_path), dict)
        assert bad.read_text() == "this is [not toml"

    def test_redact_longest_first(self):
        assert redact("a abcdef b", {"x": "abc", "y": "abcdef"}) == "a *** b"

    def test_temporary_env_restores(self, monkeypatch):
        monkeypatch.setenv("PF_KEEP", "old")
        monkeypatch.delenv("PF_NEW", raising=False)
        with temporary_env({"PF_KEEP": "tmp", "PF_NEW": "x"}):
            assert os.environ["PF_KEEP"] == "tmp" and os.environ["PF_NEW"] == "x"
        assert os.environ["PF_KEEP"] == "old" and "PF_NEW" not in os.environ


class TestNetworkGuard:
    def test_blocks_outbound_but_allows_loopback(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        accepted: List[Any] = []
        threading.Thread(
            target=lambda: accepted.append(server.accept()), daemon=True
        ).start()
        try:
            with brain_checks.no_outbound_network():
                with pytest.raises(brain_checks.NetworkBlocked):
                    socket.create_connection(("203.0.113.9", 443), timeout=1)
                socket.create_connection(("127.0.0.1", port), timeout=2).close()
            # The guard is removed again afterwards.
            assert socket.socket.connect.__name__ == "connect"
        finally:
            server.close()


# -- command line ---------------------------------------------------------------


class TestCommandLine:
    def test_list_shows_every_check(self, capsys):
        assert cli_main(["preflight", "--list"]) == 0
        out = capsys.readouterr().out
        for item in registered_checks():
            assert item.id in out

    def test_unknown_check_id_is_usage_error(self, capsys):
        assert cli_main(["preflight", "--only", "nope.nope"]) == 2
        assert "Unknown check id" in capsys.readouterr().err

    def test_exit_status_follows_failures(self, monkeypatch, capsys):
        good = [Check("t.g", "app", "good", lambda c: passed())]
        bad = good + [Check("t.b", "app", "bad", lambda c: failed("nope"))]
        monkeypatch.setattr(core, "registered_checks", lambda: good)
        assert cli_main(["preflight"]) == 0
        monkeypatch.setattr(core, "registered_checks", lambda: bad)
        assert cli_main(["preflight"]) == 1
        capsys.readouterr()
        assert cli_main(["preflight", "--json"]) == 1
        assert json.loads(capsys.readouterr().out)["counts"][FAIL] == 1

    def test_other_commands_still_parse(self, capsys):
        # media-inspect without configuration reports an error, not a crash.
        assert cli_main(["media-inspect", "--config", "/nonexistent.toml"]) == 1
        capsys.readouterr()


class TestBuiltInRegistry:
    def test_ids_unique_and_every_group_used(self):
        checks = registered_checks()
        ids = [c.id for c in checks]
        assert len(ids) == len(set(ids))
        assert {c.group for c in checks} == set(core.GROUPS)

    def test_registering_a_check_in_a_module_makes_it_runnable(self):
        snapshot = list(core._REGISTRY)
        try:

            @check("extension.example", "future", "an added check")
            def added(c):
                return passed("ran")

            report = run_preflight(ids=["extension.example"])
            assert [r.status for r in report.results] == [PASS]
        finally:
            core._REGISTRY[:] = snapshot


# -- planned features -----------------------------------------------------------


class TestPlannedFeatures:
    def test_missing_features_warn_and_are_not_run(self, ctx):
        for entry in future_checks.PLANNED:
            result = run_builtin(entry[0], ctx)
            assert result.status == WARN, entry
            assert "NOT IMPLEMENTED" in result.detail
            assert result.seconds is None

    def test_a_new_module_without_a_check_cannot_pass_silently(self, ctx, monkeypatch):
        monkeypatch.setattr(future_checks, "_module_exists", lambda name: True)
        result = run_builtin("future.wake_word", ctx)
        assert result.status == WARN
        assert "no integration check is registered" in result.detail

    def test_unavailable_voice_services_are_warnings_not_failures(self, ctx, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text('[jarvis.voicestudio]\nbase_url = "http://127.0.0.1:9"\n')
        ctx.config_path = config
        assert run_builtin("future.speech_output", ctx).status == WARN

    def test_cloud_voice_fallback_is_flagged_without_showing_the_key(
        self, ctx, monkeypatch
    ):
        for _, name in future_checks.CLOUD_TTS_KEYS:
            monkeypatch.delenv(name, raising=False)
        clean = run_builtin("future.speech_cloud_fallback", ctx)
        assert clean.status == PASS
        monkeypatch.setenv("OPENAI_API_KEY", "sk-test-not-real-0123456789")
        exposed = run_builtin("future.speech_cloud_fallback", ctx)
        assert exposed.status == PASS  # key present, cloud not enabled
        assert "openai_tts" in exposed.detail and "OPENAI_API_KEY" in exposed.detail
        assert "sk-test-not-real" not in exposed.detail

    def test_invalid_speech_config_is_a_failure(self, ctx, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text('[jarvis.voicestudio]\nbase_url = "https://example.com"\n')
        ctx.config_path = config
        assert run_builtin("future.speech_output", ctx).status == FAIL


# -- group A --------------------------------------------------------------------


class TestAppChecks:
    def _config(self, ctx: Context, tmp_path: Path, text: str) -> None:
        path = tmp_path / "config.toml"
        path.write_text(text)
        ctx.config_path = path

    def test_missing_config_file_warns(self, ctx, tmp_path):
        ctx.config_path = tmp_path / "absent.toml"
        result = run_builtin("app.config", ctx)
        assert result.status == WARN and "no config.toml" in result.detail

    def test_valid_config_passes(self, ctx, tmp_path):
        ws, out = tmp_path / "ws", tmp_path / "out"
        ws.mkdir(), out.mkdir()
        self._config(
            ctx,
            tmp_path,
            f'[jarvis.media_workspace]\nroot = "{ws}"\n'
            f'[jarvis.media_render]\noutput_root = "{out}"\n',
        )
        assert run_builtin("app.config", ctx).status == PASS

    def test_configured_but_missing_folder_warns(self, ctx, tmp_path):
        self._config(
            ctx, tmp_path, f'[jarvis.media_workspace]\nroot = "{tmp_path}/unplugged"\n'
        )
        result = run_builtin("app.config", ctx)
        assert result.status == WARN and "missing" in result.detail

    def test_invalid_section_fails_and_names_it(self, ctx, tmp_path):
        self._config(ctx, tmp_path, "[jarvis.jobs]\nmax_attempts = 9\n")
        result = run_builtin("app.config", ctx)
        assert result.status == FAIL and "[jarvis.jobs]" in result.detail

    def test_malformed_toml_fails(self, ctx, tmp_path):
        self._config(ctx, tmp_path, "this is [not toml")
        assert run_builtin("app.config", ctx).status == FAIL

    def test_rust_extension_missing_is_a_failure_with_the_fix(self, ctx, monkeypatch):
        real = app_checks.importlib.import_module

        def fake(name, *a, **k):
            if name == "openjarvis_rust":
                raise ImportError("no module")
            return real(name, *a, **k)

        monkeypatch.setattr(app_checks.importlib, "import_module", fake)
        result = run_builtin("app.rust_ext", ctx)
        assert result.status == FAIL and "maturin develop" in result.detail

    def test_rust_extension_present_passes(self, ctx, monkeypatch):
        import sys
        import types

        module = types.ModuleType("openjarvis_rust")
        module.__version__ = "9.9"
        monkeypatch.setitem(sys.modules, "openjarvis_rust", module)
        assert run_builtin("app.rust_ext", ctx).status == PASS

    def test_registration(self, ctx):
        assert run_builtin("app.registration", ctx).status == PASS

    def test_registration_fails_when_a_tool_is_missing(self, ctx, monkeypatch):
        from openjarvis.core.registry import ToolRegistry

        monkeypatch.setattr(jarvis_ext, "ensure_registered", lambda: None)
        ToolRegistry.clear() if hasattr(ToolRegistry, "clear") else None
        result = run_builtin("app.registration", ctx)
        assert result.status == FAIL and "not registered" in result.detail

    def test_server_starts(self, ctx):
        pytest.importorskip("fastapi")
        result = run_builtin("app.server", ctx)
        assert result.status == PASS, result.detail
        assert "stub engine" in result.detail

    def test_spawn_server_is_opt_in(self, ctx):
        result = run_builtin("app.spawn_server", ctx)
        assert result.status == WARN and result.detail.startswith("NOT RUN")
        assert result.seconds is None

    def test_clean_server_leaks_nothing(self, ctx):
        pytest.importorskip("fastapi")
        result = run_builtin("app.secrets_served", ctx)
        assert result.status == PASS, result.detail

    def test_leaking_server_is_detected_without_printing_the_value(
        self, ctx, monkeypatch
    ):
        fastapi = pytest.importorskip("fastapi")

        original = app_checks._build_app

        def build():
            app = original()
            router = fastapi.APIRouter()

            @router.get("/v1/settings/echo")
            def echo():
                return {"key": os.environ["OPENAI_API_KEY"]}

            app.include_router(router)
            # The SPA catch-all was registered first and would answer instead.
            app.router.routes.insert(0, app.router.routes.pop())
            return app

        monkeypatch.setattr(app_checks, "_build_app", build)
        result = run_builtin("app.secrets_served", ctx)
        assert result.status == FAIL
        assert "/v1/settings/echo" in result.detail
        assert "preflight-canary-" not in result.detail

    def test_frontend_scan_finds_secret_shapes_and_reports_only_the_file(
        self, ctx, tmp_path, monkeypatch
    ):
        root = tmp_path / "static"
        (root / "assets").mkdir(parents=True)
        fake_key = "sk-" + "a1B2c3D4e5" * 4
        (root / "assets" / "app.js").write_text(f'const k = "{fake_key}";')
        (root / "index.html").write_text("<html></html>")
        monkeypatch.setattr(app_checks, "_scan_roots", lambda: [("served build", root)])
        result = run_builtin("app.secrets_frontend", ctx)
        assert result.status == FAIL
        assert "assets/app.js" in result.detail and fake_key not in result.detail

    def test_frontend_scan_finds_the_configured_real_value(
        self, ctx, tmp_path, monkeypatch
    ):
        root = tmp_path / "static"
        root.mkdir()
        (root / "main.js").write_text('x="topsecretvalue-12345"')
        monkeypatch.setenv("MY_PROVIDER_API_KEY", "topsecretvalue-12345")
        monkeypatch.setattr(app_checks, "_scan_roots", lambda: [("served build", root)])
        result = run_builtin("app.secrets_frontend", ctx)
        assert result.status == FAIL and "MY_PROVIDER_API_KEY" in result.detail
        assert "topsecretvalue-12345" not in result.detail

    def test_frontend_scan_clean_ignores_tests_and_placeholders(
        self, ctx, tmp_path, monkeypatch
    ):
        root = tmp_path / "src"
        root.mkdir()
        (root / "x.ts").write_text('const hint = "sk-ant-..."; const p = "sk-...";')
        (root / "x.test.ts").write_text('const k = "sk-' + "z" * 40 + '";')
        monkeypatch.setattr(
            app_checks, "_scan_roots", lambda: [("frontend source", root)]
        )
        assert run_builtin("app.secrets_frontend", ctx).status == PASS

    def test_frontend_scan_with_nothing_to_scan_is_not_a_pass(self, ctx, monkeypatch):
        monkeypatch.setattr(app_checks, "_scan_roots", lambda: [])
        result = run_builtin("app.secrets_frontend", ctx)
        assert result.status == WARN and result.seconds is None


# -- group B ---------------------------------------------------------------------


class TestMediaChecks:
    def test_missing_ffmpeg_fails_and_blocks_the_rest(self, ctx, monkeypatch):
        monkeypatch.setattr(media_checks.shutil, "which", lambda name: None)
        result = run_builtin("media.ffmpeg", ctx)
        assert result.status == FAIL and "ffmpeg and ffprobe not found" in result.detail
        blocked = run_builtin("media.fixture", ctx)
        assert blocked.status == WARN and blocked.detail.startswith("NOT RUN")

    def test_unavailable_fixture_is_a_warning_never_a_pass(self, ctx, monkeypatch):
        ctx.shared["ffmpeg"] = ctx.shared["ffprobe"] = "/usr/bin/ffmpeg"

        def refuse(root, ffmpeg):
            raise FixtureUnavailable("no libx264")

        monkeypatch.setattr(media_checks, "build_fixture", refuse)
        assert run_builtin("media.fixture", ctx).status == WARN
        for check_id in ("media.inspect", "media.render", "media.validate"):
            result = run_builtin(check_id, ctx)
            assert result.status == WARN and result.detail.startswith("NOT RUN")
            assert result.seconds is None

    @needs_ffmpeg
    def test_media_group_passes_on_real_ffmpeg(self, tmp_path):
        report = run_preflight(groups=["media"])
        assert [r.status for r in report.results] == [PASS] * 5, report.render()

    @needs_ffmpeg
    def test_a_broken_render_is_a_failure(self, ctx, monkeypatch):
        from jarvis_ext.tools.media_render import MediaRenderTool
        from openjarvis.core.types import ToolResult

        def broken(self, **params):
            return ToolResult(
                tool_name="media_render",
                content=json.dumps({"render_status": "ffmpeg_failed", "error": "x"}),
                success=False,
            )

        monkeypatch.setattr(MediaRenderTool, "execute", broken)
        report = run_preflight(groups=["media"])
        statuses = {r.id: r.status for r in report.results}
        assert statuses["media.render"] == FAIL
        assert statuses["media.validate"] == WARN  # NOT RUN: nothing was rendered

    @needs_ffmpeg
    def test_a_render_that_alters_sources_is_detected(self, ctx, monkeypatch):
        from jarvis_ext.tools.media_render import MediaRenderTool

        original = MediaRenderTool.execute

        def tamper(self, **params):
            result = original(self, **params)
            clip = next(self._workspace.root.rglob("captions.srt"))
            clip.write_text("changed")
            return result

        monkeypatch.setattr(MediaRenderTool, "execute", tamper)
        result = next(
            r for r in run_preflight(groups=["media"]).results if r.id == "media.render"
        )
        assert result.status == FAIL and "source files" in result.detail


# -- group C ---------------------------------------------------------------------

LIFECYCLE_CHECKS = [
    "jobs.create_retrieve",
    "jobs.validation",
    "jobs.transitions",
    "jobs.failure_recorded",
    "jobs.retry",
    "jobs.cancel",
]


class TestJobChecks:
    @pytest.mark.parametrize("check_id", LIFECYCLE_CHECKS)
    def test_lifecycle_checks_pass_on_the_real_store(self, ctx, check_id):
        result = run_builtin(check_id, ctx)
        assert result.status == PASS, result.detail

    def test_the_real_database_is_never_touched(self, ctx, tmp_path, monkeypatch):
        monkeypatch.setenv("OPENJARVIS_HOME", str(tmp_path / "home"))
        for check_id in LIFECYCLE_CHECKS:
            run_builtin(check_id, ctx)
        assert not (tmp_path / "home").exists() or not list(
            (tmp_path / "home").rglob("jobs.db")
        )

    def test_no_false_success_with_fake_tools(self, ctx):
        result = run_builtin("jobs.no_false_success", ctx)
        assert result.status == PASS, result.detail

    # Mutation tests: break the job manager, require the check to notice.

    def test_validation_check_fails_if_invalid_jobs_are_accepted(
        self, ctx, monkeypatch
    ):
        monkeypatch.setattr("jarvis_ext.jobs.store.validate_kind", lambda kind: kind)
        monkeypatch.setattr(
            "jarvis_ext.jobs.store.encode_json_object", lambda value, *a, **k: "{}"
        )
        result = run_builtin("jobs.validation", ctx)
        assert result.status == FAIL and "accepted" in result.detail

    def test_failure_check_fails_if_failures_are_recorded_as_success(
        self, ctx, monkeypatch
    ):
        def always_succeed(store, job, worker_id, outcome, **kwargs):
            return store.complete(job.id, worker_id, {"forced": True})

        monkeypatch.setattr("jarvis_ext.jobs.runner.record_outcome", always_succeed)
        result = run_builtin("jobs.failure_recorded", ctx)
        assert result.status == FAIL and "expected failed" in result.detail

    def test_cancel_check_fails_if_cancel_does_nothing(self, ctx, monkeypatch):
        monkeypatch.setattr(JobStore, "cancel", lambda self, job_id: self.get(job_id))
        assert run_builtin("jobs.cancel", ctx).status == FAIL

    def test_transition_check_fails_if_final_jobs_can_change(self, ctx, monkeypatch):
        monkeypatch.setattr("jarvis_ext.jobs.store.check_transition", lambda a, b: None)
        assert run_builtin("jobs.transitions", ctx).status == FAIL

    def test_no_false_success_fails_if_the_handler_ignores_tool_failure(
        self, ctx, monkeypatch
    ):
        from jarvis_ext.video.render_job import VideoRenderHandler

        monkeypatch.setattr(
            VideoRenderHandler,
            "run",
            lambda self, context: JobOutcome.succeeded({"ready_for_review": True}),
        )
        result = run_builtin("jobs.no_false_success", ctx)
        assert result.status == FAIL and "SUCCEEDED" in result.detail

    def test_retry_check_fails_if_retries_never_stop(self, ctx, monkeypatch):
        monkeypatch.setattr("jarvis_ext.jobs.store.MAX_ATTEMPTS", 100, raising=False)

        def endless(self, job_id, worker_id, *args, **kwargs):
            return self.get(job_id)

        monkeypatch.setattr(JobStore, "fail", endless)
        assert run_builtin("jobs.retry", ctx).status == FAIL

    @needs_ffmpeg
    def test_groups_media_and_jobs_pass_together_on_real_ffmpeg(self):
        report = run_preflight(groups=["media", "jobs"])
        assert report.counts()[FAIL] == 0, report.render()
        assert report.counts()[WARN] == 0, report.render()
        assert report.counts()[PASS] == 5 + 8

    @needs_ffmpeg
    def test_jobs_group_alone_builds_its_own_fixture(self):
        report = run_preflight(ids=["jobs.real_pipeline"])
        assert report.results[0].status == PASS, report.render()

    @needs_ffmpeg
    def test_a_job_that_changes_sources_fails_the_pipeline_check(
        self, ctx, monkeypatch
    ):
        from jarvis_ext.video.render_job import VideoRenderHandler

        original = VideoRenderHandler.run

        def tamper(self, context):
            outcome = original(self, context)
            next(ctx.workdir.rglob("captions.srt")).write_text("changed")
            return outcome

        monkeypatch.setattr(VideoRenderHandler, "run", tamper)
        result = run_builtin("jobs.real_pipeline", ctx)
        assert result.status == FAIL and "source files" in result.detail

    def test_job_status_enum_is_what_the_checks_assume(self):
        assert {s.value for s in JobStatus} == {
            "queued",
            "running",
            "succeeded",
            "failed",
            "cancelled",
        }
        assert job_checks.FAKE == "preflight.fake"


# -- group D ---------------------------------------------------------------------


@pytest.fixture
def nim_brain(ctx, tmp_path, monkeypatch):
    """A configured hosted brain (NIM) with a fake key; nothing real is used."""
    from openjarvis.engine.nim import NIMEngine

    EngineRegistry.register_value("nim", NIMEngine)
    config = tmp_path / "config.toml"
    config.write_text(
        '[engine]\ndefault = "nim"\n'
        '[intelligence]\ndefault_model = "meta/llama-3.1-8b-instruct"\n'
    )
    ctx.config_path = config
    monkeypatch.setenv("NIM_API_KEY", "nvapi-fake-key-for-tests-123456")
    monkeypatch.delenv("NIM_HOST", raising=False)
    return ctx


class TestBrainChecks:
    def test_config_reports_the_selection_unchanged(self, nim_brain):
        result = run_builtin("brain.config", nim_brain)
        assert result.status == PASS
        assert "'nim'" in result.detail and "llama-3.1-8b" in result.detail

    def test_unknown_engine_fails(self, ctx, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text('[engine]\ndefault = "no-such-engine"\n')
        ctx.config_path = config
        result = run_builtin("brain.config", ctx)
        assert result.status == FAIL and "not a registered engine" in result.detail

    def test_no_model_configured_warns(self, ctx, tmp_path):
        from openjarvis.engine.nim import NIMEngine

        EngineRegistry.register_value("nim", NIMEngine)
        config = tmp_path / "config.toml"
        config.write_text('[engine]\ndefault = "nim"\n')
        ctx.config_path = config
        assert run_builtin("brain.config", ctx).status == WARN
        result = run_builtin("brain.request", ctx)
        assert result.status == WARN and result.seconds is None

    def test_credentials_present_without_showing_them(self, nim_brain):
        result = run_builtin("brain.credentials", nim_brain)
        assert result.status == PASS
        assert "nvapi-fake" not in result.detail

    def test_hosted_endpoint_without_a_key_fails(self, nim_brain, monkeypatch):
        monkeypatch.delenv("NIM_API_KEY")
        result = run_builtin("brain.credentials", nim_brain)
        assert result.status == FAIL and "NIM_API_KEY" in result.detail

    def test_self_hosted_endpoint_needs_no_key(self, nim_brain, monkeypatch):
        monkeypatch.delenv("NIM_API_KEY")
        monkeypatch.setenv("NIM_HOST", "http://127.0.0.1:8000")
        assert run_builtin("brain.credentials", nim_brain).status == PASS

    def test_request_is_built_offline_and_never_sent(self, nim_brain, monkeypatch):
        # If anything bypassed the interception, this would be reached.
        def must_not_connect(self, *a, **k):
            raise AssertionError("a real connection was attempted")

        monkeypatch.setattr(socket.socket, "connect", must_not_connect)
        result = run_builtin("brain.request", nim_brain)
        assert result.status == PASS, result.detail
        assert "integrate.api.nvidia.com/v1/chat/completions" in result.detail
        assert "credential=attached" in result.detail
        assert "nvapi-fake" not in result.detail
        assert "NOT a connectivity test" in result.detail

    def test_request_check_fails_when_the_engine_tries_to_leave_the_machine(
        self, nim_brain, monkeypatch
    ):
        # Bypass the interception by making the engine open a raw socket.
        def sneaky(self, messages, **kwargs):
            socket.create_connection(("203.0.113.9", 443), timeout=1)

        from openjarvis.engine.nim import NIMEngine

        monkeypatch.setattr(NIMEngine, "generate", sneaky)
        result = run_builtin("brain.request", nim_brain)
        assert result.status == FAIL and "leave the machine" in result.detail

    def test_request_check_fails_if_no_request_is_made(self, nim_brain, monkeypatch):
        from openjarvis.engine.nim import NIMEngine

        monkeypatch.setattr(
            NIMEngine, "generate", lambda self, messages, **kw: {"content": "x"}
        )
        result = run_builtin("brain.request", nim_brain)
        assert result.status == FAIL and "no HTTP request" in result.detail

    def test_request_check_fails_for_a_hosted_request_without_credentials(
        self, nim_brain, monkeypatch
    ):
        monkeypatch.setattr(brain_checks, "_api_key_env", lambda: {})
        monkeypatch.delenv("NIM_API_KEY")
        result = run_builtin("brain.request", nim_brain)
        assert result.status == FAIL and "no credential" in result.detail

    def test_live_test_is_opt_in_and_does_not_contact_the_provider(
        self, nim_brain, monkeypatch
    ):
        from openjarvis.engine.nim import NIMEngine

        def forbidden(self, *a, **k):
            raise AssertionError("the live provider call must be opt-in")

        monkeypatch.setattr(NIMEngine, "generate", forbidden)
        result = run_builtin("brain.live", nim_brain)
        assert result.status == WARN and result.seconds is None
        assert "NOT TESTED" in result.detail and "--live-brain" in result.detail
        # And the whole default run never calls it either.
        report = run_preflight(groups=["brain"], config_path=nim_brain.config_path)
        live = next(r for r in report.results if r.id == "brain.live")
        assert live.detail.startswith("NOT RUN")

    def test_live_test_makes_exactly_one_request_when_asked(
        self, nim_brain, monkeypatch
    ):
        calls: List[Any] = []

        class FakeEngine:
            def generate(self, messages, **kwargs):
                calls.append(kwargs)
                return {"content": "OK", "usage": {"total_tokens": 9}}

            def close(self):
                pass

        monkeypatch.setattr(brain_checks, "_build_engine", lambda c, k: FakeEngine())
        nim_brain.live_brain = True
        result = run_builtin("brain.live", nim_brain)
        assert result.status == PASS and len(calls) == 1
        assert calls[0]["max_tokens"] == 8
        assert "answered" in result.detail

    def test_live_failure_is_a_failure_and_redacts_the_key(
        self, nim_brain, monkeypatch
    ):
        class Boom:
            def generate(self, *a, **k):
                raise RuntimeError("401 for key nvapi-fake-key-for-tests-123456")

            def close(self):
                pass

        monkeypatch.setattr(brain_checks, "_build_engine", lambda c, k: Boom())
        nim_brain.live_brain = True
        report = run_preflight(
            ids=["brain.live"], config_path=nim_brain.config_path, live_brain=True
        )
        result = report.results[0]
        assert result.status == FAIL
        assert "nvapi-fake-key-for-tests-123456" not in result.detail


class TestBrainResolution:
    """The preflight resolves the brain like ``jarvis serve`` (CLI wins)."""

    def _config(self, ctx, tmp_path):
        from openjarvis.engine.cloud import CloudEngine
        from openjarvis.engine.ollama import OllamaEngine

        EngineRegistry.register_value("ollama", OllamaEngine)
        EngineRegistry.register_value("cloud", CloudEngine)
        path = tmp_path / "config.toml"
        path.write_text('[engine]\ndefault = "ollama"\n')
        ctx.config_path = path

    def test_config_default_without_override(self, ctx, tmp_path):
        self._config(ctx, tmp_path)
        result = run_builtin("brain.config", ctx)
        assert "engine 'ollama'" in result.detail
        assert "[engine] default" in result.detail
        assert "same local/cloud side" in result.detail

    def test_command_line_override_wins(self, ctx, tmp_path):
        self._config(ctx, tmp_path)
        ctx.engine_override = "cloud"
        ctx.model_override = "openrouter/some/model:free"
        result = run_builtin("brain.config", ctx)
        assert result.status == PASS
        assert "engine 'cloud'" in result.detail
        assert "--engine override" in result.detail
        assert "no fallback" in result.detail
        assert "openrouter/some/model:free" in result.detail
        assert ctx.shared["brain"] == {
            "engine": "cloud",
            "model": "openrouter/some/model:free",
        }

    def test_cli_options_reach_the_context(self):
        from jarvis_ext.preflight.core import run_preflight

        report = run_preflight(ids=["brain.live"], engine="cloud", model="m")
        assert report.results  # runs without a network call; options accepted


class TestFreePolicyCheck:
    def _ctx(self, ctx, tmp_path, extra=""):
        from openjarvis.engine.cloud import CloudEngine

        EngineRegistry.register_value("cloud", CloudEngine)
        path = tmp_path / "config.toml"
        path.write_text(f'[engine]\ndefault = "cloud"\n{extra}')
        ctx.config_path = path
        return ctx

    def test_passes_with_the_default_policy_and_sends_nothing(self, ctx, tmp_path):
        ctx = self._ctx(ctx, tmp_path)
        result = run_builtin("brain.free_policy", ctx)
        assert result.status == PASS, result.detail
        assert "refused before any client call" in result.detail
        assert "not been shown to use it" in result.detail  # no running-server claim

    def test_free_only_disabled_is_a_warning_not_a_pass(self, ctx, tmp_path):
        ctx = self._ctx(ctx, tmp_path, "[engine.openrouter]\nfree_only = false\n")
        assert run_builtin("brain.free_policy", ctx).status == WARN

    def test_empty_approved_list_fails(self, ctx, tmp_path):
        extra = "[engine.openrouter]\napproved_free_models = []\n"
        assert run_builtin("brain.free_policy", self._ctx(ctx, tmp_path, extra)).status == FAIL

    def test_unapproved_configured_model_fails(self, ctx, tmp_path):
        ctx = self._ctx(ctx, tmp_path)
        ctx.model_override = "openrouter/openai/gpt-4o"
        result = run_builtin("brain.free_policy", ctx)
        assert result.status == FAIL and "not in approved_free_models" in result.detail

    def test_broken_enforcement_is_detected(self, ctx, tmp_path, monkeypatch):
        from openjarvis.engine.cloud import CloudEngine

        ctx = self._ctx(ctx, tmp_path)
        monkeypatch.setattr(CloudEngine, "_enforce_model_policy", lambda *a, **k: None)
        monkeypatch.setattr(CloudEngine, "_openrouter_model_id", lambda s, m, e=None: m)
        result = run_builtin("brain.free_policy", ctx)
        assert result.status == FAIL
