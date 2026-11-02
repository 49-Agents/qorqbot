// One record workspace shared by Focus and Threads. A selected record shows exactly one
// full-width pane: its dashboard or its existing Ordubashy conversation. The header keeps
// Back (in-pane related navigation), the record title, the record-type and Chat icon tabs,
// and Close. Entries stay mounted in a bounded navigation stack so tab changes and Back
// preserve dashboard edits, drafts and scroll instead of rebuilding conversations.
const RW_MAX_STACK = 20;
const RW_CHAT_ICON = '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 15a3 3 0 0 1-3 3H8l-4 3V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3z"/><path d="M8 8h8M8 12h5"/></svg>';
// The existing Project icon (the Project management briefcase) also marks a Project record.
const RW_PROJECT_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="7" width="18" height="14" rx="2"/><path d="M8 7V5a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2M3 12a24 24 0 0 0 18 0M10 13h4"/></svg>';
const RW_LINK_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M10 14 14 10M8 16l-1 1a4 4 0 0 1-6-6l4-4a4 4 0 0 1 6 0M16 8l1-1a4 4 0 0 1 6 6l-4 4a4 4 0 0 1-6 0"/></svg>';
const RW_FILE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H5v18h14V8zM14 3v5h5M8 13h8M8 17h8"/></svg>';
const RW_TABLE_ICON = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M9 4v16"/></svg>';
const RW_PROJECT_SECTIONS = [
  ['work', 'Work'], ['signoffs', 'Signoffs'], ['actions', 'Actions'], ['documents', 'Documents'],
  ['tables', 'Tables'], ['automation', 'Automation'], ['other', 'Other'],
];
// A record gets a Chat tab only when the private channel already supports that source type,
// or when the typed contract says this exact inventory target is an existing private source.
const RW_THREAD_TYPES = new Set(['task', 'signoff', 'project', 'reference']);
let rwPanelSequence = 0;

function rwEsc(value) { return esc(value == null ? '' : value); }
function rwNode(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}
// The scoped canonical identity: a bare id is never enough to tell two scopes apart.
function rwSourceKey(source) {
  if (!source) return '';
  return [source.type, source.business_id ?? 'global', source.id].join(':');
}
function rwRecordKey(record) { return rwSourceKey(record.source); }
function rwTypeLabel(record) {
  const source = record.source || {};
  if (source.type === 'project') return 'Project';
  if (source.type === 'signoff') return 'Signoff';
  if (source.type === 'task') return TASK_TYPE_LABELS[record.task_type || source.task_type] || 'Task';
  if (source.type === 'email') return 'Email';
  if (source.type === 'reference') return 'Reference';
  if (source.type === 'job') return 'Job';
  return ({data_tables: 'Table', documents: 'Document', action_logs: 'Action', agent_profiles: 'Agent', context_entities: 'Reference',
    owner_reminders: 'Reminder', routines: 'Routine', routine_runs: 'Routine run', jobs: 'Job'}
    [record.resource_type] || 'Record');
}
function rwTypeIcon(record) {
  const source = record.source || {};
  if (source.type === 'project') return RW_PROJECT_ICON;
  if (source.type === 'signoff' || record.resource_type === 'signoffs') return T_VIEW_ICON.signoffs;
  const special = taskTypeIcon(record.task_type || source.task_type);
  if (special) return special;
  if (source.type === 'task') return T_VIEW_ICON.tasks;
  if (source.type === 'email') return `<span class="task-type-icon task-type-icon-email" role="img" aria-label="Email"></span>`;
  if (record.resource_type === 'data_tables') return RW_TABLE_ICON;
  if (record.resource_type === 'documents') return RW_FILE_ICON;
  if (record.resource_type === 'owner_reminders') return T_VIEW_ICON.calendar;
  if (record.resource_type === 'routines') return (typeof RT_ICONS === 'object' && RT_ICONS.active) || T_VIEW_ICON.calendar;
  if (source.type === 'job' || record.resource_type === 'jobs') return (typeof RT_ICONS === 'object' && RT_ICONS.logs) || T_VIEW_ICON.calendar;
  if (record.url || record.opening_target?.type === 'url') return RW_LINK_ICON;
  return T_VIEW_ICON.tasks;
}
// One record descriptor built from a Focus card or a related reference. Nothing is fetched here.
function rwRecordFromItem(item, options = {}) {
  if (!item) return null;
  const binding = item.conversation || null;
  const source = item.resolved_source || item.source
    || (binding?.type === 'reference' ? {...binding, label: item.reference?.title || item.label, resource_type: 'focus_cards'} : null);
  if (!source || !source.type || !source.id) return null;
  const businessId = source.business_id ?? item.source?.business_id ?? item.conversation?.business_id ?? null;
  const biz = source.business_slug || item.context_business_slug || options.biz || null;
  const taskType = source.task_type || item.task_type || '';
  const label = source.type === 'project'
    ? (source.label || source.business_name || 'Project')
    : (source.label || item.label || source.title || item.reference?.title || 'Record');
  // A Focus reference is an actual private source; an ordinary email or inventory reading is not.
  const threaded = source.type === 'reference' ? !!binding : RW_THREAD_TYPES.has(source.type) && source.type !== 'reference';
  return {
    source: {type: source.type, id: source.id, business_id: businessId},
    biz, business_name: source.business_name || null, label, task_type: taskType, state: source.state || item.state || null,
    resource_type: source.resource_type || item.resource_type || null, summary: source.summary || item.summary || null,
    opening_target: item.opening_target || null, url: source.url || item.url || item.opening_target?.url || item.reference?.target?.url || null,
    can_thread: options.can_thread ?? threaded,
    revision: item.source_revision ?? source.revision ?? null,
    project_id: options.project_id || null,
    project_item: options.project_item || null,
  };
}
// A Project inventory row names both its owning Project and the target's own scope.
function rwRecordFromProjectItem(item, project) {
  if (!item || !item.type || !item.id) return null;
  return rwRecordFromItem({
    source: {type: item.type, id: item.id, business_id: item.business_id ?? null, business_slug: item.business_slug,
      business_name: item.business_name, resource_type: item.resource_type, task_type: item.task_type, state: item.state,
      label: item.label, revision: item.revision},
    item, ...(item.can_thread === undefined ? {} : {can_thread: item.can_thread}),
  }, {project_id: project?.id ?? null, ...(item.can_thread === undefined ? {} : {can_thread: item.can_thread})});
}

// ---------------------------------------------------------------- confirm discard
// Only a genuinely destructive discard asks; tab switches and Back never do.
function rwConfirmDiscard(entry) {
  return new Promise(resolve => {
    let settled = false;
    const finish = value => { if (settled) return; settled = true; resolve(value); };
    const actions = [{label: 'Keep editing', onClick: close => { close(); finish(false); }}];
    if (typeof entry?.dashboard?.save === 'function') actions.push({label: 'Save', primary: true, onClick: async (close, el) => {
      const error = el.querySelector('[role="alert"]');
      try { await entry.dashboard.save(); close(); finish(true); }
      catch (failure) { if (error) { error.textContent = uiFailure(failure); error.hidden = false; } }
    }});
    actions.push({label: 'Discard', onClick: close => { close(); finish(true); }});
    modal({stack: true, width: 460, title: 'Unsaved changes', body: '<p>This record dashboard has unsaved changes.</p><p class="ferr" role="alert" hidden></p>',
      actions, onClose: () => finish(false)});
  });
}

// ---------------------------------------------------------------- controller
// host receives the workspace. options:
//   visible(): whether the workspace is on screen for unread acknowledgment
//   onClose(): hide the surrounding pane after the record was closed by the owner
//   entry: 'focus' | 'threads' (presentation only)
//   onRead(source), onUnread(source), onTitleChange(record)
function recordWorkspaceMount(host, options = {}) {
  if (!host) return null;
  host.classList.add('rw');
  host.replaceChildren();
  const recordsHost = rwNode('div', 'rw-records');
  host.append(recordsHost);
  let stack = [], disposed = false;
  const api = {
    open, navigate, back, close, dispose, current, refresh, hasUnsaved, updateTitle, isOpen: () => stack.length > 0,
    entry, root: () => stack[0] || null, alive: () => !disposed,
  };
  if (options.entry === 'threads') host.classList.add('rw-in-threads');
  return api;

  function entry() { return stack[stack.length - 1] || null; }
  function current() { const item = entry(); return item && item.alive ? item.record : null; }
  function makeEntry(record) {
    const item = {
      record, alive: true, tab: record.can_thread ? 'chat' : 'dashboard', dashboardLoaded: false, dashboard: null,
      scroll: 0, recordEl: null, dashHost: null, chatHost: null, chat: null,
    };
    item.recordEl = rwNode('article', 'rw-record');
    item.recordEl.dataset.rwKey = rwRecordKey(record);
    item.recordEl.dataset.rwType = record.source.type;
    item.recordEl.innerHTML = `<header class="rw-head">
      <button type="button" class="quiet rw-back" aria-label="Back" hidden>← Back</button>
      <span class="rw-title" role="heading" aria-level="2"></span>
      <div class="rw-tabs" role="tablist" aria-label="Record views"></div>
      <button type="button" class="quiet rw-close" aria-label="Close record"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button>
    </header><div class="rw-body"><div class="rw-pane rw-dashboard" role="tabpanel" tabindex="0"></div><div class="rw-pane rw-chat" role="tabpanel" tabindex="0" hidden><div class="agfocus-conversation rw-conversation"></div></div></div>`;
    item.dashHost = item.recordEl.querySelector('.rw-dashboard');
    item.chatHost = item.recordEl.querySelector('.rw-chat');
    item.conversationHost = item.recordEl.querySelector('.rw-conversation');
    item.panelId = 'rw-' + (++rwPanelSequence);
    item.dashHost.id = item.panelId + '-dashboard';
    item.chatHost.id = item.panelId + '-chat';
    item.tabs = item.recordEl.querySelector('.rw-tabs');
    item.tabs.innerHTML = tabsHtml(record, item);
    // The record title is the canonical label; Project cards never use a Focus display-name override.
    titlePaint(item);
    item.back = item.recordEl.querySelector('.rw-back');
    item.back.onclick = () => { void back(); };
    item.recordEl.querySelector('.rw-close').onclick = () => { void close(); };
    item.tabs.querySelectorAll('[data-rw-tab]').forEach(tab => {
      tab.onclick = () => selectTab(item, tab.dataset.rwTab);
      tab.onkeydown = event => tabKey(item, event);
    });
    if (item.tab === 'dashboard') item.dashHost.hidden = false; else item.chatHost.hidden = false;
    item.recordEl.dataset.tab = item.tab;
    return item;
  }
  function tabsHtml(record, item) {
    const tabs = [];
    tabs.push(`<button type="button" class="quiet rw-tab" role="tab" data-rw-tab="dashboard" id="${item.dashHost.id}-tab" aria-controls="${item.dashHost.id}" aria-label="${rwEsc(rwTypeLabel(record))} dashboard" title="${rwEsc(rwTypeLabel(record))} dashboard">${rwTypeIcon(record)}</button>`);
    if (record.can_thread) tabs.push(`<button type="button" class="quiet rw-tab" role="tab" data-rw-tab="chat" id="${item.chatHost.id}-tab" aria-controls="${item.chatHost.id}" aria-label="Chat" title="Chat">${RW_CHAT_ICON}</button>`);
    return tabs.join('');
  }
  function paintTabs(item) {
    item.tabs.querySelectorAll('[data-rw-tab]').forEach(tab => {
      const on = tab.dataset.rwTab === item.tab;
      tab.setAttribute('aria-selected', String(on));
      tab.tabIndex = on ? 0 : -1;
      tab.classList.toggle('rw-tab-on', on);
    });
    item.dashHost.setAttribute('role', 'tabpanel');
    item.chatHost.setAttribute('role', 'tabpanel');
    const dashTab = item.tabs.querySelector('[data-rw-tab="dashboard"]');
    if (dashTab) dashTab.setAttribute('aria-controls', item.dashHost.id);
  }
  function titlePaint(item) {
    const title = item.record.label || 'Record';
    const el = item.recordEl.querySelector('.rw-title');
    if (el.textContent !== title) el.textContent = title;
    el.removeAttribute('title');
    item.recordEl.setAttribute('aria-label', title);
  }
  function tabKey(item, event) {
    const tabs = [...item.tabs.querySelectorAll('[data-rw-tab]')];
    const index = tabs.indexOf(event.currentTarget);
    if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
      event.preventDefault();
      const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length;
      tabs[next].focus({preventScroll: true});
    } else if (['Enter', ' '].includes(event.key)) {
      event.preventDefault(); tabs[index].click();
    }
  }

  // -------------------------------------------------------------- stack
  async function open(record, {tab = null, root = true} = {}) {
    if (disposed || !record) return false;
    const key = rwRecordKey(record);
    const existing = stack.find(item => rwRecordKey(item.record) === key);
    if (root && stack.length) {
      const last = entry();
      if (rwRecordKey(last.record) === key) {
        if (tab) selectTab(last, tab);
        return true;
      }
      if (existing) {
        // A record already on this path is restored, not duplicated.
        while (stack.length && rwRecordKey(entry().record) !== key) removeEntry(stack.pop());
        reveal();
        return true;
      }
      if (hasUnsaved() && !await discardAny()) return false;
      clearEntries();
    } else if (!root && existing) {
      while (stack.length && rwRecordKey(entry().record) !== key) removeEntry(stack.pop());
      reveal();
      return true;
    }
    pushEntry(record, tab);
    return true;
  }
  function navigate(record, {tab = null} = {}) {
    if (disposed || !record) return Promise.resolve(false);
    const existing = stack.find(item => rwRecordKey(item.record) === rwRecordKey(record));
    if (existing) {
      while (stack.length && entry() !== existing) removeEntry(stack.pop());
      reveal();
      return Promise.resolve(true);
    }
    pushEntry(record, tab);
    return Promise.resolve(true);
  }
  function pushEntry(record, tab) {
    if (stack.length >= RW_MAX_STACK) removeEntry(stack.shift());
    const item = makeEntry(record);
    stack.push(item);
    recordsHost.append(item.recordEl);
    showEntry(item, {tab});
    paintBack();
    options.onTitleChange?.(record);
    return item;
  }
  function reveal() {
    stack.forEach((item, index) => { item.recordEl.hidden = index !== stack.length - 1; });
    const item = entry();
    if (!item) return;
    item.recordEl.hidden = false;
    paintTabs(item);
    paintBack();
    restoreScroll(item);
    mountChat(item);
    if (item.tab === 'chat') showChat(item);
    options.onTitleChange?.(item.record);
    syncChatVisibility();
  }
  function showEntry(item, {tab = null} = {}) {
    stack.forEach(other => { other.recordEl.hidden = other !== item; });
    paintTabs(item);
    paintBack();
    if (tab) item.tab = tab;
    else if (!item.record.can_thread) item.tab = 'dashboard';
    item.tab = item.record.can_thread ? item.tab : 'dashboard';
    selectTab(item, item.tab, {initial: true});
    options.onTitleChange?.(item.record);
  }
  function paintBack() {
    const item = entry();
    if (!item) return;
    const prior = stack.length > 1 ? stack[stack.length - 2] : null;
    item.back.hidden = !prior;
    if (prior) item.back.setAttribute('aria-label', 'Back to ' + (prior.record.label || 'previous record'));
  }
  function removeEntry(item) {
    if (!item || !item.alive) return;
    item.alive = false;
    item._scrollSave?.();
    item._scrollSave = null;
    try { item.dashboard?.dispose?.(); } catch (_) { /* a dead dashboard cannot keep the stack alive */ }
    item.dashboard = null;
    try { item.chat?.dispose?.(); } catch (_) { /* idem */ }
    item.chat = null;
    item.chatMounted = false;
    const focusedOutside = !item.recordEl.contains(document.activeElement);
    item.recordEl.remove();
    if (!focusedOutside) entry()?.tabs.querySelector('[data-rw-tab]')?.focus({preventScroll: true});
  }
  function clearEntries() {
    for (const item of stack.splice(0)) removeEntry(item);
  }
  function captureScroll(item) {
    if (!item?.recordEl) return;
    item.scroll = item.dashHost?.scrollTop || 0;
  }
  function restoreScroll(item) {
    if (!item?.dashHost) return;
    // A hidden/detached pane can lose its scroll position; restore the retained value when shown.
    item.dashHost.scrollTop = item.scroll || 0;
  }
  async function back() {
    if (disposed || stack.length < 2) return false;
    const leaving = entry();
    // Back restores the prior record; leaving a dashboard with unsaved edits still asks first.
    if (leaving.dashboard?.hasUnsaved?.()) {
      selectTab(leaving, 'dashboard', {initial: true});
      if (!await rwConfirmDiscard(leaving)) { reveal(); return false; }
    }
    captureScroll(leaving);
    collapseChat(leaving);
    stack.pop();
    removeEntry(leaving);
    const item = entry();
    item.recordEl.hidden = false;
    restoreScroll(item);
    paintBack();
    paintTabs(item);
    selectTab(item, item.tab, {initial: true, restore: true});
    options.onTitleChange?.(item.record);
    item.tabs.querySelector('[data-rw-tab]')?.focus({preventScroll: true});
    return true;
  }
  function hasUnsaved() {
    return stack.some(item => !item.alive ? false : !!item.dashboard?.hasUnsaved?.());
  }
  async function discardAny() {
    // More than one dashboard on the Back path can be dirty; each gets its own explicit choice.
    const handled = new Set();
    for (;;) {
      const dirty = stack.find(item => !handled.has(item) && item.dashboard?.hasUnsaved?.()) || null;
      if (!dirty) return true;
      handled.add(dirty);
      selectTab(dirty, 'dashboard', {initial: true});
      if (!await rwConfirmDiscard(dirty)) { reveal(); return false; }
    }
  }
  function refresh() {
    const item = entry();
    if (item?.alive) item.chat?.refresh?.();
  }
  function updateTitle(record) {
    const item = stack.find(row => rwRecordKey(row.record) === rwRecordKey(record));
    if (!item) return;
    item.record = {...item.record, label: record.label, state: record.state ?? item.record.state, revision: record.revision ?? item.record.revision};
    titlePaint(item);
    options.onTitleChange?.(item.record);
  }
  async function close() {
    if (disposed) return;
    if (!await discardAny()) return;
    dispose();
    options.onClose?.();
  }
  function dispose() {
    if (disposed) return;
    disposed = true;
    clearEntries();
    host.replaceChildren();
    host.classList.remove('rw');
  }

  // -------------------------------------------------------------- tabs
  function selectTab(item, tab, {initial = false, restore = false} = {}) {
    if (!item?.alive) return;
    if (tab === 'chat' && !item.record.can_thread) tab = 'dashboard';
    if (!restore) captureScroll(item);
    const previous = item.tab;
    item.tab = tab;
    item.recordEl.dataset.tab = tab;
    item.dashHost.hidden = tab !== 'dashboard';
    item.chatHost.hidden = tab !== 'chat';
    const dashTab = item.tabs.querySelector('[data-rw-tab="dashboard"]');
    if (dashTab) dashTab.setAttribute('aria-selected', String(tab === 'dashboard'));
    paintTabs(item);
    if (tab === 'dashboard') {
      if (!item.dashboardLoaded) mountDashboard(item);
      else if (!initial && item.dashboard?.onShow) { try { item.dashboard.onShow(); } catch (_) { /* keeps its own state */ } }
      restoreScroll(item);
    }
    if (tab === 'chat') {
      mountChat(item);
      showChat(item);
    } else if (previous === 'chat' || item.chat) collapseChat(item);
    syncChatVisibility();
    options.onToggleTab?.(tab);
  }
  function showChat(item) {
    item.chat?.expand?.();
    const input = item.conversationHost?.querySelector('.conversation-inline-compose textarea, .gchat-composer textarea');
    if (input && item.chatWasFocused) input.focus({preventScroll: true});
  }
  function collapseChat(item) {
    item.chat?.collapse?.();
  }

  // -------------------------------------------------------------- dashboard mount
  function mountDashboard(item) {
    if (!item?.alive || item.dashboardLoaded) return;
    item.dashboardLoaded = true;
    const host = item.dashHost;
    host.innerHTML = '<p class="muted rw-status" role="status">Loading…</p>';
    const alive = () => item.alive && host.isConnected && item.dashHost === host;
    const workspace = api;
    const record = item.record;
    const navigate = target => {
      if (!target) return;
      void workspace.navigate(target);
    };
    (async () => {
      try {
        const dashboard = await rwDashboardMount(host, record, {
          alive, navigate, workspace, entry: item, options,
          // The Project's real name and the task's own source title can differ from the card label.
          title(next) {
            if (!alive() || !next) return;
            if (next.label && next.label !== item.record.label) {
              item.record.label = next.label;
              titlePaint(item);
              options.onTitleChange?.(item.record);
            }
          },
        });
        if (!alive()) { try { dashboard?.dispose?.(); } catch (_) { /* late */ } return; }
        item.dashboard = dashboard || {};
      } catch (failure) {
        if (!alive()) return;
        host.replaceChildren();
        if (accessLost(failure) || sourceGone(failure)) {
          host.append(rwNode('p', 'muted rw-unavailable', 'This record is unavailable. It may have been removed, or you may not have access to it.'));
        } else {
          const message = rwNode('p', 'ferr', uiFailure(failure, {load: true}));
          message.setAttribute('role', 'alert');
          host.append(message);
        }
      }
    })();
  }

  // -------------------------------------------------------------- chat mount
  function mountChat(item) {
    if (!item?.alive || !item.record.can_thread || item.chatMounted) return;
    item.chatMounted = true;
    const record = item.record;
    const source = {...record.source};
    const binding = {type: source.type, id: source.id, biz: record.biz || null, revision: record.revision || null};
    const privateSource = {type: source.type, id: source.id, ...(source.business_id != null ? {business_id: source.business_id} : {})};
    const host = item.conversationHost;
    item.chat = conversationMount(host, binding, {
      inlineComposer: true, related: true, dismissOnOutside: false,
      // Focus and Threads are Global projections; the canonical source names the owning Project.
      privateGlobal: true, privateSource,
      isVisible: () => item.alive && item.tab === 'chat' && !item.chatHost.hidden && !document.hidden
        && options.visible?.() !== false && item.recordEl.isConnected && !item.recordEl.hidden,
      openReference: ref => { void rwOpenChatReference(item, ref); },
      onRead: () => options.onRead?.(source),
      onUnread: () => options.onUnread?.(source),
      onSent: () => options.onSent?.(source),
      onUnavailable: () => {
        if (!item.alive) return;
        if (entry() === item) void rwRecordUnavailable(item);
      },
    });
    // A record thread has one composer: its own pane. No second Comment toggle is drawn.
    host.querySelector('.conversation-toggle')?.remove();
    host.addEventListener('focusin', () => { item.chatWasFocused = true; });
  }
  function syncChatVisibility() {
    const item = entry();
    if (!item) return;
    if (item.tab !== 'chat') collapseChat(item);
    else if (!item.chatHost.hidden) showChat(item);
  }
  async function rwRecordUnavailable(item) {
    // The source is gone: drop the mounted content instead of leaving stale private text behind.
    const index = stack.indexOf(item);
    if (index < 0) return;
    stack.splice(index, 1);
    removeEntry(item);
    if (!stack.length) { dispose(); options.onClose?.(); return; }
    reveal();
  }
  async function rwOpenChatReference(item, ref) {
    if (!ref) return;
    const target = ref.resource_type === 'email_conversations' || ref.type === 'email'
      ? rwRecordFromItem({source: {type: 'email', id: ref.id, business_id: ref.business_id ?? null, business_slug: ref.business_slug, label: ref.label}}, {can_thread: false})
      : rwRecordFromItem({source: {type: ref.type, id: ref.id, business_id: ref.business_id ?? null, business_slug: ref.business_slug, business_name: ref.business_name, label: ref.label, state: ref.state, resource_type: ref.resource_type}}, {can_thread: ref.type === 'task' || ref.type === 'signoff' || ref.type === 'project'});
    if (target) void navigate(target);
  }
}
// ---------------------------------------------------------------- dashboards
async function rwDashboardMount(host, record, ctx) {
  host.replaceChildren();
  host.classList.add('rw-dashboard-panel');
  const source = record.source;
  const path = !record.biz ? '/root' : `/biz/${encodeURIComponent(record.biz)}`;
  const call = async (operation, args = {}, options = {}) => coreData(await post(`${path}/operations/${operation}`, {...args, ...options}));
  if (source.type === 'project') return rwProjectDashboard(host, record, ctx, call);
  if (source.type === 'signoff') return rwSignoffDashboard(host, record, ctx);
  if (source.type === 'job') return rwJobDashboard(host, record, ctx);
  if (source.type === 'task') return rwTaskDashboard(host, record, ctx, call);
  if (source.type === 'email' || record.resource_type === 'email_conversations') {
    host.classList.add('record-panel');
    historicalEmailMount(host);
    return {dirty: () => false, dispose() {}};
  }
  return rwReferenceDashboard(host, record, ctx, call);
}

// ---------------------------------------------------------------- Task
async function rwTaskDashboard(host, record, ctx, call) {
  host.classList.add('record-panel');
  const data = await call('tasks_get', {task_id: record.source.id});
  if (!ctx.alive()) return null;
  const task = data.task;
  if (!task) throw new Error('Task is no longer available.');
  // A related item may arrive with only its identifier; the authoritative Task title fills the header.
  if (task.title && (!record.label || record.label === 'Record' || record.label === record.source.id)) ctx.title?.({label: task.title});
  const type = task.task_type || 'task';
  if (TASK_SPECIAL_TYPES.includes(type) && typeof RECORD_RENDERERS === 'object' && RECORD_RENDERERS[type]) {
    const local = {task, ...task};
    let controller = null;
    const dashboard = RECORD_RENDERERS[type](host, {
      task: local, biz: record.biz || null,
      current: () => ctx.alive() && host.isConnected,
      apply(fresh) { Object.assign(local, fresh); Object.assign(task, fresh); },
      openTask: link => { void ctx.navigate(rwRecordFromItem({source: {type: 'task', id: link.task_id, business_id: link.business_id ?? null, business_slug: link.business_slug, label: link.label || link.title}}, {biz: link.business_slug})); },
      // Associated Signoffs/Actions open on the same stack; Back returns to this Story/record dashboard.
      openRelated: other => {
        const target = rwRecordFromItem({source: {type: other.type, id: other.id, business_id: other.business_id ?? record.source.business_id ?? null,
          business_slug: other.business_slug || record.biz, business_name: other.business_name, label: other.label || other.title,
          resource_type: other.resource_type, revision: other.revision, state: other.state}});
        if (target) void ctx.navigate(target);
      },
      beforeNavigate: () => {},
      navigate: link => ctx.navigate(link),
    });
    controller = dashboard;
    return controller;
  }
  if (record.source.business_id == null && typeof globalTasksMount === 'function') {
    globalTasksMount(host, {initialTaskId: task.id, onBack: () => {}});
    return {dirty: () => false, hasUnsaved: () => false, dispose() { host._globalTasks?.dispose?.(); host.replaceChildren(); }};
  }
  const mounted = tDetail(task, {host, onClose: () => {}});
  return {dirty: () => false, hasUnsaved: () => false, dispose() { mounted?.close?.(); host.replaceChildren(); }};
}

// ---------------------------------------------------------------- Signoff
function rwSignoffDashboard(host, record, ctx) {
  host.classList.add('record-panel', 'rw-signoff-panel');
  const slug = record.biz || null;
  const view = sgCreateView({
    active: () => ctx.alive() && host.isConnected,
    body: () => host,
    inline: true, execution: true, waitingOnly: false,
    workspace: () => slug || 'scope:global',
    global: record.source.business_id == null,
    category: record.category,
    item: () => ({id: record.source.id, businessId: record.source.business_id ?? null}),
    // The workspace Chat tab is this Signoff's own channel: the card's nested copy would be a second composer.
    omitConversationFor: conversationKey({type: 'signoff', id: record.source.id, biz: record.biz || null}),
    dropOnDenied: true,
  });
  queueMicrotask(() => { if (ctx.alive() && host.isConnected) view.paint(); });
  return {dirty: () => false, hasUnsaved: () => false, dispose() { view.dispose(); host.replaceChildren(); }};
}

// ---------------------------------------------------------------- reminder / routine / job readers
function rwFormatTime(value) {
  if (!value) return '—';
  if (typeof rtTime === 'function') return rtTime(value);
  try { return new Intl.DateTimeFormat('en-US', {weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date(value)); }
  catch (_) { return String(value); }
}
// A safe, read-only reminder: text, schedule and state. Scheduling/cancellation stay in their existing UI.
function rwReminderReader(host, record, item, reminder) {
  const box = rwNode('section', 'rw-reader rw-reminder');
  box.append(rwNode('h3', '', 'Reminder'));
  box.append(rwNode('p', 'rw-reminder-text', reminder.text || item.label || record.label || 'Reminder'));
  const fields = [['Scheduled for', rwFormatTime(reminder.at)], ['State', reminder.state || '—']];
  if (reminder.revision != null) fields.push(['Revision', String(reminder.revision)]);
  const meta = rwNode('p', 'muted rw-reminder-meta', fields.map(([label, value]) => `${label}: ${value}`).join(' · '));
  box.append(meta);
  host.append(box);
  return {dirty: () => false, hasUnsaved: () => false, dispose() { host.replaceChildren(); }};
}
// A routine is the Project automation item; its runs are read from the existing scoped routine history,
// independently paged. A structured run job can be opened through jobs_get, never enumerated here.
function rwRoutineReader(host, record, ctx, routine) {
  let disposed = false, loading = false, cursor = null, generation = 0;
  const routineId = routine.id || record.source.id;
  const base = record.biz ? `/biz/${encodeURIComponent(record.biz)}` : '/root';
  const scopeArgs = record.biz ? {} : {scope: 'global'};
  const shell = rwNode('section', 'rw-reader rw-routine');
  shell.innerHTML = `<h3></h3><dl class="rw-routine-meta"></dl><div class="rw-routine-history"><h4>Run history</h4>
    <div class="rw-run-list"></div><p class="muted rw-run-empty">Loading runs…</p>
    <div class="rw-run-actions"><button type="button" class="quiet" data-routine-more hidden>More runs</button><button type="button" class="quiet" data-routine-retry hidden>Retry</button></div>
    <p class="ferr" role="alert" data-routine-error hidden></p></div>`;
  host.append(shell);
  shell.querySelector('h3').textContent = routine.name || record.label || 'Routine';
  const meta = shell.querySelector('.rw-routine-meta');
  const lines = [];
  if (routine.status) lines.push(['Status', routine.status]);
  if (routine.next) lines.push(['Next scheduled', rwFormatTime(routine.next)]);
  if (routine.program) lines.push(['Program', routine.program]);
  if (routine.target) lines.push(['Target', {root: 'Global', 'each-business': 'Each project', business: 'This project'}[routine.target] || routine.target]);
  if (routine.unavailable_reason) lines.push(['Scheduling', routine.unavailable_reason]);
  if (routine.why) lines.push(['About', routine.why]);
  for (const [label, value] of lines) {
    const dt = rwNode('dt', '', label); const dd = rwNode('dd', '', String(value));
    meta.append(dt, dd);
  }
  meta.hidden = !lines.length;
  const list = shell.querySelector('.rw-run-list'), more = shell.querySelector('[data-routine-more]');
  const retry = shell.querySelector('[data-routine-retry]');
  const empty = shell.querySelector('.rw-run-empty'), error = shell.querySelector('[data-routine-error]');
  const stateLabel = state => ({queued: 'Queued', running: 'Running', succeeded: 'Succeeded', failed: 'Failed', cancelled: 'Cancelled'}[state] || state || 'Unknown');
  function runRow(run) {
    const row = rwNode('div', 'rw-run-row');
    const state = rwNode('span', 'rw-run-state ' + (run.state === 'failed' ? 'bad' : ''), stateLabel(run.state));
    const time = rwNode('time', 'rw-run-time', rwFormatTime(run.started_at || run.created_at || run.start));
    if (run.started_at || run.created_at || run.start) time.dateTime = run.started_at || run.created_at || run.start;
    row.append(state, time);
    if (run.trigger) row.append(rwNode('span', 'rw-run-trigger', run.trigger));
    const jobId = run.job_id || run.result_json?.job_id;
    if (jobId) {
      const button = rwNode('button', 'quiet rw-run-job', 'View job');
      button.type = 'button';
      button.setAttribute('aria-label', 'View job ' + jobId);
      button.onclick = () => void ctx.navigate(rwRecordFromItem({source: {type: 'job', id: jobId, business_id: routine.business_id ?? record.source.business_id ?? null,
        business_slug: record.biz, business_name: record.business_name, label: 'Job'}}, {biz: record.biz}));
      row.append(button);
    }
    return row;
  }
  async function load(append = false) {
    if (disposed || loading) return;
    loading = true; const own = ++generation;
    if (!append) { cursor = null; list.replaceChildren(); }
    more.disabled = true; retry.hidden = true; error.hidden = true;
    if (!append) { empty.hidden = false; empty.textContent = 'Loading runs…'; }
    try {
      const data = coreData(await post(`${base}/operations/routines_runs`, {...scopeArgs, routine_id: routineId, limit: 25,
        ...(append && cursor ? {cursor} : {})}));
      if (disposed || own !== generation || !ctx.alive()) return;
      for (const run of data.items || []) list.append(runRow(run));
      cursor = data.next_cursor || null;
      more.hidden = !cursor;
      empty.textContent = 'No runs yet.';
      empty.hidden = !!list.children.length;
    } catch (failure) {
      if (disposed || own !== generation || !ctx.alive()) return;
      error.textContent = uiFailure(failure, {load: true});
      error.hidden = false;
      more.hidden = true;
      retry.hidden = false;
      empty.hidden = true;
    } finally {
      if (own !== generation) return;
      loading = false; more.disabled = false; more.textContent = 'More runs';
    }
  }
  more.onclick = () => {
    if (loading) return;
    more.textContent = 'Loading…';
    void load(true);
  };
  retry.onclick = () => { if (!loading) void load(false); };
  void load();
  return {dirty: () => false, hasUnsaved: () => false, dispose() { disposed = true; generation++; host.replaceChildren(); }};
}
// A job is reached only from a structured routine run; this is its canonical scoped read, not a jobs browser.
async function rwJobDashboard(host, record, ctx) {
  host.classList.add('record-panel');
  const path = record.biz ? `/biz/${encodeURIComponent(record.biz)}` : '/root';
  const data = coreData(await post(`${path}/operations/jobs_get`, {job_id: record.source.id, ...(!record.biz ? {scope: 'global'} : {})}));
  if (!ctx.alive()) return null;
  const job = data.job;
  if (!job) throw new Error('This job is no longer available.');
  const box = rwNode('section', 'rw-reader rw-job');
  box.append(rwNode('h3', '', 'Job'));
  const lines = [['Kind', job.kind], ['State', job.state], ['Updated', job.updated_at && rwFormatTime(job.updated_at)], ['Attempts', job.attempts]]
    .filter(([, value]) => value != null && value !== '');
  const meta = rwNode('dl', 'rw-routine-meta');
  for (const [label, value] of lines) meta.append(rwNode('dt', '', label), rwNode('dd', '', String(value)));
  box.append(meta);
  const code = job.error_json?.code;
  if (code) box.append(rwNode('p', 'ferr', 'Error: ' + code));
  if (data.details_available === false) box.append(rwNode('p', 'muted', 'Full output is only available to the initiating agent and the owner.'));
  host.append(box);
  return {dirty: () => false, hasUnsaved: () => false, dispose() { host.replaceChildren(); }};
}

// ---------------------------------------------------------------- reference / inventory readers
async function rwReferenceDashboard(host, record, ctx, call) {
  host.classList.add('record-panel');
  // A Project inventory record is authorized and read through project_item_get before any content is shown.
  let item = {}, content = {}, authorized = false;
  if (record.project_id) {
    const data = await coreData(await post('/root/operations/project_item_get', {scope: 'global', project_id: record.project_id,
      item: {type: record.source.type, id: record.source.id, business_id: record.source.business_id ?? null}}));
    if (!ctx.alive()) return null;
    item = data.item || {}; content = data.record || {}; authorized = true;
    const before = record.label;
    if (item.label) record.label = item.label;
    if (content.label && !record.label) record.label = content.label;
    if (record.label !== before) ctx.title?.(record);
  }
  const target = record.opening_target;
  const url = record.url || item.url || content.url || (target && target.type === 'url' ? target.url : null);
  if (url) {
    const safe = rwSafeUrl(url);
    if (!safe) throw new Error('This reference URL is unavailable.');
    host.innerHTML = `<iframe class="agfocus-reference-frame" aria-label="${rwEsc(record.label || 'Reference')}" src="${rwEsc(safe)}" referrerpolicy="no-referrer" sandbox="allow-scripts allow-same-origin allow-forms allow-popups allow-downloads" allow="clipboard-read; clipboard-write; fullscreen"></iframe><p class="muted agfocus-reference-help">If this site prevents embedding, <a href="${rwEsc(safe)}" target="_blank" rel="noopener noreferrer">open it in a new tab</a>.</p>`;
    return {dirty: () => false, hasUnsaved: () => false, dispose() { host.replaceChildren(); }};
  }
  if ((record.resource_type || item.resource_type) === 'data_tables') {
    const box = rwNode('section', 'rw-reader');
    box.append(rwNode('h3', '', item.label || record.label || 'Table'));
    box.append(rwNode('p', 'muted', content.summary || item.summary || 'This table keeps its rows in the existing table reader.'));
    const open = rwNode('button', 'quiet', 'Open table');
    open.type = 'button';
    open.onclick = () => tablesOpen(record.biz || null, record.source.id);
    box.append(open); host.append(box);
    return {dirty: () => false, hasUnsaved: () => false, dispose() { host.replaceChildren(); }};
  }
  if (authorized) {
    if (content.reminder) return rwReminderReader(host, record, item, content.reminder);
    if (content.routine) return rwRoutineReader(host, record, ctx, content.routine);
    const box = rwNode('section', 'rw-reader');
    box.append(rwNode('h3', '', item.label || record.label || 'Record'));
    const summary = content.summary || content.overview || content.body || item.summary || '';
    if (summary) box.append(rwNode('div', 'work-source', String(summary)));
    const lines = [];
    for (const [label, key] of [['Scope', 'business_name'], ['State', 'state'], ['Type', 'task_type'], ['Kind', 'kind'], ['Revision', 'revision']]) {
      const value = content[key] ?? item[key];
      if (value != null && value !== '') lines.push(`${label}: ${value}`);
    }
    if (lines.length) box.append(rwNode('p', 'muted', lines.join(' · ')));
    if (!summary && !lines.length) box.append(rwNode('p', 'muted', 'No preview available.'));
    host.append(box);
    return {dirty: () => false, hasUnsaved: () => false, dispose() { host.replaceChildren(); }};
  }
  const box = rwNode('section', 'rw-reader');
  box.append(rwNode('p', 'work-source', record.summary || 'No preview available.'));
  host.append(box);
  return {dirty: () => false, hasUnsaved: () => false, dispose() { host.replaceChildren(); }};
}
function rwSafeUrl(value) {
  try {
    if (!value || /\s/.test(value)) return null;
    const url = new URL(value);
    return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : null;
  } catch (_) { return null; }
}

// ---------------------------------------------------------------- Project
async function rwProjectCall(operation, args = {}, options = {}) {
  return coreData(await post(`/root/operations/${operation}`, {scope: 'global', ...args, ...options}));
}
async function rwProjectDashboard(host, record, ctx) {
  host.classList.add('record-panel', 'rw-project-panel');
  const projectId = record.source.id;
  const project = {id: projectId, name: record.label, summary: '', archived: false};
  const data = await rwProjectCall('project_record_get', {project_id: projectId});
  if (!ctx.alive()) return null;
  if (!data.project) throw new Error('This Project is unavailable.');
  Object.assign(project, data.project);
  // The real Project row supplies the exact name; a Focus display-name override never renames it.
  if (project.name && project.name !== record.label) {
    record.label = project.name;
    ctx.title?.(record);
  }
  const sectionState = {};
  let activeSection = 'work', generation = 0, disposed = false;
  const shell = rwNode('div', 'rw-project');
  shell.innerHTML = `<section class="rw-project-head"><h3 class="rw-project-name"></h3><span class="rw-project-archived" hidden>Archived</span><p class="rw-project-summary muted"></p>
    <div class="rw-project-tools"><button type="button" class="quiet" data-project-link>Link existing item</button><button type="button" class="quiet" data-project-closed aria-pressed="false">Closed work</button><button type="button" class="quiet" data-project-archived aria-pressed="false">Archived</button></div></section>
    <div class="rw-project-sections" role="group" aria-label="Project sections"></div>
    <div class="rw-project-body"><div class="rw-project-list"></div><button type="button" class="quiet rw-project-more" data-project-more hidden>More</button><p class="muted rw-project-empty" hidden>Nothing here yet.</p><p class="ferr" role="alert" data-project-error hidden></p></div>`;
  host.append(shell);
  shell.querySelector('.rw-project-name').textContent = project.name || record.label || 'Project';
  shell.querySelector('.rw-project-summary').textContent = project.summary || '';
  shell.querySelector('.rw-project-archived').hidden = !project.archived;
  shell.querySelector('.rw-project-sections').innerHTML = RW_PROJECT_SECTIONS.map(([key, label]) =>
    `<button type="button" class="quiet" data-project-section="${key}" aria-pressed="${key === activeSection}">${label}</button>`).join('');
  const list = shell.querySelector('.rw-project-list');
  const more = shell.querySelector('[data-project-more]');
  const empty = shell.querySelector('.rw-project-empty');
  const errorBox = shell.querySelector('[data-project-error]');
  const stateFor = section => sectionState[section] ||= {items: [], cursor: null, loaded: false, loading: false, key: ''};
  const filters = () => JSON.stringify([shell.querySelector('[data-project-closed]').getAttribute('aria-pressed') === 'true', shell.querySelector('[data-project-archived]').getAttribute('aria-pressed') === 'true']);
  function paintSections() {
    shell.querySelectorAll('[data-project-section]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.projectSection === activeSection)));
    shell.querySelector('[data-project-archived]').setAttribute('aria-pressed', String(!!shell.dataset.includeArchived));
  }
  function rowFor(item) {
    const row = rwNode('div', 'rw-project-row');
    row.dataset.rwItemType = item.type;
    const open = rwNode('button', 'quiet rw-project-open');
    open.type = 'button';
    const icon = rwNode('span', 'rw-project-icon');
    icon.innerHTML = rwTypeIcon({source: {type: item.type}, task_type: item.task_type, resource_type: item.resource_type});
    const label = rwNode('span', 'rw-project-label', item.label || 'Untitled');
    const scope = item.business_id == null ? 'Global' : item.business_id !== projectId ? (item.business_name || item.business_slug || 'Other Project') : '';
    const scopeEl = rwNode('span', 'rw-project-scope', scope);
    scopeEl.hidden = !scope;
    const state = item.state || (item.association?.owned ? 'Owned' : '');
    const stateEl = rwNode('span', 'rw-project-state', state);
    stateEl.hidden = !state;
    const assoc = rwNode('span', 'rw-project-assoc', item.association?.linked ? (item.association.owned ? 'Owned · Linked' : 'Linked') : '');
    assoc.hidden = !item.association?.linked;
    open.append(icon, label, scopeEl, stateEl, assoc);
    open.setAttribute('aria-label', [item.label, scope, item.state].filter(Boolean).join(', '));
    open.onclick = () => {
      const target = rwRecordFromProjectItem(item, project);
      if (target) void ctx.navigate(target, {tab: target.can_thread ? 'chat' : 'dashboard'});
    };
    row.append(open);
    if (item.association?.linked && item.association?.link_id) {
      const unlink = rwNode('button', 'quiet rw-project-unlink', 'Unlink');
      unlink.type = 'button';
      unlink.setAttribute('aria-label', 'Unlink ' + (item.label || 'item'));
      let key = null, pending = false;
      unlink.onclick = async () => {
        if (pending) return;
        pending = true; key ||= newRequestId(); unlink.disabled = true;
        try {
          await rwProjectCall('project_items_unlink', {project_id: projectId, link_id: item.association.link_id,
            expected_revision: item.association.link_revision}, {idempotency_key: key});
          if (!disposed) await load(activeSection, false, true);
        } catch (failure) {
          unlink.disabled = false;
          if (['validation_error', 'forbidden', 'policy_denied', 'not_found', 'revision_conflict'].includes(failure.code)) key = null;
          if (!disposed) { errorBox.textContent = uiFailure(failure); errorBox.hidden = false; }
        } finally { pending = false; }
      };
      row.append(unlink);
    }
    return row;
  }
  async function load(section, append = false, fresh = false) {
    const state = stateFor(section);
    const key = filters();
    if (state.loading) { if (!append) state.pending = true; return; }
    if (!append && state.loaded && state.key === key && !fresh) { paint(section); return; }
    if (!append && (state.key !== key || fresh)) { state.items = []; state.cursor = null; state.loaded = false; state.key = key; }
    state.loading = true;
    if (section === activeSection) { more.disabled = true; errorBox.hidden = true; paint(section); }
    const own = ++generation;
    try {
      let data, pages = 0;
      const [includeClosed, includeArchived] = JSON.parse(key);
      do {
        data = await rwProjectCall('project_items_list', {project_id: projectId, section,
          root_tasks_only: false, include_closed: includeClosed, include_archived: includeArchived,
          limit: 25, ...(append && state.cursor ? {cursor: state.cursor} : {})});
        if (disposed || own !== generation) return;
        state.items = append ? [...state.items, ...(data.items || [])] : (data.items || []);
        state.cursor = data.next_cursor || null; pages++;
      } while (append && state.cursor && pages < 1);
      state.loaded = true;
      if (section === activeSection) paint(section);
    } catch (failure) {
      if (disposed || own !== generation) return;
      if (!append) state.items = [];
      state.loaded = true;
      if (section === activeSection) {
        errorBox.textContent = accessLost(failure) || sourceGone(failure) ? 'This project inventory is unavailable.' : uiFailure(failure, {load: true});
        errorBox.hidden = false;
        paint(section);
      }
    } finally {
      state.loading = false;
      if (!disposed && section === activeSection) { more.disabled = false; }
      if (!disposed && state.pending) { state.pending = false; void load(section, false, true); }
    }
  }
  function paint(section) {
    if (section !== activeSection) return;
    const state = stateFor(section);
    list.replaceChildren();
    for (const item of state.items) list.append(rowFor(item));
    // A failed read keeps its own error message; it never doubles as “nothing here”.
    empty.hidden = !errorBox.hidden || (!state.loaded && !state.loading) || !!state.items.length;
    empty.textContent = state.loading && !state.items.length ? 'Loading…' : 'Nothing here yet.';
    more.hidden = !state.cursor;
    more.textContent = state.loading && state.items.length ? 'Loading more…' : 'More';
    paintSections();
  }
  function selectSection(section) {
    if (section === activeSection) return;
    activeSection = section;
    paintSections();
    errorBox.hidden = true;
    const state = stateFor(section);
    if (!state.loaded) { list.replaceChildren(); empty.hidden = false; empty.textContent = 'Loading…'; more.hidden = true; }
    else paint(section);
    void load(section);
  }
  shell.querySelector('.rw-project-sections').onclick = event => {
    const button = event.target.closest('[data-project-section]');
    if (button) selectSection(button.dataset.projectSection);
  };
  shell.querySelector('[data-project-closed]').onclick = event => {
    const button = event.currentTarget;
    button.setAttribute('aria-pressed', String(button.getAttribute('aria-pressed') !== 'true'));
    stateFor(activeSection).key = '';
    void load(activeSection, false, true);
  };
  shell.querySelector('[data-project-archived]').onclick = event => {
    const button = event.currentTarget;
    button.setAttribute('aria-pressed', String(button.getAttribute('aria-pressed') !== 'true'));
    shell.dataset.includeArchived = button.getAttribute('aria-pressed');
    stateFor(activeSection).key = '';
    void load(activeSection, false, true);
  };
  more.onclick = () => load(activeSection, true);
  shell.querySelector('[data-project-link]').onclick = () => {
    rwProjectLinkDialog(project, () => load(activeSection, false, true));
  };
  Object.assign(project, {id: projectId});
  ctx.title?.(record);
  paint(activeSection);
  void load(activeSection);
  return {
    dirty: () => false, hasUnsaved: () => false,
    dispose() { disposed = true; generation++; host.replaceChildren(); },
  };
}

function rwProjectLinkDialog(project, onLinked) {
  let submission = null, pending = false;
  const projects = (state.bizs || []).filter(biz => biz.slug);
  const scopeOptions = `<option value="">Global</option>${projects.map(biz => `<option value="${esc(biz.business_id || biz.id)}" ${(biz.business_id || biz.id) === project.id ? 'selected' : ''}>${esc(biz.name || biz.slug)}${biz.archived ? ' (archived)' : ''}</option>`).join('')}`;
  modal({title: 'Link an existing item', width: 640, stack: true,
    body: `<div class="rw-link-dialog"><label>Scope<select class="inp" data-link-scope>${scopeOptions}</select></label>
      <label>Kind<select class="inp" data-link-type><option value="">Any kind</option><option value="task">Work</option><option value="signoff">Signoff</option><option value="action">Action</option><option value="document">Document</option><option value="table">Table</option><option value="reminder">Reminder</option><option value="routine">Routine</option><option value="project">Project</option></select></label>
      <label>Search<input class="inp" type="search" data-link-search placeholder="Search the selected scope…" maxlength="200"></label>
      <div class="rw-link-results" role="list"></div><p class="muted rw-link-empty" hidden>No matching records.</p>
      <button type="button" class="quiet" data-link-more hidden>More</button><p class="ferr" role="alert" data-link-error hidden></p></div>`,
    actions: [{label: 'Close', onClick: close => close()}],
    onOpen: el => {
      const scope = el.querySelector('[data-link-scope]'), type = el.querySelector('[data-link-type]'), search = el.querySelector('[data-link-search]');
      const results = el.querySelector('.rw-link-results'), empty = el.querySelector('.rw-link-empty'), more = el.querySelector('[data-link-more]'), error = el.querySelector('[data-link-error]');
      let cursor = null, generation = 0, timer = null;
      const load = async (append = false) => {
        const own = ++generation;
        error.hidden = true;
        if (!append) { results.replaceChildren(); cursor = null; }
        const businessId = scope.value || null;
        try {
          const data = await rwProjectCall('project_items_candidates', {project_id: project.id, source_business_id: businessId,
            ...(type.value ? {type: type.value} : {}), ...(search.value.trim() ? {query: search.value.trim()} : {}),
            limit: 25, ...(append && cursor ? {cursor} : {})});
          if (!el.isConnected || own !== generation) return;
          cursor = data.next_cursor || null;
          more.hidden = !cursor;
          for (const item of data.items || []) {
            const row = rwNode('button', 'quiet rw-link-option');
            row.type = 'button';
            row.setAttribute('role', 'listitem');
            row.innerHTML = `<span class="rw-project-icon">${rwTypeIcon({source: {type: item.type}, task_type: item.task_type, resource_type: item.resource_type})}</span><span class="rw-project-label"></span><span class="rw-project-scope muted"></span>`;
            row.querySelector('.rw-project-label').textContent = item.label || item.name || 'Untitled';
            const label = item.business_id == null ? 'Global' : (item.business_name || item.business_slug || 'Other Project');
            row.querySelector('.rw-project-scope').textContent = label;
            row.onclick = async () => {
              if (pending) return;
              const signature = JSON.stringify([businessId, item.type, item.id]);
              if (submission?.signature !== signature) submission = {signature, key: newRequestId()};
              pending = true; row.disabled = true; error.hidden = true;
              try {
                const data = await rwProjectCall('project_items_link', {project_id: project.id,
                  item: {type: item.type, id: item.id, business_id: item.business_id ?? null}}, {idempotency_key: submission.key});
                submission = null;
                el.querySelector('.mx')?.click();
                onLinked?.(data);
              } catch (failure) {
                if (['validation_error', 'forbidden', 'policy_denied', 'not_found', 'revision_conflict'].includes(failure.code)) submission = null;
                error.textContent = uiFailure(failure); error.hidden = false; row.disabled = false;
              } finally { pending = false; }
            };
            results.append(row);
          }
          empty.hidden = !!results.children.length || !!cursor;
          if (!append) empty.textContent = 'No matching records.';
        } catch (failure) {
          if (!el.isConnected || own !== generation) return;
          error.textContent = uiFailure(failure, {load: true}); error.hidden = false;
        }
      };
      scope.onchange = () => load();
      type.onchange = () => load();
      search.oninput = () => { clearTimeout(timer); timer = setTimeout(() => load(), 200); };
      more.onclick = () => load(true);
      load();
    }});
}

// ---------------------------------------------------------------- small shared helpers for entry points
// A Focus/Threads item reports unread in the same shape the row already uses.
function rwRecordFromFocusCard(item) {
  const record = rwRecordFromItem(item, {can_thread: !item.unavailable && RW_THREAD_TYPES.has((item.resolved_source || item.source || item.conversation || {}).type)});
  // Focus may carry a presentation-only name. A Project never uses it; other records keep their
  // familiar card name while the dashboard shows the record's real content.
  if (record && record.source.type !== 'project' && item.display_name?.trim()) record.label = item.display_name.trim();
  return record;
}