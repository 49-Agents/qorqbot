'use strict';
// The per-agent client, model and effort control (js/agent-options.js). node --test tests/agent-options.test.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const options = require('../js/agent-options.js');

const view = (over = {}) => ({client: null, model: null, effort: null, effective_client: 'claude',
  clients: {claude: {ready: true}, codex: {ready: false}}, session_open: false, ...over});
const failure = (code, httpStatus = 400, message = '') => Object.assign(new Error(message), {code, httpStatus});
const agents = [{agent_id: 'a1', name: 'Ordubashy'}, {agent_id: 'a2', name: 'Worker'}];

test('each field becomes the request body the server expects; empty clears', () => {
  assert.deepEqual(options.body('client', 'codex'), {body: {client: 'codex'}});
  assert.deepEqual(options.body('client', ''), {body: {client: null}});
  assert.deepEqual(options.body('model', '  opus '), {body: {model: 'opus'}});
  assert.deepEqual(options.body('model', ''), {body: {model: null}});
  assert.deepEqual(options.body('effort', 'xhigh'), {body: {effort: 'xhigh'}});
  assert.deepEqual(options.body('effort', ''), {body: {effort: null}});
  for (const effort of ['minimal', 'low', 'medium', 'high', 'xhigh', 'max']) assert.deepEqual(options.body('effort', effort), {body: {effort}});
});

test('values the server would refuse never leave the page', () => {
  assert.ok(options.body('client', 'gemini').error);
  assert.ok(options.body('effort', 'huge').error);
  assert.ok(options.body('model', '-bad').error);
  assert.ok(options.body('model', 'has space').error);
  assert.ok(options.body('model', 'a'.repeat(101)).error);
  assert.deepEqual(options.body('model', 'claude-opus-4:1m[x]/y.z'), {body: {model: 'claude-opus-4:1m[x]/y.z'}});
  assert.ok(options.body('colour', 'x').error);
});

test('a refused change of client says the session must be ended first', () => {
  assert.match(options.refusal(failure('session_open', 409)), /session is open.*End the session.*first/i);
  assert.match(options.refusal(failure('invalid', 400, 'Choose Claude Code or Codex.')), /Choose Claude Code or Codex/);
  assert.match(options.refusal(failure('x', 404)), /isn’t available/);
  assert.match(options.refusal(null, 'model'), /model could not be saved/);
});

test('the row shows which clients are ready, marks the others and still lets them be chosen', () => {
  const m = options.rowModel(view({effective_client: 'claude'}));
  assert.deepEqual(m.choices.map(c => [c.value, c.label]), [['', 'Automatic (Claude Code)'], ['claude', 'Claude Code'], ['codex', 'Codex (not connected)']]);
  assert.equal(m.notice, '');
  const set = options.rowModel(view({client: 'codex', model: 'gpt-6-luna', effort: 'high'}));
  assert.deepEqual([set.client, set.model, set.effort], ['codex', 'gpt-6-luna', 'high']);
  assert.match(set.notice, /Codex isn’t connected yet/);
  assert.deepEqual(set.suggestions, options.SUGGESTIONS.codex);
  assert.deepEqual(options.rowModel(view()).suggestions, options.SUGGESTIONS.claude);
  assert.deepEqual([options.rowModel({}).client, options.rowModel(null).model, options.rowModel(undefined).effort], ['', '', '']);
});

function fake({views = {}, reads = [], writes = []} = {}) {
  return {read: async id => { reads.push(id); const v = views[id]; if (v instanceof Error) throw v; return v; },
    write: async (id, change) => { writes.push([id, change]); const v = views[id]; if (v instanceof Error) throw v; return {...v, ...change}; }};
}

test('the controller reads every agent and sends only the changed field', async () => {
  const reads = [], writes = [];
  const io = fake({views: {a1: view(), a2: view({model: 'opus'})}, reads, writes});
  const ctl = options.controller({agents, ...io});
  await ctl.load();
  assert.deepEqual(reads.sort(), ['a1', 'a2']);
  assert.equal(ctl.state.loading, false); assert.equal(ctl.state.unavailable, false);
  assert.equal(await ctl.change('a1', 'effort', 'high'), true);
  assert.deepEqual(writes, [['a1', {effort: 'high'}]]);
  assert.equal(ctl.state.rows[0].view.effort, 'high'); assert.equal(ctl.state.rows[0].saved, true);
  // Same value again: nothing is sent.
  assert.equal(await ctl.change('a1', 'effort', 'high'), true);
  assert.equal(await ctl.change('a2', 'model', 'opus'), true);
  assert.equal(writes.length, 1);
  assert.equal(await ctl.change('a2', 'model', ''), true);
  assert.deepEqual(writes[1], ['a2', {model: null}]);
});

test('a refused change keeps the old value and reads as a refusal', async () => {
  const io = fake({views: {a1: view(), a2: view()}});
  const ctl = options.controller({agents, ...io});
  await ctl.load();
  io.write = async () => { throw failure('session_open', 409); };
  const again = options.controller({agents, read: io.read, write: io.write}); await again.load();
  assert.equal(await again.change('a1', 'client', 'codex'), false);
  const row = again.state.rows[0];
  assert.match(row.error, /End the session/); assert.equal(row.view.client, null); assert.equal(row.saving, false);
  // The next successful change clears the refusal.
  again.state.rows[0].view = view();
  io.write = async (id, change) => ({...view(), ...change});
  const next = options.controller({agents, read: io.read, write: io.write}); await next.load();
  assert.equal(await next.change('a1', 'model', 'sonnet'), true);
  assert.equal(next.state.rows[0].error, '');
});

test('a bad value is refused on the page and sends nothing', async () => {
  const writes = [];
  const ctl = options.controller({agents, ...fake({views: {a1: view(), a2: view()}, writes})}); await ctl.load();
  assert.equal(await ctl.change('a1', 'model', 'two words'), false);
  assert.match(ctl.state.rows[0].error, /letters, digits/);
  assert.equal(writes.length, 0);
});

test('when the routes answer 404 for every agent the section reads as unavailable; one failing agent does not hide the rest', async () => {
  const gone = failure('not_found', 404);
  const none = options.controller({agents, ...fake({views: {a1: gone, a2: gone}})}); await none.load();
  assert.equal(none.state.unavailable, true);
  const some = options.controller({agents, ...fake({views: {a1: gone, a2: view()}})}); await some.load();
  assert.equal(some.state.unavailable, false);
  assert.equal(some.state.rows[0].missing, true);
  const broken = options.controller({agents, ...fake({views: {a1: failure('internal', 500, 'Something went wrong.'), a2: view()}})}); await broken.load();
  assert.equal(broken.state.unavailable, false); assert.match(broken.state.rows[0].error, /Something went wrong/);
  assert.equal((await options.controller({agents: []}).load(), 0), 0);
});

test('the default routes are the per-agent chat options routes', () => {
  const source = fs.readFileSync(path.join(__dirname, '../js/agent-options.js'), 'utf8');
  assert.match(source, /`\/root\/agents\/\$\{encodeURIComponent\(id\)\}\/chat\/options`/);
  assert.match(source, /api\(base\(id\)\)/); assert.match(source, /post\(base\(id\), change\)/);
});

test('Settings mounts the control from the agents menu list and has no start or trust rows', () => {
  const settings = fs.readFileSync(path.join(__dirname, '../js/settings.js'), 'utf8');
  assert.match(settings, /loadGlobalAgentProfiles\(\)\)\.filter\(globalRosterAgent\)/);
  assert.match(settings, /agentOptions\.mount\(host, agents\)/);
  assert.match(settings, /data-agent-options/);
  assert.doesNotMatch(settings, /Starting an agent|Trusted agent folders|agent-trust/);
});

test('the model offered follows the chosen client, and a model of the other client is shown as the default with the reason', () => {
  const codex = options.rowModel(view({client: 'codex', model: '', suggestions: {claude: ['opus'], codex: ['gpt-9']}}));
  assert.deepEqual(codex.suggestions, ['gpt-9']);
  const stale = options.rowModel(view({client: 'codex', model: 'haiku', model_fits: false}));
  assert.equal(stale.model, '');
  assert.match(stale.notice, /“haiku” is not a Codex model, so Codex starts on its default/);
  assert.equal(options.rowModel(view({client: 'codex', model: 'gpt-6-luna', model_fits: true})).model, 'gpt-6-luna');
  assert.match(options.NOTE, /clears a model that belongs to the other client/);
});

test('a refused model says why in the server\'s own plain words', () => {
  const error = failure('validation_error', 422, 'haiku is a Claude Code model, and Helper uses Codex. Choose a Codex model, or leave it on default.');
  assert.match(options.refusal(error, 'model'), /^haiku is a Claude Code model, and Helper uses Codex/);
});
