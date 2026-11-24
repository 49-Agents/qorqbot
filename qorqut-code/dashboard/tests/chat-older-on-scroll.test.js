'use strict';
// olderOnScroll (agent Chat): older messages load when the reader moves up at the top of the feed, never by a button and never by themselves.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const browserTest = (name, run) => test(name, {skip: !available && 'Local Playwright Chromium is unavailable'}, run);
const root = path.join(__dirname, '..');

async function fixture(run) {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage({viewport: {width: 600, height: 600}, hasTouch: true});
    await page.setContent('<main id="chat" style="display:flex;flex-direction:column;height:400px;width:500px"></main>');
    for (const file of ['app.css', 'css/chat-view.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.evaluate(() => {
      window.esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
      window.glyphCalls = []; window.agentGlyph = () => '';
    });
    await page.addStyleTag({content: '.gchat-msg { min-height: 120px; }'});  // tall rows so the feed really scrolls
    await page.addScriptTag({path: path.join(root, 'js/safe-links.js')});
    await page.addScriptTag({path: path.join(root, 'js/chat-view.js')});
    await page.evaluate(() => {
      window.asked = 0;
      window.rows = (from, to) => Array.from({length: to - from}, (_, i) => ({key: 'e' + (from + i), id: 'e' + (from + i), position: from + i, speaker: 'user', text: 'message ' + (from + i) + ' line\n\nline\n\nline\n\nline', timestamp: 1700000000000 + i}));
      window.start = (option, older = {visible: true, disabled: false}, many = true) => {
        document.querySelector('#chat').innerHTML = '';
        window.view = mountChatView(document.querySelector('#chat'), {olderOnScroll: option, onOlder: () => { asked++; }});
        view.render({entries: rows(100, many ? 140 : 102), older, composer: {hidden: true}});
        window.sc = document.querySelector('.gchat-scroll');
      };
      window.settle = () => new Promise(done => requestAnimationFrame(() => requestAnimationFrame(done)));
    });
    await run(page);
  } finally { await browser.close(); }
}
const asked = page => page.evaluate(() => asked);
const scrollTo = (page, top) => page.evaluate(async top => { sc.scrollTop = top; await settle(); }, top);

browserTest('olderOnScroll hides the button and loads only when the reader moves up near the top', async () => {
  await fixture(async page => {
    await page.evaluate(() => start(true));
    await page.evaluate(() => settle());
    assert.equal(await page.locator('.gchat-older').isHidden(), true, 'the button stays hidden although older messages exist');
    assert.equal(await asked(page), 0, 'nothing loads on mount or render');
    await scrollTo(page, 3000);
    await scrollTo(page, 3500);
    assert.equal(await asked(page), 0, 'downward scroll');
    await scrollTo(page, 1500);
    assert.equal(await asked(page), 0, 'upward scroll far from the top');
    await scrollTo(page, 60);
    assert.equal(await asked(page), 1, 'upward scroll within the edge of the top');
    await page.evaluate(() => { view.render({entries: rows(60, 140), older: {visible: true, disabled: false}, composer: {hidden: true}}, {scroll: 'prepend'}); });
    await page.evaluate(() => settle());
    assert.equal(await asked(page), 1, 'a prepend restoring the reader place does not load again by itself');
    await scrollTo(page, 0);
    const resting = await asked(page);
    await scrollTo(page, 40);
    assert.equal(await asked(page), resting, 'moving down from the top does not load');
  });
});

browserTest('olderOnScroll: wheel up at the top loads, wheel down or away from the top does not', async () => {
  await fixture(async page => {
    await page.evaluate(() => start(true));
    await scrollTo(page, 0);
    await page.evaluate(() => { asked = 0; });   // arriving at the top is itself an upward scroll
    await page.mouse.move(250, 200);
    await page.mouse.wheel(0, 100);
    await page.evaluate(() => settle());
    assert.equal(await asked(page), 0, 'wheel down');
    await scrollTo(page, 0);
    await page.evaluate(() => { asked = 0; });
    await page.mouse.wheel(0, -100);
    await page.evaluate(() => settle());
    assert.equal(await asked(page), 1, 'wheel up at the top (no scroll event fires at 0)');
    await scrollTo(page, 2000);
    const before = await asked(page);
    await page.evaluate(() => sc.dispatchEvent(new WheelEvent('wheel', {deltaY: -50})));
    assert.equal(await asked(page), before, 'wheel up far from the top');
  });
});

browserTest('olderOnScroll: a feed too short to scroll loads on wheel up, PageUp on the scroller, and a downward touch drag', async () => {
  await fixture(async page => {
    await page.evaluate(() => start(true, {visible: true, disabled: false}, false));
    await page.evaluate(() => sc.dispatchEvent(new WheelEvent('wheel', {deltaY: -10})));
    assert.equal(await asked(page), 1);
    await page.evaluate(() => { sc.focus(); sc.dispatchEvent(new KeyboardEvent('keydown', {key: 'PageUp', bubbles: true})); });
    assert.equal(await asked(page), 2, 'PageUp with the scroller focused');
    await page.evaluate(() => document.querySelector('.gchat-input, textarea')?.dispatchEvent(new KeyboardEvent('keydown', {key: 'ArrowUp', bubbles: true})));
    assert.equal(await asked(page), 2, 'a key in the composer is not a scroll');
    const touch = (type, y) => page.evaluate(([type, y]) => sc.dispatchEvent(new TouchEvent(type, {bubbles: true, touches: type === 'touchend' ? [] : [new Touch({identifier: 1, target: sc, clientX: 10, clientY: y})]})), [type, y]);
    await touch('touchstart', 100); await touch('touchmove', 110);
    assert.equal(await asked(page), 2, 'a drag under 24 px');
    await touch('touchmove', 140);
    assert.equal(await asked(page), 3, 'a drag down past 24 px');
    await touch('touchmove', 200); await touch('touchend', 0);
    assert.equal(await asked(page), 3, 'one drag asks once');
  });
});

browserTest('olderOnScroll does not load when nothing older is visible, while one is loading, or when the option is off', async () => {
  await fixture(async page => {
    for (const [option, older] of [[true, {visible: false, disabled: false}], [true, {visible: true, disabled: true}], [false, {visible: true, disabled: false}]]) {
      await page.evaluate(([option, older]) => { start(option, older); asked = 0; }, [option, older]);
      await scrollTo(page, 800); await scrollTo(page, 30);
      await page.evaluate(() => sc.dispatchEvent(new WheelEvent('wheel', {deltaY: -50})));
      assert.equal(await asked(page), 0, JSON.stringify([option, older]));
    }
    assert.equal(await page.locator('.gchat-older').isVisible(), true, 'with the option off the button is still offered');
    await page.locator('.gchat-older').click();
    assert.equal(await asked(page), 1, 'and still emits onOlder');
  });
});
