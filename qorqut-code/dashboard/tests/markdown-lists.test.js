'use strict';
// The chat's markdown (also used for Signoff explanations) nests list items by indentation.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const root = path.join(__dirname, '..');

test('nested bullets become nested lists, with bold and italics', {skip: (!chromium || !fs.existsSync(executable)) && 'Local Playwright Chromium is unavailable'}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage();
    await page.setContent('<main></main>');
    await page.evaluate(() => { window.esc = v => String(v ?? '').replace(/[&<>"']/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c])); });
    for (const file of ['js/safe-links.js', 'js/chat-view.js']) await page.addScriptTag({path: path.join(root, file)});
    const shape = await page.evaluate(() => {
      const main = document.querySelector('main');
      main.innerHTML = chatViewMarkdown('**49Agents** (top level): Strategy\n\n- **Marketing**: the four channels\n  - **SEO**: sites\n  - **Personal reputation**. Three stories:\n    - *Direct*: Dorado\n    - *Second-level*: podcast\n  - **Social**: LinkedIn\n- **Agency**: clients\n1. one\n2. two');
      const tree = list => [...list.children].map(li => {
        const own = [...li.childNodes].filter(n => !(n.nodeType === 1 && /^(UL|OL)$/.test(n.tagName))).map(n => n.textContent).join('').trim();
        const inner = li.querySelector(':scope > ul, :scope > ol');
        return inner ? [own, tree(inner)] : own;
      });
      return {strong: main.querySelector('p strong')?.textContent, em: main.querySelector('em')?.textContent, stars: main.textContent.includes('*'),
        lists: [...main.querySelectorAll(':scope > ul, :scope > ol')].map(l => [l.tagName, tree(l)])};
    });
    assert.deepEqual(shape, {strong: '49Agents', em: 'Direct', stars: false, lists: [
      ['UL', [['Marketing: the four channels', ['SEO: sites', ['Personal reputation. Three stories:', ['Direct: Dorado', 'Second-level: podcast']], 'Social: LinkedIn']], 'Agency: clients']],
      ['OL', ['one', 'two']],
    ]});
  } finally { await browser.close(); }
});
