#!/bin/sh
# Starts Penguin's Model Downloader. The first run sets up a private Python environment in .venv.
set -e
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
if ! command -v "$PY" >/dev/null 2>&1; then
  echo "Python 3 is needed. Install it from https://www.python.org/downloads/ and try again."
  exit 1
fi
if [ ! -x .venv/bin/python ]; then
  echo "First run: setting things up (about a minute)..."
  "$PY" -m venv .venv
fi
if ! .venv/bin/python -c "import huggingface_hub" >/dev/null 2>&1; then
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install --quiet -r requirements.txt
fi
exec .venv/bin/python app.py
