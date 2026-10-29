"""Group D: the configured AI provider ("brain"), without changing it.

These checks read the configuration the way ``jarvis serve`` does, build the
engine with the same factory, and prove what request *would* be sent by
intercepting it in-process.  A socket-level guard blocks any non-loopback
connection while they run, so they cannot reach a provider, use quota or bill.

A live request is opt-in (``--live-brain``) and makes exactly one tiny call.
Without the flag the live check is reported as NOT RUN, never as a pass.
"""

from __future__ import annotations

import contextlib
import json
import socket
import time
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import urlparse

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

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


class NetworkBlocked(RuntimeError):
    """Raised when something tries to open a non-loopback connection."""


@contextlib.contextmanager
def no_outbound_network() -> Iterator[None]:
    """Refuse every non-loopback ``socket.connect`` while active."""
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def check_address(address: Any) -> None:
        host = address[0] if isinstance(address, tuple) and address else address
        if isinstance(host, str) and host in _LOOPBACK:
            return
        if isinstance(address, str):  # AF_UNIX path
            return
        raise NetworkBlocked(f"outbound connection to {host!r} blocked by preflight")

    def guarded_connect(self: socket.socket, address: Any) -> Any:
        check_address(address)
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: Any) -> Any:
        check_address(address)
        return real_connect_ex(self, address)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.socket.connect_ex = guarded_connect_ex  # type: ignore[method-assign]
    try:
        yield
    finally:
        socket.socket.connect = real_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = real_connect_ex  # type: ignore[method-assign]


def _brain_settings(ctx: Context):
    config = ctx.shared.get("core_config")
    if config is None:
        from openjarvis.core.config import load_config

        config = load_config(ctx.config_path)
        ctx.shared["core_config"] = config
    # Same precedence as ``jarvis serve``: command-line option, then
    # [server] model, then [intelligence] default_model.
    server_model = getattr(config.server, "model", "") or ""
    model = (
        ctx.model_override or server_model or config.intelligence.default_model or ""
    )
    return config, ctx.engine_override or config.engine.default, model


def _selection_source(ctx: Context) -> str:
    if ctx.engine_override:
        return "--engine override: authoritative, never substituted"
    return "[engine] default from the configuration"


def _api_key_env() -> Dict[str, Optional[str]]:
    """Provider keys configured on this machine, as ``serve`` would see them."""
    return {
        name: value
        for name, value in known_secret_values().items()
        if name.endswith("_API_KEY")
    }


def _register_engines() -> None:
    import openjarvis.engine  # noqa: F401  (registers the built-in engines)


def _is_remote(url: str) -> bool:
    host = urlparse(url if "//" in url else f"//{url}").hostname or ""
    return bool(host) and host not in _LOOPBACK


def _build_engine(config: Any, key: str) -> Any:
    from openjarvis.engine._discovery import _make_engine

    return _make_engine(key, config)


@check("brain.config", "brain", "Configured brain and provider selection", budget=3)
def brain_config(ctx: Context) -> Outcome:
    from openjarvis.core.registry import EngineRegistry

    _register_engines()
    config, key, model = _brain_settings(ctx)
    keys = EngineRegistry.keys()
    if key not in keys:
        return failed(
            f"[engine] default = {key!r} is not a registered engine "
            f"(registered: {', '.join(sorted(keys))})"
        )
    ctx.shared["brain"] = {"engine": key, "model": model}
    where = "model not configured"
    if model:
        where = f"model {model!r}"
    fallback = (
        "no fallback"
        if ctx.engine_override
        else "fallback may cross local/cloud"
        if config.engine.allow_cross_boundary_fallback
        else "fallback stays on the same local/cloud side"
    )
    detail = (
        f"engine {key!r} ({_selection_source(ctx)}; {fallback}), {where}, "
        f"agent {config.server.agent!r}; "
        f"{len(keys)} engine providers registered (selection unchanged)"
    )
    if not model:
        return warned(
            detail + ". No default model: 'jarvis serve' will pick the first model "
            "the engine lists. If the running server was started with --engine/"
            "--model, pass the same options to this command"
        )
    return passed(detail)


@check(
    "brain.credentials",
    "brain",
    "Provider credentials are present (not shown)",
    budget=5,
)
def brain_credentials(ctx: Context) -> Outcome:
    _register_engines()
    config, key, model = _brain_settings(ctx)
    env = _api_key_env()
    present = sorted(env)
    with temporary_env(env):
        try:
            engine = _build_engine(config, key)
        except Exception as exc:  # noqa: BLE001
            return failed(
                f"engine {key!r} could not be constructed: {type(exc).__name__}: {exc}"
            )
        try:
            host = getattr(engine, "_host", "")
            if key == "cloud":
                if not engine.health():
                    return failed(
                        "engine 'cloud' has no usable provider client. Keys found "
                        f"in env/credentials: {', '.join(present) or 'none'}; the "
                        "provider SDK may also be missing ('--extra inference-cloud')"
                    )
                if model and not engine.can_serve(model):
                    return failed(
                        f"engine 'cloud' cannot route model {model!r}: no provider "
                        "client for it (missing key or SDK)"
                    )
                head = (
                    f"cloud engine can serve {model!r}"
                    if model
                    else "cloud engine ready"
                )
                return passed(f"{head}; key variable(s) present: {', '.join(present)}")
            if key == "nim":
                has_key = bool(env.get("NIM_API_KEY"))
                if _is_remote(host) and not has_key:
                    return failed(
                        "engine 'nim' points at a hosted endpoint but NIM_API_KEY "
                        "is not set"
                    )
                return passed(
                    f"nim endpoint {urlparse(host).hostname}; NIM_API_KEY "
                    + ("present" if has_key else "not needed for this endpoint")
                )
            if host and _is_remote(host):
                return passed(
                    f"engine {key!r} uses remote host {urlparse(host).hostname}; "
                    "key variables present: " + (", ".join(present) or "none")
                )
            return passed(
                f"engine {key!r} is local or in-process; no credential needed"
            )
        finally:
            with contextlib.suppress(Exception):
                engine.close()


class _Recorder:
    """Collects the requests httpx would send and answers with canned replies."""

    def __init__(self, model: str) -> None:
        self.model = model
        self.requests: List[Dict[str, Any]] = []

    def reply(self, request: Any) -> Any:
        import httpx

        path = request.url.path
        try:
            body = json.loads(request.content or b"{}")
        except ValueError:
            body = {}
        headers = {k.lower() for k in request.headers.keys()}
        self.requests.append(
            {
                "method": request.method,
                "host": request.url.host,
                "scheme": request.url.scheme,
                "path": path,
                "has_auth": bool(
                    headers & {"authorization", "x-api-key", "x-goog-api-key"}
                ),
                "body_model": body.get("model") if isinstance(body, dict) else None,
                "messages": len(body.get("messages", []) or [])
                if isinstance(body, dict)
                else 0,
                "max_tokens": body.get("max_tokens")
                if isinstance(body, dict)
                else None,
            }
        )
        if path.endswith("/chat/completions"):
            payload: Dict[str, Any] = {
                "id": "preflight",
                "object": "chat.completion",
                "created": 0,
                "model": self.model,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": "OK"},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1,
                    "completion_tokens": 1,
                    "total_tokens": 2,
                },
            }
        elif path.endswith("/messages"):
            payload = {
                "id": "preflight",
                "type": "message",
                "role": "assistant",
                "model": self.model,
                "content": [{"type": "text", "text": "OK"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 1, "output_tokens": 1},
            }
        elif path.endswith("/api/chat"):
            payload = {
                "model": self.model,
                "message": {"role": "assistant", "content": "OK"},
                "done": True,
            }
        else:
            return httpx.Response(
                404, json={"error": "preflight: no canned reply"}, request=request
            )
        return httpx.Response(200, json=payload, request=request)


@contextlib.contextmanager
def intercepted_http(recorder: _Recorder) -> Iterator[None]:
    """Route every synchronous httpx request to *recorder* (no network at all)."""
    import httpx

    original = httpx.Client.send

    def send(self: Any, request: Any, **kwargs: Any) -> Any:
        return recorder.reply(request)

    httpx.Client.send = send  # type: ignore[method-assign]
    try:
        yield
    finally:
        httpx.Client.send = original  # type: ignore[method-assign]


@check(
    "brain.request",
    "brain",
    "Request is built correctly, offline (nothing sent)",
    budget=5,
)
def brain_request(ctx: Context) -> Outcome:
    from openjarvis.core.types import Message, Role

    _register_engines()
    config, key, model = _brain_settings(ctx)
    if not model:
        return not_run(
            "no default model is configured, so there is no request to build "
            "(set [intelligence] default_model or [server] model)"
        )
    recorder = _Recorder(model)
    with temporary_env(_api_key_env()), no_outbound_network():
        try:
            engine = _build_engine(config, key)
        except Exception as exc:  # noqa: BLE001
            return failed(
                f"engine {key!r} could not be constructed: {type(exc).__name__}: {exc}"
            )
        error = ""
        try:
            with intercepted_http(recorder):
                engine.generate(
                    [Message(role=Role.USER, content="preflight request shape check")],
                    model=model,
                    max_tokens=8,
                    temperature=0,
                )
        except NetworkBlocked as exc:
            return failed(f"the engine tried to leave the machine: {exc}")
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}"
        finally:
            with contextlib.suppress(Exception):
                engine.close()
    if not recorder.requests:
        return failed(
            "the engine made no HTTP request through httpx, so the request shape "
            "could not be verified" + (f" ({error})" if error else "")
        )
    sent = recorder.requests[0]
    problems = []
    if sent["method"] != "POST":
        problems.append(f"method {sent['method']}")
    if not sent["body_model"]:
        problems.append("request body has no model")
    if sent["messages"] < 1:
        problems.append("request body has no messages")
    remote = sent["host"] not in _LOOPBACK
    if remote and not sent["has_auth"]:
        problems.append("hosted endpoint but the request carries no credential")
    if problems:
        return failed(
            f"request to {sent['host']}{sent['path']}: " + "; ".join(problems)
        )
    where = "hosted" if remote else "local"
    note = (
        "; canned reply parsed"
        if not error
        else f"; reply not parsed ({error}), shape only"
    )
    return passed(
        f"{where} POST {sent['scheme']}://{sent['host']}{sent['path']} "
        f"model={sent['body_model']!r} messages={sent['messages']} "
        f"credential={'attached' if sent['has_auth'] else 'none'}{note}. "
        "Nothing was sent; this is NOT a connectivity test"
    )


@check(
    "brain.free_policy",
    "brain",
    "Free-model-only policy is configured and enforced (offline)",
    budget=5,
)
def brain_free_policy(ctx: Context) -> Outcome:
    """Exercise the real cloud engine with fake clients: nothing is sent.

    This proves the code and the configuration on disk. It says nothing about
    a server that is already running; that process adopted the policy only if
    it was started after the change.
    """
    from types import SimpleNamespace

    from openjarvis.engine.cloud import CloudEngine
    from openjarvis.engine.openrouter_policy import OpenRouterModelNotAllowed

    config, key, model = _brain_settings(ctx)
    section = config.engine.openrouter
    approved = sorted(section.approved_free_models)
    if not section.free_only:
        return warned(
            "[engine.openrouter] free_only is false: any model name is "
            "accepted, including paid ones. Nothing was tested"
        )
    if not approved:
        return failed(
            "free_only is true but approved_free_models is empty: every "
            "cloud model would be refused"
        )

    calls: List[str] = []

    class _Recorder:
        def __init__(self, name: str) -> None:
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create)
            )
            self._name = name

        def _create(self, **kwargs: Any) -> Any:
            calls.append(f"{self._name}:{kwargs.get('model')}")
            raise RuntimeError("recorded; no request was made")

    blank = {name: None for name in _api_key_env()}
    blank.update({k: None for k in ("OPENROUTER_API_KEY", "OPENAI_API_KEY")})
    with temporary_env(blank), no_outbound_network():
        _register_engines()
        engine = _build_engine(config, "cloud")
        if not isinstance(engine, CloudEngine) or engine.openrouter_policy is None:
            return failed(
                "the engine built from this configuration carries no free-model "
                "policy, so paid models would not be refused"
            )
        for attr in [a for a in vars(engine) if a.endswith("_client")]:
            setattr(engine, attr, _Recorder(attr))
        from openjarvis.core.types import Message, Role

        probe = [Message(role=Role.USER, content="x")]
        rejected = ["gpt-4o", "claude-sonnet-4-6", "openrouter/auto", "no-such-model"]
        rejected.append(approved[0].replace(":free", ""))
        for name in rejected:
            try:
                engine.generate(probe, model=name)
            except OpenRouterModelNotAllowed:
                continue
            except Exception as exc:  # noqa: BLE001
                return failed(
                    f"model {name!r} was not refused by the policy "
                    f"({type(exc).__name__})"
                )
            return failed(f"model {name!r} was accepted although it is not approved")
        if calls:
            return failed(f"a client was called for a refused model: {calls}")
        try:
            engine.generate(probe, model=approved[0])
        except OpenRouterModelNotAllowed:
            return failed(f"approved model {approved[0]!r} was refused")
        except Exception:  # noqa: BLE001  (the recorder raises on purpose)
            pass
        if calls != [f"_openrouter_client:{approved[0].removeprefix('openrouter/')}"]:
            return failed(f"approved model reached an unexpected client: {calls}")

    detail = (
        f"free_only on; approved: {', '.join(approved)}; {len(rejected)} "
        "unapproved names refused before any client call; approved model "
        "reaches only the OpenRouter client. Verified on code + configuration "
        "only: a server started before this change has not been shown to use it"
    )
    if key == "cloud" and model and model not in approved:
        return failed(
            f"the configured model {model!r} is not in approved_free_models, so "
            "every request to it would be refused. " + detail
        )
    return passed(detail)


@check("brain.live", "brain", "Live provider round-trip (opt-in)", budget=30)
def brain_live(ctx: Context) -> Outcome:
    if not ctx.live_brain:
        return not_run(
            "opt-in, NOT TESTED. The provider has not been contacted. "
            "--live-brain sends exactly one tiny request (max 8 tokens) that may "
            "count against a free-tier quota or bill a paid account"
        )
    from openjarvis.core.types import Message, Role

    _register_engines()
    config, key, model = _brain_settings(ctx)
    if not model:
        return failed("--live-brain needs a configured default model")
    with temporary_env(_api_key_env()):
        try:
            engine = _build_engine(config, key)
        except Exception as exc:  # noqa: BLE001
            return failed(
                f"engine {key!r} could not be constructed: {type(exc).__name__}: {exc}"
            )
        started = time.perf_counter()
        try:
            reply = engine.generate(
                [Message(role=Role.USER, content="Reply with the single word OK.")],
                model=model,
                max_tokens=8,
                temperature=0,
            )
        except Exception as exc:  # noqa: BLE001
            return failed(f"live request failed: {type(exc).__name__}: {exc}"[:300])
        finally:
            with contextlib.suppress(Exception):
                engine.close()
    elapsed = time.perf_counter() - started
    text = str(reply.get("content", "")).strip()
    if not text:
        return failed(
            "the provider answered without any text "
            f"(finish_reason={reply.get('finish_reason')})"
        )
    usage = reply.get("usage", {})
    return passed(
        f"1 live request to engine {key!r} model {model!r} answered in {elapsed:.2f} s "
        f"({usage.get('total_tokens', '?')} tokens)"
    )
