// Owner Browser view: the selected agent's configured browser desktops, streamed
// through their exact Guacamole connection URLs. The binding read is owner-only
// presentation; it grants no automation and never changes Chrome itself.
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

// Mount-time discovery only decides whether a Browser tab is offered.
function agbrowserDiscover(p) {
  if (!p.agentId || p.workspaceScope !== 'global') return;
  agbrowserSurfaces(p).then(() => { if (_agpad === p) agpadWorkspaceViewsSync(p); }).catch(() => {});
}

function agbrowserTab(p) { return !!(p.browserRequested || p.surfaces?.browsers.length); }

const AGBROWSER_ICON_RECONNECT = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M20 11a8 8 0 0 0-14.5-4.4L4 8.5M4 4v4.5h4.5M4 13a8 8 0 0 0 14.5 4.4l1.5-1.9M20 20v-4.5h-4.5"/></svg>';
const AGBROWSER_ICON_MAIL = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3.5 7 8.5 6 8.5-6"/></svg>';
const agbrowserIcon = name => (typeof T_VIEW_ICON === 'object' && T_VIEW_ICON[name]) || '';

// Icon-only connected toggle; every button carries its own accessible name.
function agbrowserToggle(label, items) {
  return `<div class="agbrowser-switch" role="group" aria-label="${esc(label)}">${items.map(item =>
    `<button type="button" class="quiet agbrowser-seg" ${item.attr}="${esc(item.value)}" aria-pressed="${item.pressed}" aria-label="${esc(item.label)}" title="${esc(item.label)}">${item.icon}</button>`).join('')}</div>`;
}

// Ordubashy alone can switch between its Browser and its full Desktop.
function agbrowserModeToggle(p, mode) {
  if (p.workspaceRole !== 'ordubashy') return '';
  return agbrowserToggle('View', [
    {attr: 'data-agent-mode', value: 'browser', label: 'Browser', icon: agbrowserIcon('browser'), pressed: mode === 'browser'},
    {attr: 'data-agent-mode', value: 'desktop', label: 'Desktop', icon: agbrowserIcon('desktop'), pressed: mode === 'desktop'}]);
}

function agbrowserReconnectButton(attr) {
  return `<button type="button" class="quiet agbrowser-reconnect" ${attr} aria-label="Reconnect" title="Reconnect">${AGBROWSER_ICON_RECONNECT}</button>`;
}

function agbrowserWireMode(p, host) {
  for (const button of host.querySelectorAll('[data-agent-mode]')) button.onclick = () => {
    if (button.getAttribute('aria-pressed') !== 'true') agpadSetView(p, button.dataset.agentMode);
  };
}

function agbrowserStatus(p, body, message, role = 'status', retry = null) {
  body.innerHTML = `<section class="agfocus-desktop-view agbrowser"><div class="agbrowser-bar">${agbrowserModeToggle(p, 'browser')}` +
    (retry ? `<span class="agbrowser-actions">${agbrowserReconnectButton('data-browser-reconnect')}</span>` : '') + '</div>' +
    `<p class="${role === 'alert' ? 'ferr' : 'muted'}" role="${role}">${esc(message)}</p></section>`;
  const button = $('[data-browser-reconnect]', body);
  if (button && retry) button.onclick = retry;
  agbrowserWireMode(p, body);
}

// The frame and its size observers are removed before another stream mounts, so
// a switch or teardown never leaves two viewer connections open.
function agbrowserFrame(p, host, surface, fit) {
  p.browserFrameDispose?.();
  const stage = $('.agbrowser-stage', host);
  const frame = document.createElement('iframe');
  frame.className = 'agfocus-reference-frame agfocus-desktop-frame agbrowser-frame';
  frame.title = `${surface.label} browser`;
  frame.referrerPolicy = 'no-referrer';
  frame.setAttribute('allow', 'clipboard-read; clipboard-write; fullscreen');
  frame.dataset.surface = surface.id;
  stage.replaceChildren(frame);
  const display = agfocusDesktopURL(surface.url);
  frame.src = display.url;
  const disposers = [];
  fit({host, onDispose: fn => disposers.push(fn)}, {width: surface.width || display.width, height: surface.height || display.height});
  p.browserFrameDispose = () => {
    p.browserFrameDispose = null;
    disposers.splice(0).forEach(fn => fn());
    frame.src = 'about:blank';
    frame.remove();
  };
}

function agbrowserShow(p, body, surfaces, selected) {
  const several = surfaces.browsers.length > 1;
  const icon = item => /outlook|mail/i.test(item.id + ' ' + item.label) ? AGBROWSER_ICON_MAIL : agbrowserIcon('browser');
  body.innerHTML = `<section class="agfocus-desktop-view agbrowser"><div class="agfocus-desktop-help agbrowser-bar">
    ${agbrowserModeToggle(p, 'browser')}
    ${several ? agbrowserToggle('Browser', surfaces.browsers.map(item => ({attr: 'data-browser-surface', value: item.id, label: item.label, icon: icon(item), pressed: false}))) : ''}
    <span class="agbrowser-actions">${agbrowserReconnectButton('data-browser-reconnect')}</span></div>
    <div class="agbrowser-stage"></div></section>`;
  const host = $('.agbrowser', body);
  const choose = surface => {
    for (const button of host.querySelectorAll('[data-browser-surface]')) button.setAttribute('aria-pressed', String(button.dataset.browserSurface === surface.id));
    ls(agbrowserSelectionKey(surfaces.agentId), surface.id);
    agbrowserFrame(p, host, surface, agfocusDesktopFrameFit);
  };
  for (const button of host.querySelectorAll('[data-browser-surface]')) button.onclick = () => {
    const surface = surfaces.browsers.find(item => item.id === button.dataset.browserSurface);
    if (surface && button.getAttribute('aria-pressed') !== 'true') choose(surface);
  };
  $('[data-browser-reconnect]', host).onclick = () => agbrowserMount(p, true);
  agbrowserWireMode(p, host);
  choose(selected);
}

async function agbrowserMount(p, reconnect = false) {
  const body = $('#agpad-body', p.el);
  if (!body) return;
  p.browserFrameDispose?.();
  const request = p.browserRequest = (p.browserRequest || 0) + 1;
  p.workspaceDispose = () => { p.browserRequest++; p.browserFrameDispose?.(); };
  const current = () => agpadActive(p) && p.view === 'browser' && p.browserRequest === request && body.isConnected;
  agbrowserStatus(p, body, 'Loading browser…');
  let surfaces;
  try { surfaces = await agbrowserSurfaces(p, reconnect); } catch (error) {
    if (current()) agbrowserStatus(p, body, 'The browser configuration could not be read. ' + error.message, 'alert', () => agbrowserMount(p, true));
    return;
  }
  if (!current() || surfaces.agentId !== p.agentId) return;
  if (!surfaces.browsers.length) {
    agbrowserStatus(p, body, surfaces.configured ? 'No browser is configured for this agent.'
      : 'Browser viewing is not configured yet. An installation owner adds this agent’s browser binding.', 'status', () => agbrowserMount(p, true));
    return;
  }
  const saved = ls(agbrowserSelectionKey(surfaces.agentId));
  agbrowserShow(p, body, surfaces, surfaces.browsers.find(item => item.id === saved) || surfaces.browsers[0]);
}
