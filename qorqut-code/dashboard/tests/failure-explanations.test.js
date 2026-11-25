// Failed requests tell the owner what happened and what to do, without internal vocabulary.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname, '../js/core.js'), 'utf8');
const section = (from, to) => {
  const start = source.indexOf(from), end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `${from} not found`);
  return source.slice(start, end);
};
const context = {};
vm.createContext(context);
vm.runInContext(`${section('const FAILURE_RESTART', 'function newRequestId')}
${section('function coreData', '// A read that fails')}
${section('function uiFailure', '// One shared owner-session read')}
Object.assign(globalThis, {explainFailure, failureError, uiFailure, coreData, FAILURE_CHANGED});`, context);
const {explainFailure, failureError, uiFailure, coreData, FAILURE_CHANGED} = context;

test('owner-written server messages pass through', () => {
  assert.equal(explainFailure('validation_error', 'Enter a project name.', 422), 'Enter a project name.');
  assert.equal(explainFailure('not_found', 'Task is no longer available.', 404), 'Task is no longer available.');
});

test('internal wording is replaced', () => {
  for (const message of ['Use action=repair to start it', 'browser_not_ready', 'Endpoint is not registered',
    'Read /private/tmp/x/y failed', 'Invalid principal', 'Traceback (most recent call last)']) {
    const text = explainFailure('not_found', message, 404);
    assert.notEqual(text, message);
    assert.match(text, /no longer exists/);
  }
});

test('an unknown code with internal wording still says what to do', () => {
  assert.match(explainFailure('invalid_plan', 'plan_step failed', 400), /Refresh the page and try again.*restart Qorqbot/);
});

test('server failures never show their message', () => {
  assert.match(explainFailure('validation_error', 'Enter a project name.', 500), /went wrong inside Qorqbot/);
});

test('conflicts explain that something changed', () => {
  for (const code of ['revision_conflict', 'operation_conflict', 'stale_worker']) assert.equal(explainFailure(code, 'x', 409), FAILURE_CHANGED);
  assert.equal(uiFailure({code: 'revision_conflict', message: 'x', httpStatus: 409}), FAILURE_CHANGED);
});

test('a lost connection and an unreadable reply are named', () => {
  assert.match(failureError('network_unavailable').message, /isn't responding.*still running/);
  const unreadable = failureError('invalid_response', 502);
  assert.match(unreadable.message, /couldn't read/);
  assert.equal(unreadable.code, 'invalid_response');
  assert.equal(unreadable.httpStatus, 502);
});

test('uiFailure states what failed and why', () => {
  assert.equal(uiFailure({code: 'validation_error', message: 'Enter a project name.', httpStatus: 422}), "Couldn't save this. Enter a project name.");
  assert.match(uiFailure({code: 'not_found', message: 'gone', httpStatus: 404}, {load: true}), /^Couldn't load this\. gone/);
  assert.match(uiFailure(failureError('network_unavailable')), /^Couldn't confirm this\. Qorqbot isn't responding/);
  assert.equal(uiFailure({code: 'signoff_required', message: 'Needs your signoff.'}), 'Needs your signoff.');
});

test('coreData keeps the code and explains the message', () => {
  const result = {status: 'error', httpStatus: 404, coreError: {code: 'not_found', message: 'Endpoint is not registered'}, error: 'x'};
  assert.throws(() => coreData(result), error => error.code === 'not_found' && /no longer exists/.test(error.message));
  assert.throws(() => coreData(null), error => error.code === 'invalid_response');
});

test('translating twice changes nothing', () => {
  for (const code of ['network_unavailable', 'invalid_response', 'forbidden', 'not_found', 'busy', 'invalid_plan']) {
    const once = explainFailure(code, '', 400);
    assert.equal(explainFailure(code, once, 400), once);
  }
});
