// BeSafe — codice delle due pagine (index.html = dashboard, segnale.html = monitor per i pedoni).
// Connessione con la classe WebUI di libs/arduino.js, come nell'esempio di App Lab.
// Messaggi (CONTRATTO.md): riceve "frame_data" e "frame" (video JPEG base64), invia "set_thresholds"
// e "trigger_scenario". Nessun calcolo: la regola sta in python/frame_data.py.
(function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const SIDE_NAMES = { N: "NORD", S: "SUD", E: "EST", O: "OVEST" };
  const fmt = (x, d = 1) => x.toLocaleString("it-IT", { minimumFractionDigits: d, maximumFractionDigits: d });

  // =================================================== connessione condivisa
  // handlers: { frame_data(data), frame(msg), status("online" | "offline") }
  function connect(handlers) {
    const ui = new WebUI();
    ui.on_connect(() => handlers.status && handlers.status("online"));
    ui.on_disconnect(() => handlers.status && handlers.status("offline"));
    ui.on_message("frame_data", (data) => handlers.frame_data && handlers.frame_data(data));
    if (handlers.frame) ui.on_message("frame", handlers.frame);
    if (handlers.status) handlers.status("offline");
    return { send: (ev, data) => ui.send_message(ev, data) };
  }

  if (document.body.dataset.page === "segnale") segnale();
  else dashboard();

  // =================================================================== segnale
  // RIPOSO: logo e conteggi · ATTENZIONE: "ATTENZIONE – Veicolo da NORD" · PERICOLO: "WARNING – Veicolo da NORD"
  function segnale() {
    let lastCounts = "", lastText = "";
    const renderDirs = (el, dirs) => {
      el.innerHTML = ["→", "←"].map((d) => `<li><b>${d}</b>${(dirs || {})[d] || 0}</li>`).join("");
    };
    renderDirs($("dirs-pedone"), {});
    renderDirs($("dirs-ciclista"), {});

    connect({
      frame_data(data) {
        const cls = `segnale ${data.state}`;
        if (document.body.className !== cls) document.body.className = cls;
        if (data.state !== "RIPOSO") {
          const title = data.state === "PERICOLO" ? "WARNING" : "ATTENZIONE";
          const sub = data.danger_side ? `Veicolo da ${SIDE_NAMES[data.danger_side]}` : "Veicolo in arrivo";
          if (title + sub !== lastText) {
            lastText = title + sub;
            $("title").textContent = title;
            $("subtitle").textContent = sub;
          }
        }
        const key = JSON.stringify(data.counts || {});
        if (key !== lastCounts) {
          lastCounts = key;
          renderDirs($("dirs-pedone"), data.counts.pedone);
          renderDirs($("dirs-ciclista"), data.counts.ciclista);
        }
      },
      status(s) {
        $("dot").className = "dot " + (s === "online" ? "online" : "");
        $("mode").textContent = s === "online" ? "" : "disconnesso";
      },
    });

    // Doppio clic: schermo intero (per il monitor del dispositivo)
    document.addEventListener("dblclick", () => {
      if (document.fullscreenElement) document.exitFullscreen();
      else document.documentElement.requestFullscreen().catch(() => {});
    });
  }

  // ================================================================= dashboard
  function dashboard() {
    // Colori per categoria (CONTRATTO.md); grigio per le classi sconosciute
    const CLS_COLORS = { pedone: "#A855F7", ciclista: "#0891B2" };
    const UNKNOWN = "#9CA3AF";
    const COUNT_LINE = "#FFFFFF";
    const SIDES = ["N", "E", "S", "O"];
    // "pericolo" di un lato = il veicolo non si ferma in tempo: arancio; rosso solo con lo stato PERICOLO
    const STATUS_TEXT = { libero: "libero", arrivo: "in arrivo", nonferma: "non si ferma in tempo",
      pericolo: "non si ferma in tempo", offline: "offline" };
    const RANGE_VIS_M = 45; // distanza rappresentata da un braccio intero della croce (solo grafica)
    const DEMO = new URLSearchParams(location.search).get("demo") === "1";

    let latest = null;     // ultimo frame_data ricevuto
    let dirty = false;     // serve ridisegnare l'overlay
    let frames = 0, videoFrames = 0;

    const conn = connect({
      frame_data(data) {
        frames++;
        if (!latest || latest.camera_t !== data.camera_t || latest.state !== data.state) dirty = true;
        latest = data;
        updatePanel(data);
      },
      frame: drawVideo,
      status(s) {
        $("status").className = "status " + s;
        $("status-text").textContent = s === "online" ? "Online (locale)" : "Disconnesso · riconnessione…";
      },
    });
    setInterval(() => {
      $("fps").textContent = `${videoFrames} FPS video · ${frames} dati/s`;
      frames = videoFrames = 0;
    }, 1000);

    // Pannello demo (solo ?demo=1): modalità dei sensori simulati, scenario, azzeramento contatori
    $("demo-card").hidden = !DEMO;
    $("scenario").addEventListener("click", () => conn.send("trigger_scenario", {}));
    $("reset-counts").addEventListener("click", () => {
      if (confirm("Azzerare rilevati, attraversamenti e veicoli?")) conn.send("reset_counts", {});
    });

    // ------------------------------------------------------------- video
    const frame = $("frame"), card = $("video-card");
    const col = document.querySelector(".stage-col"), debugEl = document.querySelector(".debug");
    const videoCanvas = $("video"), overlay = $("overlay");
    let aspect = 4 / 3, decoding = false, videoDirty = false;
    const img = new Image();

    // Stesso meccanismo dell'esempio: JPEG in base64 nel messaggio "frame", disegnato su canvas
    function drawVideo(msg) {
      if (!msg || !msg.img || decoding) return;          // se il browser è indietro salta un frame
      decoding = true;
      img.onload = () => {
        decoding = false;
        videoFrames++;
        const w = img.naturalWidth, h = img.naturalHeight;
        if (Math.abs(w / h - aspect) > 1e-3 || $("placeholder").style.display !== "none") {
          aspect = w / h;
          $("res").textContent = `${w}×${h}`;
          $("placeholder").style.display = "none";
          fit();
        }
        videoDirty = true;                                 // lo disegna render(), al prossimo frame dello schermo
      };
      img.onerror = () => { decoding = false; };
      img.src = "data:image/jpeg;base64," + msg.img;
    }

    // Il frame mantiene il rapporto d'aspetto del video e sta nello spazio disponibile
    // (tutta la colonna sinistra meno la barra della camera).
    function fit() {
      const wide = window.innerWidth > 1000;
      let w = col.clientWidth;
      let h = w / aspect;
      const maxH = col.clientHeight - $("camera-bar").offsetHeight - 4;   // il video ha tutta la colonna
      if (wide && h > maxH) {
        h = Math.max(160, maxH);
        w = h * aspect;
      }
      card.style.width = `${Math.floor(w) + 4}px`;
      frame.style.width = `${Math.floor(w)}px`;
      frame.style.height = `${Math.floor(h)}px`;
      const dpr = window.devicePixelRatio || 1;
      for (const c of [videoCanvas, overlay]) {
        c.width = Math.round(w * dpr);
        c.height = Math.round(h * dpr);
      }
      dirty = true;
      videoDirty = true;
    }
    new ResizeObserver(() => requestAnimationFrame(fit)).observe(col);
    window.addEventListener("resize", fit);

    function ctxFor(canvas) {
      const ctx = canvas.getContext("2d");
      const dpr = window.devicePixelRatio || 1;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      return [ctx, canvas.width / dpr, canvas.height / dpr];
    }

    // ---------------------------------------------------- overlay
    function labelText(ctx, text, x, y, color, fs) {
      ctx.font = `600 ${fs}px "Open Sans", system-ui, sans-serif`;
      ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.strokeStyle = "rgba(0,0,0,0.55)"; ctx.strokeText(text, x, y);
      ctx.fillStyle = color; ctx.fillText(text, x, y);
    }

    function polyline(ctx, pts, W, H, close) {
      ctx.beginPath();
      pts.forEach(([x, y], i) => (i ? ctx.lineTo(x * W, y * H) : ctx.moveTo(x * W, y * H)));
      if (close) ctx.closePath();
      ctx.stroke();
    }

    // Linea di conteggio (bianca) con l'etichetta; durante "Imposta linea" quella nuova in anteprima
    function drawCountLine(ctx, line, W, H, fs, label) {
      const [a, b] = line;
      ctx.lineCap = "round";
      ctx.strokeStyle = COUNT_LINE; ctx.lineWidth = 3; ctx.setLineDash([14, 9]);
      polyline(ctx, line, W, H, false);
      ctx.setLineDash([]);
      const right = a[0] >= b[0] ? a : b;
      ctx.font = `600 ${fs}px "Open Sans", system-ui, sans-serif`;
      const tw = ctx.measureText(label).width;
      const lx = Math.min(right[0] * W + 8, W - tw - 4);
      labelText(ctx, label, lx, right[1] * H + fs * 0.35, COUNT_LINE, fs);
    }

    // Freccia orientata secondo dir (vettore unitario in pixel), centrata in (cx, cy)
    function arrow(ctx, cx, cy, dir, len) {
      const [dx, dy] = dir;
      const x1 = cx - (dx * len) / 2, y1 = cy - (dy * len) / 2;
      const x2 = cx + (dx * len) / 2, y2 = cy + (dy * len) / 2;
      const head = len * 0.42, ang = Math.atan2(dy, dx);
      ctx.lineWidth = 2; ctx.lineCap = "round"; ctx.lineJoin = "round";
      ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2);
      ctx.moveTo(x2 - head * Math.cos(ang - 0.6), y2 - head * Math.sin(ang - 0.6)); ctx.lineTo(x2, y2);
      ctx.lineTo(x2 - head * Math.cos(ang + 0.6), y2 - head * Math.sin(ang + 0.6));
      ctx.stroke();
    }

    function drawObject(ctx, o, W, H, fs) {
      const color = CLS_COLORS[o.cls] || UNKNOWN;
      const [x1, y1, x2, y2] = [o.box[0] * W, o.box[1] * H, o.box[2] * W, o.box[3] * H];
      ctx.strokeStyle = color; ctx.lineWidth = 2.5;
      ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);

      // Etichetta piena sopra il box: "pedone #3 ↑"
      const text = `${o.cls} #${o.id}`;
      ctx.font = `600 ${fs}px "Open Sans", system-ui, sans-serif`;
      const moving = o.dir && (o.dir[0] || o.dir[1]);
      const pad = fs * 0.45, arrowW = moving ? fs * 1.5 : 0;
      const lw = ctx.measureText(text).width + pad * 2 + arrowW, lh = fs * 1.55;
      const lx = Math.min(Math.max(0, x1 - 1.25), W - lw);
      let ly = y1 - lh;
      if (ly < 0) ly = y1;                                   // troppo in alto: etichetta dentro il box
      ctx.fillStyle = color;
      ctx.fillRect(lx, ly, lw, lh);
      ctx.fillStyle = "#fff";
      ctx.textBaseline = "middle";
      ctx.fillText(text, lx + pad, ly + lh / 2 + 1);
      ctx.textBaseline = "alphabetic";
      if (moving) {
        ctx.strokeStyle = "#fff";
        arrow(ctx, lx + lw - pad - arrowW / 2 + fs * 0.2, ly + lh / 2, o.dir, fs * 0.95);
      }
    }

    function render() {
      if (videoDirty && img.complete && img.naturalWidth) {
        videoDirty = false;
        const [ctx, W, H] = ctxFor(videoCanvas);
        ctx.drawImage(img, 0, 0, W, H);
      }
      if (dirty && latest) {
        dirty = false;
        const [ctx, W, H] = ctxFor(overlay);
        ctx.clearRect(0, 0, W, H);
        const fs = Math.max(11, Math.min(17, W * 0.014));
        if (lineMode) {
          if (linePts.length) {
            const p = linePts[0];
            ctx.fillStyle = COUNT_LINE;
            ctx.beginPath(); ctx.arc(p[0] * W, p[1] * H, 6, 0, 2 * Math.PI); ctx.fill();
            if (hoverPt) drawCountLine(ctx, [p, hoverPt], W, H, fs, "nuova linea");
          }
        } else if (latest.zones && latest.zones.count_line) {
          drawCountLine(ctx, latest.zones.count_line, W, H, fs, "linea di conteggio");
        }
        for (const o of latest.objects) drawObject(ctx, o, W, H, fs);
      }
      requestAnimationFrame(render);
    }
    requestAnimationFrame(render);

    // ------------------------------------------------- imposta linea di conteggio
    // "Imposta linea" → due clic sul video → set_count_line (coordinate 0–1); la scheda la salva in config.json.
    // Esc o un nuovo clic sul tasto annulla.
    let lineMode = false, linePts = [], hoverPt = null;

    function pointFromEvent(e) {
      const r = overlay.getBoundingClientRect();
      const x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
      return [Math.min(1, Math.max(0, x)), Math.min(1, Math.max(0, y))].map((v) => Math.round(v * 1e4) / 1e4);
    }

    function setLineMode(on) {
      lineMode = on;
      linePts = [];
      hoverPt = null;
      frame.classList.toggle("picking", on);
      $("set-line").classList.toggle("active", on);
      $("line-hint").hidden = !on;
      $("line-hint").textContent = "Clic sul primo punto della linea (Esc per annullare)";
      dirty = true;
    }

    $("set-line").addEventListener("click", () => setLineMode(!lineMode));
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && lineMode) setLineMode(false); });
    overlay.addEventListener("mousemove", (e) => {
      if (!lineMode || !linePts.length) return;
      hoverPt = pointFromEvent(e);
      dirty = true;
    });
    overlay.addEventListener("click", (e) => {
      if (!lineMode) return;
      linePts.push(pointFromEvent(e));
      if (linePts.length === 1) {
        $("line-hint").textContent = "Clic sul secondo punto";
        dirty = true;
        return;
      }
      conn.send("set_count_line", { count_line: linePts });
      setLineMode(false);
    });

    $("demo-mode").addEventListener("change", (e) => conn.send("set_demo_mode", { mode: e.target.value }));

    // ------------------------------------------------------------- soglie
    const sliders = { t_reaction_s: $("tr"), decel_ms2: $("decel"), brake_threshold_ms2: $("brake") };
    const outputs = { t_reaction_s: $("tr-val"), decel_ms2: $("decel-val"), brake_threshold_ms2: $("brake-val") };
    let editingUntil = 0, debounce = null, pending = null;

    function paintSlider(el) {
      const pct = ((el.value - el.min) / (el.max - el.min)) * 100;
      el.style.setProperty("--pct", `${pct}%`);
    }
    const showValue = (key) => { outputs[key].textContent = fmt(Number(sliders[key].value)); };

    for (const [key, el] of Object.entries(sliders)) {
      el.addEventListener("input", () => {
        showValue(key);
        paintSlider(el);
        editingUntil = Date.now() + 1500;
        clearTimeout(debounce);
        debounce = setTimeout(() => {
          pending = {};
          for (const [k, s] of Object.entries(sliders)) pending[k] = Number(s.value);
          $("sync").classList.add("busy");
          conn.send("set_thresholds", pending);
        }, 200);
      });
    }

    // Gli slider partono dai valori della scheda e restano allineati (tranne mentre li muovi)
    function syncThresholds(th) {
      if (!th) return;
      const cond = $("condition");
      if (cond.textContent !== th.condition) {
        cond.textContent = th.condition || "—";
        cond.className = `badge ${th.condition || ""}`;
      }
      if (pending && Object.keys(pending).every((k) => Math.abs(th[k] - pending[k]) < 1e-6)) {
        pending = null;
        $("sync").classList.remove("busy");      // la scheda ha confermato
      }
      if (Date.now() < editingUntil) return;
      for (const [key, el] of Object.entries(sliders)) {
        if (th[key] === undefined) continue;
        el.value = th[key];
        el.disabled = false;
        showValue(key);
        paintSlider(el);
      }
    }

    // ------------------------------------------------------------- pannello
    let lastState = null, lastSev = null, lastCounts = "", lastCritical = "", jsonAt = 0;

    function severityLabel(state, sev) {
      if (state === "RIPOSO") return "Nessuna";
      return sev < 0.33 ? "Bassa" : sev < 0.66 ? "Media" : "Alta";
    }

    // "30 km/h · 12,0 m / arresto 14,1 m"
    function vehicleText(s) {
      const parts = [`${Math.round(s.speed_kmh)} km/h`];
      if (s.distance_m !== null) parts.push(`${fmt(s.distance_m)} m` + (s.d_stop_m !== null ? ` / arresto ${fmt(s.d_stop_m)} m` : ""));
      return parts.join(" · ");
    }

    function updateState(data) {
      const { state, severity: sev } = data;
      if (state !== lastState || sev !== lastSev) {
        lastState = state; lastSev = sev;
        $("state-box").className = `state-box s-${state}`;
        card.className = `video-card s-${state}`;
        $("state-name").textContent = state;
        $("state-sev").textContent = `Gravità: ${severityLabel(state, sev)}`;
        $("sev-fill").style.width = `${Math.round(sev * 100)}%`;
      }
      let html;
      if (data.danger_side) {
        const s = data.sensors[data.danger_side];
        html = `<b>Veicolo da ${SIDE_NAMES[data.danger_side]}</b> · ${vehicleText(s)}${s.braking ? " · frena" : ""}`;
      } else {
        html = data.people_exposed ? "Persone inquadrate · nessun veicolo pericoloso"
          : "Nessuna persona inquadrata";
      }
      if (html !== lastCritical) { lastCritical = html; $("critical").innerHTML = html; }
    }

    const BRAKE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round"><circle cx="12" cy="12" r="7"/><path d="M12 8.5v4"/><circle cx="12" cy="15.5" r="0.4"/><path d="M3.5 7.5a10 10 0 0 0 0 9M20.5 7.5a10 10 0 0 1 0 9"/></svg>';

    function updateSensors(sensors, state, vehicles, sources) {
      if (!sensors) return;
      vehicles = vehicles || {};
      sources = sources || {};
      for (const side of SIDES) {
        const s = sensors[side];
        if (!s) continue;
        let status = s.online === false ? "offline" : s.status;
        if (status === "pericolo" && state !== "PERICOLO") status = "nonferma";   // arancio, non ancora allarme
        const li = document.querySelector(`#sides li[data-side="${side}"]`);
        li.className = status;
        const off = sources[side] === "off";                 // lato senza sensore: ignorato dalla regola
        const meas = s.distance_m === null
          ? `<span class="none">${off ? "veicolo non presente" : status === "offline" ? "nessun dato" : "nessun veicolo"}</span>`
          : `${fmt(s.distance_m)} m · ${Math.round(s.speed_kmh)} km/h` +
            (s.braking ? ` <span class="brake" title="sta frenando: ${fmt(s.decel_ms2)} m/s²">${BRAKE_ICON}frena</span>` : "");
        const stop = s.d_stop_m !== null ? `arresto ${fmt(s.d_stop_m)} m` : "&nbsp;";
        const html = `<div class="head"><span class="name">${SIDE_NAMES[side]}</span><span class="st">${STATUS_TEXT[status]}</span></div>` +
          `<div class="meas">${meas}</div><div class="stop">${stop}</div>` +
          `<div class="veh">veicoli: ${vehicles[side] || 0}</div>`;
        if (li.innerHTML !== html) li.innerHTML = html;

        // Croce: braccio colorato e veicolo alla sua distanza (verso il centro = più vicino)
        const arm = document.querySelector(`#cross .arm[data-side="${side}"]`);
        arm.setAttribute("class", `arm ${status}${s.distance_m !== null ? " has-car" : ""}`);
        if (s.distance_m !== null) {
          const p = Math.min(1, s.distance_m / RANGE_VIS_M) * 66;
          const car = arm.querySelector(".car");
          if (side === "N") car.setAttribute("y", 66 - p);
          if (side === "S") car.setAttribute("y", 124 + p);
          if (side === "E") car.setAttribute("x", 124 + p);
          if (side === "O") car.setAttribute("x", 66 - p);
        }
      }
    }

    const dirItems = (dirs) => ["→", "←"].map((d) => `<li><b>${d}</b>${(dirs || {})[d] || 0}</li>`).join("");

    // Persone: rilevati (numero grande) e attraversamenti della linea (→ / ←); veicoli passati in totale
    function updateCounts(counts) {
      const key = JSON.stringify(counts);
      if (key === lastCounts) return;
      lastCounts = key;
      const detected = counts.detected || {};
      for (const cls of ["pedone", "ciclista"]) {
        $(`det-${cls}`).textContent = detected[cls] || 0;
        $(`dirs-${cls}`).innerHTML = dirItems(counts[cls]);
      }
      const veh = Object.values(counts.vehicles || {}).reduce((a, b) => a + b, 0);
      $("veh-total").textContent = `${veh} ${veh === 1 ? "veicolo passato" : "veicoli passati"}`;
    }

    // Evento: ora della scheda + lato, velocità, distanza e spazio di arresto
    function addEvent(ev) {
      const time = new Date(ev.t * 1000).toLocaleTimeString("it-IT", { hour: "2-digit", minute: "2-digit" });
      const list = $("events");
      const empty = list.querySelector(".empty");
      if (empty) empty.remove();
      const li = document.createElement("li");
      li.className = "new";
      li.innerHTML = `<span class="time">${time}</span><span>Veicolo da ${SIDE_NAMES[ev.side]} · ${vehicleText(ev)}</span>`;
      list.prepend(li);
      while (list.children.length > 20) list.lastElementChild.remove();
    }

    function updatePanel(data) {
      // Pannello demo: senza sensori simulati restano solo l'azzeramento; "Genera pericolo" non in "off"
      $("demo-mode").disabled = !data.mock_sensors;
      $("scenario").disabled = !data.mock_sensors || data.demo_mode === "off";
      if (data.demo_mode && document.activeElement !== $("demo-mode")) $("demo-mode").value = data.demo_mode;
      syncThresholds(data.thresholds);
      updateState(data);
      updateSensors(data.sensors, data.state, (data.counts || {}).vehicles, data.sources);
      const model = `${data.model || "YoloX"} · on-device`;
      if ($("model").textContent !== model) $("model").textContent = model;
      updateCounts(data.counts || {});
      if (data.event) addEvent(data.event);
      $("cam-age").textContent = data.camera_t === null ? "analisi —"
        : `analisi ${fmt(Math.max(0, data.t - data.camera_t))} s fa`;
      if (debugEl.open && Date.now() - jsonAt > 200) {          // JSON solo se il pannello è aperto
        jsonAt = Date.now();
        $("json").textContent = JSON.stringify(data, null, 2);
      }
    }
  }
})();
