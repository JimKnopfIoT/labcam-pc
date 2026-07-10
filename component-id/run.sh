#!/usr/bin/env bash
# Starts the component-ID service from the venv (reads .env: ANTHROPIC_API_KEY,
# COMPONENT_ID_MODEL, PORT). Listens on 0.0.0.0:$PORT -> reachable from phone and local network.
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -d .venv ]; then
  python3 -m venv .venv
  .venv/bin/pip install -q -r requirements.txt
fi
exec .venv/bin/python app.py
