"""Deterministic video pipeline used by ``media_render`` and ``media_validate``.

* :mod:`.plan`      — the validated ``EditPlan`` model and output presets.
* :mod:`.settings`  — ``[jarvis.media_render]`` configuration.
* :mod:`.output`    — the output folder: temp files, versioned names, publishing.
* :mod:`.ffmpeg`    — FFmpeg argument construction and process control.
* :mod:`.subtitles` — SRT parsing/sanitising and the transcription extension point.
* :mod:`.checks`    — checks that a rendered file matches its plan.

Nothing here decides *what* to edit; it executes a plan exactly as given.
"""
