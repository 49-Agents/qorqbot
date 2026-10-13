// Focused regressions for the 2026-10-13 QA repairs. Synthetic API only; no native provider is involved.
import { test as base, expect, type Page, type Route } from '@playwright/test';
import { SyntheticAPI, alpha, beta, makeAgent } from './synthetic-api';
import { Store, meaningful, nameProblem } from '../src/store';

const test = base.extend<{ api: SyntheticAPI }>({
  api: async ({}, use) => { const api = await new SyntheticAPI().start(); try { await use(api); } finally { await api.close(); } },
  page: async ({ page }, use) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await use(page);
    expect(errors).toEqual([]);
  },
});
// Headless Chromium hides scrollbars by default; geometry checks need the real ones.
test.use({ launchOptions: { executablePath: '/usr/bin/google-chrome', ignoreDefaultArgs: ['--hide-scrollbars'] } });
const message = (page: Page) => page.getByRole('textbox', { name: /^Message / });
const calls = (api: SyntheticAPI, name: string) => api.calls.filter(c => c.name === name);
const refusal = { status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'unavailable', message: 'Synthetic transient refusal' } }) };
const rejection = { status: 409, contentType: 'application/json', body: JSON.stringify({ ok: false, operation_id: null, outcome: null, data: null, event_seq: null, error: { code: 'invalid_argument', message: 'Synthetic definitive rejection', retryable: false } }) };
const evidence = process.env.QA_EVIDENCE_DIR;
async function open(page: Page, api: SyntheticAPI) {
  await page.goto(api.url);
  await expect(page.getByRole('button', { name: 'Chat', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(message(page)).toBeVisible();
}
const poll = (page: Page) => page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
const body = (route: Route) => JSON.parse(route.request().postData() || '{}');

test('blank bodies never create a request on any entry path; valid bodies keep exact bytes', async ({ page, api }) => {
  expect(meaningful('  \n\t')).toBe(false);
  const store = new Store();
  store.supported.add('messages_send');
  store.setDraft(alpha, '  \n ');
  await store.send(alpha);
  expect(store.pending.size).toBe(0);
  await open(page, api);
  const send = page.getByRole('button', { name: 'Send', exact: true });
  await message(page).fill(' \n\t  ');
  await expect(send).toBeDisabled();
  await message(page).press('Enter');
  await page.locator('form.composer').evaluate((form: HTMLFormElement) => form.requestSubmit());
  await send.evaluate((button: HTMLButtonElement) => { button.disabled = false; button.click(); });
  await page.waitForTimeout(300);
  expect(calls(api, 'messages_send')).toHaveLength(0);
  const exact = '  héllo 😀 \n  tail  ';
  await message(page).fill(exact + '\n');
  await expect(send).toBeEnabled();
  await message(page).press('Enter');
  await expect.poll(() => calls(api, 'messages_send').length).toBe(1);
  expect(calls(api, 'messages_send')[0].args.body).toBe(exact + '\n');
});

test('a failed first read is an error with retry, not an empty conversation; a successful empty read is empty', async ({ page, api }) => {
  let fail = true;
  await page.route('**/api/v1/operations/conversation_list', route => fail ? route.fulfill(refusal) : route.continue());
  await page.goto(api.url);
  await expect(page.getByRole('button', { name: 'Retry history' })).toBeVisible();
  await expect(page.getByText('No messages saved yet.')).toHaveCount(0);
  await message(page).fill('kept draft');
  await poll(page);
  await page.waitForTimeout(400);
  await expect(page.getByText('No messages saved yet.')).toHaveCount(0);
  fail = false;
  await page.getByRole('button', { name: 'Retry history' }).click();
  await expect(page.getByText('No messages saved yet.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Retry history' })).toHaveCount(0);
  await expect(message(page)).toHaveValue('kept draft');
  // A later live-refresh failure keeps loaded history and never reports it as empty history loss.
  api.add(alpha, 'visible history');
  await poll(page);
  await expect(page.getByText('visible history')).toBeVisible();
  fail = true;
  await poll(page);
  await expect(page.getByRole('button', { name: 'Retry history' })).toBeVisible();
  await expect(page.getByText('visible history')).toBeVisible();
});

test('a failed earlier page survives successful polls and retries from the same cursor without duplicates', async ({ page, api }) => {
  for (let i = 1; i <= 60; i++) api.add(alpha, `m${i}`);
  const earlier: number[] = [];
  let fail = true;
  await page.route('**/api/v1/operations/conversation_list', route => {
    const args = body(route);
    if (args.before_seq === undefined) return route.continue();
    earlier.push(args.before_seq);
    return fail ? route.fulfill(refusal) : route.continue();
  });
  await open(page, api);
  await expect(page.getByText('m60', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Load earlier', exact: true }).click();
  const retry = page.getByRole('button', { name: 'Retry earlier messages' });
  await expect(retry).toBeVisible();
  const before = calls(api, 'conversation_list').length;
  api.add(alpha, 'm61');
  await poll(page);
  await expect(page.getByText('m61', { exact: true })).toBeVisible();
  expect(calls(api, 'conversation_list').length).toBeGreaterThan(before);
  await expect(retry).toBeVisible();
  fail = false;
  await retry.click();
  await expect(page.getByText('m1', { exact: true })).toBeVisible();
  await expect(retry).toHaveCount(0);
  expect(new Set(earlier).size).toBe(1);
  const ids = await page.locator('[data-entry-id]').evaluateAll(rows => rows.map(row => row.getAttribute('data-entry-id')));
  expect(ids).toHaveLength(61);
  expect(new Set(ids).size).toBe(61);
});

test('name validation counts Unicode code points and corrects in place', async ({ page, api }) => {
  expect(nameProblem('😀'.repeat(120))).toBe('');
  expect(nameProblem('😀'.repeat(121))).not.toBe('');
  expect(nameProblem('   ')).not.toBe('');
  expect(nameProblem('ok\ud800')).not.toBe('');
  expect(nameProblem(` ${'x'.repeat(120)} `)).toBe('');
  await open(page, api);
  await page.getByRole('button', { name: 'Create agent', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Create agent' });
  const field = dialog.getByLabel('Display name');
  const submit = dialog.getByRole('button', { name: 'Create agent', exact: true });
  await submit.click();
  await expect(dialog.getByRole('alert')).toHaveText('Name must be 1–120 characters.');
  await field.fill('😀'.repeat(121));
  await submit.click();
  await expect(dialog.getByRole('alert')).toHaveText('Name must be 1–120 characters.');
  expect(calls(api, 'agents_create')).toHaveLength(0);
  await field.fill('  ' + '😀'.repeat(120) + '  ');
  await submit.click();
  await expect(dialog).toHaveCount(0);
  expect(calls(api, 'agents_create')).toHaveLength(1);
  expect(calls(api, 'agents_create')[0].args.display_name).toBe('😀'.repeat(120));
  // Inline rename: invalid text stays editable, a corrected value saves once.
  const inspector = page.getByRole('complementary', { name: 'Agent details' });
  const rename = inspector.getByRole('textbox', { name: /^Rename / });
  await inspector.locator('.identity-name').click();
  await rename.fill('   ');
  await rename.press('Enter');
  await expect(rename).toBeVisible();
  await expect(inspector.getByRole('alert')).toHaveText('Name must be 1–120 characters.');
  if (evidence) await page.screenshot({ path: `${evidence}/rename-validation-1440.png`, clip: { x: 1100, y: 0, width: 340, height: 220 } });
  await message(page).click();
  await expect(rename).toBeVisible();
  await rename.fill('x'.repeat(121));
  await rename.press('Enter');
  await expect(inspector.getByRole('alert')).toBeVisible();
  expect(calls(api, 'agents_update')).toHaveLength(0);
  await rename.fill('x' + '😀'.repeat(119));
  await rename.press('Enter');
  await expect(inspector.locator('.identity-name')).toHaveText('x' + '😀'.repeat(119));
  expect(calls(api, 'agents_update')).toHaveLength(1);
  await expect(inspector.locator('.identity-name')).toBeFocused();
});

test('a definitive failure is corrected with a fresh key; an uncertain one stays immutable and retries exactly', async ({ page, api }) => {
  await open(page, api);
  const inspector = page.getByRole('complementary', { name: 'Agent details' });
  const name = inspector.locator('.identity-name');
  const rename = inspector.getByRole('textbox', { name: /^Rename / });
  await page.route('**/api/v1/operations/agents_update', route => route.fulfill(rejection), { times: 1 });
  await name.click(); await rename.fill('First attempt'); await rename.press('Enter');
  await expect(inspector.getByText('Synthetic definitive rejection')).toBeVisible();
  await expect(inspector.getByText('Attempted name: “First attempt”.')).toBeVisible();
  await expect(inspector.getByRole('button', { name: 'Retry original request' })).toHaveCount(0);
  await expect(name).toBeEnabled();
  await name.click();
  await expect(rename).toHaveValue('First attempt');
  await rename.fill('Corrected'); await rename.press('Enter');
  await expect(name).toHaveText('Corrected');
  const keys = calls(api, 'agents_update').map(c => c.args.idempotency_key);
  expect(keys).toHaveLength(1);
  // The rejected request never reached the synthetic server; the corrected one is a new request.
  await expect(inspector.getByRole('alert')).toHaveCount(0);

  await page.route('**/api/v1/operations/agents_update', route => route.abort(), { times: 1 });
  await name.click(); await rename.fill('Lost confirmation'); await rename.press('Enter');
  await expect(inspector.getByText(/Confirmation uncertain/)).toBeVisible();
  await expect(name).toBeDisabled();
  const retry = inspector.getByRole('button', { name: 'Retry original request' });
  await expect(retry).toBeVisible();
  await retry.click();
  await expect(name).toHaveText('Lost confirmation');
  const sent = calls(api, 'agents_update');
  expect(sent).toHaveLength(2);
  expect(sent[1].args.display_name).toBe('Lost confirmation');
  expect(sent[1].args.idempotency_key).not.toBe(sent[0].args.idempotency_key);
});

test('sidebar rename icon opens inline rename without selecting; focus returns on Escape but not on blur', async ({ page, api }) => {
  await open(page, api);
  const nav = page.getByRole('navigation', { name: 'Agents' });
  const row = (n: string) => nav.locator('.agent-row').filter({ hasText: n });
  const selected = nav.getByRole('button', { name: /^Atlas Initializing/ });
  await expect(selected).toHaveAttribute('aria-pressed', 'true');
  await row('onbash').dblclick();
  await expect(nav.getByRole('textbox')).toHaveCount(0);
  await selected.click();
  await page.mouse.move(700, 500);
  const icon = nav.getByRole('button', { name: 'Rename onbash' });
  await expect(icon).toHaveCSS('opacity', '0');
  await row('onbash').hover();
  await expect(icon).toHaveCSS('opacity', '1');
  if (evidence) await page.screenshot({ path: `${evidence}/sidebar-rename-icon-hover-1440.png`, clip: { x: 0, y: 0, width: 260, height: 300 } });
  await icon.click();
  const input = nav.getByRole('textbox', { name: 'Rename onbash' });
  await expect(input).toBeFocused();
  await expect(selected).toHaveAttribute('aria-pressed', 'true');
  await expect(nav.getByRole('button', { name: /^onbash/ })).toHaveCount(0);
  await input.fill('discarded');
  await input.press('Escape');
  await expect(nav.getByRole('button', { name: 'Rename onbash' })).toBeFocused();
  expect(calls(api, 'agents_update')).toHaveLength(0);
  await expect(selected).toHaveAttribute('aria-pressed', 'true');
  await nav.getByRole('button', { name: 'Rename onbash' }).press('Enter');
  await nav.getByRole('textbox', { name: 'Rename onbash' }).fill('Background');
  await message(page).click();
  await expect(message(page)).toBeFocused();
  await expect.poll(() => calls(api, 'agents_update').length).toBe(1);
  expect(calls(api, 'agents_update')[0].args).toMatchObject({ agent_id: beta, display_name: 'Background' });
  await expect(nav.getByRole('button', { name: /^Atlas Initializing/ })).toHaveAttribute('aria-pressed', 'true');
  await expect(nav.getByRole('button', { name: 'Rename Background' })).toBeVisible();
  // Keyboard users skip every Copy button to reach the composer.
  api.add(alpha, 'one'); api.add(alpha, 'two');
  await poll(page);
  await expect(page.getByText('two', { exact: true })).toBeVisible();
  const skip = page.getByRole('link', { name: 'Skip to message' });
  await expect(skip).not.toBeInViewport();
  await skip.focus();
  await expect(skip).toBeInViewport();
  await skip.press('Enter');
  await expect(message(page)).toBeFocused();
  await page.getByRole('button', { name: 'Copy message' }).first().focus();
  await expect(page.getByRole('button', { name: 'Copy message' }).first()).toBeFocused();
});

test('copy feedback is visible, honest and keeps the status role', async ({ page, api }) => {
  api.add(alpha, 'copy me');
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: (text: string) => (window as any).copyRejected ? Promise.reject(new Error('denied')) : Promise.resolve((window as any).copied = text) } });
  });
  await open(page, api);
  const entry = page.locator('.entry', { hasText: 'copy me' });
  await entry.hover();
  await entry.getByRole('button', { name: 'Copy message' }).click();
  await expect(entry.getByRole('status')).toHaveText('Copied');
  await expect(entry.getByRole('status')).toBeVisible();
  expect(await page.evaluate(() => (window as any).copied)).toBe('copy me');
  await page.evaluate(() => { (window as any).copyRejected = true; });
  await entry.hover();
  await entry.getByRole('button', { name: 'Copy message' }).click();
  await expect(entry.getByRole('status')).toHaveText('Could not copy. Select the text to copy it.');
  await expect(entry.getByRole('status')).toBeVisible();
  if (evidence) await page.screenshot({ path: `${evidence}/copy-failure-1440.png` });
});

for (const size of [{ width: 1440, height: 960 }, { width: 937, height: 779 }]) {
  test(`scrollbars are a 1px gray thumb with no rail or arrows, text is readable, at ${size.width}x${size.height}`, async ({ page, api }) => {
    await page.setViewportSize(size);
    for (let i = 0; i < 40; i++) api.add(alpha, `line ${i} ${'wrapped '.repeat(12)}`);
    api.add(alpha, 'U'.repeat(400));
    for (let i = 0; i < 30; i++) api.agents.push(makeAgent(`3000000${i.toString(16).padStart(1, '0')}-0000-4000-8000-000000000001`.slice(0, 36).replace(/^3000000(.)/, (_, c) => `3000000${c}`), `Extra ${i}`));
    api.operations.push('runtime_get', 'terminal_get');
    await open(page, api);
    await message(page).fill(Array.from({ length: 30 }, (_, i) => `draft ${i}`).join('\n'));
    if (size.width <= 1100) await page.getByRole('button', { name: 'Details', exact: true }).click();
    // Make the inspector overflow independent of the viewport height.
    await page.evaluate(() => { document.querySelector<HTMLElement>('.inspector')!.insertAdjacentHTML('beforeend', '<div style="height:2000px"></div>'); });
    const geometry = await page.evaluate(() => ['.history', '.agent-list', '.inspector', '.composer textarea'].map(selector => {
      const el = document.querySelector<HTMLElement>(selector);
      if (!el) return { selector, missing: true };
      const style = getComputedStyle(el);
      return { selector, thumb: el.offsetWidth - el.clientWidth - parseFloat(style.borderLeftWidth) - parseFloat(style.borderRightWidth), scrolls: el.scrollHeight > el.clientHeight, colorAuto: style.scrollbarColor === 'auto', widthAuto: style.scrollbarWidth === 'auto',
        horizontalOverflow: el.scrollWidth > el.clientWidth, overflowX: style.overflowX, rail: el.offsetHeight - el.clientHeight - (parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth)) };
    }));
    for (const g of geometry) {
      expect(g, JSON.stringify(g)).toMatchObject({ thumb: 1, scrolls: true, colorAuto: true, widthAuto: true, horizontalOverflow: false });
      expect((g as { rail: number }).rail).toBe(0);
    }
    const contrast = await page.evaluate(() => {
      const channel = (v: number) => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; };
      const lum = (c: string) => { const m = c.match(/\d+(\.\d+)?/g)!.map(Number); return .2126 * channel(m[0]) + .7152 * channel(m[1]) + .0722 * channel(m[2]); };
      const ratio = (a: string, b: string) => { const [x, y] = [lum(a), lum(b)].sort((p, q) => q - p); return (x + .05) / (y + .05); };
      const place = getComputedStyle(document.querySelector('.composer textarea')!, '::placeholder').color;
      const time = getComputedStyle(document.querySelector('.message-meta time')!).color;
      const name = getComputedStyle(document.querySelector('.message-meta strong')!).color;
      return { placeholder: ratio(place, 'rgb(32,32,32)'), time: ratio(time, 'rgb(11,11,11)'), name: ratio(name, 'rgb(11,11,11)') };
    });
    expect(contrast.placeholder).toBeGreaterThanOrEqual(4.5);
    expect(contrast.time).toBeGreaterThanOrEqual(4.5);
    expect(contrast.name).toBeGreaterThanOrEqual(4.5);
    if (evidence) {
      if (size.width <= 1100) await page.keyboard.press('Escape');
      await message(page).fill('');
      await page.locator('.entry').nth(3).hover();
      await page.screenshot({ path: `${evidence}/scrollbars-${size.width}.png` });
    }
  });
}
