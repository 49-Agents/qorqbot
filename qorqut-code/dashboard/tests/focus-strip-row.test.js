'use strict';
// Ordubashy's Focus top area is one row: Notes, Browser, Desktop, Calendar, a thin divider, then the scrolling attention queue.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Browser checks are optional. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const browserTest = (name, fn) => test(name, {skip: (!chromium || !fs.existsSync(executable)) && 'Local Playwright Chromium is unavailable'}, fn);
const root = path.join(__dirname, '..');

browserTest('the Focus strip is one row with the queue after a divider that shows only while the queue does', async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage({viewport: {width: 700, height: 700}});
    await page.setContent('<body class="agent-workspace-app"><div class="agfocus" style="width:600px"><section class="attention-queue" id="queue"></section></div></body>');
    for (const file of ['app.css', 'css/agent-focus.css', 'css/agent-workspace.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.addScriptTag({content: "window.$=(s,h=document)=>h.querySelector(s);window.T_VIEW_ICON={notes:'<svg viewBox=\"0 0 24 24\"><path d=\"M1 1h9\"/></svg>',browser:'<svg viewBox=\"0 0 24 24\"><path d=\"M1 1h9\"/></svg>',desktop:'<svg viewBox=\"0 0 24 24\"><path d=\"M1 1h9\"/></svg>'};window.agfocusDesktopTab=()=>{};"});
    await page.addScriptTag({path: path.join(root, 'js/agent-viewbox.js')});
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
