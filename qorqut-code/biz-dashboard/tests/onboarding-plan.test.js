'use strict';
// node --test biz-dashboard/tests/*.test.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
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

test('resumeIndex maps the retired activity step to calendar unless activity exists', () => {
  assert.equal(plan.resumeIndex(steps, {status: 'in_progress', step: 'activity'}), 5);
  const withActivity = [...steps.slice(0, 5), {id: 'activity', chapter: 'workspace'}, ...steps.slice(5)];
  assert.equal(plan.resumeIndex(withActivity, {status: 'in_progress', step: 'activity'}), 5);
  assert.equal(plan.resumeIndex(steps, {status: 'completed', step: 'activity'}), 0);
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

// Key handling: a stand-in for the tour's DOM. An element is a name; `inside` lists the ones the highlighted area holds.
function tour({step, inside = [], focusables = ['next', 'skip'], active = 'next'}) {
  const calls = {exits: 0, focused: []};
  const elements = Object.fromEntries(focusables.map(name => [name, {name, focus: () => calls.focused.push(name)}]));
  const target = {contains: node => inside.includes(node)};
  const state = {step, target, active: elements[active], focusables: () => focusables.map(name => elements[name]),
    exit: () => { calls.exits++; }};
  const press = (key, {from = 'outside', shiftKey = false} = {}) => {
    const event = {key, shiftKey, target: from, prevented: false, stopped: false,
      preventDefault() { this.prevented = true; }, stopImmediatePropagation() { this.stopped = true; }};
    return {event, result: plan.handleKey(event, state)};
  };
  return {press, calls};
}
const untouched = (event, calls) => {
  assert.equal(event.prevented, false);
  assert.equal(event.stopped, false);
  assert.equal(calls.exits, 0);
  assert.deepEqual(calls.focused, []);
};

test('the terminal step leaves Esc and Tab to the highlighted terminal', () => {
  const {press, calls} = tour({step: {id: 'terminal', terminal: true}, inside: ['xterm']});
  for (const [key, shiftKey] of [['Escape', false], ['Tab', false], ['Tab', true]]) {
    const {event, result} = press(key, {from: 'xterm', shiftKey});
    assert.equal(result, 'pass');
    untouched(event, calls);
  }
});

test('the terminal step still lets the tour have Esc and Tab outside the terminal', () => {
  const {press, calls} = tour({step: {id: 'terminal', terminal: true}, inside: ['xterm']});
  const {event, result} = press('Escape');
  assert.equal(result, 'exit');
  assert.deepEqual([event.prevented, event.stopped, calls.exits], [true, true, 1]);
  assert.equal(press('Tab').result, 'tab');
  assert.deepEqual(calls.focused, ['skip']);
});

test('every other step keeps Esc and Tab for the tour, even inside its highlighted area', () => {
  for (const step of [{id: 'chat'}, {id: 'focus', view: 'focus'}, {id: 'start', consent: true}, {id: 'launch', launch: true}]) {
    const {press, calls} = tour({step, inside: ['composer']});
    const escape = press('Escape', {from: 'composer'});
    assert.equal(escape.result, 'exit', step.id);
    assert.deepEqual([escape.event.prevented, escape.event.stopped, calls.exits], [true, true, 1], step.id);
    const tab = press('Tab', {from: 'composer'});
    assert.equal(tab.result, 'tab', step.id);
    assert.equal(tab.event.prevented, true, step.id);
    assert.deepEqual(calls.focused, ['skip'], step.id);
  }
});

test('Tab cycles through the tour and wraps in both directions', () => {
  const {press, calls} = tour({step: {id: 'chat'}, focusables: ['a', 'b', 'c'], active: 'c'});
  press('Tab');
  press('Tab', {shiftKey: true});
  assert.deepEqual(calls.focused, ['a', 'b']);
  const first = tour({step: {id: 'chat'}, focusables: ['a', 'b', 'c'], active: 'a'});
  first.press('Tab', {shiftKey: true});
  assert.deepEqual(first.calls.focused, ['c']);
});

test('other keys and an empty Tab list are left alone on every step', () => {
  for (const step of [{id: 'chat'}, {id: 'terminal', terminal: true}, undefined]) {
    const {press, calls} = tour({step, inside: ['composer']});
    for (const key of ['Enter', 'a', 'ArrowDown']) {
      const {event, result} = press(key, {from: 'composer'});
      assert.equal(result, 'pass');
      untouched(event, calls);
    }
    const empty = tour({step, focusables: []});
    const {event, result} = empty.press('Tab');
    assert.equal(result, 'pass');
    untouched(event, empty.calls);
  }
});

test('only the terminal step is flagged to keep Esc and Tab, and the retired Activity step stays gone', () => {
  const source = fs.readFileSync(path.join(__dirname, '../js/onboarding.js'), 'utf8');
  assert.equal(source.match(/\bterminal: true/g).length, 1);
  assert.match(source, /\{id: 'terminal'[^\n]*\bterminal: true/);
  assert.doesNotMatch(source, /\{id: 'activity'/);
  assert.match(source, /onboardingPlan\.handleKey\(/);
});
