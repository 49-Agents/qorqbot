'use strict';
// node --test dashboard/tests/chat-terminals-mode.test.js
// The Chat panel when agents run in terminals (js/agent-terminal-view.js and the guarded calls in js/global-chat.js), no DOM: a payload with
// `execution: 'terminal'` gets the terminal wording and none of the managed widgets, and every other payload takes today's managed paths.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {test} = require('node:test');
const terminalView = require('../js/agent-terminal-view.js');
const runtime = require('../js/agent-runtime.js');
const slashCommands = require('../js/slash-commands.js');

const source = fs.readFileSync(path.join(__dirname, '../js/global-chat.js'), 'utf8');
const between = (from, to) => {
  const start = source.indexOf(from), end = source.indexOf(to, start);
  assert.ok(start >= 0 && end > start, `global-chat.js no longer contains ${from}`);
  return source.slice(start, end);
};
const BINDING = {provider: 'claude', conversation_id: 'c1', binding_id: 'b1'};
const terminal = (extra = {}) => ({state: 'live', reason: null, can_send: true, working: false, provider: 'claude', conversation_id: 'c1', binding: BINDING,
  execution: 'terminal', launch_commands: ['claude'], ...extra});
const offline = (extra = {}) => terminal({state: 'offline', reason: 'terminal_offline', can_send: false, binding: null, ...extra});
const managed = (extra = {}) => ({state: 'live', reason: null, can_send: true, working: false, provider: 'claude', binding: BINDING, managed: {open: true, phase: 'idle'}, ...extra});
const PLAIN = /—|–|!|\btmux\b|\/(home|tmp|usr)\//;

// ---- the payload check: only the string "terminal" means a terminal agent
test('only execution "terminal" is a terminal agent; anything else is a managed session', () => {
  assert.equal(terminalView.isTerminal(terminal()), true);
  for (const value of [undefined, null, 'managed', 'Terminal', 'tmux', true, 1]) assert.equal(terminalView.isTerminal({...managed(), execution: value}), false, String(value));
  assert.equal(terminalView.isTerminal(managed()), false);
  assert.equal(terminalView.isTerminal(null), false);
  assert.equal(terminalView.on(), false, 'no agentMode on the page: not Terminals mode');
  global.agentMode = {terminals: () => true};
  try { assert.equal(terminalView.on(), true); } finally { delete global.agentMode; }
  global.agentMode = {terminals: () => false};
  try { assert.equal(terminalView.on(), false); } finally { delete global.agentMode; }
});

// ---- the wording of a terminal agent, through the real Chat code
function chatParts({session, entries = 0, inputs = 0, viewing = true, local = false, withModule = true, openTerminal = true}) {
  const body = between('  const hasHistory = ', '  // ---- slash commands') + between('  function emptyState() {', '  // ---- managed runtime') + between('  const sessionLineText = ', '  // One repaint');
  return new Function('session', 'label', 'agentRuntime', 'viewingLive', 'entries', 'shownInputs', 'hasLocal', 'GLOBAL_CHAT_INFLIGHT', 'composerScope', 'GLOBAL_CHAT_OPENING', 'agentId', 'agentTerminalView', 'options', 'earlier',
    `${body}; return {sessionSummary, emptyState, sessionLineText, hasHistory};`)(session, 'Ada', runtime, () => viewing, {size: entries}, {length: inputs}, local,
    {has: () => false}, 'scope', {has: () => false}, 'agent-1', withModule ? terminalView : undefined, openTerminal ? {openTerminal() {}} : {}, []);
}
const words = options => { const chat = chatParts(options), summary = chat.sessionSummary(), empty = chat.emptyState(); return {key: summary.key, summaryLine: summary.line, empty, shown: terminalView.isTerminal(options.session) ? terminalView.sessionLineText(summary, empty) : chat.sessionLineText(summary, empty)}; };

test('a live terminal agent reads live or working, with no managed wording', () => {
  assert.deepEqual(words({session: terminal()}), {key: 'live', summaryLine: '', empty: {text: 'Your agent conversation appears here.', action: null}, shown: ''});
  assert.equal(words({session: terminal({working: true})}).key, 'working');
  assert.equal(words({session: terminal({reason: 'binding_not_ready'})}).key, 'starting');
  assert.equal(words({session: terminal({reason: 'capture_not_ready'}), entries: 2}).key, 'starting');
  assert.equal(words({session: terminal(), viewing: false}).key, 'starting', 'live, but the conversation shown is not the live one yet');
  assert.equal(words({session: terminal({reason: 'capture_disabled'})}).key, 'disabled');
  assert.deepEqual(chatParts({session: terminal({reason: 'binding_not_ready'})}).emptyState(), {text: 'Connecting…', action: null});
});

test('an offline terminal agent names the one word to start it, and offers Open terminal', () => {
  const empty = words({session: offline()});
  assert.equal(empty.key, 'offline');
  assert.equal(empty.summaryLine, 'Open the terminal (>_) and run claude to start Ada.');
  assert.deepEqual(empty.empty, {text: 'Open the terminal (>_) and run claude to start Ada. Its conversation will appear here.', action: {key: 'terminal', label: 'Open terminal'}});
  assert.equal(empty.shown, '', 'the empty pane says it, so the session line stays quiet');
  assert.equal(words({session: offline(), openTerminal: false}).empty.action, null);
  // with history the session line carries it
  const history = words({session: offline(), entries: 3});
  assert.deepEqual(history.empty, {text: '', action: null});
  assert.equal(history.shown, 'Open the terminal (>_) and run claude to start Ada.');
  // the conversation is being connected in the terminal
  for (const reason of ['other_conversation', 'selection_required', 'registering']) {
    assert.equal(words({session: offline({reason}), entries: 1}).shown, 'Connecting to the conversation in Terminal…', reason);
  }
  assert.equal(words({session: offline({reason: 'unsupported_runtime'}), entries: 1}).shown, '');
  assert.equal(words({session: offline({reason: 'capture_disabled'})}).key, 'disabled');
});

test('the start hint comes from launch_commands, and falls back to a plain sentence', () => {
  assert.equal(terminalView.startHint({launch_commands: ['claude']}, 'Ada'), 'Open the terminal (>_) and run claude to start Ada');
  assert.equal(terminalView.startHint({launch_commands: ['claude', 'codex']}, 'Ada'), 'Open the terminal (>_) and run claude or codex to start Ada');
  assert.equal(terminalView.startHint({launch_commands: []}, 'Ada'), 'Start or resume Ada in Terminal');
  assert.equal(terminalView.startHint({}, 'Ada'), 'Start or resume Ada in Terminal');
  assert.equal(terminalView.startHint(null, 'Ada'), 'Start or resume Ada in Terminal');
});

test('no managed word reaches the terminal wording', () => {
  for (const options of [{session: terminal()}, {session: offline()}, {session: offline(), entries: 2}, {session: terminal({working: true})}, {session: terminal({reason: 'binding_not_ready'})}]) {
    const w = words(options);
    for (const text of [w.summaryLine, w.empty.text, w.shown]) assert.doesNotMatch(text, /asleep|\boff\b|stays off|Send a message to|starts? it|Starting/i, text);
    for (const text of [w.summaryLine, w.empty.text, w.shown, terminalView.unavailableText(options.session, 'Ada')]) assert.doesNotMatch(text, PLAIN, text);
  }
});

// ---- managed payloads take today's paths, with the module loaded or not
test('a managed payload reads exactly as without the module', () => {
  const sessions = [managed(), managed({managed: {open: false}}), managed({managed: {open: false, off: true}}), managed({managed: {open: true, phase: 'working'}}),
    {...managed(), execution: 'managed'}, {...managed(), launch_commands: ['claude']}, managed({state: 'offline', binding: null, can_send: false})];
  for (const session of sessions) for (const entries of [0, 3]) {
    const withModule = words({session, entries, withModule: true}), without = words({session, entries, withModule: false});
    assert.deepEqual(withModule, without, JSON.stringify(session));
  }
  assert.match(words({session: managed({managed: {open: false}})}).empty.text, /Send a message to start Ada/);
  assert.equal(words({session: managed({managed: {open: false}})}).empty.action, null);
});

test('canSend: a terminal agent takes a message only when live and the conversation shown is the live one; a closed managed one still opens', () => {
  const canSendSource = between('  const canSend = () =>', '  const selectedFiles');
  const make = (session, viewing, withModule = true) => new Function('session', 'viewingLive', 'pairedHost', 'finished', 'agentRuntime', 'agentTerminalView',
    `${canSendSource}; return canSend;`)(session, () => viewing, null, false, runtime, withModule ? terminalView : undefined)();
  assert.equal(make(terminal(), true), true);
  assert.equal(make(terminal(), false), false, 'live, but another conversation is shown');
  assert.equal(make(offline(), false), false, 'offline: can_send is false');
  // a terminal payload that says it can send but has no live conversation (a "closed" reason must not open anything)
  assert.equal(make(terminal({reason: 'session_closed', binding: null}), false), false, 'no "a message starts the session" for a terminal agent');
  assert.equal(make(managed({managed: {open: false}, binding: null}), false), true, 'managed: a message opens it');
  assert.equal(make(managed({managed: {open: false}, binding: null}), false, false), true);
});

// ---- the managed widgets are not built for a terminal agent
test('no Stop block and no question cards for a terminal agent', () => {
  const sync = between('  const runtimeSend =', '  // The session line says what the empty');
  const run = session => {
    const calls = {bars: 0};
    const fakeRuntime = {createBar: () => { calls.bars++; return {el: {}, update() {}, dispose() {}}; }};
    const sessionLine = {parentNode: {insertBefore() {}}};
    new Function('agentRuntime', 'label', 'sessionLine', 'liveBinding', 'paintSession', 'load', 'showBanner', 'cleanups', 'coreData', 'post', 'base', 'contextStatus', 'agentTerminalView', 'session', 'view',
      `let managedBar = null; let finished = false; ${sync}; syncManaged();`)(
      fakeRuntime, 'Ada', sessionLine, () => null, () => {}, () => {}, () => {}, [], r => r, async () => ({}), '/chat', null, terminalView, session, null);
    return calls;
  };
  const t = run(terminal());
  assert.equal(t.bars, 0);
  const m = run(managed());
  assert.equal(m.bars, 1);
});

// ---- refusals
test('refusal codes map to plain sentences for a terminal agent, and a managed payload never gets them', () => {
  const expected = {
    terminal_offline: 'Not running. Start or resume it in Terminal, then send again.',
    terminal_changed: 'Not delivered. Check the agent and try again.',
    terminal_required: 'This command runs only in the agent’s Terminal. Open Terminal and run it there.',
    terminal_present: 'The agent’s Terminal is still running. Close it, then send again.',
    terminal_mode_off: 'Agents run as managed sessions on this installation.',
    runtime_switching: 'Agents are being switched. Try again in a minute.',
    unsupported_runtime: 'This agent can’t take messages here.',
  };
  for (const [code, text] of Object.entries(expected)) {
    assert.equal(terminalView.refusalText(terminal(), code), text, code);
    assert.equal(terminalView.refusalText(managed(), code), null, `${code}: managed keeps its own table`);
    assert.doesNotMatch(text, PLAIN, code);
  }
  assert.deepEqual([...terminalView.REFUSAL_CODES].sort(), Object.keys(expected).sort());
  assert.equal(terminalView.refusalText(terminal(), 'something_new'), null);
  // through the real refusalText of the Chat code
  const refusalSource = between('  const refusalText = problem =>', '  // One admission request.');
  const text = (session, code, withModule = true) => new Function('agentRuntime', 'GLOBAL_CHAT_REFUSAL_TEXT', 'agentTerminalView', 'session', `${refusalSource}; return refusalText;`)(
    runtime, {capture_disabled: 'Chat unavailable.'}, withModule ? terminalView : undefined, session)({code});
  assert.equal(text(terminal(), 'terminal_changed'), expected.terminal_changed);
  assert.equal(text(managed(), 'terminal_changed'), runtime.refusalText('terminal_changed'), 'a managed payload keeps the managed sentence');
  assert.equal(text(terminal(), 'binding_not_ready'), runtime.refusalText('binding_not_ready'));
  assert.equal(text(terminal(), 'capture_disabled'), 'Chat unavailable.');
  assert.equal(text(terminal(), 'something_new'), "Couldn't send. Try again");
  assert.equal(text(managed(), 'terminal_offline', false), "Couldn't send. Try again");
  // each one is a refusal (it stores nothing)
  const refusals = between('const GLOBAL_CHAT_REFUSALS = new Set', 'const GLOBAL_CHAT_REFUSAL_TEXT');
  const known = new Function(`${refusals}; return GLOBAL_CHAT_REFUSALS;`)();
  for (const code of terminalView.REFUSAL_CODES) assert.equal(known.has(code), true, code);
});

test('why the composer is off, for a terminal agent', () => {
  assert.equal(terminalView.unavailableText(offline(), 'Ada'), 'Not running. Start Ada in its Terminal first.');
  assert.equal(terminalView.unavailableText(terminal(), 'Ada'), 'Connecting to Ada…');
  assert.equal(terminalView.unavailableText(terminal({reason: 'capture_disabled'}), 'Ada'), 'Chat is unavailable.');
  assert.equal(terminalView.unavailableText(null, 'Ada'), 'Loading the conversation…');
  assert.equal(terminalView.unavailableText(terminal(), 'Ada', {finished: true}), 'This chat is no longer available.');
  assert.equal(terminalView.unavailableText(terminal(), 'Ada', {paired: true}), 'Close the side conversation to message Ada.');
});

// ---- slash commands
test('menu commands are offered as finish-in-the-Terminal entries, and the Terminal opens after one is sent', () => {
  const list = [{name: '/compact', where: 'chat'}, {name: '/model', where: 'unavailable'}, {name: '/cost', where: 'unavailable'}];
  const offered = terminalView.catalog(list);
  assert.deepEqual(offered.map(item => item.where), ['chat', 'terminal', 'terminal']);
  assert.deepEqual(list.map(item => item.where), ['chat', 'unavailable', 'unavailable'], 'the catalog itself is not changed');
  assert.equal(slashCommands.refusal(terminal(), offered[1], 'Ada'), null);
  const open = (text, session = terminal()) => {
    const calls = {opened: 0, told: []};
    const done = terminalView.finishInTerminal(slashCommands.command(offered, text), text, session, 'Ada', () => { calls.opened++; }, note => calls.told.push(note));
    return {done, ...calls};
  };
  assert.deepEqual(open('/model'), {done: true, opened: 1, told: ['Finish /model in Ada’s Terminal.']});
  assert.equal(open('/model sonnet').done, false, 'a named model switches at once');
  assert.equal(open('/cost').opened, 1);
  assert.equal(open('/compact').done, false);
  assert.equal(open('hello').done, false);
  assert.equal(open('/cost', managed()).done, false, 'a managed payload never opens a Terminal');
  assert.equal(terminalView.finishInTerminal(offered[2], '/cost', terminal(), 'Ada', undefined), false, 'no Terminal view to open');
  assert.equal(terminalView.opensTerminal(offered[1], '/model gpt-6', 'codex'), true);
});

// ---- the Chat/Terminal switch follows each payload
test('the switch is hidden for a payload that is not a terminal agent, and the host is told once per change', () => {
  const toggle = {hidden: false}, root = {querySelector: selector => selector === '.gchat-view-toggle' ? toggle : null};
  const told = [], memo = {}, options = {onExecution: value => told.push(value)};
  terminalView.syncChat(root, null, options, memo);
  assert.deepEqual(told, []);
  terminalView.syncChat(root, terminal(), options, memo);
  terminalView.syncChat(root, terminal({working: true}), options, memo);
  assert.equal(toggle.hidden, false);
  terminalView.syncChat(root, managed(), options, memo);
  terminalView.syncChat(root, managed(), options, memo);
  assert.equal(toggle.hidden, true);
  terminalView.syncChat(root, terminal(), options, memo);
  assert.deepEqual(told, [false, true, false]);
  assert.match(terminalView.toggleHtml(), /data-terminal[^>]*aria-pressed="false"/);
  assert.match(terminalView.toggleHtml(true), /data-chat-return[^>]*aria-pressed="false"/);
});

// ---- the second host: mountAgentView in Terminals mode and in managed mode
function fakeDocument() {
  const make = () => {
    const el = {className: '', hidden: false, isConnected: true, children: [], innerHTML: '', removed: false,
      replaceChildren(...kids) { this.children = kids; },
      remove() { this.removed = true; this.isConnected = false; },
      querySelector: selector => selector.includes('status') || selector.includes('gchat-status') ? el.status : selector === '[data-chat-return]' ? el.back : el.inner,
      status: {textContent: 'Terminal'}, back: {}, inner: {}};
    return el;
  };
  return {createElement: make};
}
function viewHarness({terminalsOn}) {
  const body = between('let globalAgentViewActive = null;', 'function mountGlobalChat(');
  const chats = [], terminals = [], doc = fakeDocument();
  const mountGlobalChat = (host, agent, options) => { const chat = {host, options, disposed: false, dispose() { this.disposed = true; }}; chats.push(chat); return chat; };
  const mountTerminal = (key, host, onStat, base) => { const t = {key, host, onStat, base, closed: false}; terminals.push(t); return () => { t.closed = true; }; };
  const view = terminalsOn ? Object.assign({}, terminalView, {on: () => true}) : undefined;
  // The module reads the page's globals (document, esc, mountTerminal); a view built here lives only while the test holds them.
  const mount = (pane, opts) => (Object.assign(global, {document: doc, esc: String, mountTerminal}), new Function('document', 'esc', 'mountGlobalChat', 'globalChatOpenFocusReference', 'agentTerminalView', 'mountTerminal', 'setInterval', 'clearInterval',
    `${body}; return mountAgentView;`)(doc, String, mountGlobalChat, () => true, view, mountTerminal, () => 1, () => {})(pane, {agent_id: 'a1', name: 'Ada'}, opts));
  return {mount, chats, terminals, pane: () => ({children: [], replaceChildren(...kids) { this.children = kids; }})};
}

test.afterEach(() => { for (const name of ['document', 'esc', 'mountTerminal']) delete global[name]; });

test('Terminals mode builds two hosts and passes the Terminal options; managed mode builds one host and passes only openReference', () => {
  const managedRun = viewHarness({terminalsOn: false});
  const managedPane = managedRun.pane();
  const mv = managedRun.mount(managedPane, {isCurrent: () => true});
  assert.equal(managedPane.children.length, 1);
  assert.equal(mv.showTerminal, undefined);
  assert.deepEqual(Object.keys(managedRun.chats[0].options), ['openReference']);
  mv.dispose();

  const run = viewHarness({terminalsOn: true}), pane = run.pane();
  const view = run.mount(pane, {isCurrent: () => true});
  assert.equal(pane.children.length, 2, 'Chat host and Terminal host');
  assert.deepEqual(Object.keys(run.chats[0].options).sort(), ['onExecution', 'openReference', 'openTerminal']);
  assert.equal(typeof view.showTerminal, 'function');
  assert.equal(pane.children[1].hidden, true, 'the Terminal view is built on demand');
  assert.equal(run.terminals.length, 0);
  run.chats[0].options.openTerminal();
  assert.equal(run.terminals.length, 1);
  assert.deepEqual([run.terminals[0].key, run.terminals[0].base], ['a1', '/root/terminal/a1']);
  assert.equal(pane.children[0].hidden, true);
  assert.equal(pane.children[1].hidden, false);
  view.showTerminal(); assert.equal(run.terminals.length, 1, 'a warm Terminal is kept');
  // a payload that is not a terminal agent drops the Terminal and returns to Chat
  run.chats[0].options.onExecution(true);
  assert.equal(run.terminals[0].closed, true);
  assert.equal(pane.children[0].hidden, false);
  view.showTerminal(); assert.equal(run.terminals.length, 1, 'no Terminal view for a managed payload');
  view.dispose();
  assert.equal(run.chats[0].disposed, true);
  assert.equal(pane.children.every(host => host.removed), true);
});

test('a caller that names the Terminal route gets that route, and view "terminal" opens it first', () => {
  const run = viewHarness({terminalsOn: true}), pane = run.pane();
  const view = run.mount(pane, {isCurrent: () => true, terminal: {key: 'k', base: '/project/x/terminal/k'}, view: 'terminal'});
  assert.equal(run.chats.length, 0, 'Chat is built lazily when Terminal is first');
  assert.deepEqual([run.terminals[0].key, run.terminals[0].base], ['k', '/project/x/terminal/k']);
  view.showChat();
  assert.equal(run.chats.length, 1);
  view.dispose();
});
