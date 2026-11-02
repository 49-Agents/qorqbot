// Viewbox: the links the selected agent placed for the owner to inspect. An owner-only read of the agent's board;
// Opening an item clears only that placement's owner attention notice; agent state is unchanged.
const AGVB_LIMIT = 30;
const AGVB_MIN = { w: 240, h: 180 };
const AGVB_STEP = 16, AGVB_STEP_LARGE = 64;
// Only these exact same-origin paths are the application's own viewer shells; every other URL is an ordinary site.
const AGVB_VIEWERS = new Set(['/files/view', '/tables/view']);
const AGVB_KIND_LABEL = { file: 'File', table: 'Table', local: 'Local site', web: 'Web page' };
const AGVB_SVG = body => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
const AGVB_ICON = {
  file: AGVB_SVG('<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5M9 13h6M9 17h6"/>'),
  table: AGVB_SVG('<rect x="3" y="4" width="18" height="16" rx="2"/><path d="M3 10h18M3 15h18M9 4v16"/>'),
  local: AGVB_SVG('<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>'),
  web: AGVB_SVG('<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c3 3 3 15 0 18M12 3c-3 3-3 15 0 18"/>'),
  back: AGVB_SVG('<path d="m12 19-7-7 7-7M5 12h14"/>'),
  fit: AGVB_SVG('<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>'),
  open: AGVB_SVG('<path d="M14 4h6v6M20 4 10 14M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>'),
};

function agvbSizeKey(p) { return `agvb:size:${p.agentId}`; }
// Manual frame size is remembered per agent on this browser; unreadable values mean automatic fill.
function agvbStoredSize(p) {
  try {
    const value = JSON.parse(ls(agvbSizeKey(p)) || 'null');
    return value && Number.isSafeInteger(value.w) && Number.isSafeInteger(value.h) && value.w > 0 && value.h > 0 ? { w: value.w, h: value.h } : null;
  } catch (_) { return null; }
}
function agvbAge(at, now = Date.now()) {
  const time = Date.parse(at);
  if (!Number.isFinite(time)) return '';
  const minutes = Math.max(0, Math.floor((now - time) / 60000));
  return minutes < 1 ? 'now' : minutes < 60 ? `${minutes}m` : minutes < 1440 ? `${Math.floor(minutes / 60)}h` : `${Math.floor(minutes / 1440)}d`;
}
// A link becomes a frame URL only as http(s). `trusted` means exactly this application's own viewer shell.
function agvbTarget(link) {
  if (typeof link !== 'string' || link.startsWith('//') || link.includes('\\')) return null;
  try {
    const url = new URL(link, location.origin);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
    return { href: url.href, trusted: url.origin === location.origin && AGVB_VIEWERS.has(url.pathname) && !url.username && !url.password };
  } catch (_) { return null; }
}
function agvbItem(value) {
  if (!value || typeof value !== 'object' || typeof value.id !== 'string' || typeof value.link !== 'string'
    || typeof value.title !== 'string' || typeof value.placed_at !== 'string') return null;
  return { id: value.id, link: value.link, title: value.title, kind: AGVB_ICON[value.kind] && value.kind !== 'back' && value.kind !== 'fit' && value.kind !== 'open' ? value.kind : 'web', placed_at: value.placed_at,
    attention_id: typeof value.attention_id === 'string' ? value.attention_id : null };
}
const agvbStamp = item => { const time = new Date(item.placed_at); return Number.isNaN(time.getTime()) ? item.placed_at : time.toLocaleString(); };
const agvbSignature = items => JSON.stringify((items || []).map(item => [item.id, item.title, item.link, item.kind, item.placed_at, item.attention_id]));

async function agviewboxNavigate(item) {
  if (typeof globalAgentsNavigate === 'function' && await globalAgentsNavigate(item.agent_id, {view: 'viewbox', viewboxItem: item.viewbox_item_id})) return;
  const samePage = location.hash === '#/global/agents', url = new URL(location.href);
  url.searchParams.set('agent', item.agent_id);
  url.searchParams.set('workspace', 'viewbox');
  url.searchParams.set('viewbox_item', item.viewbox_item_id);
  url.searchParams.delete('focus_item');
  history.replaceState(null, '', url);
  if (samePage) await renderTab(); else location.hash = '/global/agents';
}

function agviewboxReveal(p, id) {
  const vb = p?.vb;
  if (!vb || vb.dead) return;
  vb.linkedItem = id;
  // Reuse a pending board read, or refresh an existing board before opening the
  // placement. Its native frame load still owns unread acknowledgement.
  if (vb.mode === 'frame') agviewboxBack(p, true);
  else void agviewboxRefresh(p);
}

function agviewboxMount(p) {
  const body = $('#agpad-body', p.el);
  if (!body || !p.agentId) return;
  const vb = p.vb = { agentId: p.agentId, abort: null, pending: false, next: 0, items: null, error: false, mode: 'list',
    selected: null, signature: null, listScroll: 0, size: agvbStoredSize(p), frame: null,
    linkedItem: p.viewboxOpen || new URL(location.href).searchParams.get('viewbox_item') };
  // A Focus-strip click hands over the items it already shows so the detail opens without a list detour.
  const seeded = p.viewboxSeed; p.viewboxOpen = p.viewboxSeed = null;
  body.classList.add('agvb-body');
  body.classList.remove('agvb-framed');
  body.innerHTML = `<div class="agvb"><div class="agvb-listview"><p class="agvb-status" role="alert" hidden></p>
    <ul class="agvb-list" aria-label="Viewbox"></ul><p class="agvb-empty" hidden>Nothing shared yet.</p></div><div class="agvb-frameview" hidden></div></div>`;
  $('.agvb-list', body).onclick = event => {
    const row = event.target.closest('.agvb-row');
    if (row) agviewboxOpen(p, row.dataset.id);
  };
  p.workspaceDispose = () => agviewboxDispose(p, vb);
  if (seeded && vb.linkedItem && seeded.some(item => item.id === vb.linkedItem)) {
    vb.items = seeded;
    agviewboxOpen(p, vb.linkedItem);
  }
  agviewboxRefresh(p);
}

// Everything owned by the open frame and the in-flight read goes away together; a late response finds p.vb replaced.
function agviewboxDispose(p, vb = p.vb) {
  if (!vb) return;
  vb.dead = true;
  vb.abort?.abort();
  agviewboxCloseFrame(vb);
  if (p.vb === vb) p.vb = null;
}

async function agviewboxRefresh(p) {
  const vb = p.vb;
  if (!vb || vb.pending || vb.dead || !agpadActive(p) || p.view !== 'viewbox') return;
  vb.pending = true;
  const controller = vb.abort = new AbortController();
  try {
    const data = coreData(await api(`/root/agents/${encodeURIComponent(vb.agentId)}/viewbox`, { signal: controller.signal }));
    if (p.vb !== vb || vb.dead || data.agent_id !== vb.agentId || !Array.isArray(data.items)) throw new Error('Viewbox response does not match this agent');
    vb.items = data.items.map(agvbItem).filter(Boolean).slice(0, AGVB_LIMIT);
    vb.error = false;
  } catch (_) {
    if (p.vb === vb && !vb.dead && !controller.signal.aborted) vb.error = true;
  } finally {
    if (p.vb === vb && !vb.dead) {
      vb.pending = false; vb.next = Date.now() + AGPAD_POLL_MS; agviewboxPaint(p);
      if (!vb.error && vb.linkedItem) {
        const id = vb.linkedItem;
        vb.linkedItem = null;
        const url = new URL(location.href);
        if (url.searchParams.get('viewbox_item') === id) { url.searchParams.delete('viewbox_item'); history.replaceState(null, '', url); }
        if (vb.items.some(item => item.id === id)) agviewboxOpen(p, id);
      }
    }
  }
}

function agviewboxTick(p) {
  const vb = p.vb;
  if (!vb || vb.dead) return;
  if (!vb.pending && Date.now() >= vb.next) agviewboxRefresh(p);
  if (vb.mode === 'list') agviewboxAges(p);
  if (vb.frame?.loaded) vb.frame.markViewed();
}

function agviewboxAges(p) {
  const now = Date.now();
  for (const time of p.el.querySelectorAll('.agvb-age')) {
    const age = agvbAge(time.dateTime, now);
    if (time.textContent !== age) time.textContent = age;
  }
}

function agviewboxPaint(p) {
  const vb = p.vb, root = $('.agvb', p.el);
  if (!vb || !root || vb.mode !== 'list') return;
  const status = $('.agvb-status', root), empty = $('.agvb-empty', root), list = $('.agvb-list', root);
  status.hidden = !vb.error;
  status.textContent = vb.error ? 'Viewbox unavailable. Retrying…' : '';
  empty.hidden = !(vb.items && vb.items.length === 0);
  // An unchanged board is never repainted, so focus, scroll and hover survive every refresh.
  const signature = agvbSignature(vb.items);
  if (vb.items && signature !== vb.signature) {
    vb.signature = signature;
    const focused = document.activeElement?.closest?.('.agvb-row')?.dataset.id;
    const body = $('#agpad-body', p.el), scroll = body.scrollTop;
    list.innerHTML = vb.items.map(item => {
      const label = AGVB_KIND_LABEL[item.kind];
      return `<li><button type="button" class="agvb-row" data-id="${esc(item.id)}" data-kind="${esc(item.kind)}"
        aria-label="${esc(`${item.title}, ${label}, ${agvbStamp(item)}, ${item.link}`)}"><span class="agvb-ic" aria-hidden="true">${AGVB_ICON[item.kind]}</span>
        <span class="agvb-title">${esc(item.title)}</span><time class="agvb-age" datetime="${esc(item.placed_at)}">${esc(agvbAge(item.placed_at))}</time></button></li>`;
    }).join('');
    body.scrollTop = scroll;
    if (focused) list.querySelector(`.agvb-row[data-id="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });
  }
  agviewboxAges(p);
}

function agviewboxCloseFrame(vb) {
  const frame = vb.frame;
  if (!frame) return;
  frame.cleanup?.();
  frame.iframe.src = 'about:blank';
  frame.iframe.remove();
  vb.frame = null;
}

function agviewboxOpen(p, id) {
  const vb = p.vb, item = vb?.items?.find(entry => entry.id === id), root = $('.agvb', p.el);
  if (!vb || !item || !root || vb.mode !== 'list') return;
  const body = $('#agpad-body', p.el);
  vb.listScroll = body.scrollTop;
  vb.mode = 'frame';
  vb.selected = item;
  const target = agvbTarget(item.link);
  const view = $('.agvb-frameview', root);
  $('.agvb-listview', root).hidden = true;
  view.hidden = false;
  body.classList.add('agvb-framed');
  view.innerHTML = `<div class="agvb-bar"><button type="button" class="agvb-back" aria-label="${agpadFocusSurfaces(p) ? 'Back to Focus' : 'Back to Viewbox list'}">${AGVB_ICON.back}</button>
    <strong class="agvb-name">${esc(item.title)}</strong>
    <button type="button" class="agvb-fit" aria-label="Fill pane">${AGVB_ICON.fit}</button>
    <a class="agvb-open" target="_blank" rel="noopener noreferrer" aria-label="Open in new tab">${AGVB_ICON.open}<span>Open in new tab</span></a></div>
    <p class="agvb-fallback" role="status" hidden>This page did not load here. Some sites block embedding or need a full browser; use Open in new tab.</p>
    <div class="agvb-stage"><div class="agvb-box"><button type="button" class="agvb-handle" aria-label="Resize frame" aria-describedby="agvb-hint"></button></div></div>
    <p id="agvb-hint" class="agvb-sr">Use the arrow keys to resize the frame, Shift for larger steps. Fill pane resets to the full pane.</p>
    <p class="agvb-sr agvb-size" aria-live="polite"></p>`;
  const open = $('.agvb-open', view), stage = $('.agvb-stage', view), box = $('.agvb-box', view), handle = $('.agvb-handle', view),
    fallback = $('.agvb-fallback', view), announce = $('.agvb-size', view);
  if (target) open.href = target.href; else open.hidden = true;
  const markViewed = () => {
    if (!vb.dead && p.vb === vb && vb.selected === item && agpadActive(p) && p.view === 'viewbox'
      && typeof attentionQueue !== 'undefined') attentionQueue.viewboxViewed(item);
  };
  open.onclick = markViewed;
  const cleanups = [];
  const frame = vb.frame = { markViewed, loaded: false, cleanup: () => cleanups.splice(0).forEach(fn => fn()) };
  const showFallback = () => { if (vb.frame === frame) fallback.hidden = false; };

  const clamp = (value, available, minimum) => Math.max(Math.min(minimum, available), Math.min(value, available));
  const layout = () => {
    const width = Math.floor(stage.clientWidth), height = Math.floor(stage.clientHeight);
    if (!vb.size) { box.style.width = ''; box.style.height = ''; box.dataset.fill = 'true'; return; }
    box.dataset.fill = 'false';
    box.style.width = `${clamp(vb.size.w, width, AGVB_MIN.w)}px`;
    box.style.height = `${clamp(vb.size.h, height, AGVB_MIN.h)}px`;
  };
  const current = () => ({ w: box.offsetWidth, h: box.offsetHeight });
  const say = text => { announce.textContent = text; };
  const setSize = (size, save) => {
    vb.size = size;
    layout();
    if (save) ls(agvbSizeKey(p), size ? JSON.stringify(size) : '');
    const now = current();
    say(size ? `Frame ${now.w} by ${now.h} pixels` : 'Frame fills the pane');
  };
  layout();
  if (typeof ResizeObserver === 'function') {
    const observer = new ResizeObserver(layout);
    observer.observe(stage);
    cleanups.push(() => observer.disconnect());
  }
  $('.agvb-fit', view).onclick = () => setSize(null, true);

  let drag = null, shield = null;
  const endDrag = save => {
    if (!drag) return;
    drag = null;
    shield?.remove(); shield = null;
    if (save && vb.size) setSize(vb.size, true);
  };
  handle.addEventListener('pointerdown', event => {
    if (event.button !== 0 && event.pointerType === 'mouse') return;
    event.preventDefault();
    drag = { x: event.clientX, y: event.clientY, ...current() };
    // The shield keeps the frame from swallowing pointer events while dragging across it.
    shield = document.createElement('div');
    shield.className = 'agvb-shield';
    stage.appendChild(shield);
    handle.setPointerCapture?.(event.pointerId);
  });
  handle.addEventListener('pointermove', event => {
    if (!drag) return;
    vb.size = { w: Math.round(drag.w + event.clientX - drag.x), h: Math.round(drag.h + event.clientY - drag.y) };
    layout();
  });
  handle.addEventListener('pointerup', () => endDrag(true));
  handle.addEventListener('pointercancel', () => endDrag(true));
  handle.addEventListener('lostpointercapture', () => endDrag(true));
  handle.addEventListener('keydown', event => {
    // Steps start from what is rendered, not the stored size, which can exceed a shrunken pane.
    const step = event.shiftKey ? AGVB_STEP_LARGE : AGVB_STEP, now = current();
    const change = { ArrowRight: [step, 0], ArrowLeft: [-step, 0], ArrowDown: [0, step], ArrowUp: [0, -step] }[event.key];
    if (!change) return;
    event.preventDefault();
    setSize({ w: Math.max(1, now.w + change[0]), h: Math.max(1, now.h + change[1]) }, true);
  });
  const onBlur = () => endDrag(true);
  window.addEventListener('blur', onBlur);
  cleanups.push(() => { endDrag(false); window.removeEventListener('blur', onBlur); });

  // A cross-origin page can be refused by its own headers without any signal here; load does not mean it is visible.
  if (target && !(location.protocol === 'https:' && target.href.startsWith('http:'))) {
    const iframe = frame.iframe = document.createElement('iframe');
    iframe.className = 'agvb-frame';
    iframe.setAttribute('aria-label', `${item.title} (shared by this agent)`);
    iframe.referrerPolicy = 'no-referrer';
    if (!target.trusted) {
      // A separately hosted app needs its own origin for fetch, cookies and
      // storage. The browser still separates it from Qorqut's origin.
      const separateOrigin = new URL(target.href).origin !== location.origin;
      iframe.setAttribute('sandbox', 'allow-scripts allow-forms' + (separateOrigin ? ' allow-same-origin' : ''));
    }
    iframe.addEventListener('error', showFallback);
    iframe.addEventListener('load', () => { frame.loaded = true; markViewed(); });
    iframe.src = target.href;
    box.insertBefore(iframe, handle);
  } else {
    frame.iframe = document.createElement('iframe');  // inert placeholder keeps close logic uniform
    showFallback();
  }
  $('.agvb-back', view).onclick = () => agviewboxBack(p);
  $('.agvb-back', view).focus({ preventScroll: true });
}

// With Focus, an agent's Viewbox is a detail of its Focus board, so Back returns there unless a newly linked item replaces this one.
function agviewboxBack(p, toList = false) {
  const vb = p.vb, root = $('.agvb', p.el);
  if (!vb || !root || vb.mode !== 'frame') return;
  if (!toList && agpadFocusSurfaces(p)) { agpadSetView(p, 'focus'); return; }
  const body = $('#agpad-body', p.el), selected = vb.selected;
  agviewboxCloseFrame(vb);
  vb.mode = 'list';
  vb.selected = null;
  vb.signature = null;
  body.classList.remove('agvb-framed');
  const view = $('.agvb-frameview', root);
  view.hidden = true;
  view.innerHTML = '';
  $('.agvb-listview', root).hidden = false;
  agviewboxPaint(p);
  body.scrollTop = vb.listScroll;
  // Back shows the latest list: read again now rather than waiting for the next refresh.
  vb.next = 0;
  agviewboxRefresh(p);
  const rows = root.querySelectorAll('.agvb-row');
  const row = [...rows].find(entry => entry.dataset.id === selected?.id) || rows[0];
  row?.focus({ preventScroll: true });
}

// ------------------------------------------------------------ Focus surfaces strip
// For an agent with Focus: Browser and Desktop launchers, then the agent's actual Viewbox board, in the compact attention-chip styling.
// Reads the owner board (not the notification queue), so viewed placements stay while they remain on the board.
const AGVB_SURFACES = [['browser', 'Browser'], ['desktop', 'Desktop']];
const agvbSurfaceIcon = name => (typeof T_VIEW_ICON === 'object' && T_VIEW_ICON[name]) || '';

// Owner dismissal of exact queue placements (attention_id). The server keeps the durable preference; this per-agent
// state outlives a pane switch so an in-flight save still hides the placement for a re-mounted strip and its result is not lost.
const AGVB_DISMISS_TIMEOUT_MS = 15000;
const agvbDismissals = new Map();
function agvbDismissState(agentId) {
  if (!agvbDismissals.has(agentId)) agvbDismissals.set(agentId, { saved: new Set(), pending: new Set(), writes: Promise.resolve() });
  return agvbDismissals.get(agentId);
}
// The saved list must be an array of strings; anything else is an invalid board response.
function agvbDismissedIds(value) {
  if (!Array.isArray(value) || !value.every(id => typeof id === 'string')) throw new Error('Viewbox response has invalid dismissals');
  return value;
}
// One additive write per agent at a time, each bounded by its own abort; a failure never blocks the next write.
function agvbDismissSave(agentId, attentionId) {
  const state = agvbDismissState(agentId);
  const run = async () => {
    const controller = new AbortController(), timer = setTimeout(() => controller.abort(), AGVB_DISMISS_TIMEOUT_MS);
    try {
      const data = coreData(await post(`/root/agents/${encodeURIComponent(agentId)}/viewbox/dismiss`, { attention_id: attentionId }, { signal: controller.signal }));
      if (data.agent_id !== agentId || !agvbDismissedIds(data.dismissed_attention_ids).includes(attentionId)) throw new Error('Dismissal response does not match');
      for (const id of data.dismissed_attention_ids) state.saved.add(id);
    } finally { clearTimeout(timer); state.pending.delete(attentionId); }
  };
  const result = state.writes.then(run, run);
  state.writes = result.catch(() => {});
  return result;
}

function agviewboxSurfacesMount(p, host) {
  if (!host || !p.agentId) return null;
  const strip = { agentId: p.agentId, abort: null, pending: false, next: 0, items: null, error: false, signature: null, dead: false, discovery: 'checking', surfaces: null };
  host.setAttribute('aria-label', 'Browser, Desktop and Viewbox');
  host.innerHTML = `<div class="attention-stack agfocus-surfaces"><div class="agfocus-fixed" role="group" aria-label="Browser and Desktop">${AGVB_SURFACES.map(([view, label]) =>
    `<button type="button" class="attention-chip agfocus-surface-chip" data-focus-surface="${view}" aria-disabled="true">${agvbSurfaceIcon(view)}<span class="attention-label">${label}</span></button>`).join('')}</div>
    <span class="agfocus-divider" aria-hidden="true"></span>
    <div class="agfocus-surface-board" role="group" aria-label="Viewbox"></div></div>`;
  const dismissals = agvbDismissState(strip.agentId);
  const hidden = item => !!item.attention_id && (dismissals.saved.has(item.attention_id) || dismissals.pending.has(item.attention_id));
  const visibleItems = () => strip.items && strip.items.filter(item => !hidden(item));
  const board = $('.agfocus-surface-board', host);
  // A launcher is enabled only by this agent's own resolved browser-surface binding (the existing cached discovery),
  // within the scope discovery covers; Desktop is also limited to Ordubashy. Pending, failed and empty read differently.
  const launcher = view => {
    const name = view === 'browser' ? 'Browser' : 'Desktop', found = strip.surfaces;
    if (p.workspaceScope !== 'global') return [false, `${name} unavailable: not offered for this agent here`];
    if (strip.discovery === 'checking') return [false, `${name}: checking this agent’s configuration`];
    if (strip.discovery === 'failed') return [false, `${name} could not be checked. Refresh Focus to retry`];
    if (view === 'desktop' && p.workspaceRole !== 'ordubashy') return [false, 'Desktop unavailable: only Ordubashy has a desktop'];
    const bound = view === 'browser' ? found.browsers.length > 0 : !!found.desktop;
    return bound ? [true, name] : [false, `${name} unavailable: no configured ${view} for this agent`];
  };
  const paintLaunchers = () => {
    for (const chip of host.querySelectorAll('[data-focus-surface]')) {
      const [on, text] = launcher(chip.dataset.focusSurface);
      chip.title = text; chip.setAttribute('aria-label', text);
      if (on) chip.removeAttribute('aria-disabled'); else chip.setAttribute('aria-disabled', 'true');
    }
    agfocusDesktopTab(p);
  };
  // Reuses the pane's cached or pending discovery; only an explicit retry after failure forces another read.
  const discover = force => {
    if (p.workspaceScope !== 'global') { strip.discovery = 'ready'; paintLaunchers(); return; }
    const cached = !force && p.surfaces?.agentId === strip.agentId ? p.surfaces : null;
    if (cached) { strip.surfaces = cached; strip.discovery = 'ready'; paintLaunchers(); return; }
    strip.discovery = 'checking'; paintLaunchers();
    agbrowserSurfaces(p, force).then(found => {
      if (strip.dead || p.focusSurfaces !== strip || found.agentId !== strip.agentId) return;
      strip.surfaces = found; strip.discovery = 'ready';
    }, () => {
      if (strip.dead || p.focusSurfaces !== strip) return;
      strip.discovery = 'failed';
    }).then(() => { if (!strip.dead && p.focusSurfaces === strip) paintLaunchers(); });
  };
  host.onclick = event => {
    const button = event.target.closest('button');
    if (!button || !host.contains(button)) return;
    if (button.dataset.focusSurface) { if (button.getAttribute('aria-disabled') !== 'true') agpadRequestView(p, button.dataset.focusSurface); return; }
    const id = button.dataset.viewboxItem;
    if (!id || !strip.items?.some(item => item.id === id)) return;
    p.viewboxOpen = id; p.viewboxSeed = strip.items;
    agpadSetView(p, 'viewbox');
  };
  // Right-click (or the context-menu key) dismisses only a Viewbox queue placement; the fixed launchers are never touched.
  host.oncontextmenu = event => {
    const chip = event.target.closest?.('[data-viewbox-item]');
    const item = chip && board.contains(chip) ? strip.items?.find(entry => entry.id === chip.dataset.viewboxItem) : null;
    if (!item?.attention_id || item.attention_id !== chip.dataset.attentionId || hidden(item)) return;
    event.preventDefault();
    const siblings = [...board.querySelectorAll('[data-viewbox-item]')], at = siblings.indexOf(chip);
    const refocus = document.activeElement === chip ? (siblings[at + 1] || siblings[at - 1])?.dataset.viewboxItem || '' : null;
    dismissals.pending.add(item.attention_id);
    paint();
    if (refocus !== null) (refocus && board.querySelector(`[data-viewbox-item="${CSS.escape(refocus)}"]`) || host.querySelector('[data-focus-surface]'))?.focus({ preventScroll: true });
    agvbDismissSave(strip.agentId, item.attention_id).then(() => { if (!strip.dead) paint(); }, () => {
      // Only this placement comes back, and only if it is still on the current board; a refresh settles an ambiguous timeout.
      if (typeof toast === 'function') toast(`Could not dismiss “${item.title}”. Right-click it again to retry.`);
      if (strip.dead) return;
      paint(); strip.next = 0; void refresh();
    });
  };
  const paint = () => {
    const items = visibleItems();
    const signature = `${agvbSignature(items)}|${items ? 1 : 0}|${strip.error ? 1 : 0}`;
    if (signature === strip.signature) return;
    strip.signature = signature;
    const scroll = board.scrollLeft, focused = document.activeElement?.closest?.('[data-viewbox-item]')?.dataset.viewboxItem;
    board.innerHTML = (items || []).map(item => {
      const label = AGVB_KIND_LABEL[item.kind];
      return `<button type="button" class="attention-chip agfocus-surface-chip" data-viewbox-item="${esc(item.id)}" data-kind="${esc(item.kind)}"${item.attention_id ? ` data-attention-id="${esc(item.attention_id)}"` : ''}
        title="${esc(`${item.title}\n${label}${item.attention_id ? '\nRight-click to dismiss' : ''}`)}" aria-label="${esc(`${item.title}, ${label}`)}"><span class="attention-viewbox" aria-hidden="true">${AGVB_ICON[item.kind]}</span><span class="attention-label">${esc(item.title)}</span></button>`;
    }).join('') + (!items?.length ? `<span class="attention-empty">${strip.error ? 'Viewbox unavailable' : items ? '' : 'Loading Viewbox…'}</span>` : '');
    board.scrollLeft = scroll;
    if (focused) board.querySelector(`[data-viewbox-item="${CSS.escape(focused)}"]`)?.focus({ preventScroll: true });
  };
  const refresh = async () => {
    if (strip.dead) return;
    strip.abort?.abort();
    const controller = strip.abort = new AbortController();
    strip.pending = true;
    try {
      const data = coreData(await api(`/root/agents/${encodeURIComponent(strip.agentId)}/viewbox`, { signal: controller.signal }));
      if (strip.abort !== controller || strip.dead) return;
      if (data.agent_id !== strip.agentId || !Array.isArray(data.items)) throw new Error('Viewbox response does not match this agent');
      const saved = agvbDismissedIds(data.dismissed_attention_ids);
      strip.items = data.items.map(agvbItem).filter(Boolean).slice(0, AGVB_LIMIT);
      for (const id of saved) dismissals.saved.add(id);
      strip.error = false;
    } catch (_) {
      if (strip.abort !== controller || strip.dead) return;
      strip.error = true;
    }
    strip.pending = false; strip.next = Date.now() + AGPAD_POLL_MS;
    paint();
    agfocusDesktopTab(p);
  };
  strip.refresh = () => { if (strip.discovery === 'failed') discover(true); void refresh(); };
  strip.tick = () => { if (!strip.pending && Date.now() >= strip.next) void refresh(); };
  strip.dispose = () => { strip.dead = true; strip.abort?.abort(); host.onclick = null; host.oncontextmenu = null; host.innerHTML = ''; };
  paint();
  discover(false);
  void refresh();
  return strip;
}
