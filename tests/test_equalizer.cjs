"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {spawnSync} = require("node:child_process");
const {existsSync, readFileSync} = require("node:fs");
const vm = require("node:vm");
const EQ = require("../static/equalizer.js");

test("logarithmic frequency mapping covers the whole audible range", () => {
  for (const frequency of [20,80,1000,10000,20000]) assert.ok(Math.abs(EQ.positionFrequency(EQ.frequencyPosition(frequency))-frequency)<1e-8);
  assert.equal(EQ.positionFrequency(-1),20);
  assert.equal(EQ.positionFrequency(2),20000);
});
test("flat curve and combined gain compensation", () => {
  const settings = EQ.defaults();
  for (const f of [20,1000,20000]) assert.equal(EQ.response(EQ.coefficients(settings),f),0);
  settings.bands[0] = {...settings.bands[0], frequency:1000, gain:6};
  settings.bands[1] = {...settings.bands[1], frequency:1000, gain:6};
  assert.ok(Math.abs(EQ.compensation(settings)+12)<1e-7);
});
test("the visual response uses the same coefficients as the audio processor", () => {
  const cases = ["peak","low_shelf","high_shelf","highpass","lowpass"].flatMap(kind => [20,1000,20000].map(f => [kind,f,6,1.5]));
  const python = process.env.EQ_PYTHON || (existsSync(".venv/bin/python") ? ".venv/bin/python" : "python");
  const result = spawnSync(python,["-c", "import json,sys; from equalizer import biquad; print(json.dumps([biquad(*v) for v in json.loads(sys.stdin.read())]))"],{input:JSON.stringify(cases),encoding:"utf8"});
  assert.equal(result.status,0,result.stderr);
  const expected = JSON.parse(result.stdout);
  cases.forEach((args,i) => EQ.biquad(...args).forEach((v,j) => assert.ok(Math.abs(v-expected[i][j])<1e-12,`${args}: coefficient ${j}`)));
});

// Run the production editor listeners with deterministic network/gesture input.
function editorFixture() {
  class Element extends EventTarget {
    constructor(id="") {
      super(); Object.assign(this,{id, value:"", checked:false, disabled:false, children:[], attributes:new Map(), captured:new Set(), style:{setProperty(){}}, classList:{toggle(){}}, span:{textContent:""}});
    }
    setAttribute(key,value) { this.attributes.set(key,value); }
    querySelector() { return this.span; }
    append(value) { this.children.push(value); }
    contains() { return false; }
    checkValidity() { return this.valid !== false; }
    reportValidity() {}
    getContext() { return null; }
    getBoundingClientRect() { return {left:0,top:0,width:1000,height:290}; }
    setPointerCapture(id) { this.captured.add(id); }
    hasPointerCapture(id) { return this.captured.has(id); }
    releasePointerCapture(id) { this.captured.delete(id); }
  }
  const elements = new Map(), timers = new Map(), calls = [], responses = [];
  let timerId=0, editor;
  const element = id => { if (!elements.has(id)) elements.set(id,new Element(id)); return elements.get(id); };
  const document = {getElementById:element, createElement:() => new Element(), activeElement:null};
  const context = vm.createContext({document, Event, Math, JSON, Number, Boolean, String,
    setTimeout:callback => { timers.set(++timerId,callback); return timerId; }, clearTimeout:id => timers.delete(id),
    setInterval(){}, requestAnimationFrame:() => 1, ResizeObserver:class {observe(){}}});
  vm.runInContext(readFileSync(require.resolve("../static/equalizer.js"),"utf8")+"\nglobalThis.makeEditor=createEqualizerEditor;",context);
  editor = context.makeEditor({
    api:(path,body) => new Promise((resolve,reject) => { calls.push({path,body:JSON.parse(JSON.stringify(body))}); responses.push({resolve,reject}); }),
    onState:state => editor.receive(state.equalizer), getLevels:() => null
  });
  const snapshot = (settings,revision=0,available=true) => ({settings,revision,available,demo:false,message:""});
  editor.receive(snapshot(EQ.defaults()));
  function dispatch(id,type,properties={}) {
    const event = new Event(type,{cancelable:true}); Object.assign(event,properties); element(id).dispatchEvent(event); return event;
  }
  return {editor,element,dispatch,calls,responses,snapshot,
    settle:async () => { for (let i=0;i<10;i++) await Promise.resolve(); }};
}
test("queued live edits keep newer gestures and ignore stale status replies", async () => {
  const f=editorFixture();
  f.dispatch("eq-enable","click");
  assert.equal(f.calls.length,1);
  f.element("eq-gain").value="-6";
  f.dispatch("eq-gain","input"); f.dispatch("eq-gain","change");
  assert.equal(f.calls.length,1);
  f.responses[0].resolve({state:{equalizer:f.snapshot(f.calls[0].body,1)}});
  await f.settle();
  assert.equal(f.calls.length,2);
  assert.equal(f.calls[1].body.bands[0].gain,-6);
  f.responses[1].resolve({state:{equalizer:f.snapshot(f.calls[1].body,2)}});
  await f.settle();
  f.editor.receive(f.snapshot(EQ.defaults(),0));
  assert.equal(f.element("eq-gain").value,"-6.0");
  assert.equal(f.element("eq-enable").attributes.get("aria-pressed"),"true");
});
test("a touch drag cannot be stolen by another finger and cancellation commits it", async () => {
  const f=editorFixture(), x=42+EQ.frequencyPosition(80)*940, y=22+236/2;
  const pointer = (type,id,px,py) => f.dispatch("eq-graph",type,{pointerId:id,button:0,clientX:px,clientY:py});
  pointer("pointerdown",1,x,y);
  pointer("pointerdown",2,x,y);
  pointer("pointermove",2,x+400,y-100);
  assert.equal(f.element("eq-frequency").value,"80");
  pointer("pointermove",1,x+100,y-59);
  assert.equal(f.element("eq-gain").value,"6.0");
  pointer("pointercancel",1,x+100,y-59);
  assert.equal(f.calls.length,1);
  assert.equal(f.calls[0].body.bands[0].gain,6);
  assert.equal(f.element("eq-graph").captured.size,0);
});
test("failed saves keep the edited curve and expose a working retry", async () => {
  const f=editorFixture();
  f.dispatch("eq-enable","click"); f.responses[0].reject(new Error("offline")); await f.settle();
  assert.equal(f.element("eq-retry").hidden,false);
  assert.match(f.element("eq-save-status").textContent,/Non enregistré/);
  f.editor.receive(f.snapshot(EQ.defaults()));
  assert.equal(f.element("eq-enable").attributes.get("aria-pressed"),"true");
  f.dispatch("eq-retry","click"); assert.equal(f.calls.length,2);
  f.responses[1].resolve({state:{equalizer:f.snapshot(f.calls[1].body,1)}}); await f.settle();
  assert.equal(f.element("eq-retry").hidden,true);
});
test("unavailable routes disable activation but retain editable bypass settings", () => {
  const f=editorFixture();
  f.editor.receive(f.snapshot(EQ.defaults(),0,false));
  assert.equal(f.element("eq-enable").disabled,true);
  assert.equal(f.element("eq-gain").disabled,false);
  f.editor.receive(f.snapshot({...EQ.defaults(),enabled:true},1,false));
  assert.equal(f.element("eq-enable").disabled,false);
  assert.equal(f.element("eq-gain").disabled,true);
});
