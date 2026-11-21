// The owner's live view of an agent's built-in browser: the page as a stream of pictures, with the owner's
// pointer and keys sent back to it. The agent is not paused; both can use the page at the same time.
// Server: qorqut/browser/live.py, routes under /api/root/agents/<id>/browser-live.
function aglivePath(agentId, tail = '') { return `/root/agents/${encodeURIComponent(agentId)}/browser-live${tail}`; }

const AGLIVE_BUTTONS = ['left', 'middle', 'right'];
const AGLIVE_SEND_MS = 40;
const AGLIVE_LEAVE_MS = 600;

function agliveModifiers(event) {
  return (event.altKey ? 1 : 0) | (event.ctrlKey ? 2 : 0) | (event.metaKey ? 4 : 0) | (event.shiftKey ? 8 : 0);
}

// A browser key event as the server's key input. Enter types a carriage return, as Chrome expects; other
// named keys (arrows, Tab, Backspace, shortcuts) carry no text. Windows reports AltGr as Ctrl+Alt: while AltGraph is
// down, the key types its character (@, €, {) and is neither "no text" nor a Ctrl+Alt shortcut.
function agliveKey(event, action) {
  const altGraph = !!event.getModifierState?.('AltGraph');
  const plain = (!event.ctrlKey && !event.metaKey) || altGraph;
  const text = action !== 'down' ? '' : event.key === 'Enter' ? '\r' : plain && event.key.length === 1 ? event.key : '';
  const modifiers = altGraph ? agliveModifiers(event) & ~3 : agliveModifiers(event);
  return {type: 'key', action, key: event.key.slice(0, 64), code: (event.code || '').slice(0, 64), text,
    keyCode: event.keyCode > 0 && event.keyCode < 256 ? event.keyCode : 0, modifiers};
}

// Pointer events report no click count (`event.detail` is 0), so the view counts: the same button again, soon and close
// by, is the next click of a double or triple click; a fourth starts over. Returns the count for a press.
const AGLIVE_CLICK_MS = 500, AGLIVE_CLICK_PX = 5;
function agliveClickCounter() {
  let last = null;
  return event => {
    const again = last && last.button === event.button && event.timeStamp - last.time <= AGLIVE_CLICK_MS &&
      Math.abs(event.clientX - last.x) <= AGLIVE_CLICK_PX && Math.abs(event.clientY - last.y) <= AGLIVE_CLICK_PX;
    const count = again ? (last.count % 3) + 1 : 1;
    last = {button: event.button, time: event.timeStamp, x: event.clientX, y: event.clientY, count};
    return count;
  };
}

// Where on the page (in the page's own CSS pixels) a pointer event on the scaled picture landed.
function aglivePoint(event, rect, size) {
  const clamp = (value, limit) => Math.min(Math.max(value, 0), limit);
  return {x: Math.round(clamp((event.clientX - rect.left) * size.width / rect.width, size.width)),
    y: Math.round(clamp((event.clientY - rect.top) * size.height / rect.height, size.height))};
}

// Sends input in order, one request at a time; pointer moves waiting behind a request collapse into the latest one.
function agliveSender(send) {
  let queue = [], busy = false, timer = 0, stopped = false;
  async function flush() {
    timer = 0;
    if (busy || stopped || !queue.length) return;
    const events = queue.splice(0, 64);
    busy = true;
    try { await send(events); } catch (_) { /* the stream reports a closed tab; a lost event is not retried */ }
    busy = false;
    if (queue.length && !stopped) timer = setTimeout(flush, 0);
  }
  return {
    push(event) {
      if (stopped) return;
      const last = queue[queue.length - 1];
      if (event.type === 'mouse' && event.action === 'move' && last?.type === 'mouse' && last.action === 'move') queue[queue.length - 1] = event;
      else queue.push(event);
      if (!timer && !busy) timer = setTimeout(flush, event.type === 'mouse' && event.action === 'move' ? AGLIVE_SEND_MS : 0);
    },
    stop() { stopped = true; clearTimeout(timer); queue = []; },
  };
}

function agliveTabLabel(tab) {
  let host = '';
  try { host = new URL(tab.url).host; } catch (_) { /* about:blank and the like */ }
  return (tab.title || host || tab.url || 'New tab').slice(0, 80);
}

// Mounts the view in `body`. `current()` says whether this mount is still the one on screen; `onDispose(fn)`
// registers its teardown; `bar` is the caller's own controls, wired by `wire(host)`. Returns nothing: every state, including failure, is drawn in `body`.
async function agliveMount(agentId, body, options) {
  const {current, onDispose, tab = null, bar = '', wire = () => {}} = options;
  let source = null, sender = null, disposed = false;
  const dispose = () => { disposed = true; source?.close(); source = null; sender?.stop(); sender = null; };
  onDispose(dispose);
  const alive = () => !disposed && current();
  const status = (message, role = 'status') => {
    body.innerHTML = `<section class="agfocus-desktop-view agbrowser aglive"><div class="agbrowser-bar">${bar}<span class="agbrowser-actions">` +
      `<button type="button" class="quiet agbrowser-reconnect" data-live-reconnect aria-label="Reconnect">${typeof AGBROWSER_ICON_RECONNECT === 'string' ? AGBROWSER_ICON_RECONNECT : 'Reconnect'}</button></span></div>` +
      `<p class="${role === 'alert' ? 'ferr' : 'muted'}" role="${role}">${esc(message)}</p></section>`;
    body.querySelector('[data-live-reconnect]').onclick = () => { dispose(); agliveMount(agentId, body, {...options, tab: null}); };
    wire(body);
  };
  status('Loading browser…');
  let tabs;
  try { tabs = coreData(await api(aglivePath(agentId))).tabs; } catch (error) {
    if (alive()) status(error.message || 'The browser could not be loaded.', 'alert');
    return;
  }
  if (!alive()) return;
  if (!Array.isArray(tabs) || !tabs.length) { status('This browser has no open page yet. It appears here when the agent opens one.'); return; }
  const watched = tabs.find(item => item.id === tab) || tabs[0];
  body.innerHTML = `<section class="agfocus-desktop-view agbrowser aglive"><div class="agbrowser-bar">${bar}` +
    `<select class="aglive-tabs" data-live-tabs aria-label="Browser tab"></select>` +
    `<span class="aglive-note muted">Click the page to use it; press Esc twice to leave it. Your agent keeps working in it too.</span>` +
    `<span class="agbrowser-actions"><button type="button" class="quiet agbrowser-reconnect" data-live-reconnect aria-label="Reconnect">${typeof AGBROWSER_ICON_RECONNECT === 'string' ? AGBROWSER_ICON_RECONNECT : 'Reconnect'}</button></span></div>` +
    `<div class="agbrowser-stage aglive-stage"><img class="aglive-screen" data-live-screen tabindex="0" alt="${esc(agliveTabLabel(watched))}" draggable="false"></div></section>`;
  const screen = body.querySelector('[data-live-screen]'), picker = body.querySelector('[data-live-tabs]');
  const remount = next => { dispose(); agliveMount(agentId, body, {...options, tab: next}); };
  wire(body);
  const paintTabs = list => {
    picker.innerHTML = list.map(item => `<option value="${esc(item.id)}"${item.id === watched.id ? ' selected' : ''}>${esc(agliveTabLabel(item))}</option>`).join('');
    picker.hidden = list.length < 2;
  };
  paintTabs(tabs);
  picker.onchange = () => remount(picker.value);
  body.querySelector('[data-live-reconnect]').onclick = () => remount(watched.id);

  const size = {width: 0, height: 0};
  let view = null;
  sender = agliveSender(events => post(aglivePath(agentId, '/input'), {view, events}));
  const push = event => { if (view && size.width) sender.push(event); };
  source = new EventSource(evUrl(aglivePath(agentId, `/stream?tab=${encodeURIComponent(watched.id)}`)));
  const data = event => { try { return JSON.parse(event.data); } catch (_) { return null; } };
  source.addEventListener('open', event => { view = data(event)?.view || null; });
  source.addEventListener('frame', event => {
    const picture = data(event);
    if (!picture?.data || !alive()) return;
    size.width = picture.width; size.height = picture.height;
    screen.src = 'data:image/jpeg;base64,' + picture.data;
  });
  // The server could not open the view (too many open, the browser stopped); it says why in plain words.
  source.addEventListener('refused', event => { if (alive()) { const message = data(event)?.message; dispose(); status(message || 'The live view could not be opened.', 'alert'); } });
  source.addEventListener('tabs', event => { const list = data(event); if (Array.isArray(list) && alive()) paintTabs(list); });
  source.addEventListener('closed', () => { if (alive()) { dispose(); status('This tab was closed. Reconnect to see the browser’s other pages.'); } });
  // EventSource would retry by itself and open a second view; one failure ends this view and offers Reconnect.
  source.onerror = () => { if (alive()) { dispose(); status('The live view stopped. Reconnect to continue.', 'alert'); } };

  const mouse = (event, action, extra = {}) => push({type: 'mouse', action, ...aglivePoint(event, screen.getBoundingClientRect(), size),
    buttons: event.buttons & 31, modifiers: agliveModifiers(event), ...extra});
  screen.addEventListener('pointermove', event => mouse(event, 'move'));
  const countClick = agliveClickCounter();
  let clicks = 1;
  screen.addEventListener('pointerdown', event => {
    if (event.button > 2) return;
    event.preventDefault(); screen.focus({preventScroll: true});
    clicks = countClick(event);
    try { screen.setPointerCapture(event.pointerId); } catch (_) { /* a synthetic pointer has nothing to capture */ }
    mouse(event, 'down', {button: AGLIVE_BUTTONS[event.button], clicks});
  });
  screen.addEventListener('pointerup', event => {
    if (event.button > 2) return;
    event.preventDefault();
    mouse(event, 'up', {button: AGLIVE_BUTTONS[event.button], clicks});
  });
  screen.addEventListener('contextmenu', event => event.preventDefault());
  screen.addEventListener('wheel', event => {
    if (!view || !size.width) return;      // nothing to forward yet: the dashboard scrolls as usual
    event.preventDefault();
    const scale = event.deltaMode === 1 ? 40 : event.deltaMode === 2 ? 800 : 1;
    const bound = value => Math.min(Math.max(Math.round(value * scale), -10000), 10000);
    push({type: 'wheel', ...aglivePoint(event, screen.getBoundingClientRect(), size), dx: bound(event.deltaX), dy: bound(event.deltaY), modifiers: agliveModifiers(event)});
  }, {passive: false});
  // Paste keeps its default so the paste event below delivers the owner's clipboard text; a composing key is the
  // input method's, and its result arrives as text at compositionend.
  const pasteKey = event => (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'v';
  // Every key goes to the page, Tab included, so the keyboard would never get out of the picture: Escape pressed twice within
  // AGLIVE_LEAVE_MS leaves it (the first still reaches the page; the second is the owner's).
  let escapedAt = -Infinity;
  screen.addEventListener('keydown', event => {
    if (event.isComposing || pasteKey(event)) return;
    if (event.key === 'Escape' && !event.repeat) {
      const leave = event.timeStamp - escapedAt <= AGLIVE_LEAVE_MS;
      escapedAt = leave ? -Infinity : event.timeStamp;
      if (leave) { event.preventDefault(); screen.blur(); return; }
    }
    event.preventDefault();
    push(agliveKey(event, 'down'));
  });
  screen.addEventListener('keyup', event => {
    if (event.isComposing || pasteKey(event)) return;
    event.preventDefault();
    push(agliveKey(event, 'up'));
  });
  screen.addEventListener('paste', event => {
    event.preventDefault();
    const text = (event.clipboardData?.getData('text/plain') || '').slice(0, 10000);
    if (text) push({type: 'text', text});
  });
  screen.addEventListener('compositionend', event => { if (event.data) push({type: 'text', text: event.data.slice(0, 10000)}); });
}
