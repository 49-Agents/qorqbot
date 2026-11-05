#!/usr/bin/env node
// Isolated divider layout and pointer fixture; no dashboard API, terminals, or agent writes.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const server = http.createServer((req, res) => {
  if (req.url === '/') {
    res.setHeader('Content-Type', 'text/html');
    return res.end(`<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width">
      <link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/agentpane.css">
      <style>html,body{margin:0;overflow:hidden}body{display:block}.fixture{width:100%;height:600px}.listcol,.chat,.agpad{min-height:0}.listcol{background:#101820}.chat{background:#12202c}.agpad{background:#1c2c38}.agent-pane-divider{height:600px}</style>
      <div class="split global-agents agsplit fixture"><div class="listcol"></div>
        <section class="global-agent-session" aria-label="Agent session"><div class="chat termmode"></div></section>
        <div class="agent-pane-divider" data-agent-pane-divider role="separator" aria-label="Terminal and agent pane divider" aria-orientation="vertical" tabindex="0"></div>
        <aside class="tasks agpad" aria-label="Agent pane"></aside></div>
      <script>const ls=(key,value)=>value===undefined?localStorage.getItem(key):localStorage.setItem(key,value); const state={}; const $=(s,p=document)=>p.querySelector(s);</script>
      <script src="/js/agentpane.js"></script><script>window.dividerDispose=agentPaneDividerMount(document.querySelector('.agsplit'));</script>`);
  }
  const file = path.resolve(root, '.' + req.url);
  if (!file.startsWith(root + path.sep) || !fs.existsSync(file)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css');
  res.end(fs.readFileSync(file));
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({ headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE || undefined });
  try {
    const page = await browser.newPage({ viewport: { width: 1200, height: 700 } });
    const origin = `http://127.0.0.1:${server.address().port}`;
    await page.goto(origin);
    const divider = page.locator('[data-agent-pane-divider]');
    await divider.waitFor();
    const widths = async () => page.evaluate(() => {
      const root = document.querySelector('.agsplit');
      const terminal = document.querySelector('.global-agent-session').getBoundingClientRect();
      const pane = document.querySelector('.agpad').getBoundingClientRect();
      return { terminal: terminal.width, pane: pane.width, columns: getComputedStyle(root).gridTemplateColumns.split(' '), hidden: getComputedStyle(document.querySelector('[data-agent-pane-divider]')).display === 'none' };
    });
    const initial = await widths();
    const handle = await divider.boundingBox();
    await page.mouse.move(handle.x + handle.width / 2, 300);
    await page.waitForTimeout(180);
    const hoverStyle = await divider.evaluate(el => {
      const rule = getComputedStyle(el, '::before');
      return {width: rule.width, background: rule.backgroundColor, quiet: getComputedStyle(el.closest('.agsplit')).getPropertyValue('--line-2').trim()};
    });
    assert.equal(hoverStyle.width, '2px', 'nearby cursor highlights the divider');
    assert.notEqual(hoverStyle.background, hoverStyle.quiet, 'hover highlight uses a discernible neutral color');
    await page.mouse.down();
    await page.mouse.move(handle.x - 120, 300);
    await page.waitForTimeout(180);
    const dragStyle = await divider.evaluate(el => {
      const rule = getComputedStyle(el, '::before');
      return {width: rule.width, background: rule.backgroundColor};
    });
    assert.equal(dragStyle.width, '3px', 'dragging strengthens the divider highlight');
    assert.notEqual(dragStyle.background, hoverStyle.background, 'drag highlight is stronger than hover');
    await page.mouse.up();
    const dragged = await widths();
    assert(dragged.terminal < initial.terminal && dragged.pane > initial.pane, 'drag direction keeps complementary widths');
    await divider.focus();
    await page.keyboard.press('ArrowRight');
    const keyed = await widths();
    assert(keyed.terminal > dragged.terminal && keyed.pane < dragged.pane, 'keyboard arrows move the divider');
    const saved = await page.evaluate(() => localStorage.getItem('agent-pane-ratio'));
    assert(saved, 'dragged ratio is persisted');
    await page.reload();
    await divider.waitFor();
    const restored = await widths();
    assert(Math.abs(restored.terminal - keyed.terminal) < 2, 'saved ratio restores after reload');
    const maxHandle = await divider.boundingBox();
    await page.mouse.move(maxHandle.x + 700, 300);
    await page.mouse.down(); await page.mouse.up();
    const maxed = await widths();
    assert(maxed.pane >= 239, 'overview keeps its minimum width');

    await page.evaluate(() => document.querySelector('.agsplit').classList.add('nav-off'));
    await page.evaluate(() => document.querySelector('.agsplit')._agentPaneDividerRefresh());
    const navOff = await widths();
    assert.equal(navOff.columns.length, 4, 'navigation collapse retains the divider track');
    await page.evaluate(() => document.querySelector('.agsplit').classList.remove('nav-off'));
    await page.evaluate(() => document.querySelector('.agsplit')._agentPaneDividerRefresh());
    await page.evaluate(() => document.querySelector('.agsplit').classList.add('pad-off'));
    const padOff = await widths();
    assert.equal(padOff.columns.length, 3, 'pane collapse consumes the divider track');
    assert.equal(padOff.pane, 0, 'pane collapse keeps the overview closed');

    await page.setViewportSize({ width: 390, height: 700 });
    const mobile = await widths();
    assert.equal(mobile.hidden, true, 'mobile hides the divider hit area');
    assert.equal(mobile.columns.length, 3, 'mobile preserves the baseline global three-column layout');
    assert.equal(mobile.pane, 0, 'mobile pad collapse consumes all available tracks');
    await page.evaluate(() => { const root = document.querySelector('.agsplit'); root.classList.remove('pad-off'); root._agentPaneDividerRefresh(); });
    const reopened = await widths();
    assert(reopened.pane > 0, 'reopening the overview restores the saved layout');
    assert.equal(await page.evaluate(() => document.querySelector('.agent-pane-divider').getAttribute('aria-orientation')), 'vertical');
    console.log('Agent pane divider: drag direction, bounds, keyboard, persistence, navigation/pane toggles, and mobile layout passed');
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => { console.error(error); server.close(); process.exitCode = 1; });
