// Agent tasks use the drawer's shared task UI.
// Task clicks open the existing drawer detail; business writes stay in tasks.js.
const AGPAD_LOG_N = 200, AGPAD_POLL_MS = 12000;
const AGENT_PANE_RATIO_KEY = "agent-pane-ratio";
const AGENT_PANE_DEFAULT_RATIO = 1 / 2.15;
const AGENT_PANE_MIN_TERMINAL = 260, AGENT_PANE_MIN_OVERVIEW = 240;
let _agpad = null;

function agentPaneDividerMount(root) {
  const divider = root?.querySelector?.("[data-agent-pane-divider]");
  const chat = root?.querySelector?.(".chat.termmode");
  const terminal = root?.querySelector?.(".global-agent-session") || chat;
  const pane = root?.querySelector?.(".agpad");
  if (!root || !divider || !chat || !terminal || !pane || !root.style) return () => {};

  let ratio = Number(ls(AGENT_PANE_RATIO_KEY));
  if (!Number.isFinite(ratio) || ratio <= 0 || ratio >= 1) ratio = AGENT_PANE_DEFAULT_RATIO;
  let preferredRatio = ratio, shield = null, dragging = false;
  const resize = typeof ResizeObserver === "function" ? new ResizeObserver(() => {
    if (!dragging) apply(preferredRatio);
  }) : null;
  const span = () => {
    const c = terminal.getBoundingClientRect(), p = pane.getBoundingClientRect();
    return { start: c.left, width: Math.max(0, c.width + p.width), divider: divider.getBoundingClientRect() };
  };
  const bounds = () => {
    const width = span().width;
    if (!width) return [0, 1];
    const low = AGENT_PANE_MIN_TERMINAL / width;
    const high = 1 - AGENT_PANE_MIN_OVERVIEW / width;
    return low <= high ? [low, high] : [.5, .5];
  };
  const apply = (next, save = false) => {
    const [low, high] = bounds();
    ratio = Math.min(high, Math.max(low, Number(next) || AGENT_PANE_DEFAULT_RATIO));
    root.style.setProperty("--ag-terminal-fr", `${ratio}fr`);
    root.style.setProperty("--ag-pane-fr", `${1 - ratio}fr`);
    divider.setAttribute("aria-valuemin", String(Math.round(low * 100)));
    divider.setAttribute("aria-valuemax", String(Math.round(high * 100)));
    divider.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
    if (save) { preferredRatio = ratio; ls(AGENT_PANE_RATIO_KEY, String(ratio)); }
    return ratio;
  };
  apply(ratio);

  const finish = () => {
    if (!dragging) return;
    dragging = false;
    divider.classList.remove("dragging");
    root.classList.remove("agent-pane-dragging");
    shield?.remove(); shield = null;
    preferredRatio = ratio;
    ls(AGENT_PANE_RATIO_KEY, String(ratio));
  };
  const move = event => {
    if (!dragging) return;
    const { start, width, divider: d } = span();
    if (width) apply((event.clientX - start - d.width / 2) / width);
  };
  const begin = event => {
    if (event.button !== 0) return;
    event.preventDefault();
    dragging = true;
    divider.classList.add("dragging");
    root.classList.add("agent-pane-dragging");
    shield = document.createElement("div");
    shield.className = "agent-pane-drag-shield";
    shield.addEventListener("pointermove", move);
    shield.addEventListener("pointerup", finish, { once: true });
    shield.addEventListener("pointercancel", finish, { once: true });
    document.body.appendChild(shield);
    divider.setPointerCapture?.(event.pointerId);
    window.addEventListener("blur", finish);
  };
  const key = event => {
    const step = .03;
    let next;
    if (event.key === "ArrowLeft") next = ratio - step;
    else if (event.key === "ArrowRight") next = ratio + step;
    else if (event.key === "Home") next = bounds()[0];
    else if (event.key === "End") next = bounds()[1];
    else return;
    event.preventDefault();
    apply(next, true);
  };
  divider.addEventListener("pointerdown", begin);
  divider.addEventListener("pointermove", move);
  divider.addEventListener("pointerup", finish);
  divider.addEventListener("pointercancel", finish);
  divider.addEventListener("keydown", key);
  root._agentPaneDividerRefresh = () => apply(preferredRatio);
  resize?.observe(root);
  return () => {
    finish();
    resize?.disconnect();
    window.removeEventListener("blur", finish);
    divider.removeEventListener("pointerdown", begin);
    divider.removeEventListener("pointermove", move);
    divider.removeEventListener("pointerup", finish);
    divider.removeEventListener("pointercancel", finish);
    divider.removeEventListener("keydown", key);
    if (root._agentPaneDividerRefresh) delete root._agentPaneDividerRefresh;
  };
}

function agpadDispose() {
  _agpad?.focusDetail?.dispose?.();
  _agpad?.dividerDispose?.();
  _agpad = null;
}

function agpadTerminalButton(tools, old) {
  if (old) { tools.appendChild(old); return; }
  const button = document.createElement("button");
  button.className = "ttab agpad-toggle"; button.id = "agpad-toggle";
  button.setAttribute("aria-label", "Agent pane");
  button.setAttribute("aria-controls", "agpad");
  button.setAttribute("aria-expanded", "true");
  button.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" aria-hidden="true"><rect x="3" y="4" width="18" height="16" rx="2"/><path d="M15 4v16"/></svg>';
  tools.appendChild(button);
}

function agpadGroups(tasks, name) {
  const actor = `agent:${name}`;
  const created = t => t.created_by ? t.created_by_kind === 'agent' && String(t.created_by_name || '').toLowerCase() === name.toLowerCase() : t.notes?.[0]?.by === actor;
  const unfinished = tasks.filter(t => !tClosed(t));
  const open = unfinished.filter(t => !taskSnoozed(t));
  const owned = open.filter(t => tAssignees(t).includes(actor));
  return {
    open: owned,
    // Lists carry creator attribution without downloading the entire note history.
    // Retain the first-note proxy only for older payloads without a creator ID.
    waiting: open.filter(t => !tAssignees(t).includes(actor) && created(t)),
    snoozed: unfinished.filter(t => taskSnoozed(t) && (tAssignees(t).includes(actor) || created(t))),
  };
}

function agpadActive(p) {
  return p && _agpad === p && p.agent && p.open && !document.hidden &&
    (p.isCurrent ? p.isCurrent() : state.tab === "agents" && state.biz === p.biz && state.agent === p.agent) &&
    p.el.isConnected && $("#agpad") === p.el;
}

function agpadMount(name, el, options = {}) {
  _agpad?.focusDetail?.dispose?.();
  _agpad?.dividerDispose?.();
  if (_agpad) _agpad.focusDetail = null;
  const p = _agpad = {
    biz: state.biz, agent: name, el, open: !!name && ls(`agpad:${state.biz}`) !== "off",
    view: "tasks",
    logs: null, logError: false, nextLog: 0, pending: false,
    tasks: null, source: state.tasks, tasksStarted: false, taskError: false, bodyHtml: null,
    views: ["tasks", "inbox"], ...options,
  };
  p.dividerDispose = agentPaneDividerMount(el.closest?.(".agsplit"));
  p.views = [...new Set([...p.views, "status"])];
  const button = $("#agpad-toggle");
  if (button) {
    button.disabled = !name;
    button.onclick = () => {
      if (_agpad !== p || !p.agent) return;
      p.open = !p.open;
      ls(`agpad:${p.biz}`, p.open ? "on" : "off");
      p.nextLog = 0;
      agpadVisibility(p);
      agpadTick();
    };
  }
  agpadVisibility(p);
  if (!name) { el.innerHTML = ""; return; }
  el.onclick = e => {
    const row = e.target.closest(".trow");
    if (row && agpadActive(p)) tOpenTask(row.dataset.id);
  };
  agpadSetView(p, p.views[0]);
}

function agpadSetView(p, view) {
  if (_agpad !== p || !p.views.includes(view)) return;
  p.focusDetail?.dispose?.();
  p.focusDetail = null;
  const from = p.view;
  p.view = view;
  p.bodyHtml = null;
  p.el.innerHTML = tHead({ view, views: p.views, bodyId: "agpad-body", taskTools: false });
  if (view === 'status') agentCurrentWorkMount(p);
  tWireHead(p.el, from, next => agpadSetView(p, next));
  if(typeof agentPaneIndicators==='function')agentPaneIndicators(p);
  if (view === "activity") p.nextLog = 0;
  if (view === "inbox") p.nextInbox = 0;
  if (view === 'focus') { p.nextFocus = 0; agfocusMount(p); }
  if (view === 'globalTasks') globalTasksMount($('#agpad-body', p.el), {agentId: p.agentId, compact: true});
  agpadPaint(p);
  agpadTick();
}

function agpadVisibility(p) {
  p.el.closest(".agsplit").classList.toggle("pad-off", !p.open);
  p.el.hidden = !p.open;
  p.el.closest(".agsplit")._agentPaneDividerRefresh?.();
  const button = $("#agpad-toggle");
  if (button) {
    button.setAttribute("aria-expanded", String(p.open));
    button.removeAttribute("title");
  }
  if (p.open) moveTInd(p.el, p.view);
}

function agpadPaint(p) {
  if (p.view === 'status' || p.view === 'focus') return;
  if (p.view === 'globalTasks') return;
  if (p.view === "inbox") { agpadPaintInbox(p); return; }
  let html;
  if (p.view === "activity") {
    html = p.logs ? tActivityHtml(p.logs, `No actions in the latest ${AGPAD_LOG_N} business log entries.`) : tEmptyHtml("Loading activity…");
    if (p.logError) html = tEmptyHtml(p.logs ? "Could not refresh activity. Showing the last update." : "Activity unavailable. Retrying…") + (p.logs ? html : "");
  } else if (!p.tasks) {
    html = tEmptyHtml(p.taskError ? "Tasks unavailable. Waiting for the next refresh…" : "Loading tasks…");
  } else {
    const groups = agpadGroups(p.tasks, p.agent);
    html = tSection("open", groups.open, { label: "Open tasks", canAdd: false, detailsOnly: true }) +
      tSection("waiting", groups.waiting, { label: "Waiting on", canAdd: false, detailsOnly: true }) +
      (groups.snoozed.length ? tSection("snoozed", groups.snoozed, { label: "Snoozed", canAdd: false, detailsOnly: true }) : '');
  }
  if (p.bodyHtml === html) return;
  p.bodyHtml = html;
  const body = $("#agpad-body", p.el), scroll = body.scrollTop;
  body.innerHTML = html;
  body.scrollTop = scroll;
  if (p.view === "tasks") tWireRows(body, true);
  else if (p.logs) logTips(body, p.logs);
}

async function agpadRefreshLog(p) {
  if (!agpadActive(p) || p.view !== "activity" || p.pending) return;
  p.pending = true;
  try {
    const rows = await api(`/biz/${encodeURIComponent(p.biz)}/log?n=${AGPAD_LOG_N}`);
    if (!agpadActive(p) || p.view !== "activity") return;
    if (!Array.isArray(rows)) throw new Error("Log unavailable");
    p.logs = rows.filter(e => e.actor === `agent:${p.agent}`);
    p.logError = false;
  } catch (e) {
    if (agpadActive(p) && p.view === "activity") p.logError = true;
  } finally {
    p.pending = false;
    p.nextLog = Date.now() + AGPAD_POLL_MS;
    if (agpadActive(p) && p.view === "activity") agpadPaint(p);
  }
}

function agpadTick() {
  const p = _agpad;
  if (!agpadActive(p)) return;
  if(typeof agentPaneIndicators==='function')agentPaneIndicators(p);
  if (p.view === 'status' && Date.now() >= (p.nextActivity || 0)) agentCurrentWorkRefresh(p);
  if (p.views.includes('focus')) agfocusTick(p);
  if (p.view === 'status' || p.view === 'focus') return;
  if (p.view === 'globalTasks') return;
  if (p.view === "inbox") {
    if (Date.now() >= (p.nextInbox || 0)) agpadRefreshInbox(p);
    return;
  }
  // A newly mounted pane must not read the previous business's task array.
  // Ask the existing shared refresh once, then follow its cache (including
  // optimistic task edits). All subsequent task requests belong to tasks.js.
  if (!p.tasksStarted) {
    p.tasksStarted = true;
    refreshTasks().then(() => {
      if (!agpadActive(p)) return;
      p.taskError = state.tasks === p.source && p.tasks === null;
      agpadTick();
    }).catch(() => {
      if (agpadActive(p)) { p.taskError = true; agpadPaint(p); }
    });
  }
  if (state.tasks !== p.source) { p.source = p.tasks = state.tasks; p.taskError = false; }
  agpadPaint(p);
  if (p.view === "activity" && Date.now() >= p.nextLog) agpadRefreshLog(p);
}

// A cheap local reconciliation keeps edits in the shared task cache visible.
// Refresh only the visible agent pane.
setInterval(agpadTick, 1000);

function agpadInboxAge(at, unavailable = 'Time unavailable') {
  const checked = Date.parse(at);
  if (!Number.isFinite(checked)) return unavailable;
  const minutes = Math.max(0, Math.floor((Date.now() - checked) / 60000));
  const [amount, unit] = minutes >= 1440 ? [Math.floor(minutes / 1440), 'day']
    : minutes >= 60 ? [Math.floor(minutes / 60), 'hour'] : [minutes, 'minute'];
  return `${amount} ${unit}${amount === 1 ? '' : 's'} ago`;
}

function agpadWatcherAge(at) {
  const age = agpadInboxAge(at, '');
  return age ? `Last check: ${age}` : 'Last check unavailable';
}

function agpadPaintInbox(p) {
  const body = $('#agpad-body', p.el), data = p.inbox;
  if (!body) return;
  const error = p.inboxError ? '<p class="agpad-inbox-status" role="alert">Inbox unavailable. Retrying…</p>' : '';
  const agentId = p.agentId || state.agents.find(agent => agent.name === p.agent)?.agent_id;
  const card = (item, subscription = false) => item.message ? `<details class="agpad-inbox-item agpad-message" data-message-id="${esc(item.message.id)}">
    <summary><strong>Message from ${esc(item.message.sender_name)}</strong><span>${esc(agpadInboxAge(item.created_at))}${item.message.reply_to ? ' · Reply' : ''}</span><span>${esc(messagePreview(item.message.body,item.message.summary))}</span></summary>
    <div class="agpad-message-body">${esc(item.message.body)}</div>
    <span>To ${esc(item.message.recipient_name)}</span></details>` : `<button type="button" class="agpad-inbox-item" data-agent-id="${esc(agentId || '')}" data-inbox-source="${esc(item.source_id)}" data-source-type="${esc(item.source_type)}" data-business="${esc(item.business_slug || '')}">
    <strong>${esc(item.summary || 'Source item')}</strong><span>${esc(({focus_cards:'Focus Reference',stories:'Story',tasks:'Task',signoffs:'Signoff',documents:'Document',email_conversations:'Email',data_tables:'Table',action_logs:'Action',requests:'Request',agent_profiles:'Agent',principals:'Person',context_entities:'Reference'})[item.source_type] || 'Update')}${subscription ? (item.include_related ? ' · Includes linked items' : ' · Item only') : ' · ' + esc(item.event_type.split(',').map(type => ({relationship_changed:'Links changed',assignment_changed:'Assignment changed',response_submitted:'Response submitted',action_outcome:'Action result',note_added:'Note added',note:'Note added',message_changed:'Message received'})[type] || type.replaceAll('_', ' ')).join(', '))}</span>
    ${subscription ? '' : `<span>${esc(item.actor_name)} · ${esc(agpadInboxAge(item.created_at))}</span>`}</button>`;
  const html = error + (!data ? tEmptyHtml(p.inboxError ? 'No inbox data available.' : 'Loading inbox…') :
    `<div class="agpad-inbox-top"><h3 class="agpad-inbox-heading">Pending updates</h3><div class="agpad-inbox-status"><span class="agpad-watcher-dot${data.watcher_active ? ' connected' : ''}" role="img" aria-label="${data.watcher_active ? 'Watcher connected' : data.watcher ? 'Watcher disconnected' : 'Watcher not started'}" title="${data.watcher_active ? 'Watcher connected' : data.watcher ? 'Watcher disconnected' : 'Watcher not started'}"></span><span>${data.watcher ? esc(agpadWatcherAge(data.watcher.last_check_at)) : 'Claude must start its inbox watcher.'}</span></div></div>${data.items.length ? data.items.map(item => card(item)).join('') : tEmptyHtml('No accessible pending updates.')}
    ${data.next_cursor ? '<button type="button" class="quiet" id="agpad-inbox-more">More updates</button>' : ''}
    <h3 class="agpad-inbox-heading">Subscriptions</h3>${data.subscriptions.length ? data.subscriptions.map(item => card(item, true)).join('') : tEmptyHtml('No subscriptions.')}`);
  if (p.bodyHtml === html) return;
  p.bodyHtml = html;
  const scroll = body.scrollTop;
  const expandedMessages=new Set([...body.querySelectorAll('.agpad-message[open]')].map(el=>el.dataset.messageId));
  body.innerHTML = html; body.scrollTop = scroll;
  body.querySelectorAll('.agpad-message').forEach(el=>{el.open=expandedMessages.has(el.dataset.messageId);});
  body.querySelectorAll('[data-inbox-source]').forEach(button => {
    button.onclick = () => agpadOpenInboxSource(button.dataset).catch(error => toast(error.message || 'Source unavailable'));
  });
  const more = $('#agpad-inbox-more', body);
  if (more) more.onclick = () => agpadRefreshInbox(p, true);
}

async function agpadRefreshInbox(p, more = false) {
  if (!agpadActive(p) || p.view !== 'inbox' || p.inboxPending) return;
  p.inboxPending = true;
  try {
    const agentId = p.agentId || state.agents.find(agent => agent.name === p.agent)?.agent_id;
    if (!agentId) throw new Error('Registered agent unavailable');
    let cursor = more ? p.inbox?.next_cursor : null, data, items = more ? [...p.inbox.items] : [];
    const pages = more ? 1 : Math.max(1, Math.ceil((p.inbox?.items.length || 0) / 20));
    for (let page = 0; page < pages; page++) {
      data = coreData(await api(`/me/agents/${encodeURIComponent(agentId)}/inbox${cursor ? '?after=' + cursor : ''}`));
      if (!agpadActive(p) || p.view !== 'inbox') return;
      if (!Array.isArray(data.items) || !Array.isArray(data.subscriptions)) throw new Error('Invalid inbox response');
      items.push(...data.items); cursor = data.next_cursor;
      if (!cursor) break;
    }
    p.inbox = { ...data, items: [...new Map(items.map(item => [item.id, item])).values()] };
    p.inboxError = false;
  } catch (_) {
    if (agpadActive(p) && p.view === 'inbox') p.inboxError = true;
  } finally {
    p.inboxPending = false; p.nextInbox = Date.now() + AGPAD_POLL_MS;
    if (agpadActive(p) && p.view === 'inbox') agpadPaintInbox(p);
  }
}

async function agpadOpenInboxSource({ inboxSource: id, sourceType: type, business: biz, agentId }) {
  if (type === 'focus_cards') {
    const {item}=coreData(await post('/root/operations/focus_get',{scope:'global',item_id:id}));
    if(item.unavailable)throw Error('This Focus item is no longer available.');
    modal({title:esc(item.label),body:'<div id="focus-inbox-conversation"></div>',actions:[]});
    // The card's canonical binding owns the discussion, so this modal keys the
    // same state as Focus: one draft, cursor and submission per bound object.
    const binding=item.conversation || {type:'reference',id:item.id};
    conversationMount(document.querySelector('#focus-inbox-conversation'),
      {type:binding.type,id:binding.id,biz:item.resolved_source?.business_slug || item.context_business_slug || null,revision:item.source_revision},{
      privateGlobal:true,
      privateSource:{type:binding.type,id:binding.id,...(binding.business_id?{business_id:binding.business_id}:{})},
      read:async pagination=>coreData(await post('/root/operations/focus_get',{scope:'global',item_id:id,conversation:true,...pagination})).item.thread,
      send:async request=>coreData(await post('/root/operations/focus_reply',{scope:'global',item_id:id,text:request.text,idempotency_key:request.idempotency_key}))
    });
    return;
  }
  if (type === 'signoffs') return sgAttentionNavigate(biz || null, id);
  if (!biz && type === 'documents') {
    const { document: source } = coreData(await api(`/me/agents/${encodeURIComponent(agentId)}/inbox/documents/${encodeURIComponent(id)}`));
    return modal({ title: esc(source.metadata?.title || source.path), body: `<div class="story-source">${esc(source.body || '')}</div>`, actions: [] });
  }
  if (biz && (state.scope === 'global' || state.biz !== biz)) await selectBiz(biz);
  if (biz && state.biz !== biz) return;
  if (type === 'data_tables') return tablesOpen(biz, id);
  if (type === 'agent_profiles') {
    const { agent } = coreData(await api(`/biz/${encodeURIComponent(biz)}/agents/${encodeURIComponent(id)}`));
    if (agent.status !== 'active') return modal({ title: esc(agent.display_name || agent.name), body: `<p>${esc(agent.status)}</p>`, actions: [] });
    state.agent = agent.name; return goTab('agents');
  }
  if (type === 'principals' || type === 'context_entities') {
    const { item } = await storyCall(biz, 'context_items_get', { item: { type: type === 'principals' ? 'person' : 'entity', id } });
    if (type === 'context_entities' && /^https?:\/\//.test(item.url || '')) return storyOpenLink({biz}, {resource_type:'entity',resource_id:id,current:item});
    return modal({title:esc(item.label),body:`<p>${esc([item.role,item.org,item.about,item.summary].filter(Boolean).join(' · '))}</p>`,actions:[]});
  }
  if (type === 'stories') { state.ctx = '_stories/' + id; return goTab('context'); }
  if (type === 'tasks') return tOpenTask(id);
  const data = await storyCall(biz, 'stories_resource_get', { resource_type: type, resource_id: id });
  return storyOpenLink({ biz }, { resource_type: type, resource_id: id, current: data.resource || {} });
}
