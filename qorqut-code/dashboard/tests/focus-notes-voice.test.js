'use strict';
// The Focus notes carry a floating 48px microphone in their bottom right corner: the same button, pulse and waveform as the chat
// input, and the words go where the caret was.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Browser checks are optional. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const browserTest = (name, fn) => test(name, {skip: (!chromium || !fs.existsSync(executable)) && 'Local Playwright Chromium is unavailable'}, fn);
const root = path.join(__dirname, '..');

async function fixture(run) {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage({viewport: {width: 900, height: 700}});
    await page.setContent('<div class="agpad"><div class="tbody"><section class="agfocus" style="height:500px;display:flex;flex-direction:column"><div class="agfocus-layout" style="flex:1;display:flex"><div class="agfocus-notes"></div></div></section></div></div>');
    await page.addStyleTag({content: ':root{--fg:#eee;--bg:#111;--line:#333;--sans:Arial}'});
    for (const file of ['app.css', 'css/status-ball.css', 'css/notes.css', 'css/agent-focus.css', 'css/chat-view.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.evaluate(() => {
      let n = 0, saved = {revision: 1, card_ids: [], blocks: [{id: 'b1', text: 'Call the bank\nPlan launch', before_id: null, after_id: null}], statuses: {}};
      window.newRequestId = () => `00000000-0000-4000-8000-00000000000${++n}`;
      window.coreData = x => { if (x?.status === 'error') { const e = new Error('Something went wrong inside Qorqbot'); e.serverMessage = x.error.message; throw e; } return x; }; window.toasts = []; window.toast = text => toasts.push(text);
      let release;
      window.heard = new Promise(resolve => { release = resolve; });
      window.finishTranscription = () => release({text: 'tomorrow'});
      window.failTranscription = () => release({status: 'error', error: {code: 'operation_unavailable', message: 'Your OpenRouter balance is too low for voice input. Add credits at openrouter.ai.'}, httpStatus: 503});
      window.api = async url => url === '/root/connectors' ? {connectors: [{id: 'openrouter', status: 'connected'}]}
        : url.includes('transcribe') ? window.heard : structuredClone(saved);
      window.post = async (url, data) => { saved = {...saved, revision: saved.revision + 1, blocks: structuredClone(data.blocks)}; return structuredClone(saved); };
      delete window.AudioContext; delete window.webkitAudioContext;
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
    for (const file of ['status-ball.js', 'lines-editor.js', 'focus-text.js', 'voice-input.js']) await page.addScriptTag({path: path.join(root, 'js', file)});
    await page.evaluate(() => { window.panel = {agentId: 'agent', focusMount: document.querySelector('.agfocus')}; window.dispose = focusText.mount(panel, panel.focusMount); });
    await page.waitForFunction(() => !document.querySelector('.agfocus-notes-input').classList.contains('loading'));
    await page.waitForSelector('.agfocus-notes-voice .gchat-mic:not([hidden])');
    await run(page);
  } finally { await browser.close(); }
}

browserTest('the notes microphone floats in the bottom right corner, 48px, and puts the words where the caret was', async () => fixture(async page => {
  const box = await page.evaluate(() => {
    const notes = document.querySelector('.agfocus-notes').getBoundingClientRect(), mic = document.querySelector('.agfocus-notes-voice .gchat-mic').getBoundingClientRect();
    return {right: Math.round(notes.right - mic.right), bottom: Math.round(notes.bottom - mic.bottom), size: Math.round(mic.width)};
  });
  assert.deepEqual(box, {right: 12, bottom: 12, size: 48});
  // Caret after "Call the".
  await page.click('.agfocus-notes .ntdoc');
  await page.evaluate(() => {
    const line = document.querySelector('.agfocus-notes .ntdoc').firstElementChild, walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
    const text = walker.nextNode(), at = text.data.indexOf('the') + 3;
    getSelection().setBaseAndExtent(text, at, text, at);
  });
  await page.click('.agfocus-notes-voice .gchat-mic');
  await page.waitForSelector('.agfocus-notes-voice .gchat-mic[data-state="recording"]');
  assert.notEqual(await page.$eval('.agfocus-notes-voice .gchat-mic', el => getComputedStyle(el).animationName), 'none', 'it pulses while on');
  assert.equal(await page.$eval('.agfocus-notes-voice .gchat-wave', el => !el.hidden && el.getBoundingClientRect().width > 40), true, 'the waveform shows beside it');
  assert.equal(await page.evaluate(() => { const w = document.querySelector('.agfocus-notes-voice .gchat-wave').getBoundingClientRect(), m = document.querySelector('.agfocus-notes-voice .gchat-mic').getBoundingClientRect(), n = document.querySelector('.agfocus-notes').getBoundingClientRect(); return w.right <= m.left && w.left >= n.left; }), true, 'the waveform sits to its left, inside the notes');
  await page.screenshot({path: process.env.NOTES_MIC_SHOT || '/dev/null'}).catch(() => {});
  await page.click('.agfocus-notes-voice .gchat-mic');
  await page.waitForSelector('.agfocus-notes-voice .gchat-mic .gchat-mic-spinner');
  await page.evaluate(() => finishTranscription());
  await page.waitForFunction(() => focusText.state('agent').lines.join('\n').includes('tomorrow'));
  assert.equal(await page.evaluate(() => focusText.state('agent').lines.join('\n')), 'Call the tomorrow bank\nPlan launch');
  assert.deepEqual(await page.evaluate(() => toasts), []);
}));

browserTest('with no caret placed yet, the words go at the end', async () => fixture(async page => {
  await page.evaluate(() => { focusText.state('agent').view.caret = null; });
  await page.click('.agfocus-notes-voice .gchat-mic');
  await page.waitForSelector('.agfocus-notes-voice .gchat-mic[data-state="recording"]');
  await page.click('.agfocus-notes-voice .gchat-mic');
  await page.evaluate(() => finishTranscription());
  await page.waitForFunction(() => focusText.state('agent').lines.join('\n').includes('tomorrow'));
  assert.equal(await page.evaluate(() => focusText.state('agent').lines.join('\n')), 'Call the bank\nPlan launch tomorrow');
}));

browserTest('a refused transcription shows the reason the service gives, and the notes are left as they were', async () => fixture(async page => {
  await page.click('.agfocus-notes-voice .gchat-mic');
  await page.waitForSelector('.agfocus-notes-voice .gchat-mic[data-state="recording"]');
  await page.click('.agfocus-notes-voice .gchat-mic');
  await page.evaluate(() => failTranscription());
  await page.waitForFunction(() => toasts.length > 0);
  assert.deepEqual(await page.evaluate(() => toasts), ['Your OpenRouter balance is too low for voice input. Add credits at openrouter.ai.']);
  assert.equal(await page.evaluate(() => focusText.state('agent').lines.join('\n')), 'Call the bank\nPlan launch');
}));

browserTest('the hotkey records into the notes only while they have the caret', async () => fixture(async page => {
  // A second, chat-like field mounted before the notes would otherwise lose the hotkey to them.
  await page.evaluate(() => {
    const form = document.createElement('form'), input = document.createElement('textarea');
    form.id = 'chat'; form.append(input); document.body.prepend(form);
    window.chatMic = voiceInput.attach({form, input});
  });
  await page.waitForSelector('#chat .gchat-mic:not([hidden])');
  await page.evaluate(() => document.activeElement?.blur());
  await page.keyboard.press('Control+Shift+Space');
  await page.waitForSelector('#chat .gchat-mic[data-state="recording"]');
  assert.equal(await page.$eval('.agfocus-notes-voice .gchat-mic', el => el.dataset.state), 'idle', 'with nothing typed in, the chat records');
  await page.keyboard.press('Escape');
  await page.waitForSelector('#chat .gchat-mic[data-state="idle"]');
  await page.click('.agfocus-notes .ntdoc');
  await page.keyboard.press('Control+Shift+Space');
  await page.waitForSelector('.agfocus-notes-voice .gchat-mic[data-state="recording"]');
  assert.equal(await page.$eval('#chat .gchat-mic', el => el.dataset.state), 'idle');
}));
