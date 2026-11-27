'use strict';
// node --test dashboard/tests/*.test.js
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const plan = require('../js/onboarding-plan.js');

const steps = [
  {id: 'assistant', chapter: 'online'}, {id: 'intro', chapter: 'online'},
  {id: 'intro-more', progressStep: 'intro', chapter: 'online'}, {id: 'chat', chapter: 'online'},
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
  assert.equal(plan.resumeIndex(steps, {status: 'in_progress', step: 'intro'}), 1);
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

test('resumeIndex maps the retired Todo board step to calendar', () => {
  assert.equal(plan.resumeIndex(steps, {status: 'in_progress', step: 'attention'}), 5);
  assert.equal(plan.resumeIndex(steps, {status: 'completed', step: 'attention'}), 0);
  const tour = fs.readFileSync(path.join(__dirname, '../js/onboarding.js'), 'utf8');
  assert.doesNotMatch(tour, /\{id: 'attention'|attentionBoard/);
  // The Focus step points at the feather that returns to the notes.
  assert.match(tour, /\{id: 'focus'[^\n]*\n\s*body: '[^']*feather beside Browser/);
});

test('resumeIndex maps the retired connector steps to the browser step', () => {
  const withBrowser = [...steps, {id: 'browser-setup', chapter: 'connect'}];
  for (const step of ['connectors', 'connect-more'])
    assert.equal(plan.resumeIndex(withBrowser, {status: 'in_progress', step}), withBrowser.length - 1);
});

test('saveId uses the shared progress id when present', () => {
  assert.equal(plan.saveId(steps[2]), 'intro');
  assert.equal(plan.saveId(steps[4]), 'focus');
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

test('every step keeps Esc and Tab for the tour, even inside its highlighted area', () => {
  for (const step of [{id: 'chat'}, {id: 'focus', view: 'focus'}]) {
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
  for (const step of [{id: 'chat'}, undefined]) {
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

test('the retired Activity step stays gone and the tour uses the plan’s key handling', () => {
  const source = fs.readFileSync(path.join(__dirname, '../js/onboarding.js'), 'utf8');
  assert.doesNotMatch(source, /\{id: 'activity'/);
  assert.match(source, /onboardingPlan\.handleKey\(/);
});

test('resumeIndex continues the retired Learning Applier steps at settings', () => {
  const current = [...steps.slice(0, 6), {id: 'settings', chapter: 'yours'}];
  assert.equal(plan.resumeIndex(current, {status: 'in_progress', step: 'learning'}), 6);
  assert.equal(plan.resumeIndex(current, {status: 'in_progress', step: 'context'}), 6);
  assert.equal(plan.resumeIndex(steps, {status: 'in_progress', step: 'learning'}), 6);
});

test('refused agent navigation stops before changing views or waiting for a target', async () => {
  const vm = require('node:vm');
  const source = fs.readFileSync(path.join(__dirname, '../js/onboarding.js'), 'utf8');
  const prepare = source.slice(source.indexOf('  async function prepareStep('), source.indexOf('  async function showStep('));
  for (const step of [{id:'chat'}, {id:'focus',view:'focus'}]) {
    let targetCalls=0, switches=0, waits=0;
    const ctx={state:{scope:'global',globalTab:'agents'}, document:{querySelector:()=>({openAgent:true})}, isCurrent:()=>true,
      waitFor:async fn=>{waits++;return fn();}, findAgent:()=>({agent_id:'a'}), globalAgentsNavigate:async()=>false,
      _agpad:null, visible:()=>true};
    vm.createContext(ctx);vm.runInContext(prepare+'\nthis.prepareStep=prepareStep;',ctx);
    await assert.rejects(ctx.prepareStep({...step,target:()=>{targetCalls++;return {}; }},1),/could not open.*open edit.*try again/i);
    assert.equal(switches,0);assert.equal(targetCalls,0);assert.equal(waits,1);
  }
});

test('a failed step never highlights a previously visible target', () => {
  const vm = require('node:vm');
  const source = fs.readFileSync(path.join(__dirname, '../js/onboarding.js'), 'utf8');
  const position = source.slice(source.indexOf('  function position()'), source.indexOf('    // Text size zooms the whole page',source.indexOf('  function position()'))) + '\n}';
  let described, queried=0;const ctx={root:{classList:{toggle(){}}},welcome:false,steps:[{target(){queried++;return {};}}],index:0,
    stepTargetReady:false,appDialog:()=>false,visible:()=>true,describeTarget:value=>{described=value}};
  vm.createContext(ctx);vm.runInContext(position+'\nthis.position=position;',ctx);ctx.position();
  assert.equal(described,null);assert.equal(queried,0);
  ctx.stepTargetReady=true;ctx.position();assert.notEqual(described,null);assert.ok(queried>0);
});

// ---- the one tour: the real step list, the retired step ids, and the hello's readiness rule
const source = fs.readFileSync(path.join(__dirname, '../js/onboarding.js'), 'utf8');
const listText = source.slice(source.indexOf('  const allSteps = ['), source.indexOf('\n  ];', source.indexOf('  const allSteps = [')) + 5);
const ctx = {CALENDAR_GUIDE_URL: 'x', agentButton: () => null, pane: () => null, sgAttentionNavigate: () => {}, document: {querySelector: () => null}};
require('node:vm').createContext(ctx); require('node:vm').runInContext(listText.replace('const allSteps', 'this.allSteps'), ctx);
const real = [...ctx.allSteps];
const ids = list => list.map(step => step.id);

test('the tour is one list: no start, consent, terminal or managed-start step', () => {
  assert.deepEqual(ids(real), ['assistant', 'focus', 'calendar', 'projects', 'signoffs', 'settings', 'privacy',
    'connect-overview', 'browser-setup', 'chat']);
  assert.ok(real.every(step => !step.variant && !step.managed && !step.terminal && !step.launch && !step.consent && !step.managedStart));
  assert.deepEqual(plan.chapters(real).map(c => c.id), ['online', 'workspace', 'projects', 'yours', 'connect']);
  assert.doesNotMatch(JSON.stringify(real.map(({target, run, setup, terminals, ...step}) => step)) + JSON.stringify(real.at(-1).setup.none), /terminal|type <code>|type claude|folder/i);
  for (const gone of ['variantSteps', 'chooseVariant', 'launchPhase', 'managedStartPhase', 'terminalOpenRefusal']) assert.equal(plan[gone], undefined, gone);
});

test('progress saved at a retired start step continues at the step after Ordubashy’s introduction', () => {
  for (const step of ['start', 'terminal', 'managed-start'])
    assert.equal(ids(real)[plan.resumeIndex(real, {status: 'in_progress', step})], 'focus', step);
  // Every other saved id still resumes at itself, and the older retired ids at theirs.
  for (const step of ids(real)) assert.equal(ids(real)[plan.resumeIndex(real, {status: 'in_progress', step})], step, step);
  assert.equal(ids(real)[plan.resumeIndex(real, {status: 'in_progress', step: 'activity'})], 'calendar');
  assert.equal(ids(real)[plan.resumeIndex(real, {status: 'in_progress', step: 'learning'})], 'settings');
  assert.equal(ids(real)[plan.resumeIndex(real, {status: 'in_progress', step: 'connectors'})], 'browser-setup');
  assert.equal(ids(real)[plan.resumeIndex(real, {status: 'in_progress', step: 'journal'})], 'projects');
  // A replay (completed) and anything unreadable start at the beginning, without an error.
  for (const saved of [null, undefined, {status: 'completed', step: 'terminal'}, {status: 'in_progress'}, {status: 'in_progress', step: 'start', launch: 'online'}])
    assert.doesNotThrow(() => plan.resumeIndex(real, saved));
  assert.equal(plan.resumeIndex(real, {status: 'completed', step: 'terminal'}), 0);
});

const ready = {installed: true, signed_in: true}, signedOut = {installed: true, signed_in: false}, missing = {installed: false, signed_in: false};
test('the hello needs Claude Code or Codex installed and signed in', () => {
  assert.deepEqual(plan.helloReadiness({claude: ready, codex: missing}), {state: 'ready', client: ''});
  assert.deepEqual(plan.helloReadiness({claude: missing, codex: ready}), {state: 'ready', client: ''});
  assert.deepEqual(plan.helloReadiness({claude: ready, codex: ready}), {state: 'ready', client: ''});
  for (const clients of [{claude: missing, codex: missing}, {claude: signedOut, codex: missing}, {claude: signedOut, codex: signedOut}, {}, {claude: {}, codex: {}}])
    assert.equal(plan.helloReadiness(clients).state, 'none');
});

test('an agent set to a client that is not ready cannot be answered even when the other one is', () => {
  const clients = {claude: ready, codex: signedOut};
  assert.deepEqual(plan.helloReadiness(clients, {effective_client: 'codex'}), {state: 'chosen', client: 'codex'});
  assert.deepEqual(plan.helloReadiness(clients, {effective_client: 'claude'}), {state: 'ready', client: 'claude'});
  // The options route may not exist yet: only the clients count.
  assert.equal(plan.helloReadiness(clients, null).state, 'ready');
  assert.equal(plan.helloReadiness(clients, {effective_client: 'other'}).state, 'ready');
  // Neither ready wins over a chosen client.
  assert.equal(plan.helloReadiness({claude: missing, codex: missing}, {effective_client: 'claude'}).state, 'none');
});

test('an unreadable agent-clients status never blocks the hello', () => {
  for (const clients of [null, undefined, 'x']) assert.equal(plan.helloReadiness(clients).state, 'ready');
});

test('the hello does not depend on Ordubashy’s session being open: an asleep or off agent is started by the message', () => {
  const hello = source.slice(source.indexOf('if (step.hello) {'), source.indexOf('  // Leaves the tour for Settings'));
  assert.doesNotMatch(hello, /session|execution|managed|\/runtime/);
  assert.match(hello, /helloReadiness\(clients, options\)/);
  assert.doesNotMatch(source, /\/runtime`/);
});

test('the last step says which client is missing or signed out, and where to fix it', () => {
  const none = plan.helloReadiness({claude: signedOut, codex: missing});
  assert.equal(plan.helloMissing({claude: signedOut, codex: missing}, none),
    'Ordubashy needs Claude Code or Codex to answer, and neither is ready: Claude Code isn’t signed in, and Codex isn’t installed. Open Settings > Model/Provider to fix one, then come back and say hello.');
  const chosen = plan.helloReadiness({claude: ready, codex: signedOut}, {effective_client: 'codex'});
  assert.equal(plan.helloMissing({claude: ready, codex: signedOut}, chosen),
    'Ordubashy is set to use Codex, and Codex isn’t signed in. Open Settings > Model/Provider to fix it, or switch Ordubashy to Claude Code there, then say hello.');
  assert.equal(plan.helloMissing({claude: ready, codex: missing}, plan.helloReadiness({claude: ready, codex: missing})), '', 'ready: nothing is missing');
  assert.equal(plan.helloMissing(null, {state: 'none'}), '');
});

test('the last step offers Settings > Model/Provider, steps aside, and comes back to the same step', () => {
  assert.match(source, /data-tour-settings hidden>open Settings &gt; Model\/Provider/);
  const aside = source.slice(source.indexOf('  async function openSettings()'), source.indexOf('  async function showStep(next)'));
  assert.match(aside, /dispose\(\);/, 'the tour is removed from the screen, so nothing of it blocks a click');
  assert.match(aside, /settingsShowSection\('agents'\)/);
  assert.match(aside, /index = last; create\(\); await showStep\(last\)/, 'it comes back to the same, last step');
  assert.match(aside, /state\.globalTab === 'settings'/, 'when Settings is left');
  assert.match(aside, /helloReadiness\(clients\)\.state !== 'ready'/, 'or when a client becomes ready');
  assert.match(source, /function create\(\) \{\s*stopWatching\(\);/, 'starting the tour any other way stops the watch');
});

// ---- Terminals mode: the same list with a few honest overrides
const overridden = ['assistant', 'settings', 'privacy', 'connect-overview', 'chat'];
const withTerminals = plan.stepsFor(real, true), by = (list, id) => list.find(step => step.id === id);
const plain = value => JSON.stringify(value, (key, v) => typeof v === 'function' ? undefined : v);

test('managed mode gets the very same list, and Terminals mode the same steps in the same order', () => {
  assert.equal(plan.stepsFor(real, false), real);
  assert.deepEqual(ids(withTerminals), ids(real));
  assert.deepEqual(withTerminals.map(plan.saveId), real.map(plan.saveId));
  assert.deepEqual(plan.chapters(withTerminals), plan.chapters(real));
  for (const step of real) if (!overridden.includes(step.id)) assert.equal(by(withTerminals, step.id), step, step.id);
});

test('every Terminals override exists and says what is true when agents run in terminals', () => {
  assert.deepEqual(real.filter(step => step.terminals).map(step => step.id), overridden);
  const assistant = by(withTerminals, 'assistant');
  assert.match(assistant.body, /You start him in his own Terminal, and you talk to him in Chat\./);
  assert.doesNotMatch(assistant.body, /Qorqbot runs him/);
  assert.match(assistant.hint, /Walkthrough, Terminal and Settings/);
  assert.match(by(withTerminals, 'settings').body, /how agents run/);
  assert.doesNotMatch(plain(by(withTerminals, 'privacy').instructions) + plain(by(withTerminals, 'connect-overview').instructions), /Codex/);
  const chat = by(withTerminals, 'chat');
  assert.doesNotMatch(chat.hint, /starts with your first message/);
  assert.doesNotMatch(chat.body, /^Type /);
  assert.match(chat.offline, /Start Ordubashy in his Terminal first, with the terminal icon above Chat/);
  assert.equal(chat.typed, by(real, 'chat').typed);
  // Managed texts are untouched by the overrides.
  assert.match(by(real, 'assistant').body, /Qorqbot runs him for you/);
  assert.match(by(real, 'chat').hint, /He starts with your first message/);
});

test('Terminals wording is short, plain, and has no command, path or em dash', () => {
  for (const id of overridden) {
    const text = plain(real.find(step => step.id === id).terminals);
    assert.doesNotMatch(text, /\u2014|type claude|<code>|\/home|~\/|--|tmux/i, id);
  }
});

test('the hello is typed and sent only when Ordubashy’s session is live and can receive', () => {
  const live = {session: {state: 'live', can_send: true}};
  assert.equal(plan.helloLive(live), true);
  for (const chat of [null, undefined, {}, {session: {}}, {session: {state: 'live', can_send: false}}, {session: {state: 'offline', can_send: true}},
    {session: {state: 'live'}}, {session: {state: 'closed', can_send: true, managed: {open: false}}}]) assert.equal(plan.helloLive(chat), false, plain(chat));
  const hello = source.slice(source.indexOf('async function terminalHello'), source.indexOf('async function prepareStep'));
  // Not live: only the offline sentence is shown; the typing and `greet` come after that return.
  assert.ok(hello.indexOf('helloLive(chat)') < hello.indexOf('input.value = HELLO'));
  assert.match(hello, /if \(!onboardingPlan\.helloLive\(chat\)\) \{ description\.textContent = step\.offline; return; \}/);
  assert.doesNotMatch(hello, /helloReadiness|agent-clients|options/);
  // Done sends it only after asking again.
  assert.match(source, /if \(send && terminalsOn\(\)\) send = onboardingPlan\.helloLive\(await readChat\(/);
  assert.match(source, /if \(terminalsOn\(\)\) \{ await terminalHello\(step, assistant, token\); return; \}/);
  assert.match(source, /const terminalsOn = \(\) => typeof agentMode !== 'undefined' && agentMode\.terminals\(\);/);
});
