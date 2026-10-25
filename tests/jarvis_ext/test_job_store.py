"""Job model, status rules, SQLite store and ``[jarvis.jobs]`` settings."""

from __future__ import annotations

import itertools
import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from jarvis_ext.config import ExtensionConfigError
from jarvis_ext.jobs import (
    MAX_ATTEMPTS,
    SCHEMA_VERSION,
    TRANSITIONS,
    InvalidJobError,
    InvalidTransitionError,
    JobNotFoundError,
    JobSettings,
    JobStatus,
    JobStore,
    JobStoreError,
    SchemaVersionError,
    open_job_store,
)
from jarvis_ext.jobs.models import check_transition
from jarvis_ext.jobs.settings import default_database_path
from jarvis_ext.jobs.store import APPLICATION_ID

KIND = "video.render"
WORKER = "worker-1"


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "jobs.db"


@pytest.fixture
def store(db_path: Path):
    with JobStore(db_path) as s:
        yield s


def _running(store: JobStore, **submit_kwargs):
    job = store.submit(KIND, {"n": 1}, **submit_kwargs)
    claimed = store.claim_next(WORKER)
    assert claimed is not None and claimed.id == job.id
    return claimed


# -- status rules -------------------------------------------------------------


class TestTransitionTable:
    DOCUMENTED = {
        ("queued", "running"),
        ("queued", "cancelled"),
        ("running", "queued"),
        ("running", "succeeded"),
        ("running", "failed"),
        ("running", "cancelled"),
    }

    def test_table_matches_documentation(self):
        allowed = {
            (a.value, b.value) for a, targets in TRANSITIONS.items() for b in targets
        }
        assert allowed == self.DOCUMENTED

    @pytest.mark.parametrize(
        "current,target", list(itertools.product(JobStatus, JobStatus))
    )
    def test_check_transition(self, current, target):
        if (current.value, target.value) in self.DOCUMENTED:
            check_transition(current, target)
        else:
            with pytest.raises(InvalidTransitionError):
                check_transition(current, target)

    def test_final_statuses_have_no_exits(self):
        for status in JobStatus:
            assert status.is_final == (not TRANSITIONS[status])
        assert {s for s in JobStatus if s.is_final} == {
            JobStatus.SUCCEEDED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
        }


# -- submitting ---------------------------------------------------------------


class TestSubmit:
    def test_new_job_is_queued(self, store):
        job = store.submit(KIND, {"plan": {"output_name": "reel"}})
        assert job.status is JobStatus.QUEUED
        assert job.kind == KIND
        assert job.payload == {"plan": {"output_name": "reel"}}
        assert job.attempts == 0
        assert job.max_attempts == MAX_ATTEMPTS
        assert job.claimed_by is None and job.parent_id is None
        assert not job.cancel_requested and not job.is_final
        assert len(job.id) == 32
        assert [e.note for e in store.events(job.id)] == ["submitted"]

    @pytest.mark.parametrize(
        "kind",
        ["video.render", "research", "a.b.c", "data_analysis.report_v2"],
    )
    def test_valid_kinds(self, store, kind):
        assert store.submit(kind, {}).kind == kind

    @pytest.mark.parametrize(
        "kind",
        [
            "",
            "Video.render",
            "video..render",
            ".video",
            "video.",
            "1video",
            "video render",
            "video/render",
            "x" * 65,
            None,
            3,
        ],
    )
    def test_invalid_kinds(self, store, kind):
        with pytest.raises(InvalidJobError):
            store.submit(kind, {})

    @pytest.mark.parametrize(
        "payload",
        [
            [],
            "plan",
            None,
            {"a": (1, 2)},
            {1: "a"},
            {"a": float("nan")},
            {"a": float("inf")},
            {"a": object()},
            {"a": {1, 2}},
        ],
    )
    def test_invalid_payloads(self, store, payload):
        with pytest.raises(InvalidJobError):
            store.submit(KIND, payload)

    def test_payload_size_limit(self, store):
        with pytest.raises(InvalidJobError):
            store.submit(KIND, {"blob": "x" * (1024 * 1024)})

    def test_unicode_payload_round_trips(self, store):
        payload = {
            "name": "café ☕ — 'quotes' \"and\" \\back",
            "n": [1, 2.5, True, None],
        }
        job = store.submit(KIND, payload)
        assert store.get(job.id).payload == payload

    @pytest.mark.parametrize("attempts", [0, 3, -1, True, 1.0, "2"])
    def test_invalid_max_attempts(self, store, attempts):
        with pytest.raises(InvalidJobError):
            store.submit(KIND, {}, max_attempts=attempts)

    def test_parent_must_exist(self, store):
        with pytest.raises(JobNotFoundError):
            store.submit(KIND, {}, parent_id="missing")
        assert store.list_jobs() == []

    def test_revision_links_parent(self, store):
        parent = store.submit(KIND, {"v": 1})
        child = store.submit(KIND, {"v": 2}, parent_id=parent.id)
        assert child.parent_id == parent.id

    def test_get_unknown(self, store):
        with pytest.raises(JobNotFoundError):
            store.get("nope")
        with pytest.raises(JobNotFoundError):
            store.events("nope")

    def test_list_filters_and_order(self, store):
        a = store.submit("video.render", {})
        b = store.submit("research.report", {})
        c = store.submit("video.render", {})
        store.cancel(c.id)
        assert [j.id for j in store.list_jobs()] == [c.id, b.id, a.id]
        assert [j.id for j in store.list_jobs(kind="video.render")] == [c.id, a.id]
        assert [j.id for j in store.list_jobs(status=JobStatus.QUEUED)] == [b.id, a.id]
        assert [j.id for j in store.list_jobs(limit=1)] == [c.id]


# -- claiming -----------------------------------------------------------------


class TestClaim:
    def test_empty_store(self, store):
        assert store.claim_next(WORKER) is None

    def test_fifo_and_running_fields(self, store):
        first = store.submit(KIND, {"i": 1})
        second = store.submit(KIND, {"i": 2})
        job = store.claim_next(WORKER)
        assert job.id == first.id
        assert job.status is JobStatus.RUNNING
        assert job.claimed_by == WORKER and job.attempts == 1
        assert job.started_at is not None and job.finished_at is None
        assert store.claim_next(WORKER).id == second.id
        assert store.claim_next(WORKER) is None

    def test_kind_filter(self, store):
        store.submit("research.report", {})
        video = store.submit(KIND, {})
        assert store.claim_next(WORKER, kinds=[KIND]).id == video.id
        assert store.claim_next(WORKER, kinds=[KIND]) is None
        assert store.claim_next(WORKER, kinds=[]) is None
        assert store.claim_next(WORKER, kinds=["research.report"]) is not None

    def test_cancelled_jobs_are_not_claimed(self, store):
        job = store.submit(KIND, {})
        store.cancel(job.id)
        assert store.claim_next(WORKER) is None

    @pytest.mark.parametrize("worker", ["", "   ", "x" * 129, "a\nb", None, 5])
    def test_invalid_worker_id(self, store, worker):
        store.submit(KIND, {})
        with pytest.raises(InvalidJobError):
            store.claim_next(worker)
        assert store.list_jobs()[0].status is JobStatus.QUEUED

    def test_concurrent_threads_never_share_a_job(self, db_path):
        with JobStore(db_path) as setup:
            ids = {setup.submit(KIND, {"i": i}).id for i in range(60)}
        claimed: list = []
        errors: list = []
        start = threading.Barrier(8)

        def claimer(n: int) -> None:
            try:
                with JobStore(db_path, busy_timeout_seconds=30) as own:
                    start.wait()
                    while (job := own.claim_next(f"w{n}")) is not None:
                        claimed.append(job.id)
            except Exception as exc:  # pragma: no cover - reported below
                errors.append(exc)

        threads = [threading.Thread(target=claimer, args=(n,)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert errors == []
        assert len(claimed) == len(set(claimed)) == 60
        assert set(claimed) == ids
        with JobStore(db_path) as check:
            assert all(j.attempts == 1 for j in check.list_jobs())

    def test_shared_store_between_threads(self, store):
        ids = {store.submit(KIND, {"i": i}).id for i in range(40)}
        claimed: list = []

        def claimer(n: int) -> None:
            while (job := store.claim_next(f"w{n}")) is not None:
                claimed.append(job.id)

        threads = [threading.Thread(target=claimer, args=(n,)) for n in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)
        assert len(claimed) == len(set(claimed)) == 40
        assert set(claimed) == ids

    def test_concurrent_processes_never_share_a_job(self, db_path, tmp_path):
        with JobStore(db_path) as setup:
            ids = {setup.submit(KIND, {"i": i}).id for i in range(40)}
        go = tmp_path / "go"
        script = textwrap.dedent(
            """
            import sys, time
            from pathlib import Path
            from jarvis_ext.jobs import JobStore
            db, go, name = sys.argv[1], Path(sys.argv[2]), sys.argv[3]
            store = JobStore(db, busy_timeout_seconds=30)
            deadline = time.monotonic() + 30
            while not go.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            while (job := store.claim_next(name)) is not None:
                print(job.id, flush=True)
            store.close()
            """
        )
        procs = [
            subprocess.Popen(
                [sys.executable, "-c", script, str(db_path), str(go), f"p{n}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for n in range(4)
        ]
        time.sleep(0.5)
        go.touch()
        claimed: list = []
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            assert proc.returncode == 0, err
            claimed.extend(out.split())
        assert len(claimed) == len(set(claimed)) == 40
        assert set(claimed) == ids


# -- finishing, retrying, cancelling ------------------------------------------


class TestRunningJobs:
    def test_complete(self, store):
        job = _running(store)
        done = store.complete(job.id, WORKER, {"output": "reel_v001.mp4"})
        assert done.status is JobStatus.SUCCEEDED and done.is_final
        assert done.result == {"output": "reel_v001.mp4"}
        assert done.finished_at is not None
        assert [(e.from_status, e.to_status) for e in store.events(job.id)] == [
            (None, JobStatus.QUEUED),
            (JobStatus.QUEUED, JobStatus.RUNNING),
            (JobStatus.RUNNING, JobStatus.SUCCEEDED),
        ]

    def test_complete_without_result(self, store):
        job = _running(store)
        assert store.complete(job.id, WORKER).result is None

    def test_invalid_result_leaves_job_running(self, store):
        job = _running(store)
        with pytest.raises(InvalidJobError):
            store.complete(job.id, WORKER, {"bad": float("nan")})
        assert store.get(job.id).status is JobStatus.RUNNING

    def test_fail(self, store):
        job = _running(store)
        failed = store.fail(job.id, WORKER, "invalid_plan", "clip 2 ends too late")
        assert failed.status is JobStatus.FAILED
        assert failed.error_code == "invalid_plan"
        assert failed.error_message == "clip 2 ends too late"
        assert store.events(job.id)[-1].note == "invalid_plan"

    def test_error_message_is_truncated(self, store):
        job = _running(store)
        assert len(store.fail(job.id, WORKER, "x", "e" * 10_000).error_message) == 4000

    @pytest.mark.parametrize("code", ["", "Bad", "has space", "1x", None])
    def test_invalid_error_code(self, store, code):
        job = _running(store)
        with pytest.raises(InvalidJobError):
            store.fail(job.id, WORKER, code)

    def test_only_the_claiming_worker_may_finish(self, store):
        job = _running(store)
        for action in (
            lambda: store.complete(job.id, "other"),
            lambda: store.fail(job.id, "other", "x"),
            lambda: store.retry(job.id, "other", "x"),
        ):
            with pytest.raises(InvalidTransitionError):
                action()
        assert store.get(job.id).status is JobStatus.RUNNING

    def test_queued_job_cannot_be_finished(self, store):
        job = store.submit(KIND, {})
        for action in (
            lambda: store.complete(job.id, WORKER),
            lambda: store.fail(job.id, WORKER, "x"),
            lambda: store.retry(job.id, WORKER, "x"),
            lambda: store.confirm_cancelled(job.id, WORKER),
        ):
            with pytest.raises(InvalidTransitionError):
                action()
        assert store.get(job.id).status is JobStatus.QUEUED

    @pytest.mark.parametrize("final", ["succeeded", "failed", "cancelled"])
    def test_final_jobs_never_change(self, store, final):
        if final == "cancelled":
            job = store.cancel(store.submit(KIND, {}).id)
        else:
            job = _running(store)
            if final == "succeeded":
                store.complete(job.id, WORKER)
            else:
                store.fail(job.id, WORKER, "x")
        before = store.get(job.id)
        for action in (
            lambda: store.complete(job.id, WORKER),
            lambda: store.fail(job.id, WORKER, "x"),
            lambda: store.retry(job.id, WORKER, "x"),
            lambda: store.confirm_cancelled(job.id, WORKER),
            lambda: store.cancel(job.id),
        ):
            with pytest.raises(InvalidTransitionError):
                action()
        assert store.get(job.id) == before
        assert store.claim_next(WORKER) is None

    def test_retry_requeues_until_attempts_used(self, store):
        job = _running(store)
        retried = store.retry(job.id, WORKER, "ffmpeg_failed", "exit 1")
        assert retried.status is JobStatus.QUEUED
        assert retried.claimed_by is None and retried.attempts == 1
        assert retried.error_code == "ffmpeg_failed"
        again = store.claim_next("worker-2")
        assert again.id == job.id and again.attempts == 2
        with pytest.raises(InvalidTransitionError, match="attempt"):
            store.retry(job.id, "worker-2", "ffmpeg_failed")
        assert (
            store.fail(job.id, "worker-2", "ffmpeg_failed").status is JobStatus.FAILED
        )

    def test_single_attempt_job_is_never_retried(self, store):
        job = _running(store, max_attempts=1)
        with pytest.raises(InvalidTransitionError):
            store.retry(job.id, WORKER, "ffmpeg_failed")
        assert store.get(job.id).status is JobStatus.RUNNING

    def test_cancel_queued(self, store):
        job = store.cancel(store.submit(KIND, {}).id)
        assert job.status is JobStatus.CANCELLED and job.cancel_requested
        assert job.finished_at is not None

    def test_cancel_running_is_a_request(self, store):
        job = _running(store)
        requested = store.cancel(job.id)
        assert requested.status is JobStatus.RUNNING and requested.cancel_requested
        assert store.cancel(job.id) == requested  # idempotent
        with pytest.raises(InvalidTransitionError):
            store.retry(job.id, WORKER, "ffmpeg_failed")
        with pytest.raises(InvalidTransitionError):
            store.confirm_cancelled(job.id, "other")
        done = store.confirm_cancelled(job.id, WORKER)
        assert done.status is JobStatus.CANCELLED
        notes = [e.note for e in store.events(job.id)]
        assert notes[-2:] == ["cancellation requested", "cancelled by worker"]

    def test_confirm_cancelled_needs_a_request(self, store):
        job = _running(store)
        with pytest.raises(InvalidTransitionError):
            store.confirm_cancelled(job.id, WORKER)

    @pytest.mark.parametrize("outcome", ["succeeded", "failed"])
    def test_real_outcome_wins_over_cancel_request(self, store, outcome):
        """D27: work that finished first is not reported as cancelled."""
        job = _running(store)
        store.cancel(job.id)
        if outcome == "succeeded":
            done = store.complete(job.id, WORKER, {"output": "reel_v001.mp4"})
        else:
            done = store.fail(job.id, WORKER, "ffmpeg_failed")
        assert done.status.value == outcome
        assert done.cancel_requested
        with pytest.raises(InvalidTransitionError):
            store.confirm_cancelled(job.id, WORKER)


# -- persistence and schema ---------------------------------------------------


class TestPersistence:
    def test_survives_reopen(self, db_path):
        with JobStore(db_path) as first:
            left_running = first.submit(KIND, {"keep": True})
            finished = first.submit(KIND, {"i": 2})
            queued = first.submit(KIND, {"i": 3})
            first.claim_next(WORKER)
            done = first.complete(first.claim_next(WORKER).id, WORKER, {"ok": 1})
        with JobStore(db_path) as second:
            assert second.get(left_running.id).status is JobStatus.RUNNING
            assert second.get(finished.id) == done
            assert second.get(queued.id) == queued
            assert len(second.events(finished.id)) == 3
            assert second.claim_next(WORKER).id == queued.id

    def test_new_database(self, db_path, store):
        assert store.schema_version == SCHEMA_VERSION
        assert store.journal_mode == "wal"
        conn = sqlite3.connect(db_path)
        assert conn.execute("PRAGMA application_id").fetchone()[0] == APPLICATION_ID
        conn.close()

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
    def test_file_is_private(self, db_path, store):
        store.submit(KIND, {})
        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600

    def test_future_schema_is_refused_unchanged(self, db_path):
        JobStore(db_path).close()
        conn = sqlite3.connect(db_path)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()
        before = db_path.read_bytes()
        with pytest.raises(SchemaVersionError) as info:
            JobStore(db_path)
        assert str(db_path.parent) not in str(info.value)
        assert db_path.read_bytes() == before

    def test_foreign_sqlite_file_is_refused_unchanged(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE notes (x TEXT)")
        conn.commit()
        conn.close()
        before = db_path.read_bytes()
        with pytest.raises(JobStoreError, match="not a JARVIS job database"):
            JobStore(db_path)
        assert db_path.read_bytes() == before

    def test_other_application_id_is_refused(self, db_path):
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA application_id = 1234")
        conn.commit()
        conn.close()
        with pytest.raises(JobStoreError):
            JobStore(db_path)

    def test_non_database_file_is_refused(self, db_path):
        db_path.write_bytes(b"this is not sqlite" * 100)
        with pytest.raises(JobStoreError) as info:
            JobStore(db_path)
        assert str(db_path.parent) not in str(info.value)
        assert db_path.read_bytes() == b"this is not sqlite" * 100

    def test_directory_is_refused(self, db_path):
        db_path.mkdir()
        with pytest.raises(JobStoreError):
            JobStore(db_path)

    def test_symlink_is_refused(self, db_path, tmp_path):
        JobStore(tmp_path / "real.db").close()
        db_path.symlink_to(tmp_path / "real.db")
        with pytest.raises(JobStoreError, match="symbolic link"):
            JobStore(db_path)

    def test_missing_folder_is_refused(self, tmp_path):
        with pytest.raises(JobStoreError) as info:
            JobStore(tmp_path / "unplugged" / "jobs.db")
        assert str(tmp_path) not in str(info.value)
        assert not (tmp_path / "unplugged").exists()

    def test_two_stores_initialise_the_same_file(self, db_path):
        a = JobStore(db_path)
        b = JobStore(db_path)
        job = a.submit(KIND, {})
        assert b.get(job.id) == job
        a.close()
        b.close()


# -- settings -----------------------------------------------------------------


class TestJobSettings:
    def test_defaults(self):
        settings = JobSettings.from_mapping({})
        assert settings.uses_default_path
        assert settings.max_attempts == MAX_ATTEMPTS
        assert settings.database_file() == default_database_path()
        assert default_database_path() == (
            Path(os.environ["OPENJARVIS_HOME"]).resolve() / "jarvis" / "jobs.db"
        )

    def test_custom_path(self, tmp_path):
        settings = JobSettings.from_mapping(
            {"database_path": str(tmp_path / "x.db"), "max_attempts": 1}
        )
        assert settings.database_file() == tmp_path.resolve() / "x.db"
        assert settings.max_attempts == 1

    def test_home_is_expanded(self):
        settings = JobSettings.from_mapping({"database_path": "~/jobs.db"})
        assert settings.database_file() == Path.home().resolve() / "jobs.db"

    @pytest.mark.parametrize(
        "section",
        [
            {"database_path": "relative/jobs.db"},
            {"database_path": 5},
            {"database_path": "/tmp/.."},
            {"max_attempts": 0},
            {"max_attempts": 3},
            {"max_attempts": True},
            {"max_attempts": 1.5},
            {"max_attempts": "2"},
            {"workers": 2},
        ],
    )
    def test_invalid(self, section):
        with pytest.raises(ExtensionConfigError):
            JobSettings.from_mapping(section)

    def test_load_from_config_file(self, tmp_path):
        config = tmp_path / "config.toml"
        config.write_text(
            f'[jarvis.jobs]\ndatabase_path = "{tmp_path / "j.db"}"\nmax_attempts = 1\n',
            encoding="utf-8",
        )
        settings = JobSettings.load(config)
        assert settings.database_file() == tmp_path.resolve() / "j.db"
        assert settings.max_attempts == 1

    def test_open_job_store_default_location(self):
        with open_job_store(JobSettings()) as store:
            job = store.submit(KIND, {})
        path = default_database_path()
        assert path.is_file()
        if sys.platform != "win32":
            assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        with open_job_store(JobSettings()) as store:
            assert store.get(job.id).id == job.id

    def test_open_job_store_custom_folder_must_exist(self, tmp_path):
        settings = JobSettings.from_mapping(
            {"database_path": str(tmp_path / "gone" / "jobs.db")}
        )
        with pytest.raises(JobStoreError):
            open_job_store(settings)
        assert not (tmp_path / "gone").exists()

    def test_open_job_store_uses_max_attempts(self, tmp_path):
        settings = JobSettings.from_mapping(
            {"database_path": str(tmp_path / "j.db"), "max_attempts": 1}
        )
        with open_job_store(settings) as store:
            assert store.submit(KIND, {}).max_attempts == 1
