// CRM: a Table kind whose rows are memberships in existing Task records. This file holds the pieces shared by the Tables
// screen, the shared record workspace and Focus: scoped calls, record opening/pinning, the Table team thread and a
// record's activity. Every name, note, URL and recorded email below is untrusted text: it is only ever set with
// textContent or esc(), and URLs pass safeLinkUrl(). Nothing here sends mail or verifies a provider.
const CRM_PAGE = 50;
// The Task fields a CRM column may project (read-only); anything else is a local membership cell.
const CRM_RECORD_FIELDS = [
  ['title', 'Title', 'text'], ['task_type', 'Task type', 'text'], ['status', 'Task state', 'text'], ['overview', 'Overview', 'text'],
  ['customer_kind', 'Customer kind', 'text'], ['customer_state', 'Customer state label', 'text'],
  ['customer_profile.company', 'Company', 'text'], ['customer_profile.contact_name', 'Contact name', 'text'],
  ['customer_profile.email', 'Email', 'text'], ['customer_profile.phone', 'Phone', 'text'],
  ['customer_profile.website', 'Website', 'text'], ['customer_profile.notes', 'Profile notes', 'text'], ['tags', 'Tags', 'json'],
];
const CRM_DEFAULT_COLUMNS = [
  {id: 'record_name', name: 'Name', type: 'text', record_field: 'title'},
  {id: 'roles', name: 'Roles', type: 'json', record_field: 'tags'},
  {id: 'email', name: 'Email', type: 'text', record_field: 'customer_profile.email'},
];
const crmPath = (biz, op) => biz == null ? `/root/operations/${op}` : `/biz/${encodeURIComponent(biz)}/operations/${op}`;
// Global operations declare their scope; a project operation is called in that project's route.
async function crmCall(biz, op, args = {}) {
  const result = await post(crmPath(biz, op), biz == null ? {scope: 'global', ...args} : args);
  if (result?.status === 'signoff_required') throw new Error('This change is awaiting signoff. Your draft is retained.');
  return coreData(result);
}
const crmConflict = error => /conflict|stale|revision|precondition/i.test(String(error?.code || ''));
const crmIsCrm = table => table?.table_kind === 'crm';
const crmValue = value => value == null ? '' : Array.isArray(value) ? value.join(', ') : typeof value === 'object' ? JSON.stringify(value) : String(value);
const crmStageName = (table, id) => id == null ? '' : (table?.crm_config?.stages || []).find(stage => stage.id === id)?.name || id;
const crmScopeKey = ref => ref ? `${ref.type}:${ref.business_id ?? 'global'}:${ref.id}` : '';
const crmActor = actor => !actor ? '' : typeof actor === 'string' ? actor : actor.display_name || actor.name || actor.id || '';
const crmDate = value => { try { return value ? sjDateTime(value) : ''; } catch (_) { return String(value || ''); } };
function crmNode(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
const crmButton = (label, className = 'quiet', text = label) => { const button = crmNode('button', className, text); button.type = 'button'; button.setAttribute('aria-label', label); return button; };
// The Project slug a scoped reference lives in; Global (null) has none and never falls back to a current Project.
const crmSlug = ref => ref?.business_id == null ? null
  : ref.business_slug || (state.bizs || []).find(b => b.id === ref.business_id || b.business_id === ref.business_id || b.slug === ref.business_id)?.slug || null;

// ---------------------------------------------------------------- records
function crmRecordRef(entry) {
  const ref = entry?.record_ref;
  return ref && ref.id ? {type: 'task', id: ref.id, business_id: ref.business_id ?? null} : null;
}
function crmRecordDescriptor(ref, entry) {
  const record = entry?.record;
  return rwRecordFromItem({source: {type: 'task', id: ref.id, business_id: ref.business_id ?? null, label: record?.title,
    task_type: record?.task_type, state: record?.status}}, {can_thread: true});
}
// Shared fields open the existing Task editor inside the shared record workspace; nothing is edited in a CRM cell.
function crmOpenRecord(entry, {onClosed = null} = {}) {
  const ref = crmRecordRef(entry);
  const record = ref && !entry.unavailable ? crmRecordDescriptor(ref, entry) : null;
  if (!record || record.scopeMissing) { toast('This record is unavailable.'); return null; }
  let workspace = null;
  // Every close route (X, Escape, backdrop) goes through the workspace's own unsaved-change guard, and a nested record stacks
  // above this dialog instead of replacing it. The Table is refreshed from the authorized source when the record closes.
  const dialog = modal({title: esc(record.label || 'Record'), width: 1040, body: '<div class="crm-record-host"></div>', stack: true,
    beforeClose: async () => { await workspace?.close(); return !workspace; },
    isDirty: () => !!workspace?.hasUnsaved?.(),
    onClose: () => { workspace?.dispose?.(); try { onClosed?.(); } catch (_) { /* the Table keeps its own state */ } },
    onOpen: el => {
      const host = el.querySelector('.crm-record-host');
      workspace = recordWorkspaceMount(host, {entry: 'threads', visible: () => host.isConnected, onClose: () => dialog.close()});
      void workspace.open(record, {root: true, tab: 'dashboard'}).then(opened => { if (opened === false) toast('This record is unavailable.'); });
    }});
  return dialog;
}
// ---------------------------------------------------------------- Table team thread
// The shared team discussion of a Table. It is never the owner's private conversation: that keeps its own tab and
// draft. The draft below is keyed by Table scope so it survives a re-render, and a failed send keeps it.
const _crmThreadDrafts = new Map();
function crmThreadMount(host, {biz, table: initialTable, navigate = null}) {
  let table = initialTable;
  const key = JSON.stringify(['table-team', biz ?? null, table.id]);
  host.replaceChildren();
  host.classList.add('crm-thread');
  const head = crmNode('div', 'crm-thread-head');
  const title = crmNode('h4', '', 'Team discussion');
  const follow = crmButton('Follow this table', 'quiet crm-follow', 'Follow');
  head.append(title, follow);
  const status = crmNode('p', 'muted crm-thread-status'); status.setAttribute('role', 'status');
  const list = crmNode('ol', 'crm-thread-list'); list.setAttribute('aria-label', 'Team discussion messages');
  const older = crmButton('Older messages', 'quiet', 'Older messages'); older.hidden = true;
  const form = crmNode('form', 'crm-thread-compose');
  const field = crmNode('textarea', 'inp'); field.rows = 3; field.maxLength = 20000; field.setAttribute('aria-label', 'Team message');
  field.placeholder = 'Message the team about this table';
  const send = crmNode('button', 'btn primary', 'Send to team'); send.type = 'submit';
  const error = crmNode('p', 'ferr'); error.setAttribute('role', 'alert'); error.hidden = true;
  const note = crmNode('p', 'muted', 'Visible to everyone who can read this table. Your private conversation with Ordubashy is separate.');
  // The installation owner reads the team discussion here but writes through the private conversation (the backend refuses
  // a shared comment from that actor with channel_required). Until the session is known, the owner side is assumed.
  const ownerSide = () => state.session ? state.session.is_owner === true : true;
  const ownerNote = crmNode('p', 'muted crm-thread-owner', 'You can read this discussion here but cannot post to it. The Chat tab is your private conversation with Ordubashy; nothing you write there is posted or forwarded to the team.');
  ownerNote.hidden = true;
  form.append(field, send);
  host.append(head, status, older, list, form, ownerNote, error, note);
  field.value = _crmThreadDrafts.get(key)?.text || '';
  let messages = [], cursor = null, following = null, alive = true, generation = 0, loading = false, attempt = null, sending = false, unavailable = false;
  const stageNote = change => {
    const stage = change?.stage;
    return stage ? ` · stage ${crmStageName(table, stage.from) || 'unset'} → ${crmStageName(table, stage.to) || 'unset'}` : '';
  };
  const paint = () => {
    list.replaceChildren();
    for (const message of messages) {
      const item = crmNode('li', `crm-thread-message${message.kind === 'system' ? ' crm-thread-system' : ''}`);
      item.dataset.messageId = message.id;
      const who = message.kind === 'system' ? 'System' : crmActor(message.author) || message.author_name || message.author_id || 'Someone';
      item.append(crmNode('strong', '', who), crmNode('time', 'muted', ' ' + crmDate(message.created_at)));
      const body = crmNode('p', 'crm-thread-body'); body.innerHTML = renderSafeLinks(message.body || '');
      item.append(body);
      const change = message.change || message.change_json;
      if (change?.kind === 'crm_membership') {
        const detail = crmNode('p', 'muted crm-thread-change');
        const fields = Array.isArray(change.changed_fields) && change.changed_fields.length
          ? ' · ' + change.changed_fields.map(id => table.columns?.find(column => column.id === id)?.name || id).join(', ') : '';
        const record = change.record && change.record.id ? 'Open entry' : '';
        detail.textContent = `Entry ${change.operation || 'changed'}${fields}${stageNote(change)}`;
        item.append(detail);
        if (record && navigate && !change.unavailable) {
          const open = crmButton('Open record ' + (change.record_label || change.record.id), 'quiet', 'Open record');
          open.onclick = () => navigate({type: 'task', id: change.record.id, business_id: change.record.business_id ?? null});
          item.append(open);
        }
      }
      list.append(item);
    }
    if (!messages.length) list.append(Object.assign(crmNode('li', 'muted', loading ? 'Loading discussion…' : 'No discussion yet.')));
    older.hidden = !cursor;
    form.hidden = ownerSide() || unavailable; ownerNote.hidden = !ownerSide() || unavailable; note.hidden = unavailable;
    follow.textContent = following ? 'Following' : 'Follow';
    follow.setAttribute('aria-pressed', String(!!following));
    follow.hidden = following == null;
  };
  const load = async (more = false) => {
    if (!alive || loading) return;
    loading = true; const own = ++generation;
    if (!more) status.textContent = 'Loading discussion…';
    unavailable = false;
    try {
      const data = await crmCall(biz, 'tables_thread_get', {table_id: table.id, limit: 20, ...(more && cursor ? {cursor} : {})});
      if (!alive || own !== generation) return;
      const merged = new Map((more ? [...(data.messages || []), ...messages] : (data.messages || [])).map(message => [message.id, message]));
      messages = [...merged.values()].sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
      cursor = data.next_cursor ?? data.cursor ?? null;
      following = data.following ?? data.follow?.following ?? data.follow_state?.following ?? following;
      status.textContent = ''; error.hidden = true; loading = false; paint();
    } catch (failure) {
      if (!alive || own !== generation) return;
      loading = false;
      if (accessLost(failure) || sourceGone(failure)) { revoke(); return; }
      status.textContent = uiFailure(failure, {load: true}) + ' ';
      const retry = crmButton('Retry loading discussion', 'quiet', 'Retry'); retry.onclick = () => load(more); status.append(retry);
    }
  };
  // Revoked or removed access: no cached messages, no editor and no kept draft stay on screen. A later refresh may recover.
  function revoke() {
    unavailable = true; messages = []; cursor = null; following = null; attempt = null;
    _crmThreadDrafts.delete(key); field.value = ''; error.hidden = true;
    paint(); status.textContent = 'This discussion is unavailable.';
  }
  if (typeof loadSession === 'function' && !state.session) void loadSession().then(() => { if (alive) paint(); }).catch(() => {});
  field.oninput = () => { if (field.value) _crmThreadDrafts.set(key, {text: field.value}); else _crmThreadDrafts.delete(key); attempt = null; };
  form.onsubmit = async event => {
    event.preventDefault();
    const text = field.value.trim();
    if (!text || sending) return;
    if (attempt?.text !== text) attempt = {text, key: newRequestId()};
    // The composer is frozen while the send is pending, so a success can never erase text typed after it started.
    sending = true; send.disabled = true; field.readOnly = true; error.hidden = true;
    const sent = field.value;
    try {
      await crmCall(biz, 'tables_comment', {table_id: table.id, text, idempotency_key: attempt.key});
      attempt = null;
      if (alive && field.value === sent) { field.value = ''; _crmThreadDrafts.delete(key); }
      if (alive) await load();
    } catch (failure) {
      if (failure.code === 'channel_required') {
        // The backend keeps this actor in the private channel: nothing was posted and the draft is neither moved nor relayed.
        attempt = null;
        if (alive) { error.textContent = 'This account cannot post to the team discussion. Your text is kept here; nothing was posted or moved to your private conversation.'; error.hidden = false; }
      } else if (accessLost(failure) || sourceGone(failure)) { if (alive) revoke(); }
      else {
        if (alive) { error.textContent = uiFailure(failure); error.hidden = false; }
        // The draft and key are kept for an exact retry unless the request itself was refused.
        if (crmConflict(failure) || failure.code === 'validation_error') attempt = null;
      }
    } finally { sending = false; send.disabled = false; field.readOnly = false; }
  };
  older.onclick = () => load(true);
  follow.onclick = async () => {
    const next = !following; follow.disabled = true;
    try { await crmCall(biz, 'tables_follow', {table_id: table.id, following: next, idempotency_key: newRequestId()}); following = next; error.hidden = true; if (alive) paint(); }
    catch (failure) { if (alive) { error.textContent = uiFailure(failure); error.hidden = false; } }
    finally { follow.disabled = false; }
  };
  paint(); void load();
  // A Table configuration change (stage names, columns) must reach membership notes already captured by this thread.
  return {refresh: () => load(), setTable(next) { table = next; if (alive) paint(); }, dispose() { alive = false; generation++; host.replaceChildren(); }, dirty: () => false};
}

// ---------------------------------------------------------------- Task activity and recorded history
const CRM_KIND_LABEL = {email: 'Recorded email', message: 'Recorded message', development: 'Recorded development'};
function crmActivityLabel(item) {
  if (item.origin === 'recorded') return CRM_KIND_LABEL[item.kind] || 'Recorded note';
  if (item.origin === 'crm_membership') return 'CRM membership';
  return ({signoff: 'Signoff', action: 'Action', task: 'Linked task', table: 'Table', document: 'Document', material: 'Document'})[item.source?.type] || (item.origin === 'linked' ? 'Linked record' : 'Discussion');
}
function crmActivityItem(item, navigate) {
  const li = crmNode('li', `crm-activity-item crm-origin-${/^[a-z_]+$/.test(item.origin || '') ? item.origin : 'own'}`);
  li.dataset.activityId = item.id;
  const head = crmNode('div', 'crm-activity-head');
  head.append(crmNode('strong', '', crmActivityLabel(item)), crmNode('time', 'muted', ' ' + crmDate(item.occurred_at)));
  const actor = crmActor(item.actor);
  if (actor) head.append(crmNode('span', 'muted', ' · ' + actor));
  li.append(head);
  li.append(crmNode('p', 'crm-activity-summary', item.summary || ''));
  if (item.body) {
    const details = crmNode('details', 'crm-activity-body'); details.append(crmNode('summary', '', 'Show text'));
    details.append(crmNode('pre', '', item.body)); li.append(details);
  }
  if (item.origin === 'recorded') {
    const meta = crmNode('p', 'muted crm-activity-meta');
    meta.textContent = `Recorded${item.recorded_at ? ' ' + crmDate(item.recorded_at) : ''}${actor ? ' by ' + actor : ''}. This is an agent's note of the ${item.kind || 'message'}; it was not sent from here and is not provider-verified.`;
    li.append(meta);
  }
  const source = item.source || {};
  const href = source.url ? safeLinkUrl(source.url) : null;
  if (source.label || href) {
    const line = crmNode('p', 'muted crm-activity-source');
    line.append('Source: ');
    if (href) { const a = crmNode('a', 'safe-link', source.label || href); a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; line.append(a); }
    else line.append(source.label);
    li.append(line);
  } else if (source.type) li.append(crmNode('p', 'muted crm-activity-source', 'Source: ' + source.type));
  if (navigate && source.type && source.id && ['task', 'signoff', 'action', 'table'].includes(source.type) && item.origin !== 'recorded') {
    const open = crmButton('Open ' + (source.label || source.type), 'quiet', 'Open source');
    open.onclick = () => navigate({type: source.type, id: source.id, business_id: source.business_id ?? null, label: source.label});
    li.append(open);
  }
  return li;
}
// A record's chronological activity (crm_activity_list) and the explicit form that records an email/message observation.
function crmActivityMount(host, {biz, task, navigate = null}) {
  host.replaceChildren();
  host.classList.add('crm-activity');
  const head = crmNode('div', 'crm-activity-top');
  const refresh = crmButton('Refresh activity', 'quiet', 'Refresh');
  head.append(crmNode('h4', '', 'Activity'), refresh);
  const status = crmNode('p', 'muted crm-activity-status'); status.setAttribute('role', 'status');
  const list = crmNode('ol', 'crm-activity-list'); list.setAttribute('aria-label', 'Record activity');
  const older = crmButton('Older activity', 'quiet', 'Older activity'); older.hidden = true;
  const form = crmRecordForm(biz, task, () => load());
  host.append(head, status, list, older, form.el);
  let items = [], cursor = null, alive = true, generation = 0, loading = false;
  const paint = () => {
    list.replaceChildren(...items.map(item => crmActivityItem(item, navigate)));
    status.textContent = items.length ? '' : (loading ? 'Loading activity…' : 'No activity yet.');
    older.hidden = !cursor;
  };
  async function load(more = false) {
    if (!alive || loading) return;
    loading = true; const own = ++generation;
    if (!more) { status.textContent = 'Loading activity…'; }
    try {
      const data = await crmCall(biz, 'crm_activity_list', {task_id: task.id, include_linked: true, limit: CRM_PAGE, ...(more && cursor ? {cursor} : {})});
      if (!alive || own !== generation) return;
      const merged = new Map((more ? items : []).map(item => [item.id, item]));
      for (const item of data.items || []) if (!merged.has(item.id)) merged.set(item.id, item);
      items = [...merged.values()]; cursor = data.next_cursor || null; loading = false; form.el.hidden = false; paint();
    } catch (failure) {
      if (!alive || own !== generation) return;
      loading = false;
      if (accessLost(failure) || sourceGone(failure)) { items = []; cursor = null; form.reset(); form.el.hidden = true; paint(); status.textContent = 'Activity for this record is unavailable.'; return; }
      status.textContent = uiFailure(failure, {load: true}) + ' ';
      const retry = crmButton('Retry loading activity', 'quiet', 'Retry'); retry.onclick = () => load(more); status.append(retry);
    }
  }
  refresh.onclick = () => { cursor = null; void load(); };
  older.onclick = () => load(true);
  void load();
  return {refresh: () => load(), dispose() { alive = false; generation++; form.dispose(); host.replaceChildren(); }, dirty: () => form.dirty()};
}
// crm_activity_record: a recorded observation. It stores a note with its source reference; it does not send,
// connect a mailbox, approve anything or contact a provider.
function crmRecordForm(biz, task, onRecorded) {
  const details = crmNode('details', 'crm-record-form');
  details.append(crmNode('summary', '', 'Record an email or message'));
  const form = crmNode('form');
  form.innerHTML = `<p class="muted">Records what an agent or you saw, with its source. It does not send mail, connect a mailbox or verify a provider.</p>
    <label>Kind<select name="kind" class="inp"><option value="email">Email</option><option value="message">Message</option><option value="development">Development</option></select></label>
    <label>Summary<input class="inp" name="summary" maxlength="500" required autocomplete="off"></label>
    <label>Text<textarea class="inp" name="body" rows="4" maxlength="20000" required aria-required="true"></textarea></label>
    <label>When it happened<input class="inp" name="occurred_at" type="datetime-local" required></label>
    <label>Source<select name="source_kind" class="inp"><option value="email">Email</option><option value="message">Message</option><option value="external">External</option><option value="manual">Manual</option></select></label>
    <label>Provider <span class="muted">(optional)</span><input class="inp" name="provider" maxlength="500" autocomplete="off"></label>
    <label>Thread reference <span class="muted">(optional)</span><input class="inp" name="thread_id" maxlength="500" autocomplete="off"></label>
    <label>Message reference <span class="muted">(optional)</span><input class="inp" name="message_id" maxlength="500" autocomplete="off"></label>
    <label>Source link <span class="muted">(optional, http or https)</span><input class="inp" name="url" maxlength="4000" autocomplete="off"></label>
    <label>Source title <span class="muted">(optional)</span><input class="inp" name="title" maxlength="500" autocomplete="off"></label>
    <label>Event key <span class="muted">(optional; stops duplicate imports)</span><input class="inp" name="event_key" maxlength="300" autocomplete="off"></label>
    <button type="submit" class="btn primary">Record</button><p class="ferr" role="alert" hidden></p><p class="muted" role="status" hidden></p>`;
  details.append(form);
  const error = form.querySelector('.ferr'), done = form.querySelector('[role=status]');
  let attempt = null, busy = false;
  const input = name => form.elements[name].value.trim();
  form.onsubmit = async event => {
    event.preventDefault();
    if (busy) return;
    error.hidden = true; done.hidden = true;
    const url = input('url');
    if (url && !safeLinkUrl(url)) { error.textContent = 'Use an http or https source link without a username or password.'; error.hidden = false; return; }
    if (!form.elements.body.value.trim()) { error.textContent = 'Enter the text of the message you are recording.'; error.hidden = false; form.elements.body.focus(); return; }
    const when = new Date(form.elements.occurred_at.value);
    if (!Number.isFinite(when.getTime())) { error.textContent = 'Enter when it happened.'; error.hidden = false; return; }
    const source = {kind: input('source_kind'), ...Object.fromEntries(['provider', 'thread_id', 'message_id', 'title'].map(name => [name, input(name)]).filter(([, value]) => value)), ...(url ? {url} : {})};
    const args = {task_id: task.id, kind: input('kind'), summary: input('summary'), body: form.elements.body.value, occurred_at: when.toISOString(), source};
    const binding = JSON.stringify([args, input('event_key')]);
    if (attempt?.binding !== binding) attempt = {binding, key: newRequestId(), event_key: input('event_key') || 'ui:' + newRequestId()};
    busy = true; form.querySelectorAll('button,input,select,textarea').forEach(node => { node.disabled = true; });
    try {
      const data = await crmCall(biz, 'crm_activity_record', {...args, event_key: attempt.event_key, idempotency_key: attempt.key});
      done.textContent = data.duplicate ? 'Already recorded.' : 'Recorded.'; done.hidden = false;
      attempt = null; form.reset(); onRecorded?.();
    } catch (failure) {
      // Nothing is claimed as recorded; the text stays in the form for another try.
      error.textContent = crmConflict(failure) ? 'This event key was already recorded with different content. Change the key or restore the earlier content. Your text is kept.'
        : failure.code === 'channel_required' ? 'This account cannot record on this Task from here. Your text is kept.'
        : failure.code === 'validation_error' ? 'The server refused this entry. Check the required fields; your text is kept.'
        : accessLost(failure) ? 'You cannot record on this record. Your text is kept.' : uiFailure(failure);
      error.hidden = false;
      if (['validation_error', 'channel_required'].includes(failure.code) || crmConflict(failure) || accessLost(failure)) attempt = null;
    } finally { busy = false; form.querySelectorAll('button,input,select,textarea').forEach(node => { node.disabled = false; }); }
  };
  return {el: details, dispose() {}, reset() { form.reset(); error.hidden = true; done.hidden = true; attempt = null; }, dirty: () => [...form.querySelectorAll('input:not([type=hidden]),textarea')].some(node => node.value.trim())};
}

// ---------------------------------------------------------------- scope-aware existing-record picker
// Choose existing Tasks (any subtype) from Global or any accessible Project. The held scope defaults to the Table's own and
// is changed only by the owner's explicit selection: it is never inferred from the current screen. Every selection keeps its
// full scoped reference {type, id, business_id}, so the same id in two scopes stays two different records.
function crmScopeChoices() {
  return [{value: 'global', label: 'Global', business_id: null, slug: null},
    ...(state.bizs || []).filter(b => b && b.slug).map(b => ({value: 'p:' + (b.id || b.business_id), label: b.name || b.slug, business_id: b.id || b.business_id, slug: b.slug}))];
}
function crmChooseRecords({scopeId = null, selected = [], onChange, onRevoke = null}) {
  const chosen = new Map(selected.map(item => [crmScopeKey(item.ref), item]));
  const choices = crmScopeChoices();
  let held = choices.find(choice => choice.business_id === (scopeId ?? null)) || null;   // an unresolvable Table scope is never replaced by Global
  const dialog = modal({stack: true, title: 'Choose existing records', width: 640,
    body: `<label>Scope<select class="inp" data-crm-scope aria-label="Records from"></select></label>
      <input class="inp" data-crm-find aria-label="Find records" placeholder="Find records" autocomplete="off">
      <div class="work-association-list crm-pick-list" aria-label="Matching records"></div>
      <h4>Selected</h4><ul class="crm-chosen crm-picked" aria-label="Selected records"></ul>
      <div class="ferr" role="alert" hidden></div>`,
    actions: [{label: 'Cancel', onClick: close => close()}, {label: 'Use selected records', primary: true, onClick: close => { onChange([...chosen.values()]); close(); }}],
    onOpen: el => {
      const select = el.querySelector('[data-crm-scope]'), find = el.querySelector('[data-crm-find]'), list = el.querySelector('.crm-pick-list');
      const picked = el.querySelector('.crm-picked'), error = el.querySelector('.ferr');
      let generation = 0, cursor = null;
      select.replaceChildren(...(held ? [] : [new Option('Choose a scope', '')]), ...choices.map(choice => new Option(choice.label, choice.value)));
      select.value = held ? held.value : '';
      const paintPicked = () => {
        picked.replaceChildren(...[...chosen.values()].map(item => {
          const li = crmNode('li', '', `${item.title} · ${TASK_TYPE_LABELS[item.task_type] || 'Task'} · ${item.scope_label}`);
          const remove = crmButton(`Remove ${item.title} from selection`, 'quiet', 'Remove');
          remove.onclick = () => { chosen.delete(crmScopeKey(item.ref)); paintPicked(); for (const box of list.querySelectorAll('input[data-key]')) if (box.dataset.key === crmScopeKey(item.ref)) box.checked = false; };
          li.append(' ', remove); return li;
        }));
        if (!chosen.size) picked.replaceChildren(crmNode('li', 'muted', 'Nothing selected yet.'));
      };
      const load = async (more = false) => {
        const own = ++generation, scope = held;
        if (!scope) { list.replaceChildren(crmNode('p', 'muted', 'Choose a scope to search.')); return; }
        try {
          const data = await crmCall(scope.slug, 'tasks_list', {search: find.value, include_closed: true, snooze_filter: 'all', limit: 50, ...(more && cursor ? {cursor} : {})});
          // The held scope, not the current screen, validates the result.
          if (!el.isConnected || own !== generation || held !== scope) return;
          error.hidden = true;
          if (!more) list.replaceChildren();
          list.querySelector('.work-association-more')?.remove();
          const shown = new Set([...list.querySelectorAll('input[data-key]')].map(box => box.dataset.key));
          for (const task of data.tasks || []) {
            const ref = {type: 'task', id: task.id, business_id: scope.business_id}, key = crmScopeKey(ref);
            if (shown.has(key)) continue;
            shown.add(key);
            const row = crmNode('label', 'work-association-choice');
            const box = crmNode('input'); box.type = 'checkbox'; box.dataset.key = key; box.checked = chosen.has(key);
            box.onchange = () => { if (box.checked) chosen.set(key, {ref, title: task.title || task.id, task_type: task.task_type || 'task', scope_label: scope.label}); else chosen.delete(key); paintPicked(); };
            const text = crmNode('span', '', task.title || task.id);
            text.append(crmNode('small', '', ` ${TASK_TYPE_LABELS[task.task_type] || 'Task'} · ${scope.label}`));
            row.append(box, text); list.append(row);
          }
          cursor = data.next_cursor || null;
          if (cursor) { const more = crmButton('More records', 'quiet work-association-more', 'More records'); more.onclick = () => load(true); list.append(more); }
          if (!list.children.length) list.replaceChildren(crmNode('p', 'muted', 'No matching records.'));
        } catch (failure) {
          if (!el.isConnected || own !== generation || held !== scope) return;
          list.replaceChildren();
          if (accessLost(failure) || sourceGone(failure)) {
            // Selections made from a scope that is no longer readable are dropped, titles included, and cannot be submitted.
            for (const [key, item] of [...chosen]) if ((item.ref.business_id ?? null) === scope.business_id) chosen.delete(key);
            paintPicked(); try { onRevoke?.(scope.business_id); } catch (_) { /* the parent keeps its own state */ }
            error.textContent = 'This scope is unavailable. Records chosen from it were removed from the selection.';
          } else error.textContent = uiFailure(failure, {load: true});
          error.hidden = false;
        }
      };
      select.onchange = () => { held = choices.find(choice => choice.value === select.value) || null; cursor = null; ++generation; list.replaceChildren(); void load(); };
      find.oninput = () => { cursor = null; void load(); };
      paintPicked(); void load();
    }});
  return dialog;
}
