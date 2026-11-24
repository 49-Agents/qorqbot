'use strict';
// node --test dashboard/tests/*.test.js
const assert = require('node:assert/strict');
const { test } = require('node:test');
const slash = require('../js/slash-commands.js');

const catalog = [
  {name: '/compact', where: 'chat'}, {name: '/clear', where: 'chat'}, {name: '/model', where: 'unavailable'},
  {name: '/cost', where: 'unavailable'}, {name: '/context', where: 'unavailable'}, {name: '/git:sync', where: 'chat', source: 'yours'},
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
  assert.equal(slash.command(catalog, '/model').where, 'unavailable');
  assert.equal(slash.command(catalog, ' /compact keep decisions').name, '/compact');
  assert.equal(slash.command(catalog, '/unknown'), null);
  assert.equal(slash.command(catalog, 'please /compact'), null);
});

test('no command opens or hands anything over to a terminal', () => {
  assert.equal(slash.opensTerminal, undefined);
  assert.equal(slash.managedRefusal, undefined);
});

test('a command the catalog marks unavailable is refused with plain words, and nothing else is', () => {
  const list = [{name: '/cost', where: 'unavailable'}, {name: '/compact', where: 'chat'}, {name: '/model', where: 'chat'}];
  const refuse = text => slash.unavailableRefusal(slash.command(list, text), 'Ada');
  assert.equal(refuse('/cost'), '/cost isn’t available when Qorqbot runs Ada.');
  assert.equal(refuse('/compact'), null);
  assert.equal(refuse('/model sonnet'), null);
  assert.equal(refuse('/unknown'), null);
  assert.equal(refuse('hello'), null);
  assert.equal(slash.refusal({}, list[0], 'Ada'), '/cost isn’t available when Qorqbot runs Ada.');
  assert.doesNotMatch(refuse('/cost'), /terminal|tmux|CLI/i);
});

// Terminals mode lists a menu command again as an entry to finish in the Terminal (the catalog passes through js/agent-terminal-view.js).
test('the menu tags a command that finishes in the Terminal, and an unavailable one stays tagged unavailable', () => {
  const terminalView = require('../js/agent-terminal-view.js');
  const menus = [];
  global.document = {activeElement: null, createElement: () => { const menu = {setAttribute() {}, remove() {}, addEventListener() {}, querySelector: () => null}; menus.push(menu); return menu; }};
  const listeners = {};
  const input = {value: '/c', setAttribute() {}, removeAttribute() {}, addEventListener: (type, fn) => { listeners[type] = fn; }, removeEventListener() {}};
  const draw = list => {
    const menu = menus.length;
    slash.attach({input, form: {prepend() {}}, commands: () => list});
    listeners.input();
    return menus[menu].innerHTML;
  };
  try {
    const managedMenu = draw(catalog);
    assert.match(managedMenu, /slash-tag" title="Not available when Qorqbot runs the agent">Not available/);
    assert.doesNotMatch(managedMenu, />Terminal</);
    const terminalMenu = draw(terminalView.catalog(catalog));
    assert.match(terminalMenu, /slash-tag" title="Opens a menu you finish in the agent’s Terminal">Terminal/);
    assert.doesNotMatch(terminalMenu, /Not available/);
    assert.equal(slash.refusal({execution: 'terminal'}, terminalView.catalog(catalog)[2], 'Ada'), null, 'a Terminal entry is never refused');
  } finally { delete global.document; }
});
