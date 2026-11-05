#!/usr/bin/env python3
"""Instrument STATE backend.

Polls the bench instruments in parallel and broadcasts their values as JSON over a
WebSocket (~5 Hz). Both the LabCam phone app and the web dashboard connect to this
as read-only clients. Device addresses live in config.py (example values below):

  DMM7510      <dmm-ip>:5025   TCP/SCPI                 -> "dmm"
  BB3 Ch1      <bb3-ip>:5025   TCP/SCPI                 -> "bb3a"
  BB3 Ch2      <bb3-ip>:5025   TCP/SCPI (telemetry)     -> internal "bb3b_raw"
  KEL103       <kel-ip>:18190  UDP (local bind on 18190)-> "kel"
  USB tester   BLE (FNB48 family)                            -> "bb3b" (USB output)
"""
import asyncio
import copy
import json
import logging
import os
import re
import socket
import struct
import sys
import time

import websockets

try:
    from bleak import BleakClient
    _have_bleak = True
except ImportError:
    _have_bleak = False

try:
    import serial
    from serial.tools import list_ports
    _have_serial = True
except ImportError:
    _have_serial = False

# Suppress spammy WebSocket handshake tracebacks (port scans, health checks)
logging.getLogger("websockets.server").setLevel(logging.CRITICAL)
logging.getLogger("websockets.asyncio.server").setLevel(logging.CRITICAL)

# ---------- Configuration ----------
# All device addresses, ports, and polling rates live in backend/config.py.
from config import (
    DMM, BB3, KEL,
    C1_MAC, C1_NOTIFY_U, C1_WRITE_U,
    WS_HOST, WS_PORT,
    DMM_PERIOD, BB3_PERIOD, KEL_PERIOD, BROADCAST_PERIOD,
    RECONNECT_BACKOFF, TCP_BUSY_BACKOFF, C1_RECONNECT_BACKOFF,
    DE5000_SOURCE_DEFAULT, DE5000_BAUD, DE5000_SERIAL_PORT,
    DE5000_RN4871_MAC, DE5000_RN4871_NAME, DE5000_TX_UUID,
)

# DMM7510 :SENS:FUNC? response → (UI label, unit)
DMM_MODE_MAP = {
    "VOLT:DC":      ("V DC",   "V"),
    "VOLT:AC":      ("V AC",   "V"),
    "CURR:DC":      ("A DC",   "A"),
    "CURR:AC":      ("A AC",   "A"),
    "RES":          ("Ω 2W",   "Ω"),
    "FRES":         ("Ω 4W",   "Ω"),
    "CAP":          ("Cap",    "F"),
    "DIOD":         ("Diode",  "V"),
    "FREQ:VOLT":    ("Freq",   "Hz"),
    "PER:VOLT":     ("Period", "s"),
    "TEMP":         ("Temp",   "°C"),
    "VOLT:DC:RAT":  ("Ratio",  ""),
}

# :SENS:DIG:FUNC? response -> UI label (the regular function then reads "NONE").
DMM_DIGITIZE_LABELS = {"VOLT": "Digi V", "CURR": "Digi I"}
# Closing the connection alone leaves the instrument showing "TCPIP" with its display
# frozen. This command returns it to local control and restarts continuous measuring.
DMM_LOCAL_CMD = b":TRIG:CONT REST\n"
DMM_DIGITIZE_PERIOD = 30.0  # s between short looks at the instrument while it is released

# Per-function: matching range query. Values without a meaningful range (Diode, Freq,
# Period, Temp, Ratio) are omitted -> frontend then falls back to value magnitude.
DMM_RANGE_CMD = {
    "VOLT:DC":  b":SENS:VOLT:DC:RANG?\n",
    "VOLT:AC":  b":SENS:VOLT:AC:RANG?\n",
    "CURR:DC":  b":SENS:CURR:DC:RANG?\n",
    "CURR:AC":  b":SENS:CURR:AC:RANG?\n",
    "RES":      b":SENS:RES:RANG?\n",
    "FRES":     b":SENS:FRES:RANG?\n",
    "CAP":      b":SENS:CAP:RANG?\n",
}

# ---------- Shared State ----------
STATE = {
    "ts": 0,
    "devices": {
        "dmm":  {"ok": False, "mode": "—", "value": None, "unit": "", "range": None},
        "bb3a":     {"ok": False, "mode": "—", "voltage": None, "current": None, "output": None},
        "bb3b":     {"ok": False, "mode": "—", "voltage": None, "current": None, "output": None,
                     "dp": None, "dn": None, "protocol": "—"},
        "bb3b_raw": {"ok": False, "mode": "—", "voltage": None, "current": None, "output": None},
        "kel":      {"ok": False, "mode": "—", "voltage": None, "current": None, "output": None},
        "lcr":      {"ok": False, "source": DE5000_SOURCE_DEFAULT, "status": "—",
                     "mode": "—", "value": None, "unit": "",
                     "sec_mode": "", "sec_value": None, "sec_unit": "", "freq": None},
    },
}
CLIENTS: set = set()

# DE-5000 transport selection. Default = wired CP2102; a client can switch to the
# RN4871 (BLE) at runtime — see _handle_cmd() / de5000_poll().
LCR_SOURCE = DE5000_SOURCE_DEFAULT
DEVICE_DEFAULTS = copy.deepcopy(STATE["devices"])
LCR_SWITCH = asyncio.Event()   # set to make de5000_poll() drop the current transport


def log(tag, msg):
    print(f"[{tag}] {msg}", file=sys.stderr, flush=True)


# ---------- DMM7510 ----------
async def _dmm_return_to_local():
    """Hand the instrument back to its front panel over a short-lived connection."""
    writer = None
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(*DMM), timeout=3.0)
        writer.write(DMM_LOCAL_CMD)
        await writer.drain()
        await asyncio.sleep(0.2)
    except (asyncio.TimeoutError, OSError) as e:
        log("dmm", f"return to local failed: {type(e).__name__}: {e}")
    finally:
        if writer is not None:
            writer.close()


async def _dmm_discard_pending(reader):
    """The DMM keeps an unread reply across connections: drop it, or every answer
    on the new connection would be off by one."""
    try:
        while await asyncio.wait_for(reader.read(4096), 0.05):
            pass
    except asyncio.TimeoutError:
        pass


async def _dmm_session():
    backoff = RECONNECT_BACKOFF
    released = False    # digitize mode: instrument is left to the front panel
    while True:
        writer = None
        try:
            log("dmm", f"connect {DMM[0]}:{DMM[1]}")
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(*DMM), timeout=5.0
            )
            if not released:
                writer.write(b"*lang scpi;FORMat:ASCii:PRECision MAXimum\n")
                await writer.drain()
                await asyncio.sleep(0.2)
                await _dmm_discard_pending(reader)
            log("dmm", "connected")
            backoff = RECONNECT_BACKOFF
            while True:
                writer.write(b":SENS:FUNC?\n")
                await writer.drain()
                func_raw = (await asyncio.wait_for(reader.readline(), 2.0)).decode().strip().strip('"')

                # Sanity check: mode strings are words ("VOLT:DC", "DIOD", ...),
                # never numeric. If func_raw starts with a digit or sign,
                # the TCP response order has slipped. Flush the buffer
                # and restart the next round cleanly.
                # The same goes for anything not shaped like a SCPI function name:
                # otherwise a slipped text response or error message would be shown
                # as the DMM mode. Functions missing from DMM_MODE_MAP (e.g. "CONT")
                # are valid and still pass, displayed under their short raw name.
                if not re.fullmatch(r"[A-Z][A-Z:]{1,19}", func_raw):
                    log("dmm", f"SCPI desync, discarding: {func_raw[:30]!r}")
                    try:
                        await asyncio.wait_for(reader.readline(), 0.3)
                    except asyncio.TimeoutError:
                        pass
                    await asyncio.sleep(0.3)
                    continue

                # Digitize active: the regular function reads "NONE" and READ? would only
                # raise error 2801 on the device and never answer (-> timeout, reconnect).
                # Digi V / Digi I are operated at the instrument itself, so release it:
                # report the mode only (ok=False -> frontends show the mode with "—"),
                # disconnect, and only look briefly now and then whether the mode changed.
                if func_raw == "NONE":
                    writer.write(b":SENS:DIG:FUNC?\n")
                    await writer.drain()
                    dig_raw = (await asyncio.wait_for(reader.readline(), 2.0)).decode().strip().strip('"')
                    STATE["devices"]["dmm"] = {
                        "ok": False, "mode": DMM_DIGITIZE_LABELS.get(dig_raw, "—"),
                        "value": None, "unit": "", "range": None
                    }
                    writer.write(DMM_LOCAL_CMD)
                    await writer.drain()
                    await asyncio.sleep(0.2)
                    writer.close()
                    await writer.wait_closed()
                    writer = None
                    released = True
                    await asyncio.sleep(DMM_DIGITIZE_PERIOD)
                    break

                if released:    # back in a normal function: resume regular polling
                    released = False
                    writer.write(b"*lang scpi;FORMat:ASCii:PRECision MAXimum\n")
                    await writer.drain()
                    await asyncio.sleep(0.2)

                writer.write(b':READ? "defbuffer1",READ\n')
                await writer.drain()
                val_raw = (await asyncio.wait_for(reader.readline(), 2.0)).decode().strip()

                # Query the current range if the function supports it. Passing this
                # value to the frontend lets it couple the display unit (mV/V/uV
                # or mA/A ...) and the Y-scale to the device range — so nothing
                # jumps on value changes within the same range.
                rng = None
                range_cmd = DMM_RANGE_CMD.get(func_raw)
                if range_cmd is not None:
                    writer.write(range_cmd)
                    await writer.drain()
                    range_raw = (await asyncio.wait_for(reader.readline(), 2.0)).decode().strip()
                    try:
                        rng = float(range_raw)
                    except (ValueError, TypeError):
                        rng = None

                label, unit = DMM_MODE_MAP.get(func_raw, (func_raw, ""))
                try:
                    value = float(val_raw)
                except (ValueError, TypeError):
                    value = None
                STATE["devices"]["dmm"] = {
                    "ok": True, "mode": label, "value": value, "unit": unit, "range": rng
                }
                await asyncio.sleep(DMM_PERIOD)
        except asyncio.CancelledError:      # client disconnected the instrument
            if writer is not None:
                writer.close()
            raise
        except (asyncio.TimeoutError, OSError, ConnectionError) as e:
            STATE["devices"]["dmm"]["ok"] = False
            # On "connection refused/timeout" the device usually still holds the old TCP session
            backoff = min(backoff * 1.5, TCP_BUSY_BACKOFF * 2) if "Connect call failed" in str(e) or isinstance(e, (asyncio.TimeoutError, ConnectionRefusedError)) else RECONNECT_BACKOFF
            log("dmm", f"error: {type(e).__name__}: {e} — retry in {backoff:.1f}s")
            if writer is not None:
                try:
                    writer.close(); await writer.wait_closed()
                except Exception:
                    pass
            await asyncio.sleep(backoff)
        except Exception as e:
            STATE["devices"]["dmm"]["ok"] = False
            log("dmm", f"unexpected: {type(e).__name__}: {e}")
            if writer is not None:
                try:
                    writer.close(); await writer.wait_closed()
                except Exception:
                    pass
            await asyncio.sleep(RECONNECT_BACKOFF)


# ---------- BB3 ----------
async def _bb3_session():
    backoff = RECONNECT_BACKOFF
    while True:
        writer = None
        try:
            log("bb3", f"connect {BB3[0]}:{BB3[1]}")
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(*BB3), timeout=5.0
            )
            log("bb3", "connected")
            backoff = RECONNECT_BACKOFF
            while True:
                # CH1 -> "bb3a" is shown in the instrument overlay.
                # CH2 -> "bb3b_raw" is internal telemetry; the displayed "bb3b" panel
                # is populated by the Fnirsi C1 (BLE) (= real USB output behind PD module).
                for key, ch in (("bb3a", "CH1"), ("bb3b_raw", "CH2")):
                    writer.write(f"MEAS:VOLT? {ch}\n".encode())
                    await writer.drain()
                    v = float((await asyncio.wait_for(reader.readline(), 2.0)).decode().strip())

                    writer.write(f"MEAS:CURR? {ch}\n".encode())
                    await writer.drain()
                    a = float((await asyncio.wait_for(reader.readline(), 2.0)).decode().strip())

                    writer.write(f"OUTP:MODE? {ch}\n".encode())
                    await writer.drain()
                    mode = (await asyncio.wait_for(reader.readline(), 2.0)).decode().strip().strip('"')

                    writer.write(f"OUTP? {ch}\n".encode())
                    await writer.drain()
                    outp_raw = (await asyncio.wait_for(reader.readline(), 2.0)).decode().strip()
                    output_on = outp_raw == "1"

                    STATE["devices"][key] = {
                        "ok": True, "mode": mode, "voltage": v, "current": a, "output": output_on
                    }
                await asyncio.sleep(BB3_PERIOD)
        except asyncio.CancelledError:      # client disconnected the instrument
            if writer is not None:
                writer.close()
            raise
        except (asyncio.TimeoutError, OSError, ConnectionError) as e:
            STATE["devices"]["bb3a"]["ok"] = False
            STATE["devices"]["bb3b_raw"]["ok"] = False
            # On "connection refused/timeout" the device usually still holds the old TCP session
            backoff = min(backoff * 1.5, TCP_BUSY_BACKOFF * 2) if "Connect call failed" in str(e) or isinstance(e, (asyncio.TimeoutError, ConnectionRefusedError)) else RECONNECT_BACKOFF
            log("bb3", f"error: {type(e).__name__}: {e} — retry in {backoff:.1f}s")
            if writer is not None:
                try:
                    writer.close(); await writer.wait_closed()
                except Exception:
                    pass
            await asyncio.sleep(backoff)
        except Exception as e:
            STATE["devices"]["bb3a"]["ok"] = False
            STATE["devices"]["bb3b_raw"]["ok"] = False
            log("bb3", f"unexpected: {type(e).__name__}: {e}")
            if writer is not None:
                try:
                    writer.close(); await writer.wait_closed()
                except Exception:
                    pass
            await asyncio.sleep(RECONNECT_BACKOFF)


# ---------- Fnirsi C1 USB tester (BLE) ----------
# Protocol from blakelton/FNIRSI-FNB-Web-Server (Android app reverse engineering).
# Init: enable notifications -> GET_INFO (0x81) -> GET_STATUS (0x85) -> START (0x82).
# Notifications contain concatenated sub-packets [0xAA][CMD][LEN][DATA...][CRC8].
# Main data in CMD=0x04: V/I/P as int32-LE x 1/10000.
# Staleness watchdog: the BLE link can stay "connected" while the tester stops
# sending notifications. Without these thresholds the panel freezes on the last
# value forever (looks live, is not).
C1_STALE_S   = 6.0     # no measurement packet -> device counts as offline
C1_RESTART_S = 12.0    # ... re-issue START once (maybe it only stopped streaming)
C1_DEAD_S    = 30.0    # ... give up and rebuild the BLE session


# ---------- Fnirsi C1 USB tester (BLE) ----------
# Protocol from blakelton/FNIRSI-FNB-Web-Server (Android app reverse engineering).
# Init: enable notifications -> GET_INFO (0x81) -> GET_STATUS (0x85) -> START (0x82).
# Notifications contain concatenated sub-packets [0xAA][CMD][LEN][DATA...][CRC8].
# Main data in CMD=0x04: V/I/P as int32-LE x 1/10000.
C1_CMD_GET_INFO   = 0x81
C1_CMD_START      = 0x82
C1_CMD_STOP       = 0x84
C1_CMD_GET_STATUS = 0x85


def _crc16_xmodem(data):
    crc = 0
    for byte in data:
        for bit in range(8):
            x = ((byte >> (7 - bit)) & 1) == 1
            m = ((crc >> 15) & 1) == 1
            crc = (crc << 1) & 0xFFFF
            if x ^ m:
                crc ^= 0x1021
    return crc & 0xFFFF


def _c1_build_cmd(cmd, payload=b""):
    pkt = bytes([0xAA, cmd, len(payload)]) + bytes(payload)
    return pkt + bytes([_crc16_xmodem(pkt) & 0xFF])


def _c1_protocol(voltage, dp, dn):
    """Heuristic protocol detection from V and D+/D-.
    Apple tolerances wider than in blakelton/FNIRSI-FNB-Web-Server
    (the C1 display recognizes Apple 2.4A even at D+/D- ~2.45 V, i.e. below
    the 2.5-2.9 V range documented there).
    """
    if voltage is None or dp is None or dn is None:
        return "—"

    # QC2.0 signals on D+/D- with THREE discrete levels: 0 V, 0.6 V and 3.3 V.
    # (The old table used a 0.25-0.45 V bucket, which does not exist in the spec, and
    #  matched 0.6/0.6 -- i.e. an ordinary 5 V source -- as "QC 12V".)
    def qc_level(v):
        if v <= 0.35:          return 0
        if 0.45 <= v <= 0.85:  return 6      # 0.6 V
        if v >= 2.8:           return 33     # 3.3 V
        return None

    #                 D+   D-
    qc = {(6, 0): 5,          # 0.6 / 0     ->  5 V
          (33, 6): 9,         # 3.3 / 0.6   ->  9 V
          (6, 6): 12,         # 0.6 / 0.6   -> 12 V
          (33, 33): 20,       # 3.3 / 3.3   -> 20 V
          }.get((qc_level(dp), qc_level(dn)))

    # Above 5 V the BUS VOLTAGE decides what was actually negotiated. A QC pattern is only
    # believable if the rail really sits at that voltage -- reporting "QC 12V" on a 5 V bus
    # (the old behaviour) is self-contradictory.
    if voltage > 5.8:
        for volt, name in ((9, "9V"), (12, "12V"), (15, "15V"), (20, "20V")):
            if abs(voltage - volt) <= 0.6:
                # QC2.0 has no 15 V step -> that one is always PD.
                return f"QC {name}" if (qc == volt and volt != 15) else f"PD {name}"
        return f"PD {voltage:.1f}V"

    # --- 5 V rail from here on ---
    # QC may only be claimed if it negotiated 5 V too.
    if qc == 5:
        return "QC 5V"

    # A BC1.2 DCP shorts D+ to D-, so both lines sit on the SAME potential -- whatever level
    # the meter's own pull puts them at. That equality (not the absolute value) is the tell.
    # Measured on this rig: D+ 0.620 V / D- 0.622 V, i.e. 2 mV apart -> bridged.
    bridged = abs(dp - dn) < 0.05
    # Apple 2.4A: D+ ~= D- ~= 2.7 V (wider tolerance: 2.3-3.0)
    if 2.3 <= dp <= 3.0 and 2.3 <= dn <= 3.0:
        return "Apple 2.4A"
    # Apple 2.1A: one ~= 2.7 V, the other ~= 2.0 V
    if (2.3 <= dp <= 3.0 and 1.7 <= dn <= 2.2) or (1.7 <= dp <= 2.2 and 2.3 <= dn <= 3.0):
        return "Apple 2.1A"
    # Both lines at ground: no charger signalling at all -> plain host port.
    if bridged and dp < 0.15:
        return "SDP 5V"
    # 2.0 V on BOTH lines is genuinely ambiguous from a pure voltage measurement:
    # Apple 1A (2.0/2.0 divider) and DCP (bridged) look identical. Both mean "5 V dumb
    # charger", so label them honestly instead of guessing one.
    # (The old order had Apple 1A first, which made the DCP branch unreachable anyway.)
    if bridged and 1.7 <= dp <= 2.2:
        return "DCP/Apple 1A"
    if bridged:
        return "DCP"
    # Fallback: Standard Downstream Port 5 V (SDP)
    if 4.5 <= voltage <= 5.5:                       return "SDP 5V"
    return "—"


def _c1_parse_notification(data, state):
    """Walk concatenated sub-packets and update `state` in-place.

    CMD=0x04 -> V/I/P (int32 LE x 1/10000)
    CMD=0x06 -> D- (uint16 LE), D+ (uint16 LE), both x 1/1000

    Returns True if a measurement packet (0x04) was seen -- the caller uses that
    as the liveness signal for the staleness watchdog.
    """
    got_data = False
    i = 0
    while i < len(data):
        if data[i] != 0xAA or i + 2 >= len(data):
            i += 1
            continue
        cmd = data[i + 1]
        length = data[i + 2]
        if i + 3 + length > len(data):
            break
        body = data[i + 3 : i + 3 + length]
        if cmd == 0x04 and len(body) >= 12:
            v, c, _p = struct.unpack_from("<iii", body, 0)
            state["voltage"] = v / 10000.0
            state["current"] = c / 10000.0
            got_data = True
        elif cmd == 0x06 and len(body) >= 4:
            dn, dp = struct.unpack_from("<HH", body, 0)
            state["dn"] = dn / 1000.0
            state["dp"] = dp / 1000.0
        # Re-detect protocol -- D+/D- normally only changes on re-negotiation,
        # but this keeps the state always consistent
        state["protocol"] = _c1_protocol(state.get("voltage"), state.get("dp"), state.get("dn"))
        i += 3 + length + 1
    return got_data


def _c1_mark_offline(state):
    """Drop the last reading. The C1 keeps the BLE link up even when it stops
    streaming (tester switched off / went idle) -- without this the frontends
    would keep showing the frozen last value as if it were live."""
    state["ok"] = False
    state["mode"] = "—"
    state["voltage"] = None
    state["current"] = None
    state["dp"] = None
    state["dn"] = None
    state["output"] = None
    state["protocol"] = "—"


async def _c1_force_disconnect():
    """Resolve a stale BLE session via bluetoothctl -- the C1 otherwise stubbornly
    holds its previous connect state, and a fresh BleakClient.connect() then
    hits UNLIKELY_ERROR or timeout."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "disconnect", C1_MAC,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except Exception:
        pass


async def _c1_session():
    if not _have_bleak:
        log("c1", "bleak not found -- poller disabled. `pip install bleak`")
        return
    backoff = C1_RECONNECT_BACKOFF
    while True:
        client = None
        try:
            await _c1_force_disconnect()  # clean up stale session
            await asyncio.sleep(0.5)
            log("c1", f"connect {C1_MAC}")
            client = BleakClient(C1_MAC, timeout=15.0)
            await client.connect()
            log("c1", "connected")
            backoff = C1_RECONNECT_BACKOFF
            state = STATE["devices"]["bb3b"]

            live = {"t": time.monotonic()}

            def cb(_, data):
                if not _c1_parse_notification(bytes(data), state):
                    return          # status/info packet -- no fresh measurement
                live["t"] = time.monotonic()
                state["ok"] = True
                state["mode"] = "USB"
                state["output"] = True

            await client.start_notify(C1_NOTIFY_U, cb)
            await asyncio.sleep(0.5)
            await client.write_gatt_char(C1_WRITE_U, _c1_build_cmd(C1_CMD_GET_INFO))
            await asyncio.sleep(1.0)
            await client.write_gatt_char(C1_WRITE_U, _c1_build_cmd(C1_CMD_GET_STATUS))
            await asyncio.sleep(0.3)
            await client.write_gatt_char(C1_WRITE_U, _c1_build_cmd(C1_CMD_START))

            restarted = False

            while client.is_connected:
                await asyncio.sleep(0.5)
                age = time.monotonic() - live["t"]
                if age > C1_STALE_S and state["ok"]:
                    _c1_mark_offline(state)
                    log("c1", f"no data for {age:.0f}s — marking offline")
                if age > C1_RESTART_S and not restarted:
                    restarted = True
                    try:
                        await client.write_gatt_char(C1_WRITE_U, _c1_build_cmd(C1_CMD_START))
                    except Exception:
                        pass
                if age > C1_DEAD_S:
                    log("c1", "stream dead — rebuilding BLE session")
                    break
                if age < C1_STALE_S:
                    restarted = False
            log("c1", "disconnected")
        except Exception as e:
            _c1_mark_offline(STATE["devices"]["bb3b"])
            log("c1", f"error: {type(e).__name__}: {e} — retry in {backoff:.1f}s")
            await asyncio.sleep(backoff)
            backoff = min(backoff * 1.5, 30.0)
        finally:
            _c1_mark_offline(STATE["devices"]["bb3b"])
            # Clean disconnect -- STOP + disconnect -- so the C1 does not get
            # stuck in a zombie session.
            if client is not None and client.is_connected:
                try:
                    await client.write_gatt_char(C1_WRITE_U, _c1_build_cmd(C1_CMD_STOP))
                    await asyncio.sleep(0.1)
                except Exception:
                    pass
                try:
                    await client.disconnect()
                except Exception:
                    pass


# ---------- KEL103 (UDP, sync in the executor) ----------
def _kel_query(cmd, timeout=0.8, retries=2):
    """Fresh socket with local bind on 18190. Up to (1+retries) attempts."""
    last_err = None
    for _ in range(1 + retries):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", KEL[1]))
        s.settimeout(timeout)
        try:
            s.sendto((cmd + "\n").encode(), KEL)
            data, _ = s.recvfrom(4096)
            return data.decode(errors="replace").strip()
        except socket.timeout as e:
            last_err = e
        finally:
            s.close()
    raise last_err if last_err else TimeoutError(f"KEL query failed: {cmd}")


def _kel_parse_num(s: str) -> float:
    """'5.000V' → 5.0, '-1.23e-04A' → -1.23e-04"""
    i = 0
    while i < len(s) and (s[i].isdigit() or s[i] in ".-+eE"):
        i += 1
    return float(s[:i]) if i else 0.0


def _kel_read_all() -> dict:
    v = _kel_parse_num(_kel_query(":MEAS:VOLT?"))
    a = _kel_parse_num(_kel_query(":MEAS:CURR?"))
    mode = _kel_query(":FUNC?")
    onoff = _kel_query(":INP?")
    return {"ok": True, "mode": mode, "voltage": v, "current": a, "output": onoff}


async def _kel_session():
    loop = asyncio.get_event_loop()
    while True:
        try:
            data = await loop.run_in_executor(None, _kel_read_all)
            STATE["devices"]["kel"] = data
        except Exception as e:
            STATE["devices"]["kel"]["ok"] = False
            log("kel", f"error: {type(e).__name__}: {e}")
            await asyncio.sleep(RECONNECT_BACKOFF)
            continue
        await asyncio.sleep(KEL_PERIOD)


# ---------- DER EE DE-5000 LCR meter (CP2102 serial OR RN4871 BLE) ----------
# 17-byte packet: header 0x00 0x0D, footer 0x0D 0x0A. The byte stream is identical
# on both transports — the RN4871 only tunnels the meter's UART over BLE Transparent
# UART — so one decoder serves both. Default source is the wired CP2102; the frontend
# switches to RN4871 by tapping the DE-5000 label. BLE is scanned ONLY on demand.
_DE5000_QTY  = {1: "L", 2: "C", 3: "R", 4: "R"}
_DE5000_SEC  = {0: "", 1: "D", 2: "Q", 3: "ESR", 4: "θ"}
_DE5000_FREQ = {0: 100, 1: 120, 2: 1000, 3: 10000, 4: 100000, 5: None}
# unit code (info byte, bits 3-7) -> (base-SI symbol, factor to base unit)
_DE5000_UNIT = {
    0:  ("", 1.0),
    1:  ("Ω", 1.0),   2:  ("Ω", 1e3),  3:  ("Ω", 1e6),
    5:  ("H", 1e-6),  6:  ("H", 1e-3), 7:  ("H", 1.0), 8: ("H", 1e3),
    9:  ("F", 1e-12), 10: ("F", 1e-9), 11: ("F", 1e-6), 12: ("F", 1e-3),
    13: ("%", 1.0),   14: ("°", 1.0),
}


def _de5000_value(msb, lsb, info, disp):
    """(value in base SI, unit symbol) — or (None, '') on overload."""
    if disp == 3:                       # OL / overload
        return None, ""
    raw = (msb << 8) | lsb
    mul = info & 0x07                   # 10^-mul was applied at encode time
    sym, factor = _DE5000_UNIT.get((info >> 3) & 0x1F, ("", 1.0))
    return (raw / (10 ** mul)) * factor, sym


def _de5000_parse(f):
    """Decode one validated 17-byte frame into a STATE-style partial dict."""
    flags = f[2]
    main_qty = f[5]
    value, unit = _de5000_value(f[6], f[7], f[8], f[9])
    sec_value, sec_unit = _de5000_value(f[11], f[12], f[13], f[14])
    q = _DE5000_QTY.get(main_qty, "?")
    mode = "R (DC)" if main_qty == 4 else q + ("p" if flags & 0x80 else "s")  # Cs/Cp/Ls/Lp/Rs/Rp
    return {
        "mode": mode, "value": value, "unit": unit,
        "sec_mode": _DE5000_SEC.get(f[10], ""), "sec_value": sec_value, "sec_unit": sec_unit,
        "freq": _DE5000_FREQ.get((f[3] >> 5) & 0x07),
    }


def _de5000_frames(buf: bytearray):
    """Pull complete frames out of buf (resync on header+footer), keep the tail."""
    frames, i, n = [], 0, len(buf)
    while i + 17 <= n:
        if buf[i] == 0x00 and buf[i + 1] == 0x0D and buf[i + 15] == 0x0D and buf[i + 16] == 0x0A:
            frames.append(bytes(buf[i:i + 17]))
            i += 17
        else:
            i += 1
    del buf[:i]
    return frames


def _find_cp2102():
    """Return the CP2102 serial device path (VID:PID 10c4:ea60), or None."""
    if DE5000_SERIAL_PORT:
        return DE5000_SERIAL_PORT
    if not _have_serial:
        return None
    for p in list_ports.comports():
        if (p.vid, p.pid) == (0x10C4, 0xEA60):   # Silicon Labs CP2102
            return p.device
    return None


async def _wait_or_switch(t):
    """Sleep up to t s, but return immediately if a source switch was requested."""
    try:
        await asyncio.wait_for(LCR_SWITCH.wait(), timeout=t)
    except asyncio.TimeoutError:
        pass


async def _de5000_serial(st):
    if not _have_serial:
        log("lcr", "pyserial missing — `pip install pyserial`")
        st["status"] = "offline"
        await _wait_or_switch(5.0)
        return
    port = _find_cp2102()
    if not port:
        st["ok"] = False
        st["status"] = "offline"
        await _wait_or_switch(2.0)          # cheap poll, no BLE scanning
        return
    loop = asyncio.get_event_loop()
    ser = await loop.run_in_executor(
        None, lambda: serial.Serial(port, DE5000_BAUD, timeout=0.2, dsrdtr=False, rtscts=False))
    log("lcr", f"CP2102 {port} @ {DE5000_BAUD} 8N1")
    st["status"] = "usb"
    buf = bytearray()
    try:
        while not LCR_SWITCH.is_set():
            chunk = await loop.run_in_executor(None, ser.read, 64)
            if not chunk:
                continue
            buf.extend(chunk)
            for fr in _de5000_frames(buf):
                st.update(_de5000_parse(fr))
                st["ok"] = True
                st["source"] = "cp2102"
                st["status"] = "usb"
    finally:
        await loop.run_in_executor(None, ser.close)


async def _de5000_force_disconnect():
    """Clear a stale BlueZ link so the module advertises again and a fresh
    connect() does not hit 'Not connected' — same lesson as _c1_force_disconnect."""
    if not DE5000_RN4871_MAC:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "bluetoothctl", "disconnect", DE5000_RN4871_MAC,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=3.0)
    except Exception:
        pass


async def _de5000_ble(st):
    if not _have_bleak:
        log("lcr", "bleak missing")
        st["status"] = "offline"
        await _wait_or_switch(5.0)
        return
    from bleak import BleakScanner
    st["status"] = "searching"
    st["ok"] = False
    await _de5000_force_disconnect()     # drop stale link -> module advertises + clean connect
    await asyncio.sleep(0.5)
    dev = None
    if DE5000_RN4871_MAC:
        dev = await BleakScanner.find_device_by_address(DE5000_RN4871_MAC, timeout=10.0)
    if dev is None:                          # fall back to advertised-name prefix
        found = await BleakScanner.discover(timeout=8.0)
        dev = next((d for d in found if d.name and d.name.startswith(DE5000_RN4871_NAME)), None)
    if dev is None:
        log("lcr", "RN4871 not found")
        st["status"] = "offline"
        await _wait_or_switch(3.0)
        return
    log("lcr", f"RN4871 connect {dev.address}")
    buf = bytearray()

    def cb(_, data):
        buf.extend(bytes(data))
        for fr in _de5000_frames(buf):
            st.update(_de5000_parse(fr))
            st["ok"] = True
            st["source"] = "rn4871"
            st["status"] = "ble"

    async with BleakClient(dev, timeout=20.0) as client:
        st["status"] = "ble"
        # Verify the Transparent-UART TX characteristic exists and can notify.
        # A missing/no-notify char means the module is not in Transparent UART
        # mode (SS,C0) — log it clearly instead of hanging silently in start_notify.
        tx = client.services.get_characteristic(DE5000_TX_UUID)
        if tx is None or "notify" not in tx.properties:
            log("lcr", f"RN4871 TX char {DE5000_TX_UUID} missing/no-notify "
                       f"— Transparent UART (SS,C0) configured?")
            st["ok"] = False
            st["status"] = "offline"
            await _wait_or_switch(5.0)
            return
        await client.start_notify(DE5000_TX_UUID, cb)
        log("lcr", "RN4871 notify subscribed")
        while client.is_connected and not LCR_SWITCH.is_set():
            await asyncio.sleep(0.5)


async def de5000_poll():
    st = STATE["devices"]["lcr"]
    while True:
        LCR_SWITCH.clear()
        st["source"] = LCR_SOURCE
        try:
            if LCR_SOURCE == "rn4871":
                await _de5000_ble(st)
            else:
                await _de5000_serial(st)
        except Exception as e:
            st["ok"] = False
            st["status"] = "offline"
            log("lcr", f"error: {type(e).__name__}: {e}")
            await _wait_or_switch(RECONNECT_BACKOFF)


# ---------- WebSocket ----------
# Instruments a client can disconnect (double click on the name in the panel). While
# disconnected there is no query and no open connection at all.
DEVICES = {
    "dmm": {"session": _dmm_session, "keys": ("dmm",), "release": _dmm_return_to_local},
    "bb3": {"session": _bb3_session, "keys": ("bb3a", "bb3b_raw")},
    "usb": {"session": _c1_session,  "keys": ("bb3b",), "release": _c1_force_disconnect},
    "kel": {"session": _kel_session, "keys": ("kel",)},
}
# Which devices are disconnected survives a restart: the user may be working at the
# instrument, and a reboot must not put it back into remote mode.
DEVICE_STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "device_connections.json")


def _load_disconnected():
    try:
        with open(DEVICE_STATE_FILE) as f:
            names = json.load(f)["disconnected"]
        return {name for name in names if name in DEVICES}
    except FileNotFoundError:
        return set()
    except (OSError, ValueError, KeyError, TypeError) as e:
        print(f"[devices] ignoring {DEVICE_STATE_FILE}: {type(e).__name__}: {e}", file=sys.stderr)
        return set()


def _save_disconnected():
    tmp = DEVICE_STATE_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({"disconnected": sorted(n for n, d in DEVICES.items() if d["off"])}, f)
        os.replace(tmp, DEVICE_STATE_FILE)
    except OSError as e:
        print(f"[devices] cannot save {DEVICE_STATE_FILE}: {type(e).__name__}: {e}", file=sys.stderr)


_disconnected = _load_disconnected()
for _name, _dev in DEVICES.items():
    _dev["off"] = _name in _disconnected
    _dev["wake"] = asyncio.Event()


async def device_supervisor(name):
    """Runs the device's session unless a client asked to disconnect it."""
    dev = DEVICES[name]
    while True:
        dev["wake"].clear()
        for key in dev["keys"]:
            STATE["devices"][key] = copy.deepcopy(DEVICE_DEFAULTS[key])
            if dev["off"]:
                STATE["devices"][key]["disconnected"] = True
        if dev["off"]:
            await dev["wake"].wait()
            continue
        session = asyncio.create_task(dev["session"]())
        await dev["wake"].wait()
        session.cancel()
        try:
            await session
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log(name, f"session ended: {type(e).__name__}: {e}")
        if dev["off"] and "release" in dev:
            await dev["release"]()


# ---------- WebSocket ----------
def _handle_cmd(msg):
    """Client -> backend command. Currently: switch the DE-5000 transport source."""
    global LCR_SOURCE
    try:
        obj = json.loads(msg)
    except (ValueError, TypeError):
        return
    # States the wanted result instead of toggling, so a double click on PC and phone
    # at the same time cannot undo itself.
    if (obj.get("cmd") == "device_connection" and obj.get("device") in DEVICES
            and isinstance(obj.get("connected"), bool)):
        dev = DEVICES[obj["device"]]
        off = not obj["connected"]
        if dev["off"] != off:
            dev["off"] = off
            log(obj["device"], f"disconnected={off} (client request)")
            _save_disconnected()
            dev["wake"].set()
    if obj.get("cmd") == "lcr_source":
        val = obj.get("value")
        if val == "toggle":
            val = "rn4871" if LCR_SOURCE == "cp2102" else "cp2102"
        if val in ("cp2102", "rn4871") and val != LCR_SOURCE:
            LCR_SOURCE = val
            log("lcr", f"source -> {val} (client request)")
            LCR_SWITCH.set()


async def ws_handler(ws):
    CLIENTS.add(ws)
    log("ws", f"client connected ({len(CLIENTS)} total)")
    try:
        await ws.send(json.dumps(STATE))   # send current state immediately on connect
        async for msg in ws:
            _handle_cmd(msg)
    finally:
        CLIENTS.discard(ws)
        log("ws", f"client disconnected ({len(CLIENTS)} left)")


async def broadcaster():
    while True:
        await asyncio.sleep(BROADCAST_PERIOD)
        if not CLIENTS:
            continue
        STATE["ts"] = int(time.time() * 1000)
        msg = json.dumps(STATE)
        await asyncio.gather(
            *(ws.send(msg) for ws in list(CLIENTS)),
            return_exceptions=True,
        )


async def main():
    log("ws", f"serving on ws://{WS_HOST}:{WS_PORT}")
    async with websockets.serve(ws_handler, WS_HOST, WS_PORT):
        await asyncio.gather(
            *(device_supervisor(name) for name in DEVICES),
            de5000_poll(),
            broadcaster(),
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log("main", "shutdown")
