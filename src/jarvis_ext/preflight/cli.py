"""The ``python -m jarvis_ext preflight`` command."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Optional, Sequence


def add_arguments(parser: argparse.ArgumentParser) -> None:
    from jarvis_ext.preflight.core import GROUPS

    parser.add_argument("--config", type=Path, help="config.toml to read")
    parser.add_argument(
        "--group",
        action="append",
        choices=sorted(GROUPS),
        dest="groups",
        help="Run only this group (repeatable): " + ", ".join(sorted(GROUPS)),
    )
    parser.add_argument(
        "--only",
        action="append",
        dest="ids",
        metavar="CHECK_ID",
        help="Run only this check id (repeatable); see --list",
    )
    parser.add_argument("--list", action="store_true", help="List checks and exit")
    parser.add_argument("--json", action="store_true", help="Print the report as JSON")
    parser.add_argument(
        "--live-brain",
        action="store_true",
        help="OPT-IN: send ONE tiny request to the configured AI provider "
        "(may use free-tier quota or bill)",
    )
    parser.add_argument(
        "--engine",
        help="Engine key as given to 'jarvis serve --engine' (overrides "
        "[engine] default, like the real command)",
    )
    parser.add_argument(
        "--model",
        help="Model as given to 'jarvis serve --model' (overrides the "
        "configured model, like the real command)",
    )
    parser.add_argument(
        "--spawn-server",
        action="store_true",
        help="OPT-IN: start the real 'jarvis serve' on a free local port, "
        "check /health, stop it",
    )


def run(args: argparse.Namespace) -> int:
    from jarvis_ext.preflight.core import registered_checks, run_preflight

    if args.list:
        for item in registered_checks():
            budget = (
                ""
                if item.budget_seconds is None
                else f" (budget {item.budget_seconds:g} s)"
            )
            print(f"{item.id:<26} [{item.group}] {item.title}{budget}")
        return 0
    try:
        report = run_preflight(
            config_path=args.config,
            groups=args.groups,
            ids=args.ids,
            live_brain=args.live_brain,
            spawn_server=args.spawn_server,
            engine=args.engine,
            model=args.model,
        )
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(report.to_dict(), indent=2, ensure_ascii=False))
    else:
        print(report.render())
    return report.exit_code


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jarvis_ext preflight")
    add_arguments(parser)
    return run(parser.parse_args(argv))
