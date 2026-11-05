#!/usr/bin/env node
// Isolated cross-origin fixture for Guacamole focus and clipboard controls.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const root = path.resolve(__dirname, '..');
const focusScript = fs.readFileSync(path.join(root, 'tools/guacamole-click-focus/click-focus.js'), 'utf8');
const clipboardScript = fs.readFileSync(path.join(root, 'tools/guacamole-click-focus/click-clipboard.js'), 'utf8');
const clipboardCss = fs.readFileSync(path.join(root, 'tools/guacamole-click-focus/click-clipboard.css'), 'utf8');

function listen(server) {
  return new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));
}

(async () => {
  const child = http.createServer((req, res) => {
    if (req.url !== '/') { res.statusCode = 404; return res.end(); }
    res.setHeader('Content-Type', 'text/html');
    res.end(`<!doctype html><meta charset="utf-8"><style>
      .client-view { position: absolute; top: 0; left: 0; width: 100%; height: 100%; }
      .client-view-content { display: flex; flex-direction: column; width: 100%; height: 100%; }
      .client-body { flex: 1 1 auto; position: relative; }
      .client-tile, .client-main, .displayOuter, .displayMiddle, .display { width: 100%; height: 100%; }
      .display { display: inline-block; }
      ${clipboardCss}
    </style>
      <div class="client-view"><div class="client-view-content"><div class="client-body"><div id="tile" class="client-tile"><div class="client-main main"><div class="display"><canvas id="screen" width="480" height="240"></canvas></div></div></div></div></div></div>
      <script>
        Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: () => Promise.reject(new DOMException('denied', 'NotAllowedError')) } });
        const angularModule = { run(definition) { window.clipboardRun = definition; } };
        window.angular = { module() { return angularModule; } };
      </script>
      <script>${focusScript}</script>
      <script>${clipboardScript}</script>
      <script>
        const listeners = Object.create(null);
        const childListeners = Object.create(null);
        const sent = [];
        const remoteKeys = [];
        const remoteInputs = [];
        const remoteClipboardEvents = [];
        const rootScope = { $on(name, listener) { (listeners[name] ||= []).push(listener); } };
        const childScope = {
          $on(name, listener) { (childListeners[name] ||= []).push(listener); },
          $broadcast(name, ...args) { (childListeners[name] || []).forEach(listener => listener(...args)); }
        };
        const ClipboardData = function(data) { Object.assign(this, data); };
        const clipboardService = { setClipboard(data) { sent.push(data); return Promise.resolve(); } };
        const injector = { get(name) {
          if (name === 'clipboardService') return clipboardService;
          if (name === 'ClipboardData') return ClipboardData;
          throw new Error('unexpected service ' + name);
        } };
        const run = window.clipboardRun;
        run[run.length - 1](rootScope, injector);
        function emit(name, ...args) { (listeners[name] || []).forEach(listener => listener(...args)); }
        const sink = document.createElement('textarea');
        sink.id = 'guac-input-sink';
        sink.style.cssText = 'position:fixed;left:0;bottom:0;width:0;height:0;opacity:0;';
        document.body.appendChild(sink);
        function focusRemote() { sink.focus(); }
        function guacKeyHook(name, key, target) {
          const event = { defaultPrevented: false, preventDefault() { this.defaultPrevented = true; } };
          childScope.$broadcast(name, event, key);
          if (!event.defaultPrevented) remoteKeys.push(key);
        }
        window.clipboardFixture = { emit, sent, remoteKeys, remoteInputs, remoteClipboardEvents, sink, guacKeyHook };
        document.addEventListener('keydown', event => guacKeyHook('guacBeforeKeydown', event.key, event.target), true);
        document.addEventListener('keyup', event => guacKeyHook('guacBeforeKeyup', event.key, event.target), true);
        document.addEventListener('input', event => remoteInputs.push(event.data || event.target.value), true);
        document.addEventListener('paste', () => remoteClipboardEvents.push('paste'), true);
        document.querySelector('#tile').addEventListener('click', () => focusRemote());
        document.querySelector('#screen').addEventListener('mousedown', event => event.preventDefault());
        document.querySelector('#screen').addEventListener('click', () => {
          document.querySelector('#clicks').textContent = String(Number(document.querySelector('#clicks').textContent) + 1);
        });
        window.addEventListener('keydown', event => { if (event.target === sink) document.querySelector('#events').textContent += event.key; });
      </script>
      <output id="events"></output><output id="clicks">0</output>`);
  });
  const childPort = await listen(child);
  const parent = http.createServer((req, res) => {
    if (req.url !== '/') { res.statusCode = 404; return res.end(); }
    res.setHeader('Content-Type', 'text/html');
    res.end(`<!doctype html><meta charset="utf-8">
      <input id="main" aria-label="main"><iframe id="desktop" src="http://127.0.0.1:${childPort}/"></iframe>
      <input id="bottom" aria-label="bottom">`);
  });
  const parentPort = await listen(parent);
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 900, height: 700 } });
    await page.goto(`http://127.0.0.1:${parentPort}/`);
    const frame = page.frameLocator('#desktop');
    const canvas = frame.locator('#screen');

    await page.locator('#main').fill('main input');
    await canvas.boundingBox().then(box => page.mouse.click(box.x + 120, box.y + 80));
    assert.equal(await page.evaluate(() => document.activeElement.id), 'desktop', 'display click focuses cross-origin iframe');
    await page.keyboard.press('d');
    assert.equal(await frame.locator('#events').textContent(), 'd', 'desktop key reaches child');
    assert.equal(await frame.locator('#clicks').textContent(), '1', 'desktop click remains delivered');

    await page.locator('#bottom').fill('bottom input');
    await page.locator('#main').fill('main input updated');
    await canvas.boundingBox().then(box => page.mouse.click(box.x + 160, box.y + 90));
    await page.keyboard.press('t');
    assert.equal(await frame.locator('#events').textContent(), 'dt', 'desktop key returns after parent inputs');
    assert.equal(await frame.locator('#clicks').textContent(), '2', 'second desktop click remains delivered');

    await frame.locator('.qorqut-clipboard-toggle').click();
    const field = frame.locator('#qorqut-clipboard-text');
    await frame.locator('body').evaluate(() => {
      window.clipboardFixture.remoteKeys.length = 0;
      window.clipboardFixture.remoteInputs.length = 0;
    });
    await field.click();
    assert.equal(await frame.locator('body').evaluate(() => document.activeElement.id), 'qorqut-clipboard-text', 'clipboard field click keeps local focus');
    await field.press('x');
    assert.match(await field.inputValue(), /x$/, 'clipboard field accepts local keyboard input');
    await frame.locator('body').evaluate(() => document.querySelector('#qorqut-clipboard-text').dispatchEvent(new ClipboardEvent('paste', { bubbles: true, cancelable: true })));
    assert.deepEqual(await frame.locator('body').evaluate(() => window.clipboardFixture.remoteClipboardEvents), [], 'clipboard paste stays local');
    await field.fill('local clipboard text');
    assert.equal(await frame.locator('body').evaluate(() => window.clipboardFixture.remoteKeys.length), 0, 'clipboard typing never sends Guacamole key events');
    assert.equal(await frame.locator('body').evaluate(() => window.clipboardFixture.remoteInputs.length), 0, 'clipboard input never reaches the remote sink');
    await frame.locator('[data-qorqut-send]').click();
    await frame.locator('.qorqut-clipboard-status').waitFor({ state: 'visible' });
    assert.deepEqual(await frame.locator('body').evaluate(() => window.clipboardFixture.sent.map(item => ({ type: item.type, data: item.data, source: item.source }))),
      [{ type: 'text/plain', data: 'local clipboard text', source: null }], 'Send to desktop uses native clipboard service');
    assert.equal(await frame.locator('body').evaluate(() => window.clipboardFixture.remoteKeys.length), 0, 'clipboard send does not emit key events');

    await field.evaluate(element => element.blur());
    await frame.locator('body').evaluate(() => window.clipboardFixture.emit('guacClipboard', {}, { source: 'remote-client', type: 'text/plain', data: 'remote clipboard text' }));
    assert.equal(await field.inputValue(), 'remote clipboard text', 'remote text populates the explicit field');
    await frame.locator('[data-qorqut-copy]').click();
    await frame.locator('.qorqut-clipboard-status').waitFor({ state: 'visible' });
    assert.match(await frame.locator('.qorqut-clipboard-status').textContent(), /access was denied|selected/);
    assert.equal(await frame.locator('body').evaluate(() => document.activeElement.id), 'qorqut-clipboard-text', 'manual copy fallback keeps field focused');
    assert.deepEqual(await frame.locator('body').evaluate(() => [document.activeElement.selectionStart, document.activeElement.selectionEnd]), [0, 'remote clipboard text'.length], 'manual copy fallback keeps selection');

    await frame.locator('body').evaluate(() => window.clipboardFixture.emit('guacClientFocused', {}));
    await frame.locator('[data-qorqut-copy]').click();
    assert.equal(await frame.locator('.qorqut-clipboard-status').textContent(), 'No remote text is available yet.', 'client changes clear cached remote text');
    await frame.locator('body').evaluate(() => window.clipboardFixture.emit('guacClipboard', {}, { source: 'route-client', type: 'text/plain', data: 'route text' }));
    await frame.locator('body').evaluate(() => window.clipboardFixture.emit('$routeChangeStart', {}));
    assert.equal(await frame.locator('[data-qorqut-clipboard]').count(), 0, 'route start removes toolbar and clears state');
    await frame.locator('body').evaluate(() => window.clipboardFixture.emit('$routeChangeSuccess', {}));
    assert.equal(await frame.locator('[data-qorqut-clipboard]').count(), 1, 'route success remounts toolbar');
    await frame.locator('.qorqut-clipboard-toggle').click();
    await frame.locator('[data-qorqut-copy]').click();
    assert.equal(await frame.locator('.qorqut-clipboard-status').textContent(), 'No remote text is available yet.', 'route remount does not reuse old remote text');

    console.log('Guacamole clipboard/focus fixture passed: cross-origin focus, native send, guarded keys, manual copy fallback, client reset, and route remount');
  } finally {
    await browser.close();
    parent.close();
    child.close();
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
