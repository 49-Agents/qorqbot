const test = require('node:test');
const assert = require('node:assert');
const model = require('../js/connectors.js');

const item = (id, kind, status, extra = {}) => ({id, kind, status, tier: 'recommended', ...extra});

test('local connectors toggle on and off', () => {
  assert.deepStrictEqual(model.action(item('playwright', 'local', 'off')), {kind: 'toggle', label: 'Turn on'});
  assert.deepStrictEqual(model.action(item('chrome', 'local', 'connected')), {kind: 'toggle', label: 'Turn off'});
});

const online = (id, claude, codex, extra = {}) => ({id, kind: 'claude', tier: 'suggested', status: claude, clients: {claude: {status: claude}, codex}, ...extra});

test('claude column guides connect, reconnect or manage', () => {
  assert.deepStrictEqual(model.cell(online('gmail', 'not_added', {status: 'not_available'}), 'claude').action, {kind: 'guide', label: 'Connect'});
  assert.strictEqual(model.cell(online('notion', 'needs_sign_in', {status: 'not_added'}), 'claude').action.label, 'Reconnect');
  assert.deepStrictEqual(model.cell(online('gmail', 'connected', {status: 'not_available'}), 'claude').action, {kind: 'guide', label: 'Manage', quiet: true});
  assert.strictEqual(model.cell(online('gmail', 'checking', {status: 'not_available'}), 'claude').action, null);
  assert.match(model.cell(online('gmail', 'unavailable', {status: 'not_available'}, {reason: 'claude_missing'}), 'claude').hint, /Install Claude Code/);
});

test('codex column says when a service has no Codex connection', () => {
  const gmail = model.cell(online('gmail', 'connected', {status: 'not_available'}), 'codex');
  assert.strictEqual(gmail.status.label, 'Not available');
  assert.strictEqual(gmail.action, null);
  assert.match(gmail.hint, /Gmail doesn’t offer a Codex connection/);
});

test('codex column connects, signs in, waits and removes', () => {
  assert.deepStrictEqual(model.cell(online('notion', 'connected', {status: 'not_added', managed: false}), 'codex').action, {kind: 'codex-connect', label: 'Connect'});
  assert.strictEqual(model.cell(online('notion', 'connected', {status: 'needs_sign_in', managed: true}), 'codex').action.label, 'Sign in');
  const waiting = model.cell(online('notion', 'connected', {status: 'needs_sign_in', sign_in: {running: true, link: 'https://x'}}), 'codex');
  assert.strictEqual(waiting.status.label, 'Waiting for your approval');
  assert.deepStrictEqual(waiting.action, {kind: 'codex-open', label: 'Open sign-in page', link: 'https://x'});
  assert.deepStrictEqual(model.cell(online('notion', 'connected', {status: 'connected', managed: true}), 'codex').action, {kind: 'codex-disconnect', label: 'Remove', quiet: true});
  const theirs = model.cell(online('notion', 'connected', {status: 'connected', managed: false}), 'codex');
  assert.strictEqual(theirs.action, null);
  assert.match(theirs.hint, /own Codex settings/);
  assert.match(model.cell(online('notion', 'x', {status: 'needs_sign_in', sign_in: {running: false, failed: 'Sign-in timed out.'}}), 'codex').hint, /timed out/);
  assert.match(model.cell(online('notion', 'x', {status: 'unavailable', reason: 'codex_missing'}), 'codex').hint, /Install Codex/);
});

test('shared connectors use one control; Your Chrome is Claude only', () => {
  assert.ok(model.shared({id: 'playwright', kind: 'local'}));
  assert.ok(!model.shared({id: 'chrome', kind: 'local'}));
  assert.ok(!model.shared({id: 'gmail', kind: 'claude'}));
  const chrome = {id: 'chrome', kind: 'local', status: 'off', clients: {claude: {status: 'off'}, codex: {status: 'not_available'}}};
  assert.deepStrictEqual(model.cell(chrome, 'claude').action, {kind: 'toggle', label: 'Turn on'});
  assert.strictEqual(model.cell(chrome, 'codex').status.label, 'Not available');
});

test('guided connect marks the current step', () => {
  const opened = model.guideSteps('Gmail', 'opened');
  assert.deepStrictEqual(opened.map(step => step.state), ['done', 'current', 'todo', 'todo']);
  assert.match(opened[1].text, /Find Gmail/);
  assert.deepStrictEqual(model.guideSteps('Gmail', 'checking').map(step => step.state), ['done', 'done', 'done', 'current']);
  assert.match(model.guideSteps('Notion', 'opened', true)[1].text, /Connect again/);
});

test('agent tools list what each client can and cannot use', () => {
  const view = {connectors: [
    online('gmail', 'connected', {status: 'not_available'}),
    online('notion', 'connected', {status: 'connected'}),
    online('linear', 'not_added', {status: 'connected'}),
    {id: 'playwright', kind: 'local', status: 'connected', clients: {claude: {status: 'connected'}, codex: {status: 'connected'}}},
  ], account_connectors: [{name: 'Canva', status: 'connected'}, {name: 'Miro', status: 'needs_sign_in'}]};
  assert.deepStrictEqual(model.agentTools(view, 'claude'), {can: ['Gmail', 'Notion', 'Playwright', 'Canva'], lacks: ['Linear']});
  assert.deepStrictEqual(model.agentTools(view, 'codex'), {can: ['Notion', 'Linear', 'Playwright'], lacks: ['Gmail', 'Canva']});
  assert.strictEqual(model.agentTools(null, 'claude'), null);
  assert.strictEqual(model.agentTools(view, 'other'), null);
});

test('built-in and extension connectors have no button', () => {
  assert.strictEqual(model.action(item('browser', 'install', 'connected')).kind, 'none');
  assert.strictEqual(model.action(item('whatsapp', 'extension', 'not_installed')).kind, 'none');
});

test('notes explain what the owner has to do', () => {
  assert.strictEqual(model.note(item('gmail', 'claude', 'not_added')), null);
  assert.strictEqual(model.note({...item('slack', 'claude', 'failed'), clients: {claude: {status: 'failed'}}}).tone, 'warn');
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
  for (const id of ['browser', 'chrome', 'playwright', 'gmail', 'google_calendar', 'google_drive', 'whatsapp', 'notion', 'slack',
    'linear', 'atlassian', 'sentry', 'asana', 'hubspot', 'figma', 'canva', 'stripe']) {
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
