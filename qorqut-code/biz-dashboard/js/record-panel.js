// One optional information panel for Story, Customer and Email Tasks. Every opening route (Focus, business and
// Global Task details, linked records) mounts the same surface: the record's own Ordubashy conversation stays
// where it is, and a labelled button reveals this panel on its left. Nothing here touches the conversation
// mount, so its draft, scroll position and exact source survive opening, closing and saving.
const RECORD_PANELS = {story: 'Story dashboard', customer: 'Customer context', email: 'Email chain'};
let recordPanelSequence = 0;

// Global reads of relationships and Context items name their scope; Task operations never do.
const recordScope = biz => biz ? {} : {scope: 'global'};
const recordTaskLink = (ctx, task) => ({task_id: task.id, business_id: task.business_id ?? null, business_slug: ctx.biz});

async function recordLoadTask(ctx) {
  return (await workCall(ctx.biz, 'tasks_get', {task_id: ctx.task.id})).task;
}

// One write path for every panel: revision-checked, with the exact request key kept for an uncertain retry.
async function recordTaskUpdate(ctx, changes, submission) {
  const signature = JSON.stringify([ctx.task.id, ctx.task.revision, changes]);
  if (submission.signature !== signature) Object.assign(submission, {signature, key: newRequestId()});
  const result = await workCall(ctx.biz, 'tasks_update', {task_id: ctx.task.id, changes}, {expected_revision: ctx.task.revision, idempotency_key: submission.key});
  submission.signature = null;
  return result.task;
}

function recordSaver(ctx, {error, latest, setBusy}) {
  const submission = {signature: null, key: null};
  let busy = false;
  const show = (text, conflict = false) => { error.textContent = text; error.hidden = !text; latest.hidden = !conflict; };
  return {
    show,
    get busy() { return busy; },
    async run(changes, onSaved) {
      if (busy) return false;
      busy = true; setBusy(true); show('');
      try {
        const fresh = await recordTaskUpdate(ctx, changes, submission);
        if (!ctx.current()) return false;
        ctx.apply(fresh); onSaved(fresh);
        return true;
      } catch (failure) {
        if (!ctx.current()) return false;
        if (failure.code === 'revision_conflict') { submission.signature = null; show('This record changed since you opened it. Your unsaved edits are kept.', true); }
        else if (['validation_error', 'forbidden', 'policy_denied', 'not_found'].includes(failure.code)) { submission.signature = null; show(uiFailure(failure)); }
        else show(submission.signature ? "Couldn't confirm. Save again to retry the same change." : uiFailure(failure));
        return false;
      } finally { busy = false; if (ctx.current()) setBusy(false); }
    },
    // Adopt the latest revision for the write check while the form keeps what was typed.
    async adoptLatest(adopt) {
      try {
        const fresh = await recordLoadTask(ctx);
        if (!ctx.current()) return;
        adopt(fresh); ctx.task.revision = fresh.revision; ctx.apply(fresh, {keepLocal: true});
        show('Loaded the latest revision. Your edits are still in the form; review them and save again.');
      } catch (failure) { if (ctx.current()) show(uiFailure(failure, {load: true}), true); }
    },
  };
}

const recordNode = (tag, className, text) => { const node = document.createElement(tag); if (className) node.className = className; if (text != null) node.textContent = text; return node; };
function recordRetry(box, message, retry) {
  box.replaceChildren(recordNode('span', 'muted', message + ' '));
  const button = recordNode('button', 'quiet', 'Retry'); button.type = 'button'; button.onclick = retry;
  box.append(button);
}

// ---------------------------------------------------------------- Story
function recordOpenEmail(biz, id) {
  modal({title: 'Email conversation', stack: true, width: 800, body: '<div class="record-email-modal"></div>',
    onOpen: el => {
      const host = el.querySelector('.record-email-modal');
      if (biz) emailThreadMount(host, biz, id, {active: () => host.isConnected});
      else emailGlobalThreadMount(host, id, {active: () => host.isConnected});
    }});
}

function recordChildren(box, ctx) {
  let generation = 0, cursor = null;
  box.innerHTML = '<div class="record-child-list"></div><button type="button" class="quiet" data-children-more hidden>More child tasks</button><p class="muted" data-children-empty hidden>No child tasks.</p>';
  const list = box.querySelector('.record-child-list'), more = box.querySelector('[data-children-more]'), empty = box.querySelector('[data-children-empty]');
  async function load(next = false) {
    const own = ++generation;
    try {
      const data = await workCall(ctx.biz, 'tasks_list', {parent_id: ctx.task.id, include_closed: true, include_archived: true, snooze_filter: 'all', limit: 25, ...(next && cursor ? {cursor} : {})});
      if (own !== generation || !ctx.current()) return;
      if (!next) list.replaceChildren();
      for (const child of data.tasks || []) {
        const chip = recordNode('button', 'chip task-link-chip'); chip.type = 'button'; chip.title = child.title;
        chip.innerHTML = (taskShowsState(child) ? `<span class="task-state-chip"><span class="sdot ${esc(child.status)}" aria-hidden="true"></span>${esc(T_LABEL[child.status] || child.status)}</span> ` : taskTypeIcon(child.task_type)) + esc(tChipLabel(child.title)) + customerStateChip(child);
        chip.onclick = () => Promise.resolve(ctx.openTask(recordTaskLink(ctx, child))).catch(error => toast(uiFailure(error, {load: true})));
        list.append(chip, ' ');
      }
      cursor = data.next_cursor || null; more.hidden = !cursor;
      empty.hidden = !!list.children.length;
    } catch (error) { if (own === generation && ctx.current()) recordRetry(list, uiFailure(error, {load: true}), () => load()); }
  }
  more.onclick = () => load(true);
  load();
  return {refresh: () => load(), dispose() { generation++; }};
}

// Task-to-Task links use the existing `related_task` list. It carries its own revision for adding links, so it is
// re-read (not reused) after this panel saves the Task itself.
function recordTaskLinks(box, ctx) {
  const mount = () => taskLinksMount(box, ctx.biz, {id: ctx.task.id, business_id: ctx.task.business_id ?? null, revision: ctx.task.revision}, {open: link => ctx.openTask(link)});
  mount();
  return {refresh: mount, dispose() { box._taskLinks = null; }};
}

// Related email conversations and Context material are `concerns` relationships to this Task, in this Task's scope.
function recordRelated(box, ctx, kind) {
  const email = kind === 'email';
  let generation = 0, cursor = null, edges = [];
  box.innerHTML = `<div class="record-link-list"></div><p class="muted" data-related-empty hidden>${email ? 'No linked email conversations.' : 'No linked Context.'}</p>
    <div class="record-actions"><button type="button" class="quiet" data-related-add>${email ? 'Link email conversation' : 'Link Context'}</button><button type="button" class="quiet" data-related-more hidden>More links</button></div><p class="ferr" role="alert" data-related-error hidden></p>`;
  const list = box.querySelector('.record-link-list'), empty = box.querySelector('[data-related-empty]'), more = box.querySelector('[data-related-more]'), error = box.querySelector('[data-related-error]');
  const item = () => ({type: 'task', id: ctx.task.id});
  const isEmail = other => other?.resource_type === 'email_conversations';
  const matches = other => other && other.type !== 'task' && (email ? isEmail(other) : !isEmail(other) && other.type !== 'agent');
  const present = () => {
    list.replaceChildren();
    for (const edge of edges) {
      const other = edge.source?.type === 'task' && edge.source.id === ctx.task.id ? edge.target_item : edge.source_item;
      if (!matches(other)) continue;
      const row = recordNode('div', 'record-link');
      const open = recordNode('button', 'quiet record-link-open', other.label || 'Unavailable'); open.type = 'button';
      open.onclick = () => Promise.resolve(email ? recordOpenEmail(ctx.biz, other.id) : recordOpenContext(ctx, other)).catch(failure => toast(uiFailure(failure, {load: true})));
      const kindLabel = recordNode('span', 'muted', email ? 'Email' : contextItemType(other));
      const remove = recordNode('button', 'quiet record-link-remove', 'Unlink'); remove.type = 'button'; remove.setAttribute('aria-label', 'Unlink ' + (other.label || 'item'));
      let key = null;
      remove.onclick = async () => {
        remove.disabled = true; key ||= newRequestId();
        try {
          await workCall(ctx.biz, 'relationships_remove', {relationship_id: edge.id, ...recordScope(ctx.biz)}, {expected_revision: edge.revision, idempotency_key: key});
          if (ctx.current()) load();
        } catch (failure) { remove.disabled = false; error.textContent = uiFailure(failure); error.hidden = false; }
      };
      row.append(open, kindLabel, remove); list.append(row);
    }
    empty.hidden = !!list.children.length;
  };
  async function load(next = false) {
    const own = ++generation;
    try {
      let pages = 0, last = next ? cursor : null;
      const found = next ? [...edges] : [];
      do {
        const page = await workCall(ctx.biz, 'relationships_list', {item: item(), role: 'concerns', direction: 'both', include_archived: false, limit: 50, ...(last ? {cursor: last} : {}), ...recordScope(ctx.biz)});
        if (own !== generation || !ctx.current()) return;
        found.push(...page.items); last = page.next_cursor || null; pages++;
      } while (last && pages < 4);
      edges = found; cursor = last; error.hidden = true;
      more.hidden = !cursor; present();
    } catch (failure) { if (own === generation && ctx.current()) recordRetry(list, uiFailure(failure, {load: true}), () => load()); }
  }
  // The new edge runs from the linked item to this Task, like every other resource-to-Task link.
  let request = {signature: null, key: null};
  async function link(target) {
    const args = {source: target, target: item(), role: 'concerns', ...recordScope(ctx.biz)};
    const signature = JSON.stringify(args);
    if (request.signature !== signature) request = {signature, key: newRequestId()};
    await workCall(ctx.biz, 'relationships_create', args, {idempotency_key: request.key});
    request.signature = null;
    if (ctx.current()) load();
  }
  box.querySelector('[data-related-add]').onclick = () => (email ? recordPickEmail(ctx, link) : recordPickContext(ctx, link));
  more.onclick = () => load(true);
  load();
  return {refresh: () => load(), dispose() { generation++; }};
}


// A linked Context item opens its real content, read in the record's own explicit scope (Global never borrows whichever
// business happens to be selected). Registered uploads and dumps use the existing source reader, other documents their
// document reader, and people and entities their item detail. Anything else says plainly that it is unavailable.
const RECORD_SOURCE_KINDS = ['context_upload', 'context_dump'];
function recordContextModal(title, body, onOpen) {
  modal({stack: true, width: 760, title: esc(title), body, onOpen, actions: []});
}
function recordUnavailable(title, message = 'This item is unavailable. It may have been removed, or you may not have access to it.') {
  recordContextModal(title, `<p class="muted" role="status" data-record-unavailable>${esc(message)}</p>`);
}
async function recordOpenContext(ctx, other) {
  const title = other.label || 'Context';
  let item;
  try {
    item = (await workCall(ctx.biz, 'context_items_get', {item: {type: other.type, id: other.id}, ...recordScope(ctx.biz)})).item;
  } catch (failure) {
    if (!ctx.current()) return;
    if (['not_found', 'forbidden', 'policy_denied'].includes(failure.code)) return recordUnavailable(title);
    throw failure;
  }
  if (!ctx.current()) return;
  if (!item) return recordUnavailable(title);
  const kind = item.kind || item.subtype || '';
  const isDocument = (item.resource_type || other.resource_type) === 'documents' || ['material', 'topic'].includes(item.type || other.type);
  if (isDocument && RECORD_SOURCE_KINDS.includes(kind) && typeof srcMountSource === 'function') {
    const scope = ctx.biz ? {kind: 'business', id: ctx.biz} : {kind: 'root'};
    return recordContextModal(title, '<div class="record-context-source"></div>', el => {
      const host = el.querySelector('.record-context-source');
      srcMountSource(host, scope, other.id, {live: () => host.isConnected});
    });
  }
  if (isDocument) return recordOpenDocument(ctx, other, item);
  if (['person', 'entity'].includes(item.type || other.type)) {
    const lines = [item.role, item.org, item.about, item.summary, item.email, item.phone].filter(Boolean);
    return recordContextModal(item.label || title, '<div class="record-context-detail"></div>', el => {
      const box = el.querySelector('.record-context-detail');
      box.append(recordNode('p', '', lines.join(' · ') || 'No further detail is recorded.'));
      if (/^https?:\/\//.test(item.url || '')) {
        const link = recordNode('a', '', 'Open link'); link.href = item.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; box.append(link);
      }
    });
  }
  if (ctx.biz && typeof contextOpenItem === 'function') return contextOpenItem(ctx.biz, other, ctx.beforeNavigate);
  return recordUnavailable(title, 'This kind of Context cannot be opened from here.');
}
// Plain document text, read by the document's actual kind. Notes have one whole-note reader (the route supplies the
// scope, so only the document id is sent). Registered Context documents are read in bounded chunks bound to one
// scope, document and content version: a More control continues with the returned cursor, appends only the matching
// chunk and, if the document changed underneath, offers a clean restart instead of mixing versions.
const RECORD_NOTE_KINDS = ['business_note', 'agent_note', 'journal', 'global_note', 'business_owned_note'];
function recordOpenDocument(ctx, other, item) {
  const title = other.label || item.label || 'Context';
  const note = RECORD_NOTE_KINDS.includes(item.kind || '');
  return recordContextModal(title, '<div class="record-context-document" aria-live="polite">Loading…</div>', el => {
    const box = el.querySelector('.record-context-document');
    const alive = () => box.isConnected && ctx.current();
    const unavailable = () => {
      box.replaceChildren(recordNode('p', 'muted', 'This item is unavailable. It may have been removed, or you may not have access to it.'));
      box.firstChild.dataset.recordUnavailable = '';
    };
    const gone = failure => ['not_found', 'forbidden', 'policy_denied'].includes(failure.code);
    let own = 0, text = null, loaded = 0, hash = null;
    const read = async (more, cursor) => {
      const mine = ++own;
      if (!more) { box.textContent = 'Loading…'; text = null; loaded = 0; hash = null; }
      else box.querySelector('[data-record-more]')?.setAttribute('disabled', '');
      let data;
      try {
        data = note ? await workCall(ctx.biz, 'notes_get', {document_id: other.id})
          : await workCall(ctx.biz, 'context_get', {document_id: other.id, scope: ctx.biz ? 'business' : 'shared', ...(more ? {cursor} : {})});
      } catch (failure) {
        if (mine !== own || !alive()) return;
        if (gone(failure)) return unavailable();
        if (more && failure.code === 'validation_error') return conflict();
        return recordRetry(box, uiFailure(failure, {load: true}), () => read(more, cursor));
      }
      if (mine !== own || !alive()) return;
      const doc = data.document || data.note || null;
      if (!doc) return unavailable();
      if (more && (doc.id !== other.id || doc.character_offset !== loaded || (hash && doc.content_hash && doc.content_hash !== hash))) return conflict();
      const part = doc.body ?? doc.text ?? '';
      text = (text ?? '') + part; loaded += Array.from(part).length; hash = hash || doc.content_hash || null;
      paint(note ? true : data.complete !== false && !data.next_cursor, data.next_cursor);
    };
    const paint = (complete, cursor) => {
      box.replaceChildren(recordNode('div', 'work-source', text));
      if (complete) { box.append(recordNode('p', 'muted small', 'End of document.')); box.lastChild.dataset.recordComplete = ''; return; }
      const more = recordNode('button', 'quiet', 'Read more'); more.type = 'button'; more.dataset.recordMore = '';
      more.onclick = () => read(true, cursor);
      box.append(recordNode('p', 'muted small', 'Showing the first part of this document.'), more);
    };
    const conflict = () => {
      box.replaceChildren(recordNode('p', 'muted', 'This document changed while you were reading, so the rest cannot be joined to what is shown. '));
      box.firstChild.dataset.recordChanged = '';
      const again = recordNode('button', 'quiet', 'Read from the start'); again.type = 'button'; again.onclick = () => read(false);
      box.append(again);
    };
    read(false);
  });
}

function recordPickEmail(ctx, onChoose) {
  modal({stack: true, title: 'Link an email conversation', body: '<input class="inp" type="search" aria-label="Find email conversation" placeholder="Search email…"><div class="work-association-list"></div><button type="button" class="quiet" data-pick-more hidden>More</button><p class="ferr" role="alert" hidden></p>',
    onOpen: el => {
      const input = el.querySelector('input'), list = el.querySelector('.work-association-list'), more = el.querySelector('[data-pick-more]'), error = el.querySelector('.ferr');
      let generation = 0, cursor = null, busy = false;
      const load = async (next = false) => {
        const own = ++generation; error.hidden = true;
        try {
          const data = await workCall(ctx.biz, 'email_conversations_list', {...recordScope(ctx.biz), limit: 50, ...(input.value.trim() ? {query: input.value.trim()} : {}), ...(next && cursor ? {cursor} : {})});
          if (!el.isConnected || own !== generation) return;
          if (!next) list.replaceChildren();
          for (const row of data.items || []) {
            const button = recordNode('button', 'quiet record-pick-option'); button.type = 'button';
            button.textContent = [row.subject || '(No subject)', row.last_message_at ? new Date(row.last_message_at).toLocaleDateString() : ''].filter(Boolean).join(' · ');
            button.onclick = async () => {
              if (busy) return; busy = true; button.disabled = true;
              try { await onChoose({type: 'material', id: row.id}); el.querySelector('.mx')?.click(); }
              catch (failure) { error.textContent = uiFailure(failure); error.hidden = false; }
              finally { busy = false; button.disabled = false; }
            };
            list.append(button);
          }
          cursor = data.next_cursor || null; more.hidden = !cursor;
          if (!list.children.length) list.textContent = 'No conversations found.';
        } catch (failure) { if (own === generation && el.isConnected) { error.textContent = uiFailure(failure, {load: true}); error.hidden = false; } }
      };
      let timer; input.oninput = () => { clearTimeout(timer); timer = setTimeout(() => load(), 200); };
      more.onclick = () => load(true); load();
    }});
}

function recordPickContext(ctx, onChoose) {
  modal({stack: true, title: 'Link existing Context', body: `<select class="inp" aria-label="Context type">${[['person', 'Person'], ['topic', 'Topic'], ['material', 'Material'], ['entity', 'Entity']].map(([value, label]) => `<option value="${value}">${label}</option>`).join('')}</select><input class="inp" type="search" aria-label="Find Context" placeholder="Search Context…"><div class="work-association-list"></div><p class="ferr" role="alert" hidden></p>`,
    onOpen: el => {
      const type = el.querySelector('select'), input = el.querySelector('input'), list = el.querySelector('.work-association-list'), error = el.querySelector('.ferr');
      let generation = 0, busy = false;
      const load = async () => {
        const own = ++generation; error.hidden = true;
        try {
          const data = await workCall(ctx.biz, 'context_items_list', {type: type.value, limit: 50, ...(input.value.trim() ? {query: input.value.trim()} : {}), ...recordScope(ctx.biz)});
          if (!el.isConnected || own !== generation) return;
          list.replaceChildren();
          for (const found of (data.items || []).filter(other => other.resource_type !== 'email_conversations')) {
            const button = recordNode('button', 'quiet record-pick-option', found.label || 'Untitled'); button.type = 'button';
            button.onclick = async () => {
              if (busy) return; busy = true; button.disabled = true;
              try { await onChoose({type: found.type, id: found.id}); el.querySelector('.mx')?.click(); }
              catch (failure) { error.textContent = uiFailure(failure); error.hidden = false; }
              finally { busy = false; button.disabled = false; }
            };
            list.append(button);
          }
          if (!list.children.length) list.textContent = 'No matching Context.';
        } catch (failure) { if (own === generation && el.isConnected) { error.textContent = uiFailure(failure, {load: true}); error.hidden = false; } }
      };
      let timer; input.oninput = () => { clearTimeout(timer); timer = setTimeout(load, 200); };
      type.onchange = load; load();
    }});
}

// Canonical Task activity, newest first, read-only and paged by the thread's own cursor.
function recordActivity(box, ctx) {
  let generation = 0, cursor = null;
  const notes = new Map();
  box.innerHTML = '<ol class="record-activity" aria-label="Recent activity"></ol><p class="muted" data-activity-empty hidden>No activity yet.</p><div class="record-actions"><button type="button" class="quiet" data-activity-older hidden>Older activity</button><button type="button" class="quiet" data-activity-refresh>Refresh activity</button></div><p class="ferr" role="alert" data-activity-error hidden></p>';
  const list = box.querySelector('.record-activity'), empty = box.querySelector('[data-activity-empty]'), older = box.querySelector('[data-activity-older]'), error = box.querySelector('[data-activity-error]');
  const present = () => {
    list.replaceChildren();
    const rows = [...notes.values()].sort((a, b) => a.sequence != null && b.sequence != null ? b.sequence - a.sequence : String(b.created_at || '').localeCompare(String(a.created_at || '')));
    for (const note of rows) {
      const entry = recordNode('li', 'record-activity-entry' + (note.entry_kind === 'system' ? ' record-activity-system' : ''));
      const head = recordNode('div', 'record-activity-head');
      head.append(recordNode('strong', '', note.entry_kind === 'system' ? 'Activity' : conversationPersonName(note)), recordNode('time', 'muted', note.created_at ? workDate(note.created_at) : ''));
      entry.append(head);
      if (note.title && note.title !== 'Message') entry.append(recordNode('div', 'record-activity-title', note.title));
      entry.append(recordNode('div', 'record-activity-body', note.body || ''));
      list.append(entry);
    }
    empty.hidden = !!rows.length;
  };
  async function load(next = false) {
    const own = ++generation; error.hidden = true;
    try {
      const data = await workCall(ctx.biz, 'tasks_thread_get', {task_id: ctx.task.id, limit: 20, ...(next && cursor ? {cursor} : {})});
      if (own !== generation || !ctx.current()) return;
      if (!next) notes.clear();
      for (const note of data.notes || []) notes.set(note.id, note);
      cursor = data.next_cursor || null; older.hidden = !cursor;
      present();
    } catch (failure) { if (own === generation && ctx.current()) { error.textContent = uiFailure(failure, {load: true}); error.hidden = false; } }
  }
  older.onclick = () => load(true);
  box.querySelector('[data-activity-refresh]').onclick = () => load();
  load();
  return {refresh: () => load(), dispose() { generation++; }};
}

function recordSection(title, className) {
  const section = recordNode('section', 'record-section ' + (className || ''));
  section.append(recordNode('h4', '', title));
  const body = recordNode('div', 'record-section-body'); section.append(body);
  return {section, body};
}

function recordStoryMount(el, ctx) {
  const parts = {};
  const overview = recordNode('section', 'record-section');
  overview.innerHTML = `<h4>Overview</h4><textarea class="inp" data-story-overview aria-label="Story overview" rows="7" maxlength="20000"></textarea>
    <div class="record-actions"><button type="button" class="btn" data-story-save>Save overview</button><button type="button" class="quiet" data-story-latest hidden>Use latest revision</button></div><p class="ferr" role="alert" data-story-error hidden></p>`;
  const field = overview.querySelector('[data-story-overview]'), save = overview.querySelector('[data-story-save]');
  let base = ctx.task.overview || '';
  field.value = base;
  const saver = recordSaver(ctx, {error: overview.querySelector('[data-story-error]'), latest: overview.querySelector('[data-story-latest]'), setBusy: busy => { save.disabled = busy; }});
  save.onclick = () => {
    if (field.value === base) { saver.show('The overview has no changes to save.'); return; }
    const value = field.value;
    saver.run({overview: value}, () => { base = value; parts.links?.refresh(); parts.activity?.refresh(); });
  };
  // Adopting a newer snapshot keeps an unsaved overview as typed and refreshes only a clean one.
  const adopt = fresh => {
    if (!('overview' in fresh)) return;
    const dirty = field.value !== base;
    base = fresh.overview || '';
    if (!dirty) field.value = base;
  };
  overview.querySelector('[data-story-latest]').onclick = () => saver.adoptLatest(adopt);
  const children = recordSection('Child tasks'), links = recordSection('Linked tasks'), emails = recordSection('Email conversations'), activity = recordSection('Recent activity');
  el.replaceChildren(overview, children.section, links.section, emails.section, activity.section);
  parts.children = recordChildren(children.body, ctx);
  parts.links = recordTaskLinks(links.body, ctx);
  parts.emails = recordRelated(emails.body, ctx, 'email');
  parts.activity = recordActivity(activity.body, ctx);
  return {
    dirty: () => field.value !== base,
    adopt,
    // Reopening refreshes what others can change; the overview keeps any unsaved text.
    onShow() { parts.children.refresh(); parts.links.refresh(); parts.emails.refresh(); parts.activity.refresh(); },
    dispose() { for (const part of Object.values(parts)) part.dispose(); },
  };
}

// ---------------------------------------------------------------- Customer
function recordCustomerMount(el, ctx) {
  const task = ctx.task;
  const form = recordNode('section', 'record-section');
  form.innerHTML = `<h4>Customer</h4><p class="record-archived" data-customer-archived hidden>Archived</p>
    <label>Name<input class="inp" data-customer-name maxlength="1000" autocomplete="off" value="${esc(task.title || '')}"></label>
    ${customerFieldsHtml(task)}
    <label>Summary<textarea class="inp" data-customer-summary rows="3" maxlength="20000">${esc(task.overview || '')}</textarea></label>
    <div class="record-actions"><button type="button" class="btn" data-customer-save>Save details</button><button type="button" class="quiet" data-customer-archive></button><button type="button" class="quiet" data-customer-latest hidden>Use latest revision</button></div><p class="ferr" role="alert" data-customer-error hidden></p>`;
  const fields = form.querySelector('[data-task-customer]');
  fields.hidden = false;
  const input = {name: form.querySelector('[data-customer-name]'), summary: form.querySelector('[data-customer-summary]'), kind: fields.querySelector('[data-customer-kind]'), state: fields.querySelector('[data-customer-state]')};
  const states = customerStatesMount(fields, ctx.biz);
  const stateButton = recordNode('button', 'quiet', 'Set state'); stateButton.type = 'button'; stateButton.dataset.customerSaveState = '';
  input.state.after(stateButton);
  const save = form.querySelector('[data-customer-save]'), archive = form.querySelector('[data-customer-archive]');
  let base;
  const adoptBase = source => {
    const profile = source.customer_profile || {};
    base = {title: source.title || '', overview: source.overview || '', customer_kind: source.customer_kind || 'person', customer_state: source.customer_state || '',
      customer_profile: Object.fromEntries([...CUSTOMER_PROFILE_FIELDS.map(([key]) => key), 'notes'].map(key => [key, profile[key] || '']))};
  };
  adoptBase(task);
  const profileInput = key => fields.querySelector(key === 'notes' ? '[data-customer-notes]' : `[data-customer-profile="${key}"]`);
  const paintArchive = () => {
    form.querySelector('[data-customer-archived]').hidden = !ctx.task.archived;
    archive.textContent = ctx.task.archived ? 'Unarchive' : 'Archive';
  };
  paintArchive();
  const setBusy = busy => { for (const control of form.querySelectorAll('input,textarea,select,button')) control.disabled = busy; };
  const saver = recordSaver(ctx, {error: form.querySelector('[data-customer-error]'), latest: form.querySelector('[data-customer-latest]'), setBusy});
  const profileNow = () => customerFieldsRead(fields).customer_profile;
  const detailChanges = () => {
    const changes = {}, title = input.name.value.trim();
    if (title !== base.title) changes.title = title;
    if (input.kind.value !== base.customer_kind) changes.customer_kind = input.kind.value;
    const profile = profileNow();
    if (Object.keys(profile).some(key => profile[key] !== base.customer_profile[key])) changes.customer_profile = profile;
    if (input.summary.value !== base.overview) changes.overview = input.summary.value;
    return changes;
  };
  // Saving details never carries the state, and saving the state never carries the details.
  save.onclick = () => {
    const changes = detailChanges();
    if (!Object.keys(changes).length) { saver.show('There are no detail changes to save.'); return; }
    if ('title' in changes && !changes.title) { saver.show('A customer needs a name.'); return; }
    saver.run(changes, fresh => {
      if ('title' in changes) base.title = changes.title;
      if ('customer_kind' in changes) base.customer_kind = changes.customer_kind;
      if ('customer_profile' in changes) base.customer_profile = changes.customer_profile;
      if ('overview' in changes) base.overview = changes.overview;
      adopt(fresh);
      parts.links?.refresh(); parts.activity?.refresh();
    });
  };
  stateButton.onclick = () => {
    const value = customerNormalizeState(input.state.value);
    input.state.value = value;
    if (value === base.customer_state) { saver.show('The state is unchanged.'); return; }
    // The engine answers with the label's canonical spelling (an equal existing label keeps its first spelling).
    saver.run({customer_state: value}, fresh => { base.customer_state = value; adopt(fresh); states.load(); parts.links?.refresh(); parts.activity?.refresh(); });
  };
  archive.onclick = () => {
    // Archiving is its own explicit write on the existing flag; a state label never does it.
    const next = !ctx.task.archived;
    saver.run({archived: next}, fresh => { adopt(fresh); parts.links?.refresh(); });
  };
  // Adopting a newer snapshot (the latest revision, the canonical result of a save, or a save made elsewhere) refreshes
  // every clean control and keeps each unsaved one as typed.
  const adopt = fresh => {
    const dirty = {title: input.name.value.trim() !== base.title, kind: input.kind.value !== base.customer_kind, state: customerNormalizeState(input.state.value) !== base.customer_state,
      summary: input.summary.value !== base.overview, profile: Object.fromEntries(Object.keys(base.customer_profile).map(key => [key, profileInput(key).value.trim() !== base.customer_profile[key] && !(key === 'notes' && profileInput(key).value === base.customer_profile[key])]))};
    const previous = base;
    adoptBase({title: 'title' in fresh ? fresh.title : previous.title, overview: 'overview' in fresh ? fresh.overview : previous.overview,
      customer_kind: 'customer_kind' in fresh ? fresh.customer_kind : previous.customer_kind, customer_state: 'customer_state' in fresh ? fresh.customer_state : previous.customer_state,
      customer_profile: 'customer_profile' in fresh ? fresh.customer_profile : previous.customer_profile});
    if (!dirty.title) input.name.value = base.title;
    if (!dirty.kind) input.kind.value = base.customer_kind;
    if (!dirty.state) input.state.value = base.customer_state;
    if (!dirty.summary) input.summary.value = base.overview;
    for (const key of Object.keys(base.customer_profile)) if (!dirty.profile[key]) profileInput(key).value = base.customer_profile[key];
    if (base.customer_state) states.extend(base.customer_state);
    paintArchive();
  };
  form.querySelector('[data-customer-latest]').onclick = () => saver.adoptLatest(adopt);
  const parts = {};
  const tasks = recordSection('Linked tasks'), emails = recordSection('Email conversations'), context = recordSection('Context'), activity = recordSection('Recent activity');
  el.replaceChildren(form, tasks.section, emails.section, context.section, activity.section);
  states.load();
  parts.links = recordTaskLinks(tasks.body, ctx);
  parts.emails = recordRelated(emails.body, ctx, 'email');
  parts.context = recordRelated(context.body, ctx, 'context');
  parts.activity = recordActivity(activity.body, ctx);
  return {
    dirty: () => Object.keys(detailChanges()).length > 0 || customerNormalizeState(input.state.value) !== base.customer_state,
    adopt,
    onShow() { parts.links.refresh(); parts.emails.refresh(); parts.context.refresh(); parts.activity.refresh(); states.load(); },
    dispose() { states.dispose(); for (const part of Object.values(parts)) part.dispose(); },
  };
}

// ---------------------------------------------------------------- Email
function recordEmailMount(el, ctx) {
  const id = ctx.task.email_conversation_id;
  if (!id) {
    el.innerHTML = '<p class="muted">No email conversation is linked to this record. Choose one from the record\'s type controls.</p>';
    return {dirty: () => false, onShow() {}, dispose() {}};
  }
  el.innerHTML = '<div class="record-email-body"></div><div class="record-actions"><button type="button" class="quiet" data-email-reload>Reload email</button></div>';
  const body = el.querySelector('.record-email-body');
  let generation = 0;
  const mount = () => {
    const own = ++generation;
    body.replaceChildren();
    const active = () => own === generation && ctx.current() && body.isConnected;
    return ctx.biz ? emailThreadMount(body, ctx.biz, id, {active, correspondenceOnly: true}) : emailGlobalThreadMount(body, id, {active, correspondenceOnly: true});
  };
  el.querySelector('[data-email-reload]').onclick = mount;
  mount();
  return {dirty: () => false, onShow() {}, dispose() { generation++; }};
}

const RECORD_RENDERERS = {story: recordStoryMount, customer: recordCustomerMount, email: recordEmailMount};

// Wraps a record region once: [button bar] over [panel | main], where `main` keeps the region's existing children
// (the conversation) untouched. Returns the controller kept on the host.
function recordSurfaceMount(host, options) {
  const existing = host._recordSurface;
  if (existing) return existing.update(options);
  if (!RECORD_PANELS[options?.task?.task_type]) return null;
  let opts = options, open = false, disposed = false, content = null, loadGeneration = 0;
  const task = {...opts.task}, scope = opts.biz || null, type = task.task_type, id = ++recordPanelSequence;
  const main = recordNode('div', 'record-main');
  while (host.firstChild) main.append(host.firstChild);
  host.classList.add('record-surface');
  host.dataset.recordType = type;
  const bar = recordNode('div', 'record-bar');
  const toggle = recordNode('button', 'quiet record-toggle'); toggle.type = 'button';
  toggle.setAttribute('aria-expanded', 'false'); toggle.setAttribute('aria-controls', `record-panel-${id}`);
  toggle.innerHTML = `${taskTypeIcon(type)}<span>${esc(RECORD_PANELS[type])}</span>`;
  toggle.querySelector('.task-type-icon')?.removeAttribute('role'); toggle.querySelector('.task-type-icon')?.removeAttribute('aria-label'); toggle.querySelector('.task-type-icon')?.setAttribute('aria-hidden', 'true');
  bar.append(toggle);
  const panel = document.createElement('aside');
  panel.id = `record-panel-${id}`; panel.className = 'record-panel'; panel.hidden = true; panel.setAttribute('aria-label', RECORD_PANELS[type]);
  const body = recordNode('div', 'record-body');
  body.append(panel, main);
  host.append(bar, body);
  const ctx = {
    task, biz: scope,
    current: () => !disposed && host.isConnected && host._recordSurface === controller,
    // A saved change updates the shared snapshot and tells the opening surface (title, revision, Focus row).
    apply(fresh, {keepLocal = false} = {}) {
      Object.assign(task, fresh);
      if (!keepLocal) opts.onTask?.(task);
    },
    openTask: link => opts.openTask?.(link),
    beforeNavigate: () => opts.beforeNavigate?.(),
  };
  function paintMessage(text, retry) {
    panel.replaceChildren();
    if (retry) recordRetry(panel, text, retry); else panel.append(recordNode('p', 'muted', text));
  }
  async function load() {
    const own = ++loadGeneration;
    paintMessage('Loading…');
    try {
      const fresh = await recordLoadTask(ctx);
      if (own !== loadGeneration || !ctx.current()) return;
      Object.assign(task, fresh);
      panel.replaceChildren();
      content = RECORD_RENDERERS[type](panel, ctx);
    } catch (error) {
      if (own === loadGeneration && ctx.current()) paintMessage(uiFailure(error, {load: true}), load);
    }
  }
  function show() {
    if (open || disposed) return;
    open = true; panel.hidden = false; host.classList.add('record-panel-open'); toggle.setAttribute('aria-expanded', 'true');
    if (content) content.onShow(); else load();
  }
  // Closing only hides: unsaved edits and the conversation beside it are left exactly as they are.
  function hide() {
    if (!open) return;
    open = false; panel.hidden = true; host.classList.remove('record-panel-open'); toggle.setAttribute('aria-expanded', 'false');
  }
  toggle.onclick = () => (open ? hide() : show());
  function dispose() {
    if (disposed) return;
    disposed = true; loadGeneration++;
    content?.dispose?.();
    while (main.firstChild) host.append(main.firstChild);
    bar.remove(); body.remove();
    host.classList.remove('record-surface', 'record-panel-open'); delete host.dataset.recordType;
    if (host._recordSurface === controller) delete host._recordSurface;
  }
  // A different linked email conversation replaces the chain being read.
  function adoptEmail(id) {
    if (type !== 'email' || !id || id === task.email_conversation_id) return;
    task.email_conversation_id = id;
    content?.dispose?.(); content = null;
    if (open) load();
  }
  const controller = host._recordSurface = {
    open: show, close: hide, isOpen: () => open, hasUnsaved: () => !!content?.dirty(), dispose,
    update(next) {
      const nextTask = next?.task;
      if (!nextTask || nextTask.id !== task.id || (next.biz || null) !== scope || nextTask.task_type !== type) {
        dispose();
        return recordSurfaceMount(host, next);
      }
      opts = next;
      adoptEmail(nextTask.email_conversation_id);
      return controller;
    },
    // A newer authoritative snapshot of this Task (from a save made elsewhere): the write revision and every clean control
    // move to it, while unsaved panel edits stay exactly as typed.
    sync(fresh) {
      if (disposed || !fresh || String(fresh.id) !== String(task.id)) return;
      for (const key of ['revision', 'title', 'overview', 'archived', 'customer_kind', 'customer_state', 'customer_profile']) if (key in fresh) task[key] = fresh[key];
      content?.adopt?.(fresh);
      adoptEmail(fresh.email_conversation_id);
    },
  };
  return controller;
}
