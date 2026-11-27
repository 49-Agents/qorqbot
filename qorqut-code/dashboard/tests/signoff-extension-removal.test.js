'use strict';
// The removal of a downloaded extension: the card's two buttons, "Remove" (the ordinary approval) and "Remove and delete
// its data" (the owner's edit of the proposal, then the approval of the edited one), both named by the proposal's preview.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const path = require('node:path');
const fs = require('node:fs');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const chrome = ['/opt/google/chrome/chrome', '/usr/bin/google-chrome'].some(file => fs.existsSync(file));
const executable = process.env.CHROME_PATH || (chrome ? null : chromium?.executablePath());
const available = !!chromium && (chrome || (!!executable && fs.existsSync(executable)));
const launch = () => chromium.launch({headless: true, ...(executable ? {executablePath: executable} : {channel: 'chrome'}),
  args: ['--allow-file-access-from-files']});
const browserTest = (name, run) => test(name, {skip: !available && 'No local Chrome or Playwright Chromium'}, run);
const fixture = pathToFileURL(path.join(__dirname, 'fixtures/signoff-extension-removal.html')).href;

async function open(run) {
  const browser = await launch();
  try {
    const page = await browser.newPage({viewport: {width: 800, height: 700}}), errors = [];
    page.setDefaultTimeout(6000);
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(fixture);
    await run(page);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
}

async function ready(page, id) {
  await page.evaluate(id => openItem(id), id);
  await page.waitForSelector(`.sg-row[data-id="${id}"] .sg-detail-footer [data-sg="approve"]:not([disabled])`);
}

const buttons = (page, id) => page.evaluate(id => [...document.querySelectorAll(`.sg-row[data-id="${id}"] .sg-detail-footer .sg-actions button`)]
  .map(b => [b.textContent, b.dataset.sg, b.classList.contains('sg-primary-action')]), id);
const calls = page => page.evaluate(() => testHarness.calls.map(c => ({url: c.url, body: c.body})));

browserTest('the removal card has Remove, Remove and delete its data, and Reject; other cards are as before', () => open(async page => {
  await ready(page, 'removal');
  assert.deepEqual(await buttons(page, 'removal'), [['Remove', 'approve', true], ['Remove and delete its data', 'amend-approve', false],
    ['Reject', 'reject', false]]);
  // The card says what happens on approval; the two buttons say what happens to the private data.
  assert.match(await page.locator('.sg-row[data-id="removal"]').textContent(), /Qorqbot restarts once\. Projects, tables and records it created stay\./);
  assert.doesNotMatch(await page.locator('.sg-row[data-id="removal"]').textContent(), /private data stays/);
  await ready(page, 'plain');
  assert.deepEqual(await buttons(page, 'plain'), [['Approve', 'approve', true], ['Reject', 'reject', false]]);
}));

browserTest('Remove sends the ordinary approval of the proposal as it is', () => open(async page => {
  await ready(page, 'removal');
  await page.click('.sg-row[data-id="removal"] .sg-detail [data-sg="approve"]');
  await page.waitForFunction(() => testHarness.items.removal.state === 'approved');
  const sent = await calls(page);
  assert.deepEqual(sent.map(c => c.url), ['/root/signoffs/removal/approve']);
  assert.equal(sent[0].body.payload_hash, 'hremoval');
}));

browserTest('Remove and delete its data edits the proposal, then approves the edited one in one click', () => open(async page => {
  await ready(page, 'removal');
  await page.fill('.sg-row[data-id="removal"] textarea.sg-feedback', 'Clean it out.');
  await page.click('.sg-row[data-id="removal"] .sg-detail [data-sg="amend-approve"]');
  await page.waitForFunction(() => testHarness.items['removal-edited']?.state === 'approved');
  const sent = await calls(page);
  assert.deepEqual(sent.map(c => c.url), ['/root/signoffs/removal/amend', '/root/signoffs/removal-edited/approve']);
  assert.deepEqual(sent[0].body.changes, {delete_data: true});
  assert.deepEqual([sent[0].body.proposal_revision, sent[0].body.payload_hash, sent[0].body.expected_status_revision], [1, 'hremoval', 1]);
  assert.deepEqual([sent[1].body.payload_hash, sent[1].body.feedback], ['hremoval-edited', 'Clean it out.']);
  await page.waitForSelector('.sg-row[data-id="removal-edited"]');
  assert.equal(await page.locator('.sg-row[data-id="removal"]').count(), 0, 'the old proposal is gone from the card');
}));

browserTest('when the edit is saved and the approval fails, the card shows the edited proposal waiting', () => open(async page => {
  await page.evaluate(() => { testHarness.flags.failApprove = true; });
  await ready(page, 'removal');
  await page.click('.sg-row[data-id="removal"] .sg-detail [data-sg="amend-approve"]');
  await page.waitForFunction(() => testHarness.calls.length === 2);
  await page.waitForSelector('.sg-row[data-id="removal-edited"]');
  const state = await page.evaluate(() => ({edited: testHarness.items['removal-edited'].state, old: document.querySelectorAll('.sg-row[data-id="removal"]').length,
    text: document.querySelector('.sg-row[data-id="removal-edited"]').textContent}));
  assert.deepEqual([state.edited, state.old], ['pending', 0]);
  assert.match(state.text, /Qorqbot restarts once\. Projects, tables and records it created stay\./);
  // Reloading it offers the ordinary decision on the edited proposal, whose approval says the data is deleted too.
  await page.evaluate(() => { testHarness.flags.failApprove = false; });
  await ready(page, 'removal-edited');
  assert.deepEqual(await buttons(page, 'removal-edited'), [['Remove and delete its data', 'approve', true], ['Reject', 'reject', false]]);
}));

browserTest('when the edit fails, nothing is approved and the card stays the proposal as it was', () => open(async page => {
  await page.evaluate(() => { testHarness.flags.failAmend = true; });
  await ready(page, 'removal');
  await page.click('.sg-row[data-id="removal"] .sg-detail [data-sg="amend-approve"]');
  await page.waitForFunction(() => testHarness.calls.length === 1);
  await page.waitForTimeout(200);
  assert.deepEqual((await calls(page)).map(c => c.url), ['/root/signoffs/removal/amend']);
  assert.equal(await page.evaluate(() => testHarness.items.removal.state), 'pending');
  assert.equal(await page.locator('.sg-row[data-id="removal"]').count(), 1);
}));
