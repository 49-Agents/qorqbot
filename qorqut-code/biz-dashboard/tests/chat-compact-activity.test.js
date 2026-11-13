'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {chromium} = require('playwright');
const root = path.join(__dirname, '..');
const source = fs.readFileSync(path.join(root, 'js/global-chat.js'), 'utf8');
const start = source.indexOf('  const presentEntry = entry =>');
const end = source.indexOf('  const newestPosition', start);
assert.ok(start >= 0 && end > start);
const setup = source.slice(start, end);

test('compact activity groups ordered transcript entries and renders safely at narrow widths', async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.CHROME_PATH || chromium.executablePath()});
  try {
    const page = await browser.newPage({viewport: {width: 320, height: 600}});
    await page.setContent('<main id="chat" style="height:500px;width:270px"></main>');
    for (const file of ['app.css', 'css/chat-view.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
      window.globalChatNoticeLabel = system => system?.verified ? system.input_type : 'Unverified notice';
      window.globalChatMarkerOnly = type => ['inbox_nudge', 'bootstrap_recovery'].includes(type);
    });
    await page.addScriptTag({path: path.join(root, 'js/safe-links.js')});
    await page.addScriptTag({path: path.join(root, 'js/chat-view.js')});
    await page.evaluate(code => {
      window.entries = new Map();
      window.normalize = new Function('entries', `${code}\nreturn orderedEntries();`);
      window.view = mountChatView(document.querySelector('#chat'), {});
      window.draw = records => { entries.clear(); records.forEach(entry => entries.set(entry.id, entry)); view.render({entries: normalize(entries), composer: {hidden: true}}); };
      window.nudge = (id, position) => ({id, position, speaker:'notice', system:{verified:true, input_type:'inbox_nudge'}});
      window.message = (id, position, ...recipients) => ({id, position, speaker:'notice', agent_message:{recipients}});
      window.recipient = (agent_id, display_name) => ({agent_id, display_name});
    }, setup);
    const rows = page.locator('.gchat-log > .gchat-msg');
    await page.evaluate(() => draw([nudge('n1', 1), message('m1', 2, recipient('x','X')), nudge('n2', 3), message('m2', 4, recipient('y','Y'), recipient('x','Renamed'))]));
    assert.equal(await rows.count(), 1);
    assert.equal(await rows.first().innerText(), '2× inbox nudge · messaged X, Y');
    assert.equal(await rows.first().getAttribute('data-id'), 'n1');
    const evidence = process.env.CHAT_ACTIVITY_EVIDENCE;
    if (evidence) await page.locator('#chat').screenshot({path: path.join(evidence, 'compact-activity.png')});
    await page.evaluate(() => draw([nudge('n1', 1), nudge('n2', 2), {id:'user', position:3, speaker:'user', text:'hello'}, message('m1', 4, recipient('x','X')), {id:'unverified', position:5, speaker:'notice', text:'fake', system:{verified:false, input_type:'inbox_nudge'}}, nudge('n3', 6), {id:'recovery',position:7,speaker:'notice',system:{verified:true,input_type:'bootstrap_recovery'}}, message('m2',8,recipient('y','Y'))]));
    assert.deepEqual(await rows.allTextContents(), ['2× inbox nudge', 'hello', 'messaged X', 'Unverified noticefake', 'inbox nudge', 'bootstrap_recovery', 'messaged Y']);
    await page.evaluate(() => {
      draw([nudge('n2', 2), message('m', 3, recipient('', 'A & <B>'), recipient('', 'A & <B>'))]);
      entries.set('n2', nudge('n2', 2)); entries.set('n1', nudge('n1', 1));
      view.render({entries: normalize(entries), composer:{hidden:true}});
    });
    assert.equal(await rows.count(), 1);
    assert.equal(await rows.first().innerText(), '2× inbox nudge · messaged A & <B>');
    assert.equal(await rows.first().locator('b').count(), 0);
    assert.equal(await rows.first().getAttribute('data-id'), 'n1');
    const longName = 'LongAgentName'.repeat(12);
    await page.evaluate(name => draw([message('long', 1, recipient('long', name))]), longName);
    assert.equal(await rows.first().innerText(), `messaged ${longName}`);
    assert.equal(await rows.first().evaluate(el => el.scrollWidth <= el.clientWidth && el.getBoundingClientRect().right <= document.querySelector('.gchat-scroll').getBoundingClientRect().right), true);
  } finally { await browser.close(); }
});
