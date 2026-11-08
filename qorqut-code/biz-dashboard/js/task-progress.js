// Read-only progress notes. They never enter the private message state or its unread count.
function taskActivityMount(host, task, biz, adapter = {}) {
  if (!host) return null;
  host.replaceChildren(); host.classList.add('task-activity');
  const node = conversationEl;
  const content = node('div', 'task-activity-content');
  const readerHost = node('div', 'conversation-reference-reader'); readerHost.hidden = true;
  host.append(content, readerHost);
  const head = node('div', 'task-activity-head');
  const count = node('span', 'task-activity-count');
  const refresh = node('button', 'quiet', 'Refresh'); refresh.type = 'button';
  head.append(node('h4', '', 'Activity'), count, refresh);
  const status = node('p', 'task-activity-status muted'); status.setAttribute('role', 'status');
  const list = node('div', 'task-activity-list'); list.setAttribute('aria-label', 'Task progress notes');
  const older = node('button', 'quiet', 'Older activity'); older.type = 'button'; older.hidden = true;
  content.append(head, status, list, older);
  let alive = true, loading = false, generation = 0, referenceGeneration = 0;
  let notes = [], cursor = null, pages = 1, total = 0;
  const opened = new Set();
  const reader = conversationReferenceMount(readerHost, () => {
    if (!alive) return;
    readerHost.hidden = true; content.hidden = false;
  });
  const current = () => alive && host.isConnected;
  function clear() {
    notes = []; cursor = null; total = 0; opened.clear(); referenceGeneration++;
    reader.dispose(); readerHost.replaceChildren(); readerHost.hidden = true; content.hidden = false;
    paint();
  }
  async function openReference(ref) {
    if (ref.unavailable) return;
    const own = ++referenceGeneration;
    try {
      if (ref.type === 'email') {
        const parsed = conversationEmailRef(ref);
        if (!parsed) throw new Error('Invalid email reference');
        if (typeof extensionsLoad === 'function') await extensionsLoad();
        if (!current() || own !== referenceGeneration) return;
        if (!CONVERSATION_REFERENCE_READERS.has(parsed.provider)) throw new Error('Email reader unavailable');
        content.hidden = true; readerHost.hidden = false;
        reader.open(parsed);
      } else await (adapter.openReference ? adapter.openReference(ref) : taskProgressOpenReference(biz, ref));
    } catch (_) {
      if (current() && own === referenceGeneration) {
        readerHost.hidden = true; content.hidden = false;
        status.textContent = "Couldn't open this reference.";
      }
    }
  }
  function paint() {
    const scroll = host.scrollTop;
    list.replaceChildren();
    count.textContent = `${total} ${total === 1 ? 'entry' : 'entries'}`;
    let previousDay = null;
    for (const note of notes) {
      const day = sjDate(note.created_at);
      if (day !== previousDay) {
        list.append(node('h5', 'task-activity-date', day ? sjDayLabel(day, {year: 'numeric'}) : 'Undated'));
        previousDay = day;
      }
      const row = node('details', 'task-activity-entry'); row.dataset.noteId = note.id; row.open = opened.has(note.id);
      const summary = node('summary');
      const label = node('span', 'task-activity-preview', messagePreview(note.body, note.summary) || 'Progress note');
      const actor = node('span', 'task-activity-actor', conversationPersonName(note));
      const time = node('time', 'task-activity-time', sjTime(note.created_at)); time.dateTime = note.created_at || '';
      time.title = sjDateTime(note.created_at);
      const chevron = node('span', 'task-activity-chevron', '›'); chevron.setAttribute('aria-hidden', 'true');
      summary.append(label, actor, time, chevron); row.append(summary);
      const detail = node('div', 'task-activity-detail');
      if (note.author_id && note.recorder_id && note.author_id !== note.recorder_id) {
        detail.append(node('p', 'muted', `${conversationPersonName(note)} · ${note.attribution?.kind === 'owner_intent' ? 'interpreted' : 'recorded'} by ${note.recorder_name || 'Ordubashy'}`));
      }
      const body = node('div', 'task-activity-body'); body.innerHTML = chatViewMarkdown(note.body || '');
      detail.append(body);
      const refs = node('div', 'task-activity-refs');
      for (const ref of note.refs || []) {
        const button = node('button', 'quiet', ref.label || 'Reference'); button.type = 'button'; button.disabled = !!ref.unavailable;
        button.onclick = () => { void openReference(ref); }; refs.append(button);
      }
      if (refs.childElementCount) detail.append(refs);
      row.append(detail);
      row.ontoggle = () => { if (!current() || !row.isConnected) return; if (row.open) opened.add(note.id); else opened.delete(note.id); };
      list.append(row);
    }
    older.hidden = !cursor;
    host.scrollTop = scroll;
  }
  async function load(more = false) {
    if (!current() || loading) return;
    loading = true; refresh.disabled = older.disabled = true;
    const own = ++generation;
    status.textContent = notes.length ? '' : 'Loading activity…';
    try {
      await adapter.revalidate?.();
      if (!current() || own !== generation) return;
      const collected = more ? [...notes] : [];
      let next = more ? cursor : null, loaded = 0, result;
      const limit = more ? 1 : pages;
      do {
        const path = biz == null ? '/root' : `/biz/${encodeURIComponent(biz)}`;
        const data = coreData(await conversationRequest(post(`${path}/operations/tasks_get`, {
          ...(biz == null ? {scope: 'global'} : {}), task_id: task.id, notes_limit: 10, ...(next ? {notes_cursor: next} : {}),
        })));
        if (!current() || own !== generation) return;
        result = data.task;
        if (!result) throw Object.assign(new Error('Task unavailable'), {code: 'not_found'});
        collected.push(...(result.notes || [])); loaded++;
        const previous = next; next = result.notes_next_cursor || null;
        if (next && next === previous) next = null;
      } while (next && loaded < limit);
      notes = [...new Map(collected.map(note => [note.id, note])).values()]
        .sort((a, b) => (b.created_at || '').localeCompare(a.created_at || '') || b.id.localeCompare(a.id));
      cursor = next; pages = more ? pages + loaded : loaded;
      total = result.note_count ?? notes.length;
      paint(); status.textContent = notes.length ? '' : 'No activity yet.';
    } catch (failure) {
      if (!current() || own !== generation) return;
      if (CONVERSATION_GONE.includes(failure.code)) {
        clear(); status.textContent = 'Activity for this task is unavailable.'; adapter.onUnavailable?.(failure);
      } else if (failure.code === 'record_moved') {
        clear(); status.textContent = 'This task moved to another project.'; adapter.onMoved?.(failure);
      } else {
        status.textContent = "Couldn't load activity. ";
        const retry = node('button', 'quiet', 'Retry'); retry.type = 'button'; retry.onclick = () => { void load(more); }; status.append(retry);
      }
    } finally {
      if (own === generation) { loading = false; refresh.disabled = older.disabled = false; }
    }
  }
  refresh.onclick = () => { void load(); };
  older.onclick = () => { void load(true); };
  void load();
  return {refresh: () => load(), dispose() { alive = false; generation++; referenceGeneration++; reader.dispose(); host.replaceChildren(); }};
}

// Legacy task details keep progress in a disclosure, separate from the private conversation.
function taskProgressMount(host, task, biz, beforeOpen) {
  if (!host) return null;
  host.replaceChildren();
  const notes = conversationEl('details', 'task-progress-activity');
  notes.append(conversationEl('summary', '', 'Activity'));
  const activityHost = conversationEl('div'); notes.append(activityHost);
  const chatHost = conversationEl('div'); host.append(notes, chatHost);
  let activity = null;
  notes.ontoggle = () => {
    if (notes.open && !activity) activity = taskActivityMount(activityHost, task, biz, {
      openReference: ref => taskProgressOpenReference(biz, ref, beforeOpen),
    });
  };
  const conversation = conversationMount(chatHost, {type: 'task', id: task.id, biz, revision: task.revision}, {related: true});
  conversation.expand();
  return {...conversation, dispose() { activity?.dispose(); conversation.dispose(); host.replaceChildren(); }};
}

async function taskProgressOpenReference(biz, ref, beforeOpen) {
  if (ref.unavailable) return;
  if (ref.type === 'task') {
    if (await beforeOpen?.() === false) return;
    return taskOpenReference({id:ref.id,...(biz ? {} : {business_id:null}),label:ref.label,task_type:ref.task_type},biz || undefined);
  }
  if (await beforeOpen?.() === false) return;
  if (ref.type === 'action') {
    const id=ref.id.startsWith('global/') || ref.id.startsWith('business/')?ref.id:(ref.business_id?`business/${ref.business_id}/`:'global/')+ref.id;
    const {action}=coreData(await post('/root/operations/actions_get',{action_id:id}));
    return modal({title:'Action',stack:true,width:700,body:`<div class="work-source">${esc(action.text)}</div>`});
  }
  if (ref.type === 'signoff') return sgAttentionNavigate(biz || null, ref.id);
  // Materials (e.g. an email conversation) open through the shared context-item route.
  if (ref.type === 'material' && biz) return contextOpenItem(biz,{type:'material',id:ref.id,resource_type:ref.resource_type});
}
