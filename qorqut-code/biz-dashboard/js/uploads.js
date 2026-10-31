// ---------------------------------------------------------------- uploads (right panel tab)
// The "Uploads" tab of the tasks panel (see js/tasks.js T_VIEWS/tSetView): one paste box that takes either
// text, media or data files, never mixed. Text goes the normal context route (POST /biz/<b>/context/upload -> bizctx dump
// -> context/_inbox -> the filer). Media goes to assets/<polished|drafted>/[<topic>/] through
// POST /biz/<b>/assets/upload, one multipart call per file, every file of one submit sharing the same
// notes, topic and batch id. Data files use the shared data_uploads_upload operation. tasks.js calls in:
// tPaintUploads() on tab entry and business change; nothing here autosaves, so there is no flush.
//
// Pure helpers up top (tests/test_uploads.js drives them headless); DOM below.

const UP_GROUPS = [["polished", "Polished", "approved brand material; agents read it"], ["drafted", "Drafted", "raw material for agents to work from"]];
const UP_VIDEO_EXT = /\.(mp4|mov|webm|m4v|mkv|avi)$/i;
const UP_IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|avif|bmp)$/i;
const UP_DATA_EXT = /\.(csv|tsv|xlsx|xls|ods|json|jsonl|txt)$/i;
const UP_B32 = "0123456789abcdefghjkmnpqrstvwxyz";

// what the form holds: "empty" | "text" | "media"
function upMode(f) {
  if (f.files && f.files.length) return "media";
  return String(f.text || "").trim() ? "text" : "empty";
}
// why a paste of `kind` ("text" | "media") may not go into the form right now, "" when it may
function upRefuse(f, kind) {
  const m = upMode(f);
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
  return "Upload";
}
// one id per submit, same shape as the server's upload ids (UTC minute + 4 base32 chars)
function upBatchId() {
  const d = new Date(), p = n => String(n).padStart(2, "0");
  let r = "";
  for (let i = 0; i < 4; i++) r += UP_B32[Math.floor(Math.random() * UP_B32.length)];
  return `${d.getUTCFullYear()}-${p(d.getUTCMonth() + 1)}-${p(d.getUTCDate())}-${p(d.getUTCHours())}${p(d.getUTCMinutes())}-${r}`;
}

// ---------------------------------------------------------------- state
let upForm = { text: "", files: [], notes: "", topic: "", group: ls("upgroup") || "polished" };
let upBusy = false;           // a submit is running
let upDone = [];              // this session's results, newest first: {ok, kind, label, id, path, error}
let upBiz = "";

// ---------------------------------------------------------------- render
// called by tasks.js's tSetView(v) on tab-entry ("uploads") and by its 500ms business-change interval
function tPaintUploads() { upOpenView(); }
function upOpenView() {
  if (upBiz !== state.biz) { upForm.files.forEach(x=>x.url && URL.revokeObjectURL(x.url)); upBiz = state.biz; upForm = { ...upForm, text: "", files: [], notes: "", topic: "" }; upDone = []; }
  upRender();
}

function upRender() {
  const body = $("#tbody"); if (!body) return;
  if (!state.biz) { body.className = "tbody"; body.innerHTML = `<div class="tempty"><div class="ic">${SVG_TINBOX}</div><p class="t">Pick a business first.</p></div>`; return; }
  body.className = "tbody up";
  body.innerHTML = upFormHtml() + upDoneHtml();
  upWire(body);
  body.querySelectorAll('[data-upload-resource]').forEach(host=>storyRelatedChips(host,state.biz,'uploads',host.dataset.uploadResource));
}

function upFormHtml() {
  const f = upForm, mode = upMode(f), media = mode === "media";
  return `<div class="upfields upcontext"><div class="upf"><label class="upl" for="uptext">Context</label></div></div>
  <div class="upbox${media ? " media" : ""}" id="upbox" tabindex="0">
    <button type="button" class="btn upattach${mode === 'text' ? ' concealed' : ''}" id="upattach">${aicon('paperclip','currentColor',16)}Attach files</button><input type="file" id="uppick" multiple accept="image/*,video/*,.csv,.tsv,.xlsx,.xls,.ods,.json,.jsonl,.txt" hidden>
    <textarea class="uptext" id="uptext" aria-label="Context" spellcheck="false"${media ? " readonly" : ""}>${esc(f.text)}</textarea>
    ${media ? `<div class="upstrip" id="upstrip">${f.files.map((x, i) => upThumbHtml(x, i)).join("")}</div>` : ""}
    <button class="upbtn" id="upclear" type="button"${mode === "empty" ? " hidden" : ""}>Clear</button>
  </div>
  <div class="upfields">
    <div class="upf"><label class="upl" for="upnotes">Notes <span class="opt">optional</span></label><textarea class="upinp" id="upnotes" rows="3">${esc(f.notes)}</textarea></div>
  </div>
  <div class="upact"><button class="upsend" id="upsend" type="button"${mode === "empty" || upBusy ? " disabled" : ""}>${ICO_UPLOAD}<span id="upsendlbl">${esc(upSendLabel(f, upBusy))}</span></button><span class="uperr" id="uperr" hidden></span></div>`;
}
function upThumbHtml(x, i) {
  const k = upKind(x.file), url = x.url || "";
  const inner = k === "image" && url ? `<img src="${esc(url)}" alt="">` : k === "video" && url ? `<video src="${esc(url)}" muted playsinline preload="metadata"></video>` : `<span class="upext">${esc((x.file.name.split(".").pop() || "file").toLowerCase())}</span>`;
  return `<div class="upth" data-i="${i}" title="${esc(x.file.name)}">${inner}<span class="upnm">${esc(x.file.name)}</span><span class="upsz">${esc(fmtBytes(x.file.size))}</span><button class="uprm" type="button" data-i="${i}" aria-label="remove">×</button></div>`;
}
function upDoneHtml() {
  if (!upDone.length) return "";
  return `<div class="updone"><div class="upeyebrow">This session</div>${upDone.slice(0, 40).map(r => `<div class="updrow${r.ok ? "" : " bad"}"><span class="updk">${r.kind === "text" ? "context" : r.kind === "data" ? "data" : "asset"}</span><span class="updl">${esc(r.label)}</span><span class="updr">${r.ok ? esc(r.path || r.id) : esc(r.error)}</span></div>${r.ok&&r.resourceId?`<div class="up-stories" data-upload-resource="${esc(r.resourceId)}" aria-label="Related stories"></div>`:''}`).join("")}</div>`;
}

// The Marketing tab's Assets view calls this from its "Upload assets" button: open the panel on the Uploads
// tab with the destination already set to the folder the owner was looking at. It stops there — no file
// dialog is forced open, since a paste or a drop is the usual way in. One upload path for the whole
// dashboard: Marketing owns no uploader of its own.
function upStartAssets(group, topic) {
  openTasks(true);
  tSetView("uploads");                                  // renders (and resets the form if the business changed)
  if (group && UP_GROUPS.some(([k]) => k === group)) { upForm.group = group; ls("upgroup", group); }
  if (topic) upForm.topic = topic;
  upRender();
}

// what changes while typing (hint, send label/enabled, clear visibility), patched without a rebuild
function upPatch() {
  const f = upForm, mode = upMode(f);
  const attach=$("#upattach");if(attach){attach.classList.toggle("concealed",mode==="text");attach.disabled=mode==="text" || upBusy;}
  const s = $("#upsend"), sl = $("#upsendlbl");
  if (sl) sl.textContent = upSendLabel(f, upBusy);
  if (s) s.disabled = mode === "empty" || upBusy;
  const c = $("#upclear"); if (c) c.hidden = mode === "empty";
}

// ---------------------------------------------------------------- wiring
function upWire(root) {
  const ta = root.querySelector("#uptext"), box = root.querySelector("#upbox"), err = root.querySelector("#uperr");
  const fail = t => { if (!err) return; err.hidden = !t; err.textContent = t || ""; };
  const addFiles = list => {
    const files = Array.from(list || []).filter(x => x && x.size >= 0 && (x.type.startsWith("image/") || x.type.startsWith("video/") || UP_IMAGE_EXT.test(x.name) || UP_VIDEO_EXT.test(x.name) || UP_DATA_EXT.test(x.name)));
    if (!files.length) return false;
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
}

// ---------------------------------------------------------------- submit
async function upSubmit(fail) {
  if (upBusy || !state.biz) return;
  const biz = state.biz, f = upForm, mode = upMode(f);
  if (mode === "empty") return fail("nothing to upload");
  fail("");
  upBusy = true; upRender();
  try {
    if (mode === "text") {
      let r; try { r = await post(`/biz/${biz}/context/upload`, { text: f.text.trim(), notes: f.notes, topic: f.topic }); }
      catch (e) { r = { error: "could not reach the server" }; }
      const ok = !!(r && !r.error);
      upDone.unshift({ ok, kind: "text", label: f.notes || f.topic || f.text.trim().slice(0, 60), id: ok ? r.id : "", resourceId:ok?r.document_id || r.id:'', error: ok ? "" : (r && r.error) || "upload failed" });
      if (ok) { toast(r.warn ? `uploaded ${r.id}, but: ${r.warn}` : `uploaded ${r.id}; filing starts now`); upForm = { ...upForm, text: "", notes: "", topic: "" }; }
      else fail(upDone[0].error);
    } else {
      const batch = upBatchId(), topic = upTopicSlug(f.topic), n = f.files.length;
      let okN = 0;
      for (let i = 0; i < n; i++) {
        const x = f.files[i], b = $("#upsendlbl"); if (b) b.textContent = `Uploading ${i + 1}/${n}…`;
        const r = await upSendFile(biz, x.file, { group: f.group, topic, notes: f.notes, batch });
        const ok = !!(r && !r.error);
        if (ok) okN++;
        upDone.unshift({ ok, kind: upKind(x.file)==="file"?"data":"media", label: x.file.name, id: ok ? r.id : "", resourceId:ok?r.id:'', path: ok ? r.path : "", error: ok ? "" : (r && r.error) || "upload failed" });
      }
      toast(okN === n ? `${n} file${n === 1 ? "" : "s"} uploaded` : `${okN}/${n} uploaded; see the list`);
      if (okN === n) { f.files.forEach(x => x.url && URL.revokeObjectURL(x.url)); upForm = { ...upForm, files: [], notes: "", topic: "" }; }
      else { upForm.files = f.files.filter((x, i) => !upDone.slice(0, n).reverse()[i].ok); }
      if (typeof mkAssetsInvalidate === "function") mkAssetsInvalidate();
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
      const result=await storyCall(biz,'data_uploads_upload',{filename:file.name,content_b64:content,notes:fields.notes});
      return result.upload;
    }catch(error){return {error:error.message};}
  }
  const fd = new FormData();
  Object.entries(fields).forEach(([k, v]) => fd.append(k, v || ""));
  fd.append("file", file, file.name);
  return api(`/biz/${biz}/assets/upload`, { method: "POST", body: fd }).catch(() => ({ error: "could not reach the server" }));
}
