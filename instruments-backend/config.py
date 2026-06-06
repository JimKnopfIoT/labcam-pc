"""Instrument STATE backend configuration.

All device addresses, ports, and polling rates in one place. Counterpart to
`overlay/config.js` on the frontend side.

After editing, restart the backend (server.py); this file is read at module import.
"""

# ---------- Lab instruments (LAN) ----------
DMM = ("192.168.10.45", 5025)      # Keithley DMM7510  (TCP/SCPI)
BB3 = ("192.168.10.78", 5025)      # Envox BB3 Ch1 + Ch2 (TCP/SCPI, shared connection)
KEL = ("192.168.10.83", 18190)     # Korad KEL103       (UDP, local bind on 18190 required)

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
