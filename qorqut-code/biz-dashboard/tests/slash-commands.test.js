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

test('only a bare /model, or another menu command, is finished in the Terminal', () => {
  const opens = (text, provider) => slash.opensTerminal(slash.command(catalog, text), text, provider);
  assert.equal(opens('/model', 'claude'), true);
  assert.equal(opens('/model   ', 'claude'), true);
  assert.equal(opens('/model sonnet', 'claude'), false);
  assert.equal(opens(' /MODEL\nopus', 'claude'), false);
  assert.equal(opens('/model sonnet', 'codex'), true);
  assert.equal(opens('/cost now', 'claude'), true);
  assert.equal(opens('/compact', 'claude'), false);
  assert.equal(opens('/unknown', 'claude'), false);
});

test('a known command is not sent to a shared Codex server', () => {
  const codex = [{name: '/compact', where: 'chat'}, {name: '/model', where: 'terminal'}];
  const shared = {provider: 'codex', transport: 'codex_queue'};
  const refusal = (session, text) => slash.sharedServerRefusal(session, slash.command(codex, text), 'Ada');
  assert.equal(refusal(shared, '/compact now'), '/compact runs only in Ada’s Terminal. Open Terminal and run it there.');
  assert.match(refusal(shared, '/model'), /^\/model runs only in Ada’s Terminal/);
  assert.equal(refusal(shared, '/tmp/foo'), null);
  assert.equal(refusal(shared, 'run /compact'), null);
  assert.equal(refusal({provider: 'codex', transport: 'terminal'}, '/compact'), null);
  assert.equal(refusal({provider: 'claude', transport: 'terminal'}, '/compact'), null);
  assert.equal(refusal(null, '/compact'), null);
});
