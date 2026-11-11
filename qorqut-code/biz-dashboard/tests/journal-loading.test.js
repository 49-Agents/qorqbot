'use strict';
// PLAYWRIGHT_MODULE=/path/to/playwright node --test tests/journal-loading.test.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {test, before, after} = require('node:test');
let chromium;
try { ({chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const browserOnly = {skip: !available && 'Local Playwright Chromium is unavailable'};
const dashboard = path.resolve(__dirname, '..');
let browser;
before(async () => { if (available) browser = await chromium.launch({headless: true, executablePath: executable}); });
after(async () => { await browser?.close(); });

const note = (id = 'note-a', title = 'A quiet morning') => ({id, title, content: 'A thought to keep.', revision: 1,
  content_hash: 'original', date: '2026-11-10', createdAt: '2026-11-10T12:00:00Z', updatedAt: '2026-11-10T12:00:00Z',
  pinned: false, statuses: {}, filerSubmissions: []});

async function fixture(t) {
  const page = await browser.newPage({viewport: {width: 1000, height: 720}});
  t.after(() => page.close());
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  t.after(() => assert.deepEqual(errors, []));
  await page.route('http://journal.test/**', route => route.fulfill({contentType: 'text/html', body:
    '<div class="agent-journal-host" id="host"></div>'}));
  await page.goto('http://journal.test/');
  await page.addStyleTag({content: ':root{--fg:#eee;--fg-2:#aaa;--fg-3:#777;--line:#333;--sans:Arial;--mono:monospace;--accent:#aaa;--surface:#161616}body{margin:0;background:#161616;color:var(--fg)}#host{height:100vh}'});
  for (const file of ['css/writing.css', 'css/notes.css']) await page.addStyleTag({path: path.join(dashboard, file)});
  await page.addScriptTag({content: `
    const state = {scope:'global', session:{is_owner:true}, bizs:[{slug:'project',id:'project-id'}]};
    let tView = 'globalNotes';
    const stored = new Map(), requests = [], notices = [];
    const ls = (k,v) => v === undefined ? stored.get(k) : stored.set(k,v);
    const api = url => new Promise((resolve,reject) => requests.push({url,resolve,reject}));
    const post = async () => { throw new Error('No writes expected'); };
    const toast = message => notices.push(message), onChange = () => {};
    const T_DAY=86400000, SJ_ZONE='UTC', tToday=()=>Date.parse('2026-11-10');
    const sjToday=()=> '2026-11-10', sjDate=s=>s.slice(0,10);
    const esc = s => String(s ?? '').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
    const ICO_PLUS='+', ICO_UPLOAD='', SVG_TINBOX='', aicon=()=>'', agentGlyph=()=>'';
    const srcActive=()=>false, srcFilingLine=()=>'', workRelatedChips=()=>{};
    window.openJournal = (scope='/root') => { window.mount=journalMount(document.querySelector('#host'),{scope}); };
    window.finish = (index, rows) => requests[index].resolve(rows);
    window.fail = index => requests[index].reject(new TypeError('Network interrupted'));
  `});
  for (const file of ['js/writing.js', 'js/notes.js']) await page.addScriptTag({path: path.join(dashboard, file)});
  return page;
}

test('cold load animates, returns immediately from cache, and keeps an unchanged editor intact', browserOnly, async t => {
  const page = await fixture(t);
  const notes = [note(), ...Array.from({length:83}, (_,i) => ({...note(`note-${i}`, `Journal entry ${i}`), content:'A thought to keep. '.repeat(350)}))];
  await page.evaluate(() => openJournal());
  assert.equal(await page.locator('[role="status"]').innerText(), 'Loading notes…');
  assert.equal(await page.locator('#tbody').getAttribute('aria-busy'), 'true');
  assert.equal(await page.locator('.nt-loading-dot').evaluate(el => getComputedStyle(el).animationName), 'nt-loading-breathe');
  if (process.env.JOURNAL_SCREENSHOT) await page.screenshot({path: process.env.JOURNAL_SCREENSHOT});
  await page.evaluate(rows => finish(0, rows), notes);
  await page.waitForSelector('.jrcard');
  await page.evaluate(() => mount.select('note-a'));
  await page.waitForSelector('#ntdoc');
  const reopenedMs = await page.evaluate(() => {
    mount.dispose(); const start = performance.now(); openJournal();
    if (!document.querySelector('#ntdoc')) throw new Error('Reopen waited for the network');
    window.existingEditor = document.querySelector('#ntdoc');
    return performance.now() - start;
  });
  console.log(`Journal cached reopen: ${reopenedMs.toFixed(1)} ms`);
  assert.equal(await page.locator('.nt-loading').count(), 0);
  await page.evaluate(rows => finish(1, rows), notes);
  assert.equal(await page.evaluate(() => existingEditor === document.querySelector('#ntdoc')), true);
});

test('refresh cannot overwrite a draft typed into the cached note', browserOnly, async t => {
  const page = await fixture(t);
  await page.evaluate(() => { ls('nt:/root','note-a'); openJournal(); });
  await page.evaluate(rows => finish(0,rows), [note()]);
  await page.waitForSelector('#ntdoc');
  await page.evaluate(() => { mount.dispose(); openJournal(); });
  await page.locator('#ntdoc').fill('My unsaved thought');
  await page.evaluate(rows => finish(1,rows), [{...note(), content:'A newer remote version', revision:2}]);
  assert.equal(await page.locator('#ntdoc').innerText(), 'My unsaved thought');
  assert.equal(await page.evaluate(() => mount.dirty()), true);
});

test('retired requests and other project scopes cannot populate the current Journal', browserOnly, async t => {
  const page = await fixture(t);
  await page.evaluate(() => { openJournal(); mount.dispose(); openJournal('project'); });
  await page.evaluate(rows => finish(0,rows), [note()]);
  assert.equal(await page.locator('.jrcard').count(), 0);
  await page.evaluate(rows => finish(1,rows), [note('project-note','Project plan')]);
  await page.waitForSelector('.jrcard');
  await page.evaluate(() => { mount.dispose(); openJournal(); });
  assert.equal(await page.locator('.jrcard').count(), 0);
  assert.equal(await page.locator('.nt-loading').count(), 1);
  await page.evaluate(rows => finish(2,rows), [note()]);
  await page.waitForSelector('.jrcard');
  assert.equal(await page.getByText('Project plan', {exact:true}).count(), 0);
});

test('failed cold requests offer a retry; failed refreshes retain existing notes', browserOnly, async t => {
  const page = await fixture(t);
  await page.evaluate(() => { openJournal(); fail(0); });
  await page.getByRole('button', {name:'Try again'}).waitFor();
  assert.equal(await page.locator('#tbody').getAttribute('aria-busy'), null);
  await page.getByRole('button', {name:'Try again'}).click();
  await page.evaluate(rows => finish(1,rows), [note()]);
  await page.waitForSelector('.jrcard');
  await page.evaluate(() => { mount.dispose(); openJournal(); fail(2); });
  assert.ok(await page.locator('.jrcard').count() > 0);
  assert.equal(await page.locator('.nt-load-error').count(), 0);
  assert.equal(await page.evaluate(() => notices.at(-1)), "Couldn't refresh notes. Try again.");
});

test('loading respects reduced motion and fits a narrow pane', browserOnly, async t => {
  const page = await fixture(t);
  await page.setViewportSize({width: 360,height:640});
  await page.emulateMedia({reducedMotion:'reduce'});
  await page.evaluate(() => openJournal());
  assert.equal(await page.locator('.nt-loading-dot').evaluate(el => getComputedStyle(el).animationName), 'none');
  assert.equal(await page.locator('.nt-loading-paper').evaluate(el => getComputedStyle(el,'::after').display), 'none');
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
});

test('a denied refresh clears cached notes and a missing project offers retry', browserOnly, async t => {
  const page = await fixture(t);
  await page.evaluate(() => openJournal());
  await page.evaluate(rows => finish(0,rows), [note()]);
  await page.waitForSelector('.jrcard');
  await page.evaluate(() => { mount.dispose(); openJournal(); finish(1,{error:'Access denied',httpStatus:403}); });
  await page.getByRole('button', {name:'Try again'}).waitFor();
  assert.equal(await page.locator('.jrcard').count(), 0);
  await page.evaluate(() => { mount.dispose(); openJournal('missing-project'); });
  await page.getByRole('button', {name:'Try again'}).waitFor();
  assert.equal(await page.locator('.nt-loading').count(), 0);
});
