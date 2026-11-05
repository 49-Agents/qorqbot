// Isolated browser acceptance for Focus text blocks: production CSS/JS with a mocked server that follows the focus-layout contract.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo = path.resolve(__dirname, '..');
const chief = '11111111-1111-4111-8111-111111111111', marketer = '22222222-2222-4222-8222-222222222222';
const uuid = n => `00000000-0000-4000-8000-${String(n).padStart(12, '0')}`;
const cards = ['task-a', 'task-b', 'task-c', 'task-d'].map((id, i) => ({id, label: ['Reply to supplier quote', 'Review launch checklist', 'Book dentist appointment', 'Draft newsletter'][i]}));
let order, layouts, layoutPosts, moves, failNext, pageSize;
const reset = () => {
  order = cards.map(card => card.id); layoutPosts = []; moves = []; failNext = false; pageSize = 50;
  layouts = new Map([[chief, {revision: 1, blocks: [
    {id: uuid(1), text: 'Today', before_id: 'task-a', after_id: null},
    {id: uuid(2), text: '\n\n', before_id: 'task-c', after_id: 'task-b'},
    {id: uuid(3), text: 'Later / ideas', before_id: 'task-c', after_id: 'task-b'},
    {id: uuid(4), text: 'Ask Maya about samples.\n', before_id: null, after_id: 'task-d'}]}],
    [marketer, {revision: 0, blocks: []}]]);
};
reset();
const item = card => ({id: card.id, revision: 1, label: card.label, state: 'open', archived: false, created_by: chief,
  source: {type: 'task', id: card.id, business_id: 'biz-a'},
  resolved_source: {type: 'task', id: card.id, business_id: 'biz-a', business_slug: 'software', business_name: 'Qorqut Software', label: card.label},
  conversation: {type: 'task', id: card.id, business_id: 'biz-a'}, source_revision: 1, references: [], display_name: card.label});
let origin;
const layoutMatchAgent = url => url.pathname.split('/').at(-2);
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  const json = (data, status = 200) => {res.statusCode = status; res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(data));};
  const ok = data => json({status: 'ok', data});
  const fail = (code, message, status) => json({status: 'error', error: {code, message}}, status);
  const body = async () => {let raw = ''; for await (const chunk of req) raw += chunk; return raw ? JSON.parse(raw) : {};};
  const layoutMatch = url.pathname.match(/^\/api\/root\/agents\/([^/]+)\/focus-layout$/);
  if (layoutMatch) {
    const agent = layoutMatch[1], layout = layouts.get(agent);
    const view = () => ({agent_id: agent, revision: layout.revision, blocks: layout.blocks, card_ids: order});
    if (req.method === 'GET') return ok(view());
    const request = await body();
    layoutPosts.push({agent, ...request});
    if (failNext) {failNext = false; return fail('internal', 'Disk is full', 500);}
    const same = JSON.stringify(request.blocks) === JSON.stringify(layout.blocks);
    if (!same && request.expected_revision !== layout.revision) return fail('revision_conflict', 'Layout changed', 409);
    if (!same) {layout.blocks = request.blocks; layout.revision++;}
    return ok(view());
  }
  if (url.pathname.endsWith('/operations/focus_get')) {
    const request = await body(), offset = request.offset || 0;
    const page = order.slice(offset, offset + pageSize);
    return ok({revision: 1, status: '', stale: false, items: page.map(id => item(cards.find(card => card.id === id))), next_offset: offset + pageSize < order.length ? offset + pageSize : null, browser: {state: 'idle'}});
  }
  if (url.pathname.endsWith('/operations/focus_move')) {
    const request = await body(); moves.push(request);
    order = order.filter(id => id !== request.item_id);
    const anchor = order.indexOf(request.before_id || request.after_id);
    order.splice(anchor + (request.after_id ? 1 : 0), 0, request.item_id);
    return ok({item: item(cards.find(card => card.id === request.item_id)), revision: 2});
  }
  if (url.pathname.endsWith('/operations/tasks_config_get')) return ok({config: {states: [{name: 'open'}, {name: 'doing'}, {name: 'done'}]}, revision: 1});
  // The host decides which roles get Focus; this fixture authorizes both agents.
  if (url.pathname.endsWith('/operations/workspace_types')) return ok({types: [{key: 'focus', display_name: 'Focus', supported_roles: ['ordubashy', 'global-worker'], renderer: 'focus_board'}]});
  if (url.pathname === '/api/root/agent-pins') return ok({initialized: true, pinned_agents: []});
  if (url.pathname === '/api/root/agents') return ok({agents: [
    {agent_id: chief, name: 'chief', display_name: 'Ordubashy', role: {id: 'ordubashy'}, scope: {kind: 'ecosystem', allowed_business_ids: [], global_capabilities: []}},
    {agent_id: marketer, name: 'marketer', display_name: 'Marketing', role: {id: 'global-worker'}, scope: {kind: 'ecosystem', allowed_business_ids: [], global_capabilities: []}}]});
  if (url.pathname === '/api/session') return ok({principal_id: 'owner', principal_kind: 'human', display_name: 'Owner', is_owner: true});
  if (url.pathname.endsWith('/browser-surfaces')) return json({agent_id: layoutMatchAgent(url), configured: true, browsers: [{id: 'main', label: 'Main browser', kind: 'browser', viewer_url: origin + '/files/view'}], desktop: null});
  if (url.pathname === '/api/root/terminals') return json([]);
  if (url.pathname.endsWith('/operations/calendar_calendars_list')) return ok({calendars: [], errors: []});
  if (url.pathname === '/api/biz') return json([]);
  if (url.pathname.endsWith('/inbox')) return ok({items: [], subscriptions: [], watcher_active: false});
  if (url.pathname === '/files/view') {res.setHeader('Content-Type', 'text/html'); return res.end('<p>Browser fixture</p>');}
  if (url.pathname.startsWith('/api/')) return ok({items: [], subscriptions: [], notes: []});
  if (url.pathname === '/') {
    res.setHeader('Content-Type', 'text/html');
    // The production shell and scripts; only the transport is mocked.
    return res.end(fs.readFileSync(path.join(repo, 'index.html'), 'utf8'));
  }
  const file = path.join(repo, url.pathname);
  if (!file.startsWith(repo + path.sep) || !fs.existsSync(file)) {res.statusCode = 404; return res.end();}
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css'); res.end(fs.readFileSync(file));
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  origin = `http://127.0.0.1:${server.address().port}`;
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox'], executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});
  try {
    const page = await browser.newPage({viewport: {width: 1300, height: 900}}), errors = [];
    page.setDefaultTimeout(8000);
    page.on('pageerror', e => {errors.push(e.message); console.error(e.message);});
    await page.route('**/*', route => route.request().url().startsWith(origin) ? route.continue() : route.abort());
    const open = async () => {
      await page.goto('about:blank');
      await page.goto(origin + '/#/global/agents');
      await page.getByRole('tab', {name: 'Focus', exact: true}).click();
      await page.locator('.agfocus-items [data-card-id="task-a"]').waitFor();
      await page.locator('.agfocus-text').first().waitFor();
    };
    // Visible order of cards and text blocks, by text for blocks and card ID for cards.
    const visible = () => page.evaluate(() => [...document.querySelectorAll('.agfocus-items > *')].map(node => node.dataset.textId ? 'T:' + node.querySelector('textarea').value.replace(/\n/g, '\\n') : node.dataset.cardId));
    const grip = async value => page.evaluateHandle(value => [...document.querySelectorAll('.agfocus-text')].find(n => n.querySelector('textarea').value === value).querySelector('.agfocus-text-grip'), value);
    const area = async value => page.evaluateHandle(value => [...document.querySelectorAll('.agfocus-text textarea')].find(el => el.value === value), value);
    const settle = async expected => {for (let i = 0; i < 200; i++) {const now = await visible(); if (JSON.stringify(now) === JSON.stringify(expected)) return; await page.waitForTimeout(25);} assert.deepEqual(await visible(), expected);};
    const saved = async (count, what) => {await until(() => layoutPosts.length === count, what); await page.waitForFunction(() => !_agpad.focusOrderPending);};
    const lastLayout = () => layouts.get(chief).blocks;
    const until = async (check, what) => {for (let i = 0; i < 200; i++) {if (check()) return; await page.waitForTimeout(25);} throw new Error('Timed out: ' + what);};

    // ---- render: server order, exact whitespace, blank blocks keep their space, no card chrome
    await open();
    assert.deepEqual(await visible(), ['T:Today', 'task-a', 'task-b', 'T:\\n\\n', 'T:Later / ideas', 'task-c', 'task-d', 'T:Ask Maya about samples.\\n']);
    const spacer = await area('\n\n');
    assert.ok((await spacer.evaluate(el => el.getBoundingClientRect().height)) >= 60, 'blank lines keep allocated height');
    assert.equal(await page.locator('.agfocus-text .ball,.agfocus-text .agfocus-scope-chip').count(), 0);
    assert.equal(await (await area('Ask Maya about samples.\n')).evaluate(el => el.scrollHeight <= el.clientHeight + 1 && el.clientHeight >= 40), true, 'a trailing newline is allocated');

    // ---- editing: Enter adds newlines, autosize follows, exact text autosaves once, selection never drags
    const today = await area('Today');
    const before = await today.evaluate(el => el.getBoundingClientRect().height);
    await today.evaluate(el => el.focus());
    await page.keyboard.press('End'); await page.keyboard.type(' plan'); await page.keyboard.press('Enter'); await page.keyboard.press('Enter');
    assert.ok(await today.evaluate((el, before) => el.getBoundingClientRect().height > before + 30, before), 'textarea grows with newlines');
    assert.equal(layoutPosts.length, 0, 'saves are debounced');
    await saved(1, 'autosave');
    assert.equal(lastLayout()[0].text, 'Today plan\n\n');
    assert.equal(layoutPosts[0].expected_revision, 1);
    assert.deepEqual(Object.keys(layoutPosts[0].blocks[0]).sort(), ['after_id', 'before_id', 'id', 'text']);
    await page.locator('.agfocus-text-status', {hasText: 'Saved'}).waitFor();
    const box = await today.evaluate(el => {const r = el.getBoundingClientRect(); return {x: r.x, y: r.y};});
    await page.mouse.move(box.x + 60, box.y + 10); await page.mouse.down(); await page.mouse.move(box.x + 140, box.y + 12, {steps: 5}); await page.mouse.up();
    assert.equal((await visible())[0], 'T:Today plan\\n\\n', 'text selection does not move the block');
    assert.equal(moves.length, 0);

    // ---- keyboard: Alt+Arrow on the handle crosses cards and text; text-only crossings never call focus_move
    await (await grip('Later / ideas')).evaluate(el => el.focus());
    await page.keyboard.press('Alt+ArrowDown');
    await saved(2, 'keyboard move saves');
    await settle(['T:Today plan\\n\\n', 'task-a', 'task-b', 'T:\\n\\n', 'task-c', 'T:Later / ideas', 'task-d', 'T:Ask Maya about samples.\\n']);
    assert.deepEqual(lastLayout().find(b => b.text === 'Later / ideas'), {id: uuid(3), text: 'Later / ideas', before_id: 'task-d', after_id: 'task-c'});
    assert.equal(moves.length, 0);
    assert.equal(await page.evaluate(() => document.activeElement.className.includes('agfocus-text-grip')), true, 'handle keeps keyboard focus');
    await page.keyboard.press('Alt+ArrowUp');
    await saved(3, 'move back');

    // ---- card moves around text: focus_move gets only card IDs; text follows the intended order
    const cardB = page.locator('[data-card-id="task-b"]');
    await cardB.focus(); await page.keyboard.press('Alt+ArrowUp');
    await until(() => moves.length === 1, 'card move');
    assert.deepEqual({item: moves[0].item_id, before: moves[0].before_id}, {item: 'task-b', before: 'task-a'});
    assert.ok(!JSON.stringify(moves).includes('0000-4000'), 'text IDs never reach focus_move');
    await saved(4, 'text follows card move');
    await settle(['T:Today plan\\n\\n', 'task-b', 'task-a', 'T:\\n\\n', 'T:Later / ideas', 'task-c', 'task-d', 'T:Ask Maya about samples.\\n']);
    const cardC = page.locator('[data-card-id="task-c"]');
    await cardC.focus(); await page.keyboard.press('Alt+ArrowUp');
    await saved(5, 'card crosses text only');
    assert.equal(moves.length, 1, 'crossing only text saves placements without focus_move');
    assert.deepEqual((await visible()).slice(3, 7), ['T:\\n\\n', 'task-c', 'T:Later / ideas', 'task-d']);

    // ---- pointer drag by handle, wide enough to cross cards
    const g = await grip('Later / ideas'), gb = await g.evaluate(el => {const r = el.getBoundingClientRect(); return {x: r.x + r.width / 2, y: r.y + r.height / 2};});
    const top = await page.locator('[data-card-id="task-b"] .agfocus-row').boundingBox();
    await page.mouse.move(gb.x, gb.y); await page.mouse.down(); await page.mouse.move(top.x + 200, top.y + 4, {steps: 8}); await page.mouse.up();
    await saved(6, 'drag saves');
    assert.deepEqual((await visible()).slice(0, 3), ['T:Today plan\\n\\n', 'T:Later / ideas', 'task-b']);
    assert.equal(moves.length, 1);

    // ---- cards stay interactive: expansion and state still work beside text
    await page.locator('[data-card-id="task-a"] .ttl').click();
    assert.equal(await page.locator('[data-card-id="task-a"]').getAttribute('aria-expanded'), 'true');
    await page.locator('.agfocus-dock .rw-close').click();

    // ---- delete is explicit; blank text is never removed implicitly
    const spacerBox = page.locator('.agfocus-text').filter({has: page.locator('textarea')}).nth(await page.evaluate(() => [...document.querySelectorAll('.agfocus-text textarea')].findIndex(el => el.value === '\n\n')));
    await (await area('\n\n')).evaluate(el => {el.focus(); el.value = ''; el.dispatchEvent(new Event('input', {bubbles: true}));});
    await until(() => lastLayout().some(b => b.text === ''), 'a blank block is saved');
    await spacerBox.hover(); await spacerBox.getByRole('button', {name: 'Delete text block'}).click();
    await until(() => !lastLayout().some(b => b.text === ''), 'delete saves');
    assert.equal(await page.locator('.agfocus-text').count(), 3);

    // ---- adding: Text or space atop the page, empty accepted, editor focused, Enter does not submit
    await page.locator('[data-focus-add]').click();
    await page.getByLabel('Text or space').check();
    assert.equal(await page.locator('[name=title]').isVisible(), false);
    await page.getByRole('button', {name: 'Add', exact: true}).click();
    await page.waitForFunction(() => document.activeElement?.classList.contains('agfocus-text-input'));
    assert.equal((await visible())[0].startsWith('T:'), true);
    assert.equal(await page.evaluate(() => document.activeElement.value), '');
    await page.keyboard.type('Next week'); await page.keyboard.press('Enter'); await page.keyboard.press('Enter');
    await until(() => lastLayout()[0].text === 'Next week\n\n', 'new block saved');
    assert.equal(lastLayout()[0].before_id, (await page.evaluate(() => document.querySelector('.agfocus-items > .agfocus-card').dataset.cardId)));

    // ---- failed save keeps the draft and retries explicitly; conflict never overwrites silently
    failNext = true;
    await page.keyboard.type('!');
    await page.locator('.agfocus-text-status[data-state=failed]').waitFor();
    assert.equal(await page.evaluate(() => document.activeElement.value), 'Next week\n\n!');
    await page.getByRole('button', {name: 'Retry', exact: true}).click();
    await until(() => lastLayout()[0].text === 'Next week\n\n!', 'retry saves');
    layouts.get(chief).revision += 5; layouts.get(chief).blocks = layouts.get(chief).blocks.slice(1);
    await (await area('Next week\n\n!')).evaluate(el => {el.focus(); el.setSelectionRange(el.value.length, el.value.length);});
    await page.keyboard.type('?');
    await page.locator('.agfocus-text-status[data-state=conflict]').waitFor();
    assert.equal(lastLayout().some(b => b.text.includes('?')), false, 'a conflicting save writes nothing');
    assert.equal(await page.evaluate(() => document.activeElement.value), 'Next week\n\n!?', 'the draft is kept');
    await page.getByRole('button', {name: 'Save mine anyway'}).click();
    await until(() => lastLayout().some(b => b.text === 'Next week\n\n!?'), 'explicit overwrite');

    // ---- reload and agent separation: pending drafts finish for the original agent only
    await (await area('Next week\n\n!?')).evaluate(el => {el.focus(); el.setSelectionRange(el.value.length, el.value.length);});
    await page.keyboard.type(' pending');
    await page.locator(`[data-global-agent="${marketer}"]`).click();
    await page.getByRole('tab', {name: 'Focus', exact: true}).click();
    await page.locator('[data-card-id="task-a"]').waitFor();
    await until(() => lastLayout().some(b => b.text.endsWith('pending')), 'draft saved to the original agent');
    assert.equal(layoutPosts.at(-1).agent, chief);
    assert.equal(await page.locator('.agfocus-text').count(), 0, 'the other agent has no blocks');
    assert.equal(layouts.get(marketer).blocks.length, 0);
    await page.locator(`[data-global-agent="${chief}"]`).click();
    await page.getByRole('tab', {name: 'Focus', exact: true}).click();
    await page.locator('.agfocus-text textarea').first().waitFor();
    assert.ok(await page.evaluate(() => [...document.querySelectorAll('.agfocus-text textarea')].some(el => el.value.endsWith(' pending'))));

    // ---- Browser round trip keeps the draft
    await (await area(await page.evaluate(() => document.querySelector('.agfocus-text textarea').value))).evaluate(el => el.focus());
    await page.keyboard.type('+');
    await page.locator('[data-focus-surface="browser"]').click();
    await page.locator('.agfocus-surface:not([hidden])').waitFor();
    assert.equal(await page.locator('.agfocus-text textarea').first().isVisible(), false, 'the list is hidden while a surface is open');
    await page.locator('[data-surface-back]').click();
    await page.locator('.agfocus-text textarea').first().waitFor();
    assert.ok(await page.evaluate(() => [...document.querySelectorAll('.agfocus-text textarea')].some(el => el.value.endsWith('pending+'))));
    await until(() => lastLayout().some(b => b.text.endsWith('pending+')), 'draft saved after round trip');

    // ---- paging: an off-page anchor is not a missing one, and notes follow their live anchor
    reset(); pageSize = 2;
    layouts.get(chief).blocks = [{id: uuid(1), text: 'On page two', before_id: 'task-c', after_id: null}, {id: uuid(2), text: 'Orphan', before_id: 'gone', after_id: null}];
    await open();
    assert.deepEqual(await visible(), ['task-a', 'task-b', 'T:Orphan']);
    await page.locator('[data-focus-more]').evaluate(el => el.click());
    await page.locator('[data-card-id="task-c"]').waitFor();
    assert.deepEqual(await visible(), ['T:On page two', 'task-c', 'task-d']);
    assert.equal(layoutPosts.length, 0, 'paging and missing anchors never write');
    assert.deepEqual(errors, []);
    console.log('Focus text blocks: render, edit, autosave, ordering, add/delete, failure, conflict, agent separation and paging passed.');
  } finally {await browser.close(); server.close();}
})().catch(error => {console.error(error); process.exitCode = 1;});
