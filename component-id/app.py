"""labcam-component-id — HTTP service.

Stdlib-only HTTP service (no framework). Routes:
  GET  /health    -> {"status":"ok"}
  POST /identify  -> component card (JSON)

Request body of /identify (JSON):
  {
    "image_b64": "<base64-encoded JPEG/PNG>",            # required
    "dmm": {"mode":"...","value":1.23,"unit":"V","range":1.0} | null   # optional
  }

Run:  python3 app.py    (port via $PORT, default 7895)

Needs ANTHROPIC_API_KEY in .env for real Claude-vision identification; without it
the service answers with a stub card (see identifier.py).
"""

from __future__ import annotations

import base64
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from identifier import identify

PORT = int(os.environ.get("PORT", "7895"))
MAX_BODY = 32 * 1024 * 1024  # 32 MB guard limit


class Handler(BaseHTTPRequestHandler):
    server_version = "labcam-component-id/1.0"

    def _cors(self) -> None:
        # Allow calls from browser clients and any LAN client.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "POST, GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def _send(self, code: int, obj: dict) -> None:
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:
        # CORS preflight (browser sends OPTIONS before the POST).
        self.send_response(204)
        self._cors()
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/health":
            self._send(200, {"status": "ok"})
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:
        if self.path != "/identify":
            self._send(404, {"error": "not found"})
            return

        length = int(self.headers.get("Content-Length", "0") or "0")
        if length <= 0:
            self._send(400, {"error": "empty body"})
            return
        if length > MAX_BODY:
            self._send(413, {"error": "body too large", "max_bytes": MAX_BODY})
            return

        raw = self.rfile.read(length)
        try:
            req = json.loads(raw)
        except Exception as e:
            self._send(400, {"error": "invalid JSON", "detail": str(e)})
            return

        b64 = req.get("image_b64")
        if not b64:
            self._send(400, {"error": "image_b64 missing"})
            return
        try:
            image_bytes = base64.b64decode(b64, validate=True)
        except Exception as e:
            self._send(400, {"error": "image_b64 not decodable", "detail": str(e)})
            return

        dmm = req.get("dmm")  # optional, may be None
        try:
            card = identify(image_bytes, dmm)
        except Exception as e:
            self._send(500, {"error": "identify failed", "detail": str(e)})
            return

        self._send(200, card)

    def log_message(self, fmt: str, *args) -> None:
        # quiet log on stderr (one line per request)
        import sys
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))


def main() -> None:
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"labcam-component-id service on :{PORT}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        srv.shutdown()


if __name__ == "__main__":
    main()
