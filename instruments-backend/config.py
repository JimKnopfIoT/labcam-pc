"""Instrument STATE backend configuration.

All device addresses, ports, and polling rates in one place. Counterpart to
`overlay/config.js` on the frontend side.

After editing, restart the backend (server.py); this file is read at module import.
"""

# ---------- Lab instruments (LAN) ----------
DMM = ("<dmm-ip>", 5025)      # Keithley DMM7510  (TCP/SCPI)
BB3 = ("<bb3-ip>", 5025)      # Envox BB3 Ch1 + Ch2 (TCP/SCPI, shared connection)
KEL = ("<kel-ip>", 18190)     # Korad KEL103       (UDP, local bind on 18190 required)

# ---------- USB tester (BLE) ----------
# Shown in the instrument overlay panel "USB" -- real USB output behind the PD module.
# Currently active: Fnirsi C1. To switch to FNB-C2 just replace the MAC
# (protocol is identical: FNB48 family including C1/FNB-C2).
C1_MAC      = "AA:BB:CC:DD:EE:FF"
C1_NOTIFY_U = "0000ffe4-0000-1000-8000-00805f9b34fb"
C1_WRITE_U  = "0000ffe9-0000-1000-8000-00805f9b34fb"

# ---------- Network server ----------
WS_HOST   = "0.0.0.0"
WS_PORT   = 7891
HTTP_HOST = "0.0.0.0"
HTTP_PORT = 7890

# ---------- Polling rates ----------
DMM_PERIOD       = 0.25   # 4 Hz
BB3_PERIOD       = 0.25   # 4 Hz (for both BB3 channels combined)
KEL_PERIOD       = 0.50   # 2 Hz (UDP latency)
BROADCAST_PERIOD = 0.20   # 5 Hz to frontend

# ---------- Reconnect backoff ----------
RECONNECT_BACKOFF    = 2.0
TCP_BUSY_BACKOFF     = 8.0   # longer when the device still holds the old TCP session
C1_RECONNECT_BACKOFF = 5.0

# ---------- DER EE DE-5000 LCR meter ----------
# Default source is the wired CP2102. Switching to RN4871 (BLE) happens ONLY on a
# click in the frontend -- otherwise BLE is never scanned (saves power/time).
DE5000_SOURCE_DEFAULT = "cp2102"        # "cp2102" | "rn4871"
DE5000_BAUD           = 9600            # DE-5000 IR output = 9600 8N1
DE5000_SERIAL_PORT    = None            # None = auto (CP2102 VID:PID 10c4:ea60), else e.g. "/dev/ttyUSB0"
# RN4871 (BLE Transparent UART): set the MAC locally OR find it by advertised name.
DE5000_RN4871_MAC     = "AA:BB:CC:DD:EE:FF"  # empty = search by name only; set = direct connect (no 8s scan)
DE5000_RN4871_NAME    = "RN487"         # advertised-name prefix as fallback
DE5000_TX_UUID        = "49535343-1e4d-4bd9-ba61-23c647249616"  # notify: module -> PC
