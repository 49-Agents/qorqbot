// Completed actions: the owner's capture popover (floating bottom-right button) and the
// Actions list inside the Activity view. Both use the shared core's native actions_* operations through
// the owner's root route. Every save after the first is a correction (a new immutable revision, checked
// against the revision the owner last saw) and removal is a withdrawal; both preserve existing versions.
// Nothing here infers a category from the page that is open: the owner chooses.
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
  return { text: "", category: null, businesses: [], date: "", hour: "", tags: [] };
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
  return { text: draft.text.trim(), category, business_ids, real_time: acRealTime(draft), tags: tagNormalize(draft.tags).sort(), pending };
}
const acSame = (a, b) => a.text === b.text && a.category === b.category && a.real_time === b.real_time && a.business_ids.join() === b.business_ids.join() && (a.tags || []).join("\n") === (b.tags || []).join("\n");
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
  ac.record = { id: action.action_id, revision: action.revision, canChange: action.can_change !== false && !action.withdrawn, withdrawn: Boolean(action.withdrawn),
    saved: { text: action.text, category: action.category || null, business_ids: (action.businesses || []).map(b => b.id).sort(), real_time,
      tags: tagNormalize(action.tags).sort() } };
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
      ...(effective.real_time ? { real_time: effective.real_time } : {}), ...(effective.tags.length ? { tags: effective.tags } : {}) };
    return { name: "actions_record", body: { ...args, idempotency_key: key } };
  }
  const saved = ac.record.saved, args = { action_id: ac.record.id };
  if (effective.text !== saved.text) args.text = effective.text;
  if (effective.category !== saved.category || effective.business_ids.join() !== saved.business_ids.join()) {
    args.category = effective.category; args.business_ids = effective.business_ids;
  }
  if (effective.real_time !== saved.real_time) args.real_time = effective.real_time;
  if (effective.tags.join("\n") !== saved.tags.join("\n")) args.tags = effective.tags;
  return { name: "actions_correct", body: { ...args, idempotency_key: key, expected_revision: ac.record.revision } };
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
      if (acUncertainError(error)) { ac.uncertain = request; ac.status = "error"; ac.message = "Couldn't confirm. Try again"; }
      else if (error.code === "revision_conflict" && ac.record) {
        ac.uncertain = null; ac.conflict = true; ac.status = "conflict";
        ac.message = "This action changed elsewhere. Your text is kept; press Save to replace the saved version.";
        try { const fresh = await acOp("actions_get", { action_id: ac.record.id }); if (ac.draft === draftRef) acAdopt(fresh.action); } catch (_) { /* keep the draft */ }
      } else { ac.uncertain = null; ac.status = "error"; ac.message = uiFailure(error); }
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
      date: occurred.precision === "instant" ? "" : occurred.date || "", hour: occurred.precision === "hour" ? String(occurred.hour) : "", tags: tagNormalize(action.tags) };
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
// Withdrawal keeps the action and every revision readable, marked withdrawn; it checks the exact revision seen.
function acWithdraw() {
  const record = ac.record; if (!record?.canChange || ac.inflight) return;
  modal({ stack: true, title: "Withdraw action", body: `<p>The action stays readable as withdrawn and accepts no later change.</p><label class="work-field">Reason (optional)<textarea class="inp" name="rationale" rows="2" maxlength="2000"></textarea></label><div class="ferr" role="alert" hidden></div>`,
    actions: [{ label: "Withdraw", primary: true, onClick: async (close, el) => {
      const box = el.querySelector(".ferr"), rationale = el.querySelector("[name=rationale]").value.trim();
      try {
        await acOp("actions_withdraw", { action_id: record.id, ...(rationale ? { rationale } : {}), expected_revision: record.revision, idempotency_key: newRequestId() });
        close();
        if (ac.record === record) { ac.draft = acNewDraft(); ac.record = null; ac.status = "idle"; ac.message = ""; ac.conflict = false; ac.uncertain = null; }
        acPaint(); acListChanged(); toast("Action withdrawn.");
      } catch (error) { box.textContent = uiFailure(error); box.hidden = false; }
    } }] });
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
  pop.querySelector("#ac-withdraw").hidden = !ac.record || !ac.record.canChange || Boolean(ac.inflight);
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
    <fieldset class="ac-businesses" ${draft.category === "business-operational" ? "" : "hidden"}><legend>Projects</legend>
      ${businesses.length ? businesses.map(b => `<button type="button" class="ac-chip ac-biz" data-ac-business="${esc(b.id)}" aria-pressed="${draft.businesses.includes(b.id)}">${esc(b.name || b.slug || "Project")}</button>`).join("") : '<p class="muted">No projects available.</p>'}
    </fieldset>
    <div class="ac-tags" id="ac-tags"></div>
    <div class="ac-time"><input type="date" id="ac-date" class="inp" aria-label="Action date" max="${today}" value="${esc(draft.date)}">
      <label>Hour<select id="ac-hour" class="inp" ${draft.date ? "" : "disabled"}><option value="">Any</option>${hours.map(h => `<option value="${h}" ${String(draft.hour) === String(h) ? "selected" : ""}>${String(h).padStart(2, "0")}:00</option>`).join("")}</select></label>
      ${draft.date ? '<button type="button" class="ghost" id="ac-now">Now</button>' : ""}</div>
    <div class="ac-foot"><span class="ac-status" role="status" aria-live="polite"></span>
      <button type="button" class="btn" id="ac-withdraw" hidden>Withdraw</button><button type="button" class="btn" id="ac-new" hidden>New action</button><button type="button" class="btn primary" id="ac-save" hidden>Save</button></div>`;
  tagPickerMount(pop.querySelector("#ac-tags"), { biz: null, labels: draft.tags, label: "Tags", onChange: labels => { acDraft().tags = labels; acEdited(); } });
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
  pop.querySelector("#ac-withdraw").onclick = acWithdraw;
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
const acList = { rows: [], cursor: null, filter: "all", business: "", withdrawn: false, loading: false, error: "", key: "", seq: 0 };
function acListChanged() { if (typeof tView !== "undefined" && tView === "activity" && acOwner()) acListRefresh(); }
function acListArgs() {
  const args = { limit: 50 };
  if (state.scope !== "global") { const b = tBiz(); if (b.id) args.business = b.id; }
  else if (acList.business) args.business = acList.business;
  if (acList.filter !== "all") args.categories = [acList.filter];
  if (acList.withdrawn) args.include_withdrawn = true;
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
    if (seq === acList.seq) { acList.error = uiFailure(error, {load: true}); acList.key = key; }
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
  return `<article class="ac-row${action.withdrawn ? " withdrawn" : ""}" data-ac-row="${i}">
    <div class="ac-row-main"><span class="ac-row-cat" title="${esc(action.category || "Unclassified")}">${acCategoryIcon(action.category)}</span>
      <p class="ac-row-text">${esc(action.text)}</p>
      ${task ? `<button class="ltask" type="button" data-ac-task title="Open related task" aria-label="Open related task">${T_VIEW_ICON.tasks}</button>` : ""}
      ${action.withdrawn ? '<span class="ac-withdrawn">Withdrawn</span>' : ""}
      ${action.can_change ? `<button type="button" class="ghost ac-edit" data-ac-edit aria-label="Correct action">Correct</button>` : ""}</div>
    <div class="ac-row-meta"><time>${esc(acOccurred(action))}</time><span>${esc(acPerson(action))}</span><span>${esc(action.category || "Unclassified")}</span>${businesses.length ? `<span>${businesses.map(b => esc(b.name || b.slug || "Project")).join(", ")}</span>` : ""}${(action.tags || []).map(tag => `<span class="ac-tag">${esc(tag)}</span>`).join("")}</div>
    ${refs.length || action.detail ? `<details class="ac-row-details"><summary>Details</summary>${action.detail ? `<p>${esc(action.detail)}</p>` : ""}${refs.map(([key, value]) => `<div><span class="muted">${esc(key)}</span> ${/^https?:\/\//.test(value) ? `<a href="${esc(value)}" target="_blank" rel="noopener">${esc(value)}</a>` : esc(value)}</div>`).join("")}</details>` : ""}
    <details class="ac-row-discussion" data-ac-discussion><summary>Discussion and earlier versions</summary><div class="ac-discussion-body"></div></details>
  </article>`;
}

// The shared discussion of one action plus its immutable revisions. A comment belongs to the discussion only: it
// never creates an action or approves anything, and the installation owner's side conversation stays in the
// private channel, so the shared Chat view is mounted read-only (its whole composer hidden; the backend refuses
// a shared comment with channel_required). Revisions and Follow stay here: they are not Chat controls.
const acDiscussions = new Set();
function acDiscussionsDispose({ detachedOnly = false } = {}) {
  for (const handle of [...acDiscussions]) if (!detachedOnly || !handle.host.isConnected) handle.dispose();
}
function acDiscussionMount(host, action, options = {}) {
  const op = options.op || acOp;
  if (host._acDiscussion) return host._acDiscussion;
  acDiscussionsDispose({ detachedOnly: true });
  const data = { messages: [], participants: [], cursor: null, loading: false };
  let alive = true, seq = 0;
  host.innerHTML = `<div class="ac-revisions"><label>Version<select aria-label="Action version">${Array.from({ length: action.revision }, (_, i) => action.revision - i).map(n => `<option value="${n}">${n === action.revision ? "Current" : `Version ${n}`}</option>`).join("")}</select></label><div class="ac-revision-text" role="status"></div></div>
    <div class="ac-follow"><button type="button" class="quiet" data-follow="true">Follow</button><button type="button" class="quiet" data-follow="false">Unfollow</button></div>
    <div class="ac-chat"></div><p class="muted ac-private-note">Comments from the owner go through the private conversation with Ordubashy.</p><p class="ferr" role="alert" hidden></p>`;
  const error = host.querySelector(".ferr");
  const fail = (e, load = false) => { if (alive) { error.textContent = uiFailure(e, {load}); error.hidden = false; } };
  const person = id => data.participants.find(p => p.id === id);
  const name = id => { const p = person(id); return p ? (p.display_name || p.name || "Unknown") : "Unknown"; };
  const chat = mountChatView(host.querySelector(".ac-chat"), {
    logLabel: "Action discussion", olderLabel: "Older messages",
    onOlder: () => load(true),
  });
  const paint = scroll => {
    if (!alive) return;
    const self = state.session?.principal_id;
    chat.render({
      entries: data.messages.map(message => {
        const system = message.kind === "system";
        const speaker = message.author_id ? name(message.author_id) : "System";
        const recorded = message.author_id && message.recorder_id && message.author_id !== message.recorder_id ? ` · recorded by ${name(message.recorder_id)}` : "";
        return { key: message.id, speaker: system ? "notice" : message.author_id && message.author_id === self ? "user" : "agent",
          text: message.body || "", timestamp: message.created_at, author: { name: speaker + recorded }, showAuthor: true };
      }),
      composer: { hidden: true },
      older: { visible: Boolean(data.cursor), disabled: data.loading, label: "Older messages" },
      empty: { text: data.messages.length ? "" : "No discussion yet." },
    }, { scroll });
  };
  const load = async (more = false) => {
    if (!alive || data.loading) return;
    data.loading = true; const mine = ++seq;
    if (more) paint("preserve");
    try {
      const result = await op("actions_thread_get", { action_id: action.action_id, limit: 20, ...(more && data.cursor ? { cursor: data.cursor } : {}) });
      if (!alive || mine !== seq) return;
      const merged = new Map((more ? [...result.messages, ...data.messages] : result.messages).map(m => [m.id, m]));
      data.messages = [...merged.values()]; data.participants = result.participants || data.participants; data.cursor = result.next_cursor; error.hidden = true;
      data.loading = false; paint(more ? "prepend" : "auto");
    } catch (e) {
      if (!alive || mine !== seq) return;
      data.loading = false; fail(e, true); if (more) paint("preserve");
    }
  };
  host.querySelector("select").onchange = async event => {
    const out = host.querySelector(".ac-revision-text"), revision = Number(event.target.value);
    try {
      const result = await op("actions_get", { action_id: action.action_id, revision });
      if (alive) out.textContent = result.action.text;
    } catch (e) { if (alive) out.textContent = ""; fail(e, true); }
  };
  host.querySelectorAll("[data-follow]").forEach(button => button.onclick = async () => {
    try { await op("actions_follow", { action_id: action.action_id, following: button.dataset.follow === "true", idempotency_key: newRequestId() }); toast(button.dataset.follow === "true" ? "Following this action." : "Unfollowed this action."); if (alive) error.hidden = true; }
    catch (e) { fail(e); }
  });
  const handle = { host, layout: () => { if (alive) chat.layout(); },
    dispose() { if (!alive) return; alive = false; acDiscussions.delete(handle); delete host._acDiscussion; chat.dispose(); } };
  host._acDiscussion = handle; acDiscussions.add(handle);
  paint("auto"); chat.layout();
  load();
  return handle;
}
// The one Action content reader: both the Activity row renderer and the record workspace use
// acRowHtml plus the existing revision/discussion reader, so source text and metadata cannot diverge.
function acActionReader(host, action, options = {}) {
  if (!host) return null;
  const row = document.createElement('article');
  row.className = 'ac-row ac-action-reader';
  const value = {...action, action_id: action.action_id || action.id};
  row.innerHTML = acRowHtml(value, 0);
  const text = row.querySelector('.ac-row-text');
  if (text) text.classList.add('rw-action-text');
  row.querySelector('[data-ac-edit]')?.remove();
  host.replaceChildren(row);
  let discussion = null;
  const details = row.querySelector('[data-ac-discussion]');
  if (details) {
    details.open = true;
    discussion = acDiscussionMount(details.querySelector('.ac-discussion-body'), value, {op: options.op});
  }
  return {dirty: () => false, hasUnsaved: () => false, el: row,
    dispose() { discussion?.dispose?.(); host.replaceChildren(); }};
}

function acListPaint() {
  if (typeof tView === "undefined" || tView !== "activity") return;
  const body = $("#tbody"); if (!body) return;
  // Mounted discussions belong to the rows about to be replaced; none may outlive them or paint late.
  acDiscussionsDispose();
  // A scope or business switch invalidates the rows on screen; never show another scope's list.
  if (acList.key !== JSON.stringify(acListArgs()) && !acList.loading) { acList.rows = []; acList.cursor = null; acList.error = ""; acListRefresh(); }
  const global = state.scope === "global";
  const filters = `<div class="ac-filters"><div class="ttabs ac-filter" role="group" aria-label="Category filter">${[["all", "All"], ...AC_CATEGORIES.map(([id]) => [id, id]), ["unclassified", "Unclassified"]].map(([id, label]) =>
      `<button type="button" class="ttab ac-filter-btn${acList.filter === id ? " on" : ""}" data-ac-filter="${id}" aria-pressed="${acList.filter === id}">${esc(label)}</button>`).join("")}</div>
    ${global ? `<select class="inp" id="ac-filter-business" aria-label="Project filter"><option value="">All projects</option>${acBusinesses().map(b => `<option value="${esc(b.id)}" ${acList.business === b.id ? "selected" : ""}>${esc(b.name || b.slug || "Project")}</option>`).join("")}</select>` : ""}
    <label class="ac-withdrawn-toggle"><input type="checkbox" id="ac-show-withdrawn" ${acList.withdrawn ? "checked" : ""}> Show withdrawn</label>
    <span class="muted ac-zone">${esc(SJ_LABEL)}</span></div>`;
  let list;
  if (acList.error && !acList.rows.length) list = tEmptyHtml(acList.error);
  else if (!acList.rows.length) list = tEmptyHtml(acList.loading ? "Loading actions…" : "No completed actions yet.");
  else list = `<div class="ac-list">${acList.rows.map(acRowHtml).join("")}</div>${acList.cursor ? '<button class="btn" id="ac-more" type="button">Load more</button>' : ""}`;
  body.innerHTML = filters + (acList.error && acList.rows.length ? `<p class="muted" role="alert">${esc(acList.error)}</p>` : "") + list;
  body.querySelectorAll("[data-ac-filter]").forEach(button => button.onclick = () => { acList.filter = button.dataset.acFilter; acList.rows = []; acList.cursor = null; acListPaint(); acListRefresh(); });
  body.querySelector("#ac-show-withdrawn").onchange = event => { acList.withdrawn = event.target.checked; acList.rows = []; acList.cursor = null; acListPaint(); acListRefresh(); };
  const business = body.querySelector("#ac-filter-business");
  if (business) business.onchange = () => { acList.business = business.value; acList.rows = []; acList.cursor = null; acListRefresh(); };
  body.querySelector("#ac-more")?.addEventListener("click", () => acListRefresh({ more: true }));
  body.querySelectorAll("[data-ac-row]").forEach(row => {
    const action = acList.rows[Number(row.dataset.acRow)];
    const discussion = row.querySelector("[data-ac-discussion]");
    discussion.addEventListener("toggle", () => {
      if (!discussion.open) return;
      const host = discussion.querySelector(".ac-discussion-body");
      if (host._acDiscussion) host._acDiscussion.layout(); else acDiscussionMount(host, action);
    });
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
      if (!biz) throw new Error("This task’s project is unavailable.");
    }
    await taskProgressOpenReference(biz, { type: "task", id });
  } catch (error) { toast(uiFailure(error, {load: true})); }
}
// The owner's Activity view lists canonical actions; other viewers keep the business log.
function acActivityActive() { return acOwner(); }
