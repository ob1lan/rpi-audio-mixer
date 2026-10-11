"use strict";
const $ = id => document.getElementById(id);
let state = null, toastTimer = null, polling = false, authenticated = false;
const cards = new Map(), receivers = new Map(), writes = new Map(), actions = new Set();
const routeRows = new Map();
const dragging = new Set();
const activeSliders = new Map();
let layoutFrame = null, outputDirty = false, outputFingerprint = "", savedOutputFingerprint = "", openedConfiguration = false;
let channelBank = 0, bankCapacity = 1;

function scheduleLayout() {
  if (layoutFrame !== null) return;
  layoutFrame = requestAnimationFrame(() => {
    layoutFrame = null;
    if ($("dashboard").hidden || dragging.size) return;
    const desk = document.querySelector(".console"), deskStyle = getComputedStyle(desk);
    const viewport = window.visualViewport;
    const availableHeight = viewport && Math.abs(viewport.scale - 1) < .01 ? viewport.height : window.innerHeight;
    const sourceCards = [...$("devices").children];
    const activeIndex = sourceCards.findIndex(card => card.contains(document.activeElement));
    const navigation = $("channel-navigation");
    // Bank by width, preserving the console's vertical faders on phones too.
    const availableWidth = document.querySelector(".desk-toolbar").clientWidth;
    const rails = parseFloat(deskStyle.borderLeftWidth) + parseFloat(deskStyle.borderRightWidth);
    const masterWidth = parseFloat(deskStyle.getPropertyValue("--master-width"));
    const sourceWidth = parseFloat(deskStyle.getPropertyValue("--strip-min-width"));
    bankCapacity = Math.max(1, Math.min(8, Math.floor((availableWidth - rails - 10 - masterWidth) / sourceWidth)));
    navigation.hidden = sourceCards.length <= bankCapacity;
    const bankCount = Math.max(1, Math.ceil(sourceCards.length / bankCapacity));
    channelBank = Math.max(0, Math.min(channelBank, bankCount - 1));
    if (activeIndex >= 0) channelBank = Math.floor(activeIndex / bankCapacity);
    const start = channelBank * bankCapacity;
    sourceCards.forEach((card, index) => { card.hidden = index < start || index >= start + bankCapacity; });
    desk.style.setProperty("--source-columns", String(Math.min(sourceCards.length - start, bankCapacity)));
    desk.classList.toggle("no-sources", sourceCards.length === 0);
    const current = $("master-volume").getBoundingClientRect().height;
    const fitted = Math.floor(availableHeight - document.querySelector("main").getBoundingClientRect().height + current - 8);
    const minimum = parseFloat(getComputedStyle(document.documentElement).getPropertyValue("--min-fader-height"));
    document.documentElement.style.setProperty("--fader-height", `${Math.max(minimum, Math.min(260, fitted))}px`);
    $("bank-prev").disabled = channelBank === 0;
    $("bank-next").disabled = channelBank === bankCount - 1;
    $("channel-page").textContent = sourceCards.length ? `Sources ${start + 1}–${Math.min(start + bankCapacity, sourceCards.length)} / ${sourceCards.length} · Banque ${channelBank + 1}/${bankCount}` : "Aucune source";
  });
}

window.addEventListener("resize", scheduleLayout);
window.visualViewport?.addEventListener("resize", scheduleLayout);
function endDrag(event) { dragging.delete(event.pointerId); scheduleLayout(); }
window.addEventListener("pointerup", endDrag);
window.addEventListener("pointercancel", endDrag);
$("bank-prev").addEventListener("click", () => { channelBank--; scheduleLayout(); });
$("bank-next").addEventListener("click", () => { channelBank++; scheduleLayout(); });
for (const [panelId, buttonId] of [["receivers-panel", "open-receivers"], ["configuration-panel", "open-configuration"], ["equalizer-panel", "open-equalizer"]]) {
  const panel = $(panelId);
  $(buttonId).addEventListener("click", () => panel.showModal());
  panel.querySelector(".dialog-close").addEventListener("click", () => panel.close());
  panel.addEventListener("click", event => { if (event.target === panel) {
    const rect = panel.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) panel.close();
  } });
}
window.addEventListener("keydown", event => {
  if (event.key === "Escape") dragging.clear();
});

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
    for (const panel of document.querySelectorAll(".settings-panel[open]")) panel.close();
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
  if (slider.classList.contains("fader")) {
    const minimum = Number(slider.min), span = Number(slider.max) - minimum;
    slider.closest(".fader-well").style.setProperty("--level", String(span > 0 ? (Number(slider.value) - minimum) / span : 0));
  }
  if (slider.id === "master-gain") $("gain-knob").style.setProperty("--rotation", `${-135 + Number(slider.value) / Math.max(.1, Number(slider.max)) * 270}deg`);
  else {
    const card = slider.closest(".channel-strip");
    if (slider.classList.contains("device-gain")) {
      card.querySelector(".source-knob").style.setProperty("--rotation", `${-135 + Number(slider.value) / Math.max(.1, Number(slider.max)) * 270}deg`);
    }
    if (slider.id === "master-volume" || slider.id === "master-volume-right") {
      const other = $(slider.id === "master-volume" ? "master-volume-right" : "master-volume");
      other.value = slider.value;
      other.setAttribute("aria-valuetext", `${slider.value} %`);
      other.closest(".fader-well").style.setProperty("--level", String(Number(slider.value) / 100));
    }
  }
}
function isBusy(key, slider) {
  const write = writes.get(key);
  const activeControl = document.activeElement;
  const masterFaders = [$("master-volume"), $("master-volume-right")];
  return activeSliders.has(slider) || activeControl === slider || (key === "master" && masterFaders.some(fader => fader === activeControl || activeSliders.has(fader))) || !!(write && (write.pending || write.running));
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
  slider.addEventListener("pointerdown", event => { if (!slider.disabled) dragging.add(event.pointerId); });
  slider.addEventListener("input", () => {
    progress(slider);
    output.textContent = suffix === " dB" ? `+${Number(slider.value).toFixed(1)} dB` : `${slider.value} %`;
    slider.setAttribute("aria-valuetext", output.textContent);
    updateGainButtons(slider);
    sendQueued(key, path, getBody(Number(slider.value)));
  });
  slider.addEventListener("change", () => flush(key, path));
  slider.addEventListener("blur", () => { if (state && authenticated) render(state); });
}
function setSliderValue(slider, value) {
  const minimum = Number(slider.min), maximum = Number(slider.max), step = Number(slider.step) || 1;
  const rounded = minimum + Math.round((value - minimum) / step) * step;
  const next = String(Number(Math.max(minimum, Math.min(maximum, rounded)).toFixed(6)));
  if (slider.value === next) return;
  slider.value = next;
  slider.dispatchEvent(new Event("input", {bubbles:true}));
}
function pointerDrag(slider, valueFromPointer) {
  slider.addEventListener("pointerdown", event => {
    const masterFaders = [$("master-volume"), $("master-volume-right")];
    const competingMaster = masterFaders.includes(slider) && masterFaders.some(fader => activeSliders.has(fader));
    if (slider.disabled || event.button !== 0) return;
    if (activeSliders.has(slider) || competingMaster) { event.preventDefault(); return; }
    event.preventDefault();
    slider.focus({preventScroll:true});
    const start = {value:Number(slider.value), x:event.clientX, y:event.clientY,
      span:Number(slider.max) - Number(slider.min), touch:event.pointerType !== "mouse"};
    activeSliders.set(slider, event.pointerId);
    dragging.add(event.pointerId);
    const move = pointer => {
      if (pointer.pointerId !== event.pointerId || slider.disabled) return;
      pointer.preventDefault();
      setSliderValue(slider, valueFromPointer(start, pointer));
    };
    const end = pointer => {
      if (pointer.pointerId !== event.pointerId) return;
      slider.removeEventListener("pointermove", move);
      slider.removeEventListener("pointerup", end);
      slider.removeEventListener("pointercancel", end);
      slider.removeEventListener("lostpointercapture", end);
      activeSliders.delete(slider);
      dragging.delete(event.pointerId);
      if (slider.hasPointerCapture(event.pointerId)) slider.releasePointerCapture(event.pointerId);
      slider.dispatchEvent(new Event("change", {bubbles:true}));
      scheduleLayout();
    };
    slider.addEventListener("pointermove", move);
    slider.addEventListener("pointerup", end);
    slider.addEventListener("pointercancel", end);
    slider.addEventListener("lostpointercapture", end);
    slider.setPointerCapture(event.pointerId);
  });
}
function rotaryEvents(slider) {
  // A straight vertical gesture is easier to keep precise with a fingertip.
  pointerDrag(slider, (start, pointer) => start.value + (start.y - pointer.clientY + (start.touch ? 0 : (pointer.clientX - start.x) * .5)) * start.span / (start.touch ? 240 : 150));
  const container = slider.closest(".gain-control");
  for (const button of container.querySelectorAll("[data-gain-step]")) {
    button.addEventListener("click", () => {
      if (slider.disabled || activeSliders.has(slider)) return;
      setSliderValue(slider, Number(slider.value) + Number(button.dataset.gainStep) * (Number(slider.step) || .1));
      slider.dispatchEvent(new Event("change", {bubbles:true}));
    });
  }
}
function updateGainButtons(slider) {
  const container = slider.closest(".gain-control");
  if (!container) return;
  for (const button of container.querySelectorAll("[data-gain-step]")) {
    button.disabled = slider.disabled || (Number(button.dataset.gainStep) < 0 ? Number(slider.value) <= Number(slider.min) : Number(slider.value) >= Number(slider.max));
  }
}
function masterFaderEvents(slider) {
  pointerDrag(slider, (start, pointer) => {
    if (start.travel === undefined) {
      const cap = slider.closest(".fader-well").querySelector(".fader-cap");
      start.travel = Math.max(1, slider.getBoundingClientRect().height - cap.getBoundingClientRect().height);
    }
    return start.value + (start.y - pointer.clientY) * start.span / start.travel;
  });
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
  const slider = card.querySelector(".device-volume"), knob = card.querySelector(".device-gain"), label = card.querySelector(".fader-well label"), output = card.querySelector(".level-readout output");
  slider.id = `volume-${device.address.replaceAll(":", "")}`;
  slider.setAttribute("aria-label", `Volume de ${device.name}`);
  label.htmlFor = slider.id;
  output.htmlFor = slider.id;
  sliderEvents(slider, output, device.address, "/api/device", volume => ({address:device.address, volume}));
  knob.id = `gain-${device.address.replaceAll(":", "")}`;
  card.querySelector(".trim-label label").htmlFor = knob.id;
  const gainOutput = card.querySelector(".device-gain-value");
  gainOutput.htmlFor = knob.id;
  sliderEvents(knob, gainOutput, device.address, "/api/device", gain => ({address:device.address, gain}), " dB");
  rotaryEvents(knob);
  card.querySelector(".device-mute").addEventListener("click", () => {
    const current = state.devices.find(d => d.address === device.address);
    sendQueued(device.address, "/api/device", {address:device.address, muted:!current.muted});
    flush(device.address, "/api/device");
  });
  card.querySelector(".device-solo").addEventListener("click", event => {
    if (actions.has(device.address) || writes.get(device.address)?.running) return;
    const current = state.devices.find(d => d.address === device.address);
    sendQueued(device.address, "/api/device", {address:device.address, solo:!current.solo});
    event.currentTarget.disabled = true;
    flush(device.address, "/api/device");
  });
  card.querySelector(".device-connect").addEventListener("click", event => {
    const current = state.devices.find(d => d.address === device.address);
    immediate(event.currentTarget, "/api/device", {address:device.address, action:current.connected ? "disconnect" : "connect"}, device.address);
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
  if (!config.configured && !openedConfiguration) { $("configuration-panel").showModal(); openedConfiguration = true; }
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
  equalizerEditor.receive(data.equalizer);
  const gainMax = Math.max(0, Number(data.master.gain_max ?? data.gain_max ?? data.configuration?.output?.gain_max ?? 0));
  $("master-gain").max = String(gainMax);
  $("master-gain-max").textContent = `+${gainMax.toLocaleString("fr-FR")} dB`;
  $("master-gain-control").hidden = gainMax === 0;
  $("master-no-gain").hidden = gainMax > 0;
  $("gain-help").textContent = gainMax > 0
    ? `Le gain général va jusqu’à +${gainMax.toLocaleString("fr-FR")} dB. Si le son sature, réduis le gain ou les volumes des sources.`
    : "Cette sortie ne propose pas de gain positif. Les volumes des sources et le volume général restent indépendants.";
  if (data.devices.some(device => !device.gain_max)) $("gain-help").textContent += " Pour le gain par source, choisis une sortie gérée dans la configuration audio.";
  range($("master-volume"), $("master-volume-value"), data.master.volume, "master");
  range($("master-volume-right"), $("master-volume-value"), data.master.volume, "master");
  range($("master-gain"), $("master-gain-value"), data.master.gain, "master", " dB");
  $("master-volume").disabled = $("master-volume-right").disabled = $("master-mute").disabled = !data.master_ready;
  $("master-gain").disabled = !data.master_ready || gainMax === 0;
  updateGainButtons($("master-gain"));
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
    card.querySelector(".footer-channel-number").textContent = String(index + 1);
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
    card.querySelector(".strip-indicator").title = status.textContent;
    card.querySelector(".signal-led").classList.toggle("active", device.playing);
    const sample = device.sampling ? `${(device.sampling / 1000).toLocaleString("fr-FR", {maximumFractionDigits:1})} kHz` : "";
    card.querySelector(".device-format").textContent = device.codec ? `${device.codec}${sample ? " / " + sample : ""}` : "EN ATTENTE";
    card.classList.toggle("is-solo-suppressed", Boolean(device.solo_suppressed));
    card.querySelector(".device-detail").textContent = device.solo_suppressed ? "Exclu par SOLO" : (device.codec ? (device.channels === 2 ? "Deux canaux · L / R" : `${device.channels} canal`) : (!device.paired ? `Appairer sur ${device.receiver}` : (device.connected ? "Lance une lecture" : "Prêt à se reconnecter")));
    card.querySelector(".device-detail").title = card.querySelector(".device-detail").textContent;
    card.querySelector(".device-format").title = card.querySelector(".device-format").textContent;
    const slider = card.querySelector(".device-volume"), knob = card.querySelector(".device-gain"), mute = card.querySelector(".device-mute"), solo = card.querySelector(".device-solo"), connect = card.querySelector(".device-connect");
    slider.setAttribute("aria-label", `Volume de ${device.name}`);
    knob.setAttribute("aria-label", `Gain de ${device.name}`);
    knob.max = String(device.gain_max || 0);
    card.querySelector(".source-gain-control").hidden = !device.gain_max;
    card.querySelector(".source-no-gain").hidden = !!device.gain_max;
    card.querySelector(".fader-well label").textContent = `Volume de ${device.name}`;
    range(slider, card.querySelector(".level-readout output"), device.volume, device.address);
    range(knob, card.querySelector(".device-gain-value"), device.gain ?? 0, device.address, " dB");
    slider.disabled = mute.disabled = device.volume === null;
    knob.disabled = !device.gain_ready;
    updateGainButtons(knob);
    mute.classList.toggle("is-muted", device.muted);
    mute.setAttribute("aria-pressed", String(device.muted));
    mute.setAttribute("aria-label", `${device.muted ? "Rétablir" : "Couper"} le son de ${device.name}`);
    solo.disabled = Boolean(actions.has(device.address) || writes.get(device.address)?.running);
    solo.classList.toggle("is-solo", Boolean(device.solo));
    solo.setAttribute("aria-pressed", String(Boolean(device.solo)));
    solo.setAttribute("aria-label", `${device.solo ? "Désactiver" : "Activer"} SOLO pour ${device.name}`);
    solo.title = device.solo ? "Désactiver SOLO et rétablir les autres sources si aucun SOLO ne reste actif" : "Écouter uniquement les sources en SOLO ; les MUTE restent prioritaires";
    card.querySelector(".connect-text").textContent = pending ? "Connexion…" : (device.connected ? "Déconnecter" : "Reconnecter");
    connect.title = card.querySelector(".connect-text").textContent;
    connect.classList.toggle("is-connected", device.connected);
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
sliderEvents($("master-volume-right"), $("master-volume-value"), "master", "/api/master", volume => ({volume}));
masterFaderEvents($("master-volume"));
masterFaderEvents($("master-volume-right"));
sliderEvents($("master-gain"), $("master-gain-value"), "master", "/api/master", gain => ({gain}), " dB");
rotaryEvents($("master-gain"));
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
const equalizerEditor = createEqualizerEditor({
  api,
  onState:data => { state = data; if (authenticated) render(state); },
  getLevels:() => meterData && performance.now() - meterFetched < 600 ? meterData : null
});
poll();
setInterval(() => { if (authenticated) poll(); }, 2000);

let meterData = null, meterFetched = 0, meterPolling = false;
const heldPeak = {left:-100, right:-100}, heldUntil = {left:0, right:0}, clipUntil = {left:0, right:0};
async function pollLevels() {
  if (!authenticated || document.hidden || meterPolling) return;
  meterPolling = true;
  try { meterData = await api("/api/levels"); meterFetched = performance.now(); }
  catch { meterData = null; }
  finally { meterPolling = false; }
}
function paintMeter() {
  const now = performance.now(), fresh = Boolean(meterData?.available && now - meterFetched < 600);
  const db = {};
  for (const channel of ["left", "right"]) {
    const amplitude = fresh ? Math.max(0, Math.min(1, Number(meterData[channel]) || 0)) : 0;
    db[channel] = amplitude > 0 ? 20 * Math.log10(amplitude) : -100;
    if (fresh && meterData[`clip_${channel}`]) clipUntil[channel] = now + 1000;
    if (!fresh) { heldPeak[channel] = -100; clipUntil[channel] = 0; }
    else if (db[channel] >= heldPeak[channel] || now > heldUntil[channel]) {
      heldPeak[channel] = db[channel]; heldUntil[channel] = now + 650;
    }
  }
  for (const led of $("stereo-meter").querySelectorAll(".meter-led")) {
    const channel = led.dataset.channel, threshold = Number(led.dataset.db);
    const hold = heldPeak[channel] >= threshold && heldPeak[channel] < threshold + (threshold < -18 ? 6 : 3);
    led.classList.toggle("lit", threshold === 0 ? fresh && now < clipUntil[channel] : fresh && (db[channel] >= threshold || hold));
  }
  $("meter-status").textContent = fresh ? (meterData.demo ? "SIMULÉ" : "MAIN MIX") : "INACTIF";
  $("stereo-meter").setAttribute("aria-label", fresh ? `Niveaux de sortie ${meterData.demo ? "simulés " : ""}: gauche ${db.left > -90 ? db.left.toFixed(0) + " dBFS" : "silence"}, droite ${db.right > -90 ? db.right.toFixed(0) + " dBFS" : "silence"}${now < clipUntil.left || now < clipUntil.right ? ", écrêtage" : ""}` : "Mesure des niveaux de sortie indisponible");
  $("meter-note").textContent = fresh ? (meterData.demo ? "NIVEAUX SIMULÉS · APERÇU" : "MAIN MIX · CRÊTES NUMÉRIQUES L / R") : (meterData?.message || "MAIN MIX · MESURE INACTIVE");
  $("meter-note").title = meterData?.message || "";
}
setInterval(pollLevels, 100);
setInterval(paintMeter, 50);
document.addEventListener("visibilitychange", () => { if (!document.hidden) pollLevels(); });
