// ---------------------------------------------------------------- uploads (right panel tab)
// The "Uploads" tab of the tasks panel (see js/tasks.js T_VIEWS/tSetView): one paste box that takes either
// text or media, never both. Text goes the normal context route (POST /biz/<b>/context/upload -> bizctx dump
// -> context/_inbox -> the filer). Media goes to assets/<polished|drafted>/[<topic>/] through
// POST /biz/<b>/assets/upload, one multipart call per file, every file of one submit sharing the same
// notes, topic and batch id (the server stamps them into assets/uploads.jsonl). tasks.js calls in:
// tPaintUploads() on tab entry and business change; nothing here autosaves, so there is no flush.
//
// Pure helpers up top (tests/test_uploads.js drives them headless); DOM below.

const UP_GROUPS = [["polished", "Polished", "approved brand material; agents read it"], ["drafted", "Drafted", "raw material for agents to work from"]];
const UP_VIDEO_EXT = /\.(mp4|mov|webm|m4v|mkv|avi)$/i;
const UP_IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|avif|bmp)$/i;
const UP_B32 = "0123456789abcdefghjkmnpqrstvwxyz";

// what the form holds: "empty" | "text" | "media"
function upMode(f) {
  if (f.files && f.files.length) return "media";
  return String(f.text || "").trim() ? "text" : "empty";
}
// why a paste of `kind` ("text" | "media") may not go into the form right now, "" when it may
function upRefuse(f, kind) {
  const m = upMode(f);
  if (kind === "media" && m === "text") return "clear the text first; one upload is text or media";
  if (kind === "text" && m === "media") return "remove the media first; one upload is text or media";
  return "";
}
// every topic path of the /context tree, depth-first, for the topic datalist
function upTopicPaths(tree) {
  const out = [];
  const walk = n => { if (!n) return; if (n.path) out.push(n.path); (n.children || []).forEach(walk); };
  walk(tree);
  return out;
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
// the hint under the box and the send button's label: shared by the full render and the in-place patch
// that runs while typing (a rebuild there would replace the textarea and drop focus)
function upHint(f) {
  const m = upMode(f);
  if (m === "empty") return "paste text, images or videos here — or drop files, or pick them";
  if (m === "text") return `${f.text.trim().length.toLocaleString()} chars → context; the filer splits it by topic`;
  return `${f.files.length} file${f.files.length === 1 ? "" : "s"} → assets/${f.group}${f.topic ? "/" + upTopicSlug(f.topic) : ""}`;
}
function upSendLabel(f, busy) {
  if (busy) return "Uploading…";
  return upMode(f) === "media" ? `Upload ${f.files.length} to assets` : "Upload to context";
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
let upTopics = [];            // topic paths of the current business's context tree
let upBusy = false;           // a submit is running
let upDone = [];              // this session's results, newest first: {ok, kind, label, id, path, error}
let upBiz = "";

// ---------------------------------------------------------------- render
// called by tasks.js's tSetView(v) on tab-entry ("uploads") and by its 500ms business-change interval
function tPaintUploads() { upOpenView(); }
function upOpenView() {
  if (upBiz !== state.biz) { upBiz = state.biz; upForm = { ...upForm, text: "", files: [], notes: "", topic: "" }; upTopics = []; upDone = []; }
  upRender();
  if (!state.biz) return;
  const biz = state.biz;
  api(`/biz/${biz}/context`).then(d => {
    if (state.biz !== biz || tView !== "uploads") return;
    upTopics = upTopicPaths(d && d.tree);
    const dl = $("#uptopics"); if (dl) dl.innerHTML = upTopics.map(t => `<option value="${esc(t)}">`).join("");
  });
}

function upRender() {
  const body = $("#tbody"); if (!body) return;
  if (!state.biz) { body.className = "tbody"; body.innerHTML = `<div class="tempty"><div class="ic">${SVG_TINBOX}</div><p class="t">Pick a business first.</p></div>`; return; }
  body.className = "tbody up";
  body.innerHTML = upFormHtml() + upDoneHtml();
  upWire(body);
}

function upFormHtml() {
  const f = upForm, mode = upMode(f), media = mode === "media";
  return `<div class="uphead"><h1>Uploads</h1><p class="upsub">Text goes into context. Images and videos go into assets. One or the other per upload.</p></div>
  <div class="upbox${media ? " media" : ""}" id="upbox" tabindex="0">
    <textarea class="uptext" id="uptext" placeholder="paste anything: notes from a call, a doc, a thread, a decision — or images and videos" spellcheck="false"${media ? " readonly" : ""}>${esc(f.text)}</textarea>
    ${media ? `<div class="upstrip" id="upstrip">${f.files.map((x, i) => upThumbHtml(x, i)).join("")}</div>` : ""}
    <div class="upfoot"><span class="uphint" id="uphint">${esc(upHint(f))}</span><label class="upbtn uppick">pick files<input type="file" id="uppick" multiple accept="image/*,video/*" hidden></label><button class="upbtn" id="upclear" type="button"${mode === "empty" ? " hidden" : ""}>clear</button></div>
  </div>
  <div class="upfields">
    <div class="upf"><label class="upl" for="upnotes">Notes <span class="opt">optional</span></label><input class="upinp" id="upnotes" placeholder="where it came from: call with Sarah, PH launch page" autocomplete="off" value="${esc(f.notes)}"></div>
    <div class="upf"><label class="upl" for="uptopic">Topic <span class="opt">optional</span></label><input class="upinp" id="uptopic" list="uptopics" placeholder="existing topic, or a new one" autocomplete="off" value="${esc(f.topic)}"><datalist id="uptopics">${upTopics.map(t => `<option value="${esc(t)}">`).join("")}</datalist></div>
    <div class="upf${media ? "" : " off"}" id="upgroupf"><span class="upl">Destination</span><div class="upgroups">${UP_GROUPS.map(([k, l, d]) => `<label class="upg${f.group === k ? " on" : ""}" title="${esc(d)}"><input type="radio" name="upgroup" value="${k}"${f.group === k ? " checked" : ""} hidden>${l}</label>`).join("")}</div></div>
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
  return `<div class="updone"><div class="upeyebrow">This session</div>${upDone.slice(0, 40).map(r => `<div class="updrow${r.ok ? "" : " bad"}"><span class="updk">${r.kind === "text" ? "context" : "asset"}</span><span class="updl">${esc(r.label)}</span><span class="updr">${r.ok ? esc(r.path || r.id) : esc(r.error)}</span></div>`).join("")}</div>`;
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
  const h = $("#uphint"); if (h) h.textContent = upHint(f);
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
    const files = Array.from(list || []).filter(x => x && x.size >= 0 && (x.type.startsWith("image/") || x.type.startsWith("video/") || UP_IMAGE_EXT.test(x.name) || UP_VIDEO_EXT.test(x.name)));
    if (!files.length) return false;
    const why = upRefuse(upForm, "media");
    if (why) { toast(why); return true; }
    upForm.files = upForm.files.concat(files.map(file => ({ file, url: URL.createObjectURL(file) })));
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
    box.ondrop = e => { e.preventDefault(); box.classList.remove("drag"); if (!addFiles(e.dataTransfer && e.dataTransfer.files)) toast("only images and videos"); };
    box.onpaste = e => { if (e.target === ta) return; const files = Array.from((e.clipboardData && e.clipboardData.files) || []); if (files.length) { e.preventDefault(); addFiles(files); } };
  }
  const pick = root.querySelector("#uppick");
  if (pick) pick.onchange = () => { if (!addFiles(pick.files)) toast("only images and videos"); pick.value = ""; };
  const clear = root.querySelector("#upclear");
  if (clear) clear.onclick = () => { upForm.files.forEach(x => x.url && URL.revokeObjectURL(x.url)); upForm.files = []; upForm.text = ""; upRender(); };
  root.querySelectorAll(".uprm").forEach(b => b.onclick = () => {
    const i = +b.dataset.i, x = upForm.files[i];
    if (x && x.url) URL.revokeObjectURL(x.url);
    upForm.files.splice(i, 1); upRender();
  });
  const notes = root.querySelector("#upnotes"), topic = root.querySelector("#uptopic");
  if (notes) notes.oninput = () => { upForm.notes = notes.value; };
  if (topic) topic.oninput = () => { upForm.topic = topic.value; upPatch(); };
  root.querySelectorAll('input[name="upgroup"]').forEach(r => r.onchange = () => {
    upForm.group = r.value; ls("upgroup", r.value);
    root.querySelectorAll(".upg").forEach(l => l.classList.toggle("on", l.querySelector("input").value === r.value));
    if (topic) topic.oninput();
  });
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
      upDone.unshift({ ok, kind: "text", label: f.notes || f.topic || f.text.trim().slice(0, 60), id: ok ? r.id : "", error: ok ? "" : (r && r.error) || "upload failed" });
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
        upDone.unshift({ ok, kind: "media", label: x.file.name, id: ok ? r.id : "", path: ok ? r.path : "", error: ok ? "" : (r && r.error) || "upload failed" });
      }
      toast(okN === n ? `${n} file${n === 1 ? "" : "s"} → assets/${f.group}${topic ? "/" + topic : ""}` : `${okN}/${n} uploaded; see the list`);
      if (okN === n) { f.files.forEach(x => x.url && URL.revokeObjectURL(x.url)); upForm = { ...upForm, files: [], notes: "", topic: "" }; }
      else { upForm.files = f.files.filter((x, i) => !upDone.slice(0, n).reverse()[i].ok); }
      if (typeof mkAssetsInvalidate === "function") mkAssetsInvalidate();
    }
  } finally {
    upBusy = false; upRender();
    if (typeof refreshSide === "function") refreshSide();
  }
}
function upSendFile(biz, file, fields) {
  const fd = new FormData();
  Object.entries(fields).forEach(([k, v]) => fd.append(k, v || ""));
  fd.append("file", file, file.name);
  return api(`/biz/${biz}/assets/upload`, { method: "POST", body: fd }).catch(() => ({ error: "could not reach the server" }));
}
