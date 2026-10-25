"""Run JARVIS extension tools directly, without an LLM.

    python -m jarvis_ext media-inspect [SUBPATH] [--audio|--video]
        [--no-metadata] [--no-recursive] [--max-files N] [--config PATH]
    python -m jarvis_ext media-render PLAN.json [--validate] [--config PATH]
    python -m jarvis_ext media-validate OUTPUT [--no-preview]
        [--thumbnail-at SECONDS] [--config PATH]

Prints the same JSON an agent receives.  Exit status is 0 on success, 1 when
the tool reports an error (for example an unconfigured workspace or a failed
validation).  ``PLAN.json`` may be ``-`` to read the plan from stdin.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

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

    args = parser.parse_args(argv)

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


if __name__ == "__main__":
    sys.exit(main())
