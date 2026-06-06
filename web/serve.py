#!/usr/bin/env python3
# Lab Cam Web — static file server + DMM7510 proxy.
#
# Serves the static page files (like python3 -m http.server) AND proxies the
# Keithley DMM7510 web interface paths (Virtual Front Panel) to the same origin.
# Reason: front_panel.html calls `top.document.title` in startSession() — in a
# CROSS-ORIGIN iframe this raises a SecurityError, the VFP session never starts and
# the canvas stays black. Through this proxy the DMM page is SAME-ORIGIN (same host
# + port as labcam-web) -> no SecurityError, the browser decodes the TGA itself,
# and Basic-Auth is appended server-side (no browser login required).
#
# The DMM paths do not conflict with labcam-web (which uses /index.html, /app.js,
# /style.css, /config.js — NOT /front_panel.html, /script/, /css/, /ajax_proc, /images/).
import os
import base64
import urllib.request
import urllib.error
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("PORT", "8080"))
BIND = os.environ.get("BIND", "0.0.0.0")
DMM_HOST = os.environ.get("LABCAM_DMM_HOST", "192.168.10.45")
# Basic-Auth for the DMM web interface (device login). Overridable via env var.
DMM_AUTH = os.environ.get("LABCAM_DMM_AUTH", "USER:PASSWORD")

# Path prefixes forwarded to the DMM (everything else is served as a local file).
DMM_PREFIXES = ("/front_panel.html", "/script/", "/css/", "/ajax_proc", "/images/")
_AUTH_HEADER = "Basic " + base64.b64encode(DMM_AUTH.encode()).decode()


class Handler(SimpleHTTPRequestHandler):
    def _is_dmm(self):
        p = self.path.split("?", 1)[0]
        return any(p == pre or p.startswith(pre) for pre in DMM_PREFIXES)

    def _proxy(self, method):
        url = f"http://{DMM_HOST}{self.path}"
        length = int(self.headers.get("Content-Length", 0) or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", _AUTH_HEADER)
        # Forward the client's Content-Type (ajax_proc uses form-urlencoded).
        ct = self.headers.get("Content-Type")
        if ct:
            req.add_header("Content-Type", ct)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                data = r.read()
                self.send_response(r.status)
                rct = r.headers.get("Content-Type", "application/octet-stream")
                self.send_header("Content-Type", rct)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.end_headers()
            self.wfile.write(e.read() or b"")
        except Exception as e:  # DMM unreachable or similar
            self.send_response(502)
            self.end_headers()
            self.wfile.write(f"DMM proxy error: {e}".encode())

    def do_GET(self):
        if self._is_dmm():
            return self._proxy("GET")
        return super().do_GET()

    def do_POST(self):
        if self._is_dmm():
            return self._proxy("POST")
        self.send_error(405)


if __name__ == "__main__":
    print(f"Lab Cam Web: http://127.0.0.1:{PORT}/  (LAN: http://{BIND}:{PORT}/ ; "
          f"DMM proxy -> {DMM_HOST}; Ctrl+C to quit)")
    ThreadingHTTPServer((BIND, PORT), Handler).serve_forever()
