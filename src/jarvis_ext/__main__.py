"""Run JARVIS extension tools directly, without an LLM.

    python -m jarvis_ext media-inspect [SUBPATH] [--audio|--video]
        [--no-metadata] [--no-recursive] [--max-files N] [--config PATH]
    python -m jarvis_ext media-render PLAN.json [--validate] [--config PATH]
    python -m jarvis_ext media-validate OUTPUT [--no-preview]
        [--thumbnail-at SECONDS] [--config PATH]
    python -m jarvis_ext jobs submit PLAN.json [--config PATH]
    python -m jarvis_ext jobs list [--status S] [--kind K] [--limit N]
    python -m jarvis_ext jobs show JOB_ID
    python -m jarvis_ext jobs cancel JOB_ID
    python -m jarvis_ext jobs worker [--once]

Prints the same JSON an agent receives.  Exit status is 0 on success, 1 when
the tool reports an error (for example an unconfigured workspace or a failed
validation).  ``PLAN.json`` may be ``-`` to read the plan from stdin.

``jobs`` commands print JSON on stdout; errors go to stderr with exit
status 1.  ``JOB_ID`` may be the first 6+ characters of an id.  The worker
logs progress to stderr and prints a report when it exits; Ctrl-C (or
SIGTERM) lets the current job finish, a second one stops it now.  It sends
a notification for each finished job (``[jarvis.jobs] notifications``).
"""

from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Any, List, Optional

from jarvis_ext.config import ExtensionConfigError
from jarvis_ext.jobs.models import JobError, JobStatus
from jarvis_ext.tools.media_render import MediaRenderTool
from jarvis_ext.tools.media_validate import MediaValidateTool
from jarvis_ext.tools.media_workspace import MediaWorkspaceInspectTool
from openjarvis.core.types import ToolResult


def _emit(result: ToolResult) -> int:
    try:
        text = json.dumps(json.loads(result.content), indent=2, ensure_ascii=False)
    except ValueError:
        text = result.content
    print(text, file=sys.stdout if result.success else sys.stderr)
    return 0 if result.success else 1


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _read_text(source: str) -> str:
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8")


def _jobs(args: argparse.Namespace) -> int:
    """``jobs`` sub-commands.  Errors never include absolute paths."""
    from jarvis_ext.jobs.settings import JobSettings
    from jarvis_ext.jobs.store import open_job_store
    from jarvis_ext.jobs.worker import job_summary

    try:
        settings = JobSettings.load(args.config)
        if args.jobs_command == "submit":
            from jarvis_ext.video.render_job import KIND, read_payload

            try:
                plan = json.loads(_read_text(args.plan))
            except OSError as exc:
                print(f"Cannot read plan: {exc.strerror or exc}", file=sys.stderr)
                return 1
            except ValueError as exc:
                print(f"The plan is not valid JSON: {exc}", file=sys.stderr)
                return 1
            payload = {"plan": plan}
            _, problem = read_payload(payload)
            if problem:
                print(problem, file=sys.stderr)
                return 1
            with open_job_store(settings) as store:
                job = store.submit(KIND, payload)
            _print_json(
                {
                    **job_summary(job),
                    "next_step": "Run 'python -m jarvis_ext jobs worker' "
                    "(or 'jobs worker --once') to process it.",
                }
            )
            return 0
        if args.jobs_command == "list":
            with open_job_store(settings) as store:
                jobs = store.list_jobs(
                    status=JobStatus(args.status) if args.status else None,
                    kind=args.kind,
                    limit=args.limit,
                )
            _print_json({"jobs": [job_summary(j) for j in jobs]})
            return 0
        if args.jobs_command == "show":
            with open_job_store(settings) as store:
                job = store.find(args.job_id)
                events = store.events(job.id)
                notification = store.notification(job.id)
            _print_json(
                {
                    "job": job.to_dict(),
                    "notification": notification.to_dict() if notification else None,
                    "events": [
                        {
                            "at": e.at,
                            "from": e.from_status.value if e.from_status else None,
                            "to": e.to_status.value,
                            "note": e.note,
                        }
                        for e in events
                    ],
                }
            )
            return 0
        if args.jobs_command == "cancel":
            with open_job_store(settings) as store:
                job = store.cancel(store.find(args.job_id).id)
            if job.status is JobStatus.CANCELLED:
                message = "Cancelled."
            else:
                message = (
                    "Cancellation requested. The worker stops the job (and "
                    "FFmpeg) and then marks it cancelled; if the job finishes "
                    "first, its real outcome is kept."
                )
            _print_json({**job_summary(job), "message": message})
            return 0
        return _worker(args, settings)
    except (JobError, ExtensionConfigError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def _worker(args: argparse.Namespace, settings: Any) -> int:
    from jarvis_ext.jobs.store import open_job_store
    from jarvis_ext.jobs.worker import Worker, WorkerLockedError
    from jarvis_ext.notify import NotificationError, make_notifier
    from jarvis_ext.video.render_job import KIND, VideoRenderHandler

    try:
        notifier = make_notifier(settings.notifications)
    except NotificationError as exc:
        print(
            f"jobs.notifications = {settings.notifications!r} cannot be used: {exc}",
            file=sys.stderr,
        )
        return 1
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    with open_job_store(settings) as store:
        worker = Worker(
            store,
            {KIND: VideoRenderHandler(config_path=args.config)},
            lock_path=settings.lock_file(),
            settings=settings,
            notifier=notifier,
        )
        signals = {"count": 0}

        def on_signal(signum: int, frame: Any) -> None:
            signals["count"] += 1
            if signals["count"] == 1:
                print(
                    "Stopping after the current job (press Ctrl-C again to "
                    "stop it now).",
                    file=sys.stderr,
                )
                worker.stop()
            else:
                print("Stopping the current job now.", file=sys.stderr)
                worker.stop(force=True)

        stop_signals = (signal.SIGINT, signal.SIGTERM)
        previous = {sig: signal.signal(sig, on_signal) for sig in stop_signals}
        try:
            report = worker.run(once=args.once)
        except WorkerLockedError as exc:
            print(str(exc), file=sys.stderr)
            return 1
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
    _print_json(report)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis_ext")
    commands = parser.add_subparsers(dest="command", required=True)

    inspect = commands.add_parser(
        "media-inspect", help="List media in the configured media workspace."
    )
    inspect.add_argument("subpath", nargs="?", default="")
    kind = inspect.add_mutually_exclusive_group()
    kind.add_argument("--video", action="store_const", const="video", dest="media")
    kind.add_argument("--audio", action="store_const", const="audio", dest="media")
    inspect.add_argument("--no-metadata", action="store_true")
    inspect.add_argument("--no-recursive", action="store_true")
    inspect.add_argument("--max-files", type=int)
    inspect.add_argument("--config", type=Path, help="config.toml to read")

    render = commands.add_parser("media-render", help="Render an edit plan.")
    render.add_argument("plan", help="Edit plan JSON file, or - for stdin")
    render.add_argument(
        "--validate", action="store_true", help="Run media-validate afterwards"
    )
    render.add_argument("--config", type=Path, help="config.toml to read")

    validate = commands.add_parser(
        "media-validate", help="Validate a render and create review files."
    )
    validate.add_argument("output", help="Render file name, e.g. reel_v001.mp4")
    validate.add_argument("--no-preview", action="store_true")
    validate.add_argument("--thumbnail-at", type=float)
    validate.add_argument("--config", type=Path, help="config.toml to read")

    jobs = commands.add_parser("jobs", help="Queue and run long-running jobs.")
    job_commands = jobs.add_subparsers(dest="jobs_command", required=True)
    submit = job_commands.add_parser("submit", help="Queue a video.render job.")
    submit.add_argument("plan", help="Edit plan JSON file, or - for stdin")
    listing = job_commands.add_parser("list", help="List recent jobs.")
    listing.add_argument("--status", choices=[s.value for s in JobStatus])
    listing.add_argument("--kind")
    listing.add_argument("--limit", type=_limit, default=20)
    show = job_commands.add_parser("show", help="Show one job and its history.")
    show.add_argument("job_id")
    cancel = job_commands.add_parser("cancel", help="Cancel a job.")
    cancel.add_argument("job_id")
    worker = job_commands.add_parser("worker", help="Run queued jobs.")
    worker.add_argument(
        "--once", action="store_true", help="Run at most one job, then exit"
    )
    for sub in (submit, listing, show, cancel, worker):
        sub.add_argument("--config", type=Path, help="config.toml to read")

    args = parser.parse_args(argv)

    if args.command == "jobs":
        return _jobs(args)

    if args.command == "media-render":
        try:
            text = (
                sys.stdin.read()
                if args.plan == "-"
                else Path(args.plan).read_text(encoding="utf-8")
            )
        except OSError as exc:
            print(f"Cannot read plan: {exc.strerror or exc}", file=sys.stderr)
            return 1
        result = MediaRenderTool(config_path=args.config).execute(plan=text)
        code = _emit(result)
        if code != 0 or not args.validate:
            return code
        output = json.loads(result.content)["output"]["file"]
        return _emit(MediaValidateTool(config_path=args.config).execute(output=output))

    if args.command == "media-validate":
        params = {"output": args.output, "preview": not args.no_preview}
        if args.thumbnail_at is not None:
            params["thumbnail_at_seconds"] = args.thumbnail_at
        return _emit(MediaValidateTool(config_path=args.config).execute(**params))

    tool = MediaWorkspaceInspectTool(config_path=args.config)
    params = {
        "subpath": args.subpath,
        "media_type": args.media or "all",
        "include_metadata": not args.no_metadata,
        "recursive": not args.no_recursive,
    }
    if args.max_files is not None:
        params["max_files"] = args.max_files
    result = tool.execute(**params)
    if not result.success:
        print(result.content, file=sys.stderr)
        return 1
    print(json.dumps(json.loads(result.content), indent=2, ensure_ascii=False))
    return 0


def _limit(text: str) -> int:
    value = int(text)
    if not 1 <= value <= 1000:
        raise argparse.ArgumentTypeError("must be between 1 and 1000")
    return value


if __name__ == "__main__":
    sys.exit(main())
