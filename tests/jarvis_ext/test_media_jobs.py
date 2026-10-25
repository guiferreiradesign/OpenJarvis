"""Phase 3 step 7: the agent tools ``media_job_submit``, ``media_job_status``
and ``media_job_revise``.

What actually runs, by group:

* ``TestRegistration``, ``TestPermissions``, ``TestSubmit``, ``TestStatus``,
  ``TestRevise``: the tools against a real job store in ``tmp_path``; no
  render.
* ``TestWithWorker``: tools + the real worker and ``media_render`` tool with
  fake FFmpeg (``FakeProcess``/``FakeRunner``) and the integrity-checking
  fake validator from ``test_job_revisions``.
* ``TestRealFfmpegAgentTools``: tools + worker + real FFmpeg and the real
  validator on generated clips (skipped without FFmpeg).
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Dict

import pytest

import jarvis_ext
from jarvis_ext.jobs import JobSettings, JobStatus, JobStore, NotificationState
from jarvis_ext.jobs.worker import Worker
from jarvis_ext.tools.media_jobs import (
    QUEUE_CAPABILITIES,
    REVISE_TOOL,
    STATUS_CAPABILITIES,
    STATUS_TOOL,
    SUBMIT_TOOL,
    MediaJobReviseTool,
    MediaJobStatusTool,
    MediaJobSubmitTool,
)
from jarvis_ext.video.render_job import KIND, VideoRenderHandler
from openjarvis.core.registry import ToolRegistry
from openjarvis.core.types import ToolCall
from openjarvis.tools._stubs import ToolExecutor

from .test_job_revisions import IntegrityValidate, render_tool
from .video_fakes import HAVE_FFMPEG, FakeProcess, digest_tree, make_test_media

PLAN = {
    "output_name": "reel",
    "clips": [{"source": "Shoot/a.mp4", "start": 1, "end": 4}],
}
FAST = {
    "retry_delay_seconds": 0,
    "poll_seconds": 0.1,
    "heartbeat_seconds": 0.05,
    "stale_after_seconds": 1,
}
needs_ffmpeg = pytest.mark.skipif(
    not HAVE_FFMPEG, reason="ffmpeg/ffprobe not installed"
)


def write_config(tmp_path: Path, **sections: Dict[str, Any]) -> Path:
    jobs = {"database_path": str(tmp_path / "jobs.db"), **FAST}
    jobs.update(sections.pop("jobs", {}))
    lines = ["[jarvis.jobs]"] + [f"{k} = {json.dumps(v)}" for k, v in jobs.items()]
    for name, values in sections.items():
        lines.append(f"[jarvis.{name}]")
        lines += [f"{k} = {json.dumps(v)}" for k, v in values.items()]
    path = tmp_path / "config.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


@pytest.fixture
def config(tmp_path):
    return write_config(tmp_path)


def tools(config: Path):
    return (
        MediaJobSubmitTool(config_path=config),
        MediaJobStatusTool(config_path=config),
        MediaJobReviseTool(config_path=config),
    )


def call(tool, **params) -> Dict[str, Any]:
    result = tool.execute(**params)
    data = json.loads(result.content)
    assert result.success is (data["status"] == "ok"), data
    return data


def stored(tmp_path: Path):
    with JobStore(tmp_path / "jobs.db") as s:
        return s.list_jobs()


def finished_parent(tmp_path: Path, status=JobStatus.SUCCEEDED, plan=PLAN):
    with JobStore(tmp_path / "jobs.db") as s:
        job = s.submit(KIND, {"plan": plan})
        s.claim_next("w")
        if status is JobStatus.SUCCEEDED:
            return s.complete(job.id, "w", {"output": "reel_v001.mp4"})
        if status is JobStatus.FAILED:
            return s.fail(job.id, "w", "render_failed")
        return s.get(job.id)


# -- registration -------------------------------------------------------------


class TestRegistration:
    def test_tools_are_registered_and_discoverable(self):
        jarvis_ext.ensure_registered()
        jarvis_ext.ensure_registered()  # idempotent
        for name, cls in (
            (SUBMIT_TOOL, MediaJobSubmitTool),
            (STATUS_TOOL, MediaJobStatusTool),
            (REVISE_TOOL, MediaJobReviseTool),
        ):
            assert ToolRegistry.contains(name)
            assert isinstance(ToolRegistry.create(name), cls)

    def test_constructing_without_arguments_never_raises(self, tmp_path, monkeypatch):
        broken = tmp_path / "broken.toml"
        broken.write_text("[jarvis.jobs]\nworkers = 4\n")
        monkeypatch.setenv("OPENJARVIS_CONFIG", str(broken))
        for cls in (MediaJobSubmitTool, MediaJobStatusTool, MediaJobReviseTool):
            tool = cls()
            assert tool.to_openai_function()["function"]["name"] == tool.spec.name
            data = call(tool, job_id="abcdef", plan=PLAN)
            assert data["status"] == "config_error"
            assert "Unknown [jarvis.jobs] keys" in data["error"]
            assert str(tmp_path) not in json.dumps(data)

    def test_function_schemas(self, config):
        submit, status, revise = tools(config)
        fn = submit.to_openai_function()["function"]
        assert fn["parameters"]["required"] == ["plan"]
        assert set(fn["parameters"]["properties"]) == {"plan", "reuse"}
        fn = revise.to_openai_function()["function"]
        assert fn["parameters"]["required"] == ["job_id", "plan"]
        fn = status.to_openai_function()["function"]
        assert fn["parameters"]["required"] == []
        assert set(fn["parameters"]["properties"]) == {"job_id"}


# -- permissions --------------------------------------------------------------


def executor(tool, *grants: str) -> ToolExecutor:
    from openjarvis.security.capabilities import CapabilityPolicy

    policy = CapabilityPolicy(default_deny=True)
    for grant in grants:
        policy.grant("jarvis", grant)
    return ToolExecutor([tool], capability_policy=policy, agent_id="jarvis")


def tool_call(name: str, **params) -> ToolCall:
    return ToolCall(id="1", name=name, arguments=json.dumps(params))


class TestPermissions:
    def test_declared_capabilities(self, config):
        submit, status, revise = tools(config)
        assert submit.spec.required_capabilities == [
            "file:read",
            "file:write",
            "schedule:create",
        ]
        assert revise.spec.required_capabilities == QUEUE_CAPABILITIES
        assert status.spec.required_capabilities == ["file:read"] == STATUS_CAPABILITIES
        for tool in (submit, status, revise):
            assert tool.is_local is True
            assert "system:admin" not in tool.spec.required_capabilities
            assert "code:execute" not in tool.spec.required_capabilities
            assert "network:fetch" not in tool.spec.required_capabilities

    @pytest.mark.parametrize(
        "grants, missing",
        [
            ((), "file:read"),
            (("file:read",), "file:write"),
            (("file:read", "file:write"), "schedule:create"),
            (("schedule:create",), "file:read"),
        ],
    )
    def test_queueing_needs_every_capability(self, config, tmp_path, grants, missing):
        submit, _, revise = tools(config)
        result = executor(submit, *grants).execute(tool_call(SUBMIT_TOOL, plan=PLAN))
        assert not result.success and f"Capability '{missing}' denied" in result.content
        parent = finished_parent(tmp_path)
        result = executor(revise, *grants).execute(
            tool_call(REVISE_TOOL, job_id=parent.id, plan=PLAN)
        )
        assert not result.success and missing in result.content
        assert len(stored(tmp_path)) == 1  # only the parent

    def test_granted_queueing_works(self, config, tmp_path):
        submit, _, _ = tools(config)
        result = executor(submit, *QUEUE_CAPABILITIES).execute(
            tool_call(SUBMIT_TOOL, plan=PLAN)
        )
        assert result.success, result.content
        assert len(stored(tmp_path)) == 1

    def test_status_needs_only_file_read(self, config, tmp_path):
        _, status, _ = tools(config)
        job = finished_parent(tmp_path)
        denied = executor(status).execute(tool_call(STATUS_TOOL, job_id=job.id))
        assert not denied.success and "file:read" in denied.content
        allowed = executor(status, "file:read").execute(
            tool_call(STATUS_TOOL, job_id=job.id)
        )
        assert allowed.success, allowed.content

    def test_require_confirmation_applies_to_queueing(self, tmp_path):
        config = write_config(tmp_path, media_render={"require_confirmation": True})
        submit, status, revise = tools(config)
        assert submit.spec.requires_confirmation is True
        assert revise.spec.requires_confirmation is True
        assert status.spec.requires_confirmation is False
        result = ToolExecutor([submit]).execute(tool_call(SUBMIT_TOOL, plan=PLAN))
        assert not result.success and "confirmation" in result.content
        result = ToolExecutor(
            [submit], interactive=True, confirm_callback=lambda prompt: False
        ).execute(tool_call(SUBMIT_TOOL, plan=PLAN))
        assert not result.success and "denied" in result.content
        assert stored(tmp_path) == []
        result = ToolExecutor(
            [submit], interactive=True, confirm_callback=lambda prompt: True
        ).execute(tool_call(SUBMIT_TOOL, plan=PLAN))
        assert result.success, result.content


# -- submit -------------------------------------------------------------------


class TestSubmit:
    def test_submit_queues_a_validated_job(self, config, tmp_path):
        submit, _, _ = tools(config)
        data = call(submit, plan=PLAN)
        assert data["job_status"] == "queued" and data["kind"] == KIND
        assert data["output_name"] == "reel" and data["reuse"] is True
        assert data["parent_id"] is None
        assert "media_job_status" in data["next_step"]
        (job,) = stored(tmp_path)
        assert job.id == data["job_id"] and job.payload == {"plan": PLAN}
        assert job.status is JobStatus.QUEUED

    def test_plan_as_json_string_and_no_reuse(self, config, tmp_path):
        submit, _, _ = tools(config)
        data = call(submit, plan=json.dumps(PLAN), reuse=False)
        assert data["reuse"] is False
        assert stored(tmp_path)[0].payload == {"plan": PLAN, "reuse": False}

    @pytest.mark.parametrize(
        "params, status, message",
        [
            ({}, "invalid_plan", "complete edit plan"),
            ({"plan": []}, "invalid_plan", "complete edit plan"),
            ({"plan": {}}, "invalid_plan", "complete edit plan"),
            ({"plan": "{nope"}, "invalid_plan", "not valid JSON"),
            ({"plan": "x" * 300_000}, "invalid_plan", "too large"),
            ({"plan": {**PLAN, "clips": []}}, "invalid_plan", "Invalid plan"),
            ({"plan": {**PLAN, "fit": "stretch"}}, "invalid_plan", "Invalid plan"),
            ({"plan": {**PLAN, "output_name": "../x"}}, "invalid_plan", "Invalid"),
            ({"plan": {**PLAN, "extra": 1}}, "invalid_plan", "Unknown keys"),
            ({"plan": PLAN, "reuse": "no"}, "invalid_arguments", "reuse"),
        ],
    )
    def test_invalid_input_is_never_queued(
        self, config, tmp_path, params, status, message
    ):
        submit, _, _ = tools(config)
        data = call(submit, **params)
        assert data["status"] == status and message in data["error"]
        assert stored(tmp_path) == []

    @pytest.mark.parametrize(
        "change, label",
        [
            ({"clips": [{"source": "/Users/me/x.mp4", "start": 0, "end": 1}]},
             "clips[0].source"),
            ({"clips": [{"source": "../../Users/me/x.mp4", "start": 0, "end": 1}]},
             "clips[0].source"),
            ({"subtitles": {"source": "/Users/me/c.srt"}}, "subtitles.source"),
            ({"branding": {"asset": "../logo.png"}}, "branding.asset"),
        ],
    )  # fmt: skip
    def test_unsafe_paths_are_refused_before_queueing(
        self, config, tmp_path, change, label
    ):
        submit, _, _ = tools(config)
        data = call(submit, plan={**PLAN, **change})
        assert data["status"] == "invalid_plan"
        assert label in data["error"]
        assert "/Users/me" not in data["error"]
        assert stored(tmp_path) == []

    def test_store_errors_are_safe(self, tmp_path):
        missing = tmp_path / "no-such-folder" / "jobs.db"
        config = write_config(tmp_path, jobs={"database_path": str(missing)})
        submit, status, _ = tools(config)
        for data in (call(submit, plan=PLAN), call(status, job_id="abcdef")):
            assert data["status"] == "job_store_error"
            assert "folder does not exist" in data["error"]
            assert str(tmp_path) not in json.dumps(data)


# -- status -------------------------------------------------------------------


class TestStatus:
    def test_unknown_and_malformed_ids(self, config, tmp_path):
        finished_parent(tmp_path)
        _, status, _ = tools(config)
        assert call(status, job_id="abcdef")["status"] == "not_found"
        assert call(status, job_id="../etc")["status"] == "not_found"
        assert call(status, job_id="ab")["status"] == "not_found"
        for bad in (123, "", "   ", ["x"], {"a": 1}):
            assert call(status, job_id=bad)["status"] == "invalid_arguments"

    def test_ambiguous_prefix(self, config, tmp_path, monkeypatch):
        ids = iter(["abcdef" + "0" * 26, "abcdef" + "1" * 26])
        monkeypatch.setattr(
            "jarvis_ext.jobs.store.uuid.uuid4",
            lambda: uuid.UUID(hex=next(ids)),
        )
        with JobStore(tmp_path / "jobs.db") as s:
            s.submit(KIND, {"plan": PLAN})
            s.submit(KIND, {"plan": PLAN})
        _, status, _ = tools(config)
        data = call(status, job_id="abcdef")
        assert data["status"] == "ambiguous_job" and "more than one" in data["error"]
        assert call(status, job_id="abcdef0")["job"]["job_id"].startswith("abcdef0")

    def test_recent_jobs_without_id(self, config, tmp_path):
        finished_parent(tmp_path)
        _, status, _ = tools(config)
        (brief,) = call(status)["jobs"]
        assert brief["job_status"] == "succeeded"
        assert brief["output"] == "reel_v001.mp4"

    def test_failed_job_is_reported_without_paths_or_plan(self, config, tmp_path):
        secret_plan = {
            **PLAN,
            "clips": [{"source": "Private/client-x.mp4", "start": 0, "end": 1}],
        }
        with JobStore(tmp_path / "jobs.db") as s:
            job = s.submit(KIND, {"plan": secret_plan})
            s.claim_next("w")
            s.add_note(job.id, "w", "render started")
            s.fail(
                job.id,
                "w",
                "render_invalid_media",
                "ffprobe failed on /Users/me/Footage/raw.mov (token=abc)",
                {
                    "output": "/Users/me/out.mp4",
                    "render": {"failed_checks": ["duration"], "warnings": ["~/x a"]},
                },
            )
        _, status, _ = tools(config)
        data = call(status, job_id=job.id[:8])
        text = json.dumps(data)
        assert data["job"]["job_status"] == "failed"
        assert data["error"]["code"] == "render_invalid_media"
        assert "[path]" in data["error"]["message"]
        assert data["error"]["failed_checks"] == ["duration"]
        assert data["result"]["output"] is None  # an absolute name is withheld
        for leak in ("/Users/me", "~/x", "client-x", "Private/"):
            assert leak not in text
        assert data["notification"] == {
            "state": "pending",
            "outcome": "failed",
            "attempts": 0,
            "last_error": None,
        }
        assert [h["note"] for h in data["history"]][:2] == ["submitted", "claimed by w"]
        assert "media_job_revise" in data["next_step"]

    def test_status_is_read_only(self, config, tmp_path):
        job = finished_parent(tmp_path, JobStatus.RUNNING)
        with JobStore(tmp_path / "jobs.db") as s:
            history = s.events(job.id)
        _, status, _ = tools(config)
        for _ in range(3):
            assert call(status, job_id=job.id)["job"]["job_status"] == "running"
        with JobStore(tmp_path / "jobs.db") as s:
            assert s.get(job.id) == job
            assert s.events(job.id) == history
            assert s.notification(job.id) is None


# -- revise -------------------------------------------------------------------


class TestRevise:
    def test_revise_queues_a_child(self, config, tmp_path):
        parent = finished_parent(tmp_path)
        _, _, revise = tools(config)
        plan = {"clips": [{"source": "Shoot/a.mp4", "start": 0, "end": 2}]}
        data = call(revise, job_id=parent.id[:8], plan=plan, reuse=False)
        assert data["parent_id"] == parent.id and data["output_name"] == "reel"
        assert data["reuse"] is False
        with JobStore(tmp_path / "jobs.db") as s:
            child = s.get(data["job_id"])
            assert child.parent_id == parent.id
            assert child.payload == {
                "plan": {**plan, "output_name": "reel"},
                "reuse": False,
            }
            assert s.get(parent.id).result == {"output": "reel_v001.mp4"}

    @pytest.mark.parametrize(
        "status, params, code, message",
        [
            (JobStatus.RUNNING, {}, "not_revisable", "still running"),
            (JobStatus.SUCCEEDED, {"plan": {**PLAN, "output_name": "other"}},
             "invalid_plan", "keeps the output name"),
            (JobStatus.SUCCEEDED, {"plan": {**PLAN, "clips": []}},
             "invalid_plan", "Invalid plan"),
            (JobStatus.SUCCEEDED, {"plan": "nope"}, "invalid_plan", "not valid JSON"),
            (JobStatus.SUCCEEDED, {"job_id": "fedcba"}, "not_found", "No job"),
            (JobStatus.SUCCEEDED, {"job_id": 5}, "invalid_arguments", "job_id"),
            (JobStatus.SUCCEEDED, {"reuse": 1}, "invalid_arguments", "reuse"),
        ],
    )  # fmt: skip
    def test_refused_revisions_queue_nothing(
        self, config, tmp_path, status, params, code, message
    ):
        parent = finished_parent(tmp_path, status)
        _, _, revise = tools(config)
        data = call(revise, **{"job_id": parent.id, "plan": PLAN, **params})
        assert data["status"] == code and message in data["error"]
        assert [j.id for j in stored(tmp_path)] == [parent.id]

    def test_other_kind_is_not_revisable(self, config, tmp_path):
        with JobStore(tmp_path / "jobs.db") as s:
            job = s.submit("research.report", {"plan": PLAN})
            s.claim_next("w")
            s.complete(job.id, "w", {})
        _, _, revise = tools(config)
        data = call(revise, job_id=job.id, plan=PLAN)
        assert data["status"] == "not_revisable"


# -- with the worker (fake FFmpeg) ---------------------------------------------


@pytest.fixture
def media(tmp_path: Path):
    workspace = tmp_path / "media"
    (workspace / "Shoot").mkdir(parents=True)
    (workspace / "Shoot" / "a.mp4").write_bytes(b"source a")
    output = tmp_path / "renders"
    output.mkdir()
    return workspace, output


class TestWithWorker:
    def _work(self, tmp_path, media, process=None):
        _, output = media
        settings = JobSettings.from_mapping(
            {"database_path": str(tmp_path / "jobs.db"), **FAST}
        )
        h = VideoRenderHandler(
            render_tool=render_tool(media, process or FakeProcess()),
            validate_tool=IntegrityValidate(output),
        )
        with JobStore(tmp_path / "jobs.db") as store:
            return Worker(
                store, {KIND: h}, lock_path=settings.lock_file(), settings=settings
            ).run(once=True)

    def test_submit_status_revise_reuse(self, config, tmp_path, media):
        _, output = media
        submit, status, revise = tools(config)
        first = call(submit, plan=PLAN)["job_id"]
        assert call(status, job_id=first)["job"]["job_status"] == "queued"
        report = self._work(tmp_path, media)
        assert report["notifications"]["delivered"] == 1
        data = call(status, job_id=first)
        assert data["job"]["job_status"] == "succeeded"
        assert data["job"]["progress"] == 100.0
        assert data["result"]["output"] == "reel_v001.mp4"
        assert data["result"]["ready_for_review"] is True
        assert data["result"]["preview"].startswith("review/")
        assert data["notification"]["state"] == "delivered"
        assert data["error"] is None
        v1 = (output / "reel_v001.mp4").read_bytes()

        # A revision is the next version; v001 is untouched.
        revised = {**PLAN, "fit": "pad"}
        child = call(revise, job_id=first, plan=revised)["job_id"]
        self._work(tmp_path, media)
        data = call(status, job_id=child)
        assert data["job"]["parent_id"] == first
        assert data["result"]["output"] == "reel_v002.mp4"
        assert (output / "reel_v001.mp4").read_bytes() == v1

        # The same revision again is reused, and status says so.
        process = FakeProcess()
        again = call(revise, job_id=first, plan=revised)["job_id"]
        self._work(tmp_path, media, process)
        data = call(status, job_id=again)
        assert process.calls == []
        assert data["result"]["reused_from"] == child
        assert data["result"]["output"] == "reel_v002.mp4"
        assert any(
            h["note"].startswith("reusing reel_v002.mp4") for h in data["history"]
        )

        # no reuse: a fresh version.
        fresh = call(revise, job_id=first, plan=revised, reuse=False)["job_id"]
        self._work(tmp_path, media)
        assert call(status, job_id=fresh)["result"]["output"] == "reel_v003.mp4"
        assert sorted(p.name for p in output.glob("*.mp4")) == [
            "reel_v001.mp4",
            "reel_v002.mp4",
            "reel_v003.mp4",
        ]

    def test_tool_does_not_render_or_touch_files(self, config, tmp_path, media):
        workspace, output = media
        before = (digest_tree(workspace), digest_tree(output))
        submit, status, revise = tools(config)
        job = call(submit, plan=PLAN)["job_id"]
        call(status, job_id=job)
        assert (digest_tree(workspace), digest_tree(output)) == before

    def test_cancelled_job_status(self, config, tmp_path, media):
        submit, status, _ = tools(config)
        job = call(submit, plan=PLAN)["job_id"]
        with JobStore(tmp_path / "jobs.db") as s:
            s.cancel(job)
        report = self._work(tmp_path, media)
        assert report["processed"] == []
        data = call(status, job_id=job)
        assert data["job"]["job_status"] == "cancelled"
        assert data["notification"]["outcome"] == "cancelled"
        with sqlite3.connect(tmp_path / "jobs.db") as conn:
            (state,) = conn.execute(
                "SELECT state FROM job_notifications WHERE job_id = ?", (job,)
            ).fetchone()
        assert state == NotificationState.DELIVERED.value


# -- real FFmpeg ----------------------------------------------------------------


@needs_ffmpeg
class TestRealFfmpegAgentTools:
    def test_end_to_end(self, tmp_path):
        workspace = tmp_path / "workspace"
        output = tmp_path / "renders"
        output.mkdir()
        make_test_media(workspace, tmp_path / "assets")
        sources = digest_tree(workspace)
        config = write_config(
            tmp_path,
            media_workspace={"root": str(workspace)},
            media_render={"output_root": str(output)},
        )
        submit, status, revise = tools(config)
        plan = {
            "output_name": "agent",
            "clips": [{"source": "Shoot/landscape.mp4", "start": 0, "end": 2}],
            "video": {"x264_preset": "ultrafast"},
        }
        settings = JobSettings.load(config)

        def work():
            with JobStore(settings.database_file()) as store:
                Worker(
                    store,
                    {KIND: VideoRenderHandler(config_path=config)},
                    lock_path=settings.lock_file(),
                    settings=settings,
                ).run(once=True)

        first = call(submit, plan=plan)["job_id"]
        work()
        data = call(status, job_id=first)
        assert data["job"]["job_status"] == "succeeded", data
        assert data["result"]["ready_for_review"] is True
        assert (output / data["result"]["thumbnail"]).is_file()
        v1 = (output / "agent_v001.mp4").read_bytes()

        revised = {
            **plan,
            "clips": [{"source": "Shoot/landscape.mp4", "start": 1, "end": 3}],
        }
        child = call(revise, job_id=first, plan=revised)["job_id"]
        work()
        data = call(status, job_id=child)
        assert data["job"]["parent_id"] == first
        assert data["result"]["output"] == "agent_v002.mp4"
        assert data["result"]["ready_for_review"] is True
        assert (output / "agent_v001.mp4").read_bytes() == v1
        assert str(tmp_path) not in json.dumps(data)
        assert digest_tree(workspace) == sources
