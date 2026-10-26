# Speech providers (Phase 4.1)

JARVIS gets two separate, replaceable speech providers. Neither of them
reasons or decides anything; JARVIS (the agent) stays the only "brain".

| Role | Provider | Interface | Registry key | Settings |
|---|---|---|---|---|
| Speech input (audio → text) | MLX Whisper, `mlx-community/whisper-base-mlx` | upstream `SpeechBackend` (`openjarvis/speech/_stubs.py`) | `SpeechRegistry["mlx-whisper"]` | `[jarvis.speech_input]` |
| Speech output (text → audio) | local Voicebox service, Kokoro engine, George voice | upstream `TTSBackend` (`openjarvis/speech/tts.py`) | `TTSRegistry["voicebox"]` | `[jarvis.speech_output]` |

Code: `src/jarvis_ext/speech/` (`mlx_whisper.py`, `voicebox.py`,
`audio.py`, `settings.py`, `errors.py`). Decisions: DECISIONS.md D34–D37.

```
 microphone / file ──► MlxWhisperBackend ──► transcript text ─┐
   (later task)          (SpeechBackend)                       ▼
                                                    JARVIS agent: tools, skills,
                                                    permissions, jobs (unchanged)
                                                               │ reply text
 speaker / file ◄──── VoiceboxTTSBackend ◄────────────────────┘
   (later task)          (TTSBackend) ──HTTP──► Voicebox on 127.0.0.1:17493
```

The agent only ever sees text. Nothing in the agent, tools, job store,
worker or notifications imports MLX or Voicebox, and no tool was added, so
permissions and confirmation prompts are exactly as before. Importing
`jarvis_ext` registers two small factories and imports no speech code at
all (tested), so text-only JARVIS is as fast as before and works with
neither provider installed or running.

Not part of this step: the microphone, push-to-talk, playback, a voice
conversation loop, wake words, background listening, phone calls and the
web interface. See "What is left" below.

## Speech input: MLX Whisper Base

* **Model:** `mlx-community/whisper-base-mlx` (Whisper Base, MLX weights,
  `weights.npz` ≈ 144 MB). English by default.
* **Vocabulary prompt** (Whisper's `initial_prompt`), default:
  `JARVIS, Voicebox, Kokoro, George, Phase 4.1 roadmap`. In the benchmark
  it corrected the proper nouns Voicebox and Kokoro.
* **Output:** the raw Whisper transcript, trimmed of surrounding spaces.
  No clean-up by Qwen or any other model; that can be an optional later
  step.
* **Local only.** Audio never leaves the process; nothing is sent to a
  cloud service. The only network use is the one-off model download from
  Hugging Face if the model is not cached yet (weights are never part of
the Python install).
* **Input:** uncompressed PCM WAV (8/16/24/32-bit, any channel count and
  sample rate), at most `max_audio_seconds` (default 120 s). It is decoded
  in process; 16 kHz mono reaches the model unchanged, other rates are
  resampled with SciPy's polyphase filter (SciPy comes with
  `mlx-whisper`). `mlx_whisper` itself would start an `ffmpeg` process
  for every file; passing it the decoded samples avoids that.

### Model lifetime and latency

* Nothing is imported or downloaded at import time. The model loads on
  `MlxWhisperBackend.load()` or on the first `transcribe()`, then stays in
  memory for the life of the process. `mlx_whisper` keeps it in its
  process-wide `ModelHolder`; the provider loads it through that same
  holder (float16, as `transcribe` uses), so later requests never reload it.
* Requests are serialised with a lock (one model, one request at a time).
* No Python interpreter, CLI or `ffmpeg` subprocess is started per
  utterance (tested).
* `stats()` and `last_timing` report the cold load separately from warm
  inference: `load_seconds`, `load_count`, `last_cold`,
  `last_inference_seconds`, `last_audio_seconds`.
* Loading and failures are logged (`jarvis_ext.speech.mlx_whisper`), and
  failures raise `SpeechUnavailableError` with a short reason (no paths).

**Expected behaviour** (measured on this 8 GB Apple Silicon Mac, Python
3.13, `mlx-whisper` 0.4.3, the 16.84 s test recording, warm OS file cache):

| | mlx 0.29.3 (benchmark version) | mlx 0.31.2 (version already in `uv.lock`) |
|---|---|---|
| Model load (imports + weights) | 2.5 s | 2.1–2.3 s |
| First transcription after load | 0.46–0.50 s | 0.54–0.56 s |
| Warm transcriptions | 0.21–0.22 s | 0.21–0.23 s |
| Very first run after installing | 26 s load + 2.2 s first run (one-off compilation and caching) | same |

Both versions produced the same transcript: "Hello Jarvis, this is a
speech-to-text test. Please note that my next task is to check the Phase
4.1 roadmap at 3.45pm. Also, I want you to remember the name of Voicebox,
Kokoro, and George." (The script read aloud said "the names: Voicebox,
Kokoro, and George"; Whisper Base heard "the name of".)

The isolated benchmark (Python 3.9, `mlx_whisper` reading the file through
`ffmpeg`) measured about 3.04 s cold and 0.31–0.34 s warm.

So: the first request in a new process costs about 2.5–3 s, and every
request after that about 0.2 s for 17 s of speech. A long-running process
(the future voice interface or `jarvis serve`) should call `load()` once
at start-up.

With the locked dependency set (mlx 0.31.2, torch 2.13.0, Python 3.13)
the numbers are the same: load 1.9–2.6 s, first transcription 0.47–0.50 s
(1.25 s on the very first run after installing), warm 0.21–0.23 s.

### Memory on this 8 GB Mac, and why not Whisper Turbo

Whisper Large-v3-Turbo (`mlx-community/whisper-large-v3-turbo`, about
1.6 GB of weights) caused severe memory pressure in the benchmark and its
timings were unusable, so it is **not** used, not configured and not run by
any test. A full `speech-transcribe` run with Whisper Base peaked at about
510 MB resident (the whole process, including Python, MLX and the PyTorch
import that `mlx-whisper` pulls in), measured with `/usr/bin/time -l`. Keep in mind
that the Voicebox app, its Kokoro model, a local LLM (Ollama/MLX) and the
browser all share the same 8 GB. Do not point `model` at a larger Whisper
model on this machine without measuring memory first.

### Installing the optional dependency

MLX Whisper is the optional `speech-mlx` extra. It is not part of the
default install, and its marker limits it to Apple Silicon Macs:

```toml
speech-mlx = [
    "mlx-whisper==0.4.3; sys_platform == 'darwin' and platform_machine == 'arm64'",
]
```

`mlx` itself is not pinned. It resolves to the version already in
`uv.lock` (0.31.2), the same one the `inference-mlx` extra uses
(`mlx-lm` needs `mlx>=0.31.2`), so both extras can be installed together.

Enable it with the normal `uv sync` workflow by adding the extra to the
extras you already use, then rebuild the Rust extension as usual (every
`uv sync` removes it; see README.md):

```bash
uv sync --extra dev --extra speech-mlx
uv run maturin develop --release --manifest-path rust/crates/openjarvis-python/Cargo.toml
uv run python -m jarvis_ext speech-check        # speech_input.error should be null
```

`uv sync` installs exactly the extras you name, so a later `uv sync
--extra dev` without `--extra speech-mlx` removes MLX Whisper again, and
speech input reports itself unavailable. Don't use `uv pip install
mlx-whisper`: the next `uv sync` undoes it, and it bypasses the lock.

What the extra installs (locked, Apple Silicon only): `mlx-whisper`
0.4.3, `mlx`/`mlx-metal` 0.31.2, `torch` 2.13.0 (a 106 MB download;
`mlx-whisper` requires it although JARVIS never uses it), `numba` 0.65.0
with `llvmlite`, `scipy`, `tiktoken`, `more-itertools` and
`huggingface-hub`. On every other platform the extra installs nothing and
the provider reports "needs an Apple Silicon Mac".

**Model weights are separate.** The extra installs code only. The
Whisper weights (about 144 MB) are downloaded from Hugging Face the first
time the model is loaded (`load()`, the first transcription, or
`speech-transcribe`), not at install time and not at import. See "Model
cache" below to control or prevent that download.

The very first run after installing is slow (about 25 s to load and 1–2 s
for the first transcription) while Python, Numba and MLX build their
caches; after that, loading takes about 2–2.5 s.

### Model cache

The model is downloaded once from Hugging Face (about 144 MB) into the
Hugging Face cache, by default `~/.cache/huggingface/hub`
(`HF_HOME` / `HF_HUB_CACHE` change it). To make sure nothing is
downloaded, set `HF_HUB_OFFLINE=1`; a missing model then fails to load
with a clear error. `model` may also be a local folder containing
`config.json` and `weights.npz`.

The benchmark left a copy in `~/Library/Caches/jarvis-stt-benchmark/hf`.
JARVIS does not use that folder. To reuse it instead of downloading
again, run with `HF_HOME=~/Library/Caches/jarvis-stt-benchmark/hf` (this is
how the smoke tests above were run), or delete it once JARVIS has its own
copy.

## Speech output: Voicebox

JARVIS talks to the Voicebox app that is already installed and running on
this Mac. It never installs, starts, stops, updates or reconfigures
Voicebox, and never asks it to download models.

* **API** (Voicebox 0.5.0, checked against its `/openapi.json`):
  `GET /health`, `GET /profiles`, and `POST /generate/stream` with
  `{"profile_id", "text", "language": "en", "engine": "kokoro",
  "personality": false}`. The stream endpoint returns WAV bytes directly
  and does not add the clip to Voicebox's history (checked).
* **`personality` is always `false`.** With `true`, Voicebox rewrites the
  text with its own language model before speaking. JARVIS decides what is
  said; there is no second reasoning model.
* **Voice:** by default the first profile whose preset engine is `kokoro`
  and preset voice `bm_george`. On this Mac that is the profile named
  `JARVIS`. `profile` (or a call's `voice_id`) may be a profile id, a
  profile name or a preset voice id (so upstream's `speech.voice_id =
  "bm_george"` also works).
* **Result:** an upstream `TTSResult` with `format = "wav"` (24 kHz mono
  16-bit from Kokoro), its duration, and metadata (profile, engine,
  generation time). Voicebox has no speed setting, so `speed` is ignored.
* **Localhost only.** `base_url` must be `http://` to `localhost` or a
  loopback address with a port; anything else is a configuration error.
  Proxy environment variables are ignored and redirects are not followed.
  JARVIS opens no port of its own.
* **Failures never crash the caller**: not running →
  `SpeechUnavailableError`; slow → `SpeechTimeoutError`
  (`timeout_seconds`, default 60; connect timeout 2 s); HTTP errors,
  non-WAV or empty audio, bad profile lists → `SpeechResponseError`.
  All are `RuntimeError` subclasses with short messages. `health()` and
  `available_voices()` never raise.

Measured: about 5.8 s for the first sentence after Voicebox started (it
loads Kokoro), then about 2.4 s to generate 5.1 s of speech.

### Starting, checking and disabling Voicebox

* **Start:** open the Voicebox app. It serves its API on
  `127.0.0.1:17493`.
* **Check:** `uv run python -m jarvis_ext speech-check` (loads nothing;
  shows whether Voicebox is running and which profile would be used), or
  `curl -s http://127.0.0.1:17493/health`.
* **Try it:** `uv run python -m jarvis_ext speech-say "Good morning." --out
  ~/Desktop/jarvis-test.wav` (refuses to overwrite an existing file).
* **Disable:** quit the Voicebox app. JARVIS then reports speech output as
  unavailable and keeps working in text. Nothing in JARVIS starts it again.
  Voicebox's own settings (including whether it listens beyond localhost)
  are left as they are; on this Mac it listens on `127.0.0.1` only
  (checked with `lsof`).

## Configuration

All keys are optional; the defaults are shown (see
[config.example.toml](config.example.toml)).

```toml
[jarvis.speech_input]
model = "mlx-community/whisper-base-mlx"   # HF repo id or local folder
language = "en"                            # "" = let Whisper detect it
initial_prompt = "JARVIS, Voicebox, Kokoro, George, Phase 4.1 roadmap"
max_audio_seconds = 120

[jarvis.speech_output]
base_url = "http://127.0.0.1:17493"        # loopback only
profile = ""                               # id or name; "" = engine + voice below
engine = "kokoro"
voice = "bm_george"
language = "en"
timeout_seconds = 60
connect_timeout_seconds = 2
max_text_chars = 4000
```

Unknown keys and out-of-range values are errors, as in the other
`[jarvis.*]` sections.

Upstream's `[speech]` section can select the providers by key
(`backend = "mlx-whisper"`, `tts_backend = "voicebox"`) once `jarvis_ext`
has been imported (any import of `openjarvis.tools` does that). Neither key
was added to upstream's automatic discovery order, so nothing changes for
`backend = "auto"`. Wiring them into `jarvis serve` and the voice chat is
part of the interface work and has not been tested end to end.

## Command line

```bash
uv run python -m jarvis_ext speech-check
uv run python -m jarvis_ext speech-say "Text" --out new-file.wav [--profile JARVIS]
# needs the speech-mlx extra (uv sync --extra dev --extra speech-mlx):
uv run python -m jarvis_ext speech-transcribe recording.wav --repeat 3
```

`speech-transcribe` is the manual smoke test: it loads the model once,
transcribes the file `--repeat` times in the same process and prints
`cold_load_seconds` and each `warm_inference_seconds` separately.

## Tests

`uv run pytest tests/jarvis_ext/test_speech_*.py`: 125 tests with fake
`mlx_whisper`/`mlx.core` modules and an `httpx.MockTransport` Voicebox. No
microphone, model, MLX, network or running Voicebox is needed. One
resampling test is skipped when SciPy is not installed (it is not in the
dev extras); with `--extra speech-mlx` all 125 pass.

## Dependency decision (resolved 2026-10-26)

The benchmark ran on Python 3.9.6 with mlx 0.29.3. The project supports
Python 3.10–3.13, and its `inference-mlx` extra needs `mlx>=0.31.2`, so
the exact benchmark environment can't be reproduced in the project. You
approved the `speech-mlx` extra with only `mlx-whisper==0.4.3` and no
`mlx` pin (DECISIONS.md D37).

Validated in separate environments, built from the lock with
`UV_PROJECT_ENVIRONMENT=... uv sync --locked --extra dev --extra
speech-mlx`, so the development `.venv` was not changed:

| Python | Installs | Speech tests | Load | Warm runs | Transcript |
|---|---|---|---|---|---|
| 3.13.17 | yes (mlx 0.31.2, torch 2.13.0) | 125 passed | 1.9–2.6 s | 0.21–0.23 s | same as the benchmark run |
| 3.10.22 | yes (same versions, scipy 1.15.3) | 125 passed | 25 s on the very first run | 0.21–0.22 s | same |

The lock was regenerated with uv 0.13.1, the newest release that keeps
this lock's existing markers. It accepts `uv lock --check` under uv
0.12.19 (CI), 0.13.1 and 0.13.5. Newer uv releases (0.13.2+) rewrite three
`onnxruntime` markers into an equivalent form; that is churn from the uv
version, not from this change.

## What is left for the text and voice interface

* Microphone capture and push-to-talk (upstream `speech/voice_io.py` has
  `record_until_silence`), and playback of replies.
* Calling `load()` at start-up in the long-running process, and choosing
  the providers in `jarvis serve` / the web interface, with an end-to-end
  test of the server's `/speech` routes.
* A conversation loop: transcript → agent → reply text → Voicebox, with the
  text always shown.
* Optional transcript clean-up (off by default), and streaming or chunked
  playback for long replies.
