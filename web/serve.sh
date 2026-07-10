#!/usr/bin/env bash
# Serves the page. Default bind 0.0.0.0 -> reachable from the LAN
# (e.g. from another machine at http://<pc-ip>:8080). Locally still http://127.0.0.1:8080.
# NOTE: getUserMedia (camera) requires a "secure context" — only
# http://127.0.0.1 / localhost qualifies. Over the LAN IP (http://<pc-ip>) the
# camera is blocked; the instrument/data view still works.
set -euo pipefail
cd "$(dirname "$0")"
export PORT="${PORT:-8080}"
export BIND="${BIND:-0.0.0.0}"
# Custom server: static files + DMM7510 proxy (Virtual Front Panel as SAME-ORIGIN
# iframe; otherwise top.document in a cross-origin iframe throws -> canvas stays black).
# DMM login overridable via LABCAM_DMM_AUTH (default <username>:<password>).
exec python3 serve.py
