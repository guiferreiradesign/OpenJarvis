"""Read-only probe of a locally running VoiceStudio (not an adapter).

Run it once VoiceStudio is installed and started::

    python -m jarvis_ext.speech.voicestudio_probe [--url http://127.0.0.1:3900]

It records what the service really does so the adapter can be written against
facts: discovery document, liveness candidates, voices, and one short WAV
synthesis (format, sample rate, duration, non-silence, cold/warm latency).
Loopback only; no Origin header; sends nothing but a fixed test sentence; no
credentials are read or printed. Standard library only.
"""

from __future__ import annotations

import argparse
import io
import json
import struct
import sys
import time
import urllib.error
import urllib.request
import wave
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

DEFAULT_URL = "http://127.0.0.1:3900"
TEST_TEXT = "This is a short JARVIS voice test."
_LOOPBACK = {"127.0.0.1", "::1", "localhost"}


def _request(
    url: str, *, body: Optional[dict] = None, timeout: float = 10.0
) -> Dict[str, Any]:
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if body is not None else {}
    req = urllib.request.Request(url, data=data, headers=headers)
    started = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read()
            return {
                "status": resp.status,
                "headers": dict(resp.headers),
                "body": payload,
                "seconds": time.perf_counter() - started,
            }
    except urllib.error.HTTPError as exc:
        return {
            "status": exc.code,
            "headers": dict(exc.headers),
            "body": exc.read()[:2000],
            "seconds": time.perf_counter() - started,
        }
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return {"status": None, "error": f"{type(exc).__name__}", "seconds": 0.0}


def analyse_wav(data: bytes) -> Dict[str, Any]:
    """Validate WAV bytes: header, format, duration and non-silence."""
    try:
        with wave.open(io.BytesIO(data)) as wav:
            frames = wav.readframes(wav.getnframes())
            rate, width, channels = (
                wav.getframerate(),
                wav.getsampwidth(),
                wav.getnchannels(),
            )
            count = wav.getnframes()
    except (wave.Error, EOFError) as exc:
        return {"valid": False, "error": str(exc)}
    peak = 0
    if width == 2 and frames:
        samples = struct.unpack(f"<{len(frames) // 2}h", frames)
        peak = max(abs(s) for s in samples)
    duration = count / rate if rate else 0.0
    return {
        "valid": True,
        "sample_rate": rate,
        "bits": width * 8,
        "channels": channels,
        "duration_s": round(duration, 3),
        "peak_amplitude": peak,
        "non_silent": peak > 200 if width == 2 else None,
    }


def probe(base_url: str = DEFAULT_URL) -> Dict[str, Any]:
    host = urlparse(base_url).hostname or ""
    if host not in _LOOPBACK:
        raise ValueError("only loopback URLs are probed")
    base = base_url.rstrip("/")
    report: Dict[str, Any] = {"url": base}

    discovery = _request(f"{base}/.well-known/voicestudio-speech", timeout=5)
    report["reachable"] = discovery["status"] is not None
    if not report["reachable"]:
        report["error"] = "not reachable; is VoiceStudio running?"
        return report
    doc: Dict[str, Any] = {}
    if discovery["status"] == 200:
        try:
            doc = json.loads(discovery["body"])
        except ValueError:
            doc = {}
    report["discovery"] = {
        "status": discovery["status"],
        "protocol": doc.get("protocol"),
        "service_version": doc.get("service_version"),
        "endpoint_names": sorted((doc.get("endpoints") or {}).keys()),
        "advertises_tts": any("speech" in k or "tts" in k for k in doc.get("endpoints", {})),
        "seconds": round(discovery["seconds"], 3),
    }
    health = _request(f"{base}/health", timeout=5)
    report["health"] = {"status": health["status"], "seconds": round(health["seconds"], 3)}

    voices = _request(f"{base}/v1/audio/voices", timeout=10)
    names: List[str] = []
    if voices["status"] == 200:
        try:
            names = [v.get("name", "") for v in json.loads(voices["body"]).get("voices", [])]
        except ValueError:
            pass
    report["voices"] = {"status": voices["status"], "count": len(names), "sample": names[:5]}

    synth: Dict[str, Any] = {}
    for label in ("cold", "warm"):
        res = _request(
            f"{base}/v1/audio/speech",
            body={"model": "omnivoice", "input": TEST_TEXT, "voice": "default",
                  "response_format": "wav"},
            timeout=300,
        )
        entry: Dict[str, Any] = {"status": res["status"], "seconds": round(res["seconds"], 2)}
        if res["status"] == 200:
            entry["content_type"] = res["headers"].get("Content-Type")
            entry["wav"] = analyse_wav(res["body"])
        else:
            entry["error"] = res.get("error") or res["body"][:300].decode("utf-8", "replace")
            entry["retry_after"] = (res.get("headers") or {}).get("Retry-After")
        synth[label] = entry
        if res["status"] != 200:
            break
    report["synthesis"] = synth
    return report


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="voicestudio_probe")
    parser.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args(argv)
    try:
        report = probe(args.url)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(json.dumps(report, indent=2))
    ok = report.get("synthesis", {}).get("cold", {}).get("wav", {}).get("valid")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
