"""Render provenance: which job produced a render, and from exactly what.

``media_render`` adds an optional ``provenance`` block to the render record
when the job handler asks for it (D24, D32); renders made by an agent or the
CLI are unchanged.  The block holds no absolute path::

    "provenance": {
        "key_version": 1,
        "job_id": "<32 hex>",           # the job that published this file
        "plan_digest": "<sha256>",      # the normalised plan
        "input_key": "<sha256>" | null, # everything that determines the bytes
        "sources": [{"source": "Shoot/a.mp4", "size_bytes": 1, "sha256": "…"}],
        "subtitles_sha256": "…" | null, # the subtitle text actually muxed/burnt
        "logo_sha256": "…" | null
    }

The **input key** is a SHA-256 over the normalised plan, its preset, the
FFmpeg version, the exact render command (which already encodes every
option, the filter graph and the FFmpeg capabilities it relied on, and the
source, FFmpeg and logo paths) and the content checksums of every source,
the subtitle text and the logo.  Two renders with the same key were made by
the same FFmpeg, with the same command, from byte-identical inputs.
``input_key`` is ``null`` when a source changed while it was rendered; such a
file is never reused or resumed.

Nothing here touches the job store.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Mapping, Optional

KEY_VERSION = 1
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID = re.compile(r"^[0-9a-f]{32}$")


def canonical_digest(value: Any) -> str:
    """SHA-256 of *value* as canonical JSON (sorted keys, no whitespace)."""
    text = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(text).hexdigest()


def is_digest(value: Any) -> bool:
    return isinstance(value, str) and _HEX64.match(value) is not None


def is_job_id(value: Any) -> bool:
    return isinstance(value, str) and _JOB_ID.match(value) is not None


def record_provenance(record: Mapping[str, Any]) -> Optional[Mapping[str, Any]]:
    """The record's provenance block if it is well formed, else ``None``.

    Records without one (Phase 2 renders, renders from before this step)
    have no provenance and are never associated with a job.
    """
    block = record.get("provenance") if isinstance(record, Mapping) else None
    if (
        not isinstance(block, Mapping)
        or block.get("key_version") != KEY_VERSION
        or not is_job_id(block.get("job_id"))
        or not is_digest(block.get("plan_digest"))
        or not (block.get("input_key") is None or is_digest(block.get("input_key")))
    ):
        return None
    return block


def mismatch(
    record: Mapping[str, Any],
    *,
    job_id: str,
    input_key: Optional[str],
    sha256: Optional[str] = None,
) -> Optional[str]:
    """Why *record* is **not** the render *job_id* made from *input_key*.

    ``None`` means it is: the record names that job, carries the same
    input key, and (if *sha256* is given) records that output checksum.
    The file itself is checked by ``media_validate`` afterwards.
    """
    block = record_provenance(record)
    if block is None:
        return "its render record has no usable provenance"
    if block["job_id"] != job_id:
        return "its render record names a different job"
    if not is_digest(input_key) or block.get("input_key") != input_key:
        return "its inputs differ from this job's (plan, settings, FFmpeg or sources)"
    output = record.get("output")
    recorded = output.get("sha256") if isinstance(output, Mapping) else None
    if not is_digest(recorded) or (sha256 is not None and recorded != sha256):
        return "its recorded checksum does not match"
    return None


__all__ = [
    "KEY_VERSION",
    "canonical_digest",
    "is_digest",
    "is_job_id",
    "mismatch",
    "record_provenance",
]
