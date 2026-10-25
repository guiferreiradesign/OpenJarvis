"""Path safety shared by every JARVIS media tool.

Phase 1's ``media_workspace_inspect`` introduced these rules; the render and
validate tools reuse them unchanged so there is exactly one implementation of
"is this path allowed".

* :func:`resolve_relative` maps a user/model-supplied, root-relative path to a
  real path, refusing absolute paths, ``~``, drive letters, ``..``, NUL bytes,
  and symbolic links (or, when links are allowed, links whose resolved target
  leaves the root).
* :func:`same_or_inside` answers "is *path* the folder *ancestor* or inside
  it?" by comparing device and inode numbers rather than spelling, so it is
  not fooled by case differences on case-insensitive filesystems (APFS) or by
  alternative spellings of the same folder.  It is used to keep the output
  folder and the source workspace apart.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Optional, Tuple


class AccessDenied(Exception):
    """A requested path is not inside the authorised folder."""


def is_within(path: Path, root: Path) -> bool:
    """Lexical containment check on already-resolved paths."""
    return path == root or path.is_relative_to(root)


def split_relative(subpath: str, *, where: str = "media workspace") -> list:
    """Validate a root-relative path and return its components.

    Raises :class:`AccessDenied` for anything that is not a plain relative
    path.  ``""`` and ``"."`` return ``[]`` (the root itself).
    """
    if not subpath or subpath == ".":
        return []
    if "\x00" in subpath:
        raise AccessDenied("Path contains a NUL character.")
    if (
        PurePosixPath(subpath).is_absolute()
        or PureWindowsPath(subpath).is_absolute()
        or PureWindowsPath(subpath).drive
        or subpath.startswith("~")
    ):
        raise AccessDenied(f"Use a path relative to the {where} root.")
    parts = [p for p in re.split(r"[\\/]+", subpath) if p not in ("", ".")]
    if any(part == ".." for part in parts):
        raise AccessDenied(f"'..' is not allowed in {where} paths.")
    return parts


def resolve_relative(
    root: Path,
    subpath: str,
    *,
    follow_symlinks: bool = False,
    where: str = "media workspace",
) -> Tuple[Path, Path]:
    """Map a root-relative *subpath* to ``(real path, display path)``.

    *root* must already be resolved.  Raises :class:`AccessDenied` when the
    path is not allowed and :class:`FileNotFoundError` when it does not exist.
    """
    parts = split_relative(subpath, where=where)
    if not parts:
        return root, Path(".")

    current = root
    for part in parts:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            raise FileNotFoundError(subpath) from None
        if stat.S_ISLNK(info.st_mode):
            if not follow_symlinks:
                raise AccessDenied(f"Symbolic links are not followed in the {where}.")
            current = current.resolve(strict=True)
            if not is_within(current, root):
                raise AccessDenied(f"Path leads outside the {where}.")
    resolved = current.resolve(strict=True)
    if not is_within(resolved, root):
        raise AccessDenied(f"Path leads outside the {where}.")
    return resolved, Path(*parts)


def _identity(path: Path) -> Optional[Tuple[int, int]]:
    try:
        info = os.stat(path)
    except OSError:
        return None
    return info.st_dev, info.st_ino


def same_or_inside(path: Path, ancestor: Path) -> bool:
    """True when *path* is *ancestor* or lies anywhere below it.

    Compares ``(st_dev, st_ino)`` of *path* and each of its parents with
    *ancestor*, so different spellings (letter case on APFS, symlinked
    parents) of the same folder are recognised.  Falls back to a lexical
    comparison of resolved paths for components that do not exist yet.
    """
    target = _identity(ancestor)
    resolved_path = Path(path).resolve()
    resolved_ancestor = Path(ancestor).resolve()
    if is_within(resolved_path, resolved_ancestor):
        return True
    if target is None:
        return False
    for candidate in (resolved_path, *resolved_path.parents):
        if _identity(candidate) == target:
            return True
    return False


__all__ = [
    "AccessDenied",
    "is_within",
    "resolve_relative",
    "same_or_inside",
    "split_relative",
]
