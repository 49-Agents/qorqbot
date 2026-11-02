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
// Existing dashboard controllers differ: Story has hasUnsaved(), Customer exposes dirty().
function rwDirty(dashboard) {
  if (!dashboard) return false;
  if (typeof dashboard.hasUnsaved === 'function') return !!dashboard.hasUnsaved();
  if (typeof dashboard.dirty === 'function') return !!dashboard.dirty();
  return false;
}
function rwTypeLabel(record) {
  const source = record.source || {};
  if (source.type === 'project') return 'Project';
  if (source.type === 'signoff') return 'Signoff';
  if (source.type === 'task') return TASK_TYPE_LABELS[record.task_type || source.task_type] || 'Task';
  if (source.type === 'email') return 'Email';
  if (source.type === 'reference') return 'Reference';
  if (source.type === 'action') return 'Action';
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
  if (source.type === 'action' || record.resource_type === 'action_logs') return T_VIEW_ICON.activity;
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
  // Explicit null is Global and must not fall through to a binding or parent context.
  const businessId = Object.hasOwn(source, 'business_id') ? source.business_id
    : item.source && Object.hasOwn(item.source, 'business_id') ? item.source.business_id
    : item.conversation && Object.hasOwn(item.conversation, 'business_id') ? item.conversation.business_id : null;
  // A link or reference can carry a business id without a slug. Resolve the slug from the loaded
  // business list instead of falling back to the Project's or a fixed local slug.
  const knownSlug = businessId != null && typeof state !== 'undefined'
    ? (state.bizs || []).find(b => b.id === businessId || b.business_id === businessId || b.slug === businessId)?.slug || null
    : null;
  const explicitSlug = source.business_slug || item.context_business_slug || null;
  // A non-null business id without an accessible slug must be reported, never routed to /root.
  // A context slug is only a fallback for an item that has no explicit business id.
  const biz = explicitSlug || knownSlug || (businessId == null ? options.biz || null : null);
  const scopeMissing = businessId != null && !biz;
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
    scopeMissing,
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
    // core.modal calls onClose synchronously while closing, so settle the intended result first.
    const actions = [{label: 'Keep editing', onClick: close => { finish(false); close(); }}];
    if (typeof entry?.dashboard?.save === 'function') actions.push({label: 'Save', primary: true, onClick: async (close, el) => {
      const error = el.querySelector('[role="alert"]');
      try {
        const saved = await entry.dashboard.save();
        if (saved === false) throw new Error('The dashboard could not save. Your edits are still here.');
        if (!entry.alive) { finish(false); close(); return; }
        finish(true); close();
      } catch (failure) {
        if (!entry.alive) { finish(false); close(); return; }
        // Keep the dashboard and its retry/conflict UI alive. Close the confirmation as a
        // canceled navigation, never as a successful save.
        if (entry.dashboard) {
          const dashboardError = entry.dashHost?.querySelector('[data-story-error], [data-customer-error], .ferr');
          if (dashboardError) { dashboardError.textContent = uiFailure(failure); dashboardError.hidden = false; }
        }
        if (error) { error.textContent = uiFailure(failure); error.hidden = false; }
        finish(false); close();
      }
    }});
    actions.push({label: 'Discard', onClick: close => { finish(true); close(); }});
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
  let stack = [], disposed = false, pendingEviction = false;
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
    if (root && stack.length) {
      const last = entry();
      if (rwRecordKey(last.record) === key) {
        if (tab) selectTab(last, tab);
        return true;
      }
      // A different Focus/Threads selection starts a fresh Chat-first root path even when that
      // record already exists deeper in the old path. Its edits are guarded by discardAny().
      if (!await discardAny()) return false;
      if (disposed) return false;
      clearEntries();
    } else if (!root) {
      const existing = stack.find(item => rwRecordKey(item.record) === key);
      if (existing) {
        const above = [];
        while (stack.length && entry() !== existing) above.push(entry());
        if (above.length && !await confirmDiscardEntries(above)) return false;
        if (disposed || !existing.alive) return false;
        while (stack.length && entry() !== existing) removeEntry(stack.pop());
        reveal();
        return true;
      }
    }
    return pushEntry(record, tab);
  }
  async function navigate(record, {tab = null} = {}) {
    if (disposed || !record) return false;
    const existing = stack.find(item => rwRecordKey(item.record) === rwRecordKey(record));
    if (existing) {
      const above = [];
      while (stack.length && entry() !== existing) above.push(entry());
      if (above.length && !await confirmDiscardEntries(above)) return false;
      if (disposed || !existing.alive) return false;
      while (stack.length && entry() !== existing) removeEntry(stack.pop());
      reveal();
      return true;
    }
    return pushEntry(record, tab);
  }
  async function pushEntry(record, tab) {
    if (disposed) return false;
    if (stack.length >= RW_MAX_STACK) {
      const oldest = stack[0];
      if (rwDirty(oldest.dashboard)) {
        // Do not silently evict a dirty dashboard. The confirmation is non-blocking so a
        // scroll/animation loop cannot be held hostage; a real Discard/Save completes the
        // requested navigation once the owner has chosen.
        if (pendingEviction) return false;
        pendingEviction = true;
        void rwConfirmDiscard(oldest).then(chosen => {
          pendingEviction = false;
          if (!chosen || disposed || !oldest.alive) return;
          if (stack[0] !== oldest) return;
          removeEntry(stack.shift());
          if (!disposed) void pushEntry(record, tab);
        });
        reveal();
        return false;
      }
      if (disposed || !oldest.alive) return false;
      removeEntry(stack.shift());
    }
    // Leaving a dashboard for another record keeps the exact scroll it was hidden at.
    captureScroll(entry());
    const item = makeEntry(record);
    stack.push(item);
    recordsHost.append(item.recordEl);
    showEntry(item, {tab});
    paintBack();
    options.onTitleChange?.(record);
    return true;
  }
  // Prompt only for entries that would actually be removed; cancellation keeps the stack intact.
  async function confirmDiscardEntries(items) {
    for (const item of items) {
      if (!item?.alive || !rwDirty(item.dashboard)) continue;
      if (!await rwConfirmDiscard(item)) { reveal(); return false; }
      if (disposed || !item.alive) return false;
    }
    return true;
  }
  function reveal() {
    if (disposed) return;
    stack.forEach((item, index) => { item.recordEl.hidden = index !== stack.length - 1; });
    const item = entry();
    if (!item) return;
    item.recordEl.hidden = false;
    paintTabs(item);
    paintBack();
    restoreScroll(item);
    if (item.tab === 'dashboard') { try { item.dashboard?.onShow?.(); } catch (_) { /* its own error state */ } }
    mountChat(item);
    if (item.tab === 'chat') { showChat(item); void item.chat?.refresh?.(); }
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
    if (rwDirty(leaving.dashboard)) {
      if (!await rwConfirmDiscard(leaving)) { reveal(); return false; }
      if (disposed || !leaving.alive || entry() !== leaving) return false;
    }
    captureScroll(leaving);
    collapseChat(leaving);
    stack.pop();
    removeEntry(leaving);
    if (disposed) return false;
    const item = entry();
    if (!item?.alive) return false;
    item.recordEl.hidden = false;
    restoreScroll(item);
    paintBack();
    paintTabs(item);
    selectTab(item, item.tab, {initial: true, restore: true});
    options.onTitleChange?.(item.record);
    try { item.dashboard?.onShow?.(); } catch (_) { /* its own error state */ }
    if (item.tab === 'chat') void item.chat?.refresh?.();
    item.tabs.querySelector('[data-rw-tab]')?.focus({preventScroll: true});
    return true;
  }
  function hasUnsaved() {
    return stack.some(item => !item.alive ? false : rwDirty(item.dashboard));
  }
  async function discardAny() {
    // More than one dashboard on the Back path can be dirty; each gets its own explicit choice.
    const handled = new Set();
    for (;;) {
      if (disposed) return false;
      const dirty = stack.find(item => !handled.has(item) && rwDirty(item.dashboard)) || null;
      if (!dirty) return true;
      handled.add(dirty);
      if (!await rwConfirmDiscard(dirty)) { reveal(); return false; }
      if (disposed || !dirty.alive) return false;
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
    if (disposed) return;
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
  if (record.scopeMissing) throw new Error('This record’s Project scope is unavailable.');
  // Every Project inventory entry is authorized through the same read model before any
  // dashboard-specific read. A row opened from a stale inventory page cannot bypass it.
  if (record.project_id && !record.project_item_prefetched) {
    const authorized = await coreData(await post('/root/operations/project_item_get', {scope: 'global', project_id: record.project_id,
      item: {type: record.source.type, id: record.source.id, business_id: record.source.business_id ?? null}}));
    if (!ctx.alive()) return null;
    if (!authorized.item) throw new Error('This record is no longer in this Project.');
    record.project_item_prefetched = authorized;
  }
  const source = record.source;
  const path = !record.biz ? '/root' : `/biz/${encodeURIComponent(record.biz)}`;
  const call = async (operation, args = {}, options = {}) => coreData(await post(`${path}/operations/${operation}`, {...args, ...options}));
  if (source.type === 'project') return rwProjectDashboard(host, record, ctx, call);
  if (source.type === 'signoff') return rwSignoffDashboard(host, record, ctx);
  if (source.type === 'action' && !record.project_id) return rwActionDashboard(host, record, ctx, call);
  if ((source.type === 'table' || record.resource_type === 'data_tables') && !record.project_id) return rwTableDashboard(host, record, ctx, call);
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
        // Explicit null means Global and is preserved; an omitted scope inherits the Task's scope.
        const business_id = Object.hasOwn(other, 'business_id') ? other.business_id : (record.source.business_id ?? null);
        const target = rwRecordFromItem({source: {type: other.type, id: other.id, business_id,
          business_slug: other.business_slug ?? null, business_name: other.business_name, label: other.label || other.title,
          resource_type: other.resource_type, revision: other.revision, state: other.state}});
        return target ? ctx.navigate(target) : null;
      },
      beforeNavigate: () => {},
      navigate: link => ctx.navigate(link),
    });
    controller = dashboard;
    return controller;
  }
  const navigate = target => ctx.navigate(rwRecordFromItem({source: {
    type: target.type, id: target.id, business_id: target.business_id ?? null, business_slug: target.business_slug ?? null,
    business_name: target.business_name, label: target.label, resource_type: target.resource_type, state: target.state,
    task_type: target.task_type}}, {biz: null}));
  const relatedMount = (relatedHost, fresh) => rwTaskRelatedMount(relatedHost, record, fresh, ctx);
  if (record.source.business_id == null && typeof globalTasksMount === 'function') {
    const token = globalTasksMount(host, {initialTaskId: task.id, onBack: () => {}, embedded: true, onNavigate: navigate, renderRelated: relatedMount});
    return {dirty: () => !!token?.dirty?.(), hasUnsaved: () => !!token?.dirty?.(), dispose() { token?.dispose?.(); host.replaceChildren(); }};
  }
  const mounted = tDetail(task, {host, onClose: () => {}, embedded: true, navigate, relatedMount});
  return {dirty: () => !!mounted?.dirty?.(), hasUnsaved: () => !!mounted?.dirty?.(), dispose() { mounted?.close?.(); host.replaceChildren(); }};
}

// The Task dashboard and global-task editor share one direct related-record reader. It never opens
// the legacy Related-context modal: every row is the same in-pane workspace navigation.
function rwTaskRelatedMount(host, record, task, ctx) {
  let generation = 0;
  const path = !record.biz ? '/root' : `/biz/${encodeURIComponent(record.biz)}`;
  const call = async (operation, args) => coreData(await post(`${path}/operations/${operation}`, args));
  async function load() {
    const own = ++generation;
    host.innerHTML = '<span class="muted">Loading related records…</span>';
    try {
      const data = await call('relationships_list', {item: {type: 'task', id: task.id}, direction: 'both', include_archived: false, limit: 50});
      if (own !== generation || !host.isConnected || !ctx.alive()) return;
      host.replaceChildren();
      const seen = new Set();
      for (const edge of data.items || []) {
        const other = edge.source?.type === 'task' && edge.source?.id === task.id ? edge.target_item : edge.source_item;
        if (!other || !other.id || other.type === 'agent') continue;
        const key = `${other.type}:${other.business_id ?? 'global'}:${other.id}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const row = rwNode('div', 'rw-related-row');
        const open = rwNode('button', 'quiet rw-related-open');
        open.type = 'button';
        open.innerHTML = `${rwTypeIcon({source: {type: other.type}, resource_type: other.resource_type, task_type: other.task_type})}<span>${esc(rwTypeLabel({source: {type: other.type}, resource_type: other.resource_type, task_type: other.task_type}))}</span> ${esc(other.label || other.title || other.id)}`;
        open.onclick = () => {
          const target = rwRecordFromItem({source: {type: other.type, id: other.id, business_id: other.business_id ?? null,
            business_slug: other.business_slug ?? null, business_name: other.business_name, label: other.label || other.title,
            resource_type: other.resource_type, revision: other.revision, state: other.state, task_type: other.task_type}},
            {biz: null});
          if (target) void ctx.navigate(target);
        };
        row.append(open);
        host.append(row);
      }
      if (!host.children.length) host.innerHTML = '<p class="muted">No related records.</p>';
    } catch (error) {
      if (own !== generation || !host.isConnected) return;
      host.replaceChildren();
      host.append(rwNode('span', 'muted', uiFailure(error, {load: true}) + ' '));
      const retry = rwNode('button', 'quiet', 'Retry related records'); retry.type = 'button';
      retry.onclick = () => { void load(); };
      host.append(retry);
    }
  }
  void load();
  return {refresh: load, dispose() { generation++; }};
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
  return {dirty: () => view.hasUnsaved(), hasUnsaved: () => view.hasUnsaved(), save: () => view.save(),
    dispose() { view.dispose(); host.replaceChildren(); }};
}

// ---------------------------------------------------------------- Action
function rwActionContent(host, action) {
  const when = action.occurred?.instant || action.occurred?.date || action.occurred_at || '';
  const person = action.performed_by?.name || action.recorded_by?.name || '';
  host.innerHTML = `<section class="rw-reader rw-action"><h3>Action</h3>
    <p class="rw-action-text">${esc(action.text || 'No action text available.')}</p>
    <dl class="rw-action-meta">
      <div><dt>Category</dt><dd>${esc(action.category || 'Unclassified')}</dd></div>
      <div><dt>When</dt><dd>${esc(when ? rwFormatTime(when) : 'Undated')}</dd></div>
      ${person ? `<div><dt>Recorded by</dt><dd>${esc(person)}</dd></div>` : ''}
      <div><dt>Revision</dt><dd>${esc(String(action.revision ?? '—'))}</dd></div>
    </dl>
    ${action.withdrawn ? '<p class="rw-reader-note">This Action was withdrawn.</p>' : ''}
    ${action.detail ? `<section class="rw-reader-section"><h4>Details</h4><p>${esc(action.detail)}</p></section>` : ''}
    ${action.refs && Object.keys(action.refs).length ? `<section class="rw-reader-section"><h4>References</h4>${Object.entries(action.refs).map(([key, value]) => `<div class="rw-action-ref"><span class="muted">${esc(key)}</span> ${esc(String(value))}</div>`).join('')}</section>` : ''}
  </section>`;
}
async function rwActionDashboard(host, record, ctx, call) {
  host.classList.add('record-panel');
  const data = await call('actions_get', {action_id: record.source.id});
  if (!ctx.alive()) return null;
  const action = data.action;
  if (!action) throw new Error('Action is no longer available.');
  rwActionContent(host, action);
  return {dirty: () => false, onShow: async () => {
    try {
      const next = await call('actions_get', {action_id: record.source.id});
      if (ctx.alive() && next.action) rwActionContent(host, next.action);
    } catch (_) { /* the existing authorized read remains until a new one succeeds */ }
  }, dispose() { host.replaceChildren(); }};
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
  let revalidate = null;
  if (record.project_id) {
    const data = record.project_item_prefetched || await coreData(await post('/root/operations/project_item_get', {scope: 'global', project_id: record.project_id,
      item: {type: record.source.type, id: record.source.id, business_id: record.source.business_id ?? null}}));
    if (!ctx.alive()) return null;
    item = data.item || {}; content = data.record || {}; authorized = true;
    const nextLabel = item.label || content.label;
    if (nextLabel && nextLabel !== record.label) {
      // title() compares against the entry's current label; call it before mutating the descriptor.
      ctx.title?.({label: nextLabel});
      if (record.label !== nextLabel) record.label = nextLabel;
    }
    revalidate = async () => {
      try {
        const check = await coreData(await post('/root/operations/project_item_get', {scope: 'global', project_id: record.project_id,
          item: {type: record.source.type, id: record.source.id, business_id: record.source.business_id ?? null}}));
        if (!ctx.alive()) return;
        if (!check.item) throw Object.assign(new Error('Unavailable'), {code: 'not_found'});
      } catch (failure) {
        if (!ctx.alive() || !(accessLost(failure) || sourceGone(failure))) return;
        host.replaceChildren(rwNode('p', 'muted rw-unavailable', 'This record is unavailable. It may have been removed, or you may not have access to it.'));
      }
    };
  }
  // A Focus Reference is resolved from the authorized Focus item when the cached DTO has no target.
  if (record.source.type === 'reference' && !record.url && !record.opening_target?.url) {
    const focused = await coreData(await post('/root/operations/focus_get', {scope: 'global', item_id: record.source.id}));
    if (!ctx.alive()) return null;
    const target = focused.item?.reference?.target || focused.item?.opening_target;
    if (target?.url) record.url = target.url;
    else if (focused.item?.reference?.title) record.summary = 'This Reference has no openable target.';
  }
  // Ordinary (non-Project) documents, Context entities and people use the same authorized readers,
  // mounted in this pane instead of a modal. Project inventory records were authorized above.
  if (!record.project_id && ['document', 'documents', 'material', 'topic', 'person', 'entity', 'context_entities'].includes(record.source.type)
    || !record.project_id && ['documents', 'context_entities'].includes(record.resource_type)) {
    const refCtx = {...ctx, biz: record.biz || null, current: () => ctx.alive()};
    const reader = await recordOpenContext(refCtx, {type: ['documents', 'context_entities'].includes(record.source.type) ? (record.source.type === 'documents' ? 'document' : 'entity') : record.source.type,
      id: record.source.id, label: record.label, resource_type: record.resource_type}, {host, alive: () => ctx.alive()});
    return reader || {dirty: () => false, dispose() { host.replaceChildren(); }};
  }
  // Project inventory envelopes: these are the per-family records, never a flat preview blob.
  if (content.action) {
    rwActionContent(host, content.action);
    return {dirty: () => false, onShow: revalidate, dispose() { host.replaceChildren(); }};
  }
  if (content.document || content.note) {
    const doc = content.document || content.note;
    if ((doc.body || doc.text) && !doc.next_cursor) {
      host.innerHTML = `<section class="rw-reader rw-document"><h3>${esc(doc.title || item.label || record.label || 'Document')}</h3>
        <div class="work-source">${esc(doc.body || doc.text)}</div>
        <p class="muted small">${doc.kind ? esc(doc.kind) + ' · ' : ''}Revision ${esc(String(doc.revision ?? '—'))}</p></section>`;
      return {dirty: () => false, onShow: revalidate, dispose() { host.replaceChildren(); }};
    }
    const reader = await recordOpenDocument({...ctx, biz: record.biz || null, current: () => ctx.alive()}, record,
      {label: record.label, ...doc}, {host, alive: () => ctx.alive()});
    return reader || {dirty: () => false, dispose() { host.replaceChildren(); }};
  }
  if (content.table) {
    const box = rwNode('section', 'rw-reader rw-table-reader');
    box.append(rwNode('h3', '', content.table.title || item.label || record.label || 'Table'));
    host.append(box);
    const controller = await rwTableBody(box, record, ctx, call);
    return {dirty: () => false, onShow: revalidate, dispose() { controller?.dispose?.(); host.replaceChildren(); }};
  }
  if (content.asset || content.entity || content.person) {
    const nested = content.asset || content.entity || content.person;
    const box = rwNode('section', 'rw-reader');
    box.append(rwNode('h3', '', nested.label || nested.title || item.label || 'Context'));
    const body = nested.body || nested.summary || nested.about || nested.text;
    if (body) box.append(rwNode('div', 'work-source', String(body)));
    const lines = [];
    for (const [label, key] of [['Kind', 'kind'], ['Role', 'role'], ['Organisation', 'org'], ['Email', 'email'], ['Phone', 'phone'], ['Revision', 'revision']]) {
      const value = nested[key];
      if (value != null && value !== '') lines.push(`${label}: ${value}`);
    }
    if (lines.length) box.append(rwNode('p', 'muted', lines.join(' · ')));
    if (/^https?:\/\//.test(nested.url || '')) {
      const link = rwNode('a', '', 'Open link'); link.href = nested.url; link.target = '_blank'; link.rel = 'noopener noreferrer'; box.append(link);
    }
    if (!body && !lines.length) box.append(rwNode('p', 'muted', 'No further detail is recorded.'));
    host.append(box);
    return {dirty: () => false, onShow: revalidate, dispose() { host.replaceChildren(); }};
  }
  if (content.email || record.resource_type === 'email_conversations') {
    host.classList.add('record-panel');
    historicalEmailMount(host);
    return {dirty: () => false, dispose() { host.replaceChildren(); }};
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
    const box = rwNode('section', 'rw-reader rw-table-reader');
    box.append(rwNode('h3', '', item.label || record.label || 'Table'));
    host.append(box);
    const controller = await rwTableBody(box, record, ctx, call);
    return {dirty: () => false, hasUnsaved: () => false, onShow: revalidate, dispose() { controller?.dispose?.(); host.replaceChildren(); }};
  }
  if (authorized) {
    if (content.reminder) { const reader = rwReminderReader(host, record, item, content.reminder); if (revalidate) reader.onShow = revalidate; return reader; }
    if (content.routine) { const reader = rwRoutineReader(host, record, ctx, content.routine); if (revalidate) reader.onShow = revalidate; return reader; }
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
    return {dirty: () => false, hasUnsaved: () => false, onShow: revalidate, dispose() { host.replaceChildren(); }};
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

// The existing Tables workspace writes to the global drawer. A record dashboard keeps the
// canonical table read in this pane instead: same operations and columns, no exit button.
async function rwTableBody(host, record, ctx, call) {
  const id = record.source.id;
  const tableData = await call('tables_get', {table_id: id, ...(record.biz ? {} : {scope: 'global'})});
  if (!ctx.alive()) return null;
  const table = tableData.table;
  if (!table) throw new Error('This table is no longer available.');
  const title = rwNode('h4', '', table.title || record.label || 'Table');
  const description = table.description ? rwNode('p', 'muted', table.description) : null;
  const status = rwNode('p', 'muted', `Loading rows…`);
  const wrap = rwNode('div', 'rw-table-wrap');
  const grid = rwNode('table', 'rw-table');
  const more = rwNode('button', 'quiet', 'More rows'); more.type = 'button'; more.hidden = true;
  // The Project reader already prints the table title as its h3; do not repeat it.
  const alreadyTitled = [...host.children].some(child => child.tagName === 'H3');
  host.append(...(alreadyTitled ? [] : [title]), ...(description ? [description] : []), status, wrap, more);
  wrap.append(grid);
  let cursor = null, rows = [];
  const value = raw => raw == null ? '' : typeof raw === 'object' ? JSON.stringify(raw) : String(raw);
  const paint = () => {
    grid.replaceChildren();
    const head = rwNode('thead'), hr = rwNode('tr');
    for (const column of table.columns || []) { const th = rwNode('th', '', column.name || column.id); hr.append(th); }
    head.append(hr); grid.append(head);
    const body = rwNode('tbody');
    for (const row of rows) {
      const tr = rwNode('tr');
      for (const column of table.columns || []) {
        const td = rwNode('td', '', value(row.cells && Object.hasOwn(row.cells, column.id) ? row.cells[column.id] : null));
        tr.append(td);
      }
      body.append(tr);
    }
    grid.append(body);
    status.textContent = rows.length ? `${rows.length}${cursor ? '+' : ''} rows` : 'No rows';
  };
  try {
    const data = await call('tables_rows_list', {table_id: id, limit: 50, ...(record.biz ? {} : {scope: 'global'})});
    if (!ctx.alive()) return null;
    if (data.table_revision != null && table.revision != null && data.table_revision !== table.revision) throw new Error('This table changed while loading. Open it again to read the current version.');
    rows = data.items || []; cursor = data.next_cursor || null;
    paint();
    more.hidden = !cursor;
    more.onclick = async () => {
      more.disabled = true;
      try {
        const next = await call('tables_rows_list', {table_id: id, limit: 50, ...(cursor ? {cursor} : {}), ...(record.biz ? {} : {scope: 'global'})});
        if (!ctx.alive()) return;
        rows = [...rows, ...(next.items || [])]; cursor = next.next_cursor || null; paint(); more.hidden = !cursor;
      } catch (failure) {
        if (!ctx.alive()) return;
        status.textContent = uiFailure(failure, {load: true});
      } finally { more.disabled = false; }
    };
  } catch (failure) {
    if (!ctx.alive()) return null;
    status.textContent = uiFailure(failure, {load: true});
  }
  return {dirty: () => false, hasUnsaved: () => false, dispose() { host.replaceChildren(); }};
}
async function rwTableDashboard(host, record, ctx, call) {
  host.classList.add('record-panel');
  const body = await rwTableBody(host, record, ctx, call);
  return body || {dirty: () => false, hasUnsaved: () => false, dispose() { host.replaceChildren(); }};
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
  // Pass the authoritative name as a separate descriptor so the workspace sees the old/new difference.
  if (project.name && project.name !== record.label) {
    ctx.title?.({label: project.name});
    if (record.label !== project.name) record.label = project.name;
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
    shell.querySelector('[data-project-archived]').setAttribute('aria-pressed', String(shell.dataset.includeArchived === 'true'));
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
    if (!append && (state.key !== key || fresh)) {
      // Revalidating a visible, already-authorized page keeps its rows until the fresh read
      // succeeds or explicitly revokes them; it never flashes an empty cache.
      const preserve = fresh && state.loaded && state.key === key;
      if (!preserve) state.items = [];
      state.cursor = null; state.loaded = false; state.key = key;
    }
    state.loading = true;
    if (section === activeSection) { more.disabled = true; errorBox.hidden = true; errorBox.replaceChildren(); paint(section); }
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
      // A revoked read invalidates the previously readable page too; keeping those rows would
      // leave denied content visible after access was lost.
      if (accessLost(failure) || sourceGone(failure)) { state.items = []; state.cursor = null; }
      else if (!append) state.items = [];
      state.loaded = true;
      if (section === activeSection) {
        const unavailable = accessLost(failure) || sourceGone(failure);
        errorBox.replaceChildren(rwNode('span', '', unavailable ? 'This project inventory is unavailable.' : uiFailure(failure, {load: true}) + ' '));
        const retry = rwNode('button', 'quiet', 'Retry'); retry.type = 'button';
        retry.onclick = () => {
          if (disposed) return;
          state.loaded = false; state.key = ''; state.items = []; state.cursor = null;
          void load(section, false, true);
        };
        errorBox.append(retry);
        errorBox.hidden = false;
        paint(section);
      }
    } finally {
      state.loading = false;
      if (!disposed && section === activeSection) { more.disabled = false; paint(section); }
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
    // Re-read a section when it becomes visible; a cached page must not outlive revoked access.
    void load(section, false, true);
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
    shell.dataset.includeArchived = String(button.getAttribute('aria-pressed') === 'true');
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
  async function revalidate() {
    if (disposed) return;
    try {
      const fresh = await rwProjectCall('project_record_get', {project_id: projectId});
      if (disposed) return;
      if (!fresh.project) throw Object.assign(new Error('This Project is unavailable.'), {code: 'not_found'});
      if (fresh.project.name && fresh.project.name !== record.label) {
        ctx.title?.({label: fresh.project.name});
        if (record.label !== fresh.project.name) record.label = fresh.project.name;
      }
      shell.querySelector('.rw-project-name').textContent = fresh.project.name || record.label || 'Project';
      shell.querySelector('.rw-project-summary').textContent = fresh.project.summary || '';
      shell.querySelector('.rw-project-archived').hidden = !fresh.project.archived;
      await load(activeSection, false, true);
    } catch (failure) {
      if (disposed) return;
      stateFor(activeSection).items = []; stateFor(activeSection).cursor = null;
      list.replaceChildren();
      errorBox.replaceChildren(rwNode('span', '', accessLost(failure) || sourceGone(failure) ? 'This project inventory is unavailable.' : uiFailure(failure, {load: true})));
      errorBox.hidden = false;
      paint(activeSection);
    }
  }
  return {
    dirty: () => false, hasUnsaved: () => false, onShow: revalidate,
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
          if (accessLost(failure) || sourceGone(failure)) {
            results.replaceChildren(); cursor = null; more.hidden = true; empty.hidden = true;
          }
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