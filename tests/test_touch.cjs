"use strict";
// Exercise the production gesture handlers without controlling a browser.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const {test} = require("node:test");

const source = fs.readFileSync(path.join(__dirname, "../static/app.js"), "utf8");
const handlers = source.slice(source.indexOf("function progress("), source.indexOf("async function immediate("));

class Target extends EventTarget {
  constructor() { super(); this.style = new Map(); this.style.setProperty = this.style.set; this.attributes = new Map(); }
  setAttribute(name, value) { this.attributes.set(name, value); }
}
class Button extends Target {
  constructor(step) { super(); this.dataset = {gainStep:String(step)}; this.disabled = false; }
}
class Slider extends Target {
  constructor(id, classes, min, max, step, value, document) {
    super(); Object.assign(this, {id, min:String(min), max:String(max), step:String(step), disabled:false, document});
    this.value = value;
    this.classList = {contains:name => classes.includes(name)};
    this.captured = new Set();
    this.well = new Target();
    this.well.querySelector = () => ({getBoundingClientRect:() => ({height:46})});
    this.buttons = [new Button(-1), new Button(1)];
    this.gainControl = {querySelectorAll:() => this.buttons};
    this.knob = new Target();
    this.card = {querySelector:() => this.knob};
  }
  get value() { return this.number; }
  set value(value) { this.number = String(value); }
  focus() { if (this.document.focusWorks) this.document.activeElement = this; }
  closest(selector) { return {".fader-well":this.well, ".gain-control":this.classList.contains("fader") ? null : this.gainControl, ".channel-strip":this.card}[selector]; }
  getBoundingClientRect() { return {height:246}; }
  setPointerCapture(id) { this.captured.add(id); }
  hasPointerCapture(id) { return this.captured.has(id); }
  releasePointerCapture(id) { this.captured.delete(id); }
}
function pointer(target, type, {id=1, x=100, y=200, kind="touch", button=0} = {}) {
  const event = new Event(type, {cancelable:true, bubbles:true});
  Object.assign(event, {pointerId:id, clientX:x, clientY:y, pointerType:kind, button});
  target.dispatchEvent(event);
  return event;
}
function fixture() {
  const document = {activeElement:null, focusWorks:true};
  const gain = new Slider("gain-source", ["device-gain"], 0, 4, .1, 1, document);
  const masterGain = new Slider("master-gain", [], 0, 4, .1, 1, document);
  const left = new Slider("master-volume", ["fader"], 0, 100, 1, 50, document);
  const right = new Slider("master-volume-right", ["fader"], 0, 100, 1, 50, document);
  const volume = new Slider("volume-source", ["fader"], 0, 100, 1, 50, document);
  const elements = new Map([left, right, masterGain].map(element => [element.id, element]));
  elements.set("gain-knob", new Target());
  const activeSliders = new Map(), dragging = new Set(), writes = new Map(), requests = [], timers = new Map();
  let timerId = 0;
  const context = vm.createContext({
    document, Event, activeSliders, dragging, writes, state:null, authenticated:false,
    $:id => elements.get(id),
    setTimeout:callback => { timers.set(++timerId, callback); return timerId; },
    clearTimeout:id => timers.delete(id), scheduleLayout:() => {}, render:() => {},
    toast:message => { throw new Error(message); },
    api:async (path, body) => { requests.push({path, body:JSON.parse(JSON.stringify(body))}); return {state:null}; }
  });
  vm.runInContext(handlers + "\nglobalThis.controls = {sliderEvents, rotaryEvents, masterFaderEvents, updateGainButtons, isBusy, range, flush};", context);
  const controls = context.controls;
  const output = {textContent:""}, masterOutput = {textContent:""};
  for (const [slider, readout, key, route, property] of [
    [gain, output, "source", "/api/device", "gain"],
    [volume, {textContent:""}, "source", "/api/device", "volume"],
    [masterGain, {textContent:""}, "master", "/api/master", "gain"],
    [left, masterOutput, "master", "/api/master", "volume"],
    [right, masterOutput, "master", "/api/master", "volume"]
  ]) {
    controls.sliderEvents(slider, readout, key, route, value => key === "source" ? {address:"AA:BB:CC:DD:EE:FF", [property]:value} : {[property]:value}, property === "gain" ? " dB" : " %");
  }
  for (const slider of [gain, masterGain]) { controls.rotaryEvents(slider); controls.updateGainButtons(slider); }
  for (const slider of [left, right]) controls.masterFaderEvents(slider);
  return {gain, masterGain, left, right, volume, document, controls, activeSliders, dragging, writes, requests, output, masterOutput,
    settle:async () => { for (let i=0; i<8; i++) await Promise.resolve(); }};
}

test("live negative gain readout and rotary position use both range limits", () => {
  const f=fixture();f.gain.min="-24";f.gain.max="24";
  f.controls.range(f.gain,f.output,-12,"source"," dB");
  assert.equal(f.output.textContent,"-12.0 dB");
  assert.equal(f.gain.knob.style.get("--rotation"),"-67.5deg");
});

test("gain touch starts at its current value and ignores horizontal finger drift", async () => {
  const f = fixture();
  assert.equal(pointer(f.gain, "pointerdown").defaultPrevented, true);
  assert.equal(f.gain.value, "1");
  assert.equal(f.requests.length, 0);
  pointer(f.gain, "pointermove", {x:450, y:200});
  assert.equal(f.gain.value, "1");
  pointer(f.gain, "pointermove", {x:450, y:140});
  assert.equal(f.gain.value, "2");
  assert.equal(f.output.textContent, "+2.0 dB");
  pointer(f.gain, "pointerup", {x:450, y:140});
  await f.settle();
  assert.deepEqual(f.requests, [{path:"/api/device", body:{address:"AA:BB:CC:DD:EE:FF", gain:2}}]);
  assert.equal(f.activeSliders.size, 0);
  assert.equal(f.dragging.size, 0);
  assert.equal(f.gain.hasPointerCapture(1), false);
});

test("gain tracks its captured finger outside the control and clamps to supported bounds", () => {
  const f = fixture();
  pointer(f.gain, "pointerdown");
  pointer(f.gain, "pointermove", {y:-1000});
  assert.equal(f.gain.value, "4");
  assert.equal(f.gain.buttons[1].disabled, true);
  pointer(f.gain, "pointermove", {y:2000});
  assert.equal(f.gain.value, "0");
  assert.equal(f.gain.buttons[0].disabled, true);
  pointer(f.gain, "pointerup");
});

test("another finger cannot move or end an existing gain gesture", () => {
  const f = fixture();
  pointer(f.gain, "pointerdown");
  assert.equal(pointer(f.gain, "pointerdown", {id:2}).defaultPrevented, true);
  pointer(f.gain, "pointermove", {id:2, y:0});
  pointer(f.gain, "pointerup", {id:2});
  assert.equal(f.gain.value, "1");
  assert.equal(f.activeSliders.get(f.gain), 1);
  pointer(f.gain, "pointermove", {y:194});
  assert.equal(f.gain.value, "1.1");
  pointer(f.gain, "pointerup");
});

for (const ending of ["pointercancel", "lostpointercapture"]) {
  test(`${ending} releases the gain gesture and commits its last value`, async () => {
    const f = fixture();
    pointer(f.gain, "pointerdown");
    pointer(f.gain, "pointermove", {y:170});
    pointer(f.gain, ending);
    await f.settle();
    assert.equal(f.activeSliders.size, 0);
    assert.equal(f.dragging.size, 0);
    assert.equal(f.requests[0].body.gain, 1.5);
    pointer(f.gain, "pointermove", {y:20});
    assert.equal(f.gain.value, "1.5");
    pointer(f.gain, "pointerdown", {id:2});
    assert.equal(f.activeSliders.get(f.gain), 2);
    pointer(f.gain, "pointerup", {id:2});
  });
}

test("gain −/+ sends real 0.1 dB adjustments without floating point drift", async () => {
  const f = fixture();
  for (let i=0; i<7; i++) { f.gain.buttons[1].dispatchEvent(new Event("click")); await f.settle(); }
  assert.equal(f.gain.value, "1.7");
  f.gain.buttons[0].dispatchEvent(new Event("click"));
  await f.settle();
  assert.equal(f.gain.value, "1.6");
  assert.equal(f.requests.at(-1).body.gain, 1.6);
  f.masterGain.buttons[1].dispatchEvent(new Event("click"));
  await f.settle();
  assert.deepEqual(f.requests.at(-1), {path:"/api/master", body:{gain:1.1}});
});

test("gain buttons respect unavailable controls and both gain limits", async () => {
  const f = fixture();
  f.gain.disabled = true;
  f.controls.updateGainButtons(f.gain);
  assert(f.gain.buttons.every(button => button.disabled));
  f.gain.buttons[1].dispatchEvent(new Event("click"));
  pointer(f.gain, "pointerdown");
  pointer(f.gain, "pointermove", {y:0});
  assert.equal(f.activeSliders.size, 0);
  assert.equal(f.requests.length, 0);
  f.gain.disabled = false;
  for (const [value, index] of [[0, 0], [4, 1]]) {
    f.gain.value = value;
    f.controls.updateGainButtons(f.gain);
    assert.equal(f.gain.buttons[index].disabled, true);
    f.gain.buttons[index].dispatchEvent(new Event("click"));
    await f.settle();
    assert.equal(f.gain.value, String(value));
    assert.equal(f.requests.length, 0);
  }
});

test("either MAIN MIX fader moves both caps and sends the linked master volume", async () => {
  for (const side of ["left", "right"]) {
    const f = fixture(), slider = f[side];
    pointer(slider, "pointerdown", {y:50});
    assert.equal(slider.value, "50", "Touching the track must not jump the volume");
    pointer(slider, "pointermove", {y:0});
    assert.equal(f.left.value, "75");
    assert.equal(f.right.value, "75");
    assert.equal(f.left.well.style.get("--level"), "0.75");
    assert.equal(f.right.well.style.get("--level"), "0.75");
    assert.equal(f.masterOutput.textContent, "75 %");
    pointer(slider, "pointerup", {y:0});
    await f.settle();
    assert.deepEqual(f.requests, [{path:"/api/master", body:{volume:75}}]);
  }
});

test("a second finger on the linked master fader cannot compete with the first", () => {
  const f = fixture();
  pointer(f.left, "pointerdown");
  assert.equal(pointer(f.right, "pointerdown", {id:2}).defaultPrevented, true);
  pointer(f.right, "pointermove", {id:2, y:0});
  assert.equal(f.right.value, "50");
  assert.equal(f.activeSliders.has(f.right), false);
  pointer(f.left, "pointerup");
  pointer(f.right, "pointerdown", {id:2});
  pointer(f.right, "pointermove", {id:2, y:180});
  assert.equal(f.left.value, "60");
  pointer(f.right, "pointerup", {id:2});
});

test("status polling preserves a touch adjustment even when Safari does not focus the input", () => {
  const f = fixture();
  f.document.focusWorks = false;
  pointer(f.gain, "pointerdown");
  f.controls.range(f.gain, f.output, 0, "source", " dB");
  assert.equal(f.gain.value, "1");
  assert.equal(f.controls.isBusy("source", f.gain), true);
  pointer(f.gain, "pointerup");
  assert.equal(f.controls.isBusy("source", f.gain), false);
  pointer(f.left, "pointerdown");
  f.controls.range(f.right, f.masterOutput, 0, "master");
  assert.equal(f.left.value, "50");
  assert.equal(f.right.value, "50");
  pointer(f.left, "pointerup");
});

test("keyboard/native range events remain functional and source faders retain native dragging", async () => {
  const f = fixture();
  f.gain.value = "1.1";
  f.gain.dispatchEvent(new Event("input"));
  f.gain.dispatchEvent(new Event("change"));
  await f.settle();
  assert.equal(f.requests[0].body.gain, 1.1);
  assert.equal(pointer(f.volume, "pointerdown").defaultPrevented, false);
  assert.equal(f.activeSliders.has(f.volume), false);
  f.volume.value = "65";
  f.volume.dispatchEvent(new Event("input"));
  f.volume.dispatchEvent(new Event("change"));
  await f.settle();
  assert.equal(f.requests.at(-1).body.volume, 65);
});

test("mouse rotary gestures retain the existing diagonal adjustment", () => {
  const f = fixture();
  pointer(f.gain, "pointerdown", {kind:"mouse"});
  pointer(f.gain, "pointermove", {kind:"mouse", x:160});
  assert.equal(f.gain.value, "1.8");
  pointer(f.gain, "pointerup", {kind:"mouse"});
});
