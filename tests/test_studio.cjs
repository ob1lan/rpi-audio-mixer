"use strict";
const {test} = require("node:test");
const assert = require("node:assert/strict");
const {readFileSync, existsSync} = require("node:fs");
const {spawnSync} = require("node:child_process");
const vm = require("node:vm");
const EQMath = require("../static/equalizer.js");
const python = existsSync(".venv/bin/python") ? ".venv/bin/python" : "python";
const result = spawnSync(python,["-c","import json; from studio import studio_defaults, input_defaults; s=studio_defaults(); s['inputs']={'usb_mic':input_defaults('Micro','mic')}; print(json.dumps(s))"],{encoding:"utf8"});
assert.equal(result.status,0,result.stderr);
const defaults = JSON.parse(result.stdout);
const copy = value => JSON.parse(JSON.stringify(value));

// Deterministic DOM fixture exercises the shipped editor, without browser automation.
function fixture() {
  class Element extends EventTarget {
    constructor(tag="div") {
      super();Object.assign(this,{tagName:tag,children:[],dataset:{},attributes:new Map(),value:"",checked:false,disabled:false,open:false,className:"",ownText:""});
      this.classList={toggle:()=>{}};
    }
    append(...items) { this.children.push(...items); }
    replaceChildren(...items) { this.children=items; }
    set textContent(value) { this.ownText=String(value); this.children=[]; }
    get textContent() { return this.ownText+this.children.map(v=>v.textContent).join(""); }
    setAttribute(key,value) { this.attributes.set(key,value); }
    matches(selector) {
      return selector==="[data-studio-tab]" ? Boolean(this.dataset.studioTab) : selector[0]==="." ? this.className.split(" ").includes(selector.slice(1)) : this.tagName===selector;
    }
    querySelectorAll(selector) { const selectors=selector.split(",");return this.children.flatMap(child=>[...(selectors.some(v=>child.matches(v))?[child]:[]),...child.querySelectorAll(selector)]); }
    querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
    checkValidity() { return this.valid!==false; }
    reportValidity() { this.reported=true; }
    focus() { this.focused=true; }
    getContext() { return null; }
  }
  const elements = new Map(),calls=[],responses=[],messages=[];
  const get=id=>{if(!elements.has(id))elements.set(id,new Element());return elements.get(id);};
  const panel=get("studio-panel");panel.open=true;
  for(const tab of ["master","hardware","inputs","matrix","buses","scenes","recording"]){const button=new Element("button");button.dataset.studioTab=tab;panel.append(button);}
  const content=get("studio-content");panel.append(content);
  const context=vm.createContext({document:{getElementById:get,createElement:tag=>new Element(tag)},EQMath,Event,window:{devicePixelRatio:1},console});
  vm.runInContext(readFileSync(require.resolve("../static/studio.js"),"utf8")+"\nglobalThis.make=createStudioEditor;",context);
  let editor;
  editor=context.make({api:(path,body)=>new Promise((resolve,reject)=>{calls.push({path,body:copy(body)});responses.push({resolve,reject});}),onState:state=>editor.receive(state),toast:message=>messages.push(message)});
  const state=(revision=0,settings=defaults,scenes=[])=>({devices:[],studio:{revision,settings:copy(settings),available:true,demo:true,scenes,recording:{active:false,seconds:0,files:[]},capture_choices:[{card:"USB",device:0,name:"USB"}]}});
  editor.receive(state());
  const dispatch=(element,type="click")=>element.dispatchEvent(new Event(type));
  const tab=name=>dispatch(panel.querySelectorAll("[data-studio-tab]").find(v=>v.dataset.studioTab===name));
  const field=text=>content.querySelectorAll("label").find(v=>v.children[0]?.textContent===text)?.children[1];
  const button=text=>content.querySelectorAll("button").find(v=>v.textContent===text);
  return {editor,get,content,state,calls,responses,messages,dispatch,tab,field,button,settle:async()=>{for(let i=0;i<12;i++)await Promise.resolve();}};
}

test("polling and a failed save preserve local edits until explicit reload",async()=>{
  const f=fixture();const threshold=f.field("Seuil · dBFS");threshold.value="-25";f.dispatch(threshold,"input");
  const updated=copy(defaults);updated.buses.main.dynamics.threshold=-10;f.editor.receive(f.state(2,updated));
  assert.equal(f.field("Seuil · dBFS").value,"-25");
  f.dispatch(f.get("studio-apply"));assert.equal(f.calls[0].body.revision,0);assert.equal(f.calls[0].body.settings.buses.main.dynamics.threshold,-25);
  f.responses[0].reject(new Error("Révision périmée"));await f.settle();
  assert.equal(f.field("Seuil · dBFS").value,-25);assert.equal(f.get("studio-apply").disabled,false);
  f.dispatch(f.get("studio-reset"));assert.equal(f.field("Seuil · dBFS").value,-10);
});

test("scene naming stays transient and save uses its exact value",async()=>{
  const f=fixture();f.tab("scenes");const name=f.field("Nom de la scène");name.value="Conférence";f.dispatch(name,"input");
  assert.equal(f.get("studio-apply").disabled,true);
  f.dispatch(f.button("Sauvegarder l’état actuel"));assert.equal(f.calls[0].body.name,"Conférence");
  f.responses[0].resolve({state:f.state(0,defaults,[{name:"Conférence"}])});await f.settle();
  assert.ok(f.button("Rappeler"));
});

test("scene deletion cannot discard unapplied audio edits",()=>{
  const f=fixture();f.editor.receive(f.state(1,defaults,[{name:"Live"}]));
  const threshold=f.field("Seuil · dBFS");threshold.value="-25";f.dispatch(threshold,"input");
  f.tab("scenes");f.dispatch(f.button("Supprimer"));assert.equal(f.calls.length,0);assert.match(f.messages[0],/modifications/);
});

test("physical USB channel uses one-based labels and zero-based configuration",()=>{
  const f=fixture();f.tab("hardware");f.dispatch(f.button("Ajouter l’interface"));f.dispatch(f.button("Ajouter un micro"));
  const channel=f.field("Canal physique · à partir de 1");channel.value="2";f.dispatch(channel,"input");
  f.dispatch(f.get("studio-apply"));const config=f.calls[0].body.settings;
  assert.equal(config.captures[0].sources[0].channel,1);
  const input=config.inputs[config.captures[0].sources[0].id];assert.equal(input.kind,"mic");assert.equal(input.eq.highpass.frequency,80);
});

test("invalid numeric input blocks configuration submission",()=>{
  const f=fixture(), threshold=f.field("Seuil · dBFS");threshold.value="-25";f.dispatch(threshold,"input");threshold.valid=false;
  f.dispatch(f.get("studio-apply"));assert.equal(f.calls.length,0);assert.equal(threshold.reported,true);assert.equal(threshold.focused,true);
});

test("stopping recording preserves an unrelated dirty draft",async()=>{
  const f=fixture();const threshold=f.field("Seuil · dBFS");threshold.value="-25";f.dispatch(threshold,"input");
  f.tab("recording");f.dispatch(f.button("Démarrer l’enregistrement"));f.responses[0].resolve({state:f.state()});await f.settle();
  assert.equal(f.get("studio-apply").disabled,false);f.tab("master");assert.equal(f.field("Seuil · dBFS").value,-25);
});
