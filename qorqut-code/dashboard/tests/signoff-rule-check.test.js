'use strict';
// The owner's check of a rule an agent filed from his words: the graduation-cap chip, and the card in Focus with the rule as
// its one editable field, whom it applies to, and the email Signoff's footer.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {pathToFileURL} = require('node:url');
const path = require('node:path');
const fs = require('node:fs');
const vm = require('node:vm');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
// The installed Chrome when there is one (CHROME_PATH or the "chrome" channel), else Playwright's own Chromium.
const chrome = ['/opt/google/chrome/chrome', '/usr/bin/google-chrome'].some(file => fs.existsSync(file));
const executable = process.env.CHROME_PATH || (chrome ? null : chromium?.executablePath());
const available = !!chromium && (chrome || (!!executable && fs.existsSync(executable)));
const launch = () => chromium.launch({headless: true, ...(executable ? {executablePath: executable} : {channel: 'chrome'}),
  args: ['--allow-file-access-from-files']});
const browserTest = (name, run) => test(name, {skip: !available && 'No local Chrome or Playwright Chromium'}, run);
const fixture = pathToFileURL(path.join(__dirname, 'fixtures/signoff-rule-check.html')).href;
const CAP = 'M12 4.5 2.5 9 12 13.5 21.5 9 12 4.5ZM6.5 11.2v4.1c0 1.5 2.5 2.9 5.5 2.9s5.5-1.4 5.5-2.9v-4.1M21.5 9v5.4M20.7 14.4h1.6l.4 2.1h-2.4z';

const queueSource = fs.readFileSync(path.join(__dirname, '..', 'js', 'attention-queue.js'), 'utf8');
function chip() {
  const body = queueSource.slice(queueSource.indexOf('  const messageOperations='), queueSource.indexOf('  function paint(host){'));
  const context = {window: {}, esc: s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])),
    T_VIEW_ICON: {}, CHAT_VIEW_ICONS: {}, SG_BELL: ''};
  vm.createContext(context);
  vm.runInContext(`(()=>{${body}\nthis.api={signoffIcon,label};})()`, context);
  return context.api;
}

test('a rule check chip wears the graduation cap on the 24 grid and other chips keep theirs', () => {
  const q = chip();
  const icon = q.signoffIcon({kind: 'signoff', id: 's1', title: 'Check your rule', operation_name: 'experience_rule_check'});
  assert.match(icon, /data-signoff-kind="rule"/);
  assert.match(icon, /viewBox="0 0 24 24"[^>]*stroke-width="1.7"[^>]*stroke-linejoin="round" stroke-linecap="round"/);
  assert.ok(icon.includes(`<path d="${CAP}"/>`), 'the graduation cap path');
  assert.equal(q.label({kind: 'signoff', title: 'Check your rule', operation_name: 'experience_rule_check'}), 'Review · Check your rule');
  assert.match(q.signoffIcon({kind: 'signoff', operation_name: 'generic_change'}), /data-signoff-kind="approval"/);
  // Read and question rows keep their own icon even if they ever carried this operation's name.
  assert.match(q.signoffIcon({kind: 'signoff', operation_name: 'experience_rule_check', to_read: true}), /data-signoff-kind="read"/);
});

async function ready(page, id) {
  await page.evaluate(id => openItem(id), id);
  await page.waitForSelector(`.sg-row[data-id="${id}"] .sg-detail-footer [data-sg="reject"]:not([disabled])`);
}

browserTest('the card shows the notice, the rule as filed, whom it applies to and the email footer', async () => {
  const browser = await launch();
  try {
    const page = await browser.newPage({viewport: {width: 800, height: 700}}), errors = [];
    page.setDefaultTimeout(6000);
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(fixture);
    await page.addScriptTag({path: path.join(__dirname, '../js/attention-queue.js')});
    await page.evaluate(() => { const host = document.createElement('div'); host.className = 'focus-signoff-queue'; document.querySelector('.agfocus').prepend(host); attentionQueue.mountSignoffs(host); });
    const chipEl = page.locator('[data-signoff-key="rule"]');
    await chipEl.waitFor();
    assert.equal(await chipEl.locator('.attention-signoff').getAttribute('data-signoff-kind'), 'rule');
    assert.equal(await chipEl.getAttribute('aria-label'), 'Review Check your rule');
    await chipEl.click();
    await page.waitForSelector('.sg-row[data-id="rule"] .sg-detail-footer [data-sg="approve"]:not([disabled])');
    const card = await page.evaluate(() => {
      const row = document.querySelector('.sg-row[data-id="rule"]'), section = row.querySelector('.sg-rule-check');
      return {title: row.querySelector('.sg-rowhead')?.textContent || '', notice: section.querySelector('.sg-rule-notice').textContent,
        labels: [...section.querySelectorAll('.sg-content-label')].map(l => l.textContent),
        text: section.querySelector('textarea[data-sg-edit-field="text"]')?.value, textLabel: section.querySelector('[data-sg-edit-field="text"]').getAttribute('aria-label'),
        editable: section.querySelector('[data-sg-edit-field="text"]').dataset.editable,
        applies: section.querySelector('[data-sg-field="applies to"] .sg-content-value').textContent,
        injected: row.querySelectorAll('.sg-rule-check b, .sg-rule-check i').length,
        generic: row.querySelectorAll('.sg-changes-wrap').length, fields: row.querySelectorAll('[data-sg-edit-field]').length,
        buttons: [...row.querySelectorAll('.sg-detail-footer .sg-actions button')].map(b => [b.textContent, b.dataset.sg, b.classList.contains('sg-primary-action')]),
        note: row.querySelector('textarea.sg-feedback').getAttribute('aria-label'), compactFlag: window.signoffMessageRenderer('experience_rule_check').message,
        overflow: document.documentElement.scrollWidth > innerWidth};
    });
    assert.match(card.title, /Check your rule/);
    assert.equal(card.notice, 'Worker <i> filed this rule from your words. It is already in the Learning Applier’s queue; no instruction changes until the weekly review.');
    assert.deepEqual(card.labels, ['Rule as filed', 'Applies to']);
    assert.equal(card.text, 'Never send an email without a Signoff.\nAsk me first, every time.');
    assert.equal(card.textLabel, 'Rule as filed');
    assert.equal(card.editable, 'true');
    assert.equal(card.applies, 'all agents <b>');
    assert.equal(card.injected, 0);
    assert.equal(card.generic, 0, 'no generic change list under the card');
    assert.equal(card.fields, 1, 'the rule is the one editable field');
    assert.deepEqual(card.buttons, [['Approve', 'approve', true], ['Reject', 'reject', false]]);
    assert.equal(card.note, 'Message to send with decision (optional)');
    assert.equal(card.compactFlag, undefined, 'a chat card stays compact');
    assert.equal(card.overflow, false);
    // A note makes Reject the main button, as on an email.
    await page.fill('.sg-row[data-id="rule"] textarea.sg-feedback', 'Not quite what I said.');
    await page.waitForFunction(() => document.querySelector('.sg-row[data-id="rule"] .sg-detail [data-sg="reject"]').classList.contains('sg-primary-action'));
    assert.equal(await page.locator('.sg-row[data-id="rule"] .sg-detail [data-sg="approve"]').evaluate(b => b.classList.contains('sg-primary-action')), false);
    await page.click('.sg-row[data-id="rule"] .sg-detail [data-sg="reject"]');
    const calls = await page.evaluate(() => testHarness.calls.map(c => ({url: c.url, body: c.body})));
    assert.equal(calls.length, 1);
    assert.match(calls[0].url, /\/rule\/reject$/);
    assert.equal(calls[0].body.feedback, 'Not quite what I said.');
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

browserTest('Reject needs no note, and an edited rule is saved as an amendment the owner then approves', async () => {
  const browser = await launch();
  try {
    const page = await browser.newPage({viewport: {width: 380, height: 700}}), errors = [];
    page.setDefaultTimeout(6000);
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(fixture);
    await page.evaluate(() => { document.body.style.width = 'auto'; document.querySelector('.agfocus').style.width = '340px'; });
    await ready(page, 'other');
    await page.click('.sg-row[data-id="other"] .sg-detail [data-sg="reject"]');
    let calls = await page.evaluate(() => testHarness.calls.map(c => ({url: c.url, body: c.body})));
    assert.deepEqual([calls.length, /\/other\/reject$/.test(calls[0].url), calls[0].body.feedback], [1, true, null]);
    await page.evaluate(() => testHarness.releaseWrite());

    await ready(page, 'rule');
    const field = page.locator('.sg-row[data-id="rule"] textarea[data-sg-edit-field="text"]');
    await field.focus();
    await page.waitForSelector('.sg-row[data-id="rule"] .sg-detail [data-sg="save-message"]');
    // A blank rule is refused before any request.
    await field.fill('   ');
    await page.click('.sg-row[data-id="rule"] .sg-detail [data-sg="save-message"]');
    await page.waitForFunction(() => /1 to 2,000 characters/.test(document.querySelector('.sg-row[data-id="rule"] .sg-notice')?.textContent || ''));
    await field.fill('Never send any email or message without a Signoff.');
    await page.click('.sg-row[data-id="rule"] .sg-detail [data-sg="save-message"]');
    await page.waitForSelector('.sg-row[data-id="rule-edited"] .sg-detail-footer [data-sg="approve"]:not([disabled])');
    calls = await page.evaluate(() => testHarness.calls.map(c => ({url: c.url, body: c.body})));
    const amend = calls.at(-1);
    assert.match(amend.url, /\/rule\/amend$/);
    assert.deepEqual(amend.body.changes, {text: 'Never send any email or message without a Signoff.'});
    assert.equal(await page.locator('.sg-row[data-id="rule-edited"] textarea[data-sg-edit-field="text"]').inputValue(), 'Never send any email or message without a Signoff.');
    await page.click('.sg-row[data-id="rule-edited"] .sg-detail [data-sg="approve"]');
    calls = await page.evaluate(() => testHarness.calls.map(c => c.url));
    assert.match(calls.at(-1), /\/rule-edited\/approve$/);
    const overflow = await page.evaluate(() => [...document.querySelectorAll('.sg-row[data-id="rule-edited"] *')].some(e => e.getBoundingClientRect().right > document.querySelector('.agfocus').getBoundingClientRect().right + 1));
    assert.equal(overflow, false);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

browserTest('a rule check whose rule is missing cannot be approved from the card', async () => {
  const browser = await launch();
  try {
    const page = await browser.newPage({viewport: {width: 800, height: 700}}), errors = [];
    page.setDefaultTimeout(6000);
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(fixture);
    await page.evaluate(() => openItem('broken'));
    await page.waitForSelector('.sg-row[data-id="broken"] .sg-unavailable');
    assert.match(await page.locator('.sg-row[data-id="broken"] .sg-unavailable').textContent(), /The exact rule could not be shown/);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('the Observations page shows the owner\'s mark on a checked rule and none on other observations', () => {
  const source = fs.readFileSync(path.join(__dirname, '..', 'js', 'overseer.js'), 'utf8');
  const body = source.slice(source.indexOf('  const CHECKS = '), source.indexOf('  function proposals(filter)'));
  const context = {e: s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c])),
    when: value => value, KINDS: {owner_complaint: 'Owner rule'}, STATES: {}};
  vm.createContext(context);
  vm.runInContext(`(()=>{${body}\nthis.observationHtml=observationHtml;})()`, context);
  const row = {subject: 'all agents', kind: 'owner_complaint', filed_at: 'today', text: 'Never send mail unasked.', evidence: [], delta_proposals: []};
  for (const [check, words] of [['unchecked', 'not checked by you'], ['approved', 'approved by you'], ['rejected', 'rejected by you']])
    assert.ok(context.observationHtml({...row, owner_check: check}).includes(`<span class="overseer-check" data-owner-check="${check}">${words}</span>`), check);
  assert.doesNotMatch(context.observationHtml(row), /overseer-check/);
  assert.doesNotMatch(context.observationHtml({...row, owner_check: '<b>'}), /overseer-check/);
});
