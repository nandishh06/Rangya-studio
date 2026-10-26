# Rangya Studio

Local Flask dashboard that auto-edits talking-head videos (see README.md). No LLM calls —
the user explicitly does not want the Claude API used; editorial decisions are rule-based
(`studio/engine/decisions.py`). Only external service: ElevenLabs speech-to-text.

## Layout
- `studio/app.py` — Flask API + static UI (served by waitress); `studio/jobs.py` — single worker queue
- `studio/engine/` — transcribe (ElevenLabs) → decisions (retakes/self-talk/stutter/emphasis)
  → cutter (edl.json, silence safety net) → render (cached per-segment encodes, reframe, zoom,
  HarfBuzz-shaped burned captions via concat-demuxer PNG overlay, two-pass loudnorm)
- `studio/engine/copilot.py` — rule-based natural-language copilot (regex intents → settings/cuts edits,
  undo stack in undo.json, chat log in copilot.json); no LLM by user choice
- `studio/engine/kinetic.py` — kinetic "reel" captions (OpenCV face detection on the joined video
  places text beside the face); `media.py` — B-roll planning + music; compose stage in render.py
- ffmpeg 8 deadlocks when output -t/-frames meets atempo/apad: segments trim inside the filter graph
- `studio/effects/` — vendored 42-effect pack (MIT, fx_private exec hook removed); `engine/effects.py` plans
  (rules + user allow-list + cuts.json["effects"]) and renders them on short windows; captions dodge via zones
- `studio/engine/taste.py` — local preference model (ROOT/taste.json): learns from edits/finals via hooks in
  app.py (settings, copilot, effects add/remove/clear) and jobs.py (final render); Auto taste = settings_patch +
  taste-scored effect choice per moment (effects.ALTERNATIVES / pick_fx)
- `studio/static/` — vanilla JS dashboard; `studio/fonts/` — bundled OFL fonts
- `projects/<id>/` — per-video state: project.json, settings.json (overrides), transcript.json,
  cuts.json (auto + user edits), edl.json, state.json, preview.mp4, final.mp4, captions.srt
- `style.json` — user's saved default style (merged over `DEFAULT_STYLE` in engine/common.py)

## Constraints
- Must run on Windows and macOS: only pip wheels (flask, pillow, requests, uharfbuzz,
  freetype-py, waitress, mediapipe (brings opencv-contrib; never also install opencv-python-headless —
  they share the cv2 folder), numpy<2 for mediapipe) + ffmpeg on PATH. Don't depend on ffmpeg libass/drawtext/freetype.
- Never print or log the ElevenLabs key (`.env`).
- Dev machine: Intel i7 Mac, 16 GB; run with `NO_BROWSER=1 PORT=8765 ./start.sh` for testing.
