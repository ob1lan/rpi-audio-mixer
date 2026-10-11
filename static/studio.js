"use strict";

function createStudioEditor({api, onState, toast}) {
  const panel = document.getElementById("studio-panel"), content = document.getElementById("studio-content");
  const clone = value => JSON.parse(JSON.stringify(value));
  const names = {main:"Salle", monitor:"Retour", stream:"Streaming", cue:"Casque PFL"};
  let server = null, draft = null, revision = -1, dirty = false, busy = false, tab = "master", selectedInput = "", selectedBus = "main";

  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text !== undefined) element.textContent = text;
    if (className) element.className = className;
    return element;
  }
  function button(text, handler, secondary=true) {
    const element = node("button", text, secondary ? "secondary" : "primary");
    element.type = "button"; element.addEventListener("click", handler); element.disabled = busy;
    return element;
  }
  function mark() { dirty = true; status(); }
  function field(parent, text, object, key, {min, max, step=1, choices, type="number", help, transient=false}={}) {
    const wrap = node("label", undefined, "studio-field"); wrap.append(node("span", text));
    const input = node(choices ? "select" : "input");
    if (choices) for (const [value, title] of choices) { const option = node("option", title); option.value = value; input.append(option); }
    else { input.type = type; if (min !== undefined) input.min = min; if (max !== undefined) input.max = max; input.step = step; }
    if (type === "checkbox" && !choices) input.checked = object[key]; else input.value = object[key];
    input.disabled = busy;
    input.addEventListener(type === "checkbox" || choices ? "change" : "input", () => {
      if (!input.checkValidity() || input.value === "" && type === "number") return;
      object[key] = type === "checkbox" && !choices ? input.checked : type === "number" && !choices ? Number(input.value) : input.value;
      if (!transient) mark();
    });
    wrap.append(input); if (help) wrap.append(node("small", help)); parent.append(wrap); return input;
  }
  function section(title, help) {
    const element = node("section", undefined, "studio-section"); element.append(node("h3", title));
    if (help) element.append(node("p", help, "small")); content.append(element); return element;
  }
  function grid(parent) { const element = node("div", undefined, "studio-grid"); parent.append(element); return element; }
  function status() {
    const data = server?.studio;
    document.getElementById("studio-status").textContent = data?.demo ? "APERÇU SIMULÉ · aucun traitement matériel" :
      data?.available ? (data.settings.enabled ? "Moteur live actif" : "Traitements Master disponibles") : "Audio traité indisponible";
    document.getElementById("studio-save-status").textContent = busy ? "Application…" : dirty ? "Modifications à appliquer" : "Réglages sauvegardés";
    document.getElementById("studio-apply").disabled = busy || !dirty;
    document.getElementById("studio-reset").disabled = busy || !dirty;
    document.getElementById("open-studio").classList.toggle("eq-is-active", Boolean(data?.settings.enabled && data?.available));
  }
  function inputs(settings, devices) {
    // A newly registered Bluetooth source gets the existing console levels.
    for (const device of devices || []) if (!settings.inputs[device.address]) {
      settings.inputs[device.address] = {name:device.name, kind:"music", volume:device.volume ?? 100,
        gain:device.gain || 0, muted:Boolean(device.muted), solo:Boolean(device.solo), pfl:false, pan:0,
        eq:EQMath.defaults(), dynamics:clone(settings.buses.main.dynamics), gate:{enabled:false, threshold:-45, hold:150},
        sends:{main:{level:0, pre:false, excluded:false}, monitor:{level:-60, pre:true, excluded:false}, stream:{level:0, pre:false, excluded:false}}};
      settings.inputs[device.address].dynamics.enabled = false; settings.inputs[device.address].dynamics.limiter = false;
    }
    if (!settings.enabled) for (const device of devices || []) if(settings.inputs[device.address]) for(const key of ["volume","gain","muted","solo"]) if(device[key]!==null&&device[key]!==undefined) settings.inputs[device.address][key]=device[key];
    return settings;
  }
  function receive(data) {
    if (!data?.studio || (data.studio.revision ?? 0) < (server?.studio.revision ?? 0)) return;
    const changed = revision !== data.studio.revision || JSON.stringify(server?.devices?.map(d=>[d.address,d.volume,d.gain,d.muted,d.solo])) !== JSON.stringify(data.devices?.map(d=>[d.address,d.volume,d.gain,d.muted,d.solo]));
    server = data;
    if (!dirty && !busy && (!draft || changed)) {
      draft = inputs(clone(data.studio.settings), data.devices); revision = data.studio.revision;
      if (panel.open) render();
    }
    status(); telemetry();
  }
  async function send(body) {
    if (busy) return false;
    if (dirty && body.action !== "configure" && body.action !== "record") { toast("Applique ou annule tes modifications avant cette action."); return false; }
    const keepDraft = dirty && body.action === "record";
    busy = true; status();
    content.querySelectorAll("button,input,select").forEach(element => { element.disabled = true; });
    try {
      const result = await api("/api/studio", body);
      if (!keepDraft) { dirty = false; draft = null; revision = -1; }
      onState(result.state); receive(result.state); return true;
    } catch (error) { toast(error.message); return false; }
    finally {
      busy = false;
      if (!draft && server) { draft = inputs(clone(server.studio.settings), server.devices); revision = server.studio.revision; }
      if (panel.open) render(); status();
    }
  }
  async function apply() {
    const invalid = [...content.querySelectorAll("input")].find(input => !input.checkValidity() || input.type === "number" && input.value === "");
    if (invalid) { invalid.reportValidity(); invalid.focus(); return; }
    await send({action:"configure", settings:clone(draft), revision});
  }
  function dynamics(parent, settings) {
    const controls = grid(parent);
    field(controls,"Compresseur actif",settings,"enabled",{type:"checkbox"});
    field(controls,"Seuil · dBFS",settings,"threshold",{min:-60,max:0,step:.5});
    field(controls,"Ratio",settings,"ratio",{min:1,max:20,step:.1});
    field(controls,"Attaque · ms",settings,"attack",{min:.1,max:100,step:.1});
    field(controls,"Relâchement · ms",settings,"release",{min:10,max:2000});
    field(controls,"Gain de sortie · dB",settings,"makeup",{min:0,max:12,step:.1});
    field(controls,"Limiteur de crêtes actif",settings,"limiter",{type:"checkbox"});
    field(controls,"Plafond · dBFS",settings,"ceiling",{min:-12,max:0,step:.1});
  }
  function eq(parent, settings) {
    const details = node("details", undefined, "studio-eq"); details.append(node("summary","Égaliseur paramétrique · six bandes")); parent.append(details);
    const canvas = node("canvas"); canvas.height = 170; canvas.setAttribute("aria-label","Réponse de l’égaliseur, de 20 Hz à 20 kHz"); details.append(canvas);
    const controls = grid(details);
    field(controls,"EQ actif",settings,"enabled",{type:"checkbox"});
    field(controls,"Compenser les boosts",settings,"auto_headroom",{type:"checkbox"});
    field(controls,"Préampli · dB",settings,"preamp",{min:-24,max:0,step:.1});
    for (const [key, text] of [["highpass","Passe-haut"],["lowpass","Passe-bas"]]) {
      field(controls,text,settings[key],"enabled",{type:"checkbox"}); field(controls,`${text} · Hz`,settings[key],"frequency",{min:20,max:20000});
    }
    settings.bands.forEach((band, i) => {
      const row = node("fieldset", undefined, "studio-band"); row.append(node("legend",`Bande ${i+1}`));
      field(row,"Active",band,"enabled",{type:"checkbox"});
      field(row,"Type",band,"type",{choices:[["peak","Cloche"],["low_shelf","Plateau grave"],["high_shelf","Plateau aigu"]]});
      field(row,"Fréquence · Hz",band,"frequency",{min:20,max:20000});
      field(row,"Gain · dB",band,"gain",{min:-12,max:12,step:.1});
      const q = field(row,"Q · cloche",band,"q",{min:.2,max:12,step:.1});
      q.disabled = busy || band.type !== "peak";
      row.querySelector("select").addEventListener("change",()=>{q.disabled = busy || band.type !== "peak";});
      details.append(row);
    });
    function paint() {
      if (!details.open) return;
      const width = Math.max(200, canvas.clientWidth), height = 170, ratio = window.devicePixelRatio || 1;
      canvas.width = width*ratio; canvas.height = height*ratio;
      const ctx = canvas.getContext("2d"); if (!ctx) return;
      ctx.setTransform(ratio,0,0,ratio,0,0); ctx.clearRect(0,0,width,height);
      const x = frequency => 35 + EQMath.frequencyPosition(frequency)*(width-50), y = db => 15+(12-db)/24*130;
      ctx.font="10px system-ui"; ctx.strokeStyle="#3e504e"; ctx.fillStyle="#a9beb7";
      for (const db of [-12,0,12]) { ctx.beginPath();ctx.moveTo(35,y(db));ctx.lineTo(width-15,y(db));ctx.stroke();ctx.fillText(String(db),4,y(db)+3); }
      for (const frequency of [20,100,1000,10000,20000]) ctx.fillText(frequency>=1000?`${frequency/1000}k`:String(frequency),x(frequency)-7,163);
      const rows=EQMath.coefficients(settings), offset=settings.preamp+EQMath.compensation(settings,rows);
      ctx.save();ctx.beginPath();ctx.rect(35,15,width-50,130);ctx.clip();ctx.beginPath();ctx.strokeStyle="#d7eee7";ctx.lineWidth=2;ctx.setLineDash(settings.enabled?[]:[5,4]);
      for(let i=0;i<=240;i++){const f=EQMath.positionFrequency(i/240),px=x(f),py=y(EQMath.response(rows,f)+offset);if(i)ctx.lineTo(px,py);else ctx.moveTo(px,py);}
      ctx.stroke();ctx.restore();
    }
    details.addEventListener("toggle",paint); details.addEventListener("input",paint); details.addEventListener("change",paint);
    return details;
  }
  function feedback(parent, settings, bus) {
    const controls = grid(parent);
    field(controls,"Mode",settings,"mode",{choices:[["off","Bypass"],["assist","Assisté · suggestion"],["balance","Balance · filtres automatiques"],["live","Live automatique · expérimental"]]});
    field(controls,"Émergence spectrale · dB",settings,"sensitivity",{min:10,max:40});
    field(controls,"Filtres automatiques maximum",settings,"max_live",{min:1,max:8});
    field(controls,"Durée des filtres live · s",settings,"release",{min:5,max:120});
    const result=node("p","Aucune fréquence suspecte.","studio-feedback-status small"); result.dataset.bus=bus;parent.append(result);
    parent.append(node("p","Le mode live utilise une détection spectrale expérimentale. Une note musicale peut ressembler à un larsen. Valide d’abord les corrections en mode assisté.","small"));
    settings.filters.forEach((item,i)=>{
      const row=grid(parent);field(row,"Fréquence · Hz",item,"frequency",{min:40,max:16000});
      field(row,"Atténuation · dB",item,"gain",{min:-24,max:-1,step:.5});field(row,"Q",item,"q",{min:10,max:100});
      row.append(button("Retirer",()=>{settings.filters.splice(i,1);mark();render();}));
    });
    parent.append(button("Ajouter une coupure",()=>{if(settings.filters.length>=8){toast("Huit filtres fixes maximum.");return;}
      const suggested=server.studio.meters?.buses?.[bus]?.feedback?.suggestion?.frequency;
      settings.filters.push({frequency:suggested || 1000,gain:-6,q:60}); if(settings.mode==="off")settings.mode="assist";mark();render();}));
    parent.append(button("Figer les filtres de balance",()=>{if(dirty){toast("Applique tes modifications avant de figer la balance.");return;}send({action:"feedback_lock",bus});}));
  }
  function renderMaster() {
    dynamics(section("Dynamique du Master", "Compresseur stéréo lié et limiteur de crêtes. Le limiteur final ne répare pas une saturation produite en amont."),draft.buses.main.dynamics);
    feedback(section("FBQ · salle", "Les corrections anti-larsen ont leur propre banque de filtres."),draft.buses.main.feedback,"main");
  }
  function renderInputs() {
    const ids=Object.keys(draft.inputs); if(!ids.includes(selectedInput))selectedInput=ids[0] || "";
    const source=section("Traitements par entrée", "Disponibles avec le moteur live. Le PFL écoute avant le fader et le mute, sur la sortie casque affectée.");
    const choice={id:selectedInput};const select=field(source,"Source",choice,"id",{transient:true,choices:ids.map(id=>[id,draft.inputs[id].name])});
    select.addEventListener("change",()=>{selectedInput=select.value;render();});
    if(selectedInput){
      const item=draft.inputs[selectedInput], controls=grid(source);
      field(controls,"Nom",item,"name",{type:"text"});field(controls,"Usage",item,"kind",{choices:[["mic","Micro"],["music","Musique"],["remote","Intervenant distant"]]});
      field(controls,"Volume · %",item,"volume",{min:0,max:100});field(controls,"Gain logiciel · dB",item,"gain",{min:-24,max:24,step:.1});
      field(controls,"Balance · −1 gauche / +1 droite",item,"pan",{min:-1,max:1,step:.05});
      for(const [key,text] of [["muted","Mute"],["solo","Solo sur la salle"],["pfl","Préécoute casque PFL"]])field(controls,text,item,key,{type:"checkbox"});
      const meter=node("p","Niveau indisponible","studio-input-status small");meter.dataset.input=selectedInput;source.append(meter);
      eq(source,item.eq);
      const dynamic=section("Dynamique de l’entrée");dynamics(dynamic,item.dynamics);
      const gate=grid(section("Gate", "Atténue le signal sous le seuil, avec maintien et fermeture progressive."));
      field(gate,"Gate actif",item.gate,"enabled",{type:"checkbox"});field(gate,"Seuil · dBFS",item.gate,"threshold",{min:-80,max:-10});field(gate,"Maintien · ms",item.gate,"hold",{min:0,max:2000});
    }
    const automatic=section("Gestion de la parole"), automix=grid(automatic);
    field(automix,"Automix des micros",draft.automix,"enabled",{type:"checkbox"});field(automix,"Atténuation minimale · dB",draft.automix,"floor",{min:-40,max:-6});
    const duck=grid(automatic);field(duck,"Baisser la musique quand on parle",draft.ducking,"enabled",{type:"checkbox"});
    field(duck,"Seuil de parole · dBFS",draft.ducking,"threshold",{min:-60,max:-10});field(duck,"Baisse de la musique · dB",draft.ducking,"depth",{min:-30,max:0});field(duck,"Retour du niveau · ms",draft.ducking,"release",{min:50,max:2000});
  }
  function renderHardware() {
    const base=section("Moteur live", "Entrées USB et Bluetooth séparées, quatre bus. La latence réelle doit être mesurée sur le Pi 4B avec ton interface.");
    const controls=grid(base);field(controls,"Activer le mode live",draft,"enabled",{type:"checkbox"});
    field(controls,"Période audio · ms",draft,"period",{choices:[[5,"5 ms"],[10,"10 ms"],[20,"20 ms"],[40,"40 ms"]]}).addEventListener("change",()=>{draft.period=Number(draft.period);});
    field(controls,"Canaux de sortie de l’interface",draft,"output_channels",{min:2,max:16});
    base.append(node("p","Le mode live utilise la sortie sélectionnée dans « Sortie audio », avec le contrôle Bureau Audio Master. Les paires de sorties doivent être distinctes. Le gain analogique et l’alimentation des micros se règlent sur l’interface.","small"));
    base.append(button("Actualiser les périphériques",()=>{if(dirty){toast("Applique ou annule tes modifications avant d’actualiser.");return;}send({action:"refresh"});}));
    for(const [index,capture] of draft.captures.entries()){
      const element=section(`Capture ${index+1} · ${capture.card}`), fields=grid(element);
      field(fields,"Canaux fournis par l’interface",capture,"channels",{min:1,max:32});
      for(const source of capture.sources){
        const row=grid(element), input=draft.inputs[source.id];field(row,"Nom du micro",input,"name",{type:"text"});
        const display={channel:source.channel+1};field(row,"Canal physique · à partir de 1",display,"channel",{min:1,max:32}).addEventListener("input",()=>{source.channel=display.channel-1;});
        row.append(button("Retirer le micro",()=>{capture.sources=capture.sources.filter(v=>v.id!==source.id);delete draft.inputs[source.id];mark();render();}));
      }
      element.append(button("Ajouter un micro",()=>{
        if(capture.sources.length>=capture.channels){toast("Tous les canaux de cette capture sont déjà affectés.");return;}
        const id="usb_"+Math.random().toString(36).slice(2,10), channel=Array.from({length:capture.channels},(_,i)=>i).find(i=>!capture.sources.some(v=>v.channel===i));
        const template=idsTemplate();template.name=`Micro ${capture.sources.length+1}`;template.kind="mic";template.volume=100;template.gain=0;template.muted=false;template.solo=false;template.pfl=false;
        template.pan=0;template.dynamics.enabled=false;template.dynamics.limiter=false;template.gate={enabled:false,threshold:-45,hold:150};template.sends={main:{level:0,pre:false,excluded:false},monitor:{level:-60,pre:true,excluded:false},stream:{level:0,pre:false,excluded:false}};
        template.eq=EQMath.defaults();template.eq.enabled=true;template.eq.highpass={enabled:true,frequency:80};
        draft.inputs[id]=template;capture.sources.push({id,channel});mark();render();
      }));
      element.append(button("Retirer cette capture",()=>{for(const source of capture.sources)delete draft.inputs[source.id];draft.captures.splice(index,1);mark();render();}));
    }
    const add=section("Ajouter une interface de capture"), choices=server.studio.capture_choices || [], selection={pcm:choices[0]?`${choices[0].card}:${choices[0].device}`:""};
    field(add,"Périphérique",selection,"pcm",{transient:true,choices:choices.map(v=>[`${v.card}:${v.device}`,`${v.name} · périphérique ${v.device}`])});
    add.append(button("Ajouter l’interface",()=>{const found=choices.find(v=>`${v.card}:${v.device}`===selection.pcm);if(!found){toast("Aucune capture détectée.");return;}
      if(draft.captures.some(v=>v.card===found.card&&v.device===found.device)){toast("Cette capture est déjà ajoutée.");return;}
      draft.captures.push({card:found.card,device:found.device,channels:2,sources:[]});mark();render();}));
    if(!choices.length)add.append(node("p","Branche une interface USB puis actualise les périphériques.","small"));
  }
  function idsTemplate() {
    const id=Object.keys(draft.inputs)[0];
    if(id)return clone(draft.inputs[id]);
    return {name:"Micro",kind:"mic",volume:100,gain:0,muted:false,solo:false,pfl:false,pan:0,eq:EQMath.defaults(),dynamics:clone(draft.buses.main.dynamics),gate:{enabled:false,threshold:-45,hold:150},
      sends:{main:{level:0,pre:false,excluded:false},monitor:{level:-60,pre:true,excluded:false},stream:{level:0,pre:false,excluded:false}}};
  }
  function renderMatrix() {
    const element=section("Matrice · sources vers bus", "−60 dB coupe un envoi. Pré-fader convient aux retours indépendants. « Exclure » crée notamment un mix-minus pour un intervenant distant.");
    const wrapper=node("div",undefined,"studio-table-wrap"),table=node("table",undefined,"studio-matrix"), head=node("thead"),tr=node("tr");
    tr.append(node("th","Source"));for(const bus of ["main","monitor","stream"])tr.append(node("th",draft.buses[bus].name));head.append(tr);table.append(head);
    const body=node("tbody");
    for(const [sid,input] of Object.entries(draft.inputs)){
      const row=node("tr");const title=node("th",input.name);title.scope="row";row.append(title);
      for(const bus of ["main","monitor","stream"]){const cell=node("td"),send=input.sends[bus];
        field(cell,`${input.name} → ${draft.buses[bus].name} · dB`,send,"level",{min:-60,max:12,step:.5});
        field(cell,"Pré-fader",send,"pre",{type:"checkbox"});field(cell,"Exclure",send,"excluded",{type:"checkbox"});row.append(cell);
      }body.append(row);
    }table.append(body);wrapper.append(table);element.append(wrapper);
    element.append(node("p","Le casque reçoit les sources dont le PFL est activé dans l’onglet Entrées. Affecte sa paire de sorties dans l’onglet Bus.","small"));
  }
  function renderBuses() {
    const source=section("Bus et sorties physiques"),choice={bus:selectedBus},select=field(source,"Bus",choice,"bus",{transient:true,choices:Object.entries(names)});
    select.addEventListener("change",()=>{selectedBus=select.value;render();});
    const item=draft.buses[selectedBus], controls=grid(source);field(controls,"Nom du bus",item,"name",{type:"text"});
    field(controls,"Volume · %",item,"volume",{min:0,max:100});field(controls,"Mute du bus",item,"muted",{type:"checkbox"});
    const route={pair:item.channels.length?`${item.channels[0]},${item.channels[1]}`:""},pairs=[["","Aucune sortie physique"]];
    for(let i=0;i+1<draft.output_channels;i+=2)pairs.push([`${i},${i+1}`,`Sorties ${i+1}–${i+2}`]);
    if(route.pair&&!pairs.some(v=>v[0]===route.pair))pairs.push([route.pair,`Sorties ${item.channels.map(v=>v+1).join("–")}`]);
    field(controls,"Paire de sorties",route,"pair",{choices:pairs}).addEventListener("change",()=>{item.channels=route.pair?route.pair.split(",").map(Number):[];});
    eq(source,item.eq);dynamics(section("Dynamique du bus"),item.dynamics);
    feedback(section("FBQ du bus"),item.feedback,selectedBus);
  }
  function renderScenes() {
    const element=section("Scènes", "Une scène mémorise les niveaux, les EQ, les traitements et le routage. Le rappel d’une configuration d’entrées différente redémarre le moteur audio.");
    const selection={name:""};const text=field(element,"Nom de la scène",selection,"name",{type:"text",transient:true});text.maxLength=64;
    element.append(button("Sauvegarder l’état actuel",()=>{if(dirty){toast("Applique tes modifications avant de sauvegarder une scène.");return;}send({action:"scene_save",name:selection.name.trim()});}));
    for(const scene of server.studio.scenes || []){
      const row=node("div",undefined,"studio-file");row.append(node("strong",scene.name));
      row.append(button("Rappeler",()=>{if(dirty){toast("Applique ou annule tes modifications avant le rappel.");return;}send({action:"scene_recall",name:scene.name});}));
      const link=node("a","Exporter","secondary");link.href=`/api/scenes/${encodeURIComponent(scene.name)}`;link.download="bureau-audio-scene.json";row.append(link);
      row.append(button("Supprimer",()=>send({action:"scene_delete",name:scene.name})));element.append(row);
    }
    const upload=node("input");upload.type="file";upload.accept="application/json,.json";upload.setAttribute("aria-label","Importer une scène JSON");
    upload.addEventListener("change",async()=>{const file=upload.files[0];if(!file)return;if(dirty){toast("Applique ou annule tes modifications avant l’import.");return;}
      if(file.size>262144){toast("Le fichier de scène est trop volumineux.");return;}try{await send({action:"scene_import",name:selection.name.trim() || file.name.replace(/\.json$/i,""),scene:JSON.parse(await file.text())});}catch(error){toast(error.message);}});
    const wrap=node("label","Importer une scène");wrap.append(upload);element.append(wrap);
  }
  function renderRecording() {
    const element=section("Enregistrement WAV", "Stéréo, 48 kHz, 16 bits. Pré : avant les traitements du bus ; post : après ses traitements et son niveau. En mode classique, seule la salle est disponible, après l’EQ Master.");
    const data=server.studio.recording,choice={bus:data.bus || "main",tap:data.tap || "post"};
    field(element,"Bus à enregistrer",choice,"bus",{transient:true,choices:Object.entries(names)});field(element,"Prise",choice,"tap",{transient:true,choices:[["pre","Avant les traitements du bus"],["post","Après les traitements du bus"]]});
    element.append(button(data.active?"Arrêter et finaliser le WAV":"Démarrer l’enregistrement",()=>send({action:"record",enabled:!server.studio.recording.active,bus:choice.bus,tap:choice.tap}),false));
    const status=node("p",undefined,"studio-record-status small");element.append(status);
    for(const file of data.files || []){const row=node("div",undefined,"studio-file");row.append(node("span",`${file.name} · ${(file.bytes/1048576).toFixed(1)} Mo`));
      if(file.ready){const link=node("a","Télécharger","secondary");link.href=`/api/recordings/${encodeURIComponent(file.name)}`;link.download=file.name;row.append(link);}else row.append(node("span","Finalisation en attente"));element.append(row);}
  }
  function telemetry(data, audio) {
    const meters=data || server?.studio?.meters;
    for(const element of content.querySelectorAll(".studio-feedback-status")){
      const fbq=meters?.buses?.[element.dataset.bus]?.feedback,suggestion=fbq?.suggestion;
      const active=[...(fbq?.live || []),...(fbq?.balance || [])].map(v=>`${v.frequency} Hz`).join(", ");
      element.textContent=(suggestion?`Fréquence suspecte : ${suggestion.frequency} Hz · ${suggestion.db} dBFS · confiance ${Math.round(suggestion.confidence*100)} %`:"Aucune fréquence suspecte")+(active?` · Filtres automatiques : ${active}`:"");
    }
    for(const element of content.querySelectorAll(".studio-input-status")){const item=meters?.inputs?.[element.dataset.input];element.textContent=item?`Crête : ${(20*Math.log10(Math.max(1e-5,item.peak))).toFixed(1)} dBFS · réduction ${item.reduction} dB · automix ${item.automix_db ?? 0} dB`:"Niveau indisponible";}
    const health=document.getElementById("studio-health");if(health&&audio){const failures=Object.values(audio.streams || {}),buffers=Object.values(audio.buffers || {});const under=buffers.reduce((sum,v)=>sum+v.underruns,0),over=buffers.reduce((sum,v)=>sum+v.overruns,0);health.textContent=failures.length?`Flux interrompu : ${failures.join(" · ")}`:buffers.length?`Tampons : ${under} manque(s) de trames · ${over} dépassement(s)`:"";}
    const recording=content.querySelector(".studio-record-status");if(recording&&server){const item=server.studio.recording;recording.textContent=item.error || (item.active?`Enregistrement : ${item.seconds.toFixed(1)} s · ${item.name}`:"Enregistrement arrêté");}
    const levels=document.getElementById("studio-levels");if(levels&&meters){levels.textContent=Object.entries(meters.buses || {}).map(([bus,item])=>`${names[bus]} : ${item.peaks?(20*Math.log10(Math.max(1e-5,...item.peaks))).toFixed(1):"—"} dBFS · réduction ${item.reduction || 0} dB`).join(" | ");}
  }
  function render() {
    if(!draft)return;content.replaceChildren();
    panel.querySelectorAll("[data-studio-tab]").forEach(button=>{button.setAttribute("aria-pressed",String(button.dataset.studioTab===tab));button.classList.toggle("active",button.dataset.studioTab===tab);button.disabled=busy;});
    ({master:renderMaster,hardware:renderHardware,inputs:renderInputs,matrix:renderMatrix,buses:renderBuses,scenes:renderScenes,recording:renderRecording})[tab]();status();telemetry();
  }
  for(const button of panel.querySelectorAll("[data-studio-tab]"))button.addEventListener("click",()=>{tab=button.dataset.studioTab;render();});
  document.getElementById("studio-apply").addEventListener("click",apply);
  document.getElementById("studio-reset").addEventListener("click",()=>{dirty=false;draft=inputs(clone(server.studio.settings),server.devices);revision=server.studio.revision;render();});
  document.getElementById("open-studio").addEventListener("click",render);
  return {receive,telemetry};
}

if (typeof module !== "undefined") module.exports = {createStudioEditor};
