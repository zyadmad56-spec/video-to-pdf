#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="${PYTHON:-python3}"
ASR=0
BROWSER=0
YOUTUBE=1
POWERPOINT=0

for argument in "$@"; do
  case "$argument" in
    --asr) ASR=1 ;;
    --browser) BROWSER=1 ;;
    --no-youtube) YOUTUBE=0 ;;
    --powerpoint) POWERPOINT=1 ;;
    *) echo "Unknown option: $argument" >&2; exit 2 ;;
  esac
done

cd "$ROOT"
if [ ! -d .venv ]; then
  "$PYTHON" -m venv .venv
fi
PY="$ROOT/.venv/bin/python"
"$PY" -m pip install --upgrade pip
"$PY" -m pip install -r requirements.txt
if [ "$YOUTUBE" -eq 1 ]; then
  "$PY" -m pip install -r requirements-youtube.txt
fi
if [ "$ASR" -eq 1 ]; then
  "$PY" -m pip install -r requirements-asr.txt
fi
if [ "$POWERPOINT" -eq 1 ]; then
  "$PY" -m pip install "pywin32>=306"
fi
if [ "$BROWSER" -eq 1 ]; then
  "$PY" -m playwright install chromium
fi

echo
echo "Core setup complete."
echo "Interpreter: $PY"
echo "CLI: $PY scripts/lecture.py --help"
echo
echo "Local ASR model (not downloaded automatically):"
echo "  Place model.bin, config.json, tokenizer.json, and vocabulary.txt"
echo "  in .models/whisper-small/ or pass --model-dir."
echo
echo "YouTube: yt-dlp[default] includes the official EJS component."
echo "  Stream extraction also needs Deno (preferred) or Node.js available on PATH."
echo "  This setup does not install or download a JavaScript runtime."
echo
echo "Run: $PY scripts/lecture.py capabilities"
