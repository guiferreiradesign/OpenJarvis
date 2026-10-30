# Wake-word models (local only, never committed)

The wake-word detector loads these files from JARVIS's own origin at
`/wake-models/<file>`. Everything here except this README is gitignored and is
excluded from the PWA precache.

| File | Source | Licence note |
|---|---|---|
| `melspectrogram.onnx` | openWakeWord v0.5.1 release | CC BY-NC-SA 4.0 (non-commercial) |
| `embedding_model.onnx` | openWakeWord v0.5.1 release | CC BY-NC-SA 4.0 (non-commercial) |
| `hey_jarvis_v0.1.onnx` | openWakeWord v0.5.1 release | CC BY-NC-SA 4.0 (non-commercial) |
| `Jarvis_20260313_215039.onnx` | openwakeword.com/library/422 (third party) | Personal, non-commercial by default; full terms unverified |

Do not commit, redistribute or host these files elsewhere. After adding or
changing them run `npm run build` in `frontend/` so they are copied to the
server's static directory.
