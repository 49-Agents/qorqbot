const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* optional local browser test dependency */ }
const available = !!chromium && fs.existsSync(chromium.executablePath());
const skip = {skip: !available && 'Playwright Chromium unavailable'};

// A 1x1 JPEG; the stage stretches it, so the mapping from picture to page is what the test controls.
const JPEG = '/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAP//////////////////////////////////////////////////////////////////////////////////////wgALCAABAAEBAREA/8QAFBABAAAAAAAAAAAAAAAAAAAAAP/aAAgBAQABPxA=';
const TABS = [{id: 'A'.repeat(32), title: 'Inbox', url: 'https://mail.example/inbox'}, {id: 'B'.repeat(32), title: '', url: 'https://shop.example/cart'}];

async function mounted(browser, tabs = TABS) {
  const page = await browser.newPage({viewport: {width: 900, height: 700}});
  await page.setContent('<main id="body"></main>');
  await page.addStyleTag({content: ':root{--line-2:#34424a;--wash:#19232c;--elevated:#1c2730;--fg:#fff;--accent:#a2d7b3}body{margin:0}.aglive-screen{width:640px!important;height:400px!important}'});
  await page.addStyleTag({path: path.join(__dirname, '../css/agent-focus.css')});
  await page.evaluate(tabs => {
    window.esc = value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('"', '&quot;');
    window.coreData = value => value;
    window.evUrl = p => '/api' + p;
    window.tabs = tabs; window.sent = []; window.sources = []; window.disposers = [];
    window.api = async () => { if (window.failure) throw new Error(window.failure); return {tabs: window.tabs}; };
    window.post = async (url, body) => { window.sent.push({url, body}); return {}; };
    window.EventSource = class extends EventTarget {
      constructor(url) { super(); this.url = url; this.closed = false; window.sources.push(this); }
      close() { this.closed = true; }
      emit(name, data) { this.dispatchEvent(new MessageEvent(name, {data: JSON.stringify(data)})); }
    };
  }, tabs);
  await page.addScriptTag({path: path.join(__dirname, '../js/agent-browser-live.js')});
  await page.evaluate(() => agliveMount('agent-1', document.querySelector('#body'), {current: () => true, onDispose: fn => window.disposers.push(fn)}));
  return page;
}
const events = page => page.evaluate(() => window.sent.flatMap(item => item.body.events));
const settle = page => page.waitForTimeout(120);

test('the live view shows the page and sends the pointer where the owner clicked', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await mounted(browser);
    const screen = page.locator('[data-live-screen]');
    await screen.waitFor();
    assert.match(await page.evaluate(() => window.sources[0].url), new RegExp(`^/api/root/agents/agent-1/browser-live/stream\\?tab=${'A'.repeat(32)}&width=\\d+$`));
    assert.deepEqual(await page.locator('[data-live-tabs] option').allTextContents(), ['Inbox', 'shop.example']);
    // Nothing is sent before the stream has named its view and a picture has given the page's size.
    await screen.click({position: {x: 10, y: 10}});
    await settle(page);
    assert.deepEqual(await events(page), []);
    await page.evaluate(jpeg => { window.sources[0].emit('open', {view: 'view-1'}); window.sources[0].emit('frame', {data: jpeg, width: 1280, height: 800}); }, JPEG);
    await page.waitForFunction(() => document.querySelector('[data-live-screen]').naturalWidth === 1);
    // The picture is drawn 640 wide for a 1280-wide page: a click at 100,50 is the page's 200,100.
    const box = await screen.boundingBox();
    assert.deepEqual([box.width, box.height], [640, 400]);
    await page.mouse.click(box.x + 100, box.y + 50);
    await settle(page);
    const sent = await events(page);
    assert.deepEqual(sent.map(item => [item.type, item.action]), [['mouse', 'move'], ['mouse', 'down'], ['mouse', 'up']]);
    assert.deepEqual(sent[1], {type: 'mouse', action: 'down', x: 200, y: 100, buttons: 1, modifiers: 0, button: 'left', clicks: 1});
    assert.deepEqual(await page.evaluate(() => [window.sent[0].url, window.sent[0].body.view]), ['/root/agents/agent-1/browser-live/input', 'view-1']);
    assert.equal(await page.evaluate(() => document.activeElement === document.querySelector('[data-live-screen]')), true, 'a click focuses the page for typing');
  } finally { await browser.close(); }
});

test('keys, shortcuts, paste and the wheel reach the page', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await mounted(browser);
    const screen = page.locator('[data-live-screen]');
    await screen.waitFor();
    await page.evaluate(jpeg => { window.sources[0].emit('open', {view: 'view-1'}); window.sources[0].emit('frame', {data: jpeg, width: 640, height: 400}); }, JPEG);
    await screen.focus();
    await page.keyboard.press('a');
    await page.keyboard.press('Enter');
    await page.keyboard.press('Control+a');
    await page.keyboard.press('ArrowLeft');
    await settle(page);
    const keys = (await events(page)).filter(item => item.type === 'key' && item.action === 'down');
    assert.deepEqual(keys.map(item => [item.key, item.text, item.modifiers]),
      [['a', 'a', 0], ['Enter', '\r', 0], ['Control', '', 2], ['a', '', 2], ['ArrowLeft', '', 0]]);
    assert.equal(keys[0].keyCode, 65);
    assert.equal(keys[4].code, 'ArrowLeft');
    await page.evaluate(() => { window.sent = []; });
    await screen.evaluate(element => {
      const data = new DataTransfer(); data.setData('text/plain', 'pasted ünï');
      element.dispatchEvent(new ClipboardEvent('paste', {clipboardData: data, bubbles: true, cancelable: true}));
      element.dispatchEvent(new CompositionEvent('compositionend', {data: '日本'}));
    });
    const box = await screen.boundingBox();
    await page.mouse.move(box.x + 20, box.y + 20);
    await page.mouse.wheel(0, 240);
    await settle(page);
    const rest = await events(page);
    assert.deepEqual(rest.filter(item => item.type === 'text').map(item => item.text), ['pasted ünï', '日本']);
    assert.deepEqual(rest.filter(item => item.type === 'wheel').map(item => [item.x, item.y, item.dx, item.dy]), [[20, 20, 0, 240]]);
  } finally { await browser.close(); }
});

async function live(browser) {
  const page = await mounted(browser);
  await page.locator('[data-live-screen]').waitFor();
  await page.evaluate(jpeg => { window.sources[0].emit('open', {view: 'view-1'}); window.sources[0].emit('frame', {data: jpeg, width: 640, height: 400}); }, JPEG);
  return page;
}

test('a double and a triple click reach the page as clicks two and three, and a slow or distant click starts over', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await live(browser);
    const box = await page.locator('[data-live-screen]').boundingBox();
    for (let i = 0; i < 4; i++) await page.mouse.click(box.x + 40, box.y + 40);       // down, up x4 within the interval
    await page.mouse.click(box.x + 200, box.y + 200);                                 // far from the last: a first click again
    await settle(page);
    const presses = (await events(page)).filter(item => item.action === 'down' || item.action === 'up');
    assert.deepEqual(presses.filter(item => item.action === 'down').map(item => item.clicks), [1, 2, 3, 1, 1]);
    assert.deepEqual(presses.filter(item => item.action === 'up').map(item => item.clicks), [1, 2, 3, 1, 1], 'a release repeats its press count');
    const counter = await page.evaluate(() => { const count = agliveClickCounter(); const at = (button, timeStamp) => count({button, timeStamp, clientX: 5, clientY: 5});
      return [at(0, 1000), at(2, 1100), at(2, 1200), at(2, 1900)]; });
    assert.deepEqual(counter, [1, 1, 2, 1], 'another button or a pause starts over');
  } finally { await browser.close(); }
});

test('AltGr characters are typed, not sent as Ctrl+Alt shortcuts', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await live(browser);
    const sent = await page.evaluate(() => {
      const event = (key, extra) => ({key, code: 'KeyQ', keyCode: 81, ctrlKey: true, altKey: true, metaKey: false, shiftKey: false, getModifierState: name => name === 'AltGraph', ...extra});
      return [agliveKey(event('@'), 'down'), agliveKey(event('q', {getModifierState: () => false}), 'down'), agliveKey(event('q', {altKey: false, getModifierState: () => false}), 'down')];
    });
    assert.deepEqual(sent.map(item => [item.text, item.modifiers]), [['@', 0], ['', 3], ['', 2]]);
  } finally { await browser.close(); }
});

test('Escape pressed twice quickly leaves the picture, so Tab is the dashboard’s again', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await live(browser);
    const screen = page.locator('[data-live-screen]');
    assert.match(await page.locator('.aglive-note').innerText(), /Esc twice/);
    await screen.focus();
    await page.keyboard.press('Tab');
    assert.equal(await page.evaluate(() => document.activeElement === document.querySelector('[data-live-screen]')), true, 'Tab goes to the page');
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.activeElement === document.querySelector('[data-live-screen]')), true, 'one Escape stays');
    await page.waitForTimeout(700);                                                  // longer than the double-press window
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.activeElement === document.querySelector('[data-live-screen]')), true, 'a slow second Escape is a first one');
    await page.keyboard.press('Escape');
    assert.equal(await page.evaluate(() => document.activeElement === document.querySelector('[data-live-screen]')), false);
    await settle(page);
    const escapes = (await events(page)).filter(item => item.key === 'Escape' && item.action === 'down');
    assert.equal(escapes.length, 2, 'the Escape that leaves is not sent to the page');
  } finally { await browser.close(); }
});

test('the wheel is the dashboard’s until the picture is live, and then only over the picture', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await mounted(browser);
    await page.locator('[data-live-screen]').waitFor();
    const prevented = () => page.evaluate(() => {
      const event = new WheelEvent('wheel', {deltaY: 100, bubbles: true, cancelable: true});
      document.querySelector('[data-live-screen]').dispatchEvent(event);
      return event.defaultPrevented;
    });
    assert.equal(await prevented(), false, 'before the stream is open');
    await page.evaluate(jpeg => { window.sources[0].emit('open', {view: 'view-1'}); window.sources[0].emit('frame', {data: jpeg, width: 640, height: 400}); }, JPEG);
    assert.equal(await prevented(), true);
    const outside = await page.evaluate(() => { const event = new WheelEvent('wheel', {deltaY: 100, bubbles: true, cancelable: true}); document.querySelector('.agbrowser-bar').dispatchEvent(event); return event.defaultPrevented; });
    assert.equal(outside, false);
  } finally { await browser.close(); }
});

test('a view the server refuses says why instead of staying blank', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await mounted(browser);
    await page.locator('[data-live-screen]').waitFor();
    await page.evaluate(() => window.sources[0].emit('refused', {code: 'busy', message: 'Too many browser views are open.'}));
    await page.getByRole('alert').getByText('Too many browser views are open.').waitFor();
    assert.equal(await page.evaluate(() => window.sources[0].closed), true);
  } finally { await browser.close(); }
});

test('when finding the agent’s viewer fails, the error is shown and the live view does not replace it', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const failing of [true, false]) {
      const page = await browser.newPage();
      await page.setContent('<div id="agpad-body"></div>');
      await page.evaluate(failing => {
        window.$ = (selector, root = document) => root.querySelector(selector);
        window.esc = value => String(value ?? '');
        window.agpadActive = () => true;
        window.live = [];
        window.agliveMount = (...args) => window.live.push(args[0]);
        window.api = async () => { if (failing) throw new Error('The service is restarting'); return {agent_id: 'agent-1', configured: false, browsers: [], desktop: null}; };
      }, failing);
      await page.addScriptTag({path: path.join(__dirname, '../js/agent-browser.js')});
      await page.evaluate(() => agbrowserMount({agentId: 'agent-1', workspaceScope: 'global', workspaceRole: 'global-worker', view: 'browser', el: document.body}));
      await page.waitForFunction(() => window.live.length > 0 || document.querySelector('[role="alert"]'), null, {timeout: 3000});
      assert.deepEqual(await page.evaluate(() => window.live), failing ? [] : ['agent-1']);
      if (failing) assert.match(await page.locator('[role="alert"]').innerText(), /Couldn't load/);
    }
  } finally { await browser.close(); }
});

test('many pointer moves become few, and the click after them still arrives in order', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await mounted(browser);
    const screen = page.locator('[data-live-screen]');
    await screen.waitFor();
    await page.evaluate(jpeg => { window.sources[0].emit('open', {view: 'view-1'}); window.sources[0].emit('frame', {data: jpeg, width: 640, height: 400}); }, JPEG);
    const box = await screen.boundingBox();
    await page.mouse.move(box.x + 5, box.y + 5);
    await page.mouse.move(box.x + 300, box.y + 200, {steps: 60});
    await page.mouse.down(); await page.mouse.up();
    await settle(page);
    const sent = await events(page);
    const moves = sent.filter(item => item.action === 'move');
    assert.ok(moves.length < 40, `${moves.length} moves sent for 61`);
    assert.deepEqual(sent.slice(-3).map(item => [item.action, item.x, item.y]), [['move', 300, 200], ['down', 300, 200], ['up', 300, 200]]);
  } finally { await browser.close(); }
});

const inputs = page => page.evaluate(() => window.sent.filter(item => item.url.endsWith('/input')).map(item => item.body));
const reports = page => page.evaluate(() => window.sent.filter(item => item.url.endsWith('/received')).map(item => item.body));
const liveNow = page => page.evaluate(jpeg => { window.sources[0].emit('open', {view: 'view-1'}); window.sources[0].emit('frame', {data: jpeg, width: 640, height: 400}); }, JPEG);
// From here on no request is answered until the test answers it, as over a slow connection.
const unanswered = page => page.evaluate(() => { window.answers = []; window.post = (url, body) => { window.sent.push({url, body}); return new Promise(resolve => window.answers.push(resolve)); }; });

test('the picture is asked for at the width it is drawn, and each one that arrives is reported', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await mounted(browser);
    const failures = [];
    page.on('pageerror', error => failures.push(String(error)));
    await page.locator('[data-live-screen]').waitFor();
    const drawn = await page.evaluate(() => Math.round(document.querySelector('.aglive-stage').clientWidth * devicePixelRatio));
    assert.ok(drawn >= 200, `the stage is ${drawn} wide`);
    assert.equal(await page.evaluate(() => window.sources[0].url), `/api/root/agents/agent-1/browser-live/stream?tab=${'A'.repeat(32)}&width=${drawn}`);
    await page.evaluate(jpeg => {
      window.sources[0].emit('open', {view: 'view-1'});
      window.sources[0].emit('frame', {data: jpeg, width: 1280, height: 800, n: 1});
      window.sources[0].emit('frame', {data: jpeg, width: 1280, height: 800, n: 2});
      window.sources[0].emit('frame', {data: jpeg, width: 1280, height: 800});      // a server from before numbering: shown, not reported
    }, JPEG);
    await settle(page);
    assert.deepEqual(await reports(page), [{view: 'view-1', picture: 1}, {view: 'view-1', picture: 2}]);
    // The picture may come smaller than the page: it is laid out at the page's size all the same.
    assert.deepEqual(await page.evaluate(() => { const screen = document.querySelector('[data-live-screen]'); return [screen.getAttribute('width'), screen.getAttribute('height')]; }), ['1280', '800']);
    // The stage gets narrower: once it has settled the server is told, with the newest picture's number.
    await page.evaluate(() => { document.querySelector('#body').style.width = '500px'; });
    await page.waitForFunction(() => window.sent.some(item => item.body.width));
    const narrow = await page.evaluate(() => Math.round(document.querySelector('.aglive-stage').clientWidth * devicePixelRatio));
    assert.deepEqual((await reports(page)).at(-1), {view: 'view-1', picture: 2, width: narrow});
    assert.ok(narrow < drawn);
    // A report the server refuses (the view just closed) is nobody's error.
    await page.evaluate(jpeg => { window.post = async () => { throw new Error('closed'); }; window.sources[0].emit('frame', {data: jpeg, width: 1280, height: 800, n: 3}); }, JPEG);
    await settle(page);
    assert.deepEqual(failures, []);
  } finally { await browser.close(); }
});

test('a key does not wait for the answer to the one before it, and every request says its place in the order', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await mounted(browser);
    const screen = page.locator('[data-live-screen]');
    await screen.waitFor();
    await liveNow(page);
    await screen.click({position: {x: 10, y: 10}});
    await settle(page);
    await page.evaluate(() => { window.sent = []; });
    await unanswered(page);
    await page.keyboard.type('abcdef', {delay: 25});
    await settle(page);
    // Nothing was answered, yet the keys went out: four requests at once, no more.
    let sent = await inputs(page);
    assert.equal(sent.length, 4);
    assert.equal(sent[0].events[0].key, 'a');
    // Answers come: the rest follows, nothing lost, nothing out of order, and the numbers count on from where they were.
    await page.evaluate(() => { setInterval(() => { while (window.answers.length) window.answers.shift()({}); }, 5); });
    await settle(page);
    sent = await inputs(page);
    assert.deepEqual(sent.flatMap(item => item.events).filter(item => item.action === 'down').map(item => item.key), ['a', 'b', 'c', 'd', 'e', 'f']);
    assert.deepEqual(sent.flatMap(item => item.events).map(item => item.action), Array(6).fill(['down', 'up']).flat());
    const first = sent[0].order;
    assert.deepEqual(sent.map(item => item.order), sent.map((_, index) => first + index));
    assert.ok(sent.every(item => item.view === 'view-1'));
  } finally { await browser.close(); }
});

test('pointer moves alone wait for the answer, and a key behind them does not', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await mounted(browser);
    const screen = page.locator('[data-live-screen]');
    await screen.waitFor();
    await liveNow(page);
    await screen.click({position: {x: 10, y: 10}});
    await settle(page);
    await page.evaluate(() => { window.sent = []; });
    await unanswered(page);
    const box = await screen.boundingBox();
    await page.mouse.move(box.x + 300, box.y + 200, {steps: 40});
    await settle(page);
    await page.mouse.move(box.x + 100, box.y + 100, {steps: 40});
    await settle(page);
    let sent = await inputs(page);
    assert.equal(sent.length, 1, 'only the first moves went out while their answer is awaited');
    await page.keyboard.press('x');
    await settle(page);
    sent = await inputs(page);
    // The key goes at once, behind the one latest pointer position that was waiting.
    assert.deepEqual(sent[1].events.map(item => [item.type, item.action, item.x ?? item.key, item.y ?? '']), [['mouse', 'move', 100, 100], ['key', 'down', 'x', '']]);
    assert.deepEqual(sent.map(item => item.order), sent.map((_, index) => sent[0].order + index));
  } finally { await browser.close(); }
});

test('switching tabs, a closed tab and a broken stream each leave exactly one stream or none', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await mounted(browser);
    await page.locator('[data-live-tabs]').selectOption('B'.repeat(32));
    await page.waitForFunction(() => window.sources.length === 2);
    assert.deepEqual(await page.evaluate(() => [window.sources[0].closed, window.sources[1].closed, window.sources[1].url.includes('tab=' + 'B'.repeat(32))]), [true, false, true]);
    // The stream tells of a tab that appeared; the menu follows.
    await page.evaluate(tabs => window.sources[1].emit('tabs', [...tabs, {id: 'C'.repeat(32), title: 'New one', url: 'about:blank'}]), TABS);
    assert.equal(await page.locator('[data-live-tabs] option').count(), 3);
    assert.equal(await page.locator('[data-live-tabs]').inputValue(), 'B'.repeat(32));
    await page.evaluate(() => window.sources[1].emit('closed', {}));
    await page.getByText('This tab was closed.').waitFor();
    assert.equal(await page.evaluate(() => window.sources[1].closed), true);
    await page.locator('[data-live-reconnect]').click();
    await page.locator('[data-live-screen]').waitFor();
    assert.equal(await page.evaluate(() => window.sources.length), 3);
    await page.evaluate(() => window.sources[2].onerror());
    await page.getByText('The live view stopped.').waitFor();
    assert.equal(await page.evaluate(() => window.sources[2].closed), true, 'the view does not reconnect by itself');
    // Leaving the Browser tab closes whatever is open.
    await page.locator('[data-live-reconnect]').click();
    await page.locator('[data-live-screen]').waitFor();
    await page.evaluate(() => window.disposers.at(-1)());
    assert.equal(await page.evaluate(() => window.sources.every(source => source.closed)), true);
  } finally { await browser.close(); }
});

test('a browser that is off or has no page says so', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const empty = await mounted(browser, []);
    await empty.getByText('This browser has no open page yet.').waitFor();
    assert.equal(await empty.evaluate(() => window.sources.length), 0);
    await empty.evaluate(() => { window.failure = "This agent's browser isn't running"; });
    await empty.locator('[data-live-reconnect]').click();
    await empty.getByRole('alert').getByText("This agent's browser isn't running").waitFor();
  } finally { await browser.close(); }
});

// The Focus pane's launcher row, with the real discovery of agent-browser.js and no viewer configured for the agent.
async function launcher(browser, live) {
  const page = await browser.newPage({viewport: {width: 900, height: 300}});
  await page.setContent('<main id="row"></main>');
  await page.evaluate(live => {
    window.$ = (selector, root = document) => root.querySelector(selector);
    window.esc = value => String(value ?? '');
    window.coreData = value => value;
    window.agfocusDesktopTab = () => {};
    window.T_VIEW_ICON = {notes: '<svg data-icon="journal-feather"></svg>'};
    window.agpadWorkspaceViewsSync = () => {};
    window.requested = [];
    window.agpadRequestView = (_pane, view) => window.requested.push(view);
    window.api = async path => {
      if (path.endsWith('/browser-surfaces')) return {agent_id: 'agent-1', configured: false, browsers: [], desktop: null};
      if (live === 'built-in') return {tabs: []};
      throw Object.assign(new Error('This agent has no browser'), {code: live === 'stopped' ? 'browser_not_ready' : 'not_found'});
    };
  }, live);
  for (const name of ['agent-viewbox.js', 'agent-browser-live.js', 'agent-browser.js']) await page.addScriptTag({path: path.join(__dirname, '../js', name)});
  await page.evaluate(() => {
    const pane = window.pane = {agentId: 'agent-1', workspaceScope: 'global', workspaceRole: 'global-worker'};
    _agpad = pane;
    pane.focusSurfaces = agviewboxSurfacesMount(pane, document.querySelector('#row'));
    agbrowserDiscover(pane);
  });
  return page;
}

test('the Focus launcher opens Browser for an agent with a built-in browser and no configured viewer', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const live of ['built-in', 'stopped']) {
      const page = await launcher(browser, live);
      const chip = page.locator('[data-focus-surface="browser"]');
      await page.waitForFunction(() => document.querySelector('[data-focus-surface="browser"]').getAttribute('aria-disabled') !== 'true', null, {timeout: 3000});
      assert.equal(await chip.getAttribute('aria-label'), 'Browser', live);
      await chip.click();
      assert.deepEqual(await page.evaluate(() => window.requested), ['browser'], live);
      assert.equal(await page.locator('[data-focus-surface="desktop"]').getAttribute('aria-disabled'), 'true', live);
    }
    const none = await launcher(browser, 'none');
    await none.waitForFunction(() => document.querySelector('[data-focus-surface="browser"]').title.includes('no configured browser'));
    await none.locator('[data-focus-surface="browser"]').click({force: true});
    assert.deepEqual(await none.evaluate(() => window.requested), []);
  } finally { await browser.close(); }
});

test('the feather left of Browser carries the Journal icon and returns to the notes', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const page = await launcher(browser, 'built-in');
    assert.deepEqual(await page.evaluate(() => {
      const chips = [...document.querySelectorAll('.agfocus-fixed > button')];
      const notes = chips[0];
      return [notes.hasAttribute('data-focus-notes'), chips[1].dataset.focusSurface, notes.getAttribute('aria-label'), notes.querySelector('svg').dataset.icon, notes.hasAttribute('aria-disabled')];
    }), [true, 'browser', 'Notes', 'journal-feather', false]);
    await page.evaluate(() => {
      window.steps = [];
      pane.surface = {kind: 'browser'};
      let record = true;
      pane.focusWorkspace = {isOpen: () => record, close: async () => { steps.push('record'); record = false; }};
      window.agpadSurfaceClose = async p => { steps.push('surface'); p.surface = null; };
      window.focusText = {focus: p => steps.push(p === pane ? 'notes' : 'other')};
    });
    await page.locator('[data-focus-notes]').click();
    await page.waitForFunction(() => steps.includes('notes'));
    assert.deepEqual(await page.evaluate(() => steps), ['surface', 'record', 'notes']);
    // An open item that refuses to close keeps the board; the notes do not take the caret.
    await page.evaluate(() => { steps.length = 0; pane.surface = {kind: 'expansion'}; window.agpadSurfaceClose = async () => { steps.push('refused'); return false; }; });
    await page.locator('[data-focus-notes]').click();
    await page.waitForFunction(() => steps.includes('refused'));
    assert.deepEqual(await page.evaluate(() => steps), ['refused']);
    assert.deepEqual(await page.evaluate(() => window.requested), []);
  } finally { await browser.close(); }
});
