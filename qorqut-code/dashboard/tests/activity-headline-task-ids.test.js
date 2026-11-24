'use strict';
// An activity headline drops the ids its entry references, a short Task id included, and keeps ordinary words.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const context = vm.createContext({});
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js', 'side.js'), 'utf8'), context);
const {logHeadline} = context;

test('a referenced short Task id is removed only where it stands alone', () => {
  assert.equal(logHeadline({summary: 'Closed ab3cd after review', refs: {task: 'ab3cd'}}), 'Closed after review');
  assert.equal(logHeadline({summary: 'ab3cd: brief sent', refs: {task: 'ab3cd'}}), 'brief sent');
  // The same letters inside a longer word or slug are not the id.
  assert.equal(logHeadline({summary: 'Updated xab3cd and ab3cd-notes', refs: {task: 'ab3cd'}}), 'Updated xab3cd and ab3cd-notes');
  // Without a Task reference nothing short is guessed at.
  assert.equal(logHeadline({summary: 'Closed ab3cd after review', refs: {}}), 'Closed ab3cd after review');
});

test('UUID and long referenced ids are still removed', () => {
  const uuid = '3f2c4d5e-1a2b-4c3d-8e9f-0a1b2c3d4e5f';
  assert.equal(logHeadline({summary: `Closed ${uuid} after review`, refs: {task: uuid}}), 'Closed after review');
  assert.equal(logHeadline({summary: 'Closed seo-autopilot now', refs: {task: 'seo-autopilot'}}), 'Closed now');
  assert.equal(logHeadline({summary: 'Closed a.b+c after review', refs: {task: 'a.b+c'}}), 'Closed after review');
});
