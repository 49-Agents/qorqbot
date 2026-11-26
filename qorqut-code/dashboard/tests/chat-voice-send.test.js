'use strict';
// Sending while recording stops, transcribes and sends; the microphone spins while the words become text; the text line is
// tucked away until the pointer is over the chat, which also puts the caret in it; the stop square is 1.6 times its old size.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const root = path.join(__dirname, '..');

async function fixture(run) {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage({viewport: {width: 900, height: 800}});
    await page.setContent('<main id="chat" style="display:flex;flex-direction:column;height:500px;width:600px"></main><div id="away" style="height:200px"></div>');
    for (const file of ['app.css', 'css/chat-view.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.evaluate(() => {
      window.esc = v => String(v ?? ''); window.agentGlyph = () => ''; window.coreData = x => x; window.toast = text => toasts.push(text);
      window.toasts = []; window.sent = []; window.stops = 0;
      delete window.AudioContext; delete window.webkitAudioContext;
      let release;
      window.transcribed = new Promise(resolve => { release = resolve; });
      window.finishTranscription = () => release({text: 'hello there'});
      window.api = async url => url === '/root/connectors' ? {connectors: [{id: 'openrouter', status: 'connected'}]} : window.transcribed;
      Object.defineProperty(navigator, 'mediaDevices', {value: {getUserMedia: async () => ({getTracks: () => [{stop() {}}]})}});
      window.MediaRecorder = class extends EventTarget {
        static isTypeSupported() { return true; }
        constructor() { super(); this.state = 'inactive'; this.mimeType = 'audio/webm'; }
        start() { this.state = 'recording'; }
        stop() {
          this.state = 'inactive';
          const data = new Event('dataavailable'); data.data = new Blob(['x'], {type: 'audio/webm'});
          this.dispatchEvent(data); this.dispatchEvent(new Event('stop'));
        }
      };
    });
    await page.addScriptTag({path: path.join(root, 'js/voice-input.js')});
    await page.addScriptTag({path: path.join(root, 'js/chat-view.js')});
    await page.evaluate(() => {
      window.view = mountChatView(document.querySelector('#chat'), {onSubmit: text => sent.push(text), onStop: () => { stops++; }});
      view.render({entries: [], composer: {stop: true}});
    });
    // The buttons show only while the pointer is over the chat.
    await page.mouse.move(100, 100);
    await page.waitForSelector('.gchat-mic:not([hidden])');
    await run(page);
  } finally { await browser.close(); }
}
const browserTest = (name, run) => test(name, {skip: !available && 'Local Playwright Chromium is unavailable'}, () => fixture(run));

browserTest('Send pressed while recording stops, transcribes and sends; the microphone spins meanwhile', async page => {
  await page.mouse.move(100, 100);
  assert.equal(await page.getAttribute('.gchat-send', 'aria-label'), 'Stop');
  await page.click('.gchat-mic');
  await page.waitForSelector('.gchat-mic[data-state="recording"]');
  assert.equal(await page.getAttribute('.gchat-send', 'aria-label'), 'Send', 'a recording is a message on its way, not a stop');
  await page.click('.gchat-send');
  await page.waitForSelector('.gchat-mic[data-state="working"] .gchat-mic-spinner');
  assert.notEqual(await page.$eval('.gchat-mic', el => getComputedStyle(el).cursor), 'progress');
  assert.equal(await page.$eval('.gchat-wave', el => el.hidden), true, 'the waveform goes once the recording stops');
  // Pressing Send again while the words are turned into text sends once, not twice.
  await page.click('.gchat-send');
  assert.deepEqual(await page.evaluate(() => sent), []);
  await page.evaluate(() => finishTranscription());
  await page.waitForFunction(() => sent.length > 0);
  assert.deepEqual(await page.evaluate(() => [sent, stops]), [['hello there'], 0]);
  assert.equal(await page.$('.gchat-mic .gchat-mic-spinner'), null, 'the microphone icon is back');
});

browserTest('Escape discards the recording and a pending send with it', async page => {
  await page.mouse.move(100, 100);
  await page.click('.gchat-mic');
  await page.waitForSelector('.gchat-mic[data-state="recording"]');
  await page.keyboard.press('Escape');
  await page.waitForSelector('.gchat-mic[data-state="idle"]');
  assert.deepEqual(await page.evaluate(() => sent), []);
});

browserTest('the text line and buttons are tucked away until the pointer is over the chat, and stay while it has focus', async page => {
  const state = () => page.$eval('.gchat-composer', form => [form.classList.contains('gchat-composer-tucked'), Math.round(Math.max(form.querySelector('textarea').getBoundingClientRect().height, form.querySelector('.gchat-send').getBoundingClientRect().height))]);
  await page.mouse.move(100, 700);
  await page.waitForTimeout(400);
  assert.deepEqual(await state(), [true, 0]);
  await page.mouse.move(100, 100);
  await page.waitForTimeout(400);
  assert.deepEqual(await state(), [false, 36]);
  assert.equal(await page.evaluate(() => document.activeElement?.matches('.gchat-composer textarea')), true, 'pointing at the chat puts the caret in its input');
  await page.mouse.move(100, 700);
  await page.waitForTimeout(400);
  assert.deepEqual(await state(), [true, 0], 'nothing typed: the caret leaves with the pointer');
  await page.mouse.move(100, 100);
  await page.keyboard.type('hello');
  await page.mouse.move(100, 700);
  await page.waitForTimeout(400);
  assert.deepEqual(await state(), [false, 36], 'with typed text, it keeps the caret and stays open');
  await page.evaluate(() => document.activeElement.blur());
  await page.waitForTimeout(400);
  assert.deepEqual(await state(), [true, 0]);
});

browserTest('the feed keeps its size in every frame while the input folds and unfolds, the freed height becoming space above it', async page => {
  // Records the feed's height in every frame for a while, started after the move so it reads after the chat sets the space.
  const watch = () => page.evaluate(() => new Promise(resolve => {
    const feed = document.querySelector('.gchat-scroll'), seen = [], end = performance.now() + 450;
    const step = () => { seen.push(Math.round(feed.getBoundingClientRect().height)); performance.now() < end ? requestAnimationFrame(step) : resolve(seen); };
    requestAnimationFrame(step);
  }));
  await page.mouse.move(100, 100);
  await page.waitForTimeout(400);
  const open = await page.$eval('.gchat-scroll', el => Math.round(el.getBoundingClientRect().height));
  await page.mouse.move(100, 700);
  const fold = await watch();
  assert.equal(await page.$eval('.gchat-composer', form => form.classList.contains('gchat-composer-tucked')), true);
  assert.ok(fold.every(h => Math.abs(h - open) <= 1), `feed ${open}px open, while folding: ${fold.join(' ')}`);
  assert.ok(await page.$eval('.gchat-composer', form => parseFloat(getComputedStyle(form).marginTop)) > 30, 'the freed height is space above the input');
  await page.mouse.move(100, 100);
  const unfold = await watch();
  assert.ok(unfold.every(h => Math.abs(h - open) <= 1), `while unfolding: ${unfold.join(' ')}`);
  assert.equal(await page.$eval('.gchat-composer', form => form.style.marginTop), '', 'open again, no space is left behind');
});

browserTest('pointing at the chat never takes the caret from another text field', async page => {
  await page.mouse.move(100, 700);
  await page.evaluate(() => { const other = document.createElement('textarea'); other.id = 'other'; document.querySelector('#away').append(other); other.focus(); });
  await page.mouse.move(100, 100);
  await page.waitForTimeout(200);
  assert.equal(await page.evaluate(() => document.activeElement.id), 'other');
});

browserTest('the stop square is 1.6 times its old 16px', async page => {
  assert.equal(await page.$eval('.gchat-send.gchat-stop svg', svg => Math.round(svg.getBoundingClientRect().width * 10) / 10), 25.6);
});
