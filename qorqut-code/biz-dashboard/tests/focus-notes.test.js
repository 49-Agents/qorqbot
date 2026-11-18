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
  for (const file of ['status-ball.css', 'notes.css', 'agent-focus.css']) await page.addStyleTag({path: path.join(__dirname, '../css', file)});
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
