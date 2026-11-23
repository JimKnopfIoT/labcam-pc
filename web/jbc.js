/* ============================================================================
 *  JBC DME-2A Monitor (web UI)
 *
 *  Reads the instrument STATE WebSocket (labcam-instruments-backend, key "jbc")
 *  and shows the four tool ports. The history is kept here in the browser.
 *  Double-click on the LED disconnects/connects the station in the backend
 *  (cmd "device_connection", like the other devices).
 *  ?embed=1 = compact view for the labcam overlay (iframe).
 *  Temperature levels (only when the backend reports "writable"):
 *    double-click on a level                -> level active
 *    Ctrl+Shift+mouse wheel on active level -> temperature in 5 °C steps,
 *    sent 0.7 s after the last wheel notch (cmd "jbc_level").
 * ========================================================================== */
(() => {
  "use strict";
  const CFG = window.LABCAM_WEB || {};
  const params = new URLSearchParams(location.search);
  const STATE_WS = params.get("ws") || CFG.stateWsUrl || ("ws://" + (location.hostname || "127.0.0.1") + ":7891");
  const HIST_S = 180, STEP_MS = 500, HIST_N = HIST_S * 1000 / STEP_MS;
  if (params.get("embed") === "1") document.body.classList.add("embed");
  if (params.get("demo") === "1") document.body.dataset.demo = "1";

  const $ = (id) => document.getElementById(id);
  const css = getComputedStyle(document.documentElement);
  const C = (n) => css.getPropertyValue("--" + n).trim();
  const COL = { cyan: C("cyan"), teal: C("teal"), green: C("green"), amber: C("amber"),
                orange: C("orange"), red: C("red"), violet: C("violet"), low: C("low"),
                text: C("text"), grid: C("grid") };
  const LABEL = { work: "WORKING", stand: "STAND", sleep: "SLEEP", hiber: "HIBERNATION",
                  error: "ERROR", none: "NO TOOL" };

  function tempColor(t, set) {
    if (t == null || isNaN(t)) return COL.low;
    if (set && t > set + 25) return COL.red;
    if (t < 60) return COL.cyan;
    if (t < 150) return COL.teal;
    if (t < 250) return COL.amber;
    return COL.orange;
  }
  function rgba(hex, a) {
    const m = /^#?([0-9a-f]{2})([0-9a-f]{2})([0-9a-f]{2})/i.exec(hex);
    return m ? `rgba(${parseInt(m[1], 16)},${parseInt(m[2], 16)},${parseInt(m[3], 16)},${a})` : hex;
  }

  // ---- Build tiles ----
  const grid = $("grid");
  const tiles = [];
  for (let i = 0; i < 4; i++) {
    const el = document.createElement("section");
    el.className = "tile none";
    el.innerHTML = `
      <div class="t-head"><span class="bar"></span><span class="pno">P${i + 1}</span>
        <span class="tool">—</span><span class="spacer"></span><span class="pill dim state">NO TOOL</span></div>
      <div class="t-temp"><span class="big">– –</span>
        <span class="unit"><b>°C</b><span class="set">Set –</span></span><span class="dev"></span></div>
      <div class="t-row"><span>Levels</span><span class="lvl"></span><span class="spacer"></span>
        <span>Power</span><span class="pw">–</span></div>
      <div class="t-bar"><i></i></div>
      <canvas class="t-spark"></canvas>
      <div class="t-off">no connection</div>`;
    grid.appendChild(el);
    tiles.push({
      el, hist: [], histSet: [], last: null,
      tool: el.querySelector(".tool"), state: el.querySelector(".state"),
      big: el.querySelector(".big"), set: el.querySelector(".set"), dev: el.querySelector(".dev"),
      lvl: el.querySelector(".lvl"), pw: el.querySelector(".pw"), pwBar: el.querySelector(".t-bar i"),
      cv: el.querySelector(".t-spark"),
    });
  }

  // ---- Draw history ----
  function drawSpark(t) {
    const cv = t.cv, dpr = window.devicePixelRatio || 1;
    const w = cv.clientWidth, h = cv.clientHeight;
    if (!w || !h) return;
    if (cv.width !== Math.round(w * dpr) || cv.height !== Math.round(h * dpr)) {
      cv.width = Math.round(w * dpr); cv.height = Math.round(h * dpr);
    }
    const ctx = cv.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const sp = parseFloat(getComputedStyle(document.body).getPropertyValue("--sp")) || 14;
    const lab = Math.max(9, sp * 0.7), x0 = lab * 3.2, pw = w - x0, ph = h - 2;
    const vmin = 0, vmax = 500;
    const y = (v) => 1 + ph - (Math.max(vmin, Math.min(vmax, v)) - vmin) / (vmax - vmin) * ph;
    const x = (i, n) => x0 + pw - (n - 1 - i) * pw / (HIST_N - 1);
    const step = ph < lab * 7 ? 200 : 100;
    ctx.font = `300 ${lab}px "Fira Sans", sans-serif`;
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (let g = step; g < vmax; g += step) {
      ctx.strokeStyle = COL.grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(x0, y(g)); ctx.lineTo(w, y(g)); ctx.stroke();
      ctx.fillStyle = COL.low; ctx.fillText(g + "°", x0 - lab * 0.5, y(g));
    }
    const n = t.hist.length;
    if (n < 2) return;
    const lc = tempColor(t.last && t.last.temp, t.last && t.last.set);
    // set point dashed
    ctx.strokeStyle = rgba(COL.text, 0.35); ctx.setLineDash([4, 4]); ctx.beginPath();
    let pen = false;
    t.histSet.forEach((s, i) => {
      if (s == null) { pen = false; return; }
      const xs = x(i, t.histSet.length);
      if (!pen) { ctx.moveTo(xs, y(s)); pen = true; } else ctx.lineTo(xs, y(s));
    });
    ctx.stroke(); ctx.setLineDash([]);
    // area
    const gr = ctx.createLinearGradient(0, 0, 0, h);
    gr.addColorStop(0, rgba(lc, 0.28)); gr.addColorStop(1, rgba(lc, 0));
    ctx.fillStyle = gr; ctx.beginPath();
    let first = -1, last = -1;
    t.hist.forEach((v, i) => {
      if (v == null) return;
      if (first < 0) { first = i; ctx.moveTo(x(i, n), h); }
      ctx.lineTo(x(i, n), y(v)); last = i;
    });
    if (first >= 0) { ctx.lineTo(x(last, n), h); ctx.closePath(); ctx.fill(); }
    // line
    ctx.strokeStyle = lc; ctx.lineWidth = Math.max(1.5, sp * 0.12); ctx.lineJoin = "round"; ctx.beginPath();
    pen = false;
    t.hist.forEach((v, i) => {
      if (v == null) { pen = false; return; }
      if (!pen) { ctx.moveTo(x(i, n), y(v)); pen = true; } else ctx.lineTo(x(i, n), y(v));
    });
    ctx.stroke();
    if (last >= 0) {
      ctx.fillStyle = lc; ctx.beginPath();
      ctx.arc(x(last, n), y(t.hist[last]), Math.max(2.5, sp * 0.2), 0, 2 * Math.PI); ctx.fill();
    }
  }

  // ---- Rendering ----
  let jbc = null, wsOpen = false, lastNotice = null, demoCmd = null;
  const canWrite = () => !!(wsOpen && jbc && jbc.ok && jbc.writable);
  function sendLevel(obj) {
    obj.cmd = "jbc_level";
    if (demoCmd) demoCmd(obj);
    else if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }
  function clearPend(t) {
    if (!t.pend) return;
    clearTimeout(t.pend.commit); clearTimeout(t.pend.giveUp);
    t.pend = null;
  }
  let toastTimer = null;
  function toast(ok, text) {
    const el = $("toast");
    if (!el) return;
    el.textContent = text;
    el.className = "show" + (ok ? "" : " err");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { el.className = ""; }, ok ? 2500 : 5000);
  }

  // ---- Level controls (event delegation; the pills are rebuilt on every render) ----
  tiles.forEach((t, i) => {
    t.lastStep = 0;
    t.lvl.addEventListener("dblclick", (e) => {
      const pill = e.target.closest(".pill[data-k]");
      const d = t.last;
      if (!pill || !d || !canWrite()) return;
      const k = +pill.dataset.k;
      if (k === d.level || !d.levels || d.levels[k] == null) return;
      e.preventDefault();
      sendLevel({ port: i, level: k });
    });
    t.lvl.addEventListener("wheel", (e) => {
      if (!e.ctrlKey || !e.shiftKey) return;
      const pill = e.target.closest(".pill[data-k]");
      const d = t.last;
      if (!pill || !d || !canWrite()) return;
      const k = +pill.dataset.k;
      if (k !== d.level || !d.levels || d.levels[k] == null) return;
      e.preventDefault();                       // no browser zoom
      const dl = e.deltaY || e.deltaX;
      const now = performance.now();
      if (!dl || now - t.lastStep < 60) return; // one notch = one step
      t.lastStep = now;
      const lo = jbc.min_temp || 90, hi = jbc.max_temp || 450;
      const base = t.pend && t.pend.k === k ? t.pend.v : Math.round(d.levels[k]);
      const v = Math.max(lo, Math.min(hi, Math.round((base + (dl < 0 ? 5 : -5)) / 5) * 5));
      if (t.pend && t.pend.k !== k) clearPend(t);
      if (!t.pend) t.pend = { k, v };
      t.pend.v = v;
      clearTimeout(t.pend.commit); clearTimeout(t.pend.giveUp);
      t.pend.commit = setTimeout(() => {
        sendLevel({ port: i, level: k, temp: t.pend.v });
        t.pend.giveUp = setTimeout(() => { clearPend(t); render(); }, 4000);
      }, 700);
      render();
    }, { passive: false });
  });

  function render() {
    const ok = !!(wsOpen && jbc && jbc.ok);
    const sep = !!(jbc && jbc.disconnected);
    document.body.classList.toggle("offline", !ok);
    const led = $("led"), cs = $("connState");
    led.className = ok ? "on" : (sep ? "sep" : "");
    cs.className = led.className;
    cs.textContent = ok ? "CONNECTED" : (sep ? "DISCONNECTED" : "OFFLINE");
    $("connInfo").textContent = ok
      ? [jbc.model, "SW " + (jbc.sw || "–"), "HW " + (jbc.hw || "–")].join("  ·  ")
      : (!wsOpen ? "Backend not reachable (" + STATE_WS + ")"
         : sep ? "Polling stopped · double-click the LED to connect" : "Connect the station via USB-B");
    $("connPort").textContent = ok ? (jbc.port || "") : "";
    const mode = $("mode");
    if (mode) mode.textContent = ok && jbc.writable ? "LEVELS EDITABLE" : "READ ONLY";
    if (ok && jbc.notice && jbc.notice.id && jbc.notice.id !== lastNotice) {
      if (lastNotice !== null) toast(jbc.notice.ok, jbc.notice.text);
      lastNotice = jbc.notice.id;
    } else if (ok && jbc.notice && lastNotice === null) lastNotice = jbc.notice.id;
    let working = false;
    tiles.forEach((t, i) => {
      const d = ok && jbc.ports ? jbc.ports[i] : null;
      const st = d && d.tool_id ? d.state : "none";
      if (st === "work") working = true;
      t.el.className = "tile " + st;
      const temp = d && d.tool_id ? d.temp : null, set = d && d.tool_id ? d.set : null;
      const tc = tempColor(temp, set);
      t.el.style.setProperty("--tc", tc);
      t.tool.textContent = d && d.tool_id ? d.tool : "—";
      t.state.textContent = LABEL[st] || st.toUpperCase();
      t.state.className = "pill state" + (st === "work" ? " fill" : "") + (st === "none" ? " dim" : "");
      t.big.textContent = temp == null ? "– –" : Math.round(temp);
      t.set.textContent = set == null ? "Set –" : "Set " + Math.round(set) + " °C";
      if (st === "work" && temp != null && set != null) {
        const dv = Math.round(temp - set);
        t.dev.textContent = (dv > 0 ? "+" : "") + dv + " K";
        t.dev.style.color = Math.abs(dv) <= 10 ? COL.green : (dv > 0 ? COL.red : COL.amber);
      } else t.dev.textContent = "";
      const lv = d && d.tool_id && d.levels ? d.levels : [];
      if (t.pend && (!d || !d.tool_id || (lv[t.pend.k] != null && Math.round(lv[t.pend.k]) === t.pend.v))) clearPend(t);
      const edit = canWrite();
      t.lvl.innerHTML = lv.map((v, k) => {
        const act = k === d.level, busy = t.pend && t.pend.k === k;
        const col = busy ? COL.amber : (act ? tc : COL.low);
        const txt = v == null ? "–" : (busy ? t.pend.v : Math.round(v)) + "°";
        return `<span data-k="${k}" class="pill${act || busy ? " fill" : ""}${v == null ? " dim" : ""}${edit && v != null ? " edit" : ""}" style="--t:${col}">${txt}</span>`;
      }).join("");
      const p = d && d.power != null ? d.power : null;
      t.pw.textContent = p == null ? "–" : p.toFixed(1) + " %";
      t.pwBar.style.width = Math.min(100, p || 0) + "%";
      t.pwBar.classList.toggle("hot", (p || 0) > 80);
      t.last = d && d.tool_id ? d : null;
    });
    document.body.classList.toggle("working", working);
  }

  // Extend history at a fixed 0.5 s interval (independent of the broadcast rate)
  setInterval(() => {
    const ok = wsOpen && jbc && jbc.ok;
    tiles.forEach((t, i) => {
      const d = ok && jbc.ports ? jbc.ports[i] : null;
      t.hist.push(d && d.tool_id && d.temp != null ? d.temp : null);
      t.histSet.push(d && d.tool_id && d.set != null ? d.set : null);
      if (t.hist.length > HIST_N) { t.hist.shift(); t.histSet.shift(); }
      drawSpark(t);
    });
  }, STEP_MS);

  // ---- WebSocket ----
  let ws = null;
  function connect() {
    try { ws = new WebSocket(STATE_WS); } catch (e) { setTimeout(connect, 2000); return; }
    ws.onopen = () => { wsOpen = true; render(); };
    ws.onmessage = (ev) => {
      try {
        const s = JSON.parse(ev.data);
        jbc = s.devices && s.devices.jbc;
        render();
      } catch (e) { /* ignore */ }
    };
    ws.onclose = () => { wsOpen = false; render(); setTimeout(connect, 2000); };
    ws.onerror = () => { try { ws.close(); } catch (e) { /* */ } };
  }
  $("led").addEventListener("dblclick", () => {
    if (ws && ws.readyState === WebSocket.OPEN && jbc)
      ws.send(JSON.stringify({ cmd: "device_connection", device: "jbc", connected: !!jbc.disconnected }));
  });

  // Clock
  const clock = $("clock");
  const tick = () => { clock.textContent = new Date().toLocaleTimeString("en-GB"); };
  tick(); setInterval(tick, 1000);

  // Demo without backend (?demo=1): simulated data, e.g. for screenshots
  if (document.body.dataset.demo) {
    const sim = [
      { tool: "T210", id: 1, lv: [270, 370, 450], l: 1, t: 120, st: "work" },
      { tool: "T245", id: 2, lv: [220, 370, 450], l: 0, t: 200, st: "sleep" },
      { tool: "PA", id: 3, lv: [220, 370, 430], l: 0, t: 30, st: "hiber" },
      { tool: "Tool 10", id: 10, lv: [270, 380, 450], l: 1, t: 320, st: "stand" },
    ];
    let k = 0, nid = 0, notice = { id: 0, ok: true, text: "" };
    wsOpen = true;
    demoCmd = (c) => {
      const s = sim[c.port];
      if (c.temp == null) { s.l = c.level; notice = { id: ++nid, ok: true, text: `P${c.port + 1}: level ${c.level + 1} active (demo)` }; }
      else if (c.temp < 90 || c.temp > 450) notice = { id: ++nid, ok: false, text: `${c.temp} °C outside 90…450 °C` };
      else { s.lv[c.level] = c.temp; notice = { id: ++nid, ok: true, text: `P${c.port + 1}: level ${c.level + 1} = ${c.temp} °C (demo)` }; }
    };
    setInterval(() => {
      k++;
      if (k === 30) sim[1].st = "work";
      jbc = { ok: true, model: "DME_TCH_04", sw: "8886937", hw: "0012408C", port: "demo",
        writable: true, min_temp: 90, max_temp: 450, notice,
        ports: sim.map((s, i) => {
          const tgt = { work: s.lv[s.l], stand: 300, sleep: 200, hiber: 25 }[s.st];
          const d = tgt - s.t; s.t += d * (d > 0 ? 0.08 : 0.03) + (Math.random() - 0.5) * 2;
          return { port: i + 1, tool_id: s.id, tool: s.tool, state: s.st, temp: s.t, set: s.lv[s.l],
            power: s.st === "work" ? Math.max(0, Math.min(100, d * 1.5 + 8)) : 0, levels: s.lv, level: s.l };
        }) };
      render();
    }, STEP_MS);
  } else {
    connect();
  }
  render();
  window.addEventListener("resize", () => tiles.forEach(drawSpark));
})();
