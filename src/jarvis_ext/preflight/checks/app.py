"""Group A: application start-up, configuration and secret hygiene."""

from __future__ import annotations

import importlib
import os
import re
import secrets as _secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from jarvis_ext.preflight.core import (
    Context,
    Outcome,
    check,
    failed,
    known_secret_values,
    not_run,
    passed,
    temporary_env,
    warned,
)

# Names whose values the secret probe plants in the environment.  Real values
# already configured on this machine are searched for as well.
_CANARY_NAMES = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "OPENROUTER_API_KEY",
    "NIM_API_KEY",
)

# Shapes of real provider credentials.  A match in served or shipped frontend
# code is reported by file and pattern name only, never by value.
SECRET_PATTERNS: Dict[str, "re.Pattern[str]"] = {
    "OpenAI/Anthropic-style key": re.compile(r"sk-[A-Za-z0-9_-]{20,}"),
    "NVIDIA key": re.compile(r"nvapi-[A-Za-z0-9_-]{20,}"),
    "Google API key": re.compile(r"AIza[0-9A-Za-z_-]{35}"),
    "GitHub token": re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}"),
    "Slack token": re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"),
    "private key block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

# Settings sections read by JARVIS, with how to validate each.
_SECTIONS = (
    "media_workspace",
    "media_render",
    "jobs",
    "speech_input",
    "speech_output",
    "voicestudio",
)
_MAX_SCAN_BYTES = 8 * 1024 * 1024


def _section_loaders(config_path: Optional[Path]) -> List[Tuple[str, Any]]:
    from jarvis_ext.config import load_extension_section
    from jarvis_ext.jobs.settings import JobSettings
    from jarvis_ext.speech.settings import (
        SpeechInputSettings,
        SpeechOutputSettings,
        VoiceStudioSettings,
    )
    from jarvis_ext.tools.media_workspace import MediaWorkspaceSettings
    from jarvis_ext.video.settings import MediaRenderSettings

    return [
        (
            "media_workspace",
            lambda: MediaWorkspaceSettings.from_mapping(
                load_extension_section("media_workspace", config_path)
            ),
        ),
        (
            "media_render",
            lambda: MediaRenderSettings.from_mapping(
                load_extension_section("media_render", config_path)
            ),
        ),
        ("jobs", lambda: JobSettings.load(config_path)),
        ("speech_input", lambda: SpeechInputSettings.load(config_path)),
        ("speech_output", lambda: SpeechOutputSettings.load(config_path)),
        ("voicestudio", lambda: VoiceStudioSettings.load(config_path)),
    ]


@check("app.rust_ext", "app", "Compiled Rust extension (required) imports", budget=2)
def rust_extension(ctx: Context) -> Outcome:
    try:
        module = importlib.import_module("openjarvis_rust")
    except ImportError:
        return failed(
            "openjarvis_rust is not installed. It is required (no Python fallback): "
            "security policy, rate limiting and the executor load it. Build it with "
            "'uv run maturin develop --release --manifest-path "
            "rust/crates/openjarvis-python/Cargo.toml' (jarvis/README.md)."
        )
    return passed(f"openjarvis_rust {getattr(module, '__version__', '')}".strip())


@check("app.config", "app", "Configuration loads and validates", budget=3)
def configuration(ctx: Context) -> Outcome:
    from jarvis_ext.config import (
        ExtensionConfigError,
        resolve_config_path,
    )
    from openjarvis.core.config import load_config

    path = resolve_config_path(ctx.config_path)
    exists = path.is_file()
    try:
        config = load_config(ctx.config_path)
    except Exception as exc:  # noqa: BLE001
        return failed(f"core load_config failed: {type(exc).__name__}: {exc}")
    ctx.shared["core_config"] = config

    errors: List[str] = []
    notes: List[str] = []
    for name, loader in _section_loaders(ctx.config_path):
        try:
            loaded = loader()
        except ExtensionConfigError as exc:
            errors.append(f"[jarvis.{name}] {exc}")
            continue
        except Exception as exc:  # noqa: BLE001
            errors.append(f"[jarvis.{name}] {type(exc).__name__}: {exc}")
            continue
        ctx.shared[f"settings.{name}"] = loaded
    if errors:
        return failed("; ".join(errors))

    ws = ctx.shared["settings.media_workspace"]
    out = ctx.shared["settings.media_render"]
    if not exists:
        return warned(
            "no config.toml; core defaults in use and no [jarvis.*] sections "
            "(the fixture-based checks below do not need one)"
        )
    if ws.root is None:
        notes.append("media workspace not configured")
    elif not Path(ws.root).is_dir():
        notes.append("configured media workspace folder is missing (drive unplugged?)")
    if getattr(out, "output_root", None) is None:
        notes.append("render output_root not configured")
    elif not Path(out.output_root).is_dir():
        notes.append("configured render output folder is missing")
    base = "config.toml and 5 [jarvis.*] sections parse"
    if notes:
        return warned(base + "; " + "; ".join(notes))
    return passed(base)


@check("app.registration", "app", "JARVIS tools register with OpenJarvis", budget=2)
def registration(ctx: Context) -> Outcome:
    import jarvis_ext
    from jarvis_ext.tools import _TOOLS
    from openjarvis.core.registry import ToolRegistry

    jarvis_ext.ensure_registered()
    missing = [name for name in _TOOLS if not ToolRegistry.contains(name)]
    if missing:
        return failed("not registered: " + ", ".join(missing))
    return passed(f"{len(_TOOLS)} tools registered: " + ", ".join(sorted(_TOOLS)))


class _OfflineEngine:
    """An engine that answers /health and nothing else; never touches a network."""

    engine_id = "preflight-offline"

    def health(self) -> bool:
        return True

    def list_models(self) -> List[str]:
        return ["preflight-model"]

    def can_serve(self, model: str) -> bool:
        return True

    def close(self) -> None:
        return None

    def generate(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        raise RuntimeError("the preflight engine never generates")

    async def stream(self, *args: Any, **kwargs: Any):  # pragma: no cover
        raise RuntimeError("the preflight engine never generates")
        yield ""  # noqa: B901


def _build_app() -> Any:
    from openjarvis.server.app import create_app

    return create_app(_OfflineEngine(), "preflight-model", engine_name="preflight")


@check("app.server", "app", "Server app starts in-process (stub engine)", budget=10)
def server_starts(ctx: Context) -> Outcome:
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        return failed(
            "fastapi/httpx not installed: the server extra is needed to run "
            "'jarvis serve' ('uv sync --extra server')"
        )
    try:
        app = _build_app()
    except Exception as exc:  # noqa: BLE001
        return failed(f"create_app failed: {type(exc).__name__}: {exc}")
    ctx.shared["server_app_factory"] = _build_app
    with TestClient(app) as client:
        health = client.get("/health")
        info = client.get("/v1/info")
    if health.status_code != 200 or health.json().get("status") != "ok":
        return failed(f"/health returned {health.status_code}: {health.text[:120]}")
    if info.status_code != 200:
        return failed(f"/v1/info returned {info.status_code}")
    return passed(
        "create_app + lifespan ran; /health 200 and /v1/info 200 (uses a stub "
        "engine: this does not exercise your configured brain or 'jarvis serve')"
    )


def _probe_paths(app: Any) -> List[str]:
    paths = [
        "/health",
        "/v1/info",
        "/v1/models",
        "/v1/channels/status",
        "/openapi.json",
        "/",
        "/index.html",
        "/manifest.webmanifest",
        "/sw.js",
        # The server must never serve the user's secret files.
        "/config.toml",
        "/credentials.toml",
        "/cloud-keys.env",
        "/.env",
        "/..%2fcredentials.toml",
        "/assets/..%2f..%2fcredentials.toml",
    ]
    for route in getattr(app, "routes", []):
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", None) or set()
        if (
            "GET" in methods
            and "{" not in path
            and re.search(r"setting|config|cred|key|provider|cloud", path, re.I)
            and path not in paths
        ):
            paths.append(path)
    return paths


@check("app.secrets_served", "app", "Server responses never contain secrets", budget=15)
def secrets_not_served(ctx: Context) -> Outcome:
    try:
        from fastapi.testclient import TestClient
    except ImportError:
        return not_run("fastapi is not installed (see app.server)")
    canary = {n: f"preflight-canary-{_secrets.token_hex(12)}" for n in _CANARY_NAMES}
    real = known_secret_values()
    needles = {**{f"canary {k}": v for k, v in canary.items()}, **real}
    leaks: List[str] = []
    checked = 0
    # The canaries are set before the app is built, so anything that echoes the
    # environment would expose them.
    with temporary_env(dict(canary)):
        try:
            app = _build_app()
        except Exception as exc:  # noqa: BLE001
            return failed(f"create_app failed: {type(exc).__name__}: {exc}")
        with TestClient(app) as client:
            for path in _probe_paths(app):
                response = client.get(path, follow_redirects=False)
                checked += 1
                haystack = response.text + "\n".join(
                    f"{k}: {v}" for k, v in response.headers.items()
                )
                for label, value in needles.items():
                    if value and value in haystack:
                        leaks.append(f"{label} in GET {path}")
    if leaks:
        return failed("secret value served: " + "; ".join(sorted(set(leaks))))
    return passed(
        f"{checked} GET endpoints/paths probed with canary keys planted; none leaked "
        f"({len(real)} real configured secret value(s) were also searched for)"
    )


def _scan_roots() -> List[Tuple[str, Path]]:
    import openjarvis

    package = Path(openjarvis.__file__).resolve().parent
    roots = [("served frontend build", package / "server" / "static")]
    # src/openjarvis -> repo root; absent in an installed wheel.
    repo = package.parent.parent
    if (repo / "frontend" / "src").is_dir():
        roots.append(("frontend source", repo / "frontend" / "src"))
    return roots


def _is_scannable(path: Path) -> bool:
    name = path.name
    if re.search(r"\.(test|spec)\.[jt]sx?$", name) or "__tests__" in path.parts:
        return False
    return path.suffix.lower() in {
        ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".html", ".json", ".css",
        ".map", ".webmanifest", ".toml", ".env", ".txt",
    }  # fmt: skip


@check(
    "app.secrets_frontend", "app", "Frontend code and build hold no secrets", budget=20
)
def secrets_not_in_frontend(ctx: Context) -> Outcome:
    real = known_secret_values()
    findings: List[str] = []
    scanned = 0
    present: List[str] = []
    for label, root in _scan_roots():
        if not root.is_dir():
            continue
        present.append(label)
        for path in root.rglob("*"):
            if not path.is_file() or not _is_scannable(path):
                continue
            try:
                if path.stat().st_size > _MAX_SCAN_BYTES:
                    continue
                text = path.read_bytes().decode("latin-1")
            except OSError:
                continue
            scanned += 1
            rel = f"{label}: {path.relative_to(root)}"
            for name, pattern in SECRET_PATTERNS.items():
                if pattern.search(text):
                    findings.append(f"{rel} matches {name}")
            for key_name, value in real.items():
                if value in text:
                    findings.append(f"{rel} contains the value of {key_name}")
    if not present:
        return not_run(
            "no frontend build (server/static) or source (frontend/src) found to scan"
        )
    if findings:
        return failed("; ".join(findings[:8]) + (" ..." if len(findings) > 8 else ""))
    note = "" if len(present) == 2 else f" (only {present[0]} was available)"
    return passed(f"{scanned} files scanned in {', '.join(present)}; no secrets{note}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@check(
    "app.spawn_server", "app", "Documented 'jarvis serve' starts (opt-in)", budget=90
)
def spawn_server(ctx: Context) -> Outcome:
    if not ctx.spawn_server:
        return not_run(
            "opt-in, not performed. Pass --spawn-server to start the real "
            "'jarvis serve' with your configured brain (it probes your engines, "
            "which can include a network health request, and stops it again)"
        )
    exe = shutil.which("jarvis", path=str(Path(sys.executable).parent)) or shutil.which(
        "jarvis"
    )
    if not exe:
        return failed("the 'jarvis' command is not installed in this environment")
    port = _free_port()
    env = dict(os.environ)
    if ctx.config_path is not None:
        env["OPENJARVIS_CONFIG"] = str(ctx.config_path)
    log = ctx.workdir / "serve.log"
    with open(log, "wb") as fh:
        proc = subprocess.Popen(  # noqa: S603 - fixed argument list
            [
                exe,
                "serve",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                *(["--engine", ctx.engine_override] if ctx.engine_override else []),
                *(["--model", ctx.model_override] if ctx.model_override else []),
            ],
            stdout=fh,
            stderr=subprocess.STDOUT,
            env=env,
        )
    try:
        import httpx

        deadline = time.monotonic() + 80
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                tail = log.read_text(errors="replace").strip().splitlines()[-3:]
                return failed(
                    f"'jarvis serve' exited with status {proc.returncode}: "
                    + " | ".join(t.strip() for t in tail)[:400]
                )
            try:
                reply = httpx.get(f"http://127.0.0.1:{port}/health", timeout=2)
            except httpx.HTTPError:
                time.sleep(0.5)
                continue
            if reply.status_code == 200:
                return passed("'jarvis serve' answered /health on a free local port")
            return failed(f"/health returned {reply.status_code}")
        return failed("'jarvis serve' did not answer /health within 80 s")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
