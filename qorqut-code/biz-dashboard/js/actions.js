// Completed actions: the owner's capture popover (floating bottom-right button) and the
// Actions list inside the Activity view. Both use the shared core's native actions_* operations through
// the owner's root route. Nothing here infers a category from the page that is open: the owner chooses.
// Times are San Juan time (js/san-juan-time.js); the server owns the recording instant.
const AC_CATEGORIES = [
  ["global-systems", AICONS.code],
  ["global-operational", AICONS.globe],
  // The storefront glyph from the Learning scope picker.
  ["business-operational", '<path d="m2 7 4.41-4.41A2 2 0 0 1 7.83 2h8.34a2 2 0 0 1 1.42.59L22 7"/><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><path d="M15 22v-4a2 2 0 0 0-2-2h-2a2 2 0 0 0-2 2v4"/><path d="M2 7h20"/>'],
];
const AC_IDLE_MS = 10000;
const acSvg = inner => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
const AC_ICON = acSvg('<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4Z"/>');
const acCategoryIcon = id => acSvg((AC_CATEGORIES.find(([key]) => key === id) || [, '<circle cx="12" cy="12" r="3"/>'])[1]);

// One draft at a time. It survives popover close/reopen and pane/tab navigation.
const ac = {
  draft: null, record: null, inflight: null, uncertain: null, queued: false,
  status: "idle", message: "", timer: null, open: false, opener: null, config: null, conflict: false,
};
const acOwner = () => state.session?.principal_kind === "human" && state.session.is_owner === true;
const acOp = (name, body) => post(`/root/operations/${name}`, body).then(coreData);

function acNewDraft() {
  return { text: "", category: null, businesses: [], date: "", hour: "" };
}
function acDraft() { return ac.draft || (ac.draft = acNewDraft()); }

// ---------------------------------------------------------------- global capture button
function acMountButton() {
  let button = document.querySelector("[data-ac-capture]");
  if (!button) {
    button = document.createElement("button");
    button.type = "button"; button.className = "ac-capture";
    button.setAttribute("data-ac-capture", "");
    button.setAttribute("aria-label", "Record completed action");
    button.title = "Record completed action";
    button.setAttribute("aria-haspopup", "dialog");
    button.setAttribute("aria-expanded", "false");
    button.innerHTML = AC_ICON;
    button.onclick = event => { event.stopPropagation(); ac.open ? acClose() : acOpen(button); };
    document.body.appendChild(button);
  }
  button.hidden = !acOwner();
}
// Owner identity may land after the first paint; keep one button across navigation.
acMountButton();
if (typeof loadSession === "function" && (typeof embeddedAgentPane === 'undefined' || !embeddedAgentPane)) loadSession().then(acMountButton).catch(() => {});

// ---------------------------------------------------------------- saved state and payloads
function acRealTime(draft) {
  if (!draft.date) return null;
  return draft.hour === "" ? draft.date : `${draft.date}T${String(draft.hour).padStart(2, "0")}`;
}
// What the draft can persist now. A business category without a business is pending: new text is saved as
// Unclassified and an existing entry keeps its last valid classification until the choice is complete.
function acEffective(draft = acDraft(), record = ac.record) {
  let category = draft.category, business_ids = [...draft.businesses].sort();
  const pending = category === "business-operational" && !business_ids.length;
  if (pending) ({ category, business_ids } = record ? record.saved : { category: null, business_ids: [] });
  if (category !== "business-operational") business_ids = [];
  return { text: draft.text.trim(), category, business_ids, real_time: acRealTime(draft), pending };
}
const acSame = (a, b) => a.text === b.text && a.category === b.category && a.real_time === b.real_time && a.business_ids.join() === b.business_ids.join();
function acDirty() {
  const effective = acEffective(), draft = acDraft();
  if (!ac.record) return Boolean(draft.text.trim()) || Boolean(draft.category || draft.date);
  return !acSame(effective, ac.record.saved);
}
function acSavable() {
  const effective = acEffective();
  return Boolean(effective.text) && (!ac.record || !acSame(effective, ac.record.saved));
}
function acAdopt(action) {
  const occurred = action.occurred || {};
  const real_time = occurred.precision === "day" ? occurred.date : occurred.precision === "hour" ? `${occurred.date}T${String(occurred.hour).padStart(2, "0")}` : null;
  ac.record = { id: action.action_id, revision: action.revision, saved: { text: action.text, category: action.category || null,
    business_ids: (action.businesses || []).map(b => b.id).sort(), real_time } };
}

// ---------------------------------------------------------------- save state machine
// Writes are serialized. An uncertain request (network or server failure) is retried with its exact
// payload and idempotency key before anything newer is sent, so a retry can never create a second action.
function acRequest() {
  if (ac.uncertain) return ac.uncertain;
  const effective = acEffective();
  if (!effective.text || (ac.record && acSame(effective, ac.record.saved))) return null;
  const key = newRequestId();
  if (!ac.record) {
    const args = { text: effective.text, ...(effective.category ? { category: effective.category } : {}),
      ...(effective.business_ids.length ? { business_ids: effective.business_ids } : {}),
      ...(effective.real_time ? { real_time: effective.real_time } : {}) };
    return { name: "actions_record", body: { ...args, idempotency_key: key } };
  }
  const saved = ac.record.saved, args = { action_id: ac.record.id };
  if (effective.text !== saved.text) args.text = effective.text;
  if (effective.category !== saved.category || effective.business_ids.join() !== saved.business_ids.join()) {
    args.category = effective.category; args.business_ids = effective.business_ids;
  }
  if (effective.real_time !== saved.real_time) args.real_time = effective.real_time;
  return { name: "actions_update", body: { ...args, idempotency_key: key, expected_revision: ac.record.revision } };
}
const acUncertainError = error => !error.httpStatus || error.httpStatus >= 500 || ["invalid_response", "unavailable", "internal_error"].includes(error.code);

async function acSave({ explicit = false } = {}) {
  clearTimeout(ac.timer); ac.timer = null;
  if (ac.conflict && !explicit) return;
  if (ac.inflight) { ac.queued = true; return ac.inflight; }
  const request = acRequest();
  if (!request) {
    if (!acEffective().text && acDirty()) { ac.status = "blank"; acPaintStatus(); }
    return;
  }
  ac.conflict = false; ac.status = "saving"; ac.message = ""; acPaintStatus();
  const draftRef = ac.draft;
  ac.inflight = (async () => {
    try {
      const data = await acOp(request.name, request.body);
      ac.uncertain = null;
      if (ac.draft === draftRef) acAdopt(data.action);
      ac.status = "saved";
      acListChanged();
    } catch (error) {
      if (acUncertainError(error)) { ac.uncertain = request; ac.status = "error"; ac.message = "Not saved yet. It will retry without creating a duplicate."; }
      else if (error.code === "revision_conflict" && ac.record) {
        ac.uncertain = null; ac.conflict = true; ac.status = "conflict";
        ac.message = "This action changed elsewhere. Your text is kept; press Save to replace the saved version.";
        try { const fresh = await acOp("actions_get", { action_id: ac.record.id }); if (ac.draft === draftRef) acAdopt(fresh.action); } catch (_) { /* keep the draft */ }
      } else { ac.uncertain = null; ac.status = "error"; ac.message = error.message || "Could not save this action."; }
    } finally {
      ac.inflight = null;
    }
  })();
  await ac.inflight;
  acPaint();
  // Edits made while the request was in flight are saved next, from the latest draft.
  // Edits without a save trigger keep waiting for their own 10-second idle timer.
  // A failed request is not chained: its retry waits for the idle timer or an explicit Save.
  if (ac.queued) { ac.queued = false; if (ac.status === "saved") return acSave(); }
  if (ac.uncertain && ac.status === "error") acArm();
}
function acArm() {
  clearTimeout(ac.timer);
  ac.timer = setTimeout(() => acSave(), AC_IDLE_MS);
}
function acEdited() {
  if (ac.status === "saved" || ac.status === "blank") ac.status = "idle";
  acArm(); acPaintStatus();
}

// ---------------------------------------------------------------- popover
function acOpen(opener, action = null) {
  if (!acOwner()) return;
  // Reopening the active action keeps its draft, pending retry, conflict and newest revision: the list row
  // may be older than what this popover already saved. Switching to another action waits for this one.
  if (action && ac.record?.id !== action.action_id) {
    if (ac.inflight || ac.uncertain || ac.conflict || (ac.draft && acDirty())) { toast("Finish saving the current action first."); return; }
    const occurred = action.occurred || {};
    ac.draft = { text: action.text, category: action.category || null, businesses: (action.businesses || []).map(b => b.id),
      date: occurred.precision === "instant" ? "" : occurred.date || "", hour: occurred.precision === "hour" ? String(occurred.hour) : "" };
    acAdopt(action); ac.status = "idle"; ac.message = ""; ac.conflict = false;
  }
  acClose({ save: false, restore: false });
  ac.open = true; ac.opener = opener;
  const pop = document.createElement("div");
  pop.className = "ac-pop"; pop.id = "ac-pop"; pop.setAttribute("role", "dialog"); pop.setAttribute("aria-label", "Completed action");
  document.body.appendChild(pop);
  document.querySelectorAll("[data-ac-capture]").forEach(b => b.setAttribute("aria-expanded", String(b === opener)));
  acPaint();
  pop.querySelector("#ac-text").focus();
  if (!ac.config) acOp("actions_config_get", {}).then(config => { ac.config = config; acPaint(); }).catch(() => {});
  setTimeout(() => document.addEventListener("pointerdown", acOutside, true));
}
function acPlace() {
  const pop = $("#ac-pop"), opener = ac.opener;
  if (!pop || !opener?.isConnected) return;
  const r = opener.getBoundingClientRect(), width = Math.min(380, window.innerWidth - 16);
  pop.style.width = `${width}px`;
  pop.style.left = `${Math.max(8, Math.min(r.right - width, window.innerWidth - width - 8))}px`;
  // The panel grows with its content; measured at its final width, it shifts up to stay in view, and
  // when it is taller than the window it sits at the top margin rather than being capped or scrolled.
  pop.style.top = `${Math.max(8, Math.min(r.top - pop.offsetHeight - 6, window.innerHeight - pop.offsetHeight - 8))}px`;
}
function acOutside(event) {
  const pop = $("#ac-pop");
  if (pop && !pop.contains(event.target) && !event.target.closest?.("[data-ac-capture]")) acClose({ restore: false });
}
function acClose({ save = true, restore = true } = {}) {
  const pop = $("#ac-pop"), opener = ac.opener;
  document.removeEventListener("pointerdown", acOutside, true);
  if (!pop) return;
  pop.remove(); ac.open = false;
  document.querySelectorAll("[data-ac-capture]").forEach(b => b.setAttribute("aria-expanded", "false"));
  // Closing never loses typed text: a savable draft is saved now; the draft stays for reopening.
  if (save && acSavable() && !ac.conflict) acSave();
  if (restore) (opener?.isConnected ? opener : document.querySelector("[data-ac-capture]:not([hidden])"))?.focus();
}
function acNewAction() {
  if (ac.inflight || acDirty() || ac.uncertain) return;
  ac.draft = acNewDraft(); ac.record = null; ac.status = "idle"; ac.message = ""; ac.conflict = false;
  acPaint(); $("#ac-pop #ac-text")?.focus();
}

function acBusinesses() {
  const known = ac.config?.scope?.businesses;
  if (known) return known;
  return (state.bizs || []).filter(b => b.id).map(b => ({ id: b.id, name: b.name || b.slug }));
}
function acStatusText() {
  const effective = acEffective();
  if (ac.status === "saving") return "Saving…";
  if (ac.status === "conflict" || ac.status === "error") return ac.message;
  if (ac.status === "blank") return "Type what was completed; blank text is not saved.";
  if (ac.inflight) return "Saving…";
  if (acDirty()) return effective.text ? "Unsaved changes" : "";
  return ac.record ? "Saved" : "";
}
function acPaintStatus() {
  const pop = $("#ac-pop"); if (!pop) return;
  const dirty = acDirty(), status = pop.querySelector(".ac-status");
  status.textContent = acStatusText();
  status.dataset.state = ac.status;
  pop.querySelector("#ac-save").hidden = !(dirty || ac.conflict || ac.uncertain);
  pop.querySelector("#ac-save").disabled = Boolean(ac.inflight);
  pop.querySelector("#ac-new").hidden = !ac.record || dirty || Boolean(ac.inflight) || Boolean(ac.uncertain);
  acPlace();
}
function acPaint() {
  const pop = $("#ac-pop"); if (!pop) return;
  const draft = acDraft(), today = sjToday(), now = sjParts(Date.now());
  const active = document.activeElement && pop.contains(document.activeElement) ? document.activeElement : null;
  const focused = active ? active.id || active.dataset.acBusiness : null;
  // A background repaint (save completion, config) must not move the caret the owner is typing at.
  const caret = active?.id === "ac-text" ? { start: active.selectionStart, end: active.selectionEnd, direction: active.selectionDirection, scroll: active.scrollTop } : null;
  const businesses = acBusinesses();
  const hours = Array.from({ length: 24 }, (_, h) => h).filter(h => draft.date !== today || h <= now.hour);
  // Replacing the focused editor is not the owner leaving it: its removal must not trigger a blur save.
  const previous = pop.querySelector("#ac-text"); if (previous) previous.onblur = null;
  pop.innerHTML = `<label class="ac-label" for="ac-text">Completed action</label>
    <textarea id="ac-text" class="inp" rows="3" maxlength="2000" placeholder="What was completed?" spellcheck="true">${esc(draft.text)}</textarea>
    <div class="ac-chips" role="group" aria-label="Category (none selected is Unclassified)">${AC_CATEGORIES.map(([id]) =>
      `<button type="button" class="ac-chip ac-scope" data-ac-category="${id}" aria-pressed="${draft.category === id}" aria-label="Scope: ${id}" title="${id}">${acCategoryIcon(id)}</button>`).join("")}</div>
    <fieldset class="ac-businesses" ${draft.category === "business-operational" ? "" : "hidden"}><legend>Businesses</legend>
      ${businesses.length ? businesses.map(b => `<button type="button" class="ac-chip ac-biz" data-ac-business="${esc(b.id)}" aria-pressed="${draft.businesses.includes(b.id)}">${esc(b.name || b.slug || b.id)}</button>`).join("") : '<p class="muted">No businesses available.</p>'}
    </fieldset>
    <div class="ac-time"><input type="date" id="ac-date" class="inp" aria-label="Action date" max="${today}" value="${esc(draft.date)}">
      <label>Hour<select id="ac-hour" class="inp" ${draft.date ? "" : "disabled"}><option value="">Any</option>${hours.map(h => `<option value="${h}" ${String(draft.hour) === String(h) ? "selected" : ""}>${String(h).padStart(2, "0")}:00</option>`).join("")}</select></label>
      ${draft.date ? '<button type="button" class="ghost" id="ac-now">Now</button>' : ""}</div>
    <div class="ac-foot"><span class="ac-status" role="status" aria-live="polite"></span>
      <button type="button" class="btn" id="ac-new" hidden>New action</button><button type="button" class="btn primary" id="ac-save" hidden>Save</button></div>`;
  const text = pop.querySelector("#ac-text");
  text.oninput = () => { acDraft().text = text.value; acEdited(); };
  text.onblur = () => { if (acSavable() && !ac.conflict) acSave(); };
  pop.querySelectorAll("[data-ac-category]").forEach(button => button.onclick = () => {
    const id = button.dataset.acCategory, d = acDraft();
    d.category = d.category === id ? null : id;
    acEdited(); acPaint();
    if (d.category === "business-operational") pop.querySelector("[data-ac-business]")?.focus();
    else pop.querySelector(`[data-ac-category="${id}"]`)?.focus();
  });
  pop.querySelectorAll("[data-ac-business]").forEach(button => button.onclick = () => {
    const d = acDraft(), id = button.dataset.acBusiness, on = !d.businesses.includes(id);
    d.businesses = on ? [...d.businesses, id] : d.businesses.filter(b => b !== id);
    button.setAttribute("aria-pressed", String(on));
    acEdited(); acPaintStatus();
  });
  const date = pop.querySelector("#ac-date"), hour = pop.querySelector("#ac-hour");
  date.onchange = () => { const d = acDraft(); d.date = date.value && date.value <= today ? date.value : ""; if (!d.date) d.hour = ""; acEdited(); acPaint(); pop.querySelector("#ac-date").focus(); };
  hour.onchange = () => { acDraft().hour = hour.value; acEdited(); acPaintStatus(); };
  pop.querySelector("#ac-now")?.addEventListener("click", () => { const d = acDraft(); d.date = ""; d.hour = ""; acEdited(); acPaint(); pop.querySelector("#ac-date").focus(); });
  pop.querySelector("#ac-save").onclick = () => acSave({ explicit: true });
  pop.querySelector("#ac-new").onclick = acNewAction;
  pop.onkeydown = event => {
    if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); acClose(); }
    if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) { event.preventDefault(); acSave({ explicit: true }); }
  };
  if (focused) (pop.querySelector(`#${CSS.escape(focused)}`) || pop.querySelector(`[data-ac-business="${CSS.escape(focused)}"]`))?.focus();
  if (caret) { text.setSelectionRange(caret.start, caret.end, caret.direction); text.scrollTop = caret.scroll; }
  acPaintStatus();
}
window.addEventListener("resize", () => { if (ac.open) acPlace(); });

// ---------------------------------------------------------------- Actions list (Activity view)
const acList = { rows: [], cursor: null, filter: "all", business: "", loading: false, error: "", key: "", seq: 0 };
function acListChanged() { if (typeof tView !== "undefined" && tView === "activity" && acOwner()) acListRefresh(); }
function acListArgs() {
  const args = { limit: 50 };
  if (state.scope !== "global") { const b = tBiz(); if (b.id) args.business = b.id; }
  else if (acList.business) args.business = acList.business;
  if (acList.filter !== "all") args.categories = [acList.filter];
  return args;
}
async function acListRefresh({ more = false } = {}) {
  const args = acListArgs(), key = JSON.stringify(args), seq = ++acList.seq;
  if (more && acList.cursor) args.cursor = acList.cursor;
  acList.loading = true;
  try {
    const data = await acOp("actions_list", args);
    if (seq !== acList.seq) return;
    acList.rows = more ? [...acList.rows, ...data.actions] : data.actions;
    acList.cursor = data.next_cursor; acList.error = ""; acList.key = key;
  } catch (error) {
    if (seq === acList.seq) { acList.error = error.message || "Actions are unavailable."; acList.key = key; }
  } finally {
    if (seq === acList.seq) { acList.loading = false; acListPaint(); }
  }
}
function acOccurred(action) {
  const o = action.occurred || {}, today = sjToday();
  const day = !o.date ? "Undated" : o.date === today ? "Today" : o.date === sjAddDays(today, -1) ? "Yesterday" : sjDayLabel(o.date, { year: "numeric" });
  if (o.precision === "instant" && o.instant) return `${day} · ${sjTime(o.instant)}`;
  if (o.precision === "hour") return `${day} · ${String(o.hour).padStart(2, "0")}:00`;
  return `${day}${o.precision === "day" ? " · time unknown" : ""}`;
}
function acPerson(action) {
  const performed = action.performed_by, recorded = action.recorded_by;
  if (action.attribution === "performed" && performed) return performed.id === recorded?.id ? performed.name : `${performed.name} · recorded by ${recorded?.name || "unknown"}`;
  return recorded ? `Recorded by ${recorded.name}` : "Unknown recorder";
}
function acRowHtml(action, i) {
  const refs = Object.entries(action.refs || {}).filter(([key]) => key !== "task");
  const task = typeof action.refs?.task === "string" && action.legacy ? action.refs.task : "";
  const businesses = action.businesses || [];
  return `<article class="ac-row" data-ac-row="${i}">
    <div class="ac-row-main"><span class="ac-row-cat" title="${esc(action.category || "Unclassified")}">${acCategoryIcon(action.category)}</span>
      <p class="ac-row-text">${esc(action.text)}</p>
      ${task ? `<button class="ltask" type="button" data-ac-task title="Open related task" aria-label="Open related task">${T_VIEW_ICON.tasks}</button>` : ""}
      ${action.editable ? `<button type="button" class="ghost ac-edit" data-ac-edit aria-label="Edit action">Edit</button>` : ""}</div>
    <div class="ac-row-meta"><time>${esc(acOccurred(action))}</time><span>${esc(acPerson(action))}</span><span>${esc(action.category || "Unclassified")}</span>${businesses.length ? `<span>${businesses.map(b => esc(b.name || b.slug || b.id)).join(", ")}</span>` : ""}${action.origin && action.origin !== "manual" ? `<span>${esc(action.origin)}</span>` : ""}</div>
    ${refs.length || action.detail ? `<details class="ac-row-details"><summary>Details</summary>${action.detail ? `<p>${esc(action.detail)}</p>` : ""}${refs.map(([key, value]) => `<div><span class="muted">${esc(key)}</span> ${/^https?:\/\//.test(value) ? `<a href="${esc(value)}" target="_blank" rel="noopener">${esc(value)}</a>` : esc(value)}</div>`).join("")}</details>` : ""}
  </article>`;
}
function acListPaint() {
  if (typeof tView === "undefined" || tView !== "activity") return;
  const body = $("#tbody"); if (!body) return;
  // A scope or business switch invalidates the rows on screen; never show another scope's list.
  if (acList.key !== JSON.stringify(acListArgs()) && !acList.loading) { acList.rows = []; acList.cursor = null; acList.error = ""; acListRefresh(); }
  const global = state.scope === "global";
  const filters = `<div class="ac-filters"><div class="ttabs ac-filter" role="group" aria-label="Category filter">${[["all", "All"], ...AC_CATEGORIES.map(([id]) => [id, id]), ["unclassified", "Unclassified"]].map(([id, label]) =>
      `<button type="button" class="ttab ac-filter-btn${acList.filter === id ? " on" : ""}" data-ac-filter="${id}" aria-pressed="${acList.filter === id}">${esc(label)}</button>`).join("")}</div>
    ${global ? `<select class="inp" id="ac-filter-business" aria-label="Business filter"><option value="">All businesses</option>${acBusinesses().map(b => `<option value="${esc(b.id)}" ${acList.business === b.id ? "selected" : ""}>${esc(b.name || b.slug || b.id)}</option>`).join("")}</select>` : ""}
    <span class="muted ac-zone">${esc(SJ_LABEL)}</span></div>`;
  let list;
  if (acList.error && !acList.rows.length) list = tEmptyHtml(acList.error);
  else if (!acList.rows.length) list = tEmptyHtml(acList.loading ? "Loading actions…" : "No completed actions yet.");
  else list = `<div class="ac-list">${acList.rows.map(acRowHtml).join("")}</div>${acList.cursor ? '<button class="btn" id="ac-more" type="button">Load more</button>' : ""}`;
  body.innerHTML = filters + (acList.error && acList.rows.length ? `<p class="muted" role="alert">${esc(acList.error)}</p>` : "") + list;
  body.querySelectorAll("[data-ac-filter]").forEach(button => button.onclick = () => { acList.filter = button.dataset.acFilter; acList.rows = []; acList.cursor = null; acListPaint(); acListRefresh(); });
  const business = body.querySelector("#ac-filter-business");
  if (business) business.onchange = () => { acList.business = business.value; acList.rows = []; acList.cursor = null; acListRefresh(); };
  body.querySelector("#ac-more")?.addEventListener("click", () => acListRefresh({ more: true }));
  body.querySelectorAll("[data-ac-row]").forEach(row => {
    const action = acList.rows[Number(row.dataset.acRow)];
    row.querySelector("[data-ac-edit]")?.addEventListener("click", event => acOpen(document.querySelector("[data-ac-capture]:not([hidden])") || event.currentTarget, action));
    row.querySelector("[data-ac-task]")?.addEventListener("click", () => acOpenTask(action));
  });
}
// A linked task lives in the action's immutable storage workspace (Global or one business), never in a
// workspace inferred from the action's target businesses or from the business that is open now.
async function acOpenTask(action) {
  const storage = action.legacy, id = action.refs?.task;
  if (!storage || !id) return;
  try {
    let biz = null;
    if (storage.business_id) {
      biz = (state.bizs || []).find(b => (b.id || b.business_id) === storage.business_id)?.slug;
      if (!biz) throw new Error("This task’s business is unavailable.");
    }
    await taskProgressOpenReference(biz, { type: "task", id });
  } catch (error) { toast(error.message || "Task unavailable."); }
}
// The owner's Activity view lists canonical actions; other viewers keep the business log.
function acActivityActive() { return acOwner(); }
