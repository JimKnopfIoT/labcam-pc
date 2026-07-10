// Configuration for the browser variant.
// The host is derived automatically from the current URL so the page
// reaches services on the SAME host both locally (http://127.0.0.1:8080)
// and from another machine on the LAN (http://<pc-ip>:8080).
// Overridable via localStorage (see app.js: identifyUrl/stateWsUrl/maxEdge).
(function () {
  var h = (location.protocol === "http:" || location.protocol === "https:") && location.hostname
    ? location.hostname
    : "127.0.0.1";
  window.LABCAM_WEB = {
    // Component identification service (labcam-component-id, :7895 on the same host).
    identifyUrl: "http://" + h + ":7895/identify",
    // Instrument STATE WebSocket (instrument STATE backend, :7891, read-only for the overlay).
    stateWsUrl: "ws://" + h + ":7891",
    // Long edge of the ROI crop sent to /identify (keeps token usage low).
    maxEdge: 1568,
    // SDS2504X-HD oscilloscope — noVNC web GUI (fixed device IP, NOT derived from host).
    // Shown as a side panel on the right via button.
    scopeUrl: "http://<scope-ip>/Instrument/novnc/vnc_auto.php",
    // HT-301 thermal camera — MJPEG from the labcam-thermal service (:7896 on the same host).
    thermalUrl: "http://" + h + ":7896/stream",
    // Keithley DMM7510 — Virtual Front Panel. Same-origin path: serve.py proxies /front_panel.html
    // (+ /script /css /ajax_proc /images) to the DMM (LABCAM_DMM_HOST) and appends auth.
    // Same-origin is required because the VFP page accesses top.document (cross-origin
    // iframe throws SecurityError -> canvas stays black). Via button (📟), same slot as scope.
    dmmUrl: "/front_panel.html",
    // Microscope camera WHITELIST. ONLY cameras matching these lists are opened — every other
    // camera (IR/thermal, action cams, webcams, ...) is ignored.
    // microUsbId: USB-ID (Chromium appends "(vid:pid)" to the label). Find yours with `lsusb`.
    // microNames: fallback by a UNIQUE device-name substring, because Chromium does NOT always
    //   include the vid:pid in the label. Must be UNIQUE (not generic like "usb"/"camera").
    // Each comma-separated, e.g. microUsbId: "<microscope-vid:pid>,<other-vid:pid>".
    microUsbId: "<microscope-vid:pid>",
    microNames: "<microscope-name-substring>",
    // Server-side microscope MJPEG stream (labcam-micro :7897 on the same host). Used ONLY when
    // getUserMedia is unavailable (another machine / plain-HTTP LAN-IP = no secure context).
    // Default = native pass-through at 15 fps (CPU ~0). For a narrow link, downscale:
    // "...:7897/stream?fps=12&w=1280&q=72" (re-encode, less bandwidth, some CPU).
    microStreamUrl: "http://" + h + ":7897/stream?fps=15",
  };
})();
