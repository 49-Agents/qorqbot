'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Browser checks are optional. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const browserTest = (name, fn) => test(name, {skip: (!chromium || !fs.existsSync(executable)) && 'Local Playwright Chromium is unavailable'}, fn);
const root = path.join(__dirname, '..');

browserTest('retry buttons at the end of a full Focus attention row keep it one chip high', async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage({viewport: {width: 700, height: 700}});
    const chips = Array.from({length: 20}, () => '<button type="button" class="focus-signoff-chip"><span class="attention-signoff"><svg viewBox="0 0 24 24"><path d="M3 5h18v14H3z"/></svg></span></button>').join('');
    const retry = ['signoffs', 'reminders', 'calendar'].map(name => `<button type="button" class="quiet" data-${name}-retry>Retry ${name}</button>`).join('');
    await page.setContent(`<body class="agent-workspace-app"><div class="agfocus" style="width:600px"><section class="attention-queue agfocus-queues">
      <div class="agfocus-surface-board focus-signoff-queue agfocus-attention-row" role="group">${chips}${retry}</div></section></div></body>`);
    for (const file of ['app.css', 'css/agent-focus.css', 'css/agent-workspace.css']) await page.addStyleTag({path: path.join(root, file)});
    const sizes = await page.evaluate(() => {
      const row = document.querySelector('.agfocus-attention-row'), chip = row.querySelector('.focus-signoff-chip');
      return {row: row.getBoundingClientRect().height, chip: chip.getBoundingClientRect().height, overflow: row.scrollWidth > row.clientWidth,
        retry: [...row.querySelectorAll('.quiet')].map(b => ({height: b.getBoundingClientRect().height, width: b.getBoundingClientRect().width, text: b.scrollWidth <= b.clientWidth + 1}))};
    });
    assert.equal(sizes.overflow, true);
    // The row may add the room a horizontal scrollbar takes, never a button's wrapped text.
    assert.ok(sizes.row <= sizes.chip + 20, `row ${sizes.row}px for ${sizes.chip}px chips`);
    for (const button of sizes.retry) {
      assert.ok(button.height <= sizes.chip, `retry button ${button.height}px tall`);
      assert.ok(button.width > 60 && button.text, 'retry text stays on one line at full width');
    }
  } finally { await browser.close(); }
});
