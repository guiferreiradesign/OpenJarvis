# Voice output plan (VoiceStudio)

Status (2026-10-29): **phase A done.** `VoiceStudioTTSBackend` speaks with
Kokoro George (decision D40). The larger `jarvis_ext/voice/` layer below
(profiles, cache, chunker, `VoiceService`, streaming playback) is still a
plan. Roadmap step 1 ([ROADMAP.md](ROADMAP.md)). Decisions: D39, D40.

## Implemented: VoiceStudio TTS adapter with Kokoro George (2026-10-29)

* `src/jarvis_ext/speech/voicestudio.py`: `VoiceStudioTTSBackend`
  (upstream `TTSBackend`), registered as `TTSRegistry["voicestudio"]`.
  Voicebox is no longer the target; its code stays registered as `voicebox`
  but nothing selects it.
* Selected voice: **Kokoro 82M through MLX-Audio, `bm_george`, British
  English, WAV.** KittenTTS worked but sounded too robotic; George sounded
  better than Daniel in the listening comparison. OmniVoice stays off (its
  licence is non-commercial and was not accepted).
* **Every request names everything**, so it never depends on VoiceStudio's
  active engine: `POST http://127.0.0.1:3900/v1/audio/speech` with
  `{"model": "mlx-audio", "input": ..., "voice": "bm_george", "language":
  "British English", "response_format": "wav", "speed": 1.0}`. A per-call
  `voice_id` or `speed` overrides the configured value; nothing is omitted.
* Settings `[jarvis.voicestudio]` (`settings.py`, `VoiceStudioSettings`):
  `base_url` (loopback only, default `http://127.0.0.1:3900`), `model`
  (`mlx-audio`), `voice` (`bm_george`), `language` (`British English`),
  `speed`, `timeout_seconds` (180, covers a cold model load),
  `connect_timeout_seconds` (2), `max_text_chars` (4000, VoiceStudio's limit
  is 4096). Unknown keys and bad values are errors.
* Server and default: `speech.tts_backend` now defaults to `voicestudio`
  (voice `bm_george`), so `/v1/speech/synthesize` and `/v1/speech/tts/health`
  use it. `voicestudio` is deliberately **not** in the auto-discovery order
  (`TTS_BACKEND_ORDER`); it is used because it is the configured backend.
* **No cloud fallback:** the adapter talks only to the configured loopback
  URL. When it is down, `get_tts_backend` skips cloud backends (unchanged,
  `allow_cloud_tts_fallback` stays false) and the route answers 501 "No
  text-to-speech backend available"; chat is unaffected.
* Errors: connection refused / connect timeout -> `SpeechUnavailableError`
  ("VoiceStudio is not running..."); read timeout -> `SpeechTimeoutError`;
  403 (licence) -> unavailable with VoiceStudio's message; 429/503 -> one
  retry after `Retry-After` (capped at 5 s), then unavailable; other
  non-200 -> `SpeechResponseError`. Audio must be `RIFF/WAVE`, parse, and be
  non-empty; **sample rate and channels come from the WAV header.**
* Pronunciation: Kokoro spelled the name "J.A.R.V.I.S." letter by letter.
  The spoken copy of the text (only) now has `JARVIS`, `Jarvis`,
  `J.A.R.V.I.S.` and `J-A-R-V-I-S` replaced by the word "Jarvis"
  (`normalize_for_speech`); displayed text is unchanged.
* Not done: cancellation of an in-flight request. The server runs synthesis
  in a worker thread with no cancel hook, so a stopped utterance still
  finishes generating; the existing frontend `stop()` discards the audio.
  Streaming (`/ws/tts`) and chunking remain in the plan below.
* Verification: 47 mocked-HTTP tests in
  `tests/jarvis_ext/test_speech_voicestudio.py`; live checks against the
  local VoiceStudio v0.5.7 are recorded in the 2026-10-29 session report
  (see D40): `speech-check` reports the service running with the settings
  above, and `speech-say` returned a 24000 Hz mono 16-bit WAV from
  `bm_george` in about 2.6 s. Pronunciation was checked by duration only
  (plain "Jarvis" is shorter than the all-caps and dotted forms, which are
  spelled); it still needs a listening check by ear.

Facts below are from the earlier planning pass and are kept for reference.

## 0. VoiceStudio: contract verified from source and docs (installed since 2026-10-28)

VoiceStudio = https://github.com/debpalash/VoiceStudio (voicestudio.sh,
AGPL-3.0, Electron app + local Python backend, default engine OmniVoice,
default port **3900** via `OMNIVOICE_PORT`). Status on 2026-10-28: **not
installed on this Mac** and nothing is listening on 3900, so nothing below
has been run. Facts come from `main` (README, `docs/speech-platform.md`,
`docs/api-auth.md`, `docs/install/script.md`, `backend/api/routers/*`).

| Topic | Finding | Status |
|---|---|---|
| TTS endpoint | `POST /v1/audio/speech` (OpenAI-compatible). JSON: `model` (`omnivoice`), `input` (<= 4096 chars), `voice` (`default`, a profile id/name, or preset), `response_format` (**default is `mp3`; send `wav`**), `speed`, `language`, `instructions` | source |
| Not advertised | The discovery document (`/.well-known/voicestudio-speech`) lists only speech *input* endpoints. TTS cannot be discovered from it | source |
| Voices | `GET /v1/audio/voices` -> `{"voices":[{voice_id,id,name,type,...}],"engines":[...]}`; 200 with only the 13 OpenAI aliases if profiles cannot be read | source |
| Models | `GET /v1/models` -> OpenAI list shape incl. TTS engines | source |
| WAV | `Content-Type: audio/wav`, `Content-Length` set. Sample rate = the **engine's native rate** (not fixed), bit depth/channels not forced by the router. The adapter must read them from the WAV header, never assume 24 kHz | source; to measure |
| Errors | JSON `{"error":{message,type,param,code}}`. 400 `model_not_found`/`model_not_available`/`unsupported_response_format`, 403 licence not accepted, 409 `ambiguous_voice`, 429 busy, 503 model-load/generate timeout, 500 generation failure. 429/503 carry `Retry-After` and `X-OmniVoice-Retryable: true` | source |
| First request | May load or download a model; load timeout default **1200 s** (`OMNIVOICE_MODEL_LOAD_TIMEOUT`). The first request after start can take minutes | source |
| Auth | Loopback (`127.0.0.1`, `::1`, `localhost`) needs **no key**; `OMNIVOICE_API_KEY` is enforced for non-loopback only. CSRF checks reject disallowed `Origin`/`Sec-Fetch-Site`; a Python client that sends neither is unaffected. The two speech routes declare no admin dependency | docs + source |
| Liveness | Auth doc lists `/health` as public, but no handler was found in the files read. `/system/*` is admin-gated. Candidates: `GET /health`, `GET /.well-known/voicestudio-speech`, `GET /v1/audio/voices` | **unverified** |
| Startup time | Not documented; the app sets up its Python backend on first launch | **unmeasured** |
| Headless start | Docs call it "a headless local speech service" but give **no command** to start the backend without Electron | **unknown** |
| ASR | `POST /v1/audio/transcriptions` (OpenAI-compatible); candidate for roadmap step 2 | source |

### Installing (needs the user; not run by Claude)

Official, from the README: `curl -fsSL https://voicestudio.sh/install | sh`
(default = latest `.dmg` + `SHA256SUMS.txt`, installs `VoiceStudio.app` to
`/Applications` or `~/Applications`, no sudo, no shell-profile edits, no
launch agent). Reviewed effects to know about: it **clears the macOS
quarantine flag and re-signs the app ad hoc if the signature check fails**;
the checksum comes from the same release, so it guards corruption, not a
compromised release; the app downloads its Python backend and models on first
launch (sizes undocumented, "multiple GB" for source builds). Safer variant:
download `scripts/install.sh`, read it, then run it; or download the `.dmg`
from the GitHub release page and verify it yourself. `--main` builds need
git, Node 22+, bun and Rust and are unpinned; avoid. Uninstall:
`... | sh -s -- --uninstall` (keeps data).

After installing and starting the app, run the read-only probe, which writes
the missing facts (liveness, sample rate, cold/warm latency, WAV validity):

    .venv/bin/python -m jarvis_ext.speech.voicestudio_probe

(`src/jarvis_ext/speech/voicestudio_probe.py`, tested against a fake server;
loopback only, stdlib only, sends one fixed test sentence.)

### Apple Silicon release facts (checked 2026-10-28, nothing downloaded)

* This Mac: Apple M2, `arm64`, macOS 26.5.1, 8 GB RAM, **14 GB free** of
  228 GB (93% used). VoiceStudio documents macOS 13.3+, Apple Silicon for the
  local backend, and "about 10 GB free".
* Release **v0.5.7** (2026-10-26, not a pre-release):
  `VoiceStudio-0.5.7-mac-arm64.dmg`, 280,523,075 bytes (about 268 MiB).
  GitHub's recorded digest:
  `sha256:8db7a16f35af1e4eaca25c389d376acc86b3885f30d1982f5fd6497953f888a0`.
  `SHA256SUMS.txt` (3,013 bytes) is published in the same release; compare the
  `.dmg` line in it with `shasum -a 256` of the download. Both come from the
  same origin, so a match rules out corruption, not a compromised release.
* **Not notarized** ("release builds are not notarized yet", upstream #1779),
  so macOS will warn. The documented workarounds (`xattr -dr
  com.apple.quarantine`, ad hoc `codesign`) bypass Gatekeeper; JARVIS does not
  do that. Open it yourself via right-click > Open, or System Settings >
  Privacy & Security > Open Anyway.
* Sizes: app download 0.28 GB (verified). OmniVoice model on Hugging Face
  (`k2-fsa/OmniVoice`, ungated): 3.27 GB (verified: model 2.45 GB + audio
  tokenizer 0.81 GB). The app's Python environment and installed size are
  **not documented**; expect several more GB (estimate, unverified). Total
  likely 6-9 GB on a disk with 14 GB free.
* Memory: no RAM requirement is documented; 8 GB shared with the JARVIS
  server, a browser and Ollama is a risk to measure, not assume.

Decision: the adapter targets `POST /v1/audio/speech` with
`response_format=wav`. `/ws/tts` (PCM16 frames) is the later streaming option.
Voicebox stays until the VoiceStudio adapter passes its tests and a manual
listening check.

## 1. What exists now (inspected, not assumed)

* `jarvis_ext/speech/voicebox.py`: `VoiceboxTTSBackend` (upstream `TTSBackend`).
  Calls `GET /health`, `GET /profiles`, `POST /generate/stream`
  `{profile_id, text, language, engine, personality:false}` -> WAV bytes.
  Loopback URLs only. `health()` and `available_voices()` never raise.
* `[jarvis.speech_output]`: base_url, profile, engine, voice, language,
  timeouts, `max_text_chars`.
* Server: `POST /v1/speech/synthesize` (text <= 5000 chars, returns WAV,
  headers `X-Voice-Id`, `X-Sample-Rate`) and `GET /v1/speech/tts/health`.
  The backend is resolved once and cached on app state
  (`_resolve_tts_backend`); a failed resolution is retried by later requests.
* Frontend `lib/tts.ts`: one shared utterance at a time, abort + token fence
  on `stop()`, object-URL teardown, health probe, autoplay guard. It sends the
  **whole reply as one request** and plays one blob.
* Not present: voice profiles beyond a single configured one, any audio cache,
  chunking, streaming playback, a queue, a JARVIS-side fallback policy.
* **Risk found and fixed (2026-10-28):** upstream `_tts_discovery`
  tried `kokoro, openai_tts, cartesia, elevenlabs` after the configured
  backend, and a cloud backend is "healthy" whenever its key exists, so a key
  plus a stopped local service sent reply text to that provider (confirmed by
  `tests/speech/test_tts_no_cloud_fallback.py`). Now `get_tts_backend`
  skips cloud backends unless one is the configured `speech.tts_backend` or
  `speech.allow_cloud_tts_fallback = true` (default false). Applies to
  `/v1/speech/*` and the CLI voice chat. The adapter in phase 1 still
  replaces discovery for JARVIS.

## 2. Design

New package `src/jarvis_ext/voice/` (the existing `speech/` stays as the
provider layer; nothing there is rewritten).

| Module | Responsibility |
|---|---|
| `adapter.py` | `VoiceAdapter` protocol: `health() -> Health`, `profiles() -> list[Profile]`, `synthesize(text, profile) -> Audio` (WAV bytes + sample rate + duration). Raises only `VoiceUnavailable` / `VoiceError`. One class per service: `VoiceboxAdapter` (wraps the existing backend), later `VoiceStudioAdapter`. |
| `profiles.py` | Configurable named profiles (`[jarvis.voice.profiles.<name>]`: engine, voice, speed, language, per-service profile id). A `default` profile; unknown name -> clear error, not a silent default. |
| `chunker.py` | Sentence-aware splitting: keeps abbreviations, decimals, numbers like `3.5`, URLs, and code spans intact; strips markdown to speakable text (reuse the logic behind `toSpeakableText`); a hard max chunk length that splits at clause boundaries, never mid-word. Pure function, no I/O. |
| `cache.py` | Audio cache in a JARVIS data dir (not the repo, not media folders). Key = SHA-256 of (normalised text, profile settings, service id, adapter version). Atomic writes (temp + rename), size cap with LRU eviction, corrupt entry = miss. Never caches failures. Off switch in config. |
| `service.py` | `VoiceService`: text -> chunks -> cache lookup -> adapter -> ordered audio chunks. Owns the policy: **local services only, no cloud fallback**; per-request timeout; a short circuit-breaker after repeated failures so a dead service costs one fast failure, not a timeout per sentence. |
| `settings.py` | `[jarvis.voice]` section (profile, cache on/off + size, chunk size, breaker thresholds), validated like the other sections. |

Server changes (small, additive):

* `server/api_routes.py`: keep `/v1/speech/synthesize` working unchanged for
  existing clients. Add `POST /v1/voice/speak` (text, profile) returning an
  ordered list of chunk URLs or a chunked/streamed response, and
  `GET /v1/voice/status` (service up/down, active profile, cache stats, never
  a secret). Both return 503 with a clear reason when the service is down.
* JARVIS-side backend selection: register the `VoiceService` as the TTS
  backend used by `/v1/speech/*`, so `_tts_discovery`'s cloud order is never
  consulted. (Preferred: a JARVIS override in `jarvis_ext`; avoid editing
  upstream discovery, to keep merges clean.)

Frontend changes (`frontend/src/lib/tts.ts`, small):

* Fetch chunk 1, start playing, prefetch the next; play chunks in order via
  one `Audio` element (or a queue) so `stop()` still tears down everything.
* Interruption: `stop()` aborts the in-flight request, drops queued chunks,
  and bumps the existing token so a late response cannot start playing.
  Later, push-to-talk / speech detection calls the same `stop()` (roadmap
  step 3).
* Safe playback: only play `audio/wav` blobs from the JARVIS origin; revoke
  object URLs on every path; autoplay errors (browser policy) surface as a
  "tap to play" state, not a failure loop; text is always shown regardless.
* Unavailable service: the speaker button is hidden/disabled using the
  health probe; a mid-reply failure shows one message and the reply stays
  readable. Never retries in a loop.

## 3. API contract

JARVIS side (new): see the two `/v1/voice/*` routes above. Errors use the
existing `HTTPException` shape; no stack traces or paths in the body.

Voicebox side (verified from code): `GET /health`, `GET /profiles`,
`POST /generate/stream`. Local only.

VoiceStudio side: speech-input contract known (section 0); **TTS contract
unverified.** The adapter interface is the
contract JARVIS depends on; a new service needs only a new adapter plus
tests against a recorded or live instance.

## 4. Dependencies

None new for phases 1–4 (httpx, pathlib, hashlib already used). A VoiceStudio
adapter may need its own client; decide once it is identified. No cloud
model, no always-on service, nothing started automatically.

## 5. Phases (each ends tested; preflight `future.voice_adapter` grows with them)

1. **Adapter + policy, Voicebox only.** `adapter.py`, `service.py`,
   `settings.py`, remove the cloud fallback for JARVIS. Tests: fake service;
   service down -> `VoiceUnavailable` and `/v1/speech/*` returns a clean
   error while chat works; a cloud key present is never used.
2. **Profiles + cache.** Tests: key changes with text/profile/service; atomic
   write under simulated crash; corrupt entry ignored; size cap evicts oldest.
3. **Chunker + `/v1/voice/speak`.** Tests: table of tricky sentences
   (abbreviations, decimals, URLs, code, markdown, very long sentence, empty,
   emoji); chunk order preserved; max length respected.
4. **Frontend queue + interruption.** Vitest cases extending the existing
   tts store tests: stop mid-queue, late response ignored, service dies
   mid-reply, autoplay blocked.
5. **VoiceStudio adapter** after a running local instance confirms the TTS
   contract (section 0). Same adapter protocol, same no-cloud policy.

## 6. Verification criteria (all must hold; report what was actually run)

* Unit + frontend tests green; `python -m jarvis_ext preflight` shows
  `future.voice_adapter` as a real PASS (not the placeholder) and no new FAIL.
* With the voice service stopped: chat works, `GET /v1/voice/status` reports
  unavailable, speaking returns a 503-class error within the breaker/timeout
  budget, no exception reaches the user as a stack trace.
* With a cloud TTS key set and the service stopped: no request leaves the
  machine (checked with the preflight network guard).
* Repeating the same text hits the cache (measured: no adapter call).
* Pressing stop during a multi-sentence reply stops audio and starts no
  later chunk.
* Manual on the Mac (cannot be run from the cloud): listen to a long reply,
  check chunk joins sound natural, and check latency to first audio.
  Reported as manual, not as passed.

## 7. Out of scope here

Local Whisper loop, wake word, push-to-talk, notes, brain switching, privacy
audit (roadmap steps 2–6), voice cloning, and any cloud voice.

## 8. VoiceStudio adapter: required code changes (after the probe passes)

1. `src/jarvis_ext/voice/adapter.py` (phase 1): `VoiceAdapter` protocol and
   `VoiceUnavailable`/`VoiceError`.
2. `src/jarvis_ext/voice/voicestudio.py`: `VoiceStudioAdapter` using httpx
   (already a dependency). Loopback URLs only, no `Origin` header, no key.
   `health()` = the liveness probe the probe confirmed; `profiles()` from
   `/v1/audio/voices`; `synthesize()` posts `response_format=wav`, validates
   the WAV (header parses, rate/channels read from it, non-empty, not
   silent), maps 429/503 + `Retry-After` to a bounded single retry, other
   errors to `VoiceError`, connection errors to `VoiceUnavailable`. Timeouts:
   short connect, generous read; a longer first-request budget only when the
   service reports it is loading (never retried in a loop).
3. `[jarvis.voice]` settings: `service = "voicebox" | "voicestudio"` (default
   stays `voicebox` until VoiceStudio is verified), base URL (default
   `http://127.0.0.1:3900`), profile name, timeouts. Unknown service or
   non-loopback URL = validation error.
4. `VoiceService` (phase 1): picks the adapter, owns timeouts and the circuit
   breaker, and never calls a cloud backend (the `speech.allow_cloud_tts_fallback`
   flag already defaults to false).
5. Preflight: replace the `future.voice_adapter` placeholder with a real
   check that runs the adapter against a fake server (offline) plus an
   optional live `future.speech_output` for the configured service.
6. Tests: fake-server tests for each status code above, WAV validation
   (truncated, silent, wrong type), loopback enforcement, no-cloud fallback,
   and a recorded real response once the probe has produced one.
