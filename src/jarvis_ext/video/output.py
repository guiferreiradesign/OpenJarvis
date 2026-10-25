"""The output folder: private temp directories, versioned names, publishing.

Layout under ``[jarvis.media_render] output_root``::

    reel_v001.mp4              published render
    reel_v001.render.json      what produced it (plan, settings, checksum)
    review/reel_v001_preview_v001.mp4
    review/reel_v001_thumb_v001.jpg
    .jarvis-tmp/render-XXXX/   in-progress work, removed when the call ends

Guarantees:

* Nothing outside ``output_root`` is written, and nothing inside it is ever
  overwritten or deleted except the temp directories this module created.
* Files are produced in a temp directory and only *published* (given their
  final name) once complete and checked.
* Publishing claims the name atomically: a hard link (``link(2)`` fails if
  the name exists), or, on filesystems without hard links, an exclusive
  ``O_CREAT|O_EXCL`` placeholder that the finished file then replaces.  Two
  concurrent renders therefore never get the same version number, and a
  name that appears between "scan" and "claim" is simply skipped.
"""

from __future__ import annotations

import contextlib
import errno
import json
import os
import re
import shutil
import stat
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, Tuple

TMP_DIRNAME = ".jarvis-tmp"
REVIEW_DIRNAME = "review"
RECORD_SUFFIX = ".render.json"
MAX_VERSION = 9999
_TMP_PREFIX = "job-"
_RENDER_NAME = re.compile(r"(?P<stem>[a-z0-9][a-z0-9_-]{0,47})_v(?P<version>\d{3,4})")
_LINK_UNSUPPORTED = {
    errno.EPERM,
    errno.EXDEV,
    errno.EMLINK,
    getattr(errno, "ENOTSUP", errno.EPERM),
    getattr(errno, "EOPNOTSUPP", errno.EPERM),
}


class OutputError(RuntimeError):
    """The output folder is missing, unsafe, or a name cannot be claimed."""


def versioned_name(stem: str, version: int, suffix: str) -> str:
    return f"{stem}_v{version:03d}{suffix}"


def parse_render_name(name: str) -> Optional[Tuple[str, int]]:
    """``("reel", 1)`` for ``"reel_v001.mp4"``; ``None`` for anything else."""
    if not name.endswith(".mp4"):
        return None
    match = _RENDER_NAME.fullmatch(name[: -len(".mp4")])
    if not match:
        return None
    return match.group("stem"), int(match.group("version"))


class OutputFolder:
    """All writes the media tools make go through this class."""

    def __init__(self, root: Path) -> None:
        self.root = root

    # -- folders ------------------------------------------------------------

    def check_ready(self) -> None:
        try:
            info = os.stat(self.root)
        except OSError:
            raise OutputError(
                "The configured output folder does not exist (create it first, "
                "or connect its drive)."
            ) from None
        if not stat.S_ISDIR(info.st_mode):
            raise OutputError("The configured output folder is not a directory.")
        if not os.access(self.root, os.W_OK | os.X_OK):
            raise OutputError("The configured output folder is not writable.")

    def subdir(self, name: str) -> Path:
        """A direct sub-folder of the root, created if needed; never a link."""
        path = self.root / name
        try:
            os.mkdir(path, 0o755)
        except FileExistsError:
            pass
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OutputError(f"Output sub-folder '{name}' is not a plain directory.")
        return path

    def make_temp_dir(self) -> Path:
        return Path(tempfile.mkdtemp(prefix=_TMP_PREFIX, dir=self.subdir(TMP_DIRNAME)))

    def remove_temp_dir(self, path: Optional[Path]) -> None:
        """Delete a temp directory this class created; refuse anything else."""
        if path is None:
            return
        tmp_root = self.root / TMP_DIRNAME
        if path.parent != tmp_root or not path.name.startswith(_TMP_PREFIX):
            return
        try:
            if stat.S_ISLNK(os.lstat(path).st_mode):
                return
        except FileNotFoundError:
            return
        shutil.rmtree(path, ignore_errors=True)

    # -- names --------------------------------------------------------------

    def resolve_child(self, relative: str) -> Path:
        """Map ``"reel_v001.mp4"`` or ``"review/x.jpg"`` to a path in the root.

        Only one optional ``review/`` level is accepted; no other separators,
        no hidden names, no links.
        """
        parts = relative.split("/")
        if len(parts) == 2 and parts[0] == REVIEW_DIRNAME:
            directory, name = self.root / REVIEW_DIRNAME, parts[1]
        elif len(parts) == 1:
            directory, name = self.root, parts[0]
        else:
            raise OutputError("Give a file name from the output folder.")
        if (
            not name
            or name.startswith(".")
            or "\\" in name
            or "\x00" in name
            or name in ("..", ".")
        ):
            raise OutputError("Invalid output file name.")
        path = directory / name
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            raise FileNotFoundError(relative) from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OutputError("Output file is not a regular file.")
        return path

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    @staticmethod
    def next_version(directory: Path, stem: str) -> int:
        """One more than the highest version of *stem* in *directory*."""
        pattern = re.compile(rf"{re.escape(stem)}_v(\d{{3,4}})(?:\.|$)")
        highest = 0
        try:
            names = os.listdir(directory)
        except FileNotFoundError:
            names = []
        for name in names:
            match = pattern.match(name)
            if match:
                highest = max(highest, int(match.group(1)))
        return highest + 1

    # -- publishing ---------------------------------------------------------

    @staticmethod
    def claim(source: Path, destination: Path) -> None:
        """Give *source* the name *destination* iff that name is free.

        Raises ``FileExistsError`` when it is taken.  Never overwrites.
        """
        try:
            os.link(source, destination)
        except FileExistsError:
            raise
        except OSError as exc:
            if exc.errno not in _LINK_UNSUPPORTED:
                raise
            # No hard links here (e.g. exFAT): reserve the name exclusively,
            # then move the finished file onto our own placeholder.
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.close(fd)
            try:
                os.replace(source, destination)
            except BaseException:
                # Only our own, still-empty placeholder is removed.
                with contextlib.suppress(OSError):
                    if os.path.getsize(destination) == 0:
                        os.unlink(destination)
                raise
            return
        os.unlink(source)

    def publish_versioned(
        self,
        source: Path,
        directory: Path,
        stem: str,
        suffix: str,
        *,
        start: Optional[int] = None,
    ) -> Tuple[Path, int]:
        """Publish *source* as ``<stem>_vNNN<suffix>`` with the next free NNN."""
        version = start or self.next_version(directory, stem)
        while version <= MAX_VERSION:
            destination = directory / versioned_name(stem, version, suffix)
            try:
                self.claim(source, destination)
            except FileExistsError:
                version += 1
                continue
            return destination, version
        raise OutputError(f"No free version number left for '{stem}'.")

    def write_record(
        self, temp_dir: Path, destination: Path, record: Mapping[str, Any]
    ) -> None:
        """Write JSON next to a published file, failing if the name is taken."""
        staging = temp_dir / "record.json"
        with open(staging, "w", encoding="utf-8") as fh:
            json.dump(record, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        self.claim(staging, destination)


__all__ = [
    "MAX_VERSION",
    "OutputError",
    "OutputFolder",
    "RECORD_SUFFIX",
    "REVIEW_DIRNAME",
    "TMP_DIRNAME",
    "parse_render_name",
    "versioned_name",
]
