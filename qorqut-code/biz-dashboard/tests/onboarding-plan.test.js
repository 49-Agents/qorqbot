'use strict';
// node --test biz-dashboard/tests/*.test.js
const assert = require('node:assert/strict');
const { test } = require('node:test');
const plan = require('../js/onboarding-plan.js');

const steps = [
  {id: 'assistant', chapter: 'online'}, {id: 'terminal', chapter: 'online'},
  {id: 'terminal-start', progressStep: 'terminal', chapter: 'online'}, {id: 'chat', chapter: 'online'},
  {id: 'focus', chapter: 'workspace'}, {id: 'calendar', chapter: 'workspace'},
  {id: 'learning', chapter: 'yours'},
];

test('chapters group consecutive steps', () => {
  assert.deepEqual(plan.chapters(steps), [
    {id: 'online', start: 0, end: 3}, {id: 'workspace', start: 4, end: 5}, {id: 'yours', start: 6, end: 6}]);
  assert.deepEqual(plan.chapters([]), []);
});

test('chapterAt reports the chapter number and the step within it', () => {
  assert.deepEqual(plan.chapterAt(steps, 5), {id: 'workspace', start: 4, end: 5, number: 2, total: 3, step: 2, count: 2});
  assert.equal(plan.chapterAt(steps, 0).step, 1);
  assert.equal(plan.chapterAt(steps, 6).count, 1);
});

test('nextChapter jumps to the next chapter start and ends after the last', () => {
  assert.equal(plan.nextChapter(steps, 1), 4);
  assert.equal(plan.nextChapter(steps, 4), 6);
  assert.equal(plan.nextChapter(steps, 6), -1);
});

test('resumeIndex continues an interrupted walkthrough at its saved step', () => {
  assert.equal(plan.resumeIndex(steps, {status: 'in_progress', step: 'focus'}), 4);
  // A step that shares saved progress resumes at the first step saved under that id.
  assert.equal(plan.resumeIndex(steps, {status: 'in_progress', step: 'terminal'}), 1);
});

test('resumeIndex starts over for new, finished or unknown progress', () => {
  for (const saved of [null, {status: 'new', step: 'focus'}, {status: 'completed', step: 'learning'},
    {status: 'skipped', step: 'focus'}, {status: 'in_progress', step: 'removed-step'}]) {
    assert.equal(plan.resumeIndex(steps, saved), 0);
  }
});

test('saveId uses the shared progress id when present', () => {
  assert.equal(plan.saveId(steps[2]), 'terminal');
  assert.equal(plan.saveId(steps[4]), 'focus');
});

test('the launch step reads the Chat session as checking, waiting, connecting or online', () => {
  assert.equal(plan.launchPhase(null), 'checking');
  assert.equal(plan.launchPhase({state: 'offline', reason: 'no_session'}), 'waiting');
  assert.equal(plan.launchPhase({state: 'live', can_send: false}), 'connecting');
  assert.equal(plan.launchPhase({state: 'live', can_send: true}), 'online');
});
