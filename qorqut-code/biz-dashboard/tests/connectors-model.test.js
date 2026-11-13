const test = require('node:test');
const assert = require('node:assert');
const model = require('../js/connectors.js');

const item = (id, kind, status, extra = {}) => ({id, kind, status, ...extra});

test('an extension that is not set up from the dashboard has no button', () => {
  assert.strictEqual(model.action(item('whatsapp', 'extension', 'coming_soon')).kind, 'none');
  assert.strictEqual(model.action(item('whatsapp', 'extension', 'not_installed')).kind, 'none');
});

test('notes explain what the owner has to do', () => {
  assert.match(model.note(item('whatsapp', 'extension', 'not_set_up')).text, /Link your phone/);
  assert.strictEqual(model.note(item('whatsapp', 'extension', 'connected')), null);
  assert.match(model.note(item('whatsapp', 'extension', 'not_paired')).text, /Linked devices/);
});

test('unknown statuses fall back safely', () => {
  assert.strictEqual(model.status({status: 'something_new'}).label, 'Unknown');
  assert.strictEqual(model.status({status: 'connected'}).tone, 'ok');
});

test('every catalog id has copy and an icon', () => {
  for (const id of ['treg', 'whatsapp']) {
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

test('no Claude connector is described or offered', () => {
  for (const id of ['gmail', 'google_calendar', 'google_drive', 'notion', 'slack']) {
    assert.strictEqual(model.COPY[id], undefined, id);
    assert.strictEqual(model.ICONS[id], undefined, id);
    assert.deepStrictEqual(model.action(item(id, 'claude', 'not_added')), {kind: 'none'});
  }
  assert.deepStrictEqual(Object.keys(model).sort(), ['COPY', 'ICONS', 'STATUS', 'action', 'note', 'status']);
});

test('whatsapp on a Mac says it is coming and has no button', () => {
  const whatsapp = item('whatsapp', 'extension', 'coming_soon');
  assert.strictEqual(model.action(whatsapp).kind, 'none');
  assert.strictEqual(model.status(whatsapp).label, 'Coming soon on Mac');
  assert.deepStrictEqual(model.note(whatsapp), {text: 'The WhatsApp extension is available for Windows (WSL) and Linux. A Mac version is in progress.', tone: 'info'});
});

test('browsing is not a connector the owner sees', () => {
  for (const id of ['browser', 'chrome', 'playwright']) assert.strictEqual(model.COPY[id], undefined, id);
});
