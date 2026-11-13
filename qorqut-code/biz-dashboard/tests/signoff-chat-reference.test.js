'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const path = require('node:path');
const fs = require('node:fs');

const fixture = path.join(__dirname, 'fixtures/signoff-chat-reference.html');
const chrome = ['google-chrome', 'chromium', 'chromium-browser'].find(bin => spawnSync(bin, ['--version']).status === 0);

// Real signoffs.js, signoffs.css and Fastmail renderer in headless Chrome with a mocked API.
function run(extra = []) {
  const result = spawnSync(chrome, ['--headless=new', '--no-sandbox', '--disable-gpu', '--allow-file-access-from-files', '--window-size=600,1400',
    '--virtual-time-budget=5000', ...extra, 'file://' + fixture + (extra.some(a => a.startsWith('--screenshot')) ? '#shot' : '')], {encoding: 'utf8', maxBuffer: 1 << 26});
  return result.stdout;
}

test('chat Signoffs: full editors, saved-change age, compact actions and preserved feedback', {skip: !chrome && 'Chrome not installed'}, () => {
  const dom = run(['--dump-dom']);
  const out = JSON.parse(/<pre id="out"[^>]*>(.*?)<\/pre>/s.exec(dom)[1].replace(/&quot;/g, '"').replace(/&amp;/g, '&'));
  assert.equal(out.fullMessageVisible, true);
  assert.equal(out.fromVisible, true);
  assert.match(out.fromText, /me@example.com/);
  assert.equal(out.age, '5 min ago');
  assert.equal(out.ageSource, true);
  assert.equal(out.attentionRemoved, true);
  assert.equal(out.headerStatus, false);
  assert.equal(out.footerStatus, 'Needs review');
  assert.equal(out.editStatus, 'Editing draft');
  assert.equal(out.cancelLabel, 'Cancel');
  assert.deepEqual(out.editButtonSizes[0], out.editButtonSizes[1]);
  assert.equal(out.typingKeepsTime, true);
  assert.equal(out.cancelRestoresBody, true);
  assert.equal(out.cancelKeepsTime, true);
  assert.equal(out.refreshedAge, 'Just now');
  assert.equal(out.submittedTimestampRemoved, true);
  assert.equal(out.decisionNoteKept, true);
  assert.equal(out.completedContentVisible, true);
  assert.equal(out.completedStatus, true);
  assert.deepEqual(out.ageCases, ['Just now', '5 min ago', '1 hr ago', '1 d ago']);
  assert.equal(out.invalidAge, '');
  assert.equal(out.missingTimestamp, null);
  assert.equal(out.creationFallback, '2026-11-01T00:00:00.000Z');
  assert.deepEqual(out.effectsCollapsed, [false, false, false, false]);
  assert.equal(out.effectsExpanded, false);
  assert.deepEqual(out.effectsKeptInDom.slice(0, 3), [true, true, true]);
  assert.equal(out.effectsOutsideInlineVisible, true);
  assert.equal(out.expandedBody, true);
  assert.equal(out.expandedSubject, 'Quarterly plan');
  assert.match(out.attachments, /plan\.pdf/);
  assert.match(out.recipients, /ada@example\.com/);
  assert.equal(out.actionVisibleLabel, false);
  assert.match(out.actionAria, /Message to send with decision \(required to reject\)/);
  assert.match(out.actionPlaceholder, /reason to reject/);
  assert.equal(out.actionRows, 1);
  assert.ok(Math.abs(out.actionEmptyHeight - (out.actionLineHeight + 14)) < 1);
  assert.ok(out.actionGrownHeight > out.actionEmptyHeight + 2 * out.actionLineHeight);
  assert.equal(out.actionScrollFits, true);
  assert.equal(out.actionStillSame, true);
  assert.ok(out.narrowHeight > out.wideHeight);
  assert.equal(out.narrowFits, true);
  assert.equal(out.narrowSame, true);
  assert.equal(out.rejectBlocked, true);
  assert.equal(out.questionLabel, 'Answer (required)');
  assert.equal(out.questionRows, 1);
  assert.equal(out.questionSubmitPresent, true);
});

test('chat reference screenshot', {skip: !chrome && 'Chrome not installed'}, () => {
  if (!process.env.SIGNOFF_SCREENSHOT) return;
  fs.mkdirSync(path.dirname(process.env.SIGNOFF_SCREENSHOT), {recursive: true});
  run(['--screenshot=' + process.env.SIGNOFF_SCREENSHOT]);
});
