// Owner Browser views, streamed through exact Guacamole connection URLs: a global agent's
// own browser desktops in its pane.
// The binding reads are owner-only presentation; they grant no automation and never change Chrome.
function agbrowserPath(agentId) { return `/root/agents/${encodeURIComponent(agentId)}/browser-surfaces`; }
function agbrowserSelectionKey(agentId) { return `agpad:browser:${agentId}`; }

function agbrowserSurface(value, kind) {
  if (!value || typeof value !== 'object' || value.kind !== kind || typeof value.id !== 'string' || !value.id) return null;
  const url = agfocusURL(value.viewer_url);
  if (!url) return null;
  const dimension = n => Number.isSafeInteger(n) && n > 0 && n <= 32768 ? n : null;
  const width = dimension(value.width), height = dimension(value.height);
  return {id: value.id, label: typeof value.label === 'string' && value.label ? value.label : value.id, url,
    width: width && height ? width : null, height: width && height ? height : null};
}

// One read per selected agent (or explicit Reconnect); never polled. A response
// for another agent is refused rather than shown.
function agbrowserSurfaces(p, force = false) {
  const agentId = p.agentId;
  if (!agentId) return Promise.reject(new Error('This agent has no registered identity.'));
  if (!force && p.surfaces) return Promise.resolve(p.surfaces);
  if (!force && p.surfacesPending) return p.surfacesPending;
  const pending = p.surfacesPending = api(agbrowserPath(agentId)).then(data => {
    if (!data || data.agent_id !== agentId || !Array.isArray(data.browsers)) throw new Error('The browser configuration did not match this agent.');
    return {agentId, configured: data.configured === true,
      browsers: data.browsers.map(item => agbrowserSurface(item, 'browser')).filter(Boolean),
      desktop: agbrowserSurface(data.desktop, 'desktop')};
  });
  return pending.then(surfaces => {
    if (p.surfacesPending === pending && p.agentId === agentId) p.surfaces = surfaces;
    return surfaces;
  }).finally(() => { if (p.surfacesPending === pending) p.surfacesPending = null; });
}

// Mount-time discovery only decides whether a global agent's own Browser tab is offered: for a configured viewer,
// or for the agent's built-in browser (shown live by agent-browser-live.js), running or not. In a Focus pane the
// Browser tab is the launcher chip, which is painted before this answer arrives, so it is painted again.
function agbrowserDiscover(p) {
  if (!p.agentId || p.workspaceScope !== 'global') return;
  const sync = () => { if (_agpad === p) { agpadWorkspaceViewsSync(p); p.focusSurfaces?.paint?.(); } };
  agbrowserSurfaces(p).then(sync).catch(() => {});
  if (typeof aglivePath !== 'function') return;
  api(aglivePath(p.agentId)).then(coreData).then(() => true, error => error?.code === 'browser_not_ready')
    .then(built => { if (built) { p.liveBrowser = true; sync(); } });
}

function agbrowserTab(p) { return p.workspaceScope === 'global' && !!(p.browserRequested || p.surfaces?.browsers.length || p.liveBrowser); }

const AGBROWSER_ICON_RECONNECT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.5-4.4L4 8.5M4 4v4.5h4.5M4 13a8 8 0 0 0 14.5 4.4l1.5-1.9M20 20v-4.5h-4.5"/></svg>';
const AGBROWSER_ICON_MAIL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 7 8.5 6 8.5-6"/></svg>';
const agbrowserIcon = name => (typeof T_VIEW_ICON === 'object' && T_VIEW_ICON[name]) || '';

// Icon-only connected toggle; every button carries its own accessible name.
function agbrowserToggle(label, items) {
  return `<div class="agbrowser-switch" role="group" aria-label="${esc(label)}">${items.map(item =>
    `<button type="button" class="quiet agbrowser-seg${item.named ? ' agbrowser-named' : ''}" ${item.attr}="${esc(item.value)}" aria-pressed="${item.pressed}" aria-label="${esc(item.label)}">${item.icon}${item.named ? `<span class="agbrowser-seg-name">${esc(item.label)}</span>` : ''}</button>`).join('')}</div>`;
}

function agbrowserSurfaceIcon(p, surface) {
  if (p.workspaceRole !== 'ordubashy') return /outlook|mail/i.test(surface.id + ' ' + surface.label) ? AGBROWSER_ICON_MAIL : agbrowserIcon('browser');
  if (surface.id === 'ordubashy-browser') return identityAvatar('agent', surface.label, {role: {id: 'ordubashy'}});
  return agbrowserIcon('browser');
}

// Ordubashy alone can switch between its Browser and its full Desktop.
function agbrowserModeToggle(p, mode) {
  if (p.workspaceRole !== 'ordubashy') return '';
  return agbrowserToggle('View', [
    {attr: 'data-agent-mode', value: 'browser', label: 'Browser', icon: agbrowserIcon('browser'), pressed: mode === 'browser'},
    {attr: 'data-agent-mode', value: 'desktop', label: 'Desktop', icon: agbrowserIcon('desktop'), pressed: mode === 'desktop'}]);
}

function agbrowserReconnectButton(attr) {
  return `<button type="button" class="quiet agbrowser-reconnect" ${attr} aria-label="Reconnect">${AGBROWSER_ICON_RECONNECT}</button>`;
}

function agbrowserWireMode(p, host) {
  for (const button of host.querySelectorAll('[data-agent-mode]')) button.onclick = () => {
    if (button.getAttribute('aria-pressed') !== 'true') agpadSetView(p, button.dataset.agentMode);
  };
}

// A Focus surface is opened from the persistent launcher row, so it carries neither the
// browser/desktop switch nor a reconnect button; that row opens each mode and retries it.
function agbrowserStatus(p, body, message, role = 'status', retry = null, ctx = null) {
  const bar = ctx ? '' : agbrowserModeToggle(p, 'browser') +
    (retry ? `<span class="agbrowser-actions">${agbrowserReconnectButton('data-browser-reconnect')}</span>` : '');
  body.innerHTML = `<section class="agfocus-desktop-view agbrowser">${bar ? `<div class="agbrowser-bar">${bar}</div>` : ''}` +
    `<p class="${role === 'alert' ? 'ferr' : 'muted'}" role="${role}">${esc(message)}</p></section>`;
  const button = $('[data-browser-reconnect]', body);
  if (button && retry) button.onclick = retry;
  agbrowserWireMode(p, body);
}

// The frame and its size observers are removed before another stream mounts, so
// a switch or teardown never leaves two viewer connections open.
function agbrowserFrame(p, host, surface, fit, ctx = null) {
  p.browserFrameDispose?.();
  const stage = $('.agbrowser-stage', host);
  const frame = document.createElement('iframe');
  frame.className = 'agfocus-reference-frame agfocus-desktop-frame agbrowser-frame';
  frame.setAttribute('aria-label', `${surface.label} browser`);
  frame.referrerPolicy = 'no-referrer';
  frame.setAttribute('allow', 'clipboard-read; clipboard-write; fullscreen');
  frame.dataset.surface = surface.id;
  stage.replaceChildren(frame);
  const display = agfocusDesktopURL(surface.url);
  frame.src = display.url;
  const disposers = [];
  fit({host, minHeight: ctx?.minHeight, onDispose: fn => disposers.push(fn)}, {width: surface.width || display.width, height: surface.height || display.height});
  p.browserFrameDispose = () => {
    p.browserFrameDispose = null;
    disposers.splice(0).forEach(fn => fn());
    frame.src = 'about:blank';
    frame.remove();
  };
}

function agbrowserShow(p, body, surfaces, selected, ctx = null) {
  const several = surfaces.browsers.length > 1;
  // The switch and reconnect stay out of a Focus surface; choosing another browser surface still works there.
  const picker = several ? agbrowserToggle('Browser', surfaces.browsers.map(item => ({attr: 'data-browser-surface', value: item.id, label: item.label, icon: agbrowserSurfaceIcon(p, item), named: true, pressed: false}))) : '';
  const bar = ctx ? (picker ? `<div class="agfocus-desktop-help agbrowser-bar">${picker}</div>` : '')
    : `<div class="agfocus-desktop-help agbrowser-bar">${agbrowserModeToggle(p, 'browser')}${picker}` +
      `<span class="agbrowser-actions">${agbrowserReconnectButton('data-browser-reconnect')}</span></div>`;
  body.innerHTML = `<section class="agfocus-desktop-view agbrowser">${bar}<div class="agbrowser-stage"></div></section>`;
  const host = $('.agbrowser', body);
  const choose = surface => {
    for (const button of host.querySelectorAll('[data-browser-surface]')) button.setAttribute('aria-pressed', String(button.dataset.browserSurface === surface.id));
    ls(agbrowserSelectionKey(surfaces.agentId), surface.id);
    agbrowserFrame(p, host, surface, agfocusDesktopFrameFit, ctx);
  };
  for (const button of host.querySelectorAll('[data-browser-surface]')) button.onclick = () => {
    const surface = surfaces.browsers.find(item => item.id === button.dataset.browserSurface);
    if (surface && button.getAttribute('aria-pressed') !== 'true') choose(surface);
  };
  const reconnect = $('[data-browser-reconnect]', host);
  if (reconnect) reconnect.onclick = () => agbrowserMount(p, true, ctx);
  agbrowserWireMode(p, host);
  choose(selected);
}

// A nested surface passes its mount context (host, currentness, cleanup registration); without one the pane body is the host.
async function agbrowserMount(p, reconnect = false, ctx = null) {
  const body = ctx ? ctx.host : $('#agpad-body', p.el);
  if (!body) return;
  if (ctx) ctx.reset();
  p.browserFrameDispose?.();
  const request = p.browserRequest = (p.browserRequest || 0) + 1;
  const tab = agbrowserTakeTab(p);
  let liveDispose = null;
  const dispose = () => { p.browserRequest++; p.browserFrameDispose?.(); liveDispose?.(); };
  if (ctx) ctx.onDispose(dispose); else p.workspaceDispose = dispose;
  const current = () => (ctx ? ctx.current() : agpadActive(p) && p.view === 'browser') && p.browserRequest === request && body.isConnected;
  agbrowserStatus(p, body, 'Loading browser…', 'status', null, ctx);
  let surfaces = null, failed = false;
  try { surfaces = await agbrowserSurfaces(p, reconnect); } catch (error) { failed = true; }
  if (!current()) return;
  // Without a configured viewer, the agent's built-in browser is shown live; when finding out failed, the error is shown, not this.
  // A requested tab (a Chat reference) belongs to that built-in browser, so it is shown live whatever else is configured.
  if ((tab || (!failed && !surfaces?.browsers.length)) && typeof agliveMount === 'function') {
    agliveMount(p.agentId, body, {current, onDispose: fn => { liveDispose = fn; }, tab,
      bar: ctx ? '' : agbrowserModeToggle(p, 'browser'), wire: host => agbrowserWireMode(p, host)});
    return;
  }
  if (failed) {
    if (current()) agbrowserStatus(p, body, ctx ? 'Couldn’t load. Open Browser again to retry.' : "Couldn't load. Retry", 'alert', () => agbrowserMount(p, true, ctx), ctx);
    return;
  }
  if (!current() || surfaces.agentId !== p.agentId) return;
  if (!surfaces.browsers.length) {
    agbrowserStatus(p, body, 'Browser unavailable.', 'status', () => agbrowserMount(p, true, ctx), ctx);
    return;
  }
  const saved = ls(agbrowserSelectionKey(surfaces.agentId));
  agbrowserShow(p, body, surfaces, surfaces.browsers.find(item => item.id === saved) || surfaces.browsers[0], ctx);
}

// ---- Chat's tab reference: the shared browser, live, on this tab, in the agent pane the owner is already looking at.
// There is one browser for all agents, so any agent's live view can show any tab; a tab reference never moves the owner to another
// agent's chat. The request waits here for the Browser mount it causes (agbrowserMount takes it), so the one live view does the selecting:
// a view already open on another tab is mounted again on this one, and a tab that has gone meanwhile leaves the live view's usual pick (its
// first open page). It is dropped after a while so that a request nobody served cannot steer a later, ordinary Browser open.
const AGBROWSER_TAB_ID = /^[0-9A-F]{32}$/, AGBROWSER_TAB_WAIT_MS = 10000, AGBROWSER_TAB_UNAVAILABLE = 'This agent’s browser can’t be opened here.';
let agbrowserTabWanted = null;
function agbrowserTakeTab(p) {
  const wanted = agbrowserTabWanted;
  if (!wanted || wanted.agentId !== p.agentId) return null;
  agbrowserTabWanted = null;
  return Date.now() - wanted.at <= AGBROWSER_TAB_WAIT_MS ? wanted.tab : null;
}
// Switches the open agent pane to its Browser view, on this tab, whoever owns the tab (the owner's id is only checked for shape). With no
// agent pane open, goes to the agents page without changing the saved selection and uses the pane that opens there. Never rejects: a
// request that cannot be served says so in a toast and returns false. A pane that has not offered the Browser view yet is told it has one.
async function agentBrowserOpenTab(agentId, tabId) {
  if (typeof embeddedAgentPane !== 'undefined' && embeddedAgentPane) return (await window.parent.agentBrowserOpenTab?.(agentId, tabId)) === true;
  if (typeof agentId !== 'string' || !agentId || typeof tabId !== 'string' || !AGBROWSER_TAB_ID.test(tabId)) return false;
  let wanted = null;
  try {
    if (state.scope !== 'global' || state.globalTab !== 'agents') await selectGlobal('agents');
    const pane = _agpad;
    if (!pane?.agentId) throw new Error(AGBROWSER_TAB_UNAVAILABLE);
    const paneAgent = pane.agentId;
    if (!pane.views.includes('browser')) { pane.liveBrowser = true; agpadWorkspaceViewsSync(pane); pane.focusSurfaces?.paint?.(); }
    agbrowserTabWanted = wanted = {agentId: paneAgent, tab: tabId, at: Date.now()};
    // Its result is not the verdict: a Focus pane opens Browser a moment after this returns false. An owner who cancels the
    // unsaved-changes prompt stays where they were, silently.
    await globalAgentsNavigate(paneAgent, {view: 'browser'});
    if (_agpad?.agentId === paneAgent) return true;
    if (agbrowserTabWanted === wanted) agbrowserTabWanted = null;
    return false;
  } catch (_) {
    if (agbrowserTabWanted === wanted) agbrowserTabWanted = null;
    toast(AGBROWSER_TAB_UNAVAILABLE);
    return false;
  }
}
