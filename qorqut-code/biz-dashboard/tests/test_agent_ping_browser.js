#!/usr/bin/env node
// Ordubashy auto-ping switch over a stubbed broadcast API; no dashboard server or agent writes.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.resolve(__dirname, '..');
const pane = role => `<aside class="tasks agpad" data-role="${role}"><div class="thead nott"><div class="ttabs" role="tablist">
  <button class="ttab on" type="button">I</button><button class="ttab" type="button">T</button><button class="ttab" type="button">F</button></div></div></aside>`;
const server = http.createServer((req, res) => {
  if (req.url === '/') {
    res.setHeader('Content-Type', 'text/html');
    return res.end(`<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="/app.css">
      <link rel="stylesheet" href="/css/tasks.css"><link rel="stylesheet" href="/css/agentpane.css">
      <style>body{display:block;margin:0}.agpad{width:480px;display:block;background:#1c2c38}</style>
      ${pane('ordubashy')}${pane('business-agent')}
      <script>
        const ls = () => null, state = {}, $ = (s, p = document) => p.querySelector(s);
        window.calls = []; window.toasts = []; window.items = []; window.fail = null;
        const toast = m => toasts.push(m), newRequestId = () => 'req-' + calls.length;
        const coreData = r => { if (r.status !== 'ok') throw Error(r.error.message); return r.data; };
        async function post(url, body) {
          const name = url.split('/').pop(); calls.push({ name, body });
          if (fail === name) return { status: 'error', error: { message: 'Conflict' } };
          if (name === 'broadcasts_list') return { status: 'ok', data: { items: JSON.parse(JSON.stringify(items)) } };
          const later = new Date(Date.now() + 5 * 3600e3).toISOString();
          if (name === 'broadcasts_create') items.unshift({ id: 'b1', revision: 1, status: 'active', delivery: 'inbox', next_due_at: later,
            name: body.definition.name, message: body.definition.message,
            recipients_json: body.definition.recipients.map(agent_id => ({ agent_id, business_id: null })) });
          const item = items.find(i => i.id === body.broadcast_id);
          if (item && body.expected_revision !== item.revision) return { status: 'error', error: { message: 'Stale revision' } };
          if (name === 'broadcasts_update') Object.assign(item, { next_due_at: later, message: body.definition.message, revision: item.revision + 1 });
          if (name === 'broadcasts_pause') Object.assign(item, { status: 'paused', revision: item.revision + 1 });
          if (name === 'broadcasts_resume') Object.assign(item, { status: 'active', revision: item.revision + 1 });
          return { status: 'ok', data: {} };
        }
      </script><script src="/js/agentpane.js"></script>
      <script>
        window.panes = [...document.querySelectorAll('.agpad')].map(el =>
          ({ el, agentId: el.dataset.role === 'ordubashy' ? 'ordu-1' : 'ba-1', workspaceRole: el.dataset.role }));
        window.mountAll = () => panes.forEach(p => { _agpad = p; agpadPingMount(p); });
        _agpad = panes[0];
      </script>`);
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
    const page = await browser.newPage({ viewport: { width: 900, height: 400 } });
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    // Existing saved schedule from another agent must not count.
    await page.evaluate(() => { items.push({ id: 'x', revision: 1, status: 'active', delivery: 'inbox', name: 'Focus check-in',
      recipients_json: '[{"agent_id":"other","business_id":null}]' }); mountAll(); _agpad = panes[0]; });
    const toggle = page.locator('[data-role="ordubashy"] .agpad-ping');
    await toggle.waitFor();
    assert.equal(await page.locator('[data-role="business-agent"] .agpad-ping').count(), 0, 'only Ordubashy gets the switch');
    await page.waitForFunction(() => !document.querySelector('.agpad-ping').disabled);
    assert.equal(await toggle.getAttribute('aria-checked'), 'false', 'off without its own schedule');
    const box = await toggle.boundingBox(), tabs = await page.locator('[data-role="ordubashy"] .ttabs').boundingBox();
    const edge = await page.locator('[data-role="ordubashy"]').boundingBox();
    assert.ok(box.x > tabs.x + 3 * 44 && box.x + box.width <= edge.x + edge.width - 20, 'switch sits at the right end of the tab row');
    assert.ok(Math.abs(box.y - tabs.y) < 2, 'switch is on the tab row');

    // The tab strip must never grow a vertical scrollbar, in a normal or a narrow pane (horizontal overflow only).
    for (const width of [480, 96]) {
      await page.evaluate(w => { document.querySelectorAll('.agpad').forEach(el => { el.style.width = w + 'px'; }); }, width);
      const strip = await page.locator('[data-role="business-agent"] .ttabs').evaluate(el => ({
        y: getComputedStyle(el).overflowY, vertical: el.scrollHeight > el.clientHeight }));
      assert.deepEqual(strip, { y: 'hidden', vertical: false }, `no vertical tab scrollbar at ${width}px`);
    }
    await page.evaluate(() => document.querySelectorAll('.agpad').forEach(el => { el.style.width = ''; }));

    await toggle.click();
    await page.waitForFunction(() => document.querySelector('.agpad-ping').getAttribute('aria-checked') === 'true');
    const create = await page.evaluate(() => calls.find(c => c.name === 'broadcasts_create').body);
    assert.equal(create.scope, 'global');
    assert.deepEqual(create.definition.recipients, ['ordu-1']);
    assert.deepEqual(create.definition.timing, { kind: 'interval', every: 5, unit: 'hours' }, 'first ping 5 hours after turning on');
    const clock = page.locator('[data-role="ordubashy"] .agpad-ping-next');
    assert.equal(await clock.textContent(), '5h', 'countdown shows hours');
    assert.equal(await toggle.getAttribute('title'), null, 'no tooltip');
    const later = async minutes => { await page.evaluate(m => { items[0].next_due_at = new Date(Date.now() + m * 60e3).toISOString();
      panes[0].pingNextLoad = 0; agpadPingTick(panes[0]); }, minutes); await page.waitForFunction(() => !panes[0].pingLoading); };
    await later(130); assert.equal(await clock.textContent(), '2h');
    await later(30); assert.equal(await clock.textContent(), '30m', 'minutes under an hour');
    assert.match(create.definition.message, /Focus board.*doing.*waiting.*blocked.*focus_present.*focus_set_archived/s);
    assert.ok(create.idempotency_key);
    await page.screenshot({ path: process.env.PING_SCREENSHOT || '/dev/null', clip: { x: 0, y: 0, width: 520, height: 120 } }).catch(() => {});

    await toggle.click();
    await page.waitForFunction(() => document.querySelector('.agpad-ping').getAttribute('aria-checked') === 'false');
    const pause = await page.evaluate(() => calls.filter(c => c.name === 'broadcasts_pause').map(c => c.body));
    assert.deepEqual(pause.map(b => [b.broadcast_id, b.expected_revision]), [['b1', 1]], 'pauses only its own schedule');
    assert.equal(await clock.isHidden(), true, 'no countdown while off');
    await page.evaluate(() => { items[0].message = 'Edited in the megaphone'; items[0].next_due_at = new Date(Date.now() - 9 * 3600e3).toISOString(); });

    await toggle.click();
    await page.waitForFunction(() => document.querySelector('.agpad-ping').getAttribute('aria-checked') === 'true');
    const update = await page.evaluate(() => calls.filter(c => c.name === 'broadcasts_update').map(c => c.body));
    assert.deepEqual(update.map(b => [b.broadcast_id, b.expected_revision, b.definition.timing, b.definition.message, b.definition.recipients]),
      [['b1', 2, { kind: 'interval', every: 5, unit: 'hours' }, 'Edited in the megaphone', ['ordu-1']]], 'restarts the clock, keeps the wording');
    const resume = await page.evaluate(() => calls.filter(c => c.name === 'broadcasts_resume').map(c => c.body));
    assert.deepEqual(resume.map(b => [b.broadcast_id, b.expected_revision]), [['b1', 3]], 'resumes the saved schedule after the restart');
    assert.equal(await clock.textContent(), '5h', 'an overdue old time never fires on resume');
    assert.equal(await page.evaluate(() => calls.filter(c => c.name === 'broadcasts_create').length), 1, 'never duplicates');

    // A failed change keeps the switch honest and reports the error.
    await page.evaluate(() => { fail = 'broadcasts_pause'; });
    await toggle.click();
    await page.waitForFunction(() => toasts.includes('Conflict'));
    await page.waitForFunction(() => !document.querySelector('.agpad-ping').disabled);
    assert.equal(await toggle.getAttribute('aria-checked'), 'true');
    console.log('agent ping browser: ok');
  } finally {
    await browser.close();
    server.close();
  }
})().catch(error => { console.error(error); process.exit(1); });
