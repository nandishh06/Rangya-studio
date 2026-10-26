#!/usr/bin/env bash
# Rangya Studio — macOS / Linux launcher. First run sets everything up.
set -e
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  echo "ffmpeg is not installed."
  echo "  macOS:  brew install ffmpeg      Linux: sudo apt install ffmpeg"
  exit 1
fi
PY=$(command -v python3.12 || command -v python3.11 || command -v python3 || command -v python)
if [ ! -x .venv/bin/python ]; then
  echo "Setting up (first run)…"
  "$PY" -m venv .venv
  .venv/bin/python -m pip install -q --upgrade pip
fi
.venv/bin/python -m pip install -q -r requirements.txt
[ -z "$NO_BROWSER" ] && ( sleep 3; command -v open >/dev/null && open "http://127.0.0.1:${PORT:-8000}" || xdg-open "http://127.0.0.1:${PORT:-8000}" ) >/dev/null 2>&1 &
exec .venv/bin/python -m studio.app
