// node tests/test_founder_cutover_views.js — global agent workspace views after the Founder cutover.
// Pure source check: no browser, network or writes.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '..', 'js', 'agent-workspace.js'), 'utf8');
const start = source.indexOf('function agentWorkspaceViews'), end = source.indexOf('let agentWorkspaceDocument');
const ctx = vm.createContext({});
vm.runInContext(source.slice(start, end), ctx);
const views = (scope, role) => JSON.parse(JSON.stringify(ctx.agentWorkspaceViews(scope, role)));
const initial = role => ctx.agentWorkspaceDefault({workspaceScope: 'global', workspaceRole: role});

assert.deepEqual(views('global', 'ordubashy'), [], 'Ordubashy has no Project management pane');
assert(!views('global', 'ordubashy').includes('ideas'), 'Ordubashy has no Ideas view');
assert.equal(initial('ordubashy'), 'focus', 'Ordubashy still opens on Focus');
assert.deepEqual(views('global', 'learning-applier'), ['global_context']);
assert.equal(initial('learning-applier'), 'global_context');
assert.deepEqual(views('global', 'global-marketing'), [], 'the retired Global Marketing role has no workspace views');
assert.equal(initial('global-marketing'), null);
assert.deepEqual(views('global', 'founder'), [], 'a retired Founder has no workspace views');
assert.equal(initial('founder'), null);
assert.deepEqual(views('global', 'global-worker'), [], 'a general worker has no Project management pane');
assert.equal(initial('global-worker'), null, 'a general worker does not open on Project management');
assert.deepEqual(views('business', 'ordubashy'), []);
console.log('Founder cutover views: no Project management or Ideas, Focus default, Learning Applier and retired Founder passed.');
