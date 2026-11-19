const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* optional local browser test dependency */ }
const available = !!chromium && fs.existsSync(chromium.executablePath());
const skip = {skip: !available && 'Playwright Chromium unavailable'};

// The script the supervisor installs in every document (qorqut/browser/cookie_banners.js), run on pages modelling banners and
// look-alikes. The wider set of real-Chrome pages is scripts/check_browser_tabs.py; these cover what a refusal may be clicked in.
const SCRIPT = path.join(__dirname, '../../app/qorqut/browser/cookie_banners.js');
const FLOAT = 'position:fixed;bottom:0;left:0;right:0;background:#eee;padding:12px';
const SENTENCE = 'We use cookies and similar technologies to improve your experience and to measure how the site is used.';
const buttons = (accept = 'Accept all', reject = 'Reject all') => `<button onclick="c('accept')">${accept}</button> <button onclick="c('reject')">${reject}</button>`;
const PAGES = {
  // Clicked: a refusal inside a banner.
  'floating-banner': [`<div style="${FLOAT}"><p>${SENTENCE}</p>${buttons()}</div>`, ['reject']],
  'vendor-box-not-floating': [`<div id="onetrust-banner-sdk"><p>This site stores data on your device.</p>${buttons()}</div>`, ['reject']],
  'cookie-named-floating-box-about-privacy': [`<div class="cookie-bar" style="${FLOAT}"><p>Your privacy matters.</p>${buttons('Accept', 'Decline')}</div>`, ['reject']],
  'refusal-link-to-the-same-page': [`<div style="${FLOAT}"><p>${SENTENCE}</p><button onclick="c('accept')">Accept</button> <a href="#" onclick="c('reject');return false">Decline</a></div>`, ['reject']],
  // Left alone: a consent-looking name is not a banner.
  'admin-cookie-settings-section': [`<section id="cookie-settings"><h2>Cookie settings</h2><p>Requests from your team to change cookie settings.</p>${buttons('Approve', 'Reject')}</section>`, []],
  'admin-gdpr-class-section': [`<div class="gdpr-requests"><p>Requests to delete personal data.</p>${buttons('Approve', 'Deny')}</div>`, []],
  'cookie-named-box-that-floats-but-says-nothing-of-consent': [`<div class="cookie-jar" style="${FLOAT}"><p>Three jars of biscuits are in the basket.</p>${buttons('Buy', 'Decline')}</div>`, []],
  'cookie-named-box-too-big': [`<div class="cookie-bar" style="${FLOAT}"><p>${SENTENCE}</p>${'<p>Partner text about privacy.</p>'.repeat(200)}${buttons()}</div>`, []],
  'refusal-link-that-leaves-the-page': [`<div style="${FLOAT}"><p>${SENTENCE}</p><button onclick="c('accept')">Accept</button> <a href="/elsewhere" onclick="c('reject');return false">Reject all</a></div>`, []],
  'vendor-refusal-link-that-leaves-the-page': [`<div id="onetrust-banner-sdk"><p>${SENTENCE}</p><a href="https://other.example/optout" onclick="c('reject');return false">Reject all</a></div>`, []],
};

test('a refusal is clicked only inside a banner, and never as a link that leaves the page', skip, async () => {
  const browser = await chromium.launch({headless: true});
  try {
    const context = await browser.newContext();
    const shell = body => `<!doctype html><meta charset=utf-8><script>function c(x){(window.clicks=window.clicks||[]).push(x)}</script><h1>Page</h1><p>${'Ordinary text. '.repeat(30)}</p>${body}`;
    await context.route('http://pages.test/**', route => route.fulfill({contentType: 'text/html', body: shell(PAGES[new URL(route.request().url()).pathname.slice(1)][0])}));
    const source = fs.readFileSync(SCRIPT, 'utf8');
    await Promise.all(Object.entries(PAGES).map(async ([name, [, expected]]) => {
      const page = await context.newPage();
      await page.goto(`http://pages.test/${name}`);
      await page.evaluate(source);
      await page.waitForTimeout(1800);                              // the script looks at 0, 0.5 and 1 second
      assert.deepEqual(await page.evaluate(() => window.clicks || []), expected, name);
      assert.equal(new URL(page.url()).pathname, `/${name}`, `${name}: the page stayed where it was`);
    }));
  } finally { await browser.close(); }
});
