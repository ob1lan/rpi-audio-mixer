"use strict";

// Same RBJ biquads as equalizer.py; all audio processing stays on the server.
const EQMath = (() => {
  const rate = 48000, identity = [1, 0, 0, 1, 0, 0];
  function defaults() {
    return {enabled:false, preamp:0, auto_headroom:true,
      bands:[80, 250, 630, 1600, 4000, 10000].map(frequency => ({enabled:true, type:"peak", frequency, gain:0, q:1})),
      highpass:{enabled:false, frequency:40}, lowpass:{enabled:false, frequency:18000}};
  }
  function biquad(kind, frequency, gain=0, q=1) {
    const w = 2 * Math.PI * frequency / rate, c = Math.cos(w), s = Math.sin(w), alpha = s / (2 * q), a = 10 ** (gain / 40);
    let b, d;
    if (kind === "peak") { b = [1+alpha*a, -2*c, 1-alpha*a]; d = [1+alpha/a, -2*c, 1-alpha/a]; }
    else if (kind === "highpass") { b = [(1+c)/2, -(1+c), (1+c)/2]; d = [1+alpha, -2*c, 1-alpha]; }
    else if (kind === "lowpass") { b = [(1-c)/2, 1-c, (1-c)/2]; d = [1+alpha, -2*c, 1-alpha]; }
    else {
      const beta = s * Math.sqrt(2*a);
      if (kind === "low_shelf") {
        b = [a*((a+1)-(a-1)*c+beta), 2*a*((a-1)-(a+1)*c), a*((a+1)-(a-1)*c-beta)];
        d = [(a+1)+(a-1)*c+beta, -2*((a-1)+(a+1)*c), (a+1)+(a-1)*c-beta];
      } else if (kind === "high_shelf") {
        b = [a*((a+1)+(a-1)*c+beta), -2*a*((a-1)+(a+1)*c), a*((a+1)+(a-1)*c-beta)];
        d = [(a+1)-(a-1)*c+beta, 2*((a-1)-(a+1)*c), (a+1)-(a-1)*c-beta];
      } else throw new Error("Type de filtre inconnu");
    }
    return [...b, ...d].map(v => v / d[0]);
  }
  function coefficients(settings) {
    return ["highpass", "lowpass"].map(name => settings[name].enabled ? biquad(name, settings[name].frequency, 0, Math.SQRT1_2) : identity)
      .concat(settings.bands.map(b => b.enabled && b.gain ? biquad(b.type, b.frequency, b.gain, b.q) : identity));
  }
  function response(rows, frequency) {
    const w = 2 * Math.PI * frequency / rate, c = Math.cos(w), s = Math.sin(w), c2 = Math.cos(2*w), s2 = Math.sin(2*w);
    let amplitude = 1;
    for (const [b0,b1,b2,a0,a1,a2] of rows) {
      amplitude *= Math.hypot(b0+b1*c+b2*c2, b1*s+b2*s2) / Math.hypot(a0+a1*c+a2*c2, a1*s+a2*s2);
    }
    return 20 * Math.log10(Math.max(1e-12, amplitude));
  }
  function compensation(settings, rows=coefficients(settings)) {
    if (!settings.auto_headroom) return 0;
    let peak = 0;
    for (let i=0; i<1024; i++) peak = Math.max(peak, response(rows, 20 * 1000 ** (i / 1023)));
    for (const band of settings.bands) peak = Math.max(peak, response(rows, band.frequency));
    return -peak;
  }
  const frequencyPosition = frequency => Math.log(frequency / 20) / Math.log(1000);
  const positionFrequency = position => 20 * 1000 ** Math.max(0, Math.min(1, position));
  return {defaults, biquad, coefficients, response, compensation, frequencyPosition, positionFrequency};
})();
if (typeof module !== "undefined" && module.exports) module.exports = EQMath;

function createEqualizerEditor({api, onState, getLevels}) {
  const el = id => document.getElementById(id), clone = value => JSON.parse(JSON.stringify(value));
  const panel = el("equalizer-panel"), canvas = el("eq-graph"), context = canvas.getContext("2d");
  const colors = ["#edc66f", "#f49a7f", "#bd9be7", "#85cbc7", "#a5ce81", "#83b5e5"];
  let settings = EQMath.defaults(), server = null, selected = 0, pending = null, running = false, timer = null;
  let revision = 0, savedRevision = 0, serverRevision = -1, error = "", drag = null, paintFrame = null;
  let rows = EQMath.coefficients(settings), compensation = 0;
  const bandButtons = settings.bands.map((band, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.style.setProperty("--band-color", colors[index]);
    button.innerHTML = `B${index+1}<span></span>`;
    button.addEventListener("click", () => { selected = index; render(); });
    el("eq-bands").append(button);
    return button;
  });

  function recalculate() { rows = EQMath.coefficients(settings); compensation = EQMath.compensation(settings, rows); }
  function inputValue(id, value) { if (document.activeElement !== el(id)) el(id).value = String(value); }
  function render() {
    const band = settings.bands[selected], available = Boolean(server?.available);
    const editable = Boolean(server) && (available || !settings.enabled);
    el("eq-enable").disabled = !server || (!available && !settings.enabled);
    el("eq-enable").setAttribute("aria-pressed", String(settings.enabled));
    el("eq-enable").textContent = settings.enabled ? (available ? "EQ actif · bypass" : "EQ indisponible · bypass") : "EQ bypassé · activer";
    el("eq-enable").title = settings.enabled ? "Bypasser l’égaliseur" : "Activer l’égaliseur";
    el("open-equalizer").classList.toggle("eq-is-active", available && settings.enabled);
    el("open-equalizer").title = available && settings.enabled ? "Égaliseur MASTER actif" : "Égaliseur MASTER";
    el("eq-status").textContent = server?.demo ? "APERÇU SIMULÉ · aucun traitement matériel" : (available ? "MASTER · réglages en direct" : "Traitement indisponible");
    el("eq-unavailable").hidden = available || Boolean(server?.demo);
    el("eq-unavailable").textContent = server?.message || "Connexion à la console…";
    el("eq-band-title").textContent = `Bande ${selected+1}`;
    panel.style.setProperty("--band-color", colors[selected]);
    for (const [index, button] of bandButtons.entries()) {
      const b = settings.bands[index];
      button.setAttribute("aria-pressed", String(index === selected));
      button.setAttribute("aria-label", `Bande ${index+1}, ${Math.round(b.frequency)} Hz, ${b.gain.toFixed(1)} dB${b.enabled ? "" : ", désactivée"}`);
      button.querySelector("span").textContent = `${b.frequency >= 1000 ? (b.frequency/1000).toFixed(1)+"k" : Math.round(b.frequency)} Hz`;
      button.disabled = !server;
    }
    for (const id of ["eq-preset", "eq-reset", "eq-band-enabled", "eq-type", "eq-frequency", "eq-frequency-slider", "eq-gain", "eq-gain-slider", "eq-q", "eq-q-slider", "eq-highpass-enabled", "eq-lowpass-enabled", "eq-highpass", "eq-lowpass", "eq-auto-headroom", "eq-preamp"]) el(id).disabled = !editable;
    el("eq-q").disabled = el("eq-q-slider").disabled = !editable || band.type !== "peak";
    el("eq-q-help").textContent = band.type === "peak" ? "Q faible : correction large. Q élevé : correction étroite." : "Les plateaux utilisent une pente douce fixe, sans résonance.";
    inputValue("eq-type", band.type);
    inputValue("eq-frequency", Math.round(band.frequency));
    inputValue("eq-gain", band.gain.toFixed(1));
    inputValue("eq-q", band.q.toFixed(2));
    inputValue("eq-frequency-slider", Math.round(EQMath.frequencyPosition(band.frequency)*1000));
    inputValue("eq-gain-slider", band.gain);
    inputValue("eq-q-slider", band.q);
    el("eq-band-enabled").checked = band.enabled;
    for (const name of ["highpass", "lowpass"]) {
      el(`eq-${name}-enabled`).checked = settings[name].enabled;
      inputValue(`eq-${name}`, settings[name].frequency);
    }
    el("eq-auto-headroom").checked = settings.auto_headroom;
    inputValue("eq-preamp", settings.preamp);
    el("eq-compensation").textContent = `Compensation : ${compensation.toFixed(1)} dB · total : ${(compensation+settings.preamp).toFixed(1)} dB`;
    el("eq-curve-label").textContent = settings.enabled && available ? "Réponse de l’EQ · gain compensé" : "Courbe préparée en pointillés · bypass";
    el("eq-save-status").textContent = error || (running || pending || savedRevision < revision ? "Application et sauvegarde…" : "Réglages sauvegardés sur le Pi.");
    el("eq-save-status").classList.toggle("is-error", Boolean(error));
    el("eq-retry").hidden = !error;
    el("eq-retry").disabled = running;
    schedulePaint();
  }

  function receive(snapshot) {
    if (!snapshot || (snapshot.revision ?? 0) < serverRevision) return;
    serverRevision = snapshot.revision ?? 0;
    server = snapshot;
    const focused = panel.contains(document.activeElement) && document.activeElement?.tagName === "INPUT";
    if (!running && !pending && !drag && !error && !focused) { settings = clone(snapshot.settings); recalculate(); }
    render();
  }

  function edit(mutate) {
    const next = clone(settings);
    mutate(next);
    if (next.highpass.enabled && next.lowpass.enabled && next.highpass.frequency >= next.lowpass.frequency) {
      error = "Le passe-haut doit être inférieur au passe-bas."; render(); return false;
    }
    settings = next; revision++; error = "";
    pending = {settings:clone(settings), revision};
    recalculate(); render();
    clearTimeout(timer); timer = setTimeout(flush, 140);
    return true;
  }

  async function flush() {
    if (running || !pending) return;
    clearTimeout(timer);
    const sent = pending; pending = null; running = true; render();
    try {
      const result = await api("/api/equalizer", sent.settings);
      savedRevision = sent.revision;
      if (revision === sent.revision) error = "";
      // receive() preserves newer local gestures until the pending write lands.
      onState(result.state);
      receive(result.state.equalizer);
    } catch (failure) {
      error = `Non enregistré : ${failure.message}`;
    } finally {
      running = false;
      if (pending) flush();
      else render();
    }
  }

  function numeric(id, mutate, transform=v => v) {
    el(id).addEventListener("input", () => {
      const input = el(id);
      if (input.value === "" || !input.checkValidity() || !Number.isFinite(Number(input.value))) return;
      const value = transform(Number(input.value));
      el("eq-preset").value = "custom";
      edit(next => mutate(next, value));
    });
    el(id).addEventListener("change", () => {
      if (!el(id).checkValidity() || el(id).value === "") el(id).reportValidity();
      flush();
    });
    el(id).addEventListener("blur", () => render());
  }
  for (const [id, key, transform] of [
    ["eq-frequency", "frequency", v => v],
    ["eq-frequency-slider", "frequency", v => Math.round(EQMath.positionFrequency(v/1000))],
    ["eq-gain", "gain", v => v], ["eq-gain-slider", "gain", v => v],
    ["eq-q", "q", v => v], ["eq-q-slider", "q", v => v]
  ]) numeric(id, (next, value) => { next.bands[selected][key] = value; }, transform);
  numeric("eq-preamp", (next, value) => { next.preamp = value; });
  for (const name of ["highpass", "lowpass"]) {
    numeric(`eq-${name}`, (next, value) => { next[name].frequency = value; });
    el(`eq-${name}-enabled`).addEventListener("change", () => { edit(next => { next[name].enabled = el(`eq-${name}-enabled`).checked; }); flush(); });
  }
  for (const [id, mutate] of [
    ["eq-band-enabled", next => { next.bands[selected].enabled = el("eq-band-enabled").checked; }],
    ["eq-type", next => { next.bands[selected].type = el("eq-type").value; }],
    ["eq-auto-headroom", next => { next.auto_headroom = el("eq-auto-headroom").checked; }]
  ]) el(id).addEventListener("change", () => { el("eq-preset").value = "custom"; edit(mutate); flush(); });
  el("eq-enable").addEventListener("click", () => { edit(next => { next.enabled = !next.enabled; }); flush(); });
  el("eq-reset").addEventListener("click", () => {
    el("eq-preset").value = "flat";
    edit(next => { const enabled = next.enabled; Object.assign(next, EQMath.defaults(), {enabled}); }); flush();
  });
  el("eq-preset").addEventListener("change", () => {
    const name = el("eq-preset").value;
    if (name === "custom") return;
    edit(next => {
      const enabled = next.enabled;
      Object.assign(next, EQMath.defaults(), {enabled});
      if (name === "voice") { next.highpass = {enabled:true, frequency:80}; next.bands[1].gain = -2.5; next.bands[3].gain = 2; next.bands[4].gain = 1.5; }
      if (name === "warm") { next.bands[0].type = "low_shelf"; next.bands[0].gain = 3; next.bands[4].gain = -1.5; }
      if (name === "presence") { next.bands[2].gain = -1.5; next.bands[4].gain = 2.5; next.bands[5].type = "high_shelf"; next.bands[5].gain = 1.5; }
    }); flush();
  });
  el("eq-retry").addEventListener("click", () => { error = ""; pending = {settings:clone(settings), revision}; flush(); });
  el("eq-spectrum").addEventListener("change", schedulePaint);

  function geometry() {
    const rect = canvas.getBoundingClientRect();
    return {rect, left:42, top:22, width:Math.max(1, rect.width-60), height:Math.max(1, rect.height-54)};
  }
  function point(band, g) {
    return {x:g.left+EQMath.frequencyPosition(band.frequency)*g.width, y:g.top+(12-band.gain)/24*g.height};
  }
  canvas.addEventListener("pointerdown", event => {
    if (drag || event.button !== 0 || !server || (!server.available && settings.enabled)) return;
    const g = geometry(), x = event.clientX-g.rect.left, y = event.clientY-g.rect.top;
    const distances = settings.bands.map((b, index) => ({index, distance:Math.hypot(point(b,g).x-x, point(b,g).y-y)})).sort((a,b) => a.distance-b.distance);
    if (distances[0].distance > 24) return;
    event.preventDefault();
    selected = distances[0].index;
    const p = point(settings.bands[selected], g);
    drag = {id:event.pointerId, x:x-p.x, y:y-p.y};
    canvas.setPointerCapture(event.pointerId); render();
  });
  canvas.addEventListener("pointermove", event => {
    if (!drag || drag.id !== event.pointerId) return;
    const g = geometry();
    const frequency = Math.round(EQMath.positionFrequency((event.clientX-g.rect.left-drag.x-g.left)/g.width));
    const gain = Math.round(Math.max(-12, Math.min(12, 12-(event.clientY-g.rect.top-drag.y-g.top)/g.height*24))*10)/10;
    el("eq-preset").value = "custom";
    edit(next => { next.bands[selected].frequency = frequency; next.bands[selected].gain = gain; });
  });
  function finishDrag(event) {
    if (!drag || drag.id !== event.pointerId) return;
    drag = null; flush();
    if (canvas.hasPointerCapture(event.pointerId)) canvas.releasePointerCapture(event.pointerId);
  }
  for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) canvas.addEventListener(name, finishDrag);
  panel.addEventListener("close", () => { drag = null; flush(); });

  function schedulePaint() {
    if (paintFrame !== null) return;
    paintFrame = requestAnimationFrame(() => { paintFrame = null; paint(); });
  }
  function paint() {
    if (!panel.open || !context) return;
    const g = geometry(), ratio = window.devicePixelRatio || 1;
    canvas.width = Math.round(g.rect.width*ratio); canvas.height = Math.round(g.rect.height*ratio);
    context.setTransform(ratio,0,0,ratio,0,0); context.clearRect(0,0,g.rect.width,g.rect.height);
    const x = f => g.left+EQMath.frequencyPosition(f)*g.width;
    const y = db => g.top+(12-db)/24*g.height;
    context.font = "10px system-ui"; context.textAlign = "right";
    for (const db of [-12,-6,0,6,12]) {
      context.strokeStyle = db === 0 ? "#647780" : "#2c3b43";
      context.beginPath(); context.moveTo(g.left,y(db)); context.lineTo(g.left+g.width,y(db)); context.stroke();
      context.fillStyle = "#9ab0bb"; context.fillText(`${db>0?"+":""}${db}`,g.left-8,y(db)+3);
    }
    context.textAlign = "center";
    const ticks = g.width < 420 ? [20,100,1000,10000,20000] : [20,50,100,200,500,1000,2000,5000,10000,20000];
    for (const frequency of ticks) {
      context.strokeStyle = "#2c3b43"; context.beginPath(); context.moveTo(x(frequency),g.top); context.lineTo(x(frequency),g.top+g.height); context.stroke();
      context.fillStyle = "#9ab0bb"; context.fillText(frequency>=1000 ? `${frequency/1000}k` : String(frequency),x(frequency),g.top+g.height+18);
    }
    context.textAlign = "left"; context.fillText("dB",8,14); context.textAlign = "right"; context.fillText("Hz",g.rect.width-4,g.rect.height-4);
    const levels = getLevels(), spectrum = levels?.spectrum;
    const spectrumFresh = Boolean(levels?.available && spectrum && Array.isArray(spectrum.db) && Array.isArray(spectrum.frequencies));
    el("eq-spectrum-status").textContent = spectrumFresh && el("eq-spectrum").checked ? "Spectre de sortie mesuré · −100 à 0 dBFS" : (server?.demo ? "Spectre indisponible · aperçu simulé" : "Spectre inactif");
    context.save(); context.beginPath(); context.rect(g.left,g.top,g.width,g.height); context.clip();
    if (spectrumFresh && el("eq-spectrum").checked) {
      context.beginPath(); context.moveTo(g.left,g.top+g.height);
      spectrum.db.forEach((db,index) => context.lineTo(x(spectrum.frequencies[index]),g.top+Math.max(0,Math.min(1,-db/100))*g.height));
      context.lineTo(g.left+g.width,g.top+g.height); context.closePath(); context.fillStyle = "#90b1bd25"; context.fill(); context.strokeStyle = "#90b1bd80"; context.stroke();
    }
    function curve(coefficients, offset, color, width, dashed=false) {
      context.strokeStyle = color; context.lineWidth = width; context.setLineDash(dashed ? [5,4] : []); context.beginPath();
      for (let i=0;i<=360;i++) {
        const frequency = EQMath.positionFrequency(i/360), px = x(frequency), py = y(EQMath.response(coefficients,frequency)+offset);
        if (i===0) context.moveTo(px,py); else context.lineTo(px,py);
      }
      context.stroke(); context.setLineDash([]);
    }
    const band = settings.bands[selected];
    if (band.enabled) curve([EQMath.biquad(band.type,band.frequency,band.gain,band.q)],0,colors[selected]+"85",1);
    curve(rows,settings.preamp+compensation,"#d7eee7",2.2,!settings.enabled || !server?.available);
    context.restore();
    settings.bands.forEach((b,index) => {
      const p = point(b,g); context.beginPath(); context.arc(p.x,p.y,index===selected?10:8,0,2*Math.PI);
      context.fillStyle = b.enabled ? colors[index] : "#32434c"; context.fill(); context.lineWidth = index===selected?2:1; context.strokeStyle = index===selected?"#fff":colors[index]; context.stroke();
      context.fillStyle = b.enabled ? "#15252b" : colors[index]; context.font = "bold 10px system-ui"; context.textAlign = "center"; context.fillText(String(index+1),p.x,p.y+3);
    });
  }
  new ResizeObserver(schedulePaint).observe(canvas);
  el("open-equalizer").addEventListener("click", () => { render(); schedulePaint(); });
  setInterval(() => { if (panel.open && !document.hidden) schedulePaint(); }, 100);
  render();
  return {receive};
}
