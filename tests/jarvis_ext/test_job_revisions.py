"""Phase 3 step 6: revisions, verified output reuse, and resuming a job's
own published render.

What actually runs, by group:

* ``TestRevisionPayload``, ``TestReviseCli``: the job store and the CLI;
  no FFmpeg, no render.
* ``TestProvenanceRecord``, ``TestReuse``, ``TestResume``: the real
  ``media_render`` tool with fake FFmpeg/ffprobe (``FakeRunner`` /
  ``FakeProcess``), and ``IntegrityValidate``, a fake validator that does
  the integrity part of ``media_validate`` for real (record present,
  size and SHA-256 of the file match it) and invents review file names.
* ``TestRealFfmpegRevisions``: real FFmpeg and the real validator on
  generated clips in a temporary folder (skipped without FFmpeg).
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

from jarvis_ext.__main__ import main
from jarvis_ext.jobs import JobSettings, JobStatus, JobStore, NotificationState
from jarvis_ext.jobs.runner import (
    JobCancelled,
    WorkerInterrupted,
    run_next,
)
from jarvis_ext.jobs.worker import Worker
from jarvis_ext.tools._video_base import Refused, read_render_record
from jarvis_ext.video.output import OutputFolder
from jarvis_ext.video.provenance import KEY_VERSION, mismatch, record_provenance
from jarvis_ext.video.render_job import (
    KIND,
    RevisionError,
    VideoRenderHandler,
    make_payload,
    read_payload,
    revision_payload,
)
from openjarvis.core.types import ToolResult

from .video_fakes import (
    HAVE_FFMPEG,
    FakeProcess,
    FakeRunner,
    digest_tree,
    make_render_tool,
    make_test_media,
    probe_payload,
)

WORKER = "worker-1"
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


@pytest.fixture
def store(tmp_path: Path):
    with JobStore(tmp_path / "jobs.db") as s:
        yield s


@pytest.fixture
def dirs(tmp_path: Path):
    workspace = tmp_path / "media"
    (workspace / "Shoot").mkdir(parents=True)
    (workspace / "Shoot" / "a.mp4").write_bytes(b"source a")
    output = tmp_path / "renders"
    output.mkdir()
    return workspace, output


class VersionedRunner(FakeRunner):
    """FakeRunner reporting a chosen FFmpeg version."""

    def __init__(self, *args: Any, version: str = "9.9-test", **kw: Any) -> None:
        super().__init__(*args, **kw)
        self.version = version

    def __call__(self, cmd, **kwargs):
        if "-version" in cmd:
            out = f"ffmpeg version {self.version} Copyright\n".encode()
            return subprocess.CompletedProcess(cmd, 0, out, b"")
        return super().__call__(cmd, **kwargs)


def ffprobe(version: str = "9.9-test") -> FakeRunner:
    return VersionedRunner(
        {
            "a.mp4": probe_payload(duration=10.0),
            "b.mp4": probe_payload(duration=10.0),
            "render.mp4": probe_payload(width=1080, height=1920, duration=3.0),
        },
        version=version,
    )


def render_tool(dirs, process=None, *, version="9.9-test", **settings):
    workspace, output = dirs
    tool = make_render_tool(
        workspace,
        output,
        runner=ffprobe(version),
        process_runner=process or FakeProcess(),
        **settings,
    )
    tool.records_provenance = True
    return tool


class IntegrityValidate:
    """The integrity half of media_validate, for real; review names invented.

    ``behaviour`` may be an exception to raise, or ``"no_review"`` to pass
    without making review files.
    """

    def __init__(self, output: Path, behaviour: Any = None) -> None:
        self.folder = OutputFolder(output)
        self.behaviour = behaviour
        self.calls: List[str] = []

    def execute(self, **params: Any) -> ToolResult:
        name = params["output"]
        self.calls.append(name)
        if isinstance(self.behaviour, BaseException):
            raise self.behaviour
        try:
            record = read_render_record(self.folder, name)
            path = self.folder.resolve_child(name)
            data = path.read_bytes()
            ok = (
                len(data) == record["output"]["size_bytes"]
                and hashlib.sha256(data).hexdigest() == record["output"]["sha256"]
            )
        except (Refused, OSError, KeyError):
            ok = False
        n = len(self.calls)
        review = ok and self.behaviour != "no_review"
        report = {
            "validation_status": "passed" if ok else "failed",
            "ready_for_review": review,
            "output": name,
            "checks": [{"check": "matches_render_record", "passed": ok}],
            "preview": f"review/{name[:-4]}_preview_v{n:03d}.mp4" if review else None,
            "thumbnail": f"review/{name[:-4]}_thumb_v{n:03d}.jpg" if review else None,
            "warnings": [],
        }
        if ok and not review:
            report["validation_status"] = "passed_without_review_files"
        return ToolResult(tool_name="v", content=json.dumps(report), success=review)


def handler(dirs, process=None, validate=None, **kw) -> VideoRenderHandler:
    _, output = dirs
    return VideoRenderHandler(
        render_tool=render_tool(dirs, process, **kw),
        validate_tool=validate or IntegrityValidate(output),
    )


def run(store: JobStore, h: VideoRenderHandler):
    return run_next(store, WORKER, {KIND: h})


def renders(output: Path) -> List[str]:
    return sorted(p.name for p in output.glob("*.mp4"))


def notes(store: JobStore, job_id: str) -> List[str]:
    return [e.note for e in store.events(job_id)]


def finished_job(store, dirs, plan=PLAN, **kw):
    """Submit and run a job to completion with the fake pipeline."""
    job = store.submit(KIND, {"plan": plan})
    done = run(store, handler(dirs, **kw))
    assert done.id == job.id and done.status is JobStatus.SUCCEEDED, done.result
    return done


# -- revisions ----------------------------------------------------------------


class TestRevisionPayload:
    def _parent(self, store, status=JobStatus.SUCCEEDED, kind=KIND, plan=PLAN):
        job = store.submit(kind, {"plan": plan})
        if status is JobStatus.QUEUED:
            return job
        store.claim_next(WORKER)
        if status is JobStatus.RUNNING:
            return store.get(job.id)
        if status is JobStatus.SUCCEEDED:
            return store.complete(job.id, WORKER, {"output": "reel_v001.mp4"})
        if status is JobStatus.FAILED:
            return store.fail(job.id, WORKER, "render_failed")
        store.cancel(job.id)
        return store.confirm_cancelled(job.id, WORKER)

    @pytest.mark.parametrize(
        "status", [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED]
    )
    def test_finished_parent_gives_complete_payload(self, store, status):
        parent = self._parent(store, status)
        new_plan = {**PLAN, "fit": "pad"}
        assert revision_payload(parent, new_plan) == {"plan": new_plan}

    def test_output_name_is_filled_in(self, store):
        parent = self._parent(store)
        plan = {k: v for k, v in PLAN.items() if k != "output_name"}
        payload = revision_payload(parent, plan)
        assert payload["plan"]["output_name"] == "reel"
        assert "output_name" not in plan  # the caller's dict is not changed

    def test_no_reuse(self, store):
        payload = revision_payload(self._parent(store), PLAN, reuse=False)
        assert payload == {"plan": PLAN, "reuse": False}
        assert read_payload(payload) == (PLAN, "")

    @pytest.mark.parametrize(
        "status, message",
        [(JobStatus.QUEUED, "still queued"), (JobStatus.RUNNING, "still running")],
    )
    def test_unfinished_parent_is_refused(self, store, status, message):
        with pytest.raises(RevisionError, match=message):
            revision_payload(self._parent(store, status), PLAN)

    def test_other_kind_is_refused(self, store):
        parent = self._parent(store, kind="research.report")
        with pytest.raises(RevisionError, match="Only video.render"):
            revision_payload(parent, PLAN)

    @pytest.mark.parametrize(
        "plan, message",
        [
            ({**PLAN, "output_name": "other"}, "keeps the output name 'reel'"),
            ({**PLAN, "clips": []}, "Invalid plan"),
            ({**PLAN, "clips": [{"source": "a.mp4", "start": 5, "end": 1}]}, "Invalid"),
            ({**PLAN, "fit": "stretch"}, "Invalid plan"),
            ([PLAN], "complete EditPlan"),
            ({}, "complete EditPlan"),
        ],
    )
    def test_invalid_plan_is_refused(self, store, plan, message):
        with pytest.raises(RevisionError, match=message):
            revision_payload(self._parent(store), plan)

    def test_make_payload_checks_the_plan(self):
        assert make_payload(PLAN) == {"plan": PLAN}
        with pytest.raises(RevisionError, match="Invalid plan"):
            make_payload({**PLAN, "clips": []})


def write_config(tmp_path: Path, **sections: Dict[str, Any]) -> Path:
    jobs = {"database_path": str(tmp_path / "jobs.db"), **FAST}
    lines = ["[jarvis.jobs]"] + [f"{k} = {json.dumps(v)}" for k, v in jobs.items()]
    for name, values in sections.items():
        lines.append(f"[jarvis.{name}]")
        lines += [f"{k} = {json.dumps(v)}" for k, v in values.items()]
    path = tmp_path / "config.toml"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def cli(capsys, *args: str) -> tuple:
    code = main(["jobs", *args])
    out, err = capsys.readouterr()
    return code, out, err


class TestReviseCli:
    @pytest.fixture
    def config(self, tmp_path):
        return write_config(tmp_path)

    def _plan_file(self, tmp_path: Path, plan: Any, name: str = "plan.json") -> Path:
        path = tmp_path / name
        path.write_text(json.dumps(plan))
        return path

    def _finished_parent(self, tmp_path: Path) -> str:
        with JobStore(tmp_path / "jobs.db") as s:
            job = s.submit(KIND, {"plan": PLAN})
            s.claim_next(WORKER)
            s.complete(job.id, WORKER, {"output": "reel_v001.mp4"})
        return job.id

    def _count(self, tmp_path: Path) -> int:
        with JobStore(tmp_path / "jobs.db") as s:
            return len(s.list_jobs())

    def test_revise_queues_child(self, capsys, config, tmp_path):
        parent = self._finished_parent(tmp_path)
        plan = {"clips": [{"source": "Shoot/a.mp4", "start": 0, "end": 2}]}
        path = self._plan_file(tmp_path, plan)
        code, out, err = cli(
            capsys, "revise", parent[:8], str(path), "--config", str(config)
        )
        assert code == 0, err
        child = json.loads(out)
        assert child["parent_id"] == parent and child["status"] == "queued"
        assert child["id"] != parent
        with JobStore(tmp_path / "jobs.db") as s:
            stored = s.get(child["id"])
            assert stored.parent_id == parent and stored.kind == KIND
            assert stored.payload == {"plan": {**plan, "output_name": "reel"}}
            # The parent is untouched.
            assert s.get(parent).result == {"output": "reel_v001.mp4"}
            assert s.get(parent).status is JobStatus.SUCCEEDED

    def test_revise_no_reuse(self, capsys, config, tmp_path):
        parent = self._finished_parent(tmp_path)
        path = self._plan_file(tmp_path, PLAN)
        code, out, _ = cli(
            capsys, "revise", parent, str(path), "--no-reuse", "--config", str(config)
        )
        assert code == 0
        with JobStore(tmp_path / "jobs.db") as s:
            assert s.get(json.loads(out)["id"]).payload["reuse"] is False

    @pytest.mark.parametrize(
        "plan, message",
        [
            ({**PLAN, "clips": []}, "Invalid plan"),
            ({**PLAN, "output_name": "other"}, "keeps the output name"),
        ],
    )
    def test_invalid_revision_is_not_queued(
        self, capsys, config, tmp_path, plan, message
    ):
        parent = self._finished_parent(tmp_path)
        path = self._plan_file(tmp_path, plan)
        code, out, err = cli(
            capsys, "revise", parent, str(path), "--config", str(config)
        )
        assert code == 1 and message in err and out == ""
        assert self._count(tmp_path) == 1
        assert str(tmp_path) not in err

    def test_missing_parent(self, capsys, config, tmp_path):
        self._finished_parent(tmp_path)
        path = self._plan_file(tmp_path, PLAN)
        code, _, err = cli(
            capsys, "revise", "abcdef", str(path), "--config", str(config)
        )
        assert code == 1 and "No job" in err
        assert self._count(tmp_path) == 1

    def test_running_parent(self, capsys, config, tmp_path):
        with JobStore(tmp_path / "jobs.db") as s:
            job = s.submit(KIND, {"plan": PLAN})
            s.claim_next(WORKER)
        path = self._plan_file(tmp_path, PLAN)
        code, _, err = cli(capsys, "revise", job.id, str(path), "--config", str(config))
        assert code == 1 and "still running" in err
        assert self._count(tmp_path) == 1

    def test_bad_json(self, capsys, config, tmp_path):
        parent = self._finished_parent(tmp_path)
        path = tmp_path / "plan.json"
        path.write_text("{nope")
        code, _, err = cli(capsys, "revise", parent, str(path), "--config", str(config))
        assert code == 1 and "not valid JSON" in err

    def test_submit_rejects_invalid_plan_before_queueing(
        self, capsys, config, tmp_path
    ):
        path = self._plan_file(tmp_path, {**PLAN, "clips": []})
        code, _, err = cli(capsys, "submit", str(path), "--config", str(config))
        assert code == 1 and "Invalid plan" in err
        assert self._count(tmp_path) == 0

    def test_submit_no_reuse(self, capsys, config, tmp_path):
        path = self._plan_file(tmp_path, PLAN)
        code, out, _ = cli(
            capsys, "submit", str(path), "--no-reuse", "--config", str(config)
        )
        assert code == 0
        with JobStore(tmp_path / "jobs.db") as s:
            assert s.get(json.loads(out)["id"]).payload == {
                "plan": PLAN,
                "reuse": False,
            }

    def test_revised_job_renders_next_version(self, store, dirs):
        _, output = dirs
        parent = finished_job(store, dirs)
        v1 = (output / "reel_v001.mp4").read_bytes()
        child = store.submit(
            KIND,
            revision_payload(parent, {**PLAN, "fit": "pad"}),
            parent_id=parent.id,
        )
        done = run(store, handler(dirs))
        assert done.id == child.id and done.parent_id == parent.id
        assert done.status is JobStatus.SUCCEEDED
        assert done.result["output"] == "reel_v002.mp4"
        assert done.result.get("reused_from") is None
        assert (output / "reel_v001.mp4").read_bytes() == v1
        assert store.get(parent.id).result["output"] == "reel_v001.mp4"
        assert store.notification(child.id).outcome is JobStatus.SUCCEEDED
        assert store.notification(parent.id).state is NotificationState.PENDING

    def test_failed_revision_leaves_previous_output(self, store, dirs):
        _, output = dirs
        parent = finished_job(store, dirs)
        before = digest_tree(output)
        store.submit(
            KIND,
            revision_payload(parent, {**PLAN, "video": {"crf": 30}}),
            parent_id=parent.id,
        )
        done = run(store, handler(dirs, FakeProcess(2)))
        assert done.status is JobStatus.QUEUED  # ffmpeg_failed: one retry
        done = run(store, handler(dirs, FakeProcess(2)))
        assert done.status is JobStatus.FAILED
        assert digest_tree(output) == before
        assert store.notification(done.id).outcome is JobStatus.FAILED


# -- the provenance record ----------------------------------------------------


class TestProvenanceRecord:
    def test_job_render_records_provenance(self, store, dirs):
        _, output = dirs
        done = finished_job(store, dirs)
        record = json.loads((output / "reel_v001.render.json").read_text())
        block = record_provenance(record)
        assert block["job_id"] == done.id
        assert block["key_version"] == KEY_VERSION
        assert block["input_key"] == done.result["input_key"]
        assert block["plan_digest"] == done.result["plan_digest"]
        assert block["sources"] == [
            {
                "source": "Shoot/a.mp4",
                "size_bytes": 8,
                "sha256": hashlib.sha256(b"source a").hexdigest(),
            }
        ]
        assert str(dirs[0]) not in json.dumps(record)
        assert mismatch(record, job_id=done.id, input_key=block["input_key"]) is None

    def test_without_provenance_the_record_is_unchanged(self, dirs):
        _, output = dirs
        tool = render_tool(dirs)
        tool.records_provenance = False
        result = tool.execute(plan=PLAN, provenance={"job_id": "a" * 32})
        assert result.success
        record = json.loads((output / "reel_v001.render.json").read_text())
        assert "provenance" not in record
        assert "provenance" not in json.loads(result.content)

    def test_provenance_job_id_must_be_a_job_id(self, dirs):
        result = render_tool(dirs).execute(plan=PLAN, provenance={"job_id": "../x"})
        assert not result.success
        assert renders(dirs[1]) == []

    def test_source_changing_during_render_disables_reuse(self, store, dirs):
        workspace, output = dirs
        source = workspace / "Shoot" / "a.mp4"

        def touch_source(cmd, cwd):
            source.write_bytes(b"source A")  # same size, new content
            (cwd / "render.mp4").write_bytes(b"mp4")
            return 0

        store.submit(KIND, {"plan": PLAN})
        done = run(store, handler(dirs, FakeProcess(touch_source)))
        assert done.status is JobStatus.SUCCEEDED
        assert done.result["input_key"] is None
        record = json.loads((output / "reel_v001.render.json").read_text())
        assert record["provenance"]["input_key"] is None
        assert any("changed during the render" in w for w in record["warnings"])

    @pytest.mark.parametrize(
        "change, reason",
        [
            ({"job_id": "b" * 32}, "different job"),
            ({"input_key": "0" * 64}, "inputs differ"),
            ({"key_version": 99}, "no usable provenance"),
        ],
    )
    def test_mismatch_reasons(self, store, dirs, change, reason):
        done = finished_job(store, dirs)
        record = json.loads((dirs[1] / "reel_v001.render.json").read_text())
        record["provenance"].update(change)
        why = mismatch(record, job_id=done.id, input_key=done.result["input_key"])
        assert reason in why

    def test_checksum_mismatch_reason(self, store, dirs):
        done = finished_job(store, dirs)
        record = json.loads((dirs[1] / "reel_v001.render.json").read_text())
        why = mismatch(
            record, job_id=done.id, input_key=done.result["input_key"], sha256="0" * 64
        )
        assert "checksum" in why


# -- reuse --------------------------------------------------------------------


class TestReuse:
    def test_identical_job_reuses_without_rendering(self, store, dirs):
        _, output = dirs
        first = finished_job(store, dirs)
        process = FakeProcess()
        validate = IntegrityValidate(output)
        second = store.submit(KIND, {"plan": PLAN})
        done = run(store, handler(dirs, process, validate))
        assert done.id == second.id and done.status is JobStatus.SUCCEEDED
        assert process.calls == []  # nothing rendered
        assert validate.calls == ["reel_v001.mp4"]  # but validated again
        assert done.result["output"] == "reel_v001.mp4"
        assert done.result["reused_from"] == first.id
        assert done.result["ready_for_review"] is True
        assert done.result["validate"]["preview"].startswith("review/reel_v001_")
        assert done.progress == 100.0
        assert renders(output) == ["reel_v001.mp4"]
        history = notes(store, done.id)
        assert f"reusing reel_v001.mp4 from job {first.id[:8]}" in history
        assert "validated reel_v001.mp4" in history
        assert "render started" not in history
        assert store.notification(done.id).outcome is JobStatus.SUCCEEDED

    def test_reuse_of_a_reused_job_points_at_the_original(self, store, dirs):
        first = finished_job(store, dirs)
        store.submit(KIND, {"plan": PLAN})
        run(store, handler(dirs))
        third = store.submit(KIND, {"plan": PLAN})
        done = run(store, handler(dirs, FakeProcess()))
        assert done.id == third.id and done.result["reused_from"] == first.id

    def test_equivalent_spellings_of_a_plan_are_reused(self, store, dirs):
        finished_job(store, dirs)
        spelled = {
            "output_name": "reel",
            "clips": [{"source": "Shoot/a.mp4", "start": "00:00:01", "end": 4.0}],
            "preset": "instagram_reel",
        }
        store.submit(KIND, {"plan": spelled})
        process = FakeProcess()
        done = run(store, handler(dirs, process))
        assert done.result.get("reused_from") and process.calls == []

    @pytest.mark.parametrize(
        "change",
        [
            "plan",
            "preset",
            "ffmpeg_version",
            "source",
            "output_modified",
            "output_deleted",
            "record_deleted",
            "record_other_job",
            "record_checksum",
            "no_reuse",
            "validation_fails",
            "no_review_files",
        ],
    )
    def test_reuse_refused_falls_back_to_a_fresh_render(self, store, dirs, change):
        workspace, output = dirs
        presets = {"reel_hd": {"width": 1080, "height": 1920, "frame_rate": 30}}
        plan = {**PLAN, "preset": "reel_hd"}
        first = finished_job(store, dirs, plan, presets=presets)
        record_path = output / "reel_v001.render.json"
        record = json.loads(record_path.read_text())
        kw: Dict[str, Any] = {"presets": presets}
        payload: Dict[str, Any] = {"plan": plan}
        validate = IntegrityValidate(output)
        if change == "plan":
            payload["plan"] = {**plan, "video": {"crf": 22}}
        elif change == "preset":
            # Same plan digest, different preset definition.
            kw["presets"] = {
                "reel_hd": {**presets["reel_hd"], "max_duration_seconds": 90}
            }
        elif change == "ffmpeg_version":
            kw["version"] = "10.0-test"
        elif change == "source":
            (workspace / "Shoot" / "a.mp4").write_bytes(b"source b")
        elif change == "output_modified":
            with open(output / "reel_v001.mp4", "ab") as fh:
                fh.write(b"!")
        elif change == "output_deleted":
            os.unlink(output / "reel_v001.mp4")
        elif change == "record_deleted":
            os.unlink(record_path)
        elif change == "record_other_job":
            record["provenance"]["job_id"] = "f" * 32
            record_path.write_text(json.dumps(record))
        elif change == "record_checksum":
            record["output"]["sha256"] = "0" * 64
            record_path.write_text(json.dumps(record))
        elif change == "no_reuse":
            payload["reuse"] = False
        elif change == "validation_fails":
            # The file still matches its record, but validation (decode,
            # probe) fails now.
            def fail_once(**params):
                validate.behaviour = None
                return ToolResult(
                    tool_name="v",
                    content=json.dumps(
                        {"validation_status": "failed", "ready_for_review": False,
                         "output": params["output"]}
                    ),
                    success=False,
                )  # fmt: skip

            original = validate.execute
            validate.execute = lambda **p: (
                fail_once(**p) if p["output"] == "reel_v001.mp4" else original(**p)
            )
        elif change == "no_review_files":
            validate.behaviour = "no_review"
        process = FakeProcess()
        second = store.submit(KIND, payload)
        done = run(store, handler(dirs, process, validate, **kw))
        assert done.id == second.id
        assert done.result.get("reused_from") is None
        assert len(process.calls) == 1  # rendered afresh
        assert done.result["output"] == "reel_v002.mp4"
        if change != "no_review_files":
            assert done.status is JobStatus.SUCCEEDED, done.result
            assert done.result["ready_for_review"] is True
        # The earlier job's record of its output is unchanged.
        assert store.get(first.id).result["output"] == "reel_v001.mp4"

    def test_fallback_render_progress_starts_from_zero(self, store, dirs):
        """A failed reuse check must not leave the fresh render at 95 %."""
        _, output = dirs
        finished_job(store, dirs)
        validate = IntegrityValidate(output, "no_review")
        job = store.submit(KIND, {"plan": PLAN})
        seen: List[Any] = []

        def render(cmd, cwd):
            current = store.get(job.id)
            seen.append((current.stage, current.progress))
            validate.behaviour = None
            (cwd / "render.mp4").write_bytes(b"mp4")
            return 0

        done = run(store, handler(dirs, FakeProcess(render), validate))
        assert done.status is JobStatus.SUCCEEDED and done.progress == 100.0
        assert done.result.get("reused_from") is None
        assert seen == [("render", 0.0)]
        stages = [n for n in notes(store, job.id) if n.startswith("stage ")]
        assert stages == ["stage validate", "stage render", "stage validate"]

    def test_successful_reuse_progress(self, store, dirs):
        finished_job(store, dirs)
        job = store.submit(KIND, {"plan": PLAN})
        done = run(store, handler(dirs))
        assert done.id == job.id and done.result["reused_from"]
        assert done.progress == 100.0
        assert [n for n in notes(store, job.id) if n.startswith("stage ")] == [
            "stage validate"
        ]

    def test_failed_and_cancelled_jobs_are_never_reused(self, store, dirs):
        _, output = dirs
        first = finished_job(store, dirs)
        # Make the only candidate look failed: a failed job keeps its result.
        with sqlite3.connect(store_path(store)) as conn:
            conn.execute("UPDATE jobs SET status = 'failed' WHERE id = ?", (first.id,))
        process = FakeProcess()
        store.submit(KIND, {"plan": PLAN})
        done = run(store, handler(dirs, process))
        assert done.result.get("reused_from") is None and len(process.calls) == 1
        with sqlite3.connect(store_path(store)) as conn:
            conn.execute(
                "UPDATE jobs SET status = 'cancelled' WHERE id != ?", (first.id,)
            )
            conn.execute(
                "UPDATE jobs SET status = 'cancelled' WHERE id = ?", (first.id,)
            )
        store.submit(KIND, {"plan": PLAN})
        done = run(store, handler(dirs, process))
        assert done.result.get("reused_from") is None and len(process.calls) == 2
        assert renders(output) == ["reel_v001.mp4", "reel_v002.mp4", "reel_v003.mp4"]

    def test_unvalidated_success_is_not_reused(self, store, dirs):
        first = finished_job(store, dirs)
        result = {**first.result, "ready_for_review": False}
        with sqlite3.connect(store_path(store)) as conn:
            conn.execute(
                "UPDATE jobs SET result = ? WHERE id = ?",
                (json.dumps(result), first.id),
            )
        process = FakeProcess()
        store.submit(KIND, {"plan": PLAN})
        done = run(store, handler(dirs, process))
        assert done.result.get("reused_from") is None and len(process.calls) == 1

    def test_legacy_result_without_provenance_is_not_reused(self, store, dirs):
        first = finished_job(store, dirs)
        result = {k: v for k, v in first.result.items() if k != "input_key"}
        with sqlite3.connect(store_path(store)) as conn:
            conn.execute(
                "UPDATE jobs SET result = ? WHERE id = ?",
                (json.dumps(result), first.id),
            )
        process = FakeProcess()
        store.submit(KIND, {"plan": PLAN})
        done = run(store, handler(dirs, process))
        assert done.result.get("reused_from") is None and len(process.calls) == 1

    def test_cancel_during_reuse_validation(self, store, dirs):
        _, output = dirs
        finished_job(store, dirs)
        job = store.submit(KIND, {"plan": PLAN})

        def cancelled(**params):
            store.cancel(job.id)
            raise JobCancelled()

        validate = IntegrityValidate(output)
        validate.execute = cancelled
        done = run(store, handler(dirs, FakeProcess(), validate))
        assert done.status is JobStatus.CANCELLED
        assert done.result["output"] is None
        assert store.notification(job.id).outcome is JobStatus.CANCELLED

    def test_shutdown_during_reuse_validation_requeues(self, store, dirs):
        finished_job(store, dirs)
        job = store.submit(KIND, {"plan": PLAN})
        validate = IntegrityValidate(dirs[1], WorkerInterrupted())
        done = run(store, handler(dirs, FakeProcess(), validate))
        assert done.id == job.id and done.status is JobStatus.QUEUED
        assert done.error_code == "interrupted"

    def test_fake_tools_without_reuse_support_render_as_before(self, store, dirs):
        finished_job(store, dirs)

        class Plain:
            def __init__(self, tool):
                self.tool = tool

            def execute(self, **params):
                return self.tool.execute(**params)

        process = FakeProcess()
        h = VideoRenderHandler(
            render_tool=Plain(render_tool(dirs, process)),
            validate_tool=IntegrityValidate(dirs[1]),
        )
        store.submit(KIND, {"plan": PLAN})
        done = run(store, h)
        assert done.result["output"] == "reel_v002.mp4" and len(process.calls) == 1


def store_path(store: JobStore) -> str:
    return store._conn.execute("PRAGMA database_list").fetchone()[2]


# -- resuming this job's own published render ---------------------------------


def _attempt_one_published(store, dirs, *, note: bool, provenance: bool = True):
    """Attempt 1 claimed the job and published a render, then the worker
    died (before writing ``rendered ...`` unless *note*)."""
    job = store.submit(KIND, {"plan": PLAN})
    claimed = store.claim_next("dead-worker")
    store.add_note(job.id, "dead-worker", "render started")
    tool = render_tool(dirs)
    tool.records_provenance = provenance
    report = json.loads(tool.execute(plan=PLAN, provenance={"job_id": job.id}).content)
    assert report["output"]["file"] == "reel_v001.mp4"
    if note:
        store.add_note(job.id, "dead-worker", "rendered reel_v001.mp4")
    return claimed


def _recover(store, h, job):
    requeue, code, message, result = h.recovery(job, store.events(job.id))
    return store.recover(
        job.id,
        stale_before="9999",
        requeue=requeue,
        error_code=code,
        error_message=message,
        result=result,
    )


class TestResume:
    @pytest.mark.parametrize("note", [False, True])
    def test_resume_validates_without_rendering(self, store, dirs, note):
        _, output = dirs
        job = _attempt_one_published(store, dirs, note=note)
        process = FakeProcess()
        validate = IntegrityValidate(output)
        h = handler(dirs, process, validate)
        queued = _recover(store, h, job)
        assert queued.status is JobStatus.QUEUED
        assert queued.error_code == ("validate_interrupted" if note else "interrupted")
        done = run(store, h)
        assert done.id == job.id and done.status is JobStatus.SUCCEEDED
        assert done.attempts == 2
        assert process.calls == []
        assert validate.calls == ["reel_v001.mp4"]
        assert done.result["resumed"] is True
        assert done.result["output"] == "reel_v001.mp4"
        assert done.result["render"]["output"]["file"] == "reel_v001.mp4"
        assert renders(output) == ["reel_v001.mp4"]
        assert "resuming reel_v001.mp4: published by an earlier attempt" in notes(
            store, job.id
        )
        assert done.progress == 100.0
        assert store.notification(job.id).outcome is JobStatus.SUCCEEDED

    def test_record_without_provenance_is_not_resumed(self, store, dirs):
        _, output = dirs
        job = _attempt_one_published(store, dirs, note=True, provenance=False)
        process = FakeProcess()
        h = handler(dirs, process)
        _recover(store, h, job)
        done = run(store, h)
        assert done.status is JobStatus.FAILED
        assert done.error_code == "validate_interrupted"
        assert "does not identify this job" in done.error_message
        assert process.calls == [] and renders(output) == ["reel_v001.mp4"]

    def test_same_name_is_not_enough(self, store, dirs):
        """A render of the same output name made by another job is never
        taken as this job's."""
        _, output = dirs
        other = finished_job(store, dirs)
        job = store.submit(KIND, {"plan": PLAN, "reuse": False})
        claimed = store.claim_next("dead-worker")
        store.add_note(job.id, "dead-worker", "render started")
        process = FakeProcess()
        h = handler(dirs, process)
        _recover(store, h, claimed)
        done = run(store, h)
        assert done.result.get("resumed") is None
        assert done.result["output"] == "reel_v002.mp4" and len(process.calls) == 1
        assert store.get(other.id).result["output"] == "reel_v001.mp4"

    def test_changed_inputs_render_afresh(self, store, dirs):
        workspace, output = dirs
        job = _attempt_one_published(store, dirs, note=True)
        (workspace / "Shoot" / "a.mp4").write_bytes(b"source c")
        process = FakeProcess()
        h = handler(dirs, process)
        _recover(store, h, job)
        done = run(store, h)
        assert done.status is JobStatus.SUCCEEDED
        assert done.result["output"] == "reel_v002.mp4" and len(process.calls) == 1
        assert any(n.startswith("not resuming reel_v001.mp4: its inputs differ")
                   for n in notes(store, job.id))  # fmt: skip

    def test_corrupted_own_render_fails_without_rendering(self, store, dirs):
        _, output = dirs
        job = _attempt_one_published(store, dirs, note=True)
        with open(output / "reel_v001.mp4", "ab") as fh:
            fh.write(b"corrupt")
        process = FakeProcess()
        h = handler(dirs, process)
        _recover(store, h, job)
        done = run(store, h)
        assert done.status is JobStatus.FAILED
        assert done.error_code == "validate_failed"
        assert done.result["ready_for_review"] is False
        assert process.calls == [] and renders(output) == ["reel_v001.mp4"]

    def test_two_renders_naming_the_job_are_ambiguous(self, store, dirs):
        _, output = dirs
        job = _attempt_one_published(store, dirs, note=False)
        render_tool(dirs).execute(plan=PLAN, provenance={"job_id": job.id})
        process = FakeProcess()
        h = handler(dirs, process)
        _recover(store, h, job)
        done = run(store, h)
        assert done.status is JobStatus.FAILED
        assert done.error_code == "resume_ambiguous"
        assert process.calls == []
        assert renders(output) == ["reel_v001.mp4", "reel_v002.mp4"]

    def test_interrupted_validation_is_resumed_next_attempt(self, store, dirs):
        _, output = dirs
        job = store.submit(KIND, {"plan": PLAN})
        process = FakeProcess()
        h = handler(dirs, process, IntegrityValidate(output, WorkerInterrupted()))
        first = run(store, h)
        assert first.status is JobStatus.QUEUED
        assert first.error_code == "validate_interrupted"
        assert "without rendering again" in first.error_message
        validate = IntegrityValidate(output)
        done = run(store, handler(dirs, process, validate))
        assert done.id == job.id and done.status is JobStatus.SUCCEEDED
        assert done.result["resumed"] is True and len(process.calls) == 1
        assert renders(output) == ["reel_v001.mp4"]

    def test_interrupted_validation_on_last_attempt_fails(self, store, dirs):
        _, output = dirs
        store.submit(KIND, {"plan": PLAN}, max_attempts=1)
        h = handler(dirs, None, IntegrityValidate(output, WorkerInterrupted()))
        done = run(store, h)
        assert done.status is JobStatus.FAILED
        assert done.error_code == "validate_interrupted"
        assert done.result["output"] == "reel_v001.mp4"
        assert done.result["ready_for_review"] is False

    def test_cancel_while_resuming(self, store, dirs):
        _, output = dirs
        job = _attempt_one_published(store, dirs, note=True)
        h = handler(dirs)
        _recover(store, h, job)
        validate = IntegrityValidate(output)

        def cancelled(**params):
            store.cancel(job.id)
            raise JobCancelled()

        validate.execute = cancelled
        done = run(store, handler(dirs, FakeProcess(), validate))
        assert done.status is JobStatus.CANCELLED
        assert done.result["output"] == "reel_v001.mp4"
        assert "not validated" in done.result["note"]
        assert renders(output) == ["reel_v001.mp4"]

    def test_recovery_decision_keeps_old_behaviour_for_plain_tools(self, store):
        class Plain:
            def execute(self, **params):  # pragma: no cover - not called
                raise AssertionError

        h = VideoRenderHandler(render_tool=Plain(), validate_tool=Plain())
        job = store.submit(KIND, {"plan": PLAN})
        store.claim_next("dead-worker")
        store.add_note(job.id, "dead-worker", "rendered reel_v001.mp4")
        requeue, code, _, _ = h.recovery(job, store.events(job.id))
        assert (requeue, code) == (False, "validate_interrupted")


# -- real FFmpeg --------------------------------------------------------------


@needs_ffmpeg
class TestRealFfmpegRevisions:
    @pytest.fixture
    def env(self, tmp_path):
        workspace = tmp_path / "workspace"
        output = tmp_path / "renders"
        output.mkdir()
        make_test_media(workspace, tmp_path / "assets")
        config = write_config(
            tmp_path,
            media_workspace={"root": str(workspace)},
            media_render={"output_root": str(output)},
        )
        return workspace, output, config

    PLAN = {
        "output_name": "rev",
        "clips": [{"source": "Shoot/landscape.mp4", "start": 0, "end": 2}],
        "video": {"x264_preset": "ultrafast"},
    }

    def _show(self, capsys, config, job_id):
        code, out, _ = cli(capsys, "show", job_id, "--config", str(config))
        assert code == 0
        return json.loads(out)

    def _work(self, capsys, config):
        code, _, err = cli(capsys, "worker", "--once", "--config", str(config))
        assert code == 0, err

    def test_revise_reuse_and_corruption_fallback(self, capsys, env, tmp_path):
        workspace, output, config = env
        sources = digest_tree(workspace)
        plan = tmp_path / "plan.json"
        plan.write_text(json.dumps(self.PLAN))
        _, out, _ = cli(capsys, "submit", str(plan), "--config", str(config))
        parent = json.loads(out)["id"]
        self._work(capsys, config)
        v1 = (output / "rev_v001.mp4").read_bytes()

        # A revision with a different plan becomes v002; v001 is untouched.
        revised = {k: v for k, v in self.PLAN.items() if k != "output_name"}
        revised["clips"] = [{"source": "Shoot/landscape.mp4", "start": 1, "end": 3}]
        path = tmp_path / "revised.json"
        path.write_text(json.dumps(revised))
        code, out, err = cli(
            capsys, "revise", parent, str(path), "--config", str(config)
        )
        assert code == 0, err
        child = json.loads(out)["id"]
        self._work(capsys, config)
        shown = self._show(capsys, config, child)
        assert shown["job"]["parent_id"] == parent
        assert shown["job"]["status"] == "succeeded"
        assert shown["job"]["result"]["output"] == "rev_v002.mp4"
        assert shown["notification"]["outcome"] == "succeeded"
        assert (output / "rev_v001.mp4").read_bytes() == v1

        # The same revision again is reused (real validation, no render).
        _, out, _ = cli(capsys, "revise", parent, str(path), "--config", str(config))
        again = json.loads(out)["id"]
        self._work(capsys, config)
        result = self._show(capsys, config, again)["job"]["result"]
        assert result["output"] == "rev_v002.mp4" and result["reused_from"] == child
        assert result["ready_for_review"] is True
        assert (output / result["validate"]["thumbnail"]).is_file()

        # Corrupt v002 in place: the next identical job must not reuse it.
        data = bytearray((output / "rev_v002.mp4").read_bytes())
        data[len(data) // 2] ^= 0xFF
        (output / "rev_v002.mp4").write_bytes(bytes(data))
        _, out, _ = cli(capsys, "revise", parent, str(path), "--config", str(config))
        third = json.loads(out)["id"]
        self._work(capsys, config)
        shown = self._show(capsys, config, third)
        assert shown["job"]["result"]["output"] == "rev_v003.mp4"
        assert shown["job"]["result"].get("reused_from") is None
        assert shown["job"]["result"]["ready_for_review"] is True
        assert any("not reusing rev_v002.mp4" in e["note"] for e in shown["events"])
        assert sorted(p.name for p in output.glob("*.mp4")) == [
            "rev_v001.mp4",
            "rev_v002.mp4",
            "rev_v003.mp4",
        ]
        assert digest_tree(workspace) == sources

    def test_resume_after_publish_with_real_validation(self, capsys, env):
        workspace, output, config = env
        settings = JobSettings.load(config)
        handler = VideoRenderHandler(config_path=config)
        with JobStore(settings.database_file()) as store:
            job = store.submit(KIND, {"plan": self.PLAN})
            store.claim_next("dead-worker")
            # Attempt 1 published the render, then the worker died before
            # recording anything.
            result = handler._render.execute(
                plan=self.PLAN, provenance={"job_id": job.id}
            )
            assert result.success, result.content
            worker = Worker(
                store,
                {KIND: handler},
                lock_path=settings.lock_file(),
                settings=settings,
            )
            time.sleep(settings.stale_after_seconds + 0.2)
            report = worker.run(once=True)
            assert [r["id"] for r in report["recovered"]] == [job.id]
            done = store.get(job.id)
        assert done.status is JobStatus.SUCCEEDED, done.to_dict()
        assert done.result["resumed"] is True
        assert done.result["ready_for_review"] is True
        assert sorted(p.name for p in output.glob("*.mp4")) == ["rev_v001.mp4"]
        assert (output / done.result["validate"]["preview"]).is_file()
