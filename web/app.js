/* ============================================================================
 *  Lab Cam Web — browser variant
 *
 *  Live microscope camera via getUserMedia + instrument overlay (STATE-WS) +
 *  component identification (ROI -> :7895/identify) + screenshot download.
 *  Client of the existing service; nothing server-side needed except the
 *  identification service (:7895) and optionally the STATE-WS (:7891).
 *
 *  Must be served via http://localhost/127.0.0.1 (secure context ->
 *  getUserMedia allowed). See serve.sh.
 * ========================================================================== */
(() => {
  "use strict";
  const CFG = window.LABCAM_WEB || {};
  const IDENTIFY = CFG.identifyUrl || "http://127.0.0.1:7895/identify";
  const STATE_WS = CFG.stateWsUrl  || "ws://127.0.0.1:7891";
  const MAX_EDGE = CFG.maxEdge || 1568;
  const SCOPE_URL = CFG.scopeUrl || "http://<scope-ip>/Instrument/novnc/vnc_auto.php";
  const THERMAL_URL = CFG.thermalUrl || "http://127.0.0.1:7896/stream";
  const THERMAL_BASE = THERMAL_URL.replace(/\/stream\b.*$/, "");   // .../7896 for /points
  const DMM_URL = CFG.dmmUrl || "/front_panel.html";   // same-origin, proxied by serve.py to the DMM
  const MICRO_STREAM = CFG.microStreamUrl || ("http://" + location.hostname + ":7897/stream?fps=15");
  const LS = window.localStorage;

  const $ = (id) => document.getElementById(id);
  const video = $("video"), micro = $("micro"), cv = $("cv"), ctx = cv.getContext("2d");
  const bar = $("bar"), roibox = $("roibox"), cardEl = $("card");
  const statusEl = $("status");
  const btnInstr = $("btnInstr"), btnComp = $("btnComp"), btnId = $("btnId"), btnClear = $("btnClear");
  const scopeFrame = $("scopeframe"), scopeWrap = $("scopewrap"), btnScope = $("btnScope");
  const irFrame = $("irframe"), irWrap = $("irwrap"), btnIr = $("btnIr"), idpanel = $("idpanel");
  const btnMarkers = $("btnMarkers"), btnGradient = $("btnGradient"), btnDmm = $("btnDmm");
  const dmmFrame = $("dmmframe"), dmmWrap = $("dmmwrap");
  const setStatus = (t) => { statusEl.textContent = t || ""; };

  // ---- State ----
  let instrOn = true, compOn = false, scopeOn = false, irOn = false, dmmOn = false;
  let camSrc = video;          // active camera source: <video> (getUserMedia) OR <img> (server MJPEG)
  let roi = null, silhouette = null, lastCard = null, roiNorm = null, lastDmm = null;
  let lastState = null;

  // =====================================================================
  //  DMM formatting (ported from the instrument STATE backend / LabCam overlay)
  // =====================================================================
  function fmtDmm(value, baseUnit, mode, range) {
    if (value === null || value === undefined || !isFinite(value) || Math.abs(value) > 1e30)
      return { sign: "", num: "", unit: baseUnit || "" };
    const abs = Math.abs(value); let scaled = value, unit = baseUnit || "", fixed = null;
    if (mode === "Diode") {
      let s = abs.toFixed(6); const d = s.indexOf("."); if (d < 2) s = "0".repeat(2 - d) + s;   // DMM7510 displays 6 decimal places
      return { sign: value < 0 ? "-" : "", num: s, unit: "V" };
    }
    const r = (range != null && isFinite(range)) ? Math.abs(range) : null;
    if (baseUnit === "V" || baseUnit === "A") {
      let p = r != null ? (r <= 1.05e-4 ? "µ" : r <= 1.05e-1 ? "m" : "")
                        : (abs > 0 && abs < 1.2e-4 ? "µ" : abs > 0 && abs < 1.2 ? "m" : "");
      if (p === "µ") { scaled = value * 1e6; unit = "µ" + baseUnit; }
      else if (p === "m") { scaled = value * 1e3; unit = "m" + baseUnit; }
    } else if (baseUnit === "Ω") {
      let p = r != null ? (r >= 1.05e6 ? "M" : r >= 1.05e3 ? "k" : "")
                        : (abs >= 1.2e6 ? "M" : abs >= 1.2e3 ? "k" : "");
      if (p === "M") { scaled = value / 1e6; unit = "MΩ"; }
      else if (p === "k") { scaled = value / 1e3; unit = "kΩ"; }
    } else if (baseUnit === "F") {
      // Capacitance: unit follows the device range (otherwise m/µ/n jump on value change); value-based fallback.
      let p = r != null ? (r >= 1.05e-4 ? "m" : r >= 1.05e-7 ? "µ" : "n")
                        : (abs >= 1.2e-4 ? "m" : abs >= 1.2e-7 ? "µ" : "n");
      if (p === "m") { scaled = value * 1e3; unit = "mF"; }
      else if (p === "µ") { scaled = value * 1e6; unit = "µF"; }
      else { scaled = value * 1e9; unit = "nF"; }
    } else if (baseUnit === "Hz") {
      if (abs >= 1.2e6) { scaled = value / 1e6; unit = "MHz"; } else if (abs >= 1.2e3) { scaled = value / 1e3; unit = "kHz"; } fixed = 3;
    } else if (baseUnit === "°C") { fixed = 3; }
    else if (baseUnit === "s") {   // Time: ns/µs/ms/s
      if (abs > 0 && abs < 1e-6) { scaled = value * 1e9; unit = "ns"; }
      else if (abs > 0 && abs < 1e-3) { scaled = value * 1e6; unit = "µs"; }
      else if (abs > 0 && abs < 1) { scaled = value * 1e3; unit = "ms"; }
      fixed = 3;
    } else if (mode === "Ratio") { fixed = 3; }
    let dec;
    if (fixed != null) dec = fixed; else {
      const a = Math.abs(scaled);
      dec = (a === 0 || !isFinite(a)) ? 7 : Math.max(0, 8 - Math.max(1, Math.floor(Math.log10(a)) + 1));
    }
    return { sign: scaled < 0 ? "-" : "", num: Math.abs(scaled).toFixed(dec), unit };
  }
  const fmt3 = (v) => (v == null || !isFinite(v)) ? "—" : v.toFixed(3);

  // LCR (DE-5000): value in base SI (F/H/Ω) -> engineering prefix (p/n/µ/m/k/M).
  // D/Q/θ are unitless resp. degrees. Returns {sign, num, unit} (fits valHtml()).
  function fmtLcr(value, unit) {
    if (value == null || !isFinite(value)) return { sign: "", num: "—", unit: unit || "" };
    if (unit === "" || unit === "%" || unit === "°") {
      const a0 = Math.abs(value);
      return { sign: "", num: value.toFixed(a0 >= 100 ? 1 : (a0 >= 10 ? 2 : 4)), unit: unit || "" };
    }
    const PFX = [[1e-12,"p"],[1e-9,"n"],[1e-6,"µ"],[1e-3,"m"],[1,""],[1e3,"k"],[1e6,"M"]];
    const abs = Math.abs(value); let ch = PFX[4];
    for (const pf of PFX) { const sc = abs / pf[0]; if (sc >= 1 && sc < 1000) { ch = pf; break; } }
    const num = value / ch[0], an = Math.abs(num);
    return { sign: "", num: num.toFixed(an >= 100 ? 2 : (an >= 10 ? 3 : 4)), unit: ch[1] + unit };
  }
  function lcrYMax(value) {
    if (value == null || !isFinite(value)) return 1;
    const abs = Math.abs(value); if (abs < 1e-15) return 1;
    return Math.pow(10, Math.ceil(Math.log(abs) / Math.LN10)) * 1.2;
  }

  // =====================================================================
  //  Scroll graphs (ported from ScrollGraph.qml / OverlayConfig.js)
  //  Ring buffer per series of {t,v}; x = w - (now - t)/mpp. Fixed scale
  //  (min/max) or auto (1.08 factor). 100 ms repaint like the phone app.
  // =====================================================================
  const GCOL = { v: "#00d3ff", a: "#e4b700" };   // line colors only (no background/border)
  const GLW = 2;
  const CHARTS = {                       // identical to OverlayConfig.CHARTS
    dmm:  { min: 0, max: 12, mpp: 165 }, // max is set live from range
    bb3a: { min: 0, max: 20, mpp: 119 },
    bb3b: { min: 0, max: 6,  mpp: 119 },
    lcr:  { min: 0, max: 1,  mpp: 119 },  // DE-5000 placeholder (no data yet)
    kel:  { min: 0, mpp: 1190 },         // lower bound 0 fixed, max auto (0 stays at bottom, not center)
  };
  const GRAPHS = {};                     // id -> graph object

  // DMM graph Y-max (range-based) + 32 s range-hold logic
  function dmmYMax(value, mode, range) {
    if (mode === "Diode") return 12;
    if (range != null && isFinite(range) && Math.abs(range) > 0) return Math.abs(range) * 1.2;
    if (value == null || !isFinite(value)) return 1.2;
    const abs = Math.abs(value); if (abs < 1e-12) return 1.2;
    return Math.pow(10, Math.ceil(Math.log10(abs))) * 1.2;
  }
  const RANGE_HOLD_MS = 32000;
  let _dmmRangeHist = {}, _dmmHistMode = null;
  function dmmEffectiveRange(currentRange, currentMode) {
    const now = Date.now();
    if (_dmmHistMode !== currentMode) { _dmmRangeHist = {}; _dmmHistMode = currentMode; }
    if (currentRange != null && isFinite(currentRange)) _dmmRangeHist["" + currentRange] = now;
    let maxR = null;
    for (const k in _dmmRangeHist) {
      if (now - _dmmRangeHist[k] > RANGE_HOLD_MS) { delete _dmmRangeHist[k]; continue; }
      const rv = parseFloat(k); if (maxR === null || rv > maxR) maxR = rv;
    }
    return maxR === null ? currentRange : maxR;
  }

  function makeGraph(canvas, opts) {
    return {
      canvas, ctx: canvas.getContext("2d"),
      colors: opts.colors, mpp: opts.mpp,
      minValue: opts.min, maxValue: opts.max,   // max undefined => Auto
      autoMax: !!opts.autoMax,                  // lower bound fixed (min), only max auto
      series: [],
      append(i, t, v) {
        while (this.series.length <= i) this.series.push([]);
        if (v == null || !isFinite(v)) return;
        this.series[i].push({ t, v });
      },
    };
  }
  function paintGraph(g) {
    // Sync canvas backing to CSS size (catches layout-timing/resize events).
    if (g.canvas.clientWidth && g.canvas.width !== g.canvas.clientWidth) {
      g.canvas.width = g.canvas.clientWidth; g.canvas.height = g.canvas.clientHeight;
    }
    const ctx = g.ctx, w = g.canvas.width, h = g.canvas.height;
    if (!w || !h) return;
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = "rgba(0,0,0,0.28)"; ctx.fillRect(0, 0, w, h);   // slightly darker fill, NO border
    const now = Date.now();
    // Prune points that have scrolled more than (w+50) px off the left edge
    const maxAge = (w + 50) * g.mpp;
    for (let s = 0; s < g.series.length; s++) {
      const arr = g.series[s]; let cut = 0;
      while (cut < arr.length - 1 && (now - arr[cut].t) > maxAge) cut++;
      if (cut > 0) g.series[s] = arr.slice(cut);
    }
    // Scale
    let lo = g.minValue || 0, hi = g.maxValue;
    if (g.autoMax) {
      // Lower bound fixed (lo), max derived from data -> 0 stays at bottom, not center.
      hi = -Infinity;
      for (const arr of g.series) for (const p of arr) if (p.v > hi) hi = p.v;
      if (!isFinite(hi) || hi <= lo) hi = lo + 1;
      hi = lo + (hi - lo) * 1.08;
    } else if (g.maxValue == null || isNaN(g.maxValue)) {
      // Full auto (symmetric around the data)
      lo = Infinity; hi = -Infinity;
      for (const arr of g.series) for (const p of arr) { if (p.v < lo) lo = p.v; if (p.v > hi) hi = p.v; }
      if (!isFinite(lo) || !isFinite(hi)) { lo = 0; hi = 1; }
      if (lo === hi) { lo -= 0.5; hi += 0.5; }
      const mid = (lo + hi) / 2, half = (hi - lo) / 2 * 1.08; lo = mid - half; hi = mid + half;
    }
    const span = (hi - lo) || 1;
    const yPix = (v) => h - ((v - lo) / span) * h;
    for (let s = 0; s < g.series.length; s++) {
      const arr = g.series[s]; if (arr.length < 1) continue;
      ctx.strokeStyle = g.colors[s] || "#fff"; ctx.lineWidth = GLW; ctx.lineJoin = "round";
      ctx.beginPath(); let started = false;
      for (const p of arr) {
        const x = w - (now - p.t) / g.mpp, y = yPix(p.v);
        if (!started) { ctx.moveTo(x, y); started = true; } else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
  }
  function sizeGraphs() {
    for (const id in GRAPHS) {
      const c = GRAPHS[id].canvas;
      if (c.clientWidth) { c.width = c.clientWidth; c.height = c.clientHeight; }
    }
    // Make the component-ID panel as tall as the instrument panels
    if (bar.offsetHeight) idpanel.style.minHeight = bar.offsetHeight + "px";
  }
  function paintGraphs() { if (instrOn) for (const id in GRAPHS) paintGraph(GRAPHS[id]); }

  // =====================================================================
  //  Instrument-Overlay (DOM)
  // =====================================================================
  const PANELS = [
    { id: "dmm",  label: "DMM7510" },
    { id: "bb3a", label: "BB3 Ch1 PSU" },
    { id: "bb3b", label: "USB" },
    { id: "lcr",  label: "DER EE DE-5000" },   // placeholder (DE-5000 LCR; connection pending)
    { id: "kel",  label: "KEL103" },
  ];
  function buildBar() {
    bar.innerHTML = "";
    for (const p of PANELS) {
      const el = document.createElement("div");
      el.className = "panel"; el.id = "p-" + p.id;
      // D+/D- (USB) sit LEFT of the V and A values (.rt group), graph below.
      el.innerHTML =
        '<div class="row"><span class="mode"></span><span class="rt"><span class="aux dp"></span><span class="val v"></span></span></div>' +
        '<div class="row r2"><span class="mode2"></span><span class="rt"><span class="aux dn"></span><span class="val a"></span></span></div>' +
        '<canvas class="g"></canvas>' +
        '<div class="lab">' + p.label + '</div>';
      bar.appendChild(el);
      if (p.id === "lcr") {            // Initial placeholder; renderState() fills it once STATE arrives.
        el.querySelector(".mode").textContent = "LCR";
        el.querySelector(".val.v").textContent = "—";
        el.querySelector(".r2").style.display = "none";
        el.classList.add("off");
        // Click on the name toggles the source CP2102 <-> RN4871 (BLE).
        const lab = el.querySelector(".lab");
        lab.style.cursor = "pointer";
        lab.title = "Toggle source: CP2102 ⇄ RN4871 (BLE)";
        lab.onclick = () => sendCmd({ cmd: "lcr_source", value: "toggle" });
      }
      const cfg = CHARTS[p.id];
      GRAPHS[p.id] = makeGraph(el.querySelector("canvas.g"), {
        colors: p.id === "dmm" ? [GCOL.v] : [GCOL.v, GCOL.a],
        mpp: cfg.mpp, min: cfg.min, max: cfg.max, autoMax: p.id === "kel",
      });
    }
    sizeGraphs();
  }
  function devOK(el, ok) { el.classList.toggle("off", !ok); }
  function valHtml(f) { return f.num ? `${f.sign}${f.num}<span class="u"> ${f.unit}</span>` : `<span class="u">${f.unit}</span>`; }
  function renderState(state) {
    lastState = state; const d = state.devices || {};
    if (d.dmm) {
      const s = d.dmm, el = $("p-dmm"); devOK(el, s.ok);
      el.querySelector(".mode").textContent = s.mode || "—";
      // Offline (ok:false) => "—". The BLE backends (USB/DE-5000) keep their last reading
      // in the payload even when ok:false — without this gate stale values would remain.
      el.querySelector(".val.v").innerHTML = s.ok ? valHtml(fmtDmm(s.value, s.unit || "", s.mode, s.range)) : valHtml({ sign: "", num: "—", unit: "" });
      // Diode + |value| < 0.05 V -> "Shorted" (red) in the second row
      const isShorted = s.ok && s.mode === "Diode" && s.value != null && isFinite(s.value) && Math.abs(s.value) < 0.05;
      const r2 = el.querySelector(".r2");
      r2.style.display = isShorted ? "" : "none";
      r2.querySelector(".mode2").textContent = "";
      r2.querySelector(".aux.dn").textContent = "";
      const av = r2.querySelector(".val.a");
      av.textContent = isShorted ? "Shorted" : "";
      av.classList.toggle("shorted", isShorted);
      lastDmm = s.ok ? s : null;
      const g = GRAPHS.dmm;
      if (g && s.ok && s.value != null && isFinite(s.value)) {
        g.append(0, Date.now(), s.value);
        g.minValue = 0; g.maxValue = dmmYMax(s.value, s.mode, dmmEffectiveRange(s.range, s.mode));
      }
    }
    for (const id of ["bb3a", "bb3b", "kel"]) {
      const s = d[id]; if (!s) continue; const el = $("p-" + id); devOK(el, s.ok);
      const off = s.output === false || s.output === "OFF";   // bb3a/bb3b: bool, kel: String
      el.querySelector(".r2").style.display = "";
      // KEL103: mode (CC/CV/CW) in place of "A" (2nd row), but empty when OFF;
      // 1st row for KEL is blank (otherwise mode would appear twice). USB: no "A".
      el.querySelector(".mode").textContent = id === "bb3a" ? "CV" : (id === "kel" ? "" : "V");
      el.querySelector(".mode2").textContent = id === "bb3a" ? "CC" : (id === "kel" ? (off ? "" : (s.mode || "")) : "");
      const offHtml = '<span class="offval">Off</span>';   // grey + semi-transparent (CSS)
      const dashHtml = valHtml({ sign: "", num: "—", unit: "" });
      // Offline (ok:false) => "—" (takes precedence over "Off": offline != "definitely off").
      el.querySelector(".val.v").innerHTML = !s.ok ? dashHtml : (off ? offHtml : valHtml({ sign: "", num: fmt3(s.voltage), unit: "V" }));
      el.querySelector(".val.a").innerHTML = !s.ok ? dashHtml : (off ? offHtml : valHtml({ sign: "", num: fmt3(s.current), unit: "A" }));
      if (id === "bb3b") {
        const ex = (s.ok && s.protocol && s.protocol !== "—") ? "  " + s.protocol : "";
        el.querySelector(".lab").textContent = "USB" + ex;
        el.querySelector(".mode").textContent = "V";
        const d2 = (v) => (v == null || !isFinite(v)) ? "—" : v.toFixed(2);
        // Label "D+/D-" yellow (.aux), values blue/cyan (.v)
        const showD = s.ok && (s.dp != null || s.dn != null);
        el.querySelector(".dp").innerHTML = showD ? `D+ <span class="v">${d2(s.dp)}<span class="u"> V</span></span>` : "";
        el.querySelector(".dn").innerHTML = showD ? `D− <span class="v">${d2(s.dn)}<span class="u"> V</span></span>` : "";
      }
      const g = GRAPHS[id];
      if (g) {
        if (!off && s.ok && isFinite(s.voltage)) g.append(0, Date.now(), s.voltage);
        if (!off && s.ok && isFinite(s.current)) g.append(1, Date.now(), s.current);
      }
    }
    if (d.lcr) {
      const s = d.lcr, el = $("p-lcr"); devOK(el, s.ok);
      el.querySelector(".mode").textContent = s.mode || "LCR";
      // Offline (ok:false) => "—". The DE-5000 BLE backend keeps its last value in the
      // payload (status searching/offline); without the ok gate it would stay on screen.
      el.querySelector(".val.v").innerHTML = s.ok ? valHtml(fmtLcr(s.value, s.unit || "")) : valHtml({ sign: "", num: "—", unit: "" });
      // Secondary value (D / Q / ESR / θ) on line 2 — otherwise off.
      const r2 = el.querySelector(".r2");
      if (s.ok && s.sec_mode) {
        r2.style.display = "";
        r2.querySelector(".mode2").textContent = s.sec_mode;
        r2.querySelector(".aux.dn").textContent = "";
        const av = r2.querySelector(".val.a");
        av.classList.remove("shorted");
        av.innerHTML = valHtml(fmtLcr(s.sec_value, s.sec_unit || ""));
      } else {
        r2.style.display = "none";
      }
      // Source/status in the label: USB / BLE / BLE… (searching) / — (offline) [+ test frequency].
      const src = s.source === "rn4871" ? "BLE" : "USB";
      let ex;
      if (s.status === "searching")    ex = "BLE…";
      else if (s.status === "offline") ex = src + " —";
      else if (s.freq)                 ex = src + " · " + (s.freq >= 1000 ? (s.freq / 1000) + "k" : s.freq) + "Hz";
      else                             ex = src;
      el.querySelector(".lab").textContent = "DER EE DE-5000  " + ex;
      const g = GRAPHS.lcr;
      if (g && s.ok && s.value != null && isFinite(s.value)) {
        g.append(0, Date.now(), s.value);
        g.minValue = 0; g.maxValue = lcrYMax(s.value);
      }
    }
  }
  let stateWs = null;
  function sendCmd(obj) {                        // back-channel to the backend (e.g. toggle the LCR source)
    if (stateWs && stateWs.readyState === WebSocket.OPEN) { stateWs.send(JSON.stringify(obj)); return true; }
    return false;
  }
  function stateConnect() {
    // The STATE backend (:7891) runs as a persistent service -> simply (re)connect.
    let ws; try { ws = new WebSocket(STATE_WS); } catch (e) { setTimeout(stateConnect, 2000); return; }
    stateWs = ws;
    ws.onmessage = (ev) => { try { renderState(JSON.parse(ev.data)); } catch (e) {} };
    ws.onclose = () => { setTimeout(stateConnect, 2000); };
    ws.onerror = () => { try { ws.close(); } catch (e) {} };
  }

  // =====================================================================
  //  Camera (getUserMedia)
  // =====================================================================
  let curStream = null;
  // STRICT whitelist: only the allowed microscope camera, otherwise none (never the IR camera,
  // an action cam, a webcam, ... and never "first camera"). Matched by USB-ID (Chromium appends
  // "(vid:pid)" to the label) OR by a unique device-name substring — because Chromium does NOT
  // always include the vid:pid in the label. Both lists are comma-separated in config.js.
  function micAllow() {
    const split = s => (s || "").toLowerCase().split(",").map(x => x.trim()).filter(Boolean);
    return { ids: split(CFG.microUsbId), names: split(CFG.microNames) };
  }
  function pickMicro(devs) {
    const { ids, names } = micAllow();
    const hit = devs.find(d => {
      if (d.kind !== "videoinput") return false;
      const lbl = (d.label || "").toLowerCase();
      return ids.some(id => lbl.includes(id)) || names.some(n => lbl.includes(n));
    });
    return hit ? hit.deviceId : null;
  }
  const enumCams = async () => {
    try { return await navigator.mediaDevices.enumerateDevices(); } catch (e) { return []; }
  };
  // getUserMedia is only available in a secure context (localhost/HTTPS). From another machine
  // over the LAN-IP (plain HTTP) it is blocked -> then use the server-side MJPEG stream.
  function canUseGetUserMedia() {
    return !!(window.isSecureContext && navigator.mediaDevices && navigator.mediaDevices.getUserMedia);
  }
  function startServerStream() {
    // Microscope image as an <img> from the labcam-micro service. crossOrigin keeps the canvas
    // readable (service sends CORS *) so component identification works over the stream too.
    camSrc = micro;
    if (curStream) { curStream.getTracks().forEach(t => t.stop()); curStream = null; }
    video.style.display = "none"; micro.style.display = "block";
    micro.crossOrigin = "anonymous";
    micro.onload = () => { if (compOn && !roi) { roi = defaultRoi(); positionBox(); } draw(); };
    micro.onerror = () => setStatus("Microscope stream unreachable");
    micro.src = MICRO_STREAM;
    setStatus("");
  }
  function useLocalCam() {   // <video>/getUserMedia as the active source (local, full 1080p)
    camSrc = video; micro.removeAttribute("src"); micro.style.display = "none"; video.style.display = "block";
  }
  async function initCamera() {
    // Another machine / no secure context -> server stream directly (getUserMedia wouldn't work).
    if (!canUseGetUserMedia()) { startServerStream(); return; }
    useLocalCam();
    // 1) Enumerate WITHOUT probing. On reload permission is already granted -> labels are
    //    available -> open the microscope directly by whitelist (no double-open, never a foreign cam).
    let devs = await enumCams();
    if (devs.some(d => d.kind === "videoinput" && d.label)) {
      const pick = pickMicro(devs);
      if (pick) await startCam(pick);
      else startServerStream();        // not found locally -> server stream (finds the cam by USB-ID)
      return;
    }
    // 2) First visit (no permission yet -> no labels): probe once so Chromium exposes the labels
    //    (incl. vid:pid), then pick the microscope deliberately by whitelist.
    let probe = null;
    try { probe = await navigator.mediaDevices.getUserMedia({ video: true }); }
    catch (e) { setStatus("Camera: " + e.name); }
    devs = await enumCams();
    const pick = pickMicro(devs);
    const pt = probe && probe.getVideoTracks ? probe.getVideoTracks()[0] : null;
    const pid = pt && pt.getSettings ? pt.getSettings().deviceId : null;
    if (probe && pick && pid && pid === pick) {     // probe already got the microscope -> reuse it
      curStream = probe; video.srcObject = probe; LS.setItem("cam_id", pick); setStatus(""); return;
    }
    if (probe) probe.getTracks().forEach(t => t.stop());   // probe was a foreign camera -> stop immediately
    if (pick) await startCam(pick);
    else startServerStream();          // not found locally -> server stream as fallback
  }
  // Resolution fallback steps: when USB bandwidth is shared with the HT-301, 1080p
  // sometimes yields NO frames (black) -> fall back to lower bandwidth automatically.
  const CAM_RES = [
    { width: { ideal: 1920 }, height: { ideal: 1080 } },
    { width: { ideal: 1280 }, height: { ideal: 720 } },
    {},   // no constraint (camera default)
  ];
  function framesFlowing(ms) {   // resolves true once the video is delivering real frames
    return new Promise((resolve) => {
      let done = false;
      const finish = (v) => { if (!done) { done = true; resolve(v); } };
      const t = setTimeout(() => finish(video.videoWidth > 0 && video.currentTime > 0), ms);
      const chk = () => {
        if (done) return;
        if (video.videoWidth > 0 && video.currentTime > 0) { clearTimeout(t); finish(true); }
        else requestAnimationFrame(chk);
      };
      requestAnimationFrame(chk);
    });
  }
  async function startCam(id, attempt) {
    attempt = attempt || 0;
    if (curStream) curStream.getTracks().forEach(t => t.stop());
    const vc = Object.assign({}, CAM_RES[Math.min(attempt, CAM_RES.length - 1)]);
    if (id) vc.deviceId = { exact: id };
    try {
      curStream = await navigator.mediaDevices.getUserMedia({ video: vc });
      video.srcObject = curStream; LS.setItem("cam_id", id || "");
      setStatus("");
      // Verify frames are actually flowing; if not (USB bandwidth), retry at lower resolution.
      const ok = await framesFlowing(1800);
      if (!ok && attempt < CAM_RES.length - 1) {
        setStatus("Camera retry …"); return startCam(id, attempt + 1);
      }
      setStatus("");
    } catch (e) {
      if (attempt < CAM_RES.length - 1) { return startCam(id, attempt + 1); }
      setStatus("Camera start: " + e.name);
    }
  }

  // =====================================================================
  //  Image area (contain) in the viewport — for ROI/overlay mapping
  // =====================================================================
  // Dimensions of the active camera source (video -> videoWidth, img -> naturalWidth).
  function srcW() { return camSrc.videoWidth || camSrc.naturalWidth || 0; }
  function srcH() { return camSrc.videoHeight || camSrc.naturalHeight || 0; }
  function imgRect() {
    const Ew = window.innerWidth, Eh = window.innerHeight;
    const vw = srcW() || 16, vh = srcH() || 9;
    const a = vw / vh, ea = Ew / Eh; let w, h;
    if (ea > a) { h = Eh; w = Eh * a; } else { w = Ew; h = Ew / a; }
    return { x: (Ew - w) / 2, y: (Eh - h) / 2, w, h };
  }

  // =====================================================================
  //  Selection frame (corner handles) + silhouette drawing
  // =====================================================================
  function sizeCanvas() { cv.width = window.innerWidth; cv.height = window.innerHeight; draw(); }
  window.addEventListener("resize", () => { sizeCanvas(); positionBox(); sizeGraphs(); });

  function defaultRoi() {
    const ir = imgRect(); const s = Math.round(Math.min(ir.w, ir.h) * 0.3);
    return { x: Math.round(ir.x + (ir.w - s) / 2), y: Math.round(ir.y + (ir.h - s) / 2), w: s, h: s };
  }
  function positionBox() {
    if (!roi) return;
    roibox.style.left = roi.x + "px"; roibox.style.top = roi.y + "px";
    roibox.style.width = roi.w + "px"; roibox.style.height = roi.h + "px";
  }
  function cornersPath(c, x, y, w, h, len) {
    c.beginPath();
    c.moveTo(x, y + len); c.lineTo(x, y); c.lineTo(x + len, y);
    c.moveTo(x + w - len, y); c.lineTo(x + w, y); c.lineTo(x + w, y + len);
    c.moveTo(x + w, y + h - len); c.lineTo(x + w, y + h); c.lineTo(x + w - len, y + h);
    c.moveTo(x + len, y + h); c.lineTo(x, y + h); c.lineTo(x, y + h - len);
  }
  function draw() {
    ctx.clearRect(0, 0, cv.width, cv.height);
    if (silhouette && roi && silhouette.length >= 3 && compOn) {
      let a = 1e9, b = 1e9, mx = -1e9, my = -1e9;
      for (const p of silhouette) {
        const X = roi.x + p[0] * roi.w, Y = roi.y + p[1] * roi.h;
        if (X < a) a = X; if (X > mx) mx = X; if (Y < b) b = Y; if (Y > my) my = Y;
      }
      const w = mx - a, h = my - b;
      if (w > 0 && h > 0) {
        // Blue/yellow like the instrument panels: two equal-width lines SIDE BY SIDE
        // (yellow outer, blue inner) not stacked. The inner line is inset by one
        // line-width (t) AND shortened at the open ends by t (arm len-2t),
        // otherwise the end faces meet and blue bleeds into yellow. This leaves the
        // yellow tip alone -> cleanly nested corner. Yellow outside (dark background),
        // blue inside (bright component) -> contrast on any PCB color.
        ctx.lineJoin = "miter"; ctx.lineCap = "butt";
        const t = 4, len = Math.max(12, Math.min(w, h) * 0.22);
        ctx.lineWidth = t;
        ctx.strokeStyle = "#e4b700"; cornersPath(ctx, a, b, w, h, len); ctx.stroke();
        ctx.strokeStyle = "#00d3ff"; cornersPath(ctx, a + t, b + t, w - 2 * t, h - 2 * t, len - 2 * t); ctx.stroke();
      }
    }
  }

  // Draw / resize the frame
  let drag = null;
  const clampRoi = () => {
    const ir = imgRect();
    roi.w = Math.max(30, Math.min(roi.w, ir.w)); roi.h = Math.max(30, Math.min(roi.h, ir.h));
    roi.x = Math.max(ir.x, Math.min(roi.x, ir.x + ir.w - roi.w));
    roi.y = Math.max(ir.y, Math.min(roi.y, ir.y + ir.h - roi.h));
  };
  roibox.addEventListener("mousedown", (e) => {
    if (!compOn) return; e.preventDefault();
    const corner = e.target && e.target.dataset ? e.target.dataset.corner : null;
    drag = corner ? { type: "resize", corner } : { type: "move", offx: e.clientX - roi.x, offy: e.clientY - roi.y };
  });
  window.addEventListener("mousemove", (e) => {
    if (!drag || !roi) return;
    if (drag.type === "move") { roi.x = e.clientX - drag.offx; roi.y = e.clientY - drag.offy; }
    else {
      let x1 = roi.x, y1 = roi.y, x2 = roi.x + roi.w, y2 = roi.y + roi.h, c = drag.corner;
      if (c === "tl") { x1 = e.clientX; y1 = e.clientY; } else if (c === "tr") { x2 = e.clientX; y1 = e.clientY; }
      else if (c === "br") { x2 = e.clientX; y2 = e.clientY; } else if (c === "bl") { x1 = e.clientX; y2 = e.clientY; }
      roi = { x: Math.min(x1, x2), y: Math.min(y1, y2), w: Math.abs(x2 - x1), h: Math.abs(y2 - y1) };
    }
    clampRoi(); positionBox();
  });
  window.addEventListener("mouseup", () => { drag = null; });

  // =====================================================================
  //  ROI → Frame-Crop → /identify
  // =====================================================================
  function roiToNorm() {
    const ir = imgRect();
    return {
      x: (roi.x - ir.x) / ir.w, y: (roi.y - ir.y) / ir.h,
      w: roi.w / ir.w, h: roi.h / ir.h,
    };
  }
  function cropBase64(nrm) {
    const W = srcW(), H = srcH();
    let cw = Math.max(1, Math.round(nrm.w * W)), ch = Math.max(1, Math.round(nrm.h * H));
    const cx = Math.max(0, Math.round(nrm.x * W)), cy = Math.max(0, Math.round(nrm.y * H));
    let ow = cw, oh = ch; const m = Math.max(cw, ch);
    if (m > MAX_EDGE) { const f = MAX_EDGE / m; ow = Math.round(cw * f); oh = Math.round(ch * f); }
    const c = document.createElement("canvas"); c.width = ow; c.height = oh;
    c.getContext("2d").drawImage(camSrc, cx, cy, cw, ch, 0, 0, ow, oh);
    return c.toDataURL("image/jpeg", 0.9).split(",")[1];
  }
  async function identify() {
    if (!compOn || !roi || !srcW()) return;
    setStatus("Identifying …"); btnId.disabled = true;
    roiNorm = roiToNorm();
    let b64; try { b64 = cropBase64(roiNorm); } catch (e) { setStatus("Crop: " + e.message); return; }
    try {
      const resp = await fetch(IDENTIFY, { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ image_b64: b64, dmm: lastDmm }) });
      const card = await resp.json();
      lastCard = card; silhouette = Array.isArray(card.silhouette) ? card.silhouette : null;
      renderCard(card); draw(); setStatus(""); btnId.disabled = false;
    } catch (e) { setStatus("Identify error: " + e.message); btnId.disabled = false; }
  }

  // =====================================================================
  //  Ergebnis-Karte (DOM)
  // =====================================================================
  const txt = (v) => (v == null || v === "") ? "—" : "" + v;
  const pct = (v) => (typeof v === "number") ? Math.round(v * 100) + " %" : "—";
  const line = (cls, t) => { const d = document.createElement("div"); d.className = cls; d.textContent = t; return d; };
  function renderCard(card) {
    cardEl.innerHTML = "";
    cardEl.appendChild(line("l1", txt(card.klasse) + (card.bezeichnung ? "  ·  " + card.bezeichnung : (card.wert ? "  ·  " + card.wert : ""))));
    cardEl.appendChild(line("l2", (card.hersteller || "Manufacturer —") + (card.wert ? "  ·  " + card.wert + (card.toleranz ? " " + card.toleranz : "") : "") + "  ·  " + pct(card.konfidenz)));
    if (card.erwartung_ohm || card.erwartung_diode)
      cardEl.appendChild(line("l3", (card.erwartung_ohm ? "Ohm " + card.erwartung_ohm : "") + (card.erwartung_diode ? (card.erwartung_ohm ? "   " : "") + "Diode " + card.erwartung_diode : "")));
    if (card.hinweis) cardEl.appendChild(line("l4", card.hinweis));
    if (card.datenblatt_url) {
      const a = document.createElement("a"); a.className = "ds"; a.href = card.datenblatt_url;
      a.target = "_blank"; a.rel = "noopener"; a.textContent = "Datasheet: " + card.datenblatt_url;
      cardEl.appendChild(a);
    }
    cardEl.classList.remove("hidden");
  }

  // Screenshots are taken by the user via the Linux system (Shift+Print) —
  // overlays are real DOM and are captured as-is. No dedicated
  // screenshot button/code needed.

  // =====================================================================
  //  Wiring
  // =====================================================================
  function setComp(on) {
    compOn = on; btnComp.classList.toggle("on", on);
    roibox.classList.toggle("show", on);
    btnId.disabled = !on;
    if (on && !roi) { roi = defaultRoi(); positionBox(); }
    draw();
  }
  // =====================================================================
  //  PiP slots: IR-Cam / Scope / DMM7510 share 2 stacked slots at bottom-right.
  //  Rule: IR (when on) = top slot. Scope+DMM share the BOTTOM slot; when IR is OFF,
  //  the SECOND enabled device moves UP (to IR's slot) -> both visible.
  //  Stack (bottom->top): IR on -> [most-recent Scope/DMM, IR];  IR off -> [first, second].
  //  Lazy: set src only when shown, clear it when hidden (VNC/VFP session).
  // =====================================================================
  let secOrder = [];                               // "scope"/"dmm" in activation order (oldest first)
  const _shown = { ir: false, scope: false, dmm: false };
  const _wrap  = { ir: irWrap, scope: scopeWrap, dmm: dmmWrap };
  const _frame = { ir: irFrame, scope: scopeFrame, dmm: dmmFrame };
  const _src   = { ir: () => THERMAL_URL, scope: () => SCOPE_URL, dmm: () => DMM_URL };
  const _empty = { ir: "", scope: "about:blank", dmm: "about:blank" };
  function _setSec(d, on) { secOrder = secOrder.filter(x => x !== d); if (on) secOrder.push(d); }
  function layoutSlots() {
    // Determine stack from bottom to top
    const stack = irOn
      ? (secOrder.length ? [secOrder[secOrder.length - 1]] : []).concat(["ir"])  // IR on top, most-recent secondary below
      : secOrder.slice(0, 2);                                                     // first at bottom, second on top
    const vis = new Set(stack);
    for (const d of ["ir", "scope", "dmm"]) {      // hide non-visible + release session
      if (!vis.has(d)) {
        _wrap[d].classList.remove("show");
        if (_shown[d]) { _frame[d].src = _empty[d]; _shown[d] = false; }
      }
    }
    for (const d of stack) {                        // load and show visible slots
      if (!_shown[d]) { _frame[d].src = _src[d](); _shown[d] = true; }
      _wrap[d].classList.add("show");
    }
    let y = 2;                                      // stack from bottom edge up (6 px gap)
    for (const d of stack) { _wrap[d].style.bottom = y + "px"; y += _wrap[d].offsetHeight + 6; }
  }
  function setIr(on)    { irOn = on;    btnIr.classList.toggle("on", on);    layoutSlots(); }
  function setScope(on) { scopeOn = on; btnScope.classList.toggle("on", on); _setSec("scope", on); layoutSlots(); }
  function setDmm(on)   { dmmOn = on;   btnDmm.classList.toggle("on", on);   _setSec("dmm", on);   layoutSlots(); }
  btnIr.addEventListener("click", () => setIr(!irOn));
  btnScope.addEventListener("click", () => setScope(!scopeOn));
  btnDmm.addEventListener("click", () => setDmm(!dmmOn));
  // IR markers (Min/Max + measurement points) on/off. Default OFF -> service stays in robust YUYV mode.
  let markersOn = false;
  btnMarkers.addEventListener("click", () => {
    markersOn = !markersOn; btnMarkers.classList.toggle("on", markersOn);
    fetch(`${THERMAL_BASE}/points?markers=${markersOn ? 1 : 0}`).catch(() => {});
  });
  // IR color gradient/colorbar on/off. Rendered by the service as an overlay on the right
  // edge of the image (image geometry does NOT change, so no nobar/resize needed).
  let barOn = false;
  btnGradient.addEventListener("click", () => {
    barOn = !barOn; btnGradient.classList.toggle("on", barOn);
    fetch(`${THERMAL_BASE}/points?bar=${barOn ? 1 : 0}`).catch(() => {});
  });
  // (Calibrate button removed; FFC/emissivity endpoints in the service remain dormant for later.)
  // (setDmm + button wiring: see PiP slot logic above)

  // ---- IR measurement points: drag = move · left-double-click (empty spot) = add
  //      · right-double-click (on point) = delete. Points are rendered by the service (with deg C). ----
  const irImgFrac = () => 1;   // image always fills full width (colorbar is overlaid on top)
  let irPoints = [];                      // mirrored point list from the service
  let _dragIdx = -1, _lb = -1, _lt = 0, _lastMove = 0;
  const irCoords = (e) => {
    const r = irFrame.getBoundingClientRect();
    const fx = (e.clientX - r.left) / r.width, fy = (e.clientY - r.top) / r.height;
    const f = irImgFrac();
    if (fx < 0 || fx > f || fy < 0 || fy > 1) return null;   // outside image / on colorbar
    return { nx: fx / f, ny: fy };
  };
  const nearestIdx = (c) => {
    let best = -1, bd = 0.005;            // click tolerance (~7% of image)
    for (let i = 0; i < irPoints.length; i++) {
      const dx = irPoints[i][0] - c.nx, dy = irPoints[i][1] - c.ny, d = dx * dx + dy * dy;
      if (d < bd) { bd = d; best = i; }
    }
    return best;
  };
  const irFetch = (qs) => fetch(`${THERMAL_BASE}/points?${qs}`).then(r => r.json()).then(a => { irPoints = a; }).catch(() => {});
  irWrap.addEventListener("contextmenu", (e) => e.preventDefault());
  irWrap.addEventListener("mousedown", (e) => {
    if (!irOn) return;
    const c = irCoords(e); if (!c) return;
    e.preventDefault();
    const near = nearestIdx(c), now = performance.now();
    const dbl = (e.button === _lb && now - _lt < 350);
    _lb = e.button; _lt = now;
    if (e.button === 0) {
      if (near >= 0) _dragIdx = near;                                   // grab point -> drag
      else if (dbl) { irFetch(`set=${c.nx.toFixed(4)},${c.ny.toFixed(4)}`); _lb = -1; }   // empty + double-click = add
    } else if (e.button === 2 && dbl && near >= 0) {
      irFetch(`del=${c.nx.toFixed(4)},${c.ny.toFixed(4)}`); _lb = -1;   // right-double-click on point = delete
    }
  });
  window.addEventListener("mousemove", (e) => {
    if (_dragIdx < 0 || !(e.buttons & 1)) return;
    const c = irCoords(e); if (!c) return;
    irPoints[_dragIdx] = [c.nx, c.ny];                                  // update locally immediately -> smooth
    const now = performance.now();
    if (now - _lastMove > 60) { _lastMove = now; irFetch(`move=${_dragIdx},${c.nx.toFixed(4)},${c.ny.toFixed(4)}`); }
  });
  window.addEventListener("mouseup", () => { _dragIdx = -1; });
  setInterval(() => { if (irOn) fetch(`${THERMAL_BASE}/points`).then(r => r.json()).then(a => { if (_dragIdx < 0) irPoints = a; }).catch(() => {}); }, 700);

  btnInstr.addEventListener("click", () => { instrOn = !instrOn; bar.classList.toggle("off", !instrOn); btnInstr.classList.toggle("on", instrOn); });
  btnComp.addEventListener("click", () => setComp(!compOn));
  btnId.addEventListener("click", identify);
  btnClear.addEventListener("click", () => { lastCard = null; silhouette = null; cardEl.classList.add("hidden"); draw(); });
  video.addEventListener("loadedmetadata", () => { if (compOn && !roi) { roi = defaultRoi(); positionBox(); } draw(); });

  buildBar();
  sizeCanvas();
  initCamera();
  stateConnect();
  setInterval(paintGraphs, 100);   // 10 fps scroll repaint (like ScrollGraph.qml)
  setTimeout(sizeGraphs, 300);     // after layout: sync canvas sizes + panel height
})();
