'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const root = path.join(__dirname, '..');

test('owner URLs remain links without automatic external cards, while agent cards and local references remain', {skip: !available && 'Local Playwright Chromium is unavailable'}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage();
    await page.route('http://chat.test/', route => route.fulfill({contentType:'text/html', body:'<main id="chat" style="height:400px;width:400px"></main>'}));
    await page.goto('http://chat.test/');
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
      window.state = {bizs: []};
      window.post = () => new Promise(() => {});
    });
    await page.addScriptTag({path: path.join(root, 'js/safe-links.js')});
    await page.addScriptTag({path: path.join(root, 'js/chat-view.js')});
    await page.evaluate(() => {
      window.view = mountChatView(document.querySelector('#chat'), {});
      window.text = 'https://example.com/page https://example.com/photo.png [bad](javascript:alert)';
      window.draw = () => view.render({entries:[{key:'owner', speaker:'user', text}, {key:'agent', speaker:'agent', text}], pending:[{key:'pending', speaker:'user', state:'failed', resendable:true, text}], composer:{hidden:true}});
      draw();
    });
    const owner = page.locator('[data-id="owner"]'), agent = page.locator('[data-id="agent"]'), pending = page.locator('[data-input-id="pending"]');
    for (const row of [owner, pending]) {
      assert.equal(await row.locator('.gchat-body a.safe-link').count(), 2);
      assert.equal(await row.locator('.gchat-references').count(), 0);
      assert.equal(await row.locator('a[href^="javascript:"]').count(), 0);
      assert.equal(await row.locator('a.safe-link').first().getAttribute('href'), 'https://example.com/page');
    }
    assert.equal(await agent.locator('.gchat-ref-row').count(), 2);
    assert.equal(await agent.locator('.gchat-file-reference').count(), 1);
    await page.evaluate(() => draw());
    assert.equal(await owner.locator('.gchat-references').count(), 0);
    assert.equal(await pending.locator('.gchat-references').count(), 0);
    assert.equal(await agent.locator('.gchat-ref-row').count(), 2);
    await page.evaluate(() => view.render({entries:[{key:'local', speaker:'user', text:'[upload](/files/view?path=%2Foperations%2Fagents%2Fa%2Fdownloads%2Ffile.pdf) https://example.com/page', references:[{key:'explicit', label:'Explicit', value:{type:'task', id:'other', label:'Explicit'}}]}, {key:'typed', speaker:'agent', text:'https://example.com/page', typedReferences:[{type:'task', id:'task', label:'Task'}]}], pending:[], composer:{hidden:true}}));
    const local = page.locator('[data-id="local"]');
    assert.equal(await local.locator('.gchat-file-reference').count(), 1);
    assert.equal(await local.locator('.gchat-body a.safe-link').count(), 1);
    assert.equal(await local.locator('.gchat-body').textContent(), ' https://example.com/page');
    assert.equal(await page.locator('[data-id=typed] .gchat-typed').count(), 1);
    assert.equal(await local.locator('[data-ref-key=explicit]').count(), 1);
  } finally { await browser.close(); }
});
