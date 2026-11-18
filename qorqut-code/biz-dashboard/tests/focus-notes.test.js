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
  for (const file of ['status-ball.css', 'agent-focus.css']) await page.addStyleTag({path: path.join(__dirname, '../css', file)});
  await page.evaluate(initial => {
    window.saved = initial; window.posts = [];
    let n = 0;
    window.newRequestId = () => `00000000-0000-4000-8000-00000000000${++n}`;
    window.coreData = x => x;
    window.api = async () => structuredClone(saved);
    window.post = async (url, data) => {
      posts.push(structuredClone(data));
      if (data.expected_revision !== saved.revision) throw Object.assign(new Error('stale'), {code: 'revision_conflict'});
      saved = {...saved, revision: saved.revision + 1, blocks: structuredClone(data.blocks)};
      return structuredClone(saved);
    };
  }, saved);
  for (const file of ['status-ball.js', 'focus-text.js']) await page.addScriptTag({path: path.join(__dirname, '../js', file)});
  const mount = () => page.evaluate(() => {
    window.panel = {agentId: 'agent', focusMount: document.querySelector('.agfocus')};
    window.dispose = focusText.mount(panel, panel.focusMount);
  });
  await mount();
  await page.waitForFunction(() => !document.querySelector('.agfocus-notes-input').disabled);
  return {browser, page, mount};
}
const value = page => page.locator('.agfocus-notes-input').inputValue();
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
    await page.locator('.agfocus-notes-input').fill('Call the bank\nPlan launch');
    await settle(page);
    const posts = await page.evaluate(() => posts);
    assert.equal(posts.length, 1);
    assert.deepEqual(posts[0], {expected_revision: 0, blocks: [{id: posts[0].blocks[0].id, text: 'Call the bank\nPlan launch', before_id: null, after_id: null}]});
    assert.equal(await page.locator('[data-text-message]').textContent(), 'Saved');
  } finally { await browser.close(); }
});

browserTest('an item opened on Focus (signoff, browser, side chat) hides the notes until it closes', async () => {
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
    await page.locator('.agfocus-notes-input').fill('');
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
    await page.locator('.agfocus-notes-input').press('End');
    await page.locator('.agfocus-notes-input').type('!');
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
    await page.locator('.agfocus-notes-input').fill('mine');
    await page.waitForSelector('.agfocus-text-status[data-state=conflict]');
    assert.equal(await value(page), 'mine');
    await page.click('[data-text-overwrite]');
    await settle(page);
    assert.equal(await page.evaluate(() => saved.blocks[0].text), 'mine');
  } finally { await browser.close(); }
});

const S = '⁣';
browserTest('a line starting with "-" shows a status ball that cycles like the Journal and is saved with the text', async () => {
  const {browser, page, mount} = await fixture({revision: 1, card_ids: [], blocks: [{id: '11111111-1111-4111-8111-111111111111', text: 'Plan\n- call bank\nplain - line', before_id: null, after_id: null}]});
  try {
    // One ball, on the dash line only; the text area keeps the plain text.
    assert.equal(await page.locator('.ntball').count(), 1);
    assert.equal(await value(page), 'Plan\n- call bank\nplain - line');
    // Open -> white -> in progress, by clicking the ball.
    await page.locator('.ntball').click();
    await page.locator('.ntball').click();
    await settle(page);
    assert.equal(await page.locator('.ntball.ntb-in_progress').count(), 1);
    let posts = await page.evaluate(() => posts);
    assert.equal(posts.at(-1).blocks[0].text, `Plan\n-${S}in_progress${S} call bank\nplain - line`);
    assert.equal(await value(page), 'Plan\n- call bank\nplain - line');
    // Done strikes the line; right-click toggles blocked and back.
    await page.locator('.ntball').click();
    assert.equal(await page.locator('.agfocus-mline.done').count(), 1);
    await page.locator('.ntball').click({button: 'right'});
    assert.equal(await page.locator('.ntball.ntb-blocked').count(), 1);
    await settle(page);
    // The state survives a reload.
    await page.evaluate(() => { dispose(); });
    await mount();
    await page.evaluate(() => focusText.load(panel));
    assert.equal(await page.locator('.ntball.ntb-blocked').count(), 1);
    assert.equal(await value(page), 'Plan\n- call bank\nplain - line');
  } finally { await browser.close(); }
});

browserTest('typing a dash adds a ball, and a line keeps its ball while it is edited or moved down', async () => {
  const {browser, page} = await fixture({revision: 1, card_ids: [], blocks: [{id: '11111111-1111-4111-8111-111111111111', text: `-${S}done${S} buy milk\nend`, before_id: null, after_id: null}]});
  try {
    assert.equal(await page.locator('.ntball.ntb-done').count(), 1);
    const ta = page.locator('.agfocus-notes-input');
    await ta.click();
    await ta.press('Control+End');
    await ta.type('\n- second');
    assert.equal(await page.locator('.ntball').count(), 2);
    assert.equal(await page.locator('.ntball.ntb-done').count(), 1);
    // Editing the marked line keeps its state; a new line above it pushes it down with its ball.
    await ta.press('Control+Home');
    await ta.press('End');
    await ta.type(' now');
    await ta.press('Control+Home');
    await ta.press('Enter');
    await ta.press('ArrowUp');
    await ta.type('top');
    assert.equal(await page.locator('.ntball.ntb-done').count(), 1);
    assert.equal(await page.locator('.agfocus-mline.done').textContent(), '- buy milk now');
    await settle(page);
    assert.equal(await page.evaluate(() => saved.blocks[0].text), `top\n-${S}done${S} buy milk now\nend\n- second`);
  } finally { await browser.close(); }
});
