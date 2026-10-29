"""Preflight core: results, the check registry, the runner and the report.

A *check* is a function ``(Context) -> Outcome`` registered with
:func:`check`.  The runner times every check that actually executes, turns an
exception into a ``FAIL`` (never into a pass), downgrades a pass that blew its
latency budget to ``WARN``, and redacts any known secret value from what it
prints.  Nothing here knows about video, jobs or AI providers; those live in
``jarvis_ext.preflight.checks``.

Statuses
--------
``PASS``  the component was exercised and behaved correctly.
``FAIL``  a component that exists is broken, or a *required* one is missing.
          Any FAIL makes the command exit with status 1.
``WARN``  not a failure of what exists: an optional component is absent, a
          check could not run (``NOT RUN``), a feature is not implemented yet,
          or a check passed but exceeded its latency budget.
"""

from __future__ import annotations

import contextlib
import os
import platform
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Optional, Sequence

PASS = "PASS"
FAIL = "FAIL"
WARN = "WARN"
STATUSES = (PASS, FAIL, WARN)

# Group id -> heading, in report order.
GROUPS: Dict[str, str] = {
    "app": "A. Application and configuration",
    "media": "B. Media inspection and video rendering",
    "jobs": "C. Job management",
    "brain": "D. AI provider (brain)",
    "future": "E. Voice services and planned capabilities (missing ones are warnings)",
}


@dataclass(frozen=True)
class Outcome:
    """What a check reports.  *ran* is False when nothing was exercised."""

    status: str
    detail: str = ""
    ran: bool = True

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"Unknown preflight status {self.status!r}")


def passed(detail: str = "") -> Outcome:
    return Outcome(PASS, detail)


def failed(detail: str) -> Outcome:
    return Outcome(FAIL, detail)


def warned(detail: str) -> Outcome:
    return Outcome(WARN, detail)


def not_run(reason: str) -> Outcome:
    """A check that could not or did not execute.  Never counts as a pass."""
    return Outcome(WARN, f"NOT RUN: {reason}", ran=False)


class Blocked(Exception):
    """Raised by a check whose prerequisite did not hold; reported as NOT RUN."""


@dataclass
class Check:
    id: str
    group: str
    title: str
    func: Callable[["Context"], Outcome]
    budget_seconds: Optional[float] = None


_REGISTRY: List[Check] = []


def check(
    check_id: str,
    group: str,
    title: str,
    *,
    budget: Optional[float] = None,
) -> Callable[[Callable[["Context"], Outcome]], Callable[["Context"], Outcome]]:
    """Register a check.  *budget* is the latency budget in seconds."""
    if group not in GROUPS:
        raise ValueError(f"Unknown preflight group {group!r}")

    def register(func: Callable[["Context"], Outcome]):
        if any(c.id == check_id for c in _REGISTRY):
            raise ValueError(f"Duplicate preflight check id {check_id!r}")
        _REGISTRY.append(Check(check_id, group, title, func, budget))
        return func

    return register


def registered_checks() -> List[Check]:
    """All checks, built-in ones loaded, ordered by group then registration."""
    from jarvis_ext.preflight.checks import load_builtin_checks

    load_builtin_checks()
    order = {g: i for i, g in enumerate(GROUPS)}
    return sorted(_REGISTRY, key=lambda c: order[c.group])  # stable


@dataclass
class Context:
    """Per-run state shared by the checks.

    *workdir* is a private temporary folder; everything a check generates
    goes there and is deleted afterwards.  Checks hand results to later
    checks through :attr:`shared`.
    """

    workdir: Path
    config_path: Optional[Path] = None
    live_brain: bool = False
    spawn_server: bool = False
    # ``jarvis serve --engine/--model`` equivalents; they win over config.toml
    # exactly as they do for the real command.
    engine_override: Optional[str] = None
    model_override: Optional[str] = None
    shared: Dict[str, Any] = field(default_factory=dict)

    def need(self, key: str, because: str) -> Any:
        """Return ``shared[key]`` or raise :class:`Blocked` with *because*."""
        if key not in self.shared:
            raise Blocked(because)
        return self.shared[key]


# -- secrets -----------------------------------------------------------------

_SECRET_NAME = re.compile(
    r"(API_KEY|APIKEY|TOKEN|SECRET|PASSWORD|PASSWD|PRIVATE_KEY|CREDENTIAL)", re.I
)
_MIN_SECRET_LENGTH = 8


def known_secret_values(config_dir: Optional[Path] = None) -> Dict[str, str]:
    """Secret values this machine is configured with, as ``{label: value}``.

    Sources: environment variables whose names look like secrets, the
    OpenJarvis ``credentials.toml`` and ``cloud-keys.env``.  The files are read
    without any side effect (no rewriting, no ``os.environ`` changes).  Values
    exist only so output can be redacted and leaks detected; they are never
    printed.
    """
    found: Dict[str, str] = {}
    for name, value in os.environ.items():
        if _SECRET_NAME.search(name) and len(value) >= _MIN_SECRET_LENGTH:
            found[name] = value
    try:
        if config_dir is None:
            from openjarvis.core.paths import get_config_dir

            config_dir = get_config_dir()
        creds = Path(config_dir) / "credentials.toml"
        if creds.is_file():
            try:
                import tomllib
            except ModuleNotFoundError:  # Python 3.10
                import tomli as tomllib  # type: ignore[no-redef]
            with open(creds, "rb") as fh:
                data = tomllib.load(fh)
            for section, table in data.items():
                if isinstance(table, Mapping):
                    for key, value in table.items():
                        if isinstance(value, str) and len(value) >= _MIN_SECRET_LENGTH:
                            found.setdefault(str(key), value)
        env_file = Path(config_dir) / "cloud-keys.env"
        if env_file.is_file():
            for raw in env_file.read_text(encoding="utf-8").splitlines():
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    value = value.strip().strip("'\"")
                    if len(value) >= _MIN_SECRET_LENGTH:
                        found.setdefault(key.strip(), value)
    except Exception:  # noqa: BLE001 - redaction must never break the run
        pass
    return found


def redact(text: str, secrets: Mapping[str, str]) -> str:
    """Replace every known secret value in *text* with ``***``."""
    for value in sorted(set(secrets.values()), key=len, reverse=True):
        if value:
            text = text.replace(value, "***")
    return text


@contextlib.contextmanager
def temporary_env(values: Mapping[str, Optional[str]]) -> Iterator[None]:
    """Set (or, for ``None``, remove) environment variables, then restore them."""
    saved = {k: os.environ.get(k) for k in values}
    try:
        for key, value in values.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# -- running -----------------------------------------------------------------


@dataclass
class Result:
    id: str
    group: str
    title: str
    status: str
    detail: str
    seconds: Optional[float]  # None: the check did not execute
    budget_seconds: Optional[float] = None


def run_check(item: Check, ctx: Context, secrets: Mapping[str, str]) -> Result:
    started = time.perf_counter()
    ran = True
    try:
        outcome = item.func(ctx)
        if not isinstance(outcome, Outcome):
            outcome = failed(f"check returned {type(outcome).__name__}, not an Outcome")
    except Blocked as exc:
        outcome = not_run(str(exc))
    except Exception as exc:  # noqa: BLE001 - a crashing check is a FAIL
        outcome = failed(f"{type(exc).__name__}: {exc}")
    elapsed = time.perf_counter() - started
    ran = outcome.ran
    status, detail = outcome.status, outcome.detail
    if (
        ran
        and status == PASS
        and item.budget_seconds is not None
        and elapsed > item.budget_seconds
    ):
        status = WARN
        detail = (
            f"{detail} - passed, but {elapsed:.2f} s is over the "
            f"{item.budget_seconds:g} s latency budget"
        ).strip(" -")
    return Result(
        id=item.id,
        group=item.group,
        title=item.title,
        status=status,
        detail=redact(detail, secrets),
        seconds=elapsed if ran else None,
        budget_seconds=item.budget_seconds,
    )


@dataclass
class Report:
    results: List[Result]
    started_at: str
    total_seconds: float
    python: str
    platform: str
    config_found: Optional[bool] = None

    def counts(self) -> Dict[str, int]:
        return {s: sum(1 for r in self.results if r.status == s) for s in STATUSES}

    @property
    def exit_code(self) -> int:
        return 1 if self.counts()[FAIL] else 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "started_at": self.started_at,
            "python": self.python,
            "platform": self.platform,
            "config_file_found": self.config_found,
            "total_seconds": round(self.total_seconds, 3),
            "counts": self.counts(),
            "exit_code": self.exit_code,
            "results": [
                {
                    "id": r.id,
                    "group": r.group,
                    "status": r.status,
                    "title": r.title,
                    "detail": r.detail,
                    "seconds": None if r.seconds is None else round(r.seconds, 4),
                    "budget_seconds": r.budget_seconds,
                }
                for r in self.results
            ],
        }

    def render(self) -> str:
        lines = [
            f"JARVIS preflight  {self.started_at}  python {self.python}  "
            f"{self.platform}"
        ]
        if self.config_found is not None:
            lines.append(
                "config.toml: " + ("found" if self.config_found else "NOT found")
            )
        width = max((len(r.id) for r in self.results), default=10)
        current = None
        for r in self.results:
            if r.group != current:
                current = r.group
                lines += ["", GROUPS[r.group]]
            timing = "    -   " if r.seconds is None else f"{r.seconds:7.3f}s"
            lines.append(f"  {r.status}  {r.id:<{width}}  {timing}  {r.title}")
            if r.detail:
                lines.append(f"        {r.detail}")
        counts = self.counts()
        lines += [
            "",
            f"Summary: {counts[PASS]} PASS, {counts[FAIL]} FAIL, {counts[WARN]} WARN"
            f"  ({len(self.results)} checks, {self.total_seconds:.2f} s)",
        ]
        failures = [r for r in self.results if r.status == FAIL]
        if failures:
            lines.append("Failed: " + ", ".join(r.id for r in failures))
        lines.append(
            "Result: "
            + (
                "FAILED (exit 1)"
                if failures
                else "no failures (exit 0); WARN items were not verified as working"
                if counts[WARN]
                else "all checks passed (exit 0)"
            )
        )
        lines.append(
            "Note: this covers only the checks listed above, not the whole of JARVIS."
        )
        return "\n".join(lines)


def run_preflight(
    *,
    config_path: Optional[Path] = None,
    groups: Optional[Sequence[str]] = None,
    ids: Optional[Sequence[str]] = None,
    live_brain: bool = False,
    spawn_server: bool = False,
    engine: Optional[str] = None,
    model: Optional[str] = None,
    checks: Optional[Sequence[Check]] = None,
) -> Report:
    """Run the selected checks and return the report.

    *groups* / *ids* filter the checks (``None`` = all).  *checks* replaces the
    registry, for tests.
    """
    selected = list(checks) if checks is not None else registered_checks()
    if groups:
        unknown = sorted(set(groups) - set(GROUPS))
        if unknown:
            raise ValueError(f"Unknown group(s): {', '.join(unknown)}")
        selected = [c for c in selected if c.group in groups]
    if ids:
        known = {c.id for c in selected}
        unknown = sorted(set(ids) - known)
        if unknown:
            raise ValueError(f"Unknown check id(s): {', '.join(unknown)}")
        selected = [c for c in selected if c.id in ids]

    secrets = known_secret_values()
    try:
        from jarvis_ext.config import resolve_config_path

        config_found: Optional[bool] = resolve_config_path(config_path).is_file()
    except Exception:  # noqa: BLE001
        config_found = None
    started_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    t0 = time.perf_counter()
    results: List[Result] = []
    with tempfile.TemporaryDirectory(prefix="jarvis-preflight-") as tmp:
        ctx = Context(
            workdir=Path(tmp),
            config_path=config_path,
            live_brain=live_brain,
            spawn_server=spawn_server,
            engine_override=engine or None,
            model_override=model or None,
        )
        for item in selected:
            results.append(run_check(item, ctx, secrets))
    return Report(
        results=results,
        started_at=started_at,
        total_seconds=time.perf_counter() - t0,
        python=platform.python_version(),
        platform=f"{sys.platform}/{platform.machine()}",
        config_found=config_found,
    )


__all__ = [
    "Blocked",
    "Check",
    "Context",
    "FAIL",
    "GROUPS",
    "Outcome",
    "PASS",
    "Report",
    "Result",
    "WARN",
    "check",
    "failed",
    "known_secret_values",
    "not_run",
    "passed",
    "redact",
    "registered_checks",
    "run_check",
    "run_preflight",
    "temporary_env",
    "warned",
]
