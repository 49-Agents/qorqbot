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
