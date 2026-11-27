'use strict';
// Ordubashy's Focus top area is one row: Notes, Browser, Desktop, Calendar (only while a calendar is connected), a thin divider,
// then the scrolling attention queue.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Browser checks are optional. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const browserTest = (name, fn) => test(name, {skip: (!chromium || !fs.existsSync(executable)) && 'Local Playwright Chromium is unavailable'}, fn);
const root = path.join(__dirname, '..');
// The strip's page: its styles, the few globals it needs and, unless `linked` is undefined, a calendar connection state
// (js/global-calendar.js) whose answer `window.calendarLinked` holds.
async function stripPage(page, linked) {
  for (const file of ['app.css', 'css/agent-focus.css', 'css/agent-workspace.css']) await page.addStyleTag({path: path.join(root, file)});
  const icon = '<svg viewBox="0 0 24 24"><path d="M1 1h9"/></svg>';
  await page.evaluate(({icon, linked}) => {
    window.$ = (s, h = document) => h.querySelector(s);
    window.T_VIEW_ICON = {notes: icon, browser: icon, desktop: icon};
    window.agfocusDesktopTab = () => {};
    if (linked !== undefined) { window.calendarLinked = linked; window.calendarConnection = {connected: () => window.calendarLinked}; }
  }, {icon, linked});
  await page.addScriptTag({path: path.join(root, 'js/agent-viewbox.js')});
}

browserTest('the Focus strip is one row with the queue after a divider that shows only while the queue does', async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage({viewport: {width: 700, height: 700}});
    await page.setContent('<body class="agent-workspace-app"><div class="agfocus" style="width:600px"><section class="attention-queue" id="queue"></section></div></body>');
    await stripPage(page, true);
    const result = await page.evaluate(() => {
      const host = document.getElementById('queue');
      const strip = agviewboxSurfacesMount({agentId: 'a', workspaceScope: 'project', workspaceRole: 'ordubashy', views: ['focus', 'calendar']}, host);
      const stack = host.querySelector('.agfocus-surfaces'), queue = strip.signoffHost, divider = host.querySelector('.agfocus-divider');
      const shown = el => getComputedStyle(el).display !== 'none';
      const out = {
        stackChildren: [...stack.children].map(el => el.className.split(' ')[0]),
        queueIsHost: queue.parentElement === stack && queue.previousElementSibling === divider,
        newButton: !!host.querySelector('[data-side-chat-new]'), oldQueue: !!host.querySelector('.agfocus-side-chat-queue,.agfocus-attention-row'),
        fixed: [...host.querySelectorAll('.agfocus-fixed > button')].map(b => b.textContent.trim()),
        calendarOn: host.querySelector('[data-focus-surface="calendar"]').getAttribute('aria-disabled') !== 'true',
        dividerWhenEmpty: shown(divider),
      };
      queue.innerHTML = Array.from({length: 30}, () => '<button type="button" class="focus-signoff-chip"><svg viewBox="0 0 24 24"><path d="M3 5h18v14H3z"/></svg></button>').join('');
      queue.hidden = false;
      const row = host.querySelector('.agfocus-fixed').getBoundingClientRect(), q = queue.getBoundingClientRect(), d = divider.getBoundingClientRect();
      return {...out, dividerWhenFilled: shown(divider), sameRow: Math.abs((row.top + row.bottom) / 2 - (q.top + q.bottom) / 2) < 8 && d.left >= row.right && q.left >= d.right,
        scrolls: queue.scrollWidth > queue.clientWidth, stackHeight: stack.getBoundingClientRect().height, fixedWidth: row.width};
    });
    assert.deepEqual(result.stackChildren, ['agfocus-fixed', 'agfocus-divider', 'agfocus-surface-board']);
    assert.equal(result.queueIsHost, true);
    assert.equal(result.newButton, false); assert.equal(result.oldQueue, false);
    assert.deepEqual(result.fixed, ['Notes', 'Browser', 'Desktop', 'Calendar']);
    assert.equal(result.calendarOn, true, 'Calendar opens without any browser discovery');
    assert.equal(result.dividerWhenEmpty, false);
    assert.equal(result.dividerWhenFilled, true);
    assert.equal(result.sameRow, true);
    assert.equal(result.scrolls, true, 'a queue that does not fit scrolls sideways');
    assert.ok(result.stackHeight < 80, `one row, not two (${result.stackHeight}px)`);
    assert.ok(result.fixedWidth > 120, 'the launchers keep their width');
  } finally { await browser.close(); }
});

browserTest('the Calendar launcher shows only while a calendar is connected, and is hidden while that is unknown', async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    for (const linked of [undefined, false]) {
      const page = await browser.newPage({viewport: {width: 700, height: 700}});
      await page.setContent('<body class="agent-workspace-app"><div class="agfocus" style="width:600px"><section class="attention-queue" id="queue"></section></div></body>');
      await stripPage(page, linked);
      const label = linked === undefined ? 'unknown' : 'none';
      const read = () => page.evaluate(() => {
        const host = document.getElementById('queue'), chip = host.querySelector('[data-focus-surface="calendar"]');
        return {visible: [...host.querySelectorAll('.agfocus-fixed > button')].filter(b => getComputedStyle(b).display !== 'none').map(b => b.textContent.trim()),
          enabled: chip.getAttribute('aria-disabled') !== 'true', group: host.querySelector('.agfocus-fixed').getAttribute('aria-label'), host: host.getAttribute('aria-label')};
      });
      await page.evaluate(() => {
        // Before the check has answered the pane has no Calendar view (agentPaneViewAllowed), so `views` lacks it.
        window.pane = {agentId: 'a', workspaceScope: 'project', workspaceRole: 'ordubashy', views: ['focus']};
        window.strip = agviewboxSurfacesMount(window.pane, document.getElementById('queue'));
      });
      assert.deepEqual(await read(), {visible: ['Notes', 'Browser', 'Desktop'], enabled: false, group: 'Notes, Browser and Desktop',
        host: 'Notes, Browser, Desktop and attention queue'}, label);
      if (linked === false) {
        // Connected later: the pane adds the view and repaints the strip, so the launcher appears and opens the Calendar.
        await page.evaluate(() => { window.calendarLinked = true; window.pane.views.push('calendar'); window.strip.paint(); });
        assert.deepEqual(await read(), {visible: ['Notes', 'Browser', 'Desktop', 'Calendar'], enabled: true, group: 'Notes, Browser, Desktop and Calendar',
          host: 'Notes, Browser, Desktop, Calendar and attention queue'});
        // And it goes again when the list says there is none.
        await page.evaluate(() => { window.calendarLinked = false; window.pane.views.pop(); window.strip.paint(); });
        assert.deepEqual((await read()).visible, ['Notes', 'Browser', 'Desktop']);
      }
      await page.close();
    }
  } finally { await browser.close(); }
});
