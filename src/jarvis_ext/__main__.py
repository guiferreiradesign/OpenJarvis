"""Run JARVIS extension tools directly, without an LLM.

    python -m jarvis_ext media-inspect [SUBPATH] [--audio|--video]
        [--no-metadata] [--no-recursive] [--max-files N] [--config PATH]

Prints the same JSON an agent receives.  Exit status is 0 on success, 1 when
the tool reports an error (for example an unconfigured workspace).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import List, Optional

from jarvis_ext.tools.media_workspace import MediaWorkspaceInspectTool


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

    args = parser.parse_args(argv)
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
