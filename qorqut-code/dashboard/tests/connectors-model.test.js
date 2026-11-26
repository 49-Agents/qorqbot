const test = require('node:test');
const assert = require('node:assert');
const model = require('../js/connectors.js');

const item = (id, kind, status, extra = {}) => ({id, kind, status, ...extra});

test('an extension that is not part of this Qorqbot has no button', () => {
  assert.strictEqual(model.action(item('whatsapp', 'extension', 'not_installed')).kind, 'none');
  assert.strictEqual(model.action(item('whatsapp', 'extension', 'unsupported')).kind, 'none');
});

test('notes explain what the owner has to do', () => {
  assert.match(model.note(item('whatsapp', 'extension', 'not_set_up')).text, /Link your phone/);
  assert.strictEqual(model.note(item('whatsapp', 'extension', 'connected')), null);
});

test('unknown statuses fall back safely', () => {
  assert.strictEqual(model.status({status: 'something_new'}).label, 'Unknown');
  assert.strictEqual(model.status({status: 'connected'}).tone, 'ok');
});

test('every catalog id has copy and an icon', () => {
  for (const id of ['openrouter', 'treg', 'whatsapp']) {
    assert.ok(model.COPY[id]?.name && model.COPY[id].does && model.COPY[id].privacy, id);
    assert.ok(model.ICONS[id], id);
  }
});

test('treg is set up with a key on its own card, not on claude.ai', () => {
  assert.match(model.COPY.treg.steps.join(' '), /paste the key/);
  assert.doesNotMatch(JSON.stringify(model.COPY.treg), /custom connector|claude\.ai/i);
  assert.match(model.COPY.treg.does, /capped at \$0\.10/);
  assert.match(model.COPY.treg.does, /post, send or buy stay switched off/);
  assert.match(model.COPY.treg.privacy, /private file on this computer/);
  assert.deepStrictEqual(model.action(item('treg', 'extension', 'not_set_up')), {kind: 'key', label: 'Add key'});
  assert.deepStrictEqual(model.action(item('treg', 'extension', 'connected')), {kind: 'key', label: 'Change key', quiet: true});
  assert.deepStrictEqual(model.action(item('treg', 'extension', 'not_installed')), {kind: 'none'});
  assert.deepStrictEqual(model.note(item('treg', 'extension', 'not_set_up')), {text: 'Add your treg key to turn it on.', tone: 'warn'});
  assert.strictEqual(model.note(item('treg', 'extension', 'not_installed')).tone, 'info');
  assert.strictEqual(model.note(item('treg', 'extension', 'connected')), null);
});

test('openrouter is connected by signing in, with a pasted key as the other way', () => {
  assert.deepStrictEqual(model.action(item('openrouter', 'extension', 'not_set_up')), {kind: 'connect', label: 'Connect', key: 'Paste a key'});
  assert.deepStrictEqual(model.action(item('openrouter', 'extension', 'connected')), {kind: 'key', label: 'Change key', quiet: true});
  assert.deepStrictEqual(model.action(item('openrouter', 'extension', 'not_installed')), {kind: 'none'});
  assert.deepStrictEqual(model.note(item('openrouter', 'extension', 'not_set_up')), {text: 'Connect your OpenRouter account to turn on voice input.', tone: 'warn'});
  assert.strictEqual(model.note(item('openrouter', 'extension', 'not_installed')).tone, 'info');
  assert.strictEqual(model.note(item('openrouter', 'extension', 'connected')), null);
  assert.match(model.COPY.openrouter.does, /Ctrl\+Shift\+Space/);
  assert.match(model.COPY.openrouter.privacy, /only when you press the microphone/);
  assert.match(model.COPY.openrouter.needs, /Your own OpenRouter account/);
});

test('no Claude connector is described or offered', () => {
  for (const id of ['gmail', 'google_calendar', 'google_drive', 'notion', 'slack']) {
    assert.strictEqual(model.COPY[id], undefined, id);
    assert.strictEqual(model.ICONS[id], undefined, id);
    assert.deepStrictEqual(model.action(item(id, 'claude', 'not_added')), {kind: 'none'});
  }
  assert.deepStrictEqual(Object.keys(model).sort(), ['COPY', 'ICONS', 'LINK_STEPS', 'STATUS', 'action', 'note', 'polls', 'status']);
});

test('whatsapp is linked from its card: Link phone, Cancel while the code shows, Try again after a failure', () => {
  const whatsapp = status => item('whatsapp', 'extension', status);
  assert.deepStrictEqual(model.action(whatsapp('not_set_up')), {kind: 'wa-link', label: 'Link phone'});
  assert.deepStrictEqual(model.action(whatsapp('linking')), {kind: 'wa-cancel', label: 'Cancel', quiet: true});
  assert.deepStrictEqual(model.action(whatsapp('finishing')), {kind: 'none'});
  assert.deepStrictEqual(model.action(whatsapp('failed')), {kind: 'wa-link', label: 'Try again'});
  assert.deepStrictEqual(model.action(whatsapp('connected')), {kind: 'none'});
  assert.deepStrictEqual(model.action(item('whatsapp', 'extension', 'connected', {send_ready: false})), {kind: 'wa-link', label: 'Finish setup', quiet: true});
  assert.strictEqual(model.status(whatsapp('linking')).label, 'Linking');
  assert.strictEqual(model.status(whatsapp('unsupported')).label, 'Not available');
  assert.deepStrictEqual(model.note(whatsapp('unsupported')), {text: 'WhatsApp can’t be linked on this computer yet.', tone: 'info'});
  assert.deepStrictEqual(model.note(item('whatsapp', 'extension', 'failed', {note: 'The code expired before it was scanned.'})),
    {text: 'The code expired before it was scanned.', tone: 'warn'});
  assert.match(model.LINK_STEPS.join(' '), /Linked devices.*Link a device/);
  assert.deepStrictEqual(['not_set_up', 'linking', 'finishing', 'connected', 'failed'].filter(state => model.polls(whatsapp(state))), ['linking', 'finishing']);
  assert.strictEqual(model.polls(item('treg', 'extension', 'linking')), false);
});

test('browsing is not a connector the owner sees', () => {
  for (const id of ['browser', 'chrome', 'playwright']) assert.strictEqual(model.COPY[id], undefined, id);
});
