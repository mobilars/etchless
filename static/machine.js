/* GRBL machine control over Web Serial: connect, jog, probe grid, autolevel, stream.
   Chromium-only (Web Serial); page must be served over HTTPS or localhost. */
"use strict";

const Machine = (() => {
  let port = null, writer = null, readerAbort = null;
  let lineWaiters = [];          // resolvers for ok/error acks (send-and-wait mode)
  let probeReports = [];         // [PRB:...] lines
  let status = { state: "N/A", wpos: [0, 0, 0], mpos: [0, 0, 0], wco: [0, 0, 0] };
  let statusTimer = null;
  let stream = { active: false, paused: false, abort: false, sent: 0, total: 0, inflight: [] };
  let leveling = null;           // {xs, ys, z[iy][ix], ref}

  const enc = new TextEncoder();
  const listeners = { status: [], line: [], progress: [] };
  const on = (ev, fn) => listeners[ev].push(fn);
  const emit = (ev, arg) => listeners[ev].forEach(f => f(arg));

  // ---------------------------------------------------------------- serial

  async function connect() {
    if (!("serial" in navigator))
      throw new Error("Web Serial not available - use Chrome/Edge over HTTPS or localhost");
    port = await navigator.serial.requestPort();
    await port.open({ baudRate: 115200 });
    writer = port.writable.getWriter();
    readLoop();
    await raw("\r\n\r\n");             // wake GRBL
    await new Promise(r => setTimeout(r, 1600));
    statusTimer = setInterval(() => { raw("?").catch(() => {}); }, 250);
    return true;
  }

  async function disconnect() {
    clearInterval(statusTimer);
    stream.abort = true;
    try { readerAbort?.abort(); } catch {}
    try { writer?.releaseLock(); } catch {}
    try { await port?.close(); } catch {}
    port = writer = null;
    status.state = "N/A";
    emit("status", status);
  }

  async function readLoop() {
    readerAbort = new AbortController();
    const decoder = new TextDecoderStream();
    const closed = port.readable.pipeTo(decoder.writable, { signal: readerAbort.signal }).catch(() => {});
    const reader = decoder.readable.getReader();
    let buf = "";
    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += value;
        let i;
        while ((i = buf.search(/[\r\n]/)) >= 0) {
          const line = buf.slice(0, i).trim();
          buf = buf.slice(i + 1);
          if (line) handleLine(line);
        }
      }
    } catch {}
    await closed;
  }

  function handleLine(line) {
    if (line.startsWith("<")) { parseStatus(line); return; }
    emit("line", line);
    if (line.startsWith("[PRB:")) probeReports.push(line);
    if (line === "ok" || line.startsWith("error")) {
      const w = lineWaiters.shift();
      if (w) w(line);
      if (stream.active) streamAck(line);
    }
    if (line.startsWith("ALARM")) {
      stream.abort = true;
      const w = lineWaiters.shift();
      if (w) w(line);
    }
  }

  function parseStatus(s) {
    const state = s.match(/^<([^,|]+)/)?.[1] ?? "?";
    const mpos = s.match(/MPos:([-\d.]+),([-\d.]+),([-\d.]+)/);
    const wpos = s.match(/WPos:([-\d.]+),([-\d.]+),([-\d.]+)/);
    const wco = s.match(/WCO:([-\d.]+),([-\d.]+),([-\d.]+)/);
    status.state = state;
    if (wco) status.wco = wco.slice(1, 4).map(Number);
    if (mpos) {
      status.mpos = mpos.slice(1, 4).map(Number);
      status.wpos = status.mpos.map((v, i) => v - status.wco[i]);
    } else if (wpos) {
      status.wpos = wpos.slice(1, 4).map(Number);
    }
    emit("status", status);
  }

  async function raw(s) { await writer.write(enc.encode(s)); }

  // send one line, resolve on ok/error/alarm
  function cmd(line) {
    return new Promise((resolve, reject) => {
      lineWaiters.push(resp => resp === "ok" ? resolve(resp) : reject(new Error(`${line} -> ${resp}`)));
      raw(line + "\n").catch(reject);
    });
  }

  const unlock = () => cmd("$X");
  const home = () => cmd("$H");
  const softReset = async () => { stream.abort = true; lineWaiters = []; await raw("\x18"); };
  const hold = () => raw("!");
  const resume = () => raw("~");
  const jog = (axis, dist, feed) => cmd(`$J=G91 G21 ${axis}${dist} F${feed}`);
  const zeroAxis = axes => cmd(`G10 L20 P1 ${axes}`);

  // ---------------------------------------------------------------- probing

  async function waitIdle(timeoutMs = 60000) {
    const t0 = Date.now();
    while (status.state !== "Idle") {
      if (Date.now() - t0 > timeoutMs) throw new Error("timeout waiting for Idle");
      await new Promise(r => setTimeout(r, 120));
    }
  }

  /* Probe a grid over [minx..maxx]x[miny..maxy] (work coords).
     Returns leveling object. Z must already be zeroed on the copper at the origin. */
  async function probeGrid(minx, miny, maxx, maxy, opts, onPoint) {
    const { spacing = 7.5, safeZ = 1.5, maxDepth = 2.0, feed = 30 } = opts || {};
    const nx = Math.max(2, Math.ceil((maxx - minx) / spacing) + 1);
    const ny = Math.max(2, Math.ceil((maxy - miny) / spacing) + 1);
    const xs = Array.from({ length: nx }, (_, i) => minx + (maxx - minx) * i / (nx - 1));
    const ys = Array.from({ length: ny }, (_, i) => miny + (maxy - miny) * i / (ny - 1));
    const z = ys.map(() => xs.map(() => 0));

    await cmd("G21 G90");
    for (let iy = 0; iy < ny; iy++) {
      // serpentine to shorten travel
      const order = iy % 2 ? [...xs.keys()].reverse() : [...xs.keys()];
      for (const ix of order) {
        await cmd(`G0 Z${safeZ.toFixed(3)}`);
        await cmd(`G0 X${xs[ix].toFixed(3)} Y${ys[iy].toFixed(3)}`);
        probeReports = [];
        await cmd(`G38.2 Z-${maxDepth.toFixed(3)} F${feed}`);
        await waitIdle();
        const prb = probeReports.pop();
        const m = prb?.match(/PRB:([-\d.]+),([-\d.]+),([-\d.]+):(\d)/);
        if (!m || m[4] !== "1") throw new Error(`probe failed at X${xs[ix].toFixed(2)} Y${ys[iy].toFixed(2)}`);
        // PRB reports machine coords; convert to work coords
        z[iy][ix] = Number(m[3]) - status.wco[2];
        onPoint?.(ix, iy, z[iy][ix], nx * ny);
      }
    }
    await cmd(`G0 Z${safeZ.toFixed(3)}`);

    // reference: interpolated height nearest the work origin, so Z0 stays true there
    const lev = { xs, ys, z, ref: 0 };
    lev.ref = interpolate(lev, 0, 0);
    leveling = lev;
    return lev;
  }

  function interpolate(lev, x, y) {
    const { xs, ys, z } = lev;
    const clamp = (v, a, b) => Math.min(Math.max(v, a), b);
    x = clamp(x, xs[0], xs[xs.length - 1]);
    y = clamp(y, ys[0], ys[ys.length - 1]);
    let ix = xs.findIndex((v, i) => i === xs.length - 2 || xs[i + 1] >= x);
    let iy = ys.findIndex((v, i) => i === ys.length - 2 || ys[i + 1] >= y);
    const tx = (x - xs[ix]) / (xs[ix + 1] - xs[ix] || 1);
    const ty = (y - ys[iy]) / (ys[iy + 1] - ys[iy] || 1);
    const a = z[iy][ix] * (1 - tx) + z[iy][ix + 1] * tx;
    const b = z[iy + 1][ix] * (1 - tx) + z[iy + 1][ix + 1] * tx;
    return a * (1 - ty) + b * ty;
  }

  // ---------------------------------------------------------------- leveling transform

  const word = (line, w) => {
    const m = line.match(new RegExp(w + "(-?\\d*\\.?\\d+)"));
    return m ? Number(m[1]) : null;
  };

  /* Add the probed surface offset to every cutting move (programmed Z < 0). */
  function applyLeveling(gcode, lev) {
    let x = 0, y = 0, modalZ = 0;
    const out = [];
    for (const rawLine of gcode.split("\n")) {
      const line = rawLine.trim();
      const g = line.match(/^G([01])\b/);
      if (!g) { out.push(rawLine); continue; }
      const gx = word(line, "X"), gy = word(line, "Y"), gz = word(line, "Z");
      if (gx !== null) x = gx;
      if (gy !== null) y = gy;
      if (gz !== null) modalZ = gz;
      const dz = interpolate(lev, x, y) - lev.ref;
      if (gz !== null && gz < 0) {
        out.push(rawLine.replace(/Z-?\d*\.?\d+/, "Z" + (gz + dz).toFixed(4)));
      } else if (g[1] === "1" && gz === null && modalZ < 0 && (gx !== null || gy !== null)) {
        out.push(rawLine + " Z" + (modalZ + dz).toFixed(4));
      } else {
        out.push(rawLine);
      }
    }
    return out.join("\n");
  }

  // ---------------------------------------------------------------- streaming (char-count)

  const RX_BUFFER = 127;

  function streamAck() {
    if (stream.inflight.length) stream.inflight.shift();
    stream.sent++;
    emit("progress", { sent: stream.sent, total: stream.total });
    pump();
  }

  let pumpQueue = [];
  function pump() {
    if (!stream.active || stream.paused || stream.abort) return;
    while (pumpQueue.length) {
      const line = pumpQueue[0];
      const used = stream.inflight.reduce((s, n) => s + n, 0);
      if (used + line.length + 1 > RX_BUFFER) break;
      stream.inflight.push(line.length + 1);
      pumpQueue.shift();
      raw(line + "\n").catch(() => { stream.abort = true; });
    }
    if (!pumpQueue.length && !stream.inflight.length) {
      stream.active = false;
      emit("progress", { sent: stream.sent, total: stream.total, done: true });
    }
  }

  function startStream(gcode) {
    pumpQueue = gcode.split("\n").map(l => l.replace(/\(.*?\)/g, "").trim()).filter(Boolean);
    stream = { active: true, paused: false, abort: false, sent: 0, total: pumpQueue.length, inflight: [] };
    pump();
  }

  async function pauseStream() { stream.paused = true; await hold(); }
  async function resumeStream() { stream.paused = false; await resume(); pump(); }
  async function stopStream() {
    stream.abort = true; stream.active = false; pumpQueue = [];
    await hold();
    await new Promise(r => setTimeout(r, 400));
    await softReset();
  }

  return { connect, disconnect, cmd, unlock, home, softReset, jog, zeroAxis,
           probeGrid, interpolate, applyLeveling, startStream, pauseStream,
           resumeStream, stopStream, on,
           get status() { return status; },
           get leveling() { return leveling; },
           get connected() { return !!port; },
           get streaming() { return stream.active; } };
})();
