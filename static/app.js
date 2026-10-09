"use strict";
const $ = id => document.getElementById(id);
let state = null, toastTimer = null, polling = false, authenticated = false;
const cards = new Map(), receivers = new Map(), writes = new Map(), actions = new Set();
const routeRows = new Map();
const dragging = new Set();
let layoutFrame = null, outputDirty = false, outputFingerprint = "", savedOutputFingerprint = "", openedConfiguration = false;

function scheduleLayout() {
  if (layoutFrame !== null) return;
  layoutFrame = requestAnimationFrame(() => {
    layoutFrame = null;
    if ($("dashboard").hidden || dragging.size) return;
    const strips = [...document.querySelectorAll(".console-rack .channel-strip")];
    const rack = document.querySelector(".console-rack");
    const mobile = window.matchMedia("(max-width: 759px)").matches;
    if (!mobile && strips.length) {
      const capacity = Math.max(4, Math.floor((rack.clientWidth - 32) / 220));
      const rowCount = Math.ceil(strips.length / capacity);
      rack.style.setProperty("--channel-columns", String(Math.ceil(strips.length / rowCount)));
    }
    const rows = new Set(strips.map(strip => Math.round(strip.getBoundingClientRect().top))).size;
    if (!rows) return;
    const current = $("master-volume").getBoundingClientRect().height;
    const pageHeight = document.querySelector("main").getBoundingClientRect().height;
    const viewport = window.visualViewport;
    const availableHeight = viewport && Math.abs(viewport.scale - 1) < .01 ? viewport.height : window.innerHeight;
    // Garder une course utilisable ; les très petits écrans peuvent défiler.
    const fitted = Math.floor((availableHeight - pageHeight + rows * current) / rows);
    const next = Math.max(112, Math.min(mobile ? 300 : 600, fitted));
    if (Math.abs(next - current) > 1) document.documentElement.style.setProperty("--fader-height", `${next}px`);
  });
}

window.addEventListener("resize", scheduleLayout);
window.visualViewport?.addEventListener("resize", scheduleLayout);
function endDrag(event) { dragging.delete(event.pointerId); scheduleLayout(); }
window.addEventListener("pointerup", endDrag);
window.addEventListener("pointercancel", endDrag);
try { $("receivers-panel").open = localStorage.getItem("bureau-receivers-open") === "true"; } catch {}
$("receivers-panel").addEventListener("toggle", () => {
  try { localStorage.setItem("bureau-receivers-open", String($("receivers-panel").open)); } catch {}
  scheduleLayout();
});
$("configuration-panel").addEventListener("toggle", scheduleLayout);

function toast(message) {
  $("toast").textContent = message;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { $("toast").hidden = true; }, 8000);
}
async function api(path, body) {
  const response = await fetch(path, body === undefined ? {cache:"no-store"} : {
    method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify(body)
  });
  if (response.status === 401) {
    authenticated = false;
    $("login").hidden = false;
    $("dashboard").hidden = true;
    $("connection").textContent = "Connexion requise";
    $("connection").classList.remove("online");
    throw new Error(path === "/api/login" ? "Code incorrect." : "Entre ton code d’accès.");
  }
  const text = await response.text();
  let data;
  try { data = JSON.parse(text); } catch { data = {}; }
  if (!response.ok) throw new Error(data.error || text || "Le Pi ne répond pas.");
  return data;
}
function progress(slider) {
  if (slider.id === "master-gain") $("gain-knob").style.setProperty("--rotation", `${-120 + Number(slider.value) / Math.max(.1, Number(slider.max)) * 240}deg`);
}
function isBusy(key, slider) {
  const write = writes.get(key);
  return document.activeElement === slider || !!(write && (write.pending || write.running));
}
function range(slider, output, value, key, suffix=" %") {
  if (!isBusy(key, slider)) slider.value = value ?? 0;
  progress(slider);
  output.textContent = value === null ? "—" : (suffix === " dB" ? `+${Number(slider.value).toFixed(1)}` : slider.value) + suffix;
  slider.setAttribute("aria-valuetext", output.textContent);
}
function sendQueued(key, path, body) {
  let write = writes.get(key);
  if (!write) { write = {pending:null, running:false, timer:null}; writes.set(key, write); }
  write.pending = {...(write.pending || {}), ...body};
  clearTimeout(write.timer);
  write.timer = setTimeout(() => flush(key, path), 120);
}
async function flush(key, path) {
  const write = writes.get(key);
  if (!write || write.running || !write.pending) return;
  const body = write.pending;
  write.pending = null;
  write.running = true;
  try { state = (await api(path, body)).state; }
  catch (error) { toast(error.message); }
  finally {
    write.running = false;
    if (write.pending) await flush(key, path);
    else if (state && authenticated) render(state);
  }
}
function sliderEvents(slider, output, key, path, getBody, suffix=" %") {
  slider.addEventListener("pointerdown", event => { dragging.add(event.pointerId); });
  slider.addEventListener("input", () => {
    progress(slider);
    output.textContent = suffix === " dB" ? `+${Number(slider.value).toFixed(1)} dB` : `${slider.value} %`;
    slider.setAttribute("aria-valuetext", output.textContent);
    sendQueued(key, path, getBody(Number(slider.value)));
  });
  slider.addEventListener("change", () => flush(key, path));
  slider.addEventListener("blur", () => { if (state && authenticated) render(state); });
}
async function immediate(button, path, body, key, onSuccess = () => {}) {
  if (actions.has(key)) return;
  actions.add(key);
  button.disabled = true;
  if (state) render(state);
  try { state = (await api(path, body)).state; onSuccess(); }
  catch (error) { toast(error.message); }
  finally { actions.delete(key); if (state && authenticated) render(state); }
}
function makeCard(device) {
  const card = $("device-template").content.firstElementChild.cloneNode(true);
  const slider = card.querySelector("input"), label = card.querySelector("label"), output = card.querySelector("output");
  slider.id = `volume-${device.address.replaceAll(":", "")}`;
  slider.setAttribute("aria-label", `Volume de ${device.name}`);
  label.htmlFor = slider.id;
  output.htmlFor = slider.id;
  sliderEvents(slider, output, device.address, "/api/device", volume => ({address:device.address, volume}));
  card.querySelector(".device-mute").addEventListener("click", () => {
    const current = state.devices.find(d => d.address === device.address);
    sendQueued(device.address, "/api/device", {address:device.address, muted:!current.muted});
    flush(device.address, "/api/device");
  });
  card.querySelector(".device-connect").addEventListener("click", event => {
    const current = state.devices.find(d => d.address === device.address);
    immediate(event.target, "/api/device", {address:device.address, action:current.connected ? "disconnect" : "connect"}, device.address);
  });
  cards.set(device.address, card);
  return card;
}
function makeReceiver(adapter) {
  const card = $("receiver-template").content.firstElementChild.cloneNode(true);
  card.classList.toggle("usb", adapter.short_label === "USB");
  card.querySelector(".pair-button").addEventListener("click", event => {
    const current = state.adapters.find(a => a.address === adapter.address);
    immediate(event.target, "/api/pairing", {adapter:adapter.address, enabled:current.pairing_seconds === 0}, adapter.address);
  });
  card.querySelector(".radio-button").addEventListener("click", event =>
    immediate(event.target, "/api/radio", {adapter:adapter.address}, adapter.address));
  const input = card.querySelector(".receiver-alias-input");
  card.aliasEditor = {dirty:false};
  input.addEventListener("input", () => { card.aliasEditor.dirty = true; });
  card.querySelector(".receiver-rename").addEventListener("submit", event => {
    event.preventDefault();
    immediate(card.querySelector(".receiver-alias-save"), "/api/configuration", {
      adapter:{address:adapter.address, alias:input.value.trim()}
    }, adapter.address, () => { card.aliasEditor.dirty = false; });
  });
  receivers.set(adapter.address, card);
  return card;
}
function fillOptions(select, items, selected) {
  select.replaceChildren(...items.map(item => {
    const option = document.createElement("option");
    option.value = String(item.value);
    option.textContent = item.label;
    return option;
  }));
  if (items.some(item => String(item.value) === String(selected))) select.value = String(selected);
}
function outputChoices() { return state?.configuration?.output_choices || []; }
function selectedOutput() {
  return outputChoices().find(choice => String(choice.card) === $("output-card").value && choice.pcm === $("output-pcm").value);
}
function populateOutput(reset) {
  const choices = outputChoices(), saved = state.configuration?.output || {};
  const card = reset ? saved.card : $("output-card").value;
  const pcm = reset ? saved.pcm : $("output-pcm").value;
  const control = reset ? saved.control : $("output-control").value;
  const cards = [...new Map(choices.map(choice => [String(choice.card), choice])).values()];
  fillOptions($("output-card"), cards.map(choice => ({value:choice.card, label:choice.name || String(choice.card)})), card);
  const matches = choices.filter(choice => String(choice.card) === $("output-card").value);
  fillOptions($("output-pcm"), matches.map(choice => ({value:choice.pcm, label:choice.label || `${choice.name || choice.card} · sortie ${choice.device ?? 0}`})), pcm);
  const controls = selectedOutput()?.controls || [];
  fillOptions($("output-control"), controls.length ? controls.map(item => ({value:item.name, label:`${item.name}${item.software ? " · logiciel" : ""}`})) : [{value:"", label:"Aucun contrôle disponible"}], control);
  if (reset) $("output-name").value = saved.label || "";
  renderOutputCapabilities();
}
function renderOutputCapabilities() {
  const choice = selectedOutput(), control = choice?.controls?.find(item => item.name === $("output-control").value);
  const volumeType = control?.software ? "Volume logiciel" : "Volume matériel";
  $("output-capabilities").textContent = !choice ? "Aucune sortie audio détectée." : !control
    ? "Aucun contrôle de volume utilisable sur cette route. Choisis une autre sortie."
    : Number(control.gain_max) > 0 ? `${volumeType} et gain jusqu’à +${Number(control.gain_max).toLocaleString("fr-FR")} dB.`
    : `${volumeType} disponible, sans gain positif.`;
}
function renderConfiguration(data) {
  const config = data.configuration || {output:{}, output_choices:[], diagnostics:[], configured:false};
  const fingerprint = JSON.stringify(config.output_choices);
  const savedFingerprint = JSON.stringify(config.output);
  if (fingerprint !== outputFingerprint || (!outputDirty && savedFingerprint !== savedOutputFingerprint)) populateOutput(!outputDirty);
  outputFingerprint = fingerprint;
  savedOutputFingerprint = savedFingerprint;
  const pending = actions.has("configuration"), detected = !!selectedOutput();
  const controlAvailable = selectedOutput()?.controls?.some(item => item.name === $("output-control").value);
  $("output-form").querySelectorAll("input,select").forEach(control => { control.disabled = pending || !detected; });
  $("output-control").disabled = pending || !detected || !selectedOutput()?.controls?.length;
  $("output-save").disabled = pending || !detected || !controlAvailable;
  $("output-save").textContent = pending ? "Application…" : "Appliquer la sortie";
  $("configuration-status").textContent = config.configured ? "Sortie configurée" : "À configurer";
  const audioStatus = config.audio_status || data.audio_status;
  $("output-audio-status").textContent = typeof audioStatus === "string" ? audioStatus : (audioStatus?.message || "");
  $("configuration-diagnostics").replaceChildren(...(config.diagnostics || []).map(item => {
    const li = document.createElement("li");
    li.textContent = typeof item === "string" ? item : (item.message || String(item));
    return li;
  }));
  if (!config.configured && !openedConfiguration) { $("configuration-panel").open = true; openedConfiguration = true; }
  const destination = config.output?.label || "SORTIE AUDIO";
  $("destination-name").textContent = destination;
  $("destination-name").title = destination;
  $("master-destination-name").textContent = destination;
  $("master-destination-name").title = destination;
}
function makeRouteRow(device, retired) {
  const element = $("route-template").content.firstElementChild.cloneNode(true);
  const select = element.querySelector("select");
  const row = {element, select, dirty:false, retired};
  select.addEventListener("change", () => { row.dirty = true; render(state); });
  element.querySelector(".source-apply").addEventListener("click", event => {
    immediate(event.target, "/api/source", {address:device.address, action:retired ? "restore" : "assign", adapter:select.value}, device.address);
  });
  element.querySelector(".source-retire").addEventListener("click", event => {
    const write = writes.get(device.address);
    if (write) { clearTimeout(write.timer); write.pending = null; }
    immediate(event.target, "/api/source", {address:device.address, action:"retire"}, device.address);
  });
  return row;
}
function renderRoutes(data) {
  const groups = [[data.devices, false, $("source-routes")], [data.retired_devices || [], true, $("retired-routes")]];
  const keys = new Set(groups.flatMap(([devices, retired]) => devices.map(d => `${retired}:${d.address}`)));
  for (const [key, row] of routeRows) if (!keys.has(key)) { row.element.remove(); routeRows.delete(key); }
  for (const [devices, retired, container] of groups) for (const device of devices) {
    const key = `${retired}:${device.address}`;
    let row = routeRows.get(key);
    if (!row) { row = makeRouteRow(device, retired); routeRows.set(key, row); container.append(row.element); }
    const {element, select} = row;
    const fingerprint = data.adapters.map(a => `${a.address}:${a.short_label}:${a.alias}`).join("|");
    if (select.dataset.receivers !== fingerprint) {
      const selected = row.dirty ? select.value : device.adapter;
      select.replaceChildren(...data.adapters.map(adapter => {
        const option = document.createElement("option");
        option.value = adapter.address;
        option.textContent = `${adapter.short_label} · ${adapter.alias}`;
        return option;
      }));
      select.dataset.receivers = fingerprint;
      select.value = selected;
      if (!select.value) row.dirty = false;
    }
    if (!row.dirty) select.value = device.adapter;
    if (select.value === device.adapter) row.dirty = false;
    select.setAttribute("aria-label", `Récepteur de ${device.name}`);
    const busy = actions.has(device.address) || device.connecting;
    select.disabled = !!busy;
    element.querySelector(".source-name").textContent = device.name;
    element.querySelector(".source-address").textContent = device.address;
    element.querySelector(".source-route-state").textContent = retired
      ? (device.connected ? "Déconnexion en attente…" : "Retiré de la console")
      : (!device.receiver_present ? "Récepteur absent" : (!device.paired ? "À appairer sur ce récepteur" : (device.connected ? "Connecté" : "Appairé")));
    const apply = element.querySelector(".source-apply"), remove = element.querySelector(".source-retire");
    apply.textContent = retired ? "Rétablir" : "Appliquer";
    apply.disabled = !!busy || !select.value || (!retired && select.value === device.adapter);
    apply.setAttribute("aria-label", `${retired ? "Rétablir" : "Appliquer le récepteur de"} ${device.name}`);
    remove.hidden = retired;
    remove.disabled = !!busy;
    remove.setAttribute("aria-label", `Retirer ${device.name} de la console`);
  }
  $("route-empty").hidden = data.devices.length !== 0;
  const retiredCount = (data.retired_devices || []).length;
  $("retired-panel").hidden = retiredCount === 0;
  $("retired-count").textContent = `(${retiredCount})`;
}
function render(data) {
  authenticated = true;
  $("login").hidden = true;
  $("dashboard").hidden = false;
  $("demo-banner").hidden = !data.demo;
  $("connection").textContent = data.demo ? "Aperçu simulé" : (data.bluetooth ? "En ligne" : "Radios indisponibles");
  $("connection").classList.toggle("online", data.bluetooth);
  $("errors").hidden = data.errors.length === 0;
  $("errors").textContent = data.errors.join("\n");
  const opened = data.adapters.filter(adapter => adapter.pairing_seconds > 0);
  const available = data.adapters.filter(adapter => adapter.present && adapter.powered).length;
  $("receivers-status").textContent = opened.length
    ? `Appairage ouvert · ${opened.map(adapter => adapter.short_label).join(" + ")}`
    : `${available} / ${data.adapters.length} radios actives`;
  renderRoutes(data);
  renderConfiguration(data);
  const gainMax = Math.max(0, Number(data.master.gain_max ?? data.gain_max ?? data.configuration?.output?.gain_max ?? 0));
  $("master-gain").max = String(gainMax);
  $("master-gain-max").textContent = `+${gainMax.toLocaleString("fr-FR")} dB`;
  $("master-gain-control").hidden = gainMax === 0;
  $("master-no-gain").hidden = gainMax > 0;
  $("gain-help").textContent = gainMax > 0
    ? `Le gain général va jusqu’à +${gainMax.toLocaleString("fr-FR")} dB. Si le son sature, réduis le gain ou les volumes des sources.`
    : "Cette sortie ne propose pas de gain positif. Les volumes des sources et le volume général restent indépendants.";
  range($("master-volume"), $("master-volume-value"), data.master.volume, "master");
  range($("master-gain"), $("master-gain-value"), data.master.gain, "master", " dB");
  $("master-volume").disabled = $("master-mute").disabled = !data.master_ready;
  $("master-gain").disabled = !data.master_ready || gainMax === 0;
  $("master-mute").classList.toggle("is-muted", data.master.muted);
  $("master-mute").setAttribute("aria-label", data.master.muted ? "Rétablir le son général" : "Couper le son général");
  $("master-mute").setAttribute("aria-pressed", String(data.master.muted));
  $("master-status").textContent = !data.master_ready ? "Sortie indisponible" : (data.master.muted ? "Son coupé" : (data.master.volume === 0 ? "Volume à zéro" : "Sortie prête"));
  $("master-led").classList.toggle("active", data.master_ready && !data.master.muted && data.master.volume > 0 && data.devices.some(d => d.playing));
  const adapterAddresses = new Set(data.adapters.map(a => a.address));
  for (const [address, card] of receivers) if (!adapterAddresses.has(address)) { card.remove(); receivers.delete(address); }
  for (const adapter of data.adapters) {
    const card = receivers.get(adapter.address) || makeReceiver(adapter);
    if (!card.parentNode) $("receivers").append(card);
    card.querySelector(".receiver-badge").textContent = adapter.short_label;
    card.querySelector(".receiver-name").textContent = adapter.label;
    card.querySelector(".receiver-sources").textContent = data.devices.filter(d => d.adapter === adapter.address).map(d => d.name).join(" + ") || "Prêt pour un appareil";
    card.querySelector(".receiver-alias").textContent = adapter.alias;
    const aliasInput = card.querySelector(".receiver-alias-input"), aliasSave = card.querySelector(".receiver-alias-save");
    if (!card.aliasEditor.dirty) aliasInput.value = adapter.alias;
    aliasInput.disabled = aliasSave.disabled = actions.has(adapter.address);
    aliasInput.setAttribute("aria-label", `Nom Bluetooth du récepteur ${adapter.label}`);
    aliasSave.setAttribute("aria-label", `Renommer le récepteur ${adapter.label}`);
    const status = card.querySelector(".receiver-status");
    status.textContent = !adapter.present ? "Absent" : (adapter.powered ? "Allumé" : (adapter.power_state === "off-blocked" ? "Bloqué" : "Éteint"));
    status.classList.toggle("offline", !adapter.powered);
    const button = card.querySelector(".pair-button"), radio = card.querySelector(".radio-button");
    button.textContent = adapter.pairing_seconds ? "Fermer" : "Appairer";
    button.setAttribute("aria-label", `${adapter.pairing_seconds ? "Fermer" : "Ouvrir"} l’appairage sur ${adapter.label}`);
    button.disabled = !adapter.present || !adapter.powered || actions.has(adapter.address);
    radio.hidden = !adapter.present || adapter.powered;
    radio.disabled = actions.has(adapter.address);
    radio.setAttribute("aria-label", `Activer ${adapter.label}`);
    const countdown = card.querySelector(".pairing-countdown"), seconds = adapter.pairing_seconds;
    countdown.textContent = seconds ? `Choisis ${adapter.alias} · ${Math.floor(seconds/60)}:${String(seconds%60).padStart(2,"0")} restantes` : "Appairage fermé · appareils connus autorisés";
    countdown.classList.toggle("active", seconds > 0);
  }
  const addresses = new Set(data.devices.map(d => d.address));
  for (const [address, card] of cards) if (!addresses.has(address)) { card.remove(); cards.delete(address); }
  data.devices.forEach((device, index) => {
    const card = cards.get(device.address) || makeCard(device);
    // Ne pas déplacer un fader actif : le navigateur doit conserver le focus.
    if (!card.parentNode) $("devices").append(card);
    card.querySelector(".channel-number").textContent = String(index + 1).padStart(2,"0");
    card.querySelector(".route-label").textContent = device.receiver;
    card.querySelector(".device-name").textContent = device.name;
    card.querySelector(".device-name").title = device.remote_name;
    const receiver = data.adapters.find(a => a.address === device.adapter);
    card.querySelector(".device-subtitle").textContent = receiver?.alias || "Récepteur affecté";
    card.querySelector(".device-route-summary").textContent = `RÉCEPTEUR ${device.receiver}`;
    const pending = device.connecting || actions.has(device.address);
    const status = card.querySelector(".device-status");
    status.textContent = pending ? "Connexion en cours…" : (!device.receiver_present ? "Récepteur absent" : (!device.paired ? "À appairer" : (device.connected ? (device.playing ? "Flux actif" : "Connecté") : "Déconnecté")));
    status.title = status.textContent;
    card.querySelector(".signal-led").classList.toggle("active", device.playing);
    const sample = device.sampling ? `${(device.sampling / 1000).toLocaleString("fr-FR", {maximumFractionDigits:1})} kHz` : "";
    card.querySelector(".device-format").textContent = device.codec ? `${device.codec}${sample ? " / " + sample : ""}` : "EN ATTENTE";
    card.querySelector(".device-detail").textContent = device.codec ? (device.channels === 2 ? "Deux canaux · L / R" : `${device.channels} canal`) : (!device.paired ? `Appairer sur ${device.receiver}` : (device.connected ? "Lance une lecture" : "Prêt à se reconnecter"));
    card.querySelector(".device-detail").title = card.querySelector(".device-detail").textContent;
    card.querySelector(".device-format").title = card.querySelector(".device-format").textContent;
    const slider = card.querySelector("input"), mute = card.querySelector(".device-mute"), connect = card.querySelector(".device-connect");
    slider.setAttribute("aria-label", `Volume de ${device.name}`);
    card.querySelector("label").textContent = `Volume de ${device.name}`;
    range(slider, card.querySelector("output"), device.volume, device.address);
    slider.disabled = mute.disabled = device.volume === null;
    mute.classList.toggle("is-muted", device.muted);
    mute.setAttribute("aria-pressed", String(device.muted));
    mute.setAttribute("aria-label", `${device.muted ? "Rétablir" : "Couper"} le son de ${device.name}`);
    connect.textContent = pending ? "Connexion…" : (device.connected ? "Déconnecter" : "Reconnecter");
    connect.disabled = pending || !device.receiver_present || !device.paired || !receiver?.powered;
    connect.setAttribute("aria-label", `${device.connected ? "Déconnecter" : "Reconnecter"} ${device.name}`);
  });
  const count = data.devices.filter(d => d.connected).length;
  $("device-count").textContent = `${count} / ${data.devices.length} sources connectées`;
  $("empty-state").hidden = data.devices.length !== 0;
  scheduleLayout();
}
$("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.target.querySelector("button");
  button.disabled = true;
  try { await api("/api/login", {key:$("access-key").value}); $("login-error").textContent = ""; await poll(); }
  catch (error) { $("login-error").textContent = error.message; }
  finally { button.disabled = false; }
});
$("output-card").addEventListener("change", () => { outputDirty = true; populateOutput(false); renderConfiguration(state); });
$("output-pcm").addEventListener("change", () => { outputDirty = true; populateOutput(false); renderConfiguration(state); });
$("output-control").addEventListener("change", () => { outputDirty = true; renderOutputCapabilities(); renderConfiguration(state); });
$("output-name").addEventListener("input", () => { outputDirty = true; });
$("output-form").addEventListener("submit", event => {
  event.preventDefault();
  immediate($("output-save"), "/api/configuration", {output:{
    card:$("output-card").value, control:$("output-control").value,
    pcm:$("output-pcm").value, label:$("output-name").value.trim(), mode:selectedOutput()?.mode || "managed"
  }}, "configuration", () => { outputDirty = false; outputFingerprint = ""; });
});
sliderEvents($("master-volume"), $("master-volume-value"), "master", "/api/master", volume => ({volume}));
sliderEvents($("master-gain"), $("master-gain-value"), "master", "/api/master", gain => ({gain}), " dB");
$("master-mute").addEventListener("click", () => {
  sendQueued("master", "/api/master", {muted:!state.master.muted});
  flush("master", "/api/master");
});
async function poll() {
  if (polling) return;
  polling = true;
  try { state = await api("/api/status"); render(state); }
  catch (error) {
    if (authenticated) {
      $("connection").textContent = "Pi injoignable";
      $("connection").classList.remove("online");
      $("errors").hidden = false;
      $("errors").textContent = "Connexion au Pi perdue. Nouvelle tentative automatique…";
      $("dashboard").querySelectorAll("button,input,select").forEach(control => { control.disabled = true; });
    } else if ($("login").hidden) toast(error.message);
  } finally { polling = false; }
}
poll();
setInterval(() => { if (authenticated) poll(); }, 2000);
