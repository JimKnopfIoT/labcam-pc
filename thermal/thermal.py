#!/usr/bin/env python3
# ============================================================================
#  labcam-thermal — HT-301 thermal image (calibrated, °C) as MJPEG for labcam-web
#
#  Capture + radiometry via ht301_hacklib (stawel, GPL-3.0): cv2 raw mode
#  (CAP_PROP_ZOOM=0x8004) delivers real 16-bit data + calibration meta rows; the
#  lib builds a LUT raw->°C and the Min/Max/Center temperatures from them —
#  without any external sensor (Tmin ~ room temperature is accurate).
#
#  Pipeline: temp(°C) -> AGC on [Tmin..Tmax] (smoothed) -> CLAHE detail -> palette
#  -> Lanczos upscale -> unsharp -> colorbar right (Tmin bottom/Tmax top) + Min/Max markers.
#  Output as MJPEG (multipart) + single frame via HTTP :7896.
#
#  GPL-3.0 (uses ht301_hacklib, GPL-3.0).
# ============================================================================
import os, sys, time, threading
import numpy as np
import cv2
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ht301_hacklib as H

DEV_INDEX = int(os.environ.get("THERMAL_DEV_INDEX", "2"))   # start value only; set by USB-unique-ID
DEV = "/dev/video%d" % DEV_INDEX
# Allowed IR camera(s) by USB-unique-ID (vid:pid), NOT by /dev/videoN. This way the service
# NEVER grabs a foreign camera. Comma-separate multiple;
# optional serial filter (THERMAL_USB_SERIALS) for several identical cameras.
IR_USB_IDS = [s.strip().lower() for s in
              os.environ.get("THERMAL_USB_IDS", "<thermal-vid:pid>").split(",") if s.strip()]
IR_SERIALS = [s.strip() for s in
              os.environ.get("THERMAL_USB_SERIALS", "").split(",") if s.strip()]
IR_NAME_HINTS = ("t3-317", "infiray", "ht-301", "ht301")   # emergency fallback only, if sysfs is silent
# Mode: "cal" = calibrated RAW image (clean, °C, markers/colorbar as overlay) — default.
# "yuyv" = robust false-color image without °C (less USB, for shared bus). "auto" = depends on overlay.
THERMAL_MODE = os.environ.get("THERMAL_MODE", "cal").lower()
THERMAL_FPS = float(os.environ.get("THERMAL_FPS", "0"))   # >0 = throttle capture fps (reduce USB load)


def _node_usb_info(idx):
    """(vid:pid, serial, name) of a /dev/videoN from sysfs; ('', '', '') on error.
    .../device is the UVC interface, ../ the USB device node with idVendor/idProduct/serial."""
    base = "/sys/class/video4linux/video%d" % idx
    def rd(p):
        try: return open(p).read().strip()
        except Exception: return ""
    name = rd(base + "/name").lower()
    dev = base + "/device/.."
    vid = rd(dev + "/idVendor").lower(); pid = rd(dev + "/idProduct").lower()
    ser = rd(dev + "/serial")
    return (("%s:%s" % (vid, pid)) if vid and pid else ""), ser, name


def find_dev_index():
    # Find the IR camera by USB-unique-ID (vid:pid (+serial)) — whitelist: robust against
    # re-plugging AND against foreign cameras. Capture node = lowest matching video index.
    import glob
    best = None
    for p in sorted(glob.glob("/sys/class/video4linux/video*/name")):
        try:
            idx = int(p.rsplit("/video", 1)[1].split("/")[0])
        except Exception:
            continue
        vidpid, ser, name = _node_usb_info(idx)
        ok = bool(vidpid) and vidpid in IR_USB_IDS and (not IR_SERIALS or ser in IR_SERIALS)
        if not ok and not vidpid:           # sysfs silent -> only then by name, never a foreign camera
            ok = any(h in name for h in IR_NAME_HINTS)
        if ok and (best is None or idx < best):
            best = idx
    return best
OUT_W   = int(os.environ.get("THERMAL_W", "768"))           # output image width (browser scales)
SENSOR_W, SENSOR_H = 384, 288
CAP_W, CAP_H = 384, 292    # capture size (288 image rows + 4 metadata rows). NOT named "H" —
                           # that would shadow the import "ht301_hacklib as H"!
IMG_H = SENSOR_H           # actual image rows (remainder = metadata)
OUT_H   = OUT_W * SENSOR_H // SENSOR_W
BAR_W   = 44                                                # colorbar column width on the right (labels without decimal/°C -> narrower)
PORT    = int(os.environ.get("THERMAL_PORT", "7896"))
COLORMAP = cv2.COLORMAP_INFERNO   # violet/lava: cold dark-violet -> red -> orange -> yellow hot

# ---- image pipeline parameters (all overridable via env -> tuning without code changes) ----
def _envf(name, default):
    try: return float(os.environ.get(name, default))
    except Exception: return float(default)

RANGE_SMOOTH = _envf("THERMAL_RANGE_SMOOTH", 0.8)   # smoothing of the scale (no flicker/jumps)
TEMPORAL = _envf("THERMAL_TEMPORAL", 0.60)   # temporal denoise (low noise WITHOUT softening)
CLAHE_CLIP = _envf("THERMAL_CLAHE", 2.0)     # local contrast; 0=off. High = amplifies noise!
SHARP = _envf("THERMAL_SHARP", 0.45)         # unsharp strength AFTER the upscale
DENOISE = _envf("THERMAL_DENOISE", 1)        # 1 = edge-preserving spatial filter before CLAHE, 0 = off
AGC_PCT = _envf("THERMAL_AGC_PCT", 1.0)      # percentile for the scale (0 = hard Min/Max)
LUT_REFRESH = 30       # dev.info()/LUT rebuild only every N frames (drifts slowly) -> fast

# ---- FFC / shutter calibration ----
# A microbolometer MUST be referenced against the shutter periodically. Without that the
# absolute value drifts away (measured: -24 K after ~40 h continuous run) AND fixed-pattern noise
# grows ("grainy") — the latter is static; the temporal EMA does NOT filter it out, it preserves it.
# This went unnoticed before because every service restart triggers an FFC in Camera.__init__.
FFC_INTERVAL = _envf("THERMAL_FFC_SEC", 120)    # an FFC at least every N s (0 = manual only)
FFC_FPA_DELTA = _envf("THERMAL_FFC_DFPA", 1.5)  # also on N K drift of the sensor (FPA) temperature
FFC_SETTLE = 4                                  # frames to discard after the shutter

_clahe = cv2.createCLAHE(clipLimit=CLAHE_CLIP, tileGridSize=(8, 8)) if CLAHE_CLIP > 0 else None
FONT = cv2.FONT_HERSHEY_SIMPLEX


def enhance(g8):
    """Shared detail chain: spatial filter -> CLAHE -> color -> upscale -> mild sharpening.
    Order matters: denoise first, THEN stretch locally — the other way round CLAHE amplifies
    exactly the noise that the filter can no longer separate cleanly afterwards."""
    if DENOISE > 0:
        g8 = cv2.bilateralFilter(g8, 5, 40, 40)     # edge-preserving: noise gone, edges stay
    if _clahe is not None:
        g8 = _clahe.apply(g8)
    col = cv2.applyColorMap(g8, COLORMAP)
    up = cv2.resize(col, (OUT_W, OUT_H), interpolation=cv2.INTER_LANCZOS4)
    if SHARP > 0:
        blur = cv2.GaussianBlur(up, (0, 0), 1.0)
        up = cv2.addWeighted(up, 1.0 + SHARP, blur, -SHARP, 0)
    return up

_latest = {"jpg": None}
_lock = threading.Lock()
_active = {"dev": None, "cap": None, "proc": None}   # for clean release on SIGTERM
_points = []                           # user-defined measurement points (nx, ny) in IMAGE space 0..1
_plock = threading.Lock()
_show_markers = [False]                 # markers (Min/Max + measurement points) — OFF = YUYV mode (robust)
_show_bar = [False]                      # color gradient/colorbar — OFF = YUYV mode (robust)
# Emissivity: None = leave camera default. Applied on the HOST side in the LUT
# (dev.emissivity_override) — like IRCAM; written to the camera, ε never took effect.
# _calib_target = target temperature (°C): the cal loop searches for the ε at which the hotspot shows this value.
_emiss = [float(os.environ["THERMAL_EMISSIVITY"]) if os.environ.get("THERMAL_EMISSIVITY") else None]
_emiss_apply = [os.environ.get("THERMAL_EMISSIVITY") is not None]
_calib_target = [None]
_emiss_last = [None]                     # last active ε (for status/display)
_ffc_request = [False]                   # self-calibrate: internal camera calibration (FFC/shutter)
# Temperature range: False = normal (-20..120 °C), True = high range (-20..450 °C).
# Re-applied on every (re)connect; _range_apply -> switch within the running loop.
_high_range = [os.environ.get("THERMAL_HIGH_RANGE", "0") == "1"]
_range_apply = [False]
# Diagnostics. "range" = currently set temperature range (normal/high).
_diag = {"tmax": None, "emiss": None, "lutmax": None, "range": "normal",
         "fpa": None, "shutter": None, "ffc": None}
# Mode follows automatically: image only -> YUYV (ffmpeg, stable, microscope-compatible);
# as soon as markers OR color bar are on -> cv2 raw (calibrated, °C), but uses more USB.


WHITE = (255, 255, 255)
RED = (0, 0, 255)          # BGR -> red
CYAN = (255, 210, 90)      # cold marker (light blue)


GREEN = (90, 230, 90)      # user-defined measurement points


def render(up, lo, hi, tmaxC, tmaxpt, tminC, tminpt, points=None):
    """Image + narrow colorbar on the right (Tmax top/Tmin bottom) + Min/Max + user measurement points."""
    h, w = up.shape[:2]
    bar = _show_bar[0]
    # Stream width is ALWAYS = w (768), regardless of whether the color bar is on or off.
    # The colorbar is overlaid on the right image edge (no extra column) -> image stays
    # constantly wide AND always reaches the outer edge; no resolution change, no jitter/scaling.
    canvas = up.copy()
    sx, sy = w / SENSOR_W, h / SENSOR_H

    if not _show_markers[0]:
        tmaxpt = tminpt = None      # Min/Max markers off
        points = None               # measurement points off

    # Hotspot: red cross, temperature in GREEN (clearly visible even on bright surfaces)
    if tmaxpt is not None:
        p = (int(tmaxpt[0] * sx), int(tmaxpt[1] * sy))
        cv2.drawMarker(canvas, p, RED, cv2.MARKER_CROSS, 14, 2)
        cv2.putText(canvas, f"{tmaxC:.1f}C", (p[0] + 14, p[1] + 20), FONT, 0.44, GREEN, 1, cv2.LINE_AA)
    # Coldest point: cyan cross, temperature in GREEN
    if tminpt is not None:
        p = (int(tminpt[0] * sx), int(tminpt[1] * sy))
        cv2.drawMarker(canvas, p, CYAN, cv2.MARKER_CROSS, 14, 2)
        cv2.putText(canvas, f"{tminC:.1f}C", (p[0] + 14, p[1] + 20), FONT, 0.44, GREEN, 1, cv2.LINE_AA)

    # User measurement points: green cross + number + °C (green, uniform style).
    # Number as "#3" and NOT as "3:" — "3:29.4C" reads like a measured value from the start.
    for i, (nx, ny, tC) in enumerate(points or []):
        p = (int(nx * w), int(ny * h))
        cv2.drawMarker(canvas, p, GREEN, cv2.MARKER_CROSS, 14, 2)
        cv2.putText(canvas, f"#{i + 1} {tC:.1f}C", (p[0] + 12, p[1] + 18), FONT, 0.44, GREEN, 1, cv2.LINE_AA)

    # Colorbar (top = hot -> bottom = cold) + °C scale — overlaid on the right image edge
    # (narrow black background strip), only when color gradient is active.
    if bar:
        x0 = w - BAR_W
        canvas[:, x0:w] = 0                                # narrow black background (over the image edge)
        gx, gw, pad = x0 + 5, 9, 10
        ramp = np.linspace(255, 0, h - 2 * pad).astype(np.uint8).reshape(-1, 1)
        grad = cv2.applyColorMap(np.repeat(ramp, gw, axis=1), COLORMAP)
        canvas[pad:h - pad, gx:gx + gw] = grad

        def rlabel(txt, y, sc=0.38, th=1):
            (tw, _), _ = cv2.getTextSize(txt, FONT, sc, th)
            cv2.putText(canvas, txt, (w - tw - 5, y), FONT, sc, WHITE, th, cv2.LINE_AA)
        rlabel(f"{hi:.0f}", pad + 12)
        rlabel(f"{(lo + hi) / 2:.0f}", h // 2 + 4)
        rlabel(f"{lo:.0f}", h - pad)
    return canvas


SETTLE_SEC = 6   # Let the camera settle after (re-)start/release, otherwise raw mode
                 # stays slow (-> 0.3 fps). With this pause: full ~12.5 fps.


def want_cal():
    # Default: always calibrated RAW (clean image). "yuyv" forces the robust mode,
    # "auto" switches depending on overlay state (markers/color bar).
    if THERMAL_MODE == "yuyv":
        return False
    if THERMAL_MODE == "auto":
        return _show_markers[0] or _show_bar[0]
    return True   # "cal" (default)


def usb_reset_ht301():
    # Reset HT-301 via USB (to recover from -110 UVC hang + clean mode switch
    # YUYV<->Raw). Possible without root thanks to udev rule (video group).
    try:
        import re, subprocess, fcntl
        out = subprocess.run(["lsusb"], capture_output=True, text=True).stdout
        m = re.search(r"Bus (\d+) Device (\d+): ID <thermal-vid:pid>", out)
        if not m:
            return
        with open("/dev/bus/usb/%s/%s" % (m.group(1), m.group(2)), "wb") as fd:
            fcntl.ioctl(fd, ord('U') << 8 | 20, 0)   # USBDEVFS_RESET
    except Exception:
        pass


def run_yuyv():
    """Robust mode: ffmpeg-YUYV -> false-color image only (no °C). Compatible with the
    microscope on the shared USB bus. Runs as long as NO markers/color bar are requested."""
    import subprocess
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "v4l2",
           "-input_format", "yuyv422", "-video_size", "%dx%d" % (CAP_W, CAP_H), "-i", DEV,
           "-f", "rawvideo", "-pix_fmt", "yuyv422", "-"]
    fsz = CAP_W * CAP_H * 2
    proc = None
    ema = None; lo_s = hi_s = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, bufsize=fsz)
        _active["proc"] = proc
        while not want_cal():
            buf = proc.stdout.read(fsz)
            if len(buf) < fsz:
                break
            t = np.frombuffer(buf, dtype=">u2").reshape(CAP_H, CAP_W).astype(np.float32)[:IMG_H]
            # stronger temporal denoise for YUYV (raw 16-bit is noisier than calibrated)
            ema = t if ema is None else 0.65 * ema + 0.35 * t
            t = ema
            lo = float(np.percentile(t, 1)); hi = float(np.percentile(t, 99))
            lo_s = lo if lo_s is None else RANGE_SMOOTH * lo_s + (1 - RANGE_SMOOTH) * lo
            hi_s = hi if hi_s is None else RANGE_SMOOTH * hi_s + (1 - RANGE_SMOOTH) * hi
            g8 = (np.clip((t - lo_s) / max(1.0, hi_s - lo_s), 0, 1) * 255).astype(np.uint8)
            up = enhance(g8)
            ok, jpg = cv2.imencode(".jpg", up, [cv2.IMWRITE_JPEG_QUALITY, 88])
            if ok:
                with _lock:
                    _latest["jpg"] = jpg.tobytes()
    except Exception:
        pass
    finally:
        _active["proc"] = None
        try: proc.kill()
        except Exception: pass


def set_range(dev, high):
    """Set the HT-301 temperature range (commands/sequence from OpenRMH-IRCAM, MIT):
    0x8021 = high, 0x8020 = normal, then 500 ms until the camera has switched.
    The subsequent FFC is up to the caller."""
    try:
        dev.temperature_range_high() if high else dev.temperature_range_normal()
    except Exception:
        return
    time.sleep(0.5)
    _diag["range"] = "high" if high else "normal"


def run_cal():
    """Calibrated mode: cv2 raw -> °C, Min/Max markers, measurement points, colorbar.
    Runs as long as markers/color bar are requested."""
    cap = dev = None
    lo_s = hi_s = None
    try:
        cap = cv2.VideoCapture(DEV_INDEX, cv2.CAP_V4L2)
        try: cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception: pass
        dev = H.Camera(cap)
        if THERMAL_FPS > 0:
            try: cap.set(cv2.CAP_PROP_FPS, THERMAL_FPS)   # reduce USB load (if camera accepts it)
            except Exception: pass
        _active["dev"] = dev; _active["cap"] = cap
        if _emiss[0] is not None:
            dev.emissivity_override = float(_emiss[0]); _emiss_last[0] = float(_emiss[0])
        if _high_range[0]:
            set_range(dev, True)
            dev.calibrate()         # FFC in the new range (Camera.__init__ still ran in the old one)
        for _ in range(8):          # warm-up: discard first frames, then calibration is valid
            dev.read()
        lut = None; fc = 0; ema = None
        last_ffc = time.monotonic()   # Camera.__init__ has just triggered an FFC itself
        fpa_ref = [None]              # FPA temperature at the time of the last FFC

        def do_ffc(why):
            """Trigger shutter/FFC and discard ALL intermediate state that depends on it.
            After the FFC the values jump (measured here: +24 K) — EMA and scale smoothing
            must follow, otherwise they blend in the old state for seconds."""
            nonlocal lut, fc, ema, lo_s, hi_s, last_ffc
            try: dev.calibrate()
            except Exception: pass
            for _ in range(FFC_SETTLE):
                dev.read()                      # discard shutter frames
            lut = None; fc = 0; ema = None; lo_s = hi_s = None
            last_ffc = time.monotonic(); fpa_ref[0] = None
            _diag["ffc"] = {"at": time.strftime("%H:%M:%S"), "why": why}

        while want_cal():
            ret, frame = dev.read()
            if not ret or frame is None:
                break
            # --- periodic FFC: after a time interval OR on sensor temperature drift ---
            if FFC_INTERVAL > 0 and not _ffc_request[0] and _calib_target[0] is None:
                fpa = _diag.get("fpa")
                drift = (fpa is not None and fpa_ref[0] is not None
                         and abs(fpa - fpa_ref[0]) >= FFC_FPA_DELTA)
                if time.monotonic() - last_ffc >= FFC_INTERVAL or drift:
                    do_ffc("drift" if drift else "interval")
                    continue
            # Emissivity (env default or /emiss endpoint): host side, rebuild LUT.
            if _emiss_apply[0]:
                _emiss_apply[0] = False
                dev.emissivity_override = None if _emiss[0] is None else float(_emiss[0])
                _emiss_last[0] = dev.emissivity_override
                lut = None; fc = 0
                continue
            # Switch temperature range (/range endpoint): command, wait 0.5 s, FFC.
            if _range_apply[0]:
                _range_apply[0] = False
                set_range(dev, _high_range[0])
                do_ffc("range")
                continue
            # Self-calibrate: trigger internal camera calibration (FFC/shutter), then rebuild LUT.
            if _ffc_request[0]:
                _ffc_request[0] = False
                do_ffc("manual")
                continue
            if lut is None or fc % LUT_REFRESH == 0:
                try:
                    with np.errstate(all="ignore"):
                        inf, cand = dev.info()
                    cand = np.asarray(cand, dtype=np.float32)
                    # only accept a fully finite LUT (edge values of the table may be extreme!)
                    if np.all(np.isfinite(cand)):
                        lut = cand
                        fin = cand[np.isfinite(cand)]
                        _diag["lutmax"] = float(fin.max()) if fin.size else None
                    try:
                        _diag["emiss"] = float(inf.get("emissivity"))
                        # FPA (sensor) temperature: trigger for the drift-based FFC.
                        _diag["fpa"] = float(inf.get("temp_fpa"))
                        _diag["shutter"] = float(inf.get("temp_shutter"))
                        if fpa_ref[0] is None:
                            fpa_ref[0] = _diag["fpa"]   # reference is valid from the last FFC on
                    except Exception:
                        pass
                except Exception:
                    pass
            if lut is None:
                time.sleep(0.05)    # no busy-spin while calibration is (still) invalid
                continue
            fc += 1
            temp = lut[np.clip(frame, 0, len(lut) - 1)]
            ema = temp if ema is None else TEMPORAL * ema + (1 - TEMPORAL) * temp
            temp = ema
            tminC = float(temp.min()); tmaxC = float(temp.max()); _diag["tmax"] = tmaxC
            mn = np.unravel_index(int(np.argmin(temp)), temp.shape)
            mx = np.unravel_index(int(np.argmax(temp)), temp.shape)
            tminpt = (mn[1], mn[0]); tmaxpt = (mx[1], mx[0])
            # --- "Calibrate": find ε via bisection until hotspot == target temperature ---
            # (lower ε -> higher displayed temp). One-shot, blocks the stream ~1 s.
            if _calib_target[0] is not None:
                target = float(_calib_target[0]); _calib_target[0] = None
                hot_raw = int(np.clip(frame[mx[0], mx[1]], 0, len(lut) - 1))
                lo_e, hi_e, best = 0.5, 1.0, (_emiss_last[0] or 0.95)
                for _ in range(12):
                    mid = 0.5 * (lo_e + hi_e)
                    try:
                        dev.emissivity_override = mid
                        with np.errstate(all="ignore"):
                            _, cand = dev.info()
                        cand = np.asarray(cand, dtype=np.float32)
                        if not np.all(np.isfinite(cand)):
                            continue
                        t = float(cand[min(hot_raw, len(cand) - 1)])
                    except Exception:
                        break
                    best = mid
                    if abs(t - target) < 0.5:
                        break
                    if t < target: hi_e = mid     # temp too low -> reduce ε
                    else:          lo_e = mid
                _emiss[0] = best; _emiss_last[0] = best
                dev.emissivity_override = best
                lut = None; fc = 0
                continue
            # Scale from PERCENTILES, not from hard Min/Max: a single hot solder joint
            # (or a dead pixel) would otherwise stretch the whole scale, the scene collapsed into
            # a few gray levels and CLAHE had to tear it open brutally -> noise instead of detail.
            # The MARKERS still show the real Min/Max (tminC/tmaxC) — only the display
            # stretch uses the percentiles. The YUYV path has always done it this way.
            if AGC_PCT > 0:
                lo_r, hi_r = (float(v) for v in np.percentile(temp, (AGC_PCT, 100.0 - AGC_PCT)))
            else:
                lo_r, hi_r = tminC, tmaxC
            lo_s = lo_r if lo_s is None else RANGE_SMOOTH * lo_s + (1 - RANGE_SMOOTH) * lo_r
            hi_s = hi_r if hi_s is None else RANGE_SMOOTH * hi_s + (1 - RANGE_SMOOTH) * hi_r
            lo, hi = lo_s, max(lo_s + 0.5, hi_s)
            g8 = np.clip((temp - lo) / (hi - lo), 0, 1)
            g8 = (g8 * 255).astype(np.uint8)
            up = enhance(g8)
            pts = []
            with _plock:
                for (nx, ny) in _points:
                    px = min(SENSOR_W - 1, max(0, int(nx * SENSOR_W)))
                    py = min(SENSOR_H - 1, max(0, int(ny * SENSOR_H)))
                    pts.append((nx, ny, float(temp[py, px])))
            out = render(up, lo, hi, tmaxC, tmaxpt, tminC, tminpt, pts)
            ok, jpg = cv2.imencode(".jpg", out, [cv2.IMWRITE_JPEG_QUALITY, 88])
            if ok:
                with _lock:
                    _latest["jpg"] = jpg.tobytes()
    except Exception:
        pass
    finally:
        _active["dev"] = None; _active["cap"] = None
        try: dev.release()
        except Exception: pass
        try: cap.release()
        except Exception: pass


def supervisor():
    global DEV_INDEX, DEV
    last = None
    gone = True          # treat first access like after a replug -> settle
    while True:
        idx = find_dev_index()          # locate the IR camera by USB-unique-ID (whitelist)
        if idx is None:
            # No allowed IR camera plugged in -> open NOTHING (never grab a foreign camera).
            time.sleep(2)
            gone = True                 # next appearance is a fresh enumeration
            continue
        if idx != DEV_INDEX:
            DEV_INDEX = idx; DEV = "/dev/video%d" % idx
            gone = True                 # node changed -> camera was re-enumerated
        if gone:
            # SETTLE after (re)appearing. The HT-301 needs a few seconds after enumeration
            # before it accepts UVC controls. Accessing it immediately makes the driver answer
            # with "Failed to set UVC probe control : -110" (timeout) and the camera counts
            # as "not found" — exactly what happens on unplug/replug.
            time.sleep(SETTLE_SEC)
            gone = False
        mode = "cal" if want_cal() else "yuyv"
        if mode != last:
            # NO USB reset! That left the camera in a transitional state -> cv2 probe timeout
            # (-110). Clean release (SIGTERM handler/finally) is enough; just settle briefly.
            time.sleep(2)
            last = mode
        if mode == "cal":
            run_cal()
        else:
            run_yuyv()
        time.sleep(1)               # reconnect/switch pause


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _ok(self, body=b"ok"):
        self.send_response(200)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/points"):
            from urllib.parse import urlparse, parse_qs
            import json
            q = parse_qs(urlparse(self.path).query)
            try:
                if "markers" in q:                # toggle all markers (Min/Max + points) on/off
                    _show_markers[0] = (q["markers"][0] != "0")
                elif "bar" in q:                  # toggle color gradient/colorbar incl. background on/off
                    _show_bar[0] = (q["bar"][0] != "0")
                elif "clear" in q:
                    with _plock: _points.clear()
                elif "set" in q:
                    nx, ny = (float(v) for v in q["set"][0].split(","))
                    if 0 <= nx <= 1 and 0 <= ny <= 1:
                        with _plock:
                            _points.append((nx, ny))
                            if len(_points) > 8: _points.pop(0)
                elif "move" in q:                 # move=index,nx,ny  (move point)
                    i, nx, ny = q["move"][0].split(",")
                    i = int(i); nx = float(nx); ny = float(ny)
                    with _plock:
                        if 0 <= i < len(_points) and 0 <= nx <= 1 and 0 <= ny <= 1:
                            _points[i] = (nx, ny)
                elif "del" in q:
                    nx, ny = (float(v) for v in q["del"][0].split(","))
                    with _plock:
                        if _points:
                            j = min(range(len(_points)),
                                    key=lambda k: (_points[k][0] - nx) ** 2 + (_points[k][1] - ny) ** 2)
                            d = (_points[j][0] - nx) ** 2 + (_points[j][1] - ny) ** 2
                            if d < 0.01:          # only if close enough to the double-click (~10%)
                                _points.pop(j)
            except Exception:
                pass
            with _plock:
                body = json.dumps([[round(x, 4), round(y, 4)] for (x, y) in _points]).encode()
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/range"):
            from urllib.parse import urlparse, parse_qs
            import json
            q = parse_qs(urlparse(self.path).query)
            if "high" in q:
                _high_range[0] = q["high"][0] in ("1", "true", "on")
                _show_markers[0] = True              # range only takes effect in cal mode (°C)
                _range_apply[0] = True
            body = json.dumps({"high": _high_range[0], "diag": _diag}).encode()
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/emiss") or self.path.startswith("/calibrate"):
            from urllib.parse import urlparse, parse_qs
            import json
            q = parse_qs(urlparse(self.path).query)
            try:
                if self.path.startswith("/calibrate"):
                    _show_markers[0] = True          # activate cal mode (loop runs)
                    if "target" in q:                # ε search to target temperature (optional)
                        _calib_target[0] = float(q["target"][0])
                    else:                            # self-calibrate = internal FFC/shutter
                        _ffc_request[0] = True
                elif "e" in q:                        # manual ε
                    _emiss[0] = max(0.01, min(1.0, float(q["e"][0]))); _emiss_apply[0] = True
                elif "reset" in q:                    # back to the camera value
                    _emiss[0] = None; _emiss_apply[0] = True
            except Exception:
                pass
            body = json.dumps({"emissivity": _emiss_last[0] if _emiss_last[0] is not None else _emiss[0],
                               "calibrating": _calib_target[0], "diag": _diag}).encode()
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/stream"):
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                while True:
                    with _lock:
                        jpg = _latest["jpg"]
                    if jpg:
                        self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                         b"Content-Length: " + str(len(jpg)).encode() + b"\r\n\r\n")
                        self.wfile.write(jpg)
                        self.wfile.write(b"\r\n")
                    time.sleep(0.04)
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            with _lock:
                jpg = _latest["jpg"]
            self.send_response(200 if jpg else 503)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Content-Type", "image/jpeg")
            self.end_headers()
            if jpg:
                self.wfile.write(jpg)


def _on_signal(sig, frm):
    # Release camera/ffmpeg cleanly (otherwise raw mode stays stuck -> degraded / slow)
    for k in ("dev", "cap", "proc"):
        try:
            if _active.get(k):
                _active[k].release() if k != "proc" else _active[k].kill()
        except Exception:
            pass
    os._exit(0)


if __name__ == "__main__":
    import signal
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    threading.Thread(target=supervisor, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
