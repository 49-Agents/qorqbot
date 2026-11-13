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
  await page.addStyleTag({path: path.join(__dirname, '../css/agent-focus.css')});
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
  await page.addScriptTag({path: path.join(__dirname, '../js/focus-text.js')});
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
