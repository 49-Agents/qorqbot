const test = require('node:test');
const assert = require('node:assert');
const fs = require('node:fs');
const path = require('node:path');
const model = require('../js/voice-input.js');

const key = (extra = {}) => ({code: 'Space', ctrlKey: true, shiftKey: true, altKey: false, metaKey: false, repeat: false, isComposing: false, ...extra});

test('the hotkey is Ctrl+Shift+Space and nothing near it', () => {
  assert.strictEqual(model.isHotkey(key()), true);
  for (const other of [{ctrlKey: false}, {shiftKey: false}, {altKey: true}, {metaKey: true}, {repeat: true}, {isComposing: true}, {code: 'KeyM'}]) {
    assert.strictEqual(model.isHotkey(key(other)), false, JSON.stringify(other));
  }
  assert.strictEqual(model.isHotkey(null), false);
  assert.strictEqual(model.hotkeyLabel('Linux x86_64'), 'Ctrl+Shift+Space');
  assert.strictEqual(model.hotkeyLabel('MacIntel'), '⌃⇧Space');
});

test('spoken text goes where the cursor is, spaced from the words around it', () => {
  assert.deepStrictEqual(model.insert('', 0, 0, ' hello there '), {value: 'hello there', cursor: 11, inserted: 'hello there'});
  assert.deepStrictEqual(model.insert('Please', 6, 6, 'send it'), {value: 'Please send it', cursor: 14, inserted: ' send it'});
  assert.strictEqual(model.insert('Please tomorrow', 7, 7, 'send it').value, 'Please send it tomorrow');
  assert.strictEqual(model.insert('Done.', 4, 4, 'for now').value, 'Done for now.');
  assert.strictEqual(model.insert('line one\n', 9, 9, 'line two').value, 'line one\nline two');
});

test('a selection is replaced and an empty result changes nothing', () => {
  assert.deepStrictEqual(model.insert('send the old draft', 9, 12, 'new'), {value: 'send the new draft', cursor: 12, inserted: 'new'});
  assert.deepStrictEqual(model.insert('keep this', 4, 4, '   '), {value: 'keep this', cursor: 4, inserted: ''});
  assert.strictEqual(model.insert('abc', undefined, undefined, 'def').value, 'abc def');
});

test('text beyond the message limit is not inserted', () => {
  assert.deepStrictEqual(model.insert('12345', 5, 5, 'abcdefgh', 9), {value: '12345 abc', cursor: 9, inserted: ' abc'});
  assert.strictEqual(model.insert('123456789', 9, 9, 'more', 9).inserted, '');
});

test('a recording is sent by its plain media type, in the first format the browser supports', () => {
  assert.strictEqual(model.mediaType('audio/webm;codecs=opus'), 'audio/webm');
  assert.strictEqual(model.mediaType('Audio/MP4'), 'audio/mp4');
  assert.strictEqual(model.mediaType(''), '');
  assert.strictEqual(model.pickRecording(type => type.startsWith('audio/mp4')), 'audio/mp4');
  assert.strictEqual(model.pickRecording(() => true), 'audio/webm;codecs=opus');
  assert.strictEqual(model.pickRecording(() => false), '');
  assert.strictEqual(model.pickRecording(() => { throw new Error('unsupported'); }), '');
});

test('a microphone problem is explained in the owner’s words', () => {
  assert.match(model.microphoneProblem({name: 'NotAllowedError'}), /Allow the microphone/);
  assert.match(model.microphoneProblem({name: 'NotFoundError'}), /No microphone/);
  assert.match(model.microphoneProblem({name: 'NotReadableError'}), /in use by another app/);
  assert.match(model.microphoneProblem(new Error('other')), /could not be started/);
});

test('the waveform reads silence as a dot and speech as a clearly taller bar', () => {
  assert.strictEqual(model.level([]), 0);
  assert.strictEqual(model.level(undefined), 0);
  assert.strictEqual(model.level(new Uint8Array(256).fill(128)), 0);
  assert.strictEqual(model.level(Uint8Array.from({length: 256}, (_, i) => 128 + (i % 2 ? 1 : -1))), 0, 'room noise stays a dot');
  const tone = amplitude => Uint8Array.from({length: 512}, (_, i) => Math.round(128 + 127 * amplitude * Math.sin(i / 8)));
  const quiet = model.level(tone(0.07)), loud = model.level(tone(0.5));
  assert.ok(quiet > 0.3 && quiet < loud, `ordinary speech is visible: ${quiet}`);
  assert.ok(loud <= 1 && model.level(tone(1)) === 1);
  assert.strictEqual(model.barHeight(0), model.BAR_MIN);
  assert.strictEqual(model.barHeight(1), model.BAR_MAX);
  assert.strictEqual(model.barHeight(0.5), 13);
  assert.strictEqual(model.barHeight(-1), model.BAR_MIN);
  assert.strictEqual(model.barHeight(7), model.BAR_MAX);
  assert.strictEqual(model.barHeight(NaN), model.BAR_MIN);
});

test('the waveform history scrolls: the newest level enters at the end and the oldest leaves', () => {
  assert.deepStrictEqual(model.pushLevel([], 0.5, 4), [0, 0, 0, 0.5]);
  assert.deepStrictEqual(model.pushLevel([1, 2, 3, 4], 5, 4), [2, 3, 4, 5]);
  assert.deepStrictEqual(model.pushLevel([3, 4], 5, 4), [0, 3, 4, 5]);
  assert.deepStrictEqual(model.pushLevel([1, 2], 3, 1), [3]);
  const start = model.pushLevel(null, 0);
  assert.strictEqual(start.length, model.WAVE_BARS);
  assert.ok(start.every(value => value === 0));
  assert.strictEqual(model.WAVE_TICK_MS, 80);
});

test('the recording clock reads m:ss', () => {
  assert.strictEqual(model.elapsedLabel(0), '0:00');
  assert.strictEqual(model.elapsedLabel(7999), '0:07');
  assert.strictEqual(model.elapsedLabel(65000), '1:05');
  assert.strictEqual(model.elapsedLabel(model.MAX_SECONDS * 1000), '15:00');
  assert.strictEqual(model.elapsedLabel(-5), '0:00');
  assert.strictEqual(model.elapsedLabel(undefined), '0:00');
});

test('the dashboard loads voice input and the composer offers the microphone', () => {
  const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
  assert.match(read('index.html'), /<script src="\/js\/voice-input\.js"><\/script>/);
  assert.match(read('js/chat-view.js'), /voiceInput\.attach\(\{form, input, before: send/);
  assert.match(read('css/chat-view.css'), /\.gchat-mic\[data-state="recording"\]/);
  // The waveform is a grid column only while it is shown, and the model pill goes before it.
  assert.match(read('css/chat-view.css'), /\.gchat-composer:has\(\.gchat-attach\):has\(> \.gchat-wave:not\(\[hidden\]\)\) \{ grid-template-columns:minmax\(0,1fr\) auto auto auto auto; \}/);
  assert.match(read('js/global-chat.js'), /form\.querySelector\('\.gchat-wave, \.gchat-attach, \.gchat-mic, \.gchat-send'\)/);
});

test('a blocking Signoff has a bordeaux wash in the queue and a bordeaux frame in the list', () => {
  const read = file => fs.readFileSync(path.join(__dirname, '..', file), 'utf8');
  assert.match(read('js/attention-queue.js'), /data-blocking="true"/);
  assert.match(read('css/attention-review.css'), /\.focus-signoff-chip\[data-blocking="true"\][^}]*\{border-color:transparent;box-shadow:none;background:rgba\(118,18,30,\.38\)/);
  assert.match(read('js/signoffs.js'), /classList\.toggle\('sg-row-blocking', s\.state === 'pending' && !!s\.attention\?\.blocking\)/);
  assert.match(read('css/signoffs.css'), /\.sg-row\.sg-row-blocking[^}]*\{ border-color:#6B1520/);
});
