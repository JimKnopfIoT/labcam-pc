#!/usr/bin/env python3
# ============================================================================
#  labcam-micro -- microscope camera (USB video-capture) as an MJPEG stream for LAN
#
#  Why: the microscope camera is attached to the lab PC. In the browser, getUserMedia
#  only accesses cameras of the BROWSER machine and only in a secure context
#  (localhost/HTTPS). From ANOTHER machine (LAN-IP, plain HTTP) that does not work.
#  This service therefore serves the camera image SERVER-SIDE as MJPEG
#  (multipart/x-mixed-replace) -> a plain <img> in the frontend, visible from any
#  machine on the LAN (analogous to labcam-thermal).
#
#  The camera delivers MJPEG natively -> default = 1:1 PASS-THROUGH (no decode/re-encode)
#  = near-zero CPU. Optional re-encode (?w=<width>&q=<quality>) downscales for narrow
#  links (costs CPU). ON-DEMAND: ffmpeg/camera only run while a client is watching.
#
#  Camera selection by USB-unique-ID (vid:pid) as a whitelist, NOT by /dev/videoN
#  -> the service never accidentally grabs a foreign camera. Find yours with `lsusb`.
# ============================================================================
import os, sys, time, threading, subprocess, glob

PORT      = int(os.environ.get("MICRO_PORT", "7897"))
# Allowed microscope camera(s) by USB-unique-ID (vid:pid). Comma-separate multiple;
# optional serial filter for several identical cameras.
MICRO_USB_IDS = [s.strip().lower() for s in
                 os.environ.get("MICRO_USB_IDS", "<microscope-vid:pid>").split(",") if s.strip()]
MICRO_SERIALS = [s.strip() for s in
                 os.environ.get("MICRO_USB_SERIALS", "").split(",") if s.strip()]
MICRO_NAME_HINTS = ("<microscope-name-substring>",)   # emergency fallback only, if sysfs is silent
CAP_W     = int(os.environ.get("MICRO_W", "1920"))
CAP_H     = int(os.environ.get("MICRO_H", "1080"))
CAP_FPS   = int(os.environ.get("MICRO_FPS", "30"))    # capture fps from the camera (USB side)
IDLE_GRACE = float(os.environ.get("MICRO_IDLE_GRACE", "5"))   # s without a viewer -> close the camera

_lock = threading.Lock()
_latest = {"jpg": None, "ts": 0.0}
_clients = [0]                  # number of active stream viewers (on-demand)
_clients_lock = threading.Lock()
_proc = {"p": None}


def _node_usb_info(idx):
    base = "/sys/class/video4linux/video%d" % idx
    def rd(p):
        try: return open(p).read().strip()
        except Exception: return ""
    name = rd(base + "/name").lower()
    dev = base + "/device/.."
    vid = rd(dev + "/idVendor").lower(); pid = rd(dev + "/idProduct").lower()
    ser = rd(dev + "/serial")
    return (("%s:%s" % (vid, pid)) if vid and pid else ""), ser, name


def find_dev():
    # Find the microscope by USB-unique-ID (vid:pid (+serial)) -- whitelist, robust against
    # re-plugging AND against foreign cameras. Capture node = lowest matching video index.
    best = None
    for p in sorted(glob.glob("/sys/class/video4linux/video*/name")):
        try:
            idx = int(p.rsplit("/video", 1)[1].split("/")[0])
        except Exception:
            continue
        vidpid, ser, name = _node_usb_info(idx)
        ok = bool(vidpid) and vidpid in MICRO_USB_IDS and (not MICRO_SERIALS or ser in MICRO_SERIALS)
        if not ok and not vidpid:        # sysfs silent -> only then by name, never a foreign camera
            ok = any(h in name for h in MICRO_NAME_HINTS)
        if ok and (best is None or idx < best):
            best = idx
    return ("/dev/video%d" % best) if best is not None else None


def capture_loop():
    """While viewers are present: ffmpeg reads native MJPEG, we split the stream into single
    frames (SOI 0xFFD8) and keep the latest in _latest. NO re-encode."""
    SOI = b"\xff\xd8"
    while True:
        with _clients_lock:
            n = _clients[0]
        if n <= 0:
            # No one watching -> camera/ffmpeg closed, zero load.
            with _lock:
                _latest["jpg"] = None
            time.sleep(0.3)
            continue
        dev = find_dev()
        if dev is None:
            time.sleep(2)
            continue
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error",
               "-f", "v4l2", "-input_format", "mjpeg",
               "-video_size", "%dx%d" % (CAP_W, CAP_H), "-framerate", str(CAP_FPS),
               "-i", dev, "-c", "copy", "-f", "mjpeg", "pipe:1"]
        proc = None
        buf = b""
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                    bufsize=0)
            _proc["p"] = proc
            while True:
                with _clients_lock:
                    if _clients[0] <= 0:
                        break
                chunk = proc.stdout.read(65536)
                if not chunk:
                    break
                buf += chunk
                # emit complete frames (SOI -> next SOI)
                while True:
                    i = buf.find(SOI)
                    if i < 0:
                        break
                    j = buf.find(SOI, i + 2)
                    if j < 0:
                        if i > 0:
                            buf = buf[i:]   # keep the rest until the next frame
                        break
                    with _lock:
                        _latest["jpg"] = buf[i:j]
                        _latest["ts"] = time.time()
                    buf = buf[j:]
        except Exception:
            pass
        finally:
            _proc["p"] = None
            buf = b""
            try:
                if proc:
                    proc.kill(); proc.wait(timeout=2)
            except Exception:
                pass
        time.sleep(0.5)     # short reconnect pause


# --- optional re-encode (only when ?w=/?q= is requested) -----------------------
_cv2 = None
def _encode_resized(jpg, w, q):
    global _cv2
    if _cv2 is None:
        import cv2 as c; import numpy as np
        _cv2 = (c, np)
    cv2, np = _cv2
    img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
    if img is None:
        return jpg
    h, wid = img.shape[:2]
    if w and 0 < w < wid:
        nh = max(1, int(h * w / wid))
        img = cv2.resize(img, (w, nh), interpolation=cv2.INTER_AREA)
    ok, out = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
    return out.tobytes() if ok else jpg


from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _params(self):
        from urllib.parse import urlparse, parse_qs
        q = parse_qs(urlparse(self.path).query)
        def num(k, d):
            try: return int(q[k][0])
            except Exception: return d
        fps = max(1, min(60, num("fps", 15)))           # send fps (tame bandwidth)
        w   = num("w", 0)                                # >0 = downscale to this width (re-encode)
        q_  = max(40, min(95, num("q", 75)))             # JPEG quality on re-encode
        return fps, w, q_

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path.startswith("/stream"):
            fps, w, q = self._params()
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Connection", "close")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            with _clients_lock:
                _clients[0] += 1
            try:
                last_ts = 0.0
                period = 1.0 / fps
                t0 = time.time()    # wait briefly for the first frame (camera must start)
                while True:
                    with _lock:
                        jpg = _latest["jpg"]; ts = _latest["ts"]
                    if jpg and ts != last_ts:
                        last_ts = ts
                        out = _encode_resized(jpg, w, q) if w else jpg
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                         b"Content-Length: " + str(len(out)).encode() + b"\r\n\r\n")
                        self.wfile.write(out)
                        self.wfile.write(b"\r\n")
                    elif not jpg and time.time() - t0 > 8:
                        break   # no image after 8 s -> camera missing/busy, close the connection
                    time.sleep(period)
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                with _clients_lock:
                    _clients[0] = max(0, _clients[0] - 1)
            return
        if path.startswith("/snapshot"):
            fps, w, q = self._params()
            with _clients_lock:                 # trigger the camera once
                _clients[0] += 1
            try:
                t0 = time.time(); jpg = None
                while time.time() - t0 < 8:
                    with _lock:
                        jpg = _latest["jpg"]
                    if jpg:
                        break
                    time.sleep(0.05)
            finally:
                with _clients_lock:
                    _clients[0] = max(0, _clients[0] - 1)
            out = (_encode_resized(jpg, w, q) if (jpg and w) else jpg)
            self.send_response(200 if out else 503)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            if out:
                self.wfile.write(out)
            return
        # anything else: small status
        import json
        dev = find_dev()
        body = json.dumps({"device": dev, "clients": _clients[0],
                           "streaming": _proc["p"] is not None}).encode()
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)


def _on_signal(sig, frm):
    try:
        if _proc["p"]:
            _proc["p"].kill()
    except Exception:
        pass
    os._exit(0)


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    threading.Thread(target=capture_loop, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
