'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Browser checks are optional. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const browserTest = (name, fn) => test(name, {skip: (!chromium || !fs.existsSync(executable)) && 'Local Playwright Chromium is unavailable'}, fn);

// One Focus notes area over the owner's saved layout: `saved` is the server's copy.
async function fixture(saved) {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  const page = await browser.newPage({viewport: {width: 900, height: 700}});
  await page.setContent('<div class="agpad"><div class="tbody"><section class="agfocus"><div class="agfocus-layout"><div class="agfocus-notes"></div></div></section></div></div>');
  await page.addStyleTag({content: ':root{--fg:#eee;--line:#333;--sans:Arial}'});
  // app.css comes first, as in the dashboard: it styles every Focus button, which the status ball must not inherit.
  for (const file of ['app.css', 'css/status-ball.css', 'css/notes.css', 'css/agent-focus.css']) await page.addStyleTag({path: path.join(__dirname, '..', file)});
  await page.evaluate(initial => {
    window.saved = initial; window.posts = [];
    let n = 0;
    window.newRequestId = () => `00000000-0000-4000-8000-00000000000${++n}`;
    window.coreData = x => x;
    window.api = async () => structuredClone(saved);
    window.post = async (url, data) => {
      posts.push(structuredClone(data));
      if (data.expected_revision !== saved.revision) throw Object.assign(new Error('stale'), {code: 'revision_conflict'});
      saved = {...saved, revision: saved.revision + 1, blocks: structuredClone(data.blocks), statuses: structuredClone(data.statuses)};
      return structuredClone(saved);
    };
  }, saved);
  for (const file of ['status-ball.js', 'lines-editor.js', 'focus-text.js']) await page.addScriptTag({path: path.join(__dirname, '../js', file)});
  const mount = () => page.evaluate(() => {
    window.panel = {agentId: 'agent', focusMount: document.querySelector('.agfocus')};
    window.dispose = focusText.mount(panel, panel.focusMount);
  });
  await mount();
  await page.waitForFunction(() => !document.querySelector('.agfocus-notes-input').classList.contains('loading'));
  return {browser, page, mount};
}
// The notes as they are stored: one raw line per block, a task line starting with "- ".
const value = page => page.evaluate(() => focusText.state('agent').lines.join('\n'));
const NOTES = '.agfocus-notes .ntdoc';
const settle = page => page.waitForFunction(() => !document.querySelector('.agfocus-text-status:not([hidden])[data-state=saving]'));

browserTest('a first start shows the sample text and saves nothing until the owner edits', async () => {
  const {browser, page} = await fixture({revision: 0, card_ids: [], blocks: []});
  try {
    assert.equal(await value(page), 'Your notes here');
    // The notes fill the board's width, not just the width of their text.
    const widths = await page.evaluate(() => [document.querySelector('.agfocus-layout'), document.querySelector('.agfocus-notes-input')].map(e => e.getBoundingClientRect().width));
    assert.equal(widths[1], widths[0]);
    await page.waitForTimeout(700);
    assert.deepEqual(await page.evaluate(() => posts), []);
    await page.locator(NOTES).fill('Call the bank\nPlan launch');
    await settle(page);
    const posts = await page.evaluate(() => posts);
    assert.equal(posts.length, 1);
    assert.deepEqual(posts[0], {expected_revision: 0, blocks: [{id: posts[0].blocks[0].id, text: 'Call the bank\nPlan launch', before_id: null, after_id: null}], statuses: {}});
    assert.equal(await page.locator('[data-text-message]').textContent(), 'Saved');
  } finally { await browser.close(); }
});

browserTest('an item opened on Focus (signoff, browser) hides the notes until it closes', async () => {
  const {browser, page} = await fixture({revision: 0, card_ids: [], blocks: []});
  try {
    const notesShown = () => page.evaluate(() => {
      const r = document.querySelector('.agfocus-notes-input').getBoundingClientRect();
      return getComputedStyle(document.querySelector('.agfocus > .agfocus-layout')).display !== 'none' && r.width > 0 && r.height > 0;
    });
    assert.equal(await notesShown(), true);
    await page.evaluate(() => document.querySelector('.agfocus').classList.add('agfocus-surface-open'));
    assert.equal(await notesShown(), false);
    await page.evaluate(() => document.querySelector('.agfocus').classList.remove('agfocus-surface-open'));
    assert.equal(await notesShown(), true);
    assert.equal(await value(page), 'Your notes here');
  } finally { await browser.close(); }
});

browserTest('emptied notes stay empty after a reload instead of returning to the sample text', async () => {
  const {browser, page, mount} = await fixture({revision: 0, card_ids: [], blocks: []});
  try {
    await page.locator(NOTES).fill('');
    await settle(page);
    assert.equal(await page.evaluate(() => saved.revision), 1);
    await page.evaluate(() => { dispose(); });
    await mount();
    await page.evaluate(() => focusText.load(panel));
    assert.equal(await value(page), '');
  } finally { await browser.close(); }
});

browserTest('earlier separate text blocks read as one text and save back as one block', async () => {
  const {browser, page} = await fixture({revision: 4, card_ids: [], blocks: [
    {id: '11111111-1111-4111-8111-111111111111', text: 'Today', before_id: null, after_id: null},
    {id: '22222222-2222-4222-8222-222222222222', text: '- ship notes', before_id: null, after_id: null}]});
  try {
    assert.equal(await value(page), 'Today\n\n- ship notes');
    // The stored "-" line shows as a task line with a status ball, exactly as on a Journal page.
    assert.deepEqual(await page.locator(NOTES + ' .ntline').evaluateAll(lines => lines.map(l => [l.className, l.textContent])),
      [['ntline', 'Today'], ['ntline', ''], ['ntline nttask', 'ship notes']]);
    assert.equal(await page.locator('.agfocus-notes .ntball').count(), 1);
    // The ball is the Journal's bare ball, not a boxed Focus button.
    assert.deepEqual(await page.locator('.agfocus-notes .ntball').evaluate(b => { const c = getComputedStyle(b), r = b.getBoundingClientRect(); return [r.width, r.height, c.borderTopWidth, c.backgroundColor, c.paddingLeft]; }),
      [30, 30, '0px', 'rgba(0, 0, 0, 0)', '0px']);
    await page.locator(NOTES).press('Control+End');
    await page.keyboard.type('!');
    await settle(page);
    const posts = await page.evaluate(() => posts);
    assert.equal(posts.length, 1);
    assert.deepEqual(posts[0].blocks, [{id: '11111111-1111-4111-8111-111111111111', text: 'Today\n\n- ship notes!', before_id: null, after_id: null}]);
  } finally { await browser.close(); }
});

browserTest('a save based on an older revision keeps the edits and offers reload or overwrite', async () => {
  const {browser, page} = await fixture({revision: 2, card_ids: [], blocks: [{id: '11111111-1111-4111-8111-111111111111', text: 'old', before_id: null, after_id: null}]});
  try {
    await page.evaluate(() => { saved = {...saved, revision: 3, blocks: [{...saved.blocks[0], text: 'from elsewhere'}]}; });
    await page.locator(NOTES).fill('mine');
    await page.waitForSelector('.agfocus-text-status[data-state=conflict]');
    assert.equal(await value(page), 'mine');
    await page.click('[data-text-overwrite]');
    await settle(page);
    assert.equal(await page.evaluate(() => saved.blocks[0].text), 'mine');
  } finally { await browser.close(); }
});

browserTest('a typed "-" starts a task line whose status ball is saved, as in the Journal', async () => {
  const {browser, page, mount} = await fixture({revision: 1, card_ids: [], blocks: [{id: '11111111-1111-4111-8111-111111111111', text: '', before_id: null, after_id: null}]});
  try {
    await page.locator(NOTES).click();
    await page.keyboard.type('-call the bank');
    await page.keyboard.press('Enter');
    await page.keyboard.type('-x');
    await page.keyboard.press('Home');
    await page.keyboard.press('Backspace');   // at the start of a task line: back to a plain line
    assert.equal(await value(page), '- call the bank\nx');
    await page.keyboard.type('-send invoice, not ');
    await page.keyboard.press('End');
    await page.keyboard.press('Enter');
    await page.keyboard.type('later');
    assert.equal(await value(page), '- call the bank\n- send invoice, not x\nlater');
    await page.locator('.agfocus-notes .ntball').first().click();   // open -> white
    await page.locator('.agfocus-notes .ntball').first().click();   // white -> in progress
    await settle(page);
    assert.equal(await page.evaluate(() => saved.blocks[0].text), '- call the bank\n- send invoice, not x\nlater');
    assert.deepEqual(await page.evaluate(() => saved.statuses), {'call the bank': 'in_progress'});
    // Undo steps back through the edits; the reopened pane shows the saved status.
    await page.locator(NOTES).press('Control+z');
    assert.equal(await page.locator('.agfocus-notes .ntball').first().getAttribute('class'), 'ntball ntb-white');
    await page.locator(NOTES).press('Control+y');
    await settle(page);
    await page.evaluate(() => { dispose(); });
    await mount();
    await page.evaluate(() => focusText.load(panel));
    await page.waitForSelector('.agfocus-notes .ntball.ntb-in_progress');
    assert.equal(await page.locator('.agfocus-notes .ntball').count(), 2);
  } finally { await browser.close(); }
});

browserTest('notes saved with a hidden state token after the dash read as statuses and save that way', async () => {
  const S = '\u2063';
  const {browser, page} = await fixture({revision: 1, card_ids: [], blocks: [{id: '11111111-1111-4111-8111-111111111111', text: `Plan\n-${S}done${S} buy milk\n- call bank`, before_id: null, after_id: null}]});
  try {
    assert.equal(await value(page), 'Plan\n- buy milk\n- call bank');
    assert.equal(await page.locator('.agfocus-notes .ntball.ntb-done').count(), 1);
    assert.equal(await page.locator('.agfocus-notes .ntline.done').textContent(), 'buy milk');
    await page.locator(NOTES).click();
    await page.keyboard.press('Control+End');
    await page.keyboard.type('!');
    await settle(page);
    const posts = await page.evaluate(() => posts);
    assert.equal(posts.at(-1).blocks[0].text, 'Plan\n- buy milk\n- call bank!');
    assert.deepEqual(posts.at(-1).statuses, {'buy milk': 'done'});
  } finally { await browser.close(); }
});

browserTest('dragging a selection that starts or ends beside the text keeps it and does not jump to the end', async () => {
  const text = Array.from({length: 60}, (_, i) => `${i % 3 ? '' : '- '}Line ${i} of the notes`).join('\n');
  const {browser, page} = await fixture({revision: 1, card_ids: [], blocks: [{id: 'b', text}], statuses: {}});
  try {
    await page.evaluate(() => { document.querySelector('.tbody').style.height = '400px'; });
    const paper = await page.locator('.agfocus-notes-input').boundingBox();
    const drag = async (line, fromX, toX) => {
      const box = await page.locator('.agfocus-notes .ntline').nth(line).boundingBox();
      await page.mouse.move(paper.x + fromX, box.y + 8);
      await page.mouse.down();
      await page.mouse.move(paper.x + toX, box.y + 80, {steps: 5});
      await page.mouse.up();
      return page.evaluate(() => [document.querySelector('.agfocus-notes-input').scrollTop, String(getSelection())]);
    };
    // From the paper's left padding into the text, and from the text out to a task line's status-ball column.
    for (const [line, fromX, toX] of [[4, 5, 200], [4, 200, 20]]) {
      const [scrollTop, selected] = await drag(line, fromX, toX);
      assert.equal(scrollTop, 0);
      assert.match(selected, /Line 4 of the notes\nLine 5/);
    }
    // Pressing on the ball column and releasing on the text is not a click on blank paper either.
    assert.equal((await drag(3, 20, 200))[0], 0);
  } finally { await browser.close(); }
});

browserTest('a plain click on the blank paper below the text puts the caret at the end', async () => {
  const {browser, page} = await fixture({revision: 1, card_ids: [], blocks: [{id: 'b', text: 'One\nTwo'}], statuses: {}});
  try {
    const blank = await page.locator('.agfocus-notes-input').boundingBox();
    await page.mouse.click(blank.x + blank.width / 2, blank.y + blank.height - 10);
    assert.deepEqual(await page.evaluate(() => { const s = getSelection(); return [s.isCollapsed, s.anchorNode.textContent, s.anchorOffset]; }), [true, 'Two', 3]);
  } finally { await browser.close(); }
});

browserTest('Notes reopens where the owner left off: same scroll and caret after hiding or remounting', async () => {
  const text = Array.from({length: 80}, (_, i) => `Line ${i} of the notes`).join('\n');
  const {browser, page, mount} = await fixture({revision: 1, card_ids: [], blocks: [{id: 'b', text}], statuses: {}});
  try {
    await page.evaluate(() => { document.querySelector('.tbody').style.height = '400px'; });
    // First time: nothing remembered, the caret goes to the end as before.
    await page.evaluate(() => focusText.focus(panel));
    const first = await page.evaluate(() => getSelection().anchorNode.textContent);
    assert.equal(first, 'Line 79 of the notes');
    // The owner clicks into line 30 and leaves the text scrolled there.
    await page.locator('.agfocus-notes .ntline').nth(30).click();
    const scroll = await page.evaluate(() => { const p = document.querySelector('.agfocus-notes-input'); p.scrollTop = p.querySelectorAll('.ntline')[28].offsetTop; return p.scrollTop; });
    await page.waitForTimeout(400);
    assert.ok(scroll > 100);
    const back = () => page.evaluate(() => [document.querySelector('.agfocus-notes-input').scrollTop, getSelection().anchorNode?.textContent]);
    // A record or browser hides the notes (scroll resets to 0); Notes brings them back where they were.
    await page.evaluate(() => { document.activeElement.blur(); const l = document.querySelector('.agfocus-layout'); l.style.display = 'none'; l.offsetHeight; l.style.display = ''; focusText.focus(panel); });
    let [top, line] = await back();
    assert.ok(Math.abs(top - scroll) <= 1, `scroll ${top} vs ${scroll}`);
    assert.equal(line, 'Line 30 of the notes');
    // The pane is rebuilt (another agent and back): still the same place.
    await page.evaluate(() => dispose());
    await mount();
    await page.waitForFunction(() => !document.querySelector('.agfocus-notes-input').classList.contains('loading'));
    await page.evaluate(() => focusText.focus(panel));
    [top, line] = await back();
    assert.ok(Math.abs(top - scroll) <= 1, `scroll ${top} vs ${scroll}`);
    assert.equal(line, 'Line 30 of the notes');
    assert.equal(await page.evaluate(() => focusText.state('agent').view.caret.f.i), 30);
  } finally { await browser.close(); }
});
