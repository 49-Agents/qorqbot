'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

const fixture = path.join(__dirname, 'fixtures/chat-tab-reference.html');
const chrome = ['google-chrome', 'chromium', 'chromium-browser'].find(bin => spawnSync(bin, ['--version']).status === 0);

// The real chat-view.js, chat-view.css, app.css and theme-light.css in headless Chrome; the dashboard globals are stubs.
function run(hash, extra = []) {
  const result = spawnSync(chrome, ['--headless=new', '--no-sandbox', '--disable-gpu', '--allow-file-access-from-files', '--window-size=480,300',
    '--virtual-time-budget=5000', ...extra, 'file://' + fixture + hash], {encoding: 'utf8', maxBuffer: 1 << 26});
  return result.stdout;
}
const results = hash => {
  const dom = run(hash, ['--dump-dom']);
  const raw = /<pre id="out"[^>]*>(.*?)<\/pre>/s.exec(dom)[1].replace(/&quot;/g, '"').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
  return JSON.parse(raw);
};
const AGENT = '3f6c1b9e-2a47-4d1e-9b0c-5e8a7d2c4f10', TAB = 'A1B2C3D4E5F60718293A4B5C6D7E8F90';

for (const theme of ['', '#light']) {
  test(`a tab reference is a button with the page title and the agent, in the ${theme ? 'light' : 'dark'} theme`, {skip: !chrome && 'Chrome not installed'}, () => {
    const out = results(theme);
    assert.equal(out.look.light, !!theme);
    assert.equal(out.available.tag, 'BUTTON');
    assert.equal(out.available.title, 'Inbox (3) - Mail');
    assert.equal(out.available.agent, 'Ordubashy');
    assert.equal(out.available.aria, 'Open tab: Inbox (3) - Mail — Ordubashy');
    assert.equal(out.available.svg, true);
    // The page's global filled pill button is reset: a compact chip, not a 36px button.
    assert.ok(out.available.height < 32, `height ${out.available.height}`);
    assert.deepEqual(out.look, {cursor: 'pointer', weight: '400', radius: '6px', light: !!theme});
    // Clicking asks the dashboard for its own live view of exactly this agent and tab.
    assert.deepEqual(out.opened, [[AGENT, TAB]]);
    // A title or agent name that looks like markup is only text, in the label and in the accessible name.
    assert.deepEqual(out.escaped, {same: true, elements: 0, pwned: false, title: true, agent: true, aria: true});
    // A new title or agent name changes the same button; a blank title and no agent are still a usable chip.
    assert.deepEqual(out.renamed, {same: true, count: 1, title: 'Renamed page', agent: 'Worker', aria: 'Open tab: Renamed page — Worker'});
    assert.deepEqual(out.bare, {same: true, title: 'Untitled page', agent: null, aria: 'Open tab: Untitled page'});
  });
}

test('an unavailable or malformed tab reference is the unavailable placeholder, and only a valid one can be clicked', {skip: !chrome && 'Chrome not installed'}, () => {
  const out = results('');
  const placeholder = {count: 1, tag: 'SPAN', text: 'Unavailable reference', unavailable: true, buttons: 0};
  // Available -> unavailable replaces the button; unavailable -> available brings a button back.
  assert.deepEqual(out.flipped, {...placeholder, oldButtonGone: true});
  assert.deepEqual(out.restored, {count: 1, tag: 'BUTTON', title: 'Back again'});
  // Bad ids (wrong case or length, arrays, missing) never reach the click.
  assert.equal(out.malformed.length, 9);
  for (const item of out.malformed) assert.deepEqual(item, placeholder);
  assert.equal(out.openedAfterMalformed, 1);
  // A page without the entry point, or an entry point that fails, leaves the click harmless.
  assert.deepEqual(out.noEntryPoint, {errors: 0, opened: 1});
  assert.deepEqual(out.rejecting, {errors: 0});
});

test('a long page title is clipped to one line inside a narrow column and the agent stays visible', {skip: !chrome && 'Chrome not installed'}, () => {
  assert.deepEqual(results('').narrow, {fits: true, oneLine: true, clipped: true, ellipsis: true, agentVisible: true});
});

test('tab reference screenshots', {skip: !chrome && 'Chrome not installed'}, () => {
  const dir = process.env.CHAT_TAB_SCREENSHOT_DIR;
  if (!dir) return;
  fs.mkdirSync(dir, {recursive: true});
  for (const [name, hash] of [['dark', '#shot'], ['light', '#shot-light']]) {
    for (const width of [480, 260]) {
      const file = path.join(dir, `tab-chip-${name}-${width}.png`);
      run(hash, [`--screenshot=${file}`, `--window-size=${width},230`]);
      assert.ok(fs.existsSync(file));
    }
  }
});
