'use strict';
// node --test biz-dashboard/tests/*.test.js
const assert = require('node:assert/strict');
const { test } = require('node:test');
const slash = require('../js/slash-commands.js');

const catalog = [
  {name: '/compact', where: 'chat'}, {name: '/clear', where: 'chat'}, {name: '/model', where: 'terminal'},
  {name: '/cost', where: 'terminal'}, {name: '/context', where: 'terminal'}, {name: '/git:sync', where: 'chat', source: 'yours'},
];

test('the menu opens only while a command is being typed at the start', () => {
  assert.deepEqual(slash.match(catalog, '/c').map(i => i.name), ['/compact', '/clear', '/cost', '/context']);
  assert.deepEqual(slash.match(catalog, '/'), catalog.slice(0, slash.MAX_SHOWN));
  assert.deepEqual(slash.match(catalog, '/compact now'), []);
  assert.deepEqual(slash.match(catalog, 'hi /c'), []);
  assert.deepEqual(slash.match(catalog, ''), []);
});

test('names starting with the text come before names containing it', () => {
  assert.deepEqual(slash.match(catalog, '/sync').map(i => i.name), ['/git:sync']);
  assert.deepEqual(slash.match(catalog, '/co').map(i => i.name), ['/compact', '/cost', '/context']);
});

test('a sent text is matched to the command it runs', () => {
  assert.equal(slash.command(catalog, '/model').where, 'terminal');
  assert.equal(slash.command(catalog, ' /compact keep decisions').name, '/compact');
  assert.equal(slash.command(catalog, '/unknown'), null);
  assert.equal(slash.command(catalog, 'please /compact'), null);
});

test('no command opens or hands anything over to a terminal', () => {
  assert.equal(slash.opensTerminal, undefined);
  assert.equal(slash.managedRefusal, undefined);
});

test('a command the catalog marks unavailable is refused with plain words, and nothing else is', () => {
  const list = [{name: '/cost', where: 'unavailable'}, {name: '/compact', where: 'chat'}, {name: '/model', where: 'terminal'}];
  const refuse = text => slash.unavailableRefusal(slash.command(list, text), 'Ada');
  assert.equal(refuse('/cost'), '/cost isn’t available when Qorqbot runs Ada.');
  assert.equal(refuse('/compact'), null);
  assert.equal(refuse('/model sonnet'), null);
  assert.equal(refuse('/unknown'), null);
  assert.equal(refuse('hello'), null);
  assert.equal(slash.refusal({}, list[0], 'Ada'), '/cost isn’t available when Qorqbot runs Ada.');
  assert.doesNotMatch(refuse('/cost'), /terminal|tmux|CLI/i);
});

test('a known command is not sent to a shared Codex server', () => {
  const codex = [{name: '/compact', where: 'chat'}, {name: '/model', where: 'terminal'}];
  const shared = {provider: 'codex', transport: 'codex_queue'};
  const refusal = (session, text) => slash.sharedServerRefusal(session, slash.command(codex, text), 'Ada');
  assert.equal(refusal(shared, '/compact now'), '/compact isn’t available when Qorqbot runs Ada.');
  assert.match(refusal(shared, '/model'), /^\/model isn’t available when Qorqbot runs Ada\.$/);
  assert.equal(refusal(shared, '/tmp/foo'), null);
  assert.equal(refusal(shared, 'run /compact'), null);
  assert.equal(refusal({provider: 'codex', transport: 'terminal'}, '/compact'), null);
  assert.equal(refusal({provider: 'claude', transport: 'terminal'}, '/compact'), null);
  assert.equal(refusal(null, '/compact'), null);
});
