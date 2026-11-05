const test = require('node:test');
const assert = require('node:assert');
const model = require('../js/connectors.js');

const item = (id, kind, status, extra = {}) => ({id, kind, status, tier: 'recommended', ...extra});

test('local connectors toggle on and off', () => {
  assert.deepStrictEqual(model.action(item('playwright', 'local', 'off')), {kind: 'toggle', label: 'Turn on'});
  assert.deepStrictEqual(model.action(item('chrome', 'local', 'connected')), {kind: 'toggle', label: 'Turn off'});
});

test('claude connectors connect, sign in or manage', () => {
  assert.strictEqual(model.action(item('gmail', 'claude', 'not_added')).label, 'Connect');
  assert.strictEqual(model.action(item('notion', 'claude', 'needs_sign_in')).label, 'Sign in');
  assert.deepStrictEqual(model.action(item('gmail', 'claude', 'connected')), {kind: 'claude', label: 'Manage', quiet: true});
  assert.strictEqual(model.action(item('gmail', 'claude', 'checking')).kind, 'none');
  assert.strictEqual(model.action(item('gmail', 'claude', 'unavailable')).kind, 'none');
});

test('built-in and extension connectors have no button', () => {
  assert.strictEqual(model.action(item('browser', 'install', 'connected')).kind, 'none');
  assert.strictEqual(model.action(item('whatsapp', 'extension', 'not_installed')).kind, 'none');
});

test('notes explain what the owner has to do', () => {
  assert.match(model.note(item('gmail', 'claude', 'unavailable', {reason: 'claude_missing'})).text, /Install Claude Code/);
  assert.match(model.note(item('gmail', 'claude', 'unavailable', {reason: 'claude_failed'})).text, /sign in/);
  assert.deepStrictEqual(model.note(item('gmail', 'claude', 'not_added')), {text: 'Claude Code only. Codex can’t use Claude’s connectors.', tone: 'info'});
  assert.strictEqual(model.note(item('notion', 'claude', 'needs_sign_in')).tone, 'warn');
  assert.strictEqual(model.note(item('gmail', 'claude', 'connected')), null);
  assert.match(model.note(item('whatsapp', 'extension', 'not_paired')).text, /Linked devices/);
  assert.strictEqual(model.note(item('playwright', 'local', 'off')), null);
});

test('unknown statuses fall back safely', () => {
  assert.strictEqual(model.status({status: 'something_new'}).label, 'Unknown');
  assert.strictEqual(model.status({status: 'connected'}).tone, 'ok');
});

test('summary counts recommended connectors only', () => {
  const items = [item('browser', 'built_in', 'connected'), item('playwright', 'local', 'off'), item('gmail', 'claude', 'connected'),
    {...item('notion', 'claude', 'connected'), tier: 'suggested'}];
  assert.deepStrictEqual(model.summary(items), {done: 2, total: 3, complete: false});
});

test('every catalog id has copy and an icon', () => {
  for (const id of ['browser', 'chrome', 'playwright', 'gmail', 'google_calendar', 'google_drive', 'whatsapp', 'notion', 'slack']) {
    assert.ok(model.COPY[id]?.name && model.COPY[id].does && model.COPY[id].privacy, id);
    assert.ok(model.ICONS[id], id);
  }
});

test('chrome under WSL explains itself and has no button', () => {
  const chrome = item('chrome', 'local', 'unsupported');
  assert.strictEqual(model.action(chrome).kind, 'none');
  assert.strictEqual(model.status(chrome).label, 'Not on Windows');
  assert.deepStrictEqual(model.note(chrome).tone, 'info');
});

test('agent browser says exactly what to install', () => {
  const both = item('browser', 'install', 'needs_install', {missing: ['chrome', 'agent_browser']});
  assert.match(model.note(both).text, /Google Chrome, then run the command/);
  assert.match(model.note({...both, missing: ['agent_browser']}).text, /^Run the command/);
  assert.match(model.note({...both, missing: ['chrome']}).text, /google.com\/chrome/);
  assert.strictEqual(model.note(both).tone, 'warn');
  assert.strictEqual(model.note(item('browser', 'install', 'connected')), null);
});

test('the Claude account line names the account or what is wrong', () => {
  assert.strictEqual(model.account({}), null);
  assert.match(model.account({claude_account: {signed_in: true, method: 'claude.ai', email: 'a@b.c'}}).text, /a@b\.c/);
  assert.strictEqual(model.account({claude_account: {signed_in: false}}).tone, 'warn');
  assert.match(model.account({claude_account: {signed_in: true, method: 'api_key'}}).text, /API key/);
});

test('whatsapp on a Mac says it is coming and has no button', () => {
  const whatsapp = {...item('whatsapp', 'extension', 'coming_soon'), tier: 'suggested'};
  assert.strictEqual(model.action(whatsapp).kind, 'none');
  assert.strictEqual(model.status(whatsapp).label, 'Coming soon on Mac');
  assert.deepStrictEqual(model.note(whatsapp), {text: 'The WhatsApp extension is available for Windows (WSL) and Linux. A Mac version is in progress.', tone: 'info'});
});

test('your chrome without its extension says so and can still be turned off', () => {
  const chrome = item('chrome', 'local', 'needs_extension', {extension: false});
  assert.strictEqual(model.status(chrome).label, 'Needs the Chrome extension');
  assert.strictEqual(model.action(chrome).label, 'Turn off');
  assert.match(model.note(chrome).text, /Claude in Chrome extension/);
  assert.strictEqual(model.note(chrome).tone, 'warn');
  assert.match(model.note(item('chrome', 'local', 'off', {extension: false})).text, /extension/);
  assert.strictEqual(model.note(item('chrome', 'local', 'off', {extension: true})), null);
  assert.ok(model.COPY.chrome.install.startsWith('https://chromewebstore.google.com/'));
});
