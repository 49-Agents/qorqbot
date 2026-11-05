// ---------------------------------------------------------------- uploads (right panel tab)
// The "Uploads" tab of the tasks panel (see js/tasks.js T_VIEWS/tSetView): one paste box that takes either
// text, media or data files, never mixed. Text goes the normal context route (POST /biz/<b>/context/upload -> bizctx dump
// -> a registered source document, accepted into the Context filing queue). Media goes to assets/<polished|drafted>/[<topic>/] through
// POST /biz/<b>/assets/upload, one multipart call per file, every file of one submit sharing the same
// notes, topic and batch id. Data files use the shared data_uploads_upload operation. tasks.js calls in:
// tPaintUploads() on tab entry and business change; nothing here autosaves, so there is no flush.
//
// Pure helpers up top (tests/test_uploads.js drives them headless); DOM below.

const UP_VIDEO_EXT = /\.(mp4|mov|webm|m4v|mkv|avi)$/i;
const UP_IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|avif|bmp)$/i;
const UP_DATA_EXT = /\.(csv|tsv|xlsx|xls|ods|json|jsonl|txt)$/i;
const UP_B32 = "0123456789abcdefghjkmnpqrstvwxyz";
const UP_ENCOUNTERS = [["", "Context"], ["call", "Call"], ["meeting", "Meeting"]];
const UP_PERSON = `<svg class="upperson" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="7" r="4"/><path d="M4 21v-2a8 8 0 0 1 16 0v2"/></svg>`;

// what the form holds: "empty" | "text" | "media"
function upMode(f) {
  if (f.files && f.files.length) return "media";
  return String(f.text || "").trim() ? "text" : "empty";
}
// why a paste of `kind` ("text" | "media") may not go into the form right now, "" when it may
function upRefuse(f, kind) {
  const m = upMode(f);
  if (kind === "media" && f.origin) return "Calls and meetings are pasted as text. Choose Context to attach files.";
  if (kind === "media" && m === "text") return "Clear the text before attaching files.";
  if (kind === "text" && m === "media") return "Remove the attached files before adding text.";
  return "";
}
// the one-segment slug the server takes as a subfolder (a nested context path keeps its leaf)
function upTopicSlug(s) {
  let t = String(s || "").trim().toLowerCase();
  if (!t) return "";
  t = t.split("/").filter(Boolean).pop() || "";
  t = t.replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 64);
  if (t && !/^[a-z]/.test(t)) t = "x" + t;
  return t;
}
// image | video | file, for the preview strip
function upKind(file) {
  const type = String(file.type || ""), name = String(file.name || "");
  if (type.startsWith("image/") || (!type && UP_IMAGE_EXT.test(name))) return "image";
  if (type.startsWith("video/") || (!type && UP_VIDEO_EXT.test(name))) return "video";
  return "file";
}
function upSendLabel(f, busy) {
  if (busy) return "Uploading…";
  return upNeedsWho(f) || "Upload";
}
// the payload the text route takes: an ordinary upload sends exactly what it always did, a call or meeting adds who and when
function upPayload(f) {
  const payload = { text: f.text.trim(), notes: f.notes, topic: f.topic, task_ids: (f.tasks || []).map(task => task.id) };
  if (!f.origin) return payload;
  return { ...payload, origin: f.origin, participants: f.participants, ...(f.occurred_on ? { occurred_on: f.occurred_on } : {}) };
}
// what a call or meeting still needs before it may be sent ("" when nothing): named in the send button's label
function upNeedsWho(f) {
  return f.origin && upMode(f) !== "media" && !(f.participants || []).length ? `Add who the ${f.origin} was with` : "";
}
// "Michael Linford · Founder · Acme": one typeahead row for a Person the operation returned
function upWhoLabel(person) {
  const m = person.metadata || {};
  return [m.name, m.role, m.org].filter(Boolean).join(" · ");
}
// the typeahead's rows for `typed`: the Persons found that are not already chosen, then Add "<typed>" unless the text
// already is, case-insensitively, a found Person's name or alias or a chosen participant's name
function upWhoItems(typed, found, chosen) {
  const text = String(typed || "").trim(), key = text.toLowerCase(), same = s => String(s || "").trim().toLowerCase() === key;
  const picked = new Set((chosen || []).map(p => p.person_id).filter(Boolean));
  const rows = (found || []).filter(p => !picked.has(p.id)).map(person => ({ value: person.id, label: upWhoLabel(person), person }));
  const known = rows.some(r => [(r.person.metadata || {}).name, ...((r.person.metadata || {}).aka || [])].some(same)) || (chosen || []).some(p => same(p.name));
  if (text && !known) rows.push({ value: "", label: `Add "${text}"`, add: text });
  return rows;
}
// the participants after adding `entry` ({person_id?, name}): unchanged when the same Person or name is already there
function upWhoAdd(list, entry) {
  const name = String(entry.name || "").trim(), current = list || [];
  if (!name || current.some(p => (entry.person_id && p.person_id === entry.person_id) || p.name.toLowerCase() === name.toLowerCase())) return current;
  return [...current, entry.person_id ? { person_id: entry.person_id, name } : { name }];
}
// one id per submit, same shape as the server's upload ids (UTC minute + 4 base32 chars)
function upBatchId() {
  const d = new Date(), p = n => String(n).padStart(2, "0");
  let r = "";
  for (let i = 0; i < 4; i++) r += UP_B32[Math.floor(Math.random() * UP_B32.length)];
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}-${p(d.getUTCHours())}${p(d.getUTCMinutes())}-${r}`;
}

// ---------------------------------------------------------------- state
let upForm = { text: "", files: [], notes: "", topic: "", group: ls("upgroup") || "polished", origin: "", occurred_on: "", participants: [] };
let upWhoFound = { query: "", people: [] };   // the last typeahead answer, kept so a refocus can show it again
let upWhoTimer = null, upWhoMenu = null;
let upTextRequest = null;
let upError = "";
let upBusy = false;           // a submit is running
let upDone = [];              // this session's results, newest first: {ok, kind, label, id, path, error}
let upBiz = "";
let upScope = "";
// the context route for the current scope: Global has no business, and its traffic stays off the generic operations proxy
const upEndpoint = () => state.scope === "global" ? "/root/context" : `/biz/${encodeURIComponent(state.biz)}/context`;

// ---------------------------------------------------------------- render
// called by tasks.js's tSetView(v) on tab-entry ("uploads") and by its 500ms business-change interval
function tPaintUploads() { upOpenView(); }
function upOpenView() {
  if (upBiz !== state.biz || upScope !== state.scope) { upForm.files.forEach(x=>x.url && URL.revokeObjectURL(x.url)); upBiz = state.biz; upScope = state.scope; upTextRequest = null; upError=""; upForm = { ...upForm, text: "", files: [], notes: "", topic: "", tasks: [], origin: "", occurred_on: "", participants: [] }; upDone = []; }
  upRender();
}

function upRender() {
  const body = $("#tbody"); if (!body) return;
  if (!state.biz && state.scope !== "global") { body.className = "tbody"; body.innerHTML = `<div class="tempty"><div class="ic">${SVG_TINBOX}</div><p class="t">Pick a project first.</p></div>`; return; }
  body.className = "tbody up";
  body.innerHTML = upFormHtml() + upDoneHtml();
  upWire(body);
  if (state.scope !== "global") {
    const taskButton=body.querySelector('#ctxuploadtasks'); if(taskButton)taskButton.onclick=()=>workChooseTasks(state.biz,upForm.tasks || [],tasks=>{upForm.tasks=tasks;upRender();});
    body.querySelectorAll('[data-upload-resource]').forEach(host=>workRelatedChips(host,state.biz,'uploads',host.dataset.uploadResource));
  }
}

function upFormHtml() {
  const f = upForm, mode = upMode(f), media = mode === "media", global = state.scope === "global";
  return `<div class="upfields upcontext"><div class="upf"><label class="upl" for="uptext">Context</label></div></div>
  <div class="upbox${media ? " media" : ""}" id="upbox" tabindex="0">
    <button type="button" class="btn upattach${mode === 'text' || f.origin || global ? ' concealed' : ''}" id="upattach"${global ? " hidden" : ""}>${aicon('paperclip','currentColor',16)}Attach files</button><input type="file" id="uppick" multiple accept="image/*,video/*,.csv,.tsv,.xlsx,.xls,.ods,.json,.jsonl,.txt" hidden>
    <textarea class="uptext" id="uptext" aria-label="Context" spellcheck="false"${media ? " readonly" : ""}>${esc(f.text)}</textarea>
    ${media ? `<div class="upstrip" id="upstrip">${f.files.map((x, i) => upThumbHtml(x, i)).join("")}</div>` : ""}
    <button class="upbtn" id="upclear" type="button"${mode === "empty" ? " hidden" : ""}>Clear</button>
  </div>
  <div class="upfields">
    <div class="upf"><label class="upl" for="upnotes">Notes <span class="opt">optional</span></label><textarea class="upinp" id="upnotes" rows="3">${esc(f.notes)}</textarea></div>
  </div>
  ${mode === "media" ? "" : upEncounterHtml(f)}
  ${!global && (f.tasks || []).length ? `<button type="button" class="quiet" id="ctxuploadtasks">${f.tasks.map(task=>esc(task.title)).join(', ')}</button>` : ""}
  <div class="upact"><button class="upsend" id="upsend" type="button"${mode === "empty" || upBusy ? " disabled" : ""}>${ICO_UPLOAD}<span id="upsendlbl">${esc(upSendLabel(f, upBusy))}</span></button><span class="uperr" id="uperr" ${upError ? "" : "hidden"}>${esc(upError)}</span></div>`;
}
// the kind switch (Context is the default and means no origin) and, for a call or meeting, who it was with and when
function upEncounterHtml(f) {
  const kinds = UP_ENCOUNTERS.map(([value, text]) => `<button type="button" role="radio" class="upsegb${f.origin === value ? " on" : ""}" data-origin="${value}" aria-checked="${f.origin === value}">${text}</button>`).join("");
  return `<div class="upfields upenc"><div class="upseg" id="upkind" role="radiogroup" aria-label="Kind of context">${kinds}</div>${f.origin ? `
    <div class="upf"><label class="upl" for="upwho">Who was the meeting with</label><div class="upwhochips" id="upwhochips">${upChipsHtml(f)}</div><input class="upinp" id="upwho" type="text" autocomplete="off" role="combobox" aria-autocomplete="list" aria-haspopup="listbox" aria-expanded="false"></div>
    <div class="upf"><label class="upl" for="upwhen">When <span class="opt">optional</span></label><input type="date" class="upinp" id="upwhen" value="${esc(f.occurred_on)}"></div>` : ""}</div>`;
}
// a Person picked from the list carries a small person icon; a free-typed name does not
function upChipsHtml(f) {
  return (f.participants || []).map((p, i) => `<span class="upchip">${p.person_id ? UP_PERSON : ""}${esc(p.name)}<button class="x" type="button" data-i="${i}" aria-label="Remove">×</button></span>`).join("");
}
function upThumbHtml(x, i) {
  const k = upKind(x.file), url = x.url || "";
  const inner = k === "image" && url ? `<img src="${esc(url)}" alt="">` : k === "video" && url ? `<video src="${esc(url)}" muted playsinline preload="metadata"></video>` : `<span class="upext">${esc((x.file.name.split(".").pop() || "file").toLowerCase())}</span>`;
  return `<div class="upth" data-i="${i}" title="${esc(x.file.name)}">${inner}<span class="upnm">${esc(x.file.name)}</span><span class="upsz">${esc(fmtBytes(x.file.size))}</span><button class="uprm" type="button" data-i="${i}" aria-label="remove">×</button></div>`;
}
function upDoneHtml() {
  if (!upDone.length) return "";
  return `<div class="updone"><div class="upeyebrow">This session</div>${upDone.slice(0, 40).map(r => `<div class="updrow${r.ok ? "" : " bad"}"><span class="updk">${r.kind === "text" ? r.origin || "context" : r.kind === "data" ? "data" : "asset"}</span><span class="updl">${esc(r.label)}${r.who ? ` <span class="upwith">with ${esc(r.who)}</span>` : ""}</span><span class="updr">${r.ok ? "Uploaded" : esc(r.error)}</span></div>${r.ok&&r.resourceId?`<div class="up-work" data-upload-resource="${esc(r.resourceId)}" aria-label="Related work"></div>`:''}`).join("")}</div>`;
}

// what changes while typing (hint, send label/enabled, clear visibility), patched without a rebuild
function upPatch() {
  const f = upForm, mode = upMode(f);
  const attach=$("#upattach");if(attach){attach.classList.toggle("concealed",mode==="text" || !!f.origin);attach.disabled=mode==="text" || !!f.origin || upBusy;}
  const s = $("#upsend"), sl = $("#upsendlbl");
  if (sl) sl.textContent = upSendLabel(f, upBusy);
  if (s) s.disabled = mode === "empty" || upBusy || !!upNeedsWho(f);
  const c = $("#upclear"); if (c) c.hidden = mode === "empty";
}

// ---------------------------------------------------------------- wiring
function upWire(root) {
  const ta = root.querySelector("#uptext"), box = root.querySelector("#upbox"), err = root.querySelector("#uperr");
  const fail = t => { upError=t || ""; if (!err) return; err.hidden = !t; err.textContent = t || ""; };
  const addFiles = list => {
    const files = Array.from(list || []).filter(x => x && x.size >= 0 && (x.type.startsWith("image/") || x.type.startsWith("video/") || UP_IMAGE_EXT.test(x.name) || UP_VIDEO_EXT.test(x.name) || UP_DATA_EXT.test(x.name)));
    if (!files.length) return false;
    if (state.scope === "global") { toast("Media and data files belong to a project"); return true; }
    const combined=[...upForm.files.map(x=>x.file),...files];
    if(combined.some(file=>upKind(file)==='file') && combined.some(file=>upKind(file)!=='file')){toast('Attach data files or media in separate uploads.');return true;}
    const why = upRefuse(upForm, "media");
    if (why) { toast(why); return true; }
    upForm.files = upForm.files.concat(files.map(file => ({ file, url: upKind(file)==='file'?'':URL.createObjectURL(file) })));
    upRender();
    return true;
  };
  if (ta) {
    ta.oninput = () => {
      if (upMode(upForm) === "media") { ta.value = ""; return; }
      upForm.text = ta.value;
      upPatch();
    };
    ta.onpaste = e => {
      const dt = e.clipboardData; if (!dt) return;
      const files = Array.from(dt.files || []);
      if (files.length) { e.preventDefault(); addFiles(files); return; }
      if (upMode(upForm) === "media") { e.preventDefault(); toast(upRefuse(upForm, "text")); }
    };
    ta.onkeydown = e => { if ((e.ctrlKey || e.metaKey) && e.key === "Enter") { const b = $("#upsend"); if (b && !b.disabled) b.click(); } };
    if (upMode(upForm) === "empty") ta.focus();
  }
  if (box) {
    box.ondragover = e => { e.preventDefault(); box.classList.add("drag"); };
    box.ondragleave = () => box.classList.remove("drag");
    box.ondrop = e => { e.preventDefault(); box.classList.remove("drag"); if (!addFiles(e.dataTransfer && e.dataTransfer.files)) toast("Choose media, CSV, spreadsheet, JSON or text files."); };
    box.onpaste = e => { if (e.target === ta) return; const files = Array.from((e.clipboardData && e.clipboardData.files) || []); if (files.length) { e.preventDefault(); addFiles(files); } };
  }
  const pick = root.querySelector("#uppick");
  root.querySelector("#upattach").onclick=()=>pick.click();
  if (pick) pick.onchange = () => { if (!addFiles(pick.files)) toast("Choose media, CSV, spreadsheet, JSON or text files."); pick.value = ""; };
  const clear = root.querySelector("#upclear");
  if (clear) clear.onclick = () => { upForm.files.forEach(x => x.url && URL.revokeObjectURL(x.url)); upForm.files = []; upForm.text = ""; upRender(); };
  root.querySelectorAll(".uprm").forEach(b => b.onclick = () => {
    const i = +b.dataset.i, x = upForm.files[i];
    if (x && x.url) URL.revokeObjectURL(x.url);
    upForm.files.splice(i, 1); upRender();
  });
  const notes = root.querySelector("#upnotes");
  if (notes) notes.oninput = () => { upForm.notes = notes.value; };
  const send = root.querySelector("#upsend");
  if (send) send.onclick = () => upSubmit(fail);
  upWireEncounter(root);
  upPatch();
}

// the Context | Call | Meeting switch, the "when" date and the participant typeahead. The typeahead follows
// the Task tag picker: the menu is rebuilt on focus and on each answer, and closes 150ms after blur.
function upWireEncounter(root) {
  clearTimeout(upWhoTimer); if (upWhoMenu) upWhoMenu.close();
  root.querySelectorAll("#upkind [data-origin]").forEach(b => b.onclick = () => { upForm.origin = b.dataset.origin; upRender(); });
  const when = root.querySelector("#upwhen");
  if (when) when.oninput = when.onchange = () => { upForm.occurred_on = when.value; };
  const who = root.querySelector("#upwho"), chips = root.querySelector("#upwhochips");
  if (!who || !chips) return;
  const paint = () => {
    chips.innerHTML = upChipsHtml(upForm);
    upPatch();
  };
  // one handler on the row: the chips are drawn from the state on every rebuild of the form, so buttons bound one by one would go dead
  chips.onclick = e => {
    const x = e.target.closest && e.target.closest(".x");
    if (!x) return;
    upForm.participants.splice(+x.dataset.i, 1);
    paint();
    who.focus();
  };
  const add = entry => {
    clearTimeout(upWhoTimer); if (upWhoMenu) upWhoMenu.close();
    const before = upForm.participants;
    upForm.participants = upWhoAdd(before, entry);
    if (upForm.participants === before) toast("Already on the list");
    upWhoFound = { query: "", people: [] };
    who.value = "";
    paint();
    who.focus();                                   // a mouse pick blurs the field; keep it ready for the next name
  };
  const suggest = () => {
    if (upWhoMenu) upWhoMenu.close();
    const typed = who.value.trim();
    if (typed.length < 2 || upWhoFound.query !== typed) return;
    const rows = upWhoItems(typed, upWhoFound.people, upForm.participants);
    if (!rows.length) return;
    who.setAttribute("aria-expanded", "true");
    upWhoMenu = dropdown(who, rows, (value, row) => add(row.add ? { name: row.add } : { person_id: row.person.id, name: (row.person.metadata || {}).name }),
      null, { allowTyping: true, onClose: () => who.setAttribute("aria-expanded", "false") });
  };
  who.addEventListener("focus", suggest);
  who.addEventListener("input", () => {
    clearTimeout(upWhoTimer);
    const typed = who.value.trim();
    // rows built for older text must not stay open: Enter would pick the highlighted one
    if (upWhoMenu && (typed.length < 2 || upWhoFound.query !== typed)) upWhoMenu.close();
    if (typed.length < 2) return;
    upWhoTimer = setTimeout(async () => {
      const biz = state.biz, scope = state.scope;
      let people = [];
      try {
        const found = await pubRead(scope === 'global' ? {kind: 'root'} : {kind: 'business', id: biz},
          'context_find', {query: typed, limit: 8}, pageReads.signal);
        people = (found.items || []).map(pubPerson).filter(Boolean).map(person => ({
          id: '', metadata: {name: person.name, role: person.role, org: person.org}
        }));
      } catch (_) { people = []; }
      // a newer keystroke, another business or scope, a rebuilt form or a field the user has left owns the menu now
      if (!who.isConnected || document.activeElement !== who || state.biz !== biz || state.scope !== scope || who.value.trim() !== typed) return;
      upWhoFound = { query: typed, people };
      suggest();
    }, 250);
  });
  who.addEventListener("blur", () => setTimeout(() => { if (upWhoMenu) upWhoMenu.close(); }, 150));
  // With the menu open the dropdown owns Enter (it picks the highlighted row). Here Enter only adds the typed name
  // and never submits: sending stays on the button and Ctrl+Enter in the text.
  who.addEventListener("keydown", e => {
    if (e.key !== "Enter" || e.isComposing) return;
    e.preventDefault();
    const typed = who.value.trim();
    if (typed) add({ name: typed });
  });
}

// ---------------------------------------------------------------- submit
async function upSubmit(fail) {
  if (upBusy || (!state.biz && state.scope !== "global")) return;
  const biz = state.biz, f = upForm, mode = upMode(f);
  if (mode === "empty") return fail("nothing to upload");
  if (upNeedsWho(f)) return fail(upNeedsWho(f));
  fail("");
  upBusy = true; upRender();
  try {
    if (mode === "text") {
      const payload=upPayload(f);
      const signature=JSON.stringify([biz,payload]);
      if(upTextRequest?.signature!==signature)upTextRequest={signature,key:newRequestId()};
      let r; try { r = await post(upEndpoint() + "/upload", {...payload,idempotency_key:upTextRequest.key}); }
      catch (e) { r = { error: "Couldn't confirm. Try again" }; }
      const ok = !!(r && !r.error);
      upDone.unshift({ ok, kind: "text", origin: f.origin, who: f.origin ? f.participants.map(p => p.name).join(", ") : "", label: f.notes || f.topic || f.text.trim().slice(0, 60), id: ok ? r.id : "", resourceId:ok?r.document_id || r.id:'', error: ok ? "" : r && r.error === "Couldn't confirm. Try again" ? r.error : "Couldn't save. Try again" });
      if (ok) { upTextRequest=null; toast(r.warn ? "Added to the Context queue, with a warning." : "Added to the Context queue."); upForm = { ...upForm, text: "", notes: "", topic: "", tasks: [], origin: "", occurred_on: "", participants: [] }; }
      else fail(upDone[0].error);
    } else {
      const batch = upBatchId(), topic = upTopicSlug(f.topic), n = f.files.length;
      let okN = 0;
      for (let i = 0; i < n; i++) {
        const x = f.files[i], b = $("#upsendlbl"); if (b) b.textContent = `Uploading ${i + 1}/${n}…`;
        const r = await upSendFile(biz, x.file, { group: f.group, topic, notes: f.notes, batch, task_ids:(f.tasks || []).map(task=>task.id) });
        const ok = !!(r && !r.error);
        if (ok) okN++;
        upDone.unshift({ ok, kind: upKind(x.file)==="file"?"data":"media", label: x.file.name, id: ok ? r.id : "", resourceId:ok?r.id:'', path: ok ? r.path : "", error: ok ? "" : "Couldn't save. Try again" });
      }
      toast(okN === n ? `${n} file${n === 1 ? "" : "s"} uploaded` : `${okN}/${n} uploaded; see the list`);
      if (okN === n) { f.files.forEach(x => x.url && URL.revokeObjectURL(x.url)); upForm = { ...upForm, files: [], notes: "", topic: "", tasks: [], origin: "", occurred_on: "", participants: [] }; }
      else { upForm.files = f.files.filter((x, i) => !upDone.slice(0, n).reverse()[i].ok); }
    }
  } finally {
    upBusy = false; upRender();
    if (typeof refreshSide === "function") refreshSide();
  }
}
async function upSendFile(biz, file, fields) {
  if(upKind(file)==='file'){
    if(!file.size || file.size>20*1024*1024)return {error:'Choose a data file between 1 byte and 20 MiB.'};
    try {
      const content=await new Promise((resolve,reject)=>{const r=new FileReader();r.onload=()=>resolve(r.result.split(',')[1]);r.onerror=()=>reject(new Error('Could not read file'));r.readAsDataURL(file);});
      const result=await workCall(biz,'data_uploads_upload',{filename:file.name,content_b64:content,notes:fields.notes,task_ids:fields.task_ids || []});
      return result.upload;
    }catch(error){return {error:error.message};}
  }
  const fd = new FormData();
  Object.entries(fields).forEach(([k, v]) => fd.append(k, k === "task_ids" ? JSON.stringify(v || []) : v || ""));
  fd.append("file", file, file.name);
  return api(`/biz/${biz}/assets/upload`, { method: "POST", body: fd }).catch(() => ({ error: "could not reach the server" }));
}
