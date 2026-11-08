// The shared record workspace. A selected record shows exactly one
// full-width pane: its dashboard, private conversation, or task Activity. The header keeps
// Back (in-pane related navigation), the record title, the record-type, Chat and Activity tabs,
// and Close. Entries stay mounted in a bounded navigation stack so tab changes and Back
// preserve dashboard edits, drafts and scroll instead of rebuilding conversations.
const RW_MAX_STACK = 20;
// Unsaved CRM cell/stage edits made in a record workspace; keyed by Table scope so they survive tab switches.
const rwCrmDrafts = new Map();
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
const RW_THREAD_TYPES = new Set(['task', 'signoff', 'project', 'reference', 'table']);
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
  if (source.type === 'material') return 'Material';
  if (source.type === 'person') return 'Person';
  if (source.type === 'topic') return 'Topic';
  if (source.type === 'dev_ticket') return 'System Dev report';
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
  if (source.type === 'action' || source.type === 'dev_ticket' || record.resource_type === 'action_logs') return T_VIEW_ICON.activity;
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
  const hasBusinessId=Object.hasOwn(source,'business_id')
    || !!(item.source && Object.hasOwn(item.source,'business_id'))
    || !!(item.conversation && Object.hasOwn(item.conversation,'business_id'));
  const businessId = Object.hasOwn(source, 'business_id') ? source.business_id
    : item.source && Object.hasOwn(item.source, 'business_id') ? item.source.business_id
    : item.conversation && Object.hasOwn(item.conversation, 'business_id') ? item.conversation.business_id : null;
  // A link or reference can carry a business id without a slug. Resolve the slug from the loaded
  // business list instead of falling back to the Project's or a fixed local slug.
  const knownSlug = businessId != null && typeof state !== 'undefined'
    ? (state.bizs || []).find(b => b.id === businessId || b.business_id === businessId || b.slug === businessId)?.slug || null
    : null;
  const explicitSlug = businessId != null ? (source.business_slug || item.context_business_slug || null) : null;
  // A non-null business id without an accessible slug must be reported, never routed to /root.
  // A context slug is only a fallback for an item that has no explicit business id.
  const biz = businessId == null ? (hasBusinessId ? null : options.biz || null) : (explicitSlug || knownSlug || null);
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
  }, {project_id: project?.id ?? null,
    ...(item.type === 'dev_ticket' ? {can_thread: false} : item.can_thread === undefined ? {} : {can_thread: item.can_thread})});
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
    actions.push({label: 'Discard', onClick: close => { try { entry?.dashboard?.discard?.(); } catch (_) { /* dropped with the dashboard */ } finish(true); close(); }});
    modal({stack: true, width: 460, title: 'Unsaved changes', body: '<p>This record dashboard has unsaved changes.</p><p class="ferr" role="alert" hidden></p>',
      actions, onClose: () => finish(false)});
  });
}

// ---------------------------------------------------------------- controller
// host receives the workspace. options:
//   visible(): whether the workspace is on screen for unread acknowledgment
//   onClose(): hide the surrounding pane after the record was closed by the owner
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
    entry, root: () => stack[0] || null, alive: () => !disposed, replace, invalidate, parentTable: () => {
      const item=entry();
      return item?.pairedParent || item?.crmTable || null;
    },
  };
  return api;

  // Leave decisions stay inside the Focus record and share one pending decision.
  function inlineChoice(item, message, choices) {
    if (item.leaveChoice) return item.leaveChoice.promise;
    let finish;
    const promise = new Promise(resolve => { finish = resolve; });
    const previousFocus = document.activeElement;
    const hostItem = item.recordEl.hidden ? entry() : item;
    const guarded = new Set([item.recordEl, hostItem?.recordEl].filter(Boolean));
    const inertStates = [];
    for (const recordEl of guarded) for (const child of recordEl.children) {
      inertStates.push([child, child.inert]); child.inert = true;
    }
    const panel = rwNode('section', 'rw-leave-choice');
    panel.setAttribute('role', 'group'); panel.setAttribute('aria-label', 'Unsaved work');
    panel.append(rwNode('p', '', message));
    const error = rwNode('p', 'ferr'); error.hidden = true; panel.append(error);
    const settle = value => {
      if (item.leaveChoice?.panel !== panel) return;
      item.leaveChoice = null; panel.remove();
      for (const [child, wasInert] of inertStates) child.inert = wasInert;
      if (previousFocus?.isConnected && !previousFocus.closest?.('[hidden], [inert]')) previousFocus.focus?.({preventScroll: true});
      finish(value);
    };
    item.leaveChoice = {promise, panel, settle};
    for (const choice of choices) {
      const button = rwNode('button', 'quiet', choice.label); button.type = 'button';
      button.onclick = async () => {
        if (panel.dataset.busy) return;
        panel.dataset.busy = 'yes'; panel.querySelectorAll('button').forEach(b => { b.disabled = true; });
        try { const result = await choice.run(); settle(result); }
        catch (failure) { error.textContent = uiFailure(failure); error.hidden = false; delete panel.dataset.busy; panel.querySelectorAll('button').forEach(b => { b.disabled = false; }); }
      };
      panel.append(button);
    }
    hostItem?.recordEl.append(panel); panel.querySelector('button')?.focus({preventScroll: true});
    return promise;
  }
  async function confirmRecordDiscard(item) {
    if (options.entry !== 'focus' || item.record.source.type !== 'task') return rwConfirmDiscard(item);
    const choices = [{label: 'Keep editing', run: () => false}];
    if (typeof item.dashboard?.save === 'function') choices.push({label: 'Save', run: async () => {
      if (await item.dashboard.save() === false) throw new Error('The dashboard could not save. Your edits are still here.');
      return true;
    }});
    choices.push({label: 'Discard', run: () => {
      if (typeof item.dashboard?.discard === 'function') item.dashboard.discard();
      else {
        item.dashboard?.dispose?.(); item.dashboard = null; item.dashboardLoaded = false;
        item.dashboardMounting = false; item.dashboardGeneration = (item.dashboardGeneration || 0) + 1;
        mountDashboard(item);
      }
      return true;
    }});
    return inlineChoice(item, 'This record dashboard has unsaved changes.', choices);
  }
  async function beforeLeave(item, {guardDirty = true} = {}) {
    if (!item?.alive) return false;
    if (!item.leaveGuard) {
      item.leaveGuard = (async () => {
        if (item.moveDialog && !await leaveMove(item)) return false;
        if (item.dashboard?.beforeLeave && await item.dashboard.beforeLeave() === false) return false;
        return !disposed && item.alive && entry() === item;
      })();
    }
    const guard = item.leaveGuard;
    let allowed;
    try { allowed = await guard; } finally { if (item.leaveGuard === guard) item.leaveGuard = null; }
    if (!allowed || disposed || !item.alive || entry() !== item) return false;
    if (guardDirty && rwDirty(item.dashboard) && !await confirmRecordDiscard(item)) return false;
    return !disposed && item.alive && entry() === item;
  }
  async function leaveMove(item) {
    const dialog = item.moveDialog;
    if (!dialog) return true;
    const status = dialog.status?.();
    if (!status?.pending && !status?.uncertain) { dialog.close(); return true; }
    const choices = [{label: 'Keep', run: () => false}];
    if (status.uncertain) choices.push({label: 'Retry move', run: () => { dialog.retry(); return false; }});
    choices.push({label: 'Leave', run: () => { item.leaveChoice?.settle(true); dialog.close(); return true; }});
    return inlineChoice(item, status.pending
      ? 'The move is still in progress. Leaving cannot undo a move that may succeed.'
      : 'The move outcome is unknown. Leaving cannot undo a move that may have succeeded.', choices);
  }
  function entry() { return stack[stack.length - 1] || null; }
  function current() { const item = entry(); return item && item.alive ? item.record : null; }
  function makeEntry(record) {
    const item = {
      record, alive: true, tab: options.paired ? 'dashboard' : record.can_thread ? 'chat' : 'dashboard', dashboardLoaded: false, dashboard: null,
      scroll: 0, recordEl: null, dashHost: null, chatHost: null, chat: null,
    };
    item.recordEl = rwNode('article', 'rw-record');
    item.recordEl.dataset.rwKey = rwRecordKey(record);
    item.recordEl.dataset.rwType = record.source.type;
    // Focus expands a record in its board, so that header leads with the pill Close the owner uses to leave it.
    const closeButton = options.closeFirst
      ? `<button type="button" class="quiet rw-close" aria-label="Close record"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg><span class="rw-close-label">Close</span></button>`
      : '';
    const closeIcon = options.closeFirst ? '' : `<button type="button" class="quiet rw-close" aria-label="Close record"><svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><path d="m6 6 12 12M18 6 6 18"/></svg></button>`;
    item.recordEl.innerHTML = `<header class="rw-head${options.closeFirst ? ' rw-close-first' : ''}">${closeButton}
      <button type="button" class="quiet rw-back" aria-label="Back" hidden>← Back</button>
      <span class="rw-context" hidden>CRM /</span><span class="rw-title" role="heading" aria-level="2"></span>
      <button type="button" class="quiet rw-move" hidden>Move to project…</button>
      <div class="rw-tabs" role="tablist" aria-label="Record views"></div>${closeIcon}
    </header><div class="rw-body"><div class="rw-pane rw-dashboard" role="tabpanel" tabindex="0"></div><div class="rw-pane rw-activity" role="tabpanel" tabindex="0" hidden></div><div class="rw-pane rw-chat" role="tabpanel" tabindex="0" hidden><div class="agfocus-conversation rw-conversation"></div></div></div>`;
    item.dashHost = item.recordEl.querySelector('.rw-dashboard');
    item.chatHost = item.recordEl.querySelector('.rw-chat');
    item.activityHost = item.recordEl.querySelector('.rw-activity');
    item.conversationHost = item.recordEl.querySelector('.rw-conversation');
    item.panelId = 'rw-' + (++rwPanelSequence);
    item.dashHost.id = item.panelId + '-dashboard';
    item.chatHost.id = item.panelId + '-chat';
    item.activityHost.id = item.panelId + '-activity';
    item.tabs = item.recordEl.querySelector('.rw-tabs');
    item.tabs.innerHTML = tabsHtml(record, item);
    // The record title is the canonical label; Project cards never use a Focus display-name override.
    titlePaint(item);
    item.back = item.recordEl.querySelector('.rw-back');
    item.back.onclick = () => { void back(); };
    item.moveButton = item.recordEl.querySelector('.rw-move');
    item.moveButton.onclick = () => { void moveRecord(item); };
    paintMove(item);
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
    if (record.can_thread && !options.paired) tabs.push(`<button type="button" class="quiet rw-tab" role="tab" data-rw-tab="chat" id="${item.chatHost.id}-tab" aria-controls="${item.chatHost.id}" aria-label="Chat" title="Chat">${RW_CHAT_ICON}</button>`);
    if (record.source.type === 'task') tabs.push(`<button type="button" class="quiet rw-tab" role="tab" data-rw-tab="activity" id="${item.activityHost.id}-tab" aria-controls="${item.activityHost.id}" aria-label="Activity" title="Activity"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" aria-hidden="true"><path d="M3 12a9 9 0 1 0 2.7-6.4L3 8M3 3v5h5"/><path d="M12 7v5l3 2"/></svg></button>`);
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
  // A Task conversion changes the dashboard's subtype while its source identity and Chat
  // stay put. Update only the existing type tab's icon/label; never rebuild the chat tab.
  function paintTypeTab(item) {
    const tab = item.tabs.querySelector('[data-rw-tab="dashboard"]');
    if (!tab) return;
    tab.setAttribute('aria-label', rwTypeLabel(item.record) + ' dashboard');
    tab.title = rwTypeLabel(item.record) + ' dashboard';
    tab.innerHTML = rwTypeIcon(item.record);
    paintMove(item);
  }
  // Move to project belongs to a saved ordinary Task, Story or Customer; its scope is the record's own canonical business_id.
  function paintMove(item) {
    const source = item.record?.source;
    const movable = !!source && source.type === 'task' && (item.record.task_type || source.task_type) !== 'email'
      && source.business_id !== undefined && !item.record.scopeMissing && !(source.business_id == null && item.record.biz);
    item.moveButton.hidden = !movable;
  }
  async function moveRecord(item) {
    if (disposed || !item.alive || item.moveDialog) return;
    if (item.dashboard?.beforeLeave && await item.dashboard.beforeLeave() === false) return;
    if (disposed || !item.alive || entry() !== item) return;
    if (rwDirty(item.dashboard)) {
      if (!await confirmRecordDiscard(item)) return;
      if (disposed || !item.alive || entry() !== item || item.moveDialog || rwDirty(item.dashboard)) return;
    }
    const record = item.record, source = record.source;
    let dialog = null, panel = null, body = null, previousFocus = null, dashboardScroll = 0;
    if (options.entry === 'focus') {
      previousFocus = document.activeElement; dashboardScroll = item.dashHost.scrollTop;
      body = item.recordEl.querySelector('.rw-body');
      panel = rwNode('section', 'rw-move-panel');
      body.hidden = true; body.inert = true;
      body.after(panel);
    }
    dialog = taskMoveOpen({
      hostedContent: panel,
      onRequestClose: () => { void leaveMove(item); },
      onClosed: () => {
        if (item.moveDialog === dialog || !dialog) item.moveDialog = null;
        if (panel) { panel.remove(); body.hidden = false; body.inert = false; item.dashHost.scrollTop = dashboardScroll; if (item.alive) previousFocus?.focus?.({preventScroll: true}); }
        item.leaveChoice?.settle(false);
      },
      task: {id: source.id, title: record.label, task_type: record.task_type},
      sourceBusinessId: source.business_id ?? null, sourceSlug: record.biz || null,
      onSourceLost: () => { if (item.alive) void rwRecordUnavailable(item); },
      onSourceMoved: error => showMoved(item, error), sourceName: source.business_id == null ? 'Global' : (record.business_name || ''),
      onMoved: reference => { void replaceWithReference(item, reference); },
      onMovedElsewhere: reference => { void replaceWithReference(item, reference); },
    });
    if (!dialog) { panel?.remove(); if (body) { body.hidden = false; body.inert = false; } return; }
    item.moveDialog = dialog;
    panel?.querySelector('select')?.focus({preventScroll: true});
  }
  // The moved Task keeps its id; its new scope is a new canonical record. Replace this entry in place so Back never
  // returns to the old-scope copy.
  function replaceWithReference(item, reference) {
    const target = rwRecordFromItem({source: {type: 'task', id: reference.id, business_id: reference.business_id, business_slug: reference.business_slug,
      business_name: reference.business_name, label: reference.label, task_type: reference.task_type}});
    return target ? replace(item, target, {tab: item.tab, confirmedMove: true}) : false;
  }
  async function replace(oldItem, record, {tab = null, confirmedMove = false} = {}) {
    if (disposed || !record) return false;
    if (!confirmedMove && oldItem === entry() && !await beforeLeave(oldItem)) return false;
    const index = stack.indexOf(oldItem);
    if (index < 0 || index !== stack.length - 1) return navigate(record, {tab});
    stack.pop();
    removeEntry(oldItem);
    return pushEntry(record, tab);
  }
  // A record_moved reply names the authorized canonical destination; the owner opens it from this notice.
  function showMoved(item, error) {
    if (!item?.alive) return;
    const where = taskMoveDestination(error);
    if (!where) return;
    item.recordEl.querySelector('.rw-moved')?.remove();
    const notice = rwNode('p', 'rw-moved ferr');
    notice.setAttribute('role', 'alert');
    notice.append(rwNode('span', '', `This record moved to ${where.destination.name || 'another project'}. `));
    const open = rwNode('button', 'quiet', 'Open in new project'); open.type = 'button';
    open.onclick = async () => {
      if (!await beforeLeave(item)) return;
      if (disposed || !item.alive) return;
      void replaceWithReference(item, {id: where.task.id, business_id: where.task.business_id ?? where.destination.id,
        business_slug: where.destination.slug ?? null, business_name: where.destination.name ?? null, label: item.record.label,
        task_type: item.record.task_type});
    };
    notice.append(open);
    item.recordEl.querySelector('.rw-head').after(notice);
  }
  function titlePaint(item) {
    const title = item.record.label || 'Record';
    const el = item.recordEl.querySelector('.rw-title');
    if (el.textContent !== title) el.textContent = title;
    el.removeAttribute('title');
    const context=item.recordEl.querySelector('.rw-context');
    context.hidden=!item.pairedParent;
    if(item.pairedParent)context.textContent=`${item.pairedParent.label || 'CRM'} /`;
    if(item.pairedParent)item.recordEl.dataset.navigationContext='crm-record';
    else delete item.recordEl.dataset.navigationContext;
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
    let pairedReady = false;
    if (root && stack.length) {
      const last = entry();
      if (rwRecordKey(last.record) === key) {
        if (tab) selectTab(last, tab);
        return true;
      }
      // A different Focus selection starts a fresh Chat-first root path even when that
      // record already exists deeper in the old path. Its edits are guarded by discardAny().
      if (!await discardAny()) return false;
      if (disposed) return false;
      if (options.paired) {
        if (!(await pairedSelect(record, null))) return false;
        pairedReady = true;
      }
      clearEntries();
    } else if (!root) {
      const existing = stack.find(item => rwRecordKey(item.record) === key);
      if (existing) return finishExistingNavigation(existing, tab);
    }
    return pushEntry(record, tab, null, pairedReady);
  }
  async function navigate(record, {tab = null, parentTable = null} = {}) {
    if (disposed || !record) return false;
    const leaving = entry();
    const existing = stack.find(item => rwRecordKey(item.record) === rwRecordKey(record));
    if (leaving && rwRecordKey(leaving.record) !== rwRecordKey(record) && !await beforeLeave(leaving, {guardDirty: !!existing})) return false;
    if (existing) return finishExistingNavigation(existing, tab, leaving);
    return pushEntry(record, tab, parentTable);
  }
  // Returning to an earlier record pops only the finite slice above it. Collect that slice
  // once (never loop on an unchanged stack), guard it, then recheck the stack before removal.
  async function finishExistingNavigation(existing, tab = null, guardedItem = null) {
    const index = stack.indexOf(existing);
    if (index < 0) return false;
    const above = stack.slice(index + 1);
    if (above.length && !await confirmDiscardEntries(above, guardedItem)) return false;
    if (disposed || !existing.alive) return false;
    const now = stack.indexOf(existing);
    if (now < 0) return false;
    const currentAbove = stack.slice(now + 1);
    if (currentAbove.length !== above.length || currentAbove.some((item, i) => item !== above[i])) {
      reveal();
      return false;
    }
    if (options.paired && !(await pairedSelect(existing.record, existing.pairedParent))) { reveal(); return false; }
    for (const item of currentAbove) removeEntry(item);
    stack.splice(now + 1);
    if (tab) selectTab(existing, tab, {initial: true, restore: true});
    reveal();
    return true;
  }
  async function pushEntry(record, tab, parentTable = null, pairedReady = false) {
    if (disposed) return false;
    if (stack.length >= RW_MAX_STACK) {
      const oldest = stack[0];
      if (rwDirty(oldest.dashboard)) {
        // Do not silently evict a dirty dashboard. The confirmation is non-blocking so a
        // scroll/animation loop cannot be held hostage; a real Discard/Save completes the
        // requested navigation once the owner has chosen.
        if (pendingEviction) return false;
        pendingEviction = true;
        void confirmRecordDiscard(oldest).then(chosen => {
          pendingEviction = false;
          if (!chosen || disposed || !oldest.alive) return;
          if (stack[0] !== oldest) return;
          removeEntry(stack.shift());
          if (!disposed) void pushEntry(record, tab, parentTable, pairedReady);
        });
        reveal();
        return false;
      }
      if (disposed || !oldest.alive) return false;
      if (options.paired && !pairedReady) {
        if (!(await pairedSelect(record, parentTable))) return false;
        pairedReady = true;
      }
      removeEntry(stack.shift());
    }
    if (options.paired && !pairedReady && !(await pairedSelect(record, parentTable))) return false;
    // Leaving a dashboard for another record keeps the exact scroll it was hidden at.
    captureScroll(entry());
    const item = makeEntry(record);
    item.pairedParent = parentTable;
    titlePaint(item);
    stack.push(item);
    recordsHost.append(item.recordEl);
    showEntry(item, {tab});
    paintBack();
    options.onTitleChange?.(record);
    return true;
  }
  // Prompt only for entries that would actually be removed; cancellation keeps the stack intact.
  async function confirmDiscardEntries(items, guardedItem = null) {
    for (const item of items) {
      if (!item?.alive || item === guardedItem) continue;
      if (item === entry()) { if (!await beforeLeave(item)) { reveal(); return false; } continue; }
      if (!rwDirty(item.dashboard)) continue;
      if (!await confirmRecordDiscard(item)) { reveal(); return false; }
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
    if (!options.paired) mountChat(item);
    if (item.tab === 'chat') { showChat(item); void item.chat?.refresh?.(); }
    if (item.tab === 'activity') mountActivity(item);
    options.onTitleChange?.(item.record);
    syncChatVisibility();
  }
  function showEntry(item, {tab = null} = {}) {
    stack.forEach(other => { other.recordEl.hidden = other !== item; });
    paintTabs(item);
    paintBack();
    if (tab) item.tab = tab;
    else if (item.tab === 'chat' && (!item.record.can_thread || options.paired)) item.tab = 'dashboard';
    selectTab(item, item.tab, {initial: true});
    options.onTitleChange?.(item.record);
  }
  function paintBack() {
    const item = entry();
    if (!item) return;
    const prior = stack.length > 1 ? stack[stack.length - 2] : null;
    item.back.hidden = !prior;
    if (prior) {
      const label=item.pairedParent ? (item.pairedParent.label || 'CRM') : (prior.record.label || 'previous record');
      item.back.textContent=item.pairedParent ? `← Back to ${label}` : '← Back';
      item.back.setAttribute('aria-label', 'Back to ' + label);
    }
  }
  function removeEntry(item) {
    if (!item || !item.alive) return;
    item.alive = false;
    item.leaveChoice?.settle(false);
    try { item.moveDialog?.close(); } catch (_) { /* a dead dialog cannot keep the stack alive */ }
    item.moveDialog = null;
    item._scrollSave?.();
    item._scrollSave = null;
    try { item.dashboard?.dispose?.(); } catch (_) { /* a dead dashboard cannot keep the stack alive */ }
    item.dashboard = null;
    try { item.chat?.dispose?.(); } catch (_) { /* idem */ }
    item.chat = null;
    item.chatMounted = false;
    try { item.activity?.dispose(); } catch (_) { /* idem */ }
    item.activity = null;
    const focusedOutside = !item.recordEl.contains(document.activeElement);
    item.recordEl.remove();
    if (!focusedOutside) entry()?.tabs.querySelector('[data-rw-tab]')?.focus({preventScroll: true});
  }
  function clearEntries() {
    for (const item of stack.splice(0)) removeEntry(item);
  }
  function captureScroll(item) {
    if (!item?.recordEl || item.recordEl.hidden) return;
    if (!item.dashHost?.hidden) item.scroll = item.dashHost?.scrollTop || 0;
    if (!item.activityHost?.hidden) item.activityScroll = item.activityHost?.scrollTop || 0;
  }
  function restoreScroll(item) {
    if (!item?.dashHost) return;
    // A hidden/detached pane can lose its scroll position; restore the retained value when shown.
    item.dashHost.scrollTop = item.scroll || 0;
    if (!item.activityHost?.hidden) item.activityHost.scrollTop = item.activityScroll || 0;
  }
  async function back() {
    if (disposed || stack.length < 2) return false;
    const leaving = entry();
    // Back restores the prior record; leaving a dashboard with unsaved edits still asks first.
    if (!await beforeLeave(leaving)) { reveal(); return false; }
    const prior = stack[stack.length - 2];
    if (options.paired && !(await pairedSelect(prior.record, prior.pairedParent))) { reveal(); return false; }
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
    const currentEntry = entry();
    if (currentEntry && !await beforeLeave(currentEntry)) return false;
    const handled = new Set();
    for (;;) {
      if (disposed) return false;
      const dirty = stack.find(item => !handled.has(item) && rwDirty(item.dashboard)) || null;
      if (!dirty) return true;
      handled.add(dirty);
      if (!await confirmRecordDiscard(dirty)) { reveal(); return false; }
      if (disposed || !dirty.alive) return false;
    }
  }
  async function pairedSelect(record, parentTable) {
    if (!options.paired || typeof options.onPairedSource !== 'function') return true;
    return (await options.onPairedSource(record || null, parentTable || null)) !== false;
  }
  async function invalidate(record) {
    const target=record?.source ? rwRecordKey(record) : record?.type ? rwSourceKey(record) : String(record || '');
    const item=stack.find(candidate=>rwRecordKey(candidate.record)===target);
    if(!item)return false;
    await rwRecordUnavailable(item);
    return true;
  }
  function refresh() {
    const item = entry();
    if (item?.alive) {
      item.chat?.refresh?.();
      if (item.tab === 'activity') item.activity?.refresh();
    }
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
    if (options.paired && !(await pairedSelect(null, null))) return;
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
    if (tab === 'activity' && item.record.source.type !== 'task') tab = 'dashboard';
    if (tab === 'chat' && (!item.record.can_thread || options.paired)) tab = 'dashboard';
    // A hidden dashboard is at scrollTop 0; never let the return toggle overwrite its saved position.
    if (!restore) captureScroll(item);
    const previous = item.tab;
    item.tab = tab;
    item.recordEl.dataset.tab = tab;
    item.dashHost.hidden = tab !== 'dashboard';
    item.chatHost.hidden = tab !== 'chat';
    item.activityHost.hidden = tab !== 'activity';
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
    if (tab === 'activity') { mountActivity(item); restoreScroll(item); }
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

  function mountActivity(item) {
    if (!item?.alive || item.record.source.type !== 'task') return;
    if (item.activity) { void item.activity.refresh(); return; }
    const record = item.record;
    item.activity = taskActivityMount(item.activityHost, {id: record.source.id}, record.biz || null, {
      revalidate: async () => {
        if (record.scopeMissing) throw Object.assign(new Error('Project unavailable'), {code: 'forbidden'});
        if (record.project_id) {
          const result = coreData(await post('/root/operations/project_item_get', {scope: 'global', project_id: record.project_id,
            item: {type: 'task', id: record.source.id, business_id: record.source.business_id ?? null}}));
          if (!result.item) throw Object.assign(new Error('Record unavailable'), {code: 'not_found'});
        }
      },
      openReference: ref => rwOpenChatReference(item, ref),
      onUnavailable: () => { void rwRecordUnavailable(item); },
      onMoved: error => showMoved(item, error),
    });
  }

  // -------------------------------------------------------------- dashboard mount
  function mountDashboard(item) {
    if (!item?.alive || item.dashboardLoaded || item.dashboardMounting) return;
    item.dashboardMounting = true;
    const attempt = (item.dashboardGeneration = (item.dashboardGeneration || 0) + 1);
    const host = item.dashHost;
    host.innerHTML = '<p class="muted rw-status" role="status">Loading…</p>';
    const alive = () => item.alive && attempt === item.dashboardGeneration && host.isConnected && item.dashHost === host;
    const workspace = api;
    const record = item.record;
    // A subtype adoption carries the exact canonical Task response into the replacement
    // dashboard; it must not depend on a second read that another writer could have changed.
    const prefetchedTask = item.prefetchedTask || null;
    item.prefetchedTask = null;
    const navigate = (target, navigationOptions = {}) => {
      if (!target) return;
      void workspace.navigate(target, navigationOptions);
    };
    const revalidateProject = async () => {
      if (!record.project_id) return true;
      try {
        const check = await coreData(await post('/root/operations/project_item_get', {scope: 'global', project_id: record.project_id,
          item: {type: record.source.type, id: record.source.id, business_id: record.source.business_id ?? null}}));
        if (!alive()) return false;
        if (!check.item) throw Object.assign(new Error('This record is no longer in this Project.'), {code: 'not_found'});
        return true;
      } catch (failure) {
        if (!alive()) return false;
        if (accessLost(failure) || sourceGone(failure)) { void rwRecordUnavailable(item); return false; }
        return true;
      }
    };
    const moved = failure => showMoved(item, failure);
    const changed = message => {
      if (!alive()) return;
      if (item.dashboard?.changed) { item.dashboard.changed(message); return; }
      let notice = item.dashHost.querySelector('.rw-changed');
      if (!notice) {
        notice = rwNode('p', 'rw-changed ferr'); notice.setAttribute('role', 'alert');
        item.dashHost.prepend(notice);
      }
      notice.textContent = message;
    };
    // The first canonical Task read is the subtype authority even when the opening descriptor
    // omitted or carried a stale task_type. Normalize the entry's header metadata in place;
    // the dashboard mount already uses the same fresh task, so never remount just to relabel.
    const normalizeTaskType = fresh => {
      if (!alive() || !fresh) return;
      const nextType = String(fresh.task_type || 'task');
      const currentType = String(item.record.task_type || 'task');
      if (nextType === currentType && String(item.record.source?.task_type || 'task') === nextType) return;
      item.record = {...item.record, task_type: nextType,
        revision: fresh.revision ?? item.record.revision,
        state: fresh.status ?? item.record.state,
        label: fresh.title || item.record.label};
      titlePaint(item);
      paintTypeTab(item);
      options.onTitleChange?.(item.record);
    };
    // A successful canonical write/reload may change the Task subtype. Replace only this
    // dashboard in place, preserving the entry, source scope, Chat/draft, selected tab and
    // stack. The caller has already verified the canonical response and, for a dirty
    // entry, either refused adoption or explicitly forced a reload.
    const adoptTaskType = (fresh, {force = false} = {}) => {
      if (!alive() || !fresh) return false;
      const nextType = String(fresh.task_type || 'task');
      if (nextType === (item.record.task_type || 'task')) return false;
      if (nextType === 'email') return false; // historical email keeps its existing read-only route
      if (!force && rwDirty(item.dashboard)) {
        changed('This task changed type elsewhere. Save or discard your edits to reload.');
        return false;
      }
      const previousScroll = item.dashHost.scrollTop;
      const previousDashboard = item.dashboard;
      item.record = {...item.record, task_type: nextType,
        revision: fresh.revision ?? item.record.revision,
        state: fresh.status ?? item.record.state,
        label: fresh.title || item.record.label};
      item.dashboardLoaded = false;
      item.dashboardMounting = false;
      item.scroll = previousScroll;
      item.prefetchedTask = fresh;
      try { previousDashboard?.dispose?.(); } catch (_) { /* one replacement only */ }
      item.dashboard = null;
      item.dashHost.replaceChildren();
      titlePaint(item);
      paintTypeTab(item);
      options.onTitleChange?.(item.record);
      mountDashboard(item);
      return true;
    };
    (async () => {
      try {
        const dashboard = await rwDashboardMount(host, record, {
          alive, navigate, workspace, entry: item, options,
          markCrmTable(table) { if (alive()) item.crmTable={type:'table',id:table.id,business_id:table.business_id ?? null,label:table.label || table.title || item.record.label || 'CRM'}; },
          unavailable: () => rwRecordUnavailable(item), revalidateProject,
          adoptTaskType, normalizeTaskType, changed, moved, prefetchedTask,
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
        item.dashboardLoaded = true;
        if (item.scroll) host.scrollTop = item.scroll;
      } catch (failure) {
        if (!alive()) return;
        if (accessLost(failure) || sourceGone(failure)) {
          void rwRecordUnavailable(item);
          return;
        }
        if (failure?.code === 'record_moved' && taskMoveDestination(failure)) {
          host.replaceChildren();
          showMoved(item, failure);
          item.dashboardLoaded = false;
          return;
        }
        host.replaceChildren();
        const message = rwNode('p', 'ferr', uiFailure(failure, {load: true}));
        message.setAttribute('role', 'alert');
        const retry = rwNode('button', 'quiet', 'Retry'); retry.type = 'button';
        retry.onclick = () => { if (!item.alive) return; host.replaceChildren(); item.dashboardLoaded = false; mountDashboard(item); };
        host.append(message, retry);
        item.dashboardLoaded = false;
      } finally {
        if (attempt === item.dashboardGeneration) item.dashboardMounting = false;
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
      // The related-work header may name the record already open here; it is a presentation field, not a read.
      sourceTitle: record.label,
      // Focus is a Global projection; the canonical source names the owning Project.
      privateGlobal: true, privateSource,
      isVisible: () => item.alive && item.tab === 'chat' && !item.chatHost.hidden && !document.hidden
        && options.visible?.() !== false && item.recordEl.isConnected && !item.recordEl.hidden,
      openReference: ref => { void rwOpenChatReference(item, ref); },
      onRead: () => options.onRead?.(source),
      onUnread: () => options.onUnread?.(source),
      onSent: () => options.onSent?.(source),
      onUnavailable: () => {
        if (!item.alive) return;
        void rwRecordUnavailable(item);
      },
      onMoved: error => showMoved(item, error),
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
    // Authorization is entry-wide, not pane-wide. A denial reached through a hidden Chat or a
    // retained dashboard invalidates the whole retained entry: no stale text, no Back replay.
    if (!item?.alive) return;
    const index = stack.indexOf(item);
    if (index < 0) return;
    const wasTop = index === stack.length - 1;
    if (wasTop && options.paired) {
      const prior=stack[index-1] || null;
      await pairedSelect(prior?.record || null,prior?.pairedParent || null);
    }
    if (item.record) {
      item.record.project_item_prefetched = null;
      item.record.url = null;
      item.record.opening_target = null;
      item.record.summary = null;
    }
    stack.splice(index, 1);
    removeEntry(item);
    // Drop every cached source view so a stale detached body cannot be read or resurrected.
    try { item.dashHost?.replaceChildren(); } catch (_) {}
    try { item.conversationHost?.replaceChildren(); } catch (_) {}
    item.activityHost?.replaceChildren();
    item.record = null;
    if (!stack.length) { dispose(); options.onClose?.(); return; }
    if (wasTop) reveal();
    else paintBack();
  }
  async function rwOpenChatReference(item, ref) {
    if (!ref) return;
    if (ref.resource_type === 'email_conversations' || ref.type === 'email') {
      const target = rwRecordFromItem({source: {type: 'email', id: ref.id, business_id: ref.business_id ?? null, business_slug: ref.business_slug, label: ref.label}}, {can_thread: false});
      if (target) void navigate(target);
      return;
    }
    // A typed chat reference may name a Focus Reference. Its Chat availability comes from
    // the authorized current Focus DTO (the conversation binding), not from the type name.
    let dto = null, canThread = ref.can_thread;
    if (ref.type === 'reference') {
      try {
        const data = await coreData(await post('/root/operations/focus_get', {scope: 'global', item_id: ref.id}));
        if (!item.alive) return;
        dto = data.item || null;
        if (dto) canThread = !!(dto.conversation && (dto.conversation.type || dto.conversation.id));
      } catch (failure) {
        if (!item.alive) return;
        if (accessLost(failure) || sourceGone(failure)) return;
      }
    }
    const source = ref.type === 'reference' && dto
      ? {type: 'reference', id: ref.id, business_id: dto.conversation?.business_id ?? dto.source?.business_id ?? ref.business_id ?? null}
      : {type: ref.type, id: ref.id, business_id: ref.business_id ?? null, business_slug: ref.business_slug, business_name: ref.business_name, label: ref.label, state: ref.state, resource_type: ref.resource_type};
    const target = dto && ref.type === 'reference'
      ? rwRecordFromItem({...dto, source: dto.resolved_source || dto.source || source, conversation: dto.conversation}, {can_thread: canThread})
      : rwRecordFromItem({source}, {can_thread: ref.type === 'dev_ticket' ? false : canThread ?? (ref.type === 'task' || ref.type === 'signoff' || ref.type === 'project')});
    if (target) void navigate(target);
  }
}
// ---------------------------------------------------------------- dashboards
async function rwDashboardMount(host, record, ctx) {
  host.replaceChildren();
  host.classList.add('rw-dashboard-panel');
  // A standalone Dev report is read through the owner root selector; its canonical business_id
  // is checked there. It does not need a business slug, unlike a Project-scoped record.
  if (record.scopeMissing && !(record.source.type === 'dev_ticket' && !record.project_id))
    throw new Error('This record’s Project scope is unavailable.');
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
  if (source.type === 'dev_ticket' && !record.project_id) return rwDevTicketDashboard(host, record, ctx);
  if (source.type === 'action' && !record.project_id) return rwActionDashboard(host, record, ctx, call);
  if ((source.type === 'table' || record.resource_type === 'data_tables') && !record.project_id) return rwTableDashboard(host, record, ctx, call);
  if (source.type === 'job') return rwJobDashboard(host, record, ctx);
  if (source.type === 'task') return rwWithActivity(host, record, ctx, await rwTaskDashboard(host, record, ctx, call));
  if (source.type === 'email' || record.resource_type === 'email_conversations') {
    host.classList.add('record-panel');
    historicalEmailMount(host);
    return {dirty: () => false, dispose() {}};
  }
  return rwReferenceDashboard(host, record, ctx, call);
}

// A Task's chronological activity (discussion, linked work, CRM membership developments and agent-recorded email or
// messages) sits below its editor. It loads only when opened; the editor, its drafts and the private Chat tab are untouched.
function rwWithActivity(host, record, ctx, controller) {
  if (!controller || typeof crmActivityMount !== 'function') return controller;
  const section = rwNode('details', 'rw-activity-section');
  section.append(rwNode('summary', '', 'Related history'));
  const body = rwNode('div', 'rw-activity-body');
  section.append(body);
  let activity = null;
  section.addEventListener('toggle', () => {
    if (section.open && !activity) activity = crmActivityMount(body, {biz: record.biz || null, task: {id: record.source.id},
      navigate: ref => { const target = rwRecordFromItem({source: ref}); return target ? ctx.navigate(target) : null; }});
  });
  host.append(section);
  // An editor that repaints its host must not drop the activity section.
  const observer = new MutationObserver(() => { if (!section.isConnected && host.isConnected) host.append(section); });
  observer.observe(host, {childList: true});
  return new Proxy(controller, {
    get(target, key) {
      if (key === 'dispose') return () => { observer.disconnect(); activity?.dispose(); target.dispose?.(); };
      if (key === 'hasUnsaved') return () => rwDirty(target) || !!activity?.dirty();
      const value = Reflect.get(target, key);
      return typeof value === 'function' ? value.bind(target) : value;
    },
  });
}
// ---------------------------------------------------------------- Task
async function rwTaskDashboard(host, record, ctx, call) {
  host.classList.add('record-panel');
  const data = ctx.prefetchedTask ? {task: ctx.prefetchedTask} : await call('tasks_get', {task_id: record.source.id});
  if (!ctx.alive()) return null;
  const task = data.task;
  if (!task) throw new Error('Task is no longer available.');
  // A related item may arrive with only its identifier; the authoritative Task title fills the header.
  if (task.title && (!record.label || record.label === 'Record' || record.label === record.source.id)) ctx.title?.({label: task.title});
  const type = task.task_type || 'task';
  // The incoming descriptor is presentation only; this authorized read supplies the subtype
  // used by both the body and the type tab before the dashboard finishes mounting.
  ctx.normalizeTaskType?.(task);
  // A retained Task dashboard rechecks its authoritative source on every reveal. A denial
  // invalidates the whole entry; a transient failure keeps the current content and edits.
  const revalidateTask = async () => {
    if (!(await ctx.revalidateProject?.() ?? true)) return false;
    try {
      const fresh = await call('tasks_get', {task_id: record.source.id});
      if (!ctx.alive()) return false;
      if (!fresh.task) throw Object.assign(new Error('Task is no longer available.'), {code: 'not_found'});
      const freshType = fresh.task.task_type || 'task';
      if (freshType !== type) {
        // An external subtype change is adopted only for a clean entry. A dirty editor keeps
        // its original type/base/revision and gets a visible changed-record notice; the owner
        // can save, discard, or explicitly Reload.
        if (rwDirty(ctx.entry?.dashboard)) {
          ctx.changed?.('This task changed type elsewhere. Save or discard your edits to reload.');
          return false;
        }
        if (await ctx.adoptTaskType?.(fresh.task)) return false;
        ctx.changed?.('This task changed type elsewhere. Reload to read the latest version.');
        return false;
      }
      return true;
    } catch (failure) {
      if (!ctx.alive()) return false;
      if (accessLost(failure) || sourceGone(failure)) { await ctx.unavailable?.(); return false; }
      if (failure?.code === 'record_moved') { ctx.moved?.(failure); return false; }
      return true;
    }
  };
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
    const onShow = controller?.onShow;
    return {...controller,
      async onShow() { if (!await revalidateTask()) return; if (onShow) await onShow.call(controller); },
    };
  }
  const navigate = target => ctx.navigate(rwRecordFromItem({source: {
    type: target.type, id: target.id, business_id: target.business_id ?? null, business_slug: target.business_slug ?? null,
    business_name: target.business_name, label: target.label, resource_type: target.resource_type, state: target.state,
    task_type: target.task_type}}, {biz: null}));
  const relatedMount = (relatedHost, fresh) => rwTaskRelatedMount(relatedHost, record, fresh, ctx);
  if (record.source.business_id == null && typeof globalTasksMount === 'function') {
    const token = globalTasksMount(host, {initialTaskId: task.id, onBack: () => {}, embedded: true, onNavigate: navigate,
      renderRelated: relatedMount,
      onCanonical: async (fresh, {reload = false, changes = null} = {}) => {
        const nextType = fresh.task_type || 'task';
        if (nextType === (record.task_type || type)) return false;
        const requested = changes?.task_type;
        if (!reload && requested !== nextType && token?.dirty?.()) {
          token.changed?.('This task changed type elsewhere. Save or discard your edits to reload.');
          return true;
        }
        if (await ctx.adoptTaskType?.(fresh, {force: reload || requested === nextType})) return true;
        token.changed?.('This task changed type elsewhere. Reload to read the latest version.');
        return true;
      }});
    return {dirty: () => !!token?.dirty?.(), hasUnsaved: () => !!token?.dirty?.(), onShow: revalidateTask,
      beforeLeave: () => token?.beforeLeave?.() ?? true,
      changed(message) { token?.changed?.(message); },
      dispose() { token?.dispose?.(); host.replaceChildren(); }};
  }
  // tDetail repaints itself after a successful write. Keep one stable adapter and point it at
  // the newest controller, so Close/dirty checks never consult a torn-down type editor.
  let live = null;
  const adapter = {
    get el() { return live?.el || null; },
    dirty: () => !!live?.dirty?.(),
    hasUnsaved: () => !!live?.dirty?.(),
    close: () => live?.close?.(),
    onShow: revalidateTask,
    changed(message) { if (live?.changed) live.changed(message); else ctx.changed?.(message); },
    dispose() { live?.close?.(); host.replaceChildren(); },
  };
  const onCanonical = async (fresh, {reload = false, body = null} = {}) => {
    const nextType = fresh.task_type || 'task';
    if (nextType === (record.task_type || type)) return false;
    const requested = body?.set?.task_type || body?.changes?.task_type;
    // The owner's own successful type save is already canonical; adopt it even though the
    // old type editor still reports the just-saved change as dirty. Any other external type
    // change must not erase unresolved local fields.
    if (!reload && requested !== nextType && rwDirty(adapter)) {
      adapter.changed('This task changed type elsewhere. Save or discard your edits to reload.');
      return true;
    }
    if (await ctx.adoptTaskType?.(fresh, {force: reload || requested === nextType})) return true;
    adapter.changed('This task changed type elsewhere. Reload to read the latest version.');
    return true;
  };
  const mounted = tDetail(task, {host, onClose: () => {}, embedded: true, navigate, relatedMount,
    onMount: next => { live = next; }, onCanonical});
  live = mounted;
  return adapter;
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
    dropOnDenied: true,
    onUnavailable: () => { void ctx.unavailable?.(); },
  });
  queueMicrotask(() => { if (ctx.alive() && host.isConnected) view.paint(); });
  return {dirty: () => view.hasUnsaved(), hasUnsaved: () => view.hasUnsaved(), save: () => view.save(),
    onShow: async () => { if (!(await ctx.revalidateProject?.() ?? true)) return; await view.refresh(); },
    dispose() { view.dispose(); host.replaceChildren(); }};
}

// ---------------------------------------------------------------- Action
// One Action reader for Activity and the record pane. It reuses the existing row content
// (text, category, occurred time, attribution, details, refs) and the existing revision
// and discussion controller; this module does not define a second Action rendering.
function rwActionContent(host, action, call = null) {
  if (typeof acActionReader !== 'function') throw new Error('The Action reader is unavailable.');
  const op = call ? (name, body) => call(name, body) : null;
  return acActionReader(host, action, op ? {op} : {});
}
async function rwActionDashboard(host, record, ctx, call) {
  host.classList.add('record-panel');
  const data = await call('actions_get', {action_id: record.source.id});
  if (!ctx.alive()) return null;
  const action = data.action;
  if (!action) throw new Error('Action is no longer available.');
  let reader = rwActionContent(host, action, call);
  return {dirty: () => false, onShow: async () => {
    try {
      const next = await call('actions_get', {action_id: record.source.id});
      if (!ctx.alive()) return;
      if (!next.action) throw Object.assign(new Error('Action is no longer available.'), {code: 'not_found'});
      reader?.dispose?.();
      reader = rwActionContent(host, next.action, call);
    } catch (failure) {
      if (!ctx.alive()) return;
      if (accessLost(failure) || sourceGone(failure)) { await ctx.unavailable?.(); return; }
      // A transient failure keeps the last authorized read.
    }
  }, dispose() { reader?.dispose?.(); host.replaceChildren(); }};
}

// ---------------------------------------------------------------- reminder / routine / job readers
function rwFormatTime(value) {
  if (!value) return '—';
  if (typeof rtTime === 'function') return rtTime(value);
  try { return new Intl.DateTimeFormat('en-US', {weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false}).format(new Date(value)); }
  catch (_) { return String(value); }
}
// The shared reminder detail (js/reminders.js): the same view and controls as the owner strip. A legacy result without an
// id/revision stays read-only there; without that module this falls back to the plain text/time/state view.
function rwReminderReader(host, record, item, reminder) {
  const box = rwNode('section', 'rw-reader rw-reminder');
  box.append(rwNode('h3', '', 'Reminder'));
  host.append(box);
  if (typeof remindersDetailMount === 'function') {
    const view = remindersDetailMount(box, {text: item.label || record.label, ...reminder}, {biz: record.biz || null});
    return {dirty: () => false, hasUnsaved: () => false, onShow: () => view.refresh(), dispose() { view.dispose(); host.replaceChildren(); }};
  }
  box.append(rwNode('p', 'rw-reminder-text', reminder.text || item.label || record.label || 'Reminder'));
  const fields = [['Scheduled for', rwFormatTime(reminder.at)], ['State', reminder.state || '—']];
  if (reminder.revision != null) fields.push(['Revision', String(reminder.revision)]);
  box.append(rwNode('p', 'muted rw-reminder-meta', fields.map(([label, value]) => `${label}: ${value}`).join(' · ')));
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

// ---------------------------------------------------------------- Dev report reader
// The owner Project inventory and a standalone owner Dev reference both carry the same ticket
// record. This is the one small read-only renderer authorized for it: only the fields below,
// never arbitrary JSON or an item summary. The retired System Dev queue and its editors stay out.
const RW_TICKET_KINDS = {bug: 'Bug', feature: 'Feature', tooling: 'Tooling', workflow: 'Workflow'};
function rwTicketText(text) {
  return typeof chatViewMarkdown === 'function' ? chatViewMarkdown(String(text ?? '')) : esc(String(text ?? ''));
}
function rwTicketField(box, label, value) {
  if (value == null || value === '') return;
  const row = rwNode('div', 'rw-ticket-field');
  row.append(rwNode('dt', '', label), rwNode('dd', '', String(value)));
  box.append(row);
}
function rwDevTicketContent(host, ticket, extra = {}) {
  if (!ticket) return null;
  const box = rwNode('section', 'rw-reader rw-ticket');
  const title = ticket.title || 'System Dev report';
  box.append(rwNode('h3', '', title));
  const meta = rwNode('div', 'rw-ticket-meta');
  const kind = RW_TICKET_KINDS[ticket.kind] || ticket.kind;
  if (kind) meta.append(rwNode('span', 'rw-ticket-badge', String(kind)));
  if (ticket.status) meta.append(rwNode('span', 'rw-ticket-badge rw-ticket-status-' + (ticket.status === 'closed' ? 'closed' : 'open'),
    ticket.status === 'closed' ? 'Closed' : 'Open'));
  box.append(meta);
  if (ticket.details && String(ticket.details).trim()) {
    const body = rwNode('div', 'rw-ticket-body');
    body.innerHTML = rwTicketText(ticket.details);
    box.append(body);
  } else {
    box.append(rwNode('p', 'muted', 'No report details were supplied.'));
  }
  const times = rwNode('dl', 'rw-ticket-fields');
  rwTicketField(times, 'Created', ticket.created_at ? rwFormatTime(ticket.created_at) : '');
  rwTicketField(times, 'Updated', ticket.updated_at ? rwFormatTime(ticket.updated_at) : '');
  if (times.children.length) box.append(times);
  if (ticket.resolution && String(ticket.resolution).trim()) {
    const section = rwNode('section', 'rw-ticket-section');
    section.append(rwNode('h4', '', 'Resolution'));
    const body = rwNode('div', 'rw-ticket-body'); body.innerHTML = rwTicketText(ticket.resolution); section.append(body);
    box.append(section);
  }
  if (ticket.verification && String(ticket.verification).trim()) {
    const section = rwNode('section', 'rw-ticket-section');
    section.append(rwNode('h4', '', 'Verification'));
    const body = rwNode('div', 'rw-ticket-body'); body.innerHTML = rwTicketText(ticket.verification); section.append(body);
    box.append(section);
  }
  if (ticket.deployment_ref && String(ticket.deployment_ref).trim()) {
    const section = rwNode('section', 'rw-ticket-section');
    section.append(rwNode('h4', '', 'Deployment reference'));
    section.append(rwNode('p', 'rw-ticket-deployment', String(ticket.deployment_ref)));
    box.append(section);
  }
  // The owner list carries the ticket record only. If an authorized envelope also supplies
  // events (never fetched here), render that whitelist as supported history; never invent it.
  const events = Array.isArray(extra.events) ? extra.events : Array.isArray(ticket.events) ? ticket.events : [];
  if (events.length) {
    const section = rwNode('section', 'rw-ticket-section rw-ticket-events');
    section.append(rwNode('h4', '', 'History'));
    for (const event of events) {
      if (!event || typeof event !== 'object') continue;
      const row = rwNode('article', 'rw-ticket-event');
      const head = rwNode('div', 'rw-ticket-event-head');
      const kindLabel = ({follow_up: 'Follow-up', resolved: 'Resolved', status_changed: 'Status changed'}[event.event_kind] || 'Update');
      head.append(rwNode('span', 'rw-ticket-badge', kindLabel));
      if (event.created_at) head.append(rwNode('time', 'muted', rwFormatTime(event.created_at)));
      row.append(head);
      for (const [label, key] of [['Details', 'details'], ['Resolution', 'resolution'], ['Verification', 'verification'], ['Deployment reference', 'deployment_ref']]) {
        if (event[key] && String(event[key]).trim()) {
          const body = rwNode('div', 'rw-ticket-body'); body.innerHTML = rwTicketText(event[key]); row.append(body);
        }
      }
      section.append(row);
    }
    box.append(section);
  }
  host.append(box);
  return {dirty: () => false, hasUnsaved: () => false, dispose() { box.remove(); }};
}
async function rwDevTicketDashboard(host, record, ctx) {
  host.classList.add('record-panel');
  let view = null, generation = 0;
  const read = async () => {
    const own = ++generation;
    // The existing owner-only root read, narrowed to this exact record. This is the only
    // standalone Dev reference reader; agents' dev_tickets_get/history contract is not used.
    const data = coreData(await post('/root/operations/dev_tickets_list', {ticket_id: record.source.id, status: 'all', details: true}));
    if (!ctx.alive() || own !== generation) return false;
    const ticket = (data.items || []).find(row => row?.id === record.source.id);
    if (!ticket) { await ctx.unavailable?.(); return false; }
    // A matching id in another scope is not the reference. Explicit null is Global.
    if ((ticket.business_id ?? null) !== (record.source.business_id ?? null)) { await ctx.unavailable?.(); return false; }
    view?.dispose?.();
    view = rwDevTicketContent(host, ticket, {events: data.events, eventsNextOffset: data.events_next_offset ?? data.next_offset});
    return true;
  };
  await read();
  return {
    dirty: () => false, hasUnsaved: () => false,
    async onShow() {
      try {
        if (!(await read()) || !ctx.alive()) return;
      } catch (failure) {
        if (!ctx.alive()) return;
        if (accessLost(failure) || sourceGone(failure)) { await ctx.unavailable?.(); return; }
        // A transient revalidation keeps the last authorized report visible.
      }
    },
    dispose() { generation++; view?.dispose?.(); host.replaceChildren(); },
  };
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
    // true = the child/revealed reader may continue; false = this entry was invalidated or
    // its host is gone. A transient failure keeps the last authorized reader, so its nested
    // onShow still runs; an authorization denial removes the whole entry.
    revalidate = async () => {
      try {
        const check = await coreData(await post('/root/operations/project_item_get', {scope: 'global', project_id: record.project_id,
          item: {type: record.source.type, id: record.source.id, business_id: record.source.business_id ?? null}}));
        if (!ctx.alive()) return false;
        if (!check.item) throw Object.assign(new Error('Unavailable'), {code: 'not_found'});
        return true;
      } catch (failure) {
        if (!ctx.alive()) return false;
        if (accessLost(failure) || sourceGone(failure)) { await ctx.unavailable?.(); return false; }
        return true;
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
      id: record.source.id, label: record.label, resource_type: record.resource_type}, {host, alive: () => ctx.alive(), onUnavailable: () => { void ctx.unavailable?.(); }});
    return reader || {dirty: () => false, dispose() { host.replaceChildren(); }};
  }
  // Project inventory envelopes: these are the per-family records, never a flat preview blob.
  if (content.action) {
    const actionReader = rwActionContent(host, content.action, call);
    return {dirty: () => false, onShow: revalidate, dispose() { actionReader?.dispose?.(); host.replaceChildren(); }};
  }
  if (content.document || content.note) {
    const doc = content.document || content.note;
    // recordOpenDocument consumes the exact typed source, not the workspace descriptor,
    // and can start from the authorized top-level envelope already returned by
    // project_item_get. Its own complete/next_cursor, id, offset/hash and scope checks are
    // preserved; only the paged projection wrapper and heading live here.
    const sourceRef = {id: record.source.id, type: record.source.type, label: record.label,
      resource_type: record.resource_type};
    const wrapper = rwNode('section', 'rw-reader rw-document');
    wrapper.append(rwNode('h3', '', doc.title || item.label || record.label || 'Document'));
    const mount = rwNode('div', 'rw-document-mount');
    wrapper.append(mount);
    host.append(wrapper);
    const initial = content.document ? {document: doc, complete: content.complete, next_cursor: content.next_cursor}
      : {note: doc, complete: true};
    if (content.document && doc.next_cursor && content.next_cursor == null) initial.next_cursor = doc.next_cursor;
    if (content.document && doc.complete != null && content.complete == null) initial.complete = doc.complete;
    const reader = await recordOpenDocument({...ctx, biz: record.biz || null, current: () => ctx.alive()}, sourceRef,
      {label: record.label, ...doc}, {host: mount, alive: () => ctx.alive(), initial,
        onUnavailable: () => { void ctx.unavailable?.(); }});
    return {dirty: () => false, hasUnsaved: () => false,
      async onShow() { if (revalidate && !await revalidate()) return; await reader?.onShow?.(); },
      dispose() { reader?.dispose?.(); wrapper.remove(); host.replaceChildren(); }};
  }
  if (content.table) {
    const box = rwNode('section', 'rw-reader rw-table-reader');
    box.append(rwNode('h3', '', content.table.title || item.label || record.label || 'Table'));
    host.append(box);
    const controller = await rwTableBody(box, record, ctx, call);
    return {...rwForwardEdits(controller), onShow: async () => { if (!await revalidate()) return; await controller?.onShow?.(); },
      dispose() { controller?.dispose?.(); host.replaceChildren(); }};
  }
  if (content.ticket) {
    const ticket = rwDevTicketContent(host, content.ticket, {
      events: content.events, eventsNextOffset: content.events_next_offset ?? content.next_offset,
    });
    return {dirty: () => false, onShow: revalidate,
      dispose() { ticket?.dispose?.(); host.replaceChildren(); }};
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
    return {...rwForwardEdits(controller), onShow: async () => { if (await revalidate?.()) await controller?.onShow?.(); },
      dispose() { controller?.dispose?.(); host.replaceChildren(); }};
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

// A wrapper around a Table body must report and settle the body's own unsaved CRM edits, never hardcode "clean".
function rwForwardEdits(controller) {
  return {dirty: () => !!controller && rwDirty(controller), hasUnsaved: () => !!controller && rwDirty(controller),
    ...(typeof controller?.save === 'function' ? {save: () => controller.save()} : {}),
    ...(typeof controller?.discard === 'function' ? {discard: () => controller.discard()} : {})};
}

// The existing Tables workspace writes to the global drawer. A record dashboard keeps the
// canonical table read in this pane instead: same operations and columns, no exit button.
async function rwTableBody(host, record, ctx, call) {
  const id = record.source.id;
  const tableData = await call('tables_get', {table_id: id, ...(record.biz ? {} : {scope: 'global'})});
  if (!ctx.alive()) return null;
  const table = tableData.table;
  if (!table) throw new Error('This table is no longer available.');
  if(table.title)ctx.title?.({label:table.title});
  const title = rwNode('h4', '', table.title || record.label || 'Table');
  const description = table.description ? rwNode('p', 'muted', table.description) : null;
  const biz = record.biz || null;
  const tableBusinessId=Object.hasOwn(table,'business_id') ? table.business_id
    : (biz ? (state.bizs || []).find(project=>project.slug===biz)?.id : null);
  const canonicalScope=ref=>{
    const named=ref?.business_slug ? (state.bizs || []).find(project=>project.slug===ref.business_slug) : null;
    const hasId=Object.hasOwn(ref || {},'business_id');
    const businessId=hasId ? ref.business_id : named?.id ?? (ref?.business_slug ? ref.business_slug : tableBusinessId);
    const project=businessId == null ? null : (state.bizs || []).find(candidate=>candidate.id===businessId
      || candidate.business_id===businessId || candidate.slug===businessId);
    const mismatch=hasId && businessId != null && named && project && named.id!==project.id;
    return {businessId:businessId ?? null,businessSlug:businessId == null ? null : (mismatch ? null : (project?.slug || (businessId===tableBusinessId ? biz : null))),invalid:!!mismatch};
  };
  const parentCrm={type:'table',id:table.id,business_id:tableBusinessId,label:table.title || record.label || 'CRM'};
  // The Table team thread is the shared discussion; the private Chat tab stays its own conversation.
  const teamHost = rwNode('section', 'rw-table-team');
  const teamNavigate = ref => {
    const scope=canonicalScope(ref);
    if(scope.invalid)return null;
    const target=rwRecordFromItem({source:{...ref,business_id:scope.businessId,business_slug:scope.businessSlug}});
    return target ? ctx.navigate(target,{parentTable:crmIsCrm(table)?parentCrm:null}) : null;
  };
  const crmReady = typeof crmThreadMount === 'function' && typeof crmTableMount === 'function';
  if (crmReady && crmIsCrm(table)) {
    ctx.markCrmTable?.({...parentCrm});
    const crmHost = rwNode('div', 'crm-host');
    host.append(crmHost);
    const mount = crmTableMount(crmHost, {biz, table, draftKey: JSON.stringify(['rw', biz, table.id]), drafts: rwCrmDrafts,
      onOpenRecord: entry => {
        const ref=entry?.record_ref;
        if(!ref?.id || entry.unavailable)return;
        const scope=canonicalScope(ref);
        if(scope.invalid)return;
        const target=rwRecordFromItem({source:{type:'task',id:ref.id,business_id:scope.businessId,business_slug:scope.businessSlug,
          label:entry.record?.title,task_type:entry.record?.task_type,state:entry.record?.status}}, {can_thread:true});
        if(target)void ctx.navigate(target,{parentTable:crmIsCrm(table)?parentCrm:null});
      },
      isActive: () => ctx.alive() && host.isConnected, onUnavailable: () => { void ctx.unavailable?.(); }});
    return {dirty: () => mount.dirty(), hasUnsaved: () => mount.dirty(), save: () => mount.save(), discard: () => mount.discard(), onShow: () => mount.refresh(),
      dispose() { mount.dispose(); host.replaceChildren(); }};
  }
  const status = rwNode('p', 'muted', 'Loading rows…');
  const wrap = rwNode('div', 'rw-table-wrap');
  const grid = rwNode('table', 'rw-table');
  const more = rwNode('button', 'quiet', 'More rows'); more.type = 'button'; more.hidden = true;
  const retry = rwNode('button', 'quiet', 'Retry'); retry.type = 'button'; retry.hidden = true;
  // The Project reader already prints the table title as its h3; do not repeat it.
  const alreadyTitled = [...host.children].some(child => child.tagName === 'H3');
  host.append(...(alreadyTitled ? [] : [title]), ...(description ? [description] : []), status, wrap, more, retry, teamHost);
  const team = crmReady ? crmThreadMount(teamHost, {biz, table, navigate: teamNavigate}) : {dispose() {}};
  wrap.append(grid);
  let cursor = null, rows = [], generation = 0, disposed = false, loading = false;
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
  let retryIsAppend = false;
  const loadRows = async (append = false) => {
    if (loading || disposed || !ctx.alive()) return;
    loading = true;
    const own = ++generation;
    retryIsAppend = append;
    retry.hidden = true;
    more.disabled = true;
    if (!append) status.textContent = 'Loading rows…';
    const previousRows = rows.slice(), previousCursor = cursor;
    try {
      const data = await call('tables_rows_list', {table_id: id, limit: 50,
        ...(append && cursor ? {cursor} : {}), ...(record.biz ? {} : {scope: 'global'})});
      if (disposed || own !== generation || !ctx.alive()) return;
      if (data.table_revision != null && table.revision != null && data.table_revision !== table.revision)
        throw new Error('This table changed while loading. Open it again to read the current version.');
      rows = append ? [...previousRows, ...(data.items || [])] : (data.items || []);
      cursor = data.next_cursor || null;
      paint();
      more.hidden = !cursor;
    } catch (failure) {
      if (disposed || own !== generation || !ctx.alive()) return;
      if (accessLost(failure) || sourceGone(failure)) { await ctx.unavailable?.(); return; }
      // An append failure retains already-authorized rows and the same cursor. A failed
      // first/refresh read likewise keeps any prior authorized rows and exposes Retry.
      rows = previousRows; cursor = previousCursor;
      status.textContent = uiFailure(failure, {load: true});
      more.hidden = true;
      retry.hidden = false;
    } finally {
      if (disposed || own !== generation) return;
      loading = false;
      more.disabled = false;
    }
  };
  more.onclick = () => { if (!loading) void loadRows(true); };
  retry.onclick = () => { if (!loading) void loadRows(retryIsAppend); };
  await loadRows(false);
  const controller = {dirty: () => false, hasUnsaved: () => false,
    async onShow() {
      try {
        await call('tables_get', {table_id: id, ...(record.biz ? {} : {scope: 'global'})});
        if (!ctx.alive() || disposed) return;
        await loadRows(false);
      } catch (failure) {
        if (!disposed && ctx.alive() && (accessLost(failure) || sourceGone(failure))) await ctx.unavailable?.();
      }
    }, dispose() { disposed = true; generation++; team.dispose(); host.replaceChildren(); }};
  return controller;
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
  const stateFor = section => sectionState[section] ||= {items: [], cursor: null, loaded: false, loading: false, pending: false, request: 0, key: ''};
  // A forbidden inventory read revokes this Project's whole inventory authorization, not
  // just the section that happened to be active. Clear every cached section and invalidate
  // every in-flight section generation/proxy cursor so another switch cannot flash a stale
  // page while its own refresh is pending.
  function invalidateInventorySections() {
    generation++;
    for (const state of Object.values(sectionState)) {
      state.items = []; state.cursor = null; state.loaded = false; state.loading = false;
      state.pending = false; state.request = 0; state.key = '';
    }
  }
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
    const previous = {items: state.items.slice(), cursor: state.cursor};
    if (!append && (state.key !== key || fresh)) {
      // Revalidating a visible, already-authorized page keeps its rows until the fresh read
      // succeeds or explicitly revokes them; it never flashes an empty cache.
      const preserve = fresh && state.loaded && state.key === key;
      if (!preserve) state.items = [];
      state.cursor = null; state.loaded = false; state.key = key;
    }
    state.loading = true;
    const own = ++generation;
    state.request = own;
    if (section === activeSection) { more.disabled = true; errorBox.hidden = true; errorBox.replaceChildren(); paint(section); }
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
      const denied = accessLost(failure) || sourceGone(failure);
      if (denied) {
        invalidateInventorySections();
        const current = stateFor(section);
        current.loaded = true;
        current.key = key;
      } else if (!append) {
        // Transient failures keep previously authorized rows (and their cursor) available
        // with a Retry; they are not permission to expose denied content.
        state.items = previous.items;
        state.cursor = previous.cursor;
      }
      state.loaded = true;
      if (section === activeSection) {
        more.disabled = false;
        errorBox.replaceChildren(rwNode('span', '', denied ? 'This project inventory is unavailable.' : uiFailure(failure, {load: true}) + ' '));
        const retry = rwNode('button', 'quiet', 'Retry'); retry.type = 'button';
        retry.onclick = () => {
          if (disposed) return;
          if (denied) {
            const reset = stateFor(section);
            reset.loaded = false; reset.key = ''; reset.items = []; reset.cursor = null;
            void load(section, false, true);
          } else if (append) {
            // Retry the same continuation; the previously authorized rows and cursor are kept.
            void load(section, true);
          } else {
            state.loaded = false; state.key = ''; state.items = []; state.cursor = null;
            void load(section, false, true);
          }
        };
        errorBox.append(retry);
        errorBox.hidden = false;
        paint(section);
        more.hidden = true;
      }
    } finally {
      if (disposed || state.request !== own) return;
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
    // A reveal starts a fresh authorization read; keep the paging control busy until i
    // completes so a row click cannot race the retained page while it is being revalidated.
    more.disabled = true;
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
      if (accessLost(failure) || sourceGone(failure)) { await ctx.unavailable?.(); return; }
      stateFor(activeSection).items = []; stateFor(activeSection).cursor = null;
      list.replaceChildren();
      errorBox.replaceChildren(rwNode('span', '', uiFailure(failure, {load: true})));
      errorBox.hidden = false;
      more.disabled = false;
      paint(activeSection);
    }
  }
  // A confirmed move changes what this Project owns; re-read the visible section (a read, never an optimistic edit).
  const onTaskMoved = () => { if (!disposed) void load(activeSection, false, true); };
  document.addEventListener('qorqut-task-moved', onTaskMoved);
  return {
    dirty: () => false, hasUnsaved: () => false, onShow: revalidate,
    dispose() { disposed = true; generation++; document.removeEventListener('qorqut-task-moved', onTaskMoved); host.replaceChildren(); },
  };
}

function rwProjectLinkDialog(project, onLinked) {
  let submission = null, pending = false;
  const projects = (state.bizs || []).filter(biz => biz.slug);
  const scopeOptions = `<option value="">Global</option>${projects.map(biz => `<option value="${esc(biz.business_id || biz.id)}" ${(biz.business_id || biz.id) === project.id ? 'selected' : ''}>${esc(biz.name || biz.slug)}${biz.archived ? ' (archived)' : ''}</option>`).join('')}`;
  modal({title: 'Link an existing item', width: 640, stack: true,
    body: `<div class="rw-link-dialog"><label>Scope<select class="inp" data-link-scope>${scopeOptions}</select></label>
      <label>Kind<select class="inp" data-link-type><option value="">Any kind</option><option value="task">Work</option><option value="signoff">Signoff</option><option value="action">Action</option><option value="material">Material</option><option value="person">Person</option><option value="topic">Topic</option><option value="table">Table</option><option value="reminder">Reminder</option><option value="routine">Routine</option><option value="project">Project</option></select></label>
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
// A Focus item reports unread in the same shape the row already uses.
function rwRecordFromFocusCard(item) {
  const record = rwRecordFromItem(item, {can_thread: !item.unavailable && RW_THREAD_TYPES.has((item.resolved_source || item.source || item.conversation || {}).type)});
  // Focus may carry a presentation-only name. A Project never uses it; other records keep their
  // familiar card name while the dashboard shows the record's real content.
  if (record && record.source.type !== 'project' && item.display_name?.trim()) record.label = item.display_name.trim();
  return record;
}
