// Completed Actions remain readable through their linked records and discussions.
const AC_CATEGORIES = [
  ["global-systems", AICONS.code],
  ["global-operational", AICONS.globe],
  // The storefront glyph from the Learning scope picker.
  ["business-operational", '<path d="m2 7 4.41-4.41A2 2 0 0 1 7.83 2h8.34a2 2 0 0 1 1.42.59L22 7"/><path d="M4 12v8a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-8"/><path d="M15 22v-4a2 2 0 0 0-2-2h-2a2 2 0 0 0-2 2v4"/><path d="M2 7h20"/>'],
];
const acSvg = inner => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${inner}</svg>`;
const acCategoryIcon = id => acSvg((AC_CATEGORIES.find(([key]) => key === id) || [, '<circle cx="12" cy="12" r="3"/>'])[1]);

const acOp = (name, body) => post(`/root/operations/${name}`, body).then(coreData);

function acOccurred(action) {
  const o = action.occurred || {}, today = sjToday();
  const day = !o.date ? "Undated" : o.date === today ? "Today" : o.date === sjAddDays(today, -1) ? "Yesterday" : sjDayLabel(o.date, { year: "numeric" });
  if (o.precision === "instant" && o.instant) return `${day} · ${sjTime(o.instant, o.time_zone || SJ_ZONE)}`;
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
      ${action.withdrawn ? '<span class="ac-withdrawn">Withdrawn</span>' : ""}</div>
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
    onFilePreview: ref => agentFilePreviewOpen(ref),
    onOlder: () => load(true),
  });
  const paint = scroll => {
    if (!alive) return;
    const self = state.session?.principal_id;
    chat.render({
      entries: data.messages.map(message => {
        const system = message.kind === "system";
        const speaker = message.author_id ? name(message.author_id) : "System";
        const recorded = message.author_id && message.recorder_id && message.author_id !== message.recorder_id ? ` · ${message.attribution?.kind === 'owner_intent' ? 'interpreted' : 'recorded'} by ${message.recorder_name || name(message.recorder_id)}` : "";
        return { key: message.id, speaker: system ? "notice" : message.author_id && message.author_id === self ? "user" : "agent",
          recorder: message.recorder_kind === 'agent' ? 'agent' : undefined,
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
// The one Action content reader: both linked Action rows and the record workspace use
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
