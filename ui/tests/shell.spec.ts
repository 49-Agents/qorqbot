import { test as base, expect, type Page } from '@playwright/test';
import { SyntheticAPI, alpha, beta, owner } from './synthetic-api';
import { SyntheticTerminal } from './synthetic-terminal';
import { randomUUID } from 'node:crypto';
import { writeFile } from 'node:fs/promises';
import type { Entry, RuntimeView } from '../src/wire';

const test = base.extend<{ api: SyntheticAPI }>({
  api: async ({}, use) => { const api = await new SyntheticAPI().start(); try { await use(api); } finally { await api.close(); } },
  page: async ({ page }, use) => {
    const errors: string[] = [];
    page.on('pageerror', error => errors.push(error.message));
    await use(page);
    expect(errors).toEqual([]);
  },
});
async function openWorkspace(page: Page, api: SyntheticAPI) {
  await page.goto(api.url);
  await expect(page.getByRole('button', { name: 'Chat', exact: true })).toHaveAttribute('aria-pressed', 'true');
}
const message = (page: Page) => page.getByRole('textbox', { name: /^Message / });
const agentButton = (page: Page, name: string) => page.getByRole('navigation', { name: 'Agents' }).getByRole('button', { name: new RegExp(`^${name} Initializing`) });
const calls = (api: SyntheticAPI, name: string) => api.calls.filter(c => c.name === name);
const connectionRefusal = { status: 401, contentType: 'application/json', body: JSON.stringify({ error: 'transport_refused' }) };
const transientRefusal = { status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'unavailable', message: 'Synthetic transient refusal' } }) };

async function inspectorTab(page: Page, name: 'Details' | 'Library' | 'Computer') {
  const toggle = page.getByRole('button', { name: 'Details', exact: true, includeHidden: true });
  await expect(toggle).toBeVisible();
  // A viewport change delivers the media-query event before the following render.
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  if (await toggle.getAttribute('aria-expanded') === 'false') await toggle.click();
  await page.getByRole('tab', { name, exact: true }).click();
}
async function moreDetails(page: Page) {
  await inspectorTab(page, 'Details');
  const more = page.locator('.more-details');
  if (await more.getAttribute('open') === null) await more.locator(':scope > summary').click();
}

test('direct local opening, refused connection retry, workspace identity and recovery preserve drafts', async ({ page, api, context }, testInfo) => {
  const requests: { path: string; method?: string; cookie?: string; authorization?: string; actor?: string | string[] }[] = [];
  api.server.on('request', req => {
    if (req.url?.startsWith('/api/')) requests.push({ path: req.url, method: req.method,
      cookie: req.headers.cookie, authorization: req.headers.authorization, actor: req.headers['x-qorqut-actor'] });
  });
  await context.addCookies([{ name: 'obsolete_console_cookie', value: 'unused', url: api.url }]);
  const noLogin = async () => {
    await expect(page.getByRole('button', { name: /sign in|sign out/i })).toHaveCount(0);
    await expect(page.locator('input[type=password]')).toHaveCount(0);
  };
  let releaseIdentity!: () => void;
  const identityGate = new Promise<void>(resolve => { releaseIdentity = resolve; });
  await page.route('**/api/v1/session', async route => { await identityGate; await route.fulfill(connectionRefusal); }, { times: 1 });
  await page.goto(api.url);
  await expect(page.getByRole('status')).toHaveText('Connecting…');
  await noLogin();
  await page.screenshot({ path: testInfo.outputPath('connecting-desktop.png'), fullPage: true });
  releaseIdentity();
  await expect(page.getByRole('alert')).toHaveText('Request refused (HTTP 401).');
  await noLogin();
  await page.setViewportSize({ width: 937, height: 779 });
  await page.screenshot({ path: testInfo.outputPath('connection-error-937.png'), fullPage: true });
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  const identityResponse = page.waitForResponse(r => r.url().endsWith('/api/v1/session'));
  await page.getByRole('button', { name: 'Retry connection' }).click();
  const identity = await identityResponse;
  expect(await identity.json()).toEqual({ actor_id: owner, api_version: 1 });
  expect(identity.headers()['set-cookie']).toBeUndefined();
  await expect(message(page)).toHaveValue('');
  await noLogin();
  await page.setViewportSize({ width: 1440, height: 960 });
  await expect(page.getByRole('navigation', { name: 'Agents' })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('local-workspace-desktop.png'), fullPage: true });
  expect(requests.slice(0, 3).map(r => r.path)).toEqual(['/api/v1/session', '/api/v1/schema', '/api/v1/operations/config_get']);
  expect(calls(api, 'agents_list').some(c => c.args.after_id && c.args.recovery_epoch === api.epoch)).toBeTruthy();
  expect((await context.cookies()).map(c => c.name)).toEqual(['obsolete_console_cookie']);
  await message(page).fill('An unsent private draft');
  await page.route('**/api/v1/operations/events_list', route => route.fulfill(connectionRefusal), { times: 1 });
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect(page.getByRole('button', { name: 'Retry connection' })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveText('Request refused (HTTP 401).');
  await expect(page.getByText('Unsent drafts remain on this page.')).toBeVisible();
  await noLogin();
  await page.getByRole('button', { name: 'Retry connection' }).click();
  await expect(message(page)).toHaveValue('An unsent private draft');
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('private draft');
  await message(page).fill('Not persistent');
  await page.reload();
  await expect(message(page)).toHaveValue('');
  api.dropSend = true;
  await message(page).fill('Original pending request');
  await message(page).press('Enter');
  await expect(page.getByText(/Confirmation uncertain/)).toBeVisible();
  const originalRequest = calls(api, 'messages_send').at(-1)!.args;
  await message(page).fill('Draft during recovery');
  let schemaRequests = 0;
  await page.route('**/api/v1/schema', route => ++schemaRequests === 1 ? route.fulfill(transientRefusal) : route.continue());
  api.recover();
  await expect(page.getByRole('button', { name: 'Retry connection' })).toBeVisible();
  await expect(page.getByText('Request refused (HTTP 503).', { exact: true })).toBeVisible();
  const stoppedAt = api.calls.length;
  // Cover the old failed-recovery backoff interval: capability-empty polls must not restart.
  await page.waitForTimeout(4300);
  await expect(page.getByRole('button', { name: 'Retry connection' })).toBeVisible();
  expect(schemaRequests).toBe(1);
  expect(api.calls).toHaveLength(stoppedAt);
  api.actor = randomUUID();
  await page.getByRole('button', { name: 'Retry connection' }).click();
  await expect(message(page)).toHaveValue('Draft during recovery');
  expect(schemaRequests).toBe(2);
  await expect(page.getByText('This request belongs to a different workspace identity. Reconnect to the original workspace to retry it.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Retry original request' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
  expect(calls(api, 'messages_send')).toHaveLength(1);
  await page.route('**/api/v1/operations/events_list', route => route.fulfill(connectionRefusal), { times: 1 });
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect(page.getByRole('button', { name: 'Retry connection' })).toBeVisible();
  api.actor = owner;
  await page.getByRole('button', { name: 'Retry connection' }).click();
  await expect(message(page)).toHaveValue('Draft during recovery');
  await page.getByRole('button', { name: 'Retry original request' }).click();
  await expect(page.getByRole('button', { name: 'Retry original request' })).toHaveCount(0);
  await expect(message(page)).toHaveValue('Draft during recovery');
  expect(calls(api, 'messages_send').at(-1)!.args).toEqual(originalRequest);
  await page.unroute('**/api/v1/schema');
  api.version = 2;
  await page.reload();
  await expect(page.getByText('Incompatible API version. This interface requires version 1.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toHaveCount(0);
  api.version = 1;
  await page.route('**/api/v1/session', route => route.fulfill({ json: { actor_id: 'invalid', api_version: 1 } }), { times: 1 });
  await page.getByRole('button', { name: 'Retry connection' }).click();
  await expect(page.getByRole('alert')).toHaveText('The server did not provide a valid workspace identity.');
  await noLogin();
  api.operations = ['config_get', 'agents_list', 'events_list'];
  await page.getByRole('button', { name: 'Retry connection' }).click();
  await expect(page.getByText('Sending messages is unavailable on this server.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Create agent', exact: true })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /^Rename/ })).toHaveCount(0);
  api.operations = ['config_get', 'agents_list', 'conversation_list', 'messages_send'];
  await page.reload();
  await message(page).fill('Retain after failed recovery');
  await page.route('**/api/v1/operations/config_get', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'unavailable', message: 'Synthetic recovery refusal' } }) }));
  api.recover();
  await expect(page.getByRole('button', { name: 'Retry connection' })).toBeVisible();
  await page.unroute('**/api/v1/operations/config_get');
  await page.getByRole('button', { name: 'Retry connection' }).click();
  await expect(message(page)).toHaveValue('Retain after failed recovery');
  expect(requests.filter(r => r.path === '/api/v1/session').every(r => r.method === 'GET')).toBe(true);
  expect(requests.every(r => r.cookie === undefined && r.authorization === undefined && r.actor === undefined)).toBe(true);
  await noLogin();
});

test('creation and fixed icons, direct rename blur/Enter/cancel/pending/conflict and stable targets', async ({ page, api }, testInfo) => {
  await openWorkspace(page, api);
  const create = page.getByRole('button', { name: 'Create agent', exact: true });
  await expect(page.locator('.nav-top, .conversation-header, .row-rename')).toHaveCount(0);
  expect((await create.boundingBox())!.y).toBeGreaterThan(850);
  await create.click();
  const dialog = page.getByRole('dialog', { name: 'Create agent' });
  await expect(dialog.getByRole('radio')).toHaveCount(20);
  await expect(dialog.getByLabel('Provider')).toHaveCount(0);
  await expect(dialog.getByLabel('Model')).toHaveCount(0);
  await expect(dialog.getByLabel('Request start after provisioning')).toHaveCount(0);
  await expect(dialog.getByText('does not start a CLI')).toBeVisible();
  await dialog.getByLabel('Display name').fill('x'.repeat(121));
  await dialog.getByRole('button', { name: 'Create agent', exact: true }).click();
  await expect(dialog.getByText('Display names require 1–120 characters after trimming')).toBeVisible();
  await dialog.getByRole('button', { name: 'Dismiss error' }).click();
  await expect(dialog.getByLabel('Display name')).toHaveValue('x'.repeat(121));
  await dialog.getByLabel('Display name').fill('Atlas');
  await dialog.getByRole('radio', { name: 'feather', exact: true }).check();
  await dialog.getByRole('button', { name: 'Create agent', exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.getByText('Atlas registered — provisioning.')).toBeVisible();
  const createArgs = calls(api, 'agents_create').at(-1)!.args;
  expect(createArgs).toMatchObject({ display_name: 'Atlas', icon_key: 'feather' });
  expect(Object.keys(createArgs).sort()).toEqual(['display_name', 'icon_key', 'idempotency_key']);
  expect(api.agents.filter(a => a.display_name === 'Atlas')).toHaveLength(2);
  const created = api.agents.find(a => a.id !== alpha && a.id !== beta)!;
  const inspector = page.getByRole('complementary', { name: 'Agent details' });
  const name = inspector.locator('.identity-name');
  const rename = inspector.getByRole('textbox', { name: /^Rename / });
  await expect(inspector.getByText(/Provider|Model|Start requested|CLI configuration/)).toHaveCount(0);
  await expect(name).toHaveText('Atlas');
  await expect(inspector.getByRole('button', { name: 'Change icon' })).toHaveText('');
  await name.focus(); await name.press('Enter');
  await rename.fill('Never committed'); await rename.press('Escape');
  for (const value of ['Atlas', '   ']) {
    await name.click(); await rename.fill(value); await message(page).click();
  }
  expect(calls(api, 'agents_update')).toHaveLength(0);
  await name.click(); await rename.fill('  Blur saved  ');
  await rename.dispatchEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true });
  await expect(rename).toBeVisible();
  expect(calls(api, 'agents_update')).toHaveLength(0);
  await message(page).click();
  await expect(name).toHaveText('Blur saved');
  expect(calls(api, 'agents_update')).toHaveLength(1);
  expect(calls(api, 'agents_update')[0].args.display_name).toBe('Blur saved');
  const held = api.hold('agents_update');
  await name.click(); await rename.fill('Enter saved'); await rename.press('Enter');
  await held.seen;
  await message(page).click();
  await expect(inspector.getByRole('status')).toHaveText('Saving request…');
  await expect(name).toBeDisabled();
  await expect(inspector.getByRole('button', { name: 'Change icon' })).toBeDisabled();
  await agentButton(page, 'Blur saved').dblclick();
  await expect(page.getByRole('textbox', { name: /^Rename / })).toHaveCount(0);
  expect(calls(api, 'agents_update')).toHaveLength(2);
  held.release();
  await expect(name).toHaveText('Enter saved');
  await name.click(); await rename.fill('x'.repeat(121)); await rename.press('Enter');
  await expect(inspector.getByText('Display names require 1–120 characters after trimming')).toBeVisible();
  await expect(inspector.getByText(`Attempted name: “${'x'.repeat(121)}”.`)).toBeVisible();
  await inspector.getByRole('button', { name: 'Dismiss error' }).click();
  const beforeConflict = calls(api, 'agents_update').length;
  api.conflictUpdate = true;
  await name.click(); await rename.fill('Attempted name'); await rename.press('Enter');
  await expect(name).toHaveText('Changed elsewhere');
  await expect(inspector.getByText('Attempted name: “Attempted name”.')).toBeVisible();
  await page.getByRole('tab', { name: 'Library', exact: true }).click();
  await expect(inspector.getByText('Attempted name: “Attempted name”.')).toBeVisible();
  await page.getByRole('tab', { name: 'Details', exact: true }).click();
  await inspector.getByRole('button', { name: 'Apply attempted change to current revision' }).click();
  await expect(name).toHaveText('Attempted name');
  const updates = calls(api, 'agents_update').slice(beforeConflict);
  expect(updates).toHaveLength(2);
  expect(updates[1].args.expected_revision).toBe(updates[0].args.expected_revision + 1);
  expect(updates[0].args.idempotency_key).not.toBe(updates[1].args.idempotency_key);
  expect(updates.every(c => c.args.agent_id === created.id)).toBe(true);
  await inspector.getByRole('button', { name: 'Change icon' }).click();
  await page.getByRole('radio', { name: 'anchor', exact: true }).check();
  await page.getByRole('button', { name: 'Cancel', exact: true }).click();
  expect(created.icon_key).toBe('feather');
  await inspector.getByRole('button', { name: 'Change icon' }).click();
  await page.getByRole('radio', { name: 'anchor', exact: true }).check();
  await page.getByRole('button', { name: 'Save icon', exact: true }).click();
  await expect.poll(() => created.icon_key).toBe('anchor');
  expect(calls(api, 'agents_update').at(-1)!.args).toMatchObject({ agent_id: created.id, icon_key: 'anchor' });
  await agentButton(page, 'Attempted name').dblclick();
  await page.getByRole('textbox', { name: 'Rename Attempted name' }).fill('Keyboard rename');
  await page.getByRole('textbox', { name: 'Rename Attempted name' }).press('Enter');
  await expect(name).toHaveText('Keyboard rename');
  await message(page).fill('Keep this active draft');
  const priorUpdates = calls(api, 'agents_update').length;
  api.conflictUpdate = true;
  await agentButton(page, 'onbash').dblclick();
  await page.getByRole('textbox', { name: 'Rename onbash' }).fill('Background agent');
  // Blur during navigation saves the original ID only, without selecting it again.
  await agentButton(page, 'Keyboard rename').click();
  const attemptedText = 'Attempted name: “Background agent”.';
  await expect(page.getByRole('navigation').getByText(attemptedText, { exact: true })).toBeVisible();
  await expect(page.getByText(attemptedText, { exact: true })).toHaveCount(1);
  await expect(agentButton(page, 'Changed elsewhere')).toHaveAttribute('aria-pressed', 'false');
  await expect(name).toHaveText('Keyboard rename');
  await expect(message(page)).toHaveValue('Keep this active draft');
  await page.screenshot({ path: testInfo.outputPath('desktop-row-conflict.png'), fullPage: true });
  await page.setViewportSize({ width: 937, height: 779 });
  const drawer = page.getByRole('navigation', { name: 'Agents', exact: true });
  await expect(drawer.getByText(attemptedText, { exact: true })).toBeVisible();
  const reapply = drawer.getByRole('button', { name: 'Apply attempted change to current revision' });
  await reapply.focus(); await expect(reapply).toBeFocused();
  expect(await drawer.evaluate(e => e.scrollWidth <= e.clientWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('937-row-conflict.png'), fullPage: true });
  await reapply.press('Enter');
  await expect(drawer.getByText(attemptedText, { exact: true })).toHaveCount(0);
  await expect(agentButton(page, 'Background agent')).toHaveAttribute('aria-pressed', 'false');
  const backgroundUpdates = calls(api, 'agents_update').slice(priorUpdates);
  expect(backgroundUpdates).toHaveLength(2);
  expect(backgroundUpdates[0].args).toMatchObject({ agent_id: beta, expected_revision: 1, display_name: 'Background agent' });
  expect(backgroundUpdates[1].args).toMatchObject({ agent_id: beta, expected_revision: 2, display_name: 'Background agent' });
  expect(backgroundUpdates[0].args.idempotency_key).not.toBe(backgroundUpdates[1].args.idempotency_key);
  await drawer.getByRole('button', { name: 'Create agent', exact: true }).click();
  await expect(dialog).toBeVisible();
  await dialog.getByRole('button', { name: 'Close', exact: true }).click();
  await page.getByRole('button', { name: 'Details' }).click();
  await expect(name).toHaveText('Keyboard rename');
  await name.click(); await rename.fill('Desktop drawer blur saved');
  await inspector.getByRole('heading', { name: 'Current work' }).click();
  await expect(name).toHaveText('Desktop drawer blur saved');
  await page.getByRole('button', { name: 'Close Inspector', exact: true }).click();
  await expect(message(page)).toHaveValue('Keep this active draft');
});

test('selection and late reads/mutations retain each draft and original target', async ({ page, api }) => {
  api.add(alpha, 'Atlas history'); api.add(beta, 'onbash history');
  const gate = api.hold('conversation_list', args => args.agent_id === alpha);
  await openWorkspace(page, api); await gate.seen;
  const initialComposerHeight = (await message(page).boundingBox())!.height;
  expect(initialComposerHeight).toBeLessThan(50);
  await message(page).fill('One\nTwo\nThree\nFour');
  expect((await message(page).boundingBox())!.height).toBeGreaterThan(initialComposerHeight);
  await message(page).fill('Atlas draft');
  expect((await message(page).boundingBox())!.height).toBeLessThan(50);
  await agentButton(page, 'onbash').click();
  await expect(page.getByText('onbash history', { exact: true })).toBeVisible();
  gate.release();
  await expect(page.getByText('Atlas history', { exact: true })).toHaveCount(0);
  await message(page).fill('onbash draft');
  await agentButton(page, 'Atlas').click();
  await expect(message(page)).toHaveValue('Atlas draft');
  const send = api.hold('messages_send');
  await page.getByRole('button', { name: 'Send', exact: true }).click(); await send.seen;
  await message(page).fill('Typed while saving');
  await agentButton(page, 'onbash').click();
  await expect(message(page)).toHaveValue('onbash draft');
  send.release();
  await expect.poll(() => api.messages.length).toBe(1);
  await expect(message(page)).toHaveValue('onbash draft');
  await agentButton(page, 'Atlas').click();
  await expect(message(page)).toHaveValue('Typed while saving');
  expect(calls(api, 'messages_send')[0].args.recipient_agent_id).toBe(alpha);
  await expect(page.locator('.message-body').filter({ hasText: 'Atlas draft' })).toBeVisible();
  const late = api.hold('messages_send');
  await message(page).fill('Late connection result');
  await page.getByRole('button', { name: 'Send', exact: true }).click(); await late.seen;
  const lateArgs = calls(api, 'messages_send').at(-1)!.args;
  await page.route('**/api/v1/operations/events_list', route => route.fulfill(connectionRefusal), { times: 1 });
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect(page.getByRole('button', { name: 'Retry connection' })).toBeVisible();
  await page.getByRole('button', { name: 'Retry connection' }).click();
  await expect(message(page)).toHaveValue('Late connection result');
  await expect(page.getByText(/Confirmation uncertain/)).toBeVisible();
  await message(page).fill('New connection draft');
  late.release();
  await expect.poll(() => api.messages.length).toBe(2);
  await expect(message(page)).toHaveValue('New connection draft');
  await expect(page.getByText(/Confirmation uncertain/)).toBeVisible();
  await page.getByRole('button', { name: 'Retry original request' }).click();
  await expect(page.locator('.mutation-error')).toHaveCount(0);
  expect(calls(api, 'messages_send').at(-1)!.args).toEqual(lateArgs);
  expect(api.messages).toHaveLength(2);
  await expect(message(page)).toHaveValue('New connection draft');
});

test('ambiguous send retains same-key retries across connection and application refusals, IME and UTF-8 limit', async ({ page, api }, testInfo) => {
  await openWorkspace(page, api);
  const attempts: object[] = [];
  page.on('request', request => {
    if (request.url().endsWith('/operations/messages_send')) attempts.push(request.postDataJSON());
  });
  await message(page).fill('same request'); api.dropSend = true;
  await message(page).press('Enter');
  await expect(page.getByText(/Confirmation uncertain/)).toBeVisible();
  await expect(message(page)).toHaveValue('same request');
  expect(api.messages).toHaveLength(1);
  // Allow a full polling interval; reads must never replay an uncertain mutation.
  await expect(page.getByText('same request', { exact: true })).toBeVisible();
  expect(calls(api, 'messages_send')).toHaveLength(1);
  await page.route('**/api/v1/operations/messages_send', route => route.fulfill(connectionRefusal), { times: 1 });
  await page.getByRole('button', { name: 'Retry original request' }).click();
  await expect(page.getByRole('alert')).toHaveText('Request refused (HTTP 401).');
  await expect(page.locator('input[type=password]')).toHaveCount(0);
  await page.getByRole('button', { name: 'Retry connection' }).click();
  await expect(message(page)).toHaveValue('same request');
  await expect(page.getByText(/Confirmation uncertain/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Dismiss error' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Apply attempted change to current revision' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
  await message(page).press('Enter');
  expect(attempts).toHaveLength(2);
  await expect(message(page)).toHaveValue('same request');
  await page.screenshot({ path: testInfo.outputPath('uncertainty-after-connection-retry.png'), fullPage: true });
  await page.route('**/api/v1/operations/messages_send', route => route.fulfill({
    status: 400, contentType: 'application/json', body: JSON.stringify({
      ok: false, operation_id: null, outcome: null, data: null, event_seq: null,
      error: { code: 'invalid_argument', message: 'Synthetic retry refusal', retryable: false },
    }),
  }), { times: 1 });
  await page.getByRole('button', { name: 'Retry original request' }).click();
  await expect(page.getByText('Confirmation uncertain. Synthetic retry refusal', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Dismiss error' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Apply attempted change to current revision' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
  await expect(message(page)).toHaveValue('same request');
  expect(attempts).toHaveLength(3);
  expect(api.messages).toHaveLength(1);
  const replay = api.hold('messages_send');
  await page.getByRole('button', { name: 'Retry original request' }).click(); await replay.seen;
  await message(page).fill('Newer draft while replaying');
  replay.release();
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
  await expect(page.locator('.mutation-error')).toHaveCount(0);
  await expect(message(page)).toHaveValue('Newer draft while replaying');
  expect(attempts).toHaveLength(4);
  for (const attempt of attempts) expect(attempt).toEqual(attempts[0]);
  expect(attempts[0]).toEqual({ recipient_agent_id: alpha, body: 'same request', idempotency_key: expect.any(String) });
  expect(calls(api, 'messages_send')).toHaveLength(2);
  expect(calls(api, 'messages_send')[0].args).toEqual(calls(api, 'messages_send')[1].args);
  expect(api.messages).toHaveLength(1);
  expect(api.entries.get(alpha)).toHaveLength(1);
  api.refuseSend = true;
  await message(page).fill('transport refused'); await message(page).press('Enter');
  await expect(page.getByText(/Request refused \(HTTP 503\)/)).toBeVisible();
  await page.getByRole('button', { name: 'Retry original request' }).click();
  await expect(message(page)).toHaveValue('');
  expect(calls(api, 'messages_send')[2].args).toEqual(calls(api, 'messages_send')[3].args);
  await message(page).fill('first'); await message(page).press('Shift+Enter'); await message(page).pressSequentially('second');
  await message(page).dispatchEvent('compositionstart');
  await message(page).dispatchEvent('keydown', { key: 'Enter', code: 'Enter', isComposing: true, bubbles: true });
  await message(page).dispatchEvent('compositionend');
  expect(calls(api, 'messages_send')).toHaveLength(4);
  await message(page).press('Enter');
  await expect(message(page)).toHaveValue('');
  expect(calls(api, 'messages_send').at(-1)!.args.body).toBe('first\nsecond');
  await message(page).fill('é'.repeat(32769));
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
  await expect(page.getByText('65,538 / 65,536 bytes — message is too long')).toBeVisible();
  await message(page).fill('é'.repeat(32768));
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeEnabled();
  await message(page).press('Enter');
  await expect.poll(async () => (await message(page).inputValue()).length).toBe(0);
  expect(Buffer.byteLength(calls(api, 'messages_send').at(-1)!.args.body)).toBe(65536);
});

test('history paging anchors, equal text, quiet handling refresh and recovery', async ({ page, api }) => {
  for (let i = 1; i <= 65; i++) api.add(alpha, `Record ${i}\nA second line of saved content.`);
  const same1 = api.add(alpha, 'Equal text'); const same2 = api.add(alpha, 'Equal text');
  await openWorkspace(page, api);
  await expect(page.locator('.entry')).toHaveCount(50);
  await expect(page.getByText('Equal text', { exact: true })).toHaveCount(2);
  const beforeEventSeq = api.events.length;
  await expect.poll(() => calls(api, 'events_list').some(c => c.args.after_seq === beforeEventSeq)).toBeTruthy();
  let directoryRequests = 0;
  await page.route('**/api/v1/operations/agents_list', route => ++directoryRequests === 1 ? route.fulfill(transientRefusal) : route.continue());
  const backgroundAgent = api.agents.find(a => a.id === beta)!;
  backgroundAgent.display_name = 'Background renamed'; backgroundAgent.revision++;
  api.event('agent.presentation_changed', beta);
  await expect(page.getByText('Request refused (HTTP 503).', { exact: true })).toBeVisible();
  await expect(page.locator('.connection')).toHaveText('Offline');
  await expect(agentButton(page, 'onbash')).toBeVisible();
  await expect(agentButton(page, 'Background renamed')).toBeVisible();
  await expect(page.locator('.connection')).toHaveText('Connected');
  await expect(page.getByText('Request refused (HTTP 503).', { exact: true })).toHaveCount(0);
  expect(api.events).toHaveLength(beforeEventSeq + 1);
  expect(directoryRequests).toBeGreaterThan(1);
  expect(calls(api, 'events_list').filter(c => c.args.after_seq === beforeEventSeq).length).toBeGreaterThanOrEqual(3);
  await page.unroute('**/api/v1/operations/agents_list');
  const history = page.getByLabel('Conversation with Atlas');
  await history.evaluate(e => { e.scrollTop = 0; });
  const anchor = page.locator('.entry').first(); const anchorId = await anchor.getAttribute('data-entry-id');
  const before = (await anchor.boundingBox())!.y;
  await page.getByRole('button', { name: 'Load earlier', exact: true }).click();
  await expect(page.locator('.entry')).toHaveCount(67);
  const after = (await page.locator(`[data-entry-id="${anchorId}"]`).boundingBox())!.y;
  expect(Math.abs(after - before)).toBeLessThan(5);
  expect(calls(api, 'conversation_list').some(c => c.args.before_seq === 18 && c.args.recovery_epoch === api.epoch)).toBeTruthy();
  await history.evaluate(e => { e.scrollTop = 200; });
  api.add(alpha, 'A new record while reading');
  await expect(page.getByRole('button', { name: 'New messages ↓' })).toBeVisible();
  expect(await history.evaluate(e => e.scrollTop)).toBeCloseTo(200, 0);
  await agentButton(page, 'Background renamed').click();
  await agentButton(page, 'Atlas').click();
  expect(await history.evaluate(e => e.scrollTop)).toBeCloseTo(200, 0);
  if (same2.source.type !== 'message') throw new Error('Handling fixture requires a message source');
  same2.handling = { message_id: same2.source.message_id, handled_by: owner, handled_at: '2026-10-08T13:00:00.000000Z', note: null };
  same1.delivery!.state = 'submitted'; same1.delivery!.reason = null;
  await expect(page.locator(`[data-entry-id="${same2.entry_id}"]`).getByText(/Handled ·/)).toBeAttached({ timeout: 12000 });
  await expect(page.locator(`[data-entry-id="${same1.entry_id}"]`).getByText('Submitted', { exact: true })).toBeAttached();
  await message(page).fill('Keep across recovery');
  const oldEpoch = api.epoch; api.recover(); api.add(alpha, 'After recovery');
  await expect(page.getByText('After recovery', { exact: true })).toBeVisible();
  await expect(page.getByText('Equal text', { exact: true })).toHaveCount(0);
  await expect(message(page)).toHaveValue('Keep across recovery');
  expect(api.epoch).not.toBe(oldEpoch);
  await expect.poll(() => calls(api, 'conversation_list').some(c => c.args.recovery_epoch === api.epoch)).toBeTruthy();
});

test('escaped bubbles, compact composer and desktop inspector drawers, focus and screenshots', async ({ page, api, context }, testInfo) => {
  api.agents[0].needs_user_input = true;
  api.add(alpha, '<img src=x onerror="window.injected=true">\n  Indented & untouched', { occurred_at: new Date(Date.now() - 5 * 60000).toISOString() });
  api.add(alpha, 'No card should be inferred from browser_request_input or /workspace/report.pdf\n' + '/long-path'.repeat(150), { occurred_at: new Date(Date.now() - 2 * 3600000).toISOString() });
  api.add(alpha, 'future content', { kind: 'future_card' });
  const nativeText = '  Native café 👩‍💻 e\u0301 <script>window.injected=true</script>\n\tbrowser_request_input & /workspace/report.pdf  \n';
  const nativeUser = api.addNative(alpha, nativeText, 'user', 'claude');
  const nativeAgent = api.addNative(alpha, nativeText, 'agent', 'codex');
  expect(nativeUser.entry_id).not.toBe(nativeAgent.entry_id);
  api.publication = { pending_entries: 3, state: 'blocked', error: { code: 'storage_blocked', message: 'Publication needs inspection.' } };
  await openWorkspace(page, api);
  await expect(page.locator('.message-body').first()).toContainText('<img src=x onerror="window.injected=true">');
  expect(await page.evaluate(() => 'injected' in window)).toBe(false);
  await expect(page.locator('.history img, .history iframe')).toHaveCount(0);
  await expect(page.locator('.agent-attention')).toHaveAttribute('aria-label', 'Needs your attention');
  await expect(page.locator('.agent-activity')).toHaveCount(0);
  await expect(page.getByText('Unsupported content: future_card')).toBeVisible();
  await expect(page.locator('.entry-details, .recorded')).toHaveCount(0);
  await expect(page.locator('.message-meta time').nth(0)).toHaveText('5 minutes ago');
  await expect(page.locator('.message-meta time').nth(1)).toHaveText('2 hours ago');
  const expectNativeEntries = async () => {
    await expect(page.locator('.entry')).toHaveCount(5);
    for (const [entry, sender] of [[nativeUser, 'Terminal input'], [nativeAgent, 'Atlas']] as const) {
      const row = page.locator(`[data-entry-id="${entry.entry_id}"]`);
      await expect(row).toBeVisible();
      await expect(row.locator('header strong')).toHaveText(sender);
      expect(await row.locator('.message-body').textContent()).toBe(nativeText);
      await expect(row.locator('.message-body > *')).toHaveCount(0);
      await expect(row.locator('.delivery')).toBeEmpty();
      await expect(row.locator('header time')).toHaveAttribute('datetime', entry.occurred_at);
      await expect(row.locator('header time')).toHaveText(/\d+ days ago/);
    }
    await expect(page.getByText(/^Stored · (queued|deferred|dispatching)/)).toHaveCount(0);
    await expect(page.locator('.composer-note, .composer-footer')).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Send', exact: true })).toHaveText('');
    expect(await page.evaluate(() => 'injected' in window)).toBe(false);
  };
  await expectNativeEntries();
  const ownerBubble = page.locator(`[data-entry-id="${nativeUser.entry_id}"]`), agentBubble = page.locator(`[data-entry-id="${nativeAgent.entry_id}"]`);
  expect(await ownerBubble.locator('.message-bubble').evaluate(e => getComputedStyle(e).backgroundColor)).toBe('rgb(51, 51, 51)');
  expect(await agentBubble.locator('.message-bubble').evaluate(e => getComputedStyle(e).backgroundColor)).toBe('rgb(32, 32, 32)');
  expect((await ownerBubble.boundingBox())!.x).toBeGreaterThan((await agentBubble.boundingBox())!.x);
  await page.locator('.navigation').hover();
  await expect(agentBubble.locator('.message-meta')).toHaveCSS('opacity', '0');
  await expect(agentBubble.locator('.message-actions')).toHaveCSS('opacity', '0');
  await agentBubble.hover();
  await expect(agentBubble.locator('.message-meta')).toHaveCSS('opacity', '1');
  await expect(agentBubble.locator('.message-actions')).toHaveCSS('opacity', '1');
  const copy = agentBubble.getByRole('button', { name: 'Copy message', exact: true });
  await expect(copy).toHaveText('');
  expect((await copy.boundingBox())!.x).toBeGreaterThan((await agentBubble.boundingBox())!.x + (await agentBubble.boundingBox())!.width);
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await copy.click();
  expect(await page.evaluate(() => navigator.clipboard.readText())).toBe(nativeText);
  await expect(agentBubble.getByRole('status')).toHaveText('Copied');
  await page.screenshot({ path: testInfo.outputPath('message-hover-copy.png') });
  await expect.poll(() => calls(api, 'conversation_list').some(c => c.args.after_seq === nativeAgent.conversation_seq && c.args.recovery_epoch === api.epoch)).toBe(true);
  await page.reload();
  await expectNativeEntries();
  await moreDetails(page);
  await expect(page.getByText('Blocked · 3 pending entries')).toBeVisible();
  await expect(page.getByText('storage_blocked: Publication needs inspection.')).toBeVisible();
  await message(page).fill(Array.from({ length: 20 }, () => 'long-composer-path'.repeat(60)).join('\n'));
  const checkScrollbars = async () => {
    for (const selector of ['.history', '.composer textarea']) {
      expect(await page.locator(selector).evaluate(e => {
        const style = (pseudo: string) => getComputedStyle(e, pseudo);
        return { fits: e.scrollWidth <= e.clientWidth, width: style('::-webkit-scrollbar').width,
          thumb: style('::-webkit-scrollbar-thumb').backgroundColor, track: style('::-webkit-scrollbar-track').backgroundColor,
          arrows: style('::-webkit-scrollbar-button').display, corner: style('::-webkit-scrollbar-corner').backgroundColor };
      })).toEqual({ fits: true, width: '1px', thumb: 'rgb(65, 65, 65)', track: 'rgba(0, 0, 0, 0)', arrows: 'none', corner: 'rgba(0, 0, 0, 0)' });
    }
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await expect(page.locator('.conversation-header, .nav-top')).toHaveCount(0);
    for (const [name, path] of [['Chat', 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z'], ['Terminal', 'm4 17 6-6-6-6M12 19h8']]) {
      await expect(page.getByRole('button', { name, exact: true }).locator('path')).toHaveAttribute('d', path);
    }
  };
  await checkScrollbars();
  await page.setViewportSize({ width: 937, height: 779 });
  await checkScrollbars();
  await page.screenshot({ path: testInfo.outputPath('chat-937.png'), fullPage: true });
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.screenshot({ path: testInfo.outputPath('desktop.png'), fullPage: true });
  await page.getByRole('button', { name: 'Details' }).click();
  await expect(page.getByRole('complementary')).toHaveCount(0);
  await page.getByRole('button', { name: 'Details' }).click();
  await expect(page.getByRole('complementary')).toBeVisible();
  await page.setViewportSize({ width: 937, height: 779 });
  await expect(page.getByRole('navigation')).toBeVisible();
  await checkScrollbars();
  await message(page).fill('A desktop drawer draft');
  await page.getByRole('button', { name: 'Details' }).click();
  const inspectorDrawer = page.getByRole('dialog', { name: 'Inspector', exact: true });
  await expect(inspectorDrawer).toBeVisible();
  for (const key of ['Tab', 'Shift+Tab']) for (let i = 0; i < 8; i++) { await page.keyboard.press(key); expect(await inspectorDrawer.evaluate(e => e.contains(document.activeElement))).toBe(true); }
  await page.screenshot({ path: testInfo.outputPath('937-inspector.png'), fullPage: true });
  await page.getByRole('button', { name: 'Close Inspector', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Details' })).toBeFocused();
  await expect(message(page)).toHaveValue('A desktop drawer draft');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('chat-937-drawer-closed.png'), fullPage: true });
});

test('synthetic inbox references validate annotations, preserve native sender labels and expand by keyboard at 937px', async ({ page, api }, testInfo) => {
  const requests: string[] = [], sockets: string[] = [];
  page.on('request', request => requests.push(request.url()));
  page.on('websocket', socket => sockets.push(socket.url()));
  const stored = api.add(alpha, 'Stored message body stays in one entry.');
  if (stored.source.type !== 'message') throw new Error('Expected synthetic message');
  const annotation = { message_id: stored.source.message_id, message_entry_id: stored.entry_id };
  const pointer = `[Qorqut inbox ${annotation.message_id}]`;
  const reference = api.addNative(alpha, pointer, 'user', 'codex');
  reference.inbox_reference = annotation;
  // The referenced message can be outside this loaded page; UI does no history join.
  const unloaded = { message_id: randomUUID(), message_entry_id: randomUUID() };
  const otherReference = api.addNative(alpha, `[Qorqut inbox ${unloaded.message_id}]`, 'user', 'claude');
  otherReference.inbox_reference = unloaded;
  const ordinary: [Entry, string][] = [[stored, 'Owner']];
  const absent = api.addNative(alpha, pointer, 'user', 'codex');
  ordinary.push([absent, 'Terminal input']);
  for (const value of [null, false, 1, pointer, [], {}, { message_id: annotation.message_id },
    { message_entry_id: annotation.message_entry_id }, { ...annotation, message_id: 12 },
    { ...annotation, message_id: 'invalid' }, { ...annotation, message_entry_id: null },
    { ...annotation, message_entry_id: 'invalid' }, { ...annotation, message_id: annotation.message_id + '\n' }]) {
    const entry = api.addNative(alpha, pointer, 'user', 'claude');
    entry.inbox_reference = value as Entry['inbox_reference'];
    ordinary.push([entry, 'Terminal input']);
  }
  const fromOwner = api.addNative(alpha, 'Attributed native user text', 'user', 'claude');
  fromOwner.sender_actor_id = owner;
  const fromPeer = api.addNative(alpha, 'Native user text from a known actor', 'user', 'codex');
  fromPeer.sender_actor_id = api.agents[1].actor_id;
  const fromAgent = api.addNative(alpha, pointer, 'agent', 'codex');
  fromAgent.inbox_reference = annotation;
  const notNative = api.add(alpha, pointer, { inbox_reference: annotation });
  ordinary.push([fromOwner, 'Owner'], [fromPeer, 'onbash'], [fromAgent, 'Atlas'], [notNative, 'Owner']);
  const wrongKind = api.addNative(alpha, pointer, 'user', 'claude');
  wrongKind.kind = 'future_card'; wrongKind.inbox_reference = annotation;
  const entries = api.entries.get(alpha)!;
  const row = (entry: Entry) => page.locator(`[data-entry-id="${entry.entry_id}"]`);
  const response = page.waitForResponse(r => r.url().endsWith('/operations/conversation_list'));
  const servedUI = page.waitForResponse(r => r.url() === api.url + '/');
  await openWorkspace(page, api);
  // Capture actual served bytes privately, without session requests or credentials.
  await writeFile(testInfo.outputPath('synthetic-conversation-response.json'), await (await response).body());
  await writeFile(testInfo.outputPath('served-ui.html'), await (await servedUI).body());
  await expect(page.locator('.inbox-reference')).toHaveCount(2);
  expect(await page.locator('.entry').evaluateAll(rows => rows.map(e => e.getAttribute('data-entry-id')))).toEqual(entries.map(e => e.entry_id));
  await expect(page.getByText(stored.text, { exact: true })).toHaveCount(1);
  for (const [entry, sender] of ordinary) {
    await expect(row(entry).locator('header strong')).toHaveText(sender);
    expect(await row(entry).locator('.message-body').textContent()).toBe(entry.text);
    await expect(row(entry).locator('.inbox-reference')).toHaveCount(0);
  }
  await expect(row(wrongKind).locator('.unsupported')).toHaveText('Unsupported content: future_card');
  await expect(row(wrongKind).locator('.inbox-reference')).toHaveCount(0);
  for (const entry of [reference, otherReference]) {
    await expect(row(entry).locator('header time')).toHaveAttribute('datetime', entry.occurred_at);
    await expect(row(entry).locator('.message-body')).toBeHidden();
    await expect(row(entry).locator('.delivery')).toBeEmpty();
    await expect(row(entry).locator('a')).toHaveCount(0);
  }
  const disclosure = row(reference).locator('details'), summary = disclosure.locator('summary');
  await summary.focus();
  await page.keyboard.press('Tab');
  await expect(row(reference).getByRole('button', { name: 'Copy message' })).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(row(otherReference).locator('summary')).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(row(reference).getByRole('button', { name: 'Copy message' })).toBeFocused();
  await page.keyboard.press('Shift+Tab');
  await expect(summary).toBeFocused();
  await page.screenshot({ path: testInfo.outputPath('inbox-reference-desktop-collapsed.png') });
  await page.keyboard.press('Enter');
  await expect(disclosure).toHaveAttribute('open', '');
  expect(await disclosure.locator('.message-body').textContent()).toBe(pointer);
  await expect(disclosure.locator('.inbox-reference-id')).toHaveText(`Message ID: ${annotation.message_id}`);
  await expect(disclosure.locator('.recorded')).toHaveCount(0);
  await page.keyboard.press('Space');
  await expect(disclosure).not.toHaveAttribute('open', '');
  await page.keyboard.press('Enter');
  const noOverflow = async () => {
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.locator('.history, .inbox-reference, .inbox-reference summary, .inbox-reference .message-body, .inbox-reference-id')
      .evaluateAll(elements => elements.every(e => e.scrollWidth <= e.clientWidth))).toBe(true);
  };
  await noOverflow();
  await page.screenshot({ path: testInfo.outputPath('inbox-reference-desktop-expanded.png') });
  await page.setViewportSize({ width: 937, height: 779 });
  await summary.scrollIntoViewIfNeeded();
  await expect(summary).toBeFocused();
  await expect(disclosure.locator('.message-body')).toBeVisible();
  await noOverflow();
  await page.screenshot({ path: testInfo.outputPath('inbox-reference-937-expanded.png') });
  expect(requests.filter(value => {
    const url = new URL(value);
    return url.origin !== api.url || !/^(?:\/|\/assets\/[\w.-]+|\/api\/v1\/(?:session|schema|operations\/(?:config_get|agents_list|agents_get|events_list|conversation_list)))$/.test(url.pathname);
  })).toEqual([]);
  expect(sockets).toEqual([]);
  expect(api.messages).toEqual([]);
});

test('file-reference cards validate saved observations, copy exact paths and wrap on wide/937px desktop', async ({ page, api }, testInfo) => {
  const requests: string[] = [], sockets: string[] = [];
  page.on('request', request => requests.push(request.url()));
  page.on('websocket', socket => sockets.push(socket.url()));
  await page.addInitScript(() => {
    const copies: string[] = [];
    Object.defineProperty(window, '__fileCopies', { value: copies });
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async (path: string) => { copies.push(path); } } });
  });
  const ownerPath = 'files/  Résumé e\u0301 & notes .txt  ';
  const agentPath = '/tmp/d47/报告/' + 'long_🙂'.repeat(22) + '/<img src=x onerror="window.injected=true">.txt  ';
  const note = '  Saved observation only.\n\t<script>window.injected=true</script> & café 👩‍💻  \n';
  const ownerEntry = api.addFileReference(beta, {
    path: ownerPath, requested_path: './files//  Résumé e\u0301 & notes .txt  ', label: '  Owner report  ', note,
    recovery_epoch: randomUUID(), observed_at: '2026-10-07T12:00:00.123456Z', mtime_ns: '-1234567890123456789',
  });
  const agentEntry = api.addFileReference(beta, {
    actor_id: api.agents[1].actor_id, location: 'host', path: agentPath, requested_path: agentPath,
    label: 'Agent <report> — ' + '報告🙂'.repeat(22), size_bytes: Number.MAX_SAFE_INTEGER + 1,
  });
  const ordinary = api.add(beta, 'files_reference /workspace/report.pdf and file_reference are ordinary text.');
  const native = api.addNative(beta, 'files_reference("/tmp/report.txt")', 'agent', 'codex');
  const row = (entry: Entry) => page.locator(`[data-entry-id="${entry.entry_id}"]`);
  const ownerCard = row(ownerEntry).getByRole('region', { name: 'File reference' });
  const agentCard = row(agentEntry).getByRole('region', { name: 'File reference' });
  const ownerCopy = ownerCard.getByRole('button', { name: 'Copy path', exact: true });
  const agentCopy = agentCard.getByRole('button', { name: 'Copy path', exact: true });
  const copies = () => page.evaluate(() => (window as unknown as { __fileCopies: string[] }).__fileCopies);
  const noOverflow = async () => {
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.locator('.history, .file-card, .file-path, .file-note').evaluateAll(elements => elements.every(e => e.scrollWidth <= e.clientWidth))).toBe(true);
  };
  await openWorkspace(page, api);
  await agentButton(page, 'onbash').click();
  await expect(page.locator('.file-card')).toHaveCount(2);
  for (const [entry, sender] of [[ownerEntry, 'Owner'], [agentEntry, 'onbash']] as const) {
    await expect(row(entry).locator('header strong')).toHaveText(sender);
    await expect(row(entry).locator('header time')).toHaveAttribute('datetime', entry.occurred_at);
    await expect(row(entry).locator('header time')).toHaveText(/(?:ago|just now)/);
    await expect(row(entry).locator('.delivery')).toBeEmpty();
    await expect(row(entry).locator('.file-card a, .file-card [role="button"], .file-card button:disabled')).toHaveCount(0);
    await expect(row(entry).locator('.file-card button')).toHaveCount(1);
  }
  expect(ownerEntry.source.type === 'file_reference' && ownerEntry.source.reference.recovery_epoch).not.toBe(api.epoch);
  await expect(row(ownerEntry).locator('.recorded')).toHaveCount(0);
  await expect(row(agentEntry).locator('.recorded')).toHaveCount(0);
  await expect(ownerCard.getByText('Agent workspace', { exact: true })).toBeVisible();
  await expect(agentCard.getByText('Host path', { exact: true })).toBeVisible();
  expect(await ownerCard.locator('strong').textContent()).toBe('  Owner report  ');
  expect(await ownerCard.locator('.file-path').textContent()).toBe(ownerPath);
  expect(await agentCard.locator('.file-path').textContent()).toBe(agentPath);
  expect(await ownerCard.locator('.file-note').textContent()).toBe(note);
  const bytes = await page.evaluate(value => value.toLocaleString(), 1234567);
  await expect(ownerCard.locator('.file-size')).toHaveText(`Observed size: ${bytes} bytes`);
  await expect(agentCard.locator('.file-size')).toHaveText('Size exceeds precise display range');
  await expect(agentCard.locator('.file-note')).toHaveCount(0);
  for (const entry of [ordinary, native]) {
    await expect(row(entry).locator('.message-body')).toHaveText(entry.text);
    await expect(row(entry).locator('.file-card')).toHaveCount(0);
  }
  await expect(page.locator('.history img, .history script, .history iframe')).toHaveCount(0);
  expect(await page.evaluate(() => 'injected' in window)).toBe(false);
  await ownerCopy.focus();
  await page.keyboard.press('Enter');
  await expect(ownerCopy).toBeFocused();
  await expect(ownerCard.getByRole('status')).toHaveText('Copied');
  await expect(agentCard.getByRole('status')).toBeEmpty();
  await page.keyboard.press('Tab');
  await expect(agentCopy).toBeFocused();
  expect(await agentCopy.evaluate(e => getComputedStyle(e).outlineStyle)).toBe('solid');
  await page.keyboard.press('Space');
  await expect(agentCard.getByRole('status')).toHaveText('Copied');
  expect((await copies()).map(path => Buffer.from(path, 'utf8'))).toEqual([ownerPath, agentPath].map(path => Buffer.from(path, 'utf8')));
  await page.getByLabel('Conversation with onbash').evaluate(e => { e.scrollTop = 0; });
  await noOverflow();
  await page.screenshot({ path: testInfo.outputPath('file-reference-desktop.png'), fullPage: true });

  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async () => { throw new Error('Synthetic rejection'); } } }));
  await agentCopy.press('Enter');
  await expect(agentCard.getByRole('status')).toHaveText('Could not copy. Select and copy the path manually.');
  await expect(ownerCard.getByRole('status')).toHaveText('Copied');
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: undefined }));
  await ownerCopy.press('Enter');
  await expect(ownerCard.getByRole('status')).toHaveText('Could not copy. Select and copy the path manually.');
  expect(await copies()).toEqual([ownerPath, agentPath]);
  expect(await ownerCard.locator('.file-path').evaluate(e => {
    const range = document.createRange(); range.selectNodeContents(e);
    const selection = window.getSelection()!; selection.removeAllRanges(); selection.addRange(range);
    const value = selection.toString(); selection.removeAllRanges(); return value;
  })).toBe(ownerPath);
  await page.setViewportSize({ width: 937, height: 779 });
  await ownerCopy.focus();
  await expect(ownerCopy).toBeFocused();
  await noOverflow();
  await page.screenshot({ path: testInfo.outputPath('file-reference-937-owner.png'), fullPage: true });
  await agentCopy.focus();
  await expect(agentCopy).toBeFocused();
  await noOverflow();
  await page.screenshot({ path: testInfo.outputPath('file-reference-937-agent.png'), fullPage: true });

  // The same Chrome group checks D45 code-point bounds and malformed wire content.
  const boundaryPath = '🙂/'.repeat(2047) + '🙂🙂';
  const boundary = api.addFileReference(beta, { path: boundaryPath, requested_path: boundaryPath,
    label: '🙂'.repeat(200), note: '🙂'.repeat(4000), size_bytes: Number.MAX_SAFE_INTEGER });
  const malformed: { entry: Entry; reason: string }[] = [];
  const invalidReferences: [string, Record<string, unknown>][] = [
    ['UUID', { reference_id: 'not-a-uuid' }], ['installation UUID', { installation_id: null }],
    ['epoch UUID', { recovery_epoch: 'old' }], ['state', { state: 'missing' }], ['location', { location: 'guest' }],
    ['path type', { path: null }], ['path control', { path: 'file\u0085.txt' }], ['path traversal', { path: '../file' }],
    ['path bound', { path: boundaryPath + 'x' }], ['request bound', { requested_path: boundaryPath + 'x' }],
    ['invalid UTF-8', { path: 'file\ud800.txt' }], ['blank label', { label: '\u2003' }],
    ['label bound', { label: '🙂'.repeat(201) }], ['label control', { label: 'line\nbreak' }],
    ['note bound', { note: '🙂'.repeat(4001) }], ['note control', { note: 'bad\rnote' }],
    ['size negative', { size_bytes: -1 }], ['size fraction', { size_bytes: 0.5 }], ['size type', { size_bytes: '1' }],
    ['mtime number', { mtime_ns: 123 }], ['mtime fraction', { mtime_ns: '1.2' }], ['hash', { sha256: 'a'.repeat(64) }],
    ['required null field', { sha256: undefined }],
    ['UTC required', { observed_at: '2026-10-08T12:00:00+01:00' }], ['real date required', { recorded_at: '2026-02-30T12:00:00Z' }],
    ['extra field', { contents: 'not part of this wire record' }],
  ];
  for (const [reason, patch] of invalidReferences) {
    const entry = api.addFileReference(beta);
    if (entry.source.type !== 'file_reference') throw new Error('Expected file fixture');
    entry.source = { type: 'file_reference', reference: { ...entry.source.reference, ...patch } } as Entry['source'];
    // Keep attribution text/times aligned here so malformed-field checks do not
    // pass merely because the separate entry/reference agreement check rejected them.
    if (typeof patch.label === 'string') entry.text = patch.label;
    if (typeof patch.observed_at === 'string') entry.occurred_at = patch.observed_at;
    if (typeof patch.recorded_at === 'string') entry.recorded_at = patch.recorded_at;
    malformed.push({ entry, reason });
  }
  for (const field of ['agent_id', 'conversation_id', 'sender_actor_id', 'text', 'occurred_at', 'recorded_at'] as const) {
    const entry = api.addFileReference(beta);
    entry[field] = field.endsWith('_id') ? randomUUID() : field.endsWith('_at') ? '2026-10-06T12:00:00.000000Z' : 'Mismatched label';
    malformed.push({ entry, reason: `entry ${field} mismatch` });
  }
  for (const source of [null, { type: 'future_file' }, { type: 'file_reference', reference: null }, { type: 'file_reference', reference: [] }]) {
    const entry = api.addFileReference(beta); entry.source = source as Entry['source'];
    malformed.push({ entry, reason: 'malformed source' });
  }
  const wrongKind = api.addFileReference(beta); wrongKind.kind = 'message';
  malformed.push({ entry: wrongKind, reason: 'kind/source mismatch' });
  const last = malformed.at(-1)!.entry;
  await expect(row(last).locator('.unsupported')).toBeAttached();
  await expect(page.locator('.file-card')).toHaveCount(3);
  expect(await row(boundary).locator('.file-path').textContent()).toBe(boundaryPath);
  expect(await row(boundary).locator('strong').last().textContent()).toBe('🙂'.repeat(200));
  expect(await row(boundary).locator('.file-note').textContent()).toBe('🙂'.repeat(4000));
  const safeBytes = await page.evaluate(value => value.toLocaleString(), Number.MAX_SAFE_INTEGER);
  await expect(row(boundary).locator('.file-size')).toHaveText(`Observed size: ${safeBytes} bytes`);
  for (const { entry, reason } of malformed) {
    await expect(row(entry).locator('.unsupported'), reason).toHaveCount(1);
    await expect(row(entry).locator('.file-card, button'), reason).toHaveCount(0);
  }
  await noOverflow();
  // Repeated normal refresh reads revisit existing entries; entry ID remains the deduplication key.
  await expect.poll(() => calls(api, 'conversation_list').filter(c => c.args.agent_id === beta && c.args.after_seq === last.conversation_seq).length, { timeout: 12000 }).toBeGreaterThanOrEqual(2);
  for (const entry of [ownerEntry, agentEntry, boundary]) await expect(row(entry).locator('.file-card')).toHaveCount(1);
  await page.reload();
  await agentButton(page, 'onbash').click();
  await expect(page.locator('.file-card')).toHaveCount(3);
  for (const entry of [ownerEntry, agentEntry, boundary]) await expect(row(entry).getByRole('status')).toBeEmpty();
  await expect(page.locator('.unsupported')).toHaveCount(malformed.length);
  expect(await page.evaluate(() => 'injected' in window)).toBe(false);
  await page.setViewportSize({ width: 1440, height: 960 });
  const earlierFile = api.addFileReference(alpha, { path: 'files/earlier.txt', label: 'Earlier report' });
  for (let i = 0; i < 55; i++) api.add(alpha, `Archive record ${i}`);
  const shared = api.addFileReference(alpha, { path: 'files/current.txt', label: 'Current report' });
  if (shared.source.type !== 'file_reference') throw new Error('Expected file reference');
  api.addFileReference(alpha, { ...shared.source.reference });
  await agentButton(page, 'Atlas').click();
  await expect(page.getByRole('tab', { name: 'Details', exact: true })).toHaveAttribute('aria-selected', 'true');
  await expect(page.locator('.more-details')).not.toHaveAttribute('open', '');
  await message(page).fill('Library preserves this draft');
  const history = page.getByLabel('Conversation with Atlas');
  await history.evaluate(e => { e.scrollTop = 130; });
  const beforeTabs = await history.evaluate(e => e.scrollTop);
  await inspectorTab(page, 'Library');
  const library = page.getByRole('tabpanel', { name: 'Library', exact: true });
  await expect(library.getByRole('heading', { name: 'Files shared in chat' })).toBeVisible();
  await expect(library.getByRole('region', { name: 'File reference' })).toHaveCount(1);
  await expect(library.locator('.file-path')).toHaveText('files/current.txt');
  await expect(library.getByText('Earlier report')).toHaveCount(0);
  await page.evaluate(() => Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: async (path: string) => { (window as unknown as { __fileCopies: string[] }).__fileCopies.push(path); } } }));
  await library.getByRole('button', { name: 'Copy path' }).click();
  expect((await copies()).at(-1)).toBe('files/current.txt');
  await expect(library.getByRole('status')).toHaveText('Copied');
  await page.route('**/api/v1/operations/conversation_list', route => route.request().postDataJSON().before_seq
    ? route.fulfill({ status: 503, json: { ok: false, error: { code: 'unavailable', message: 'Earlier shared files unavailable.' } } }) : route.continue());
  await library.getByRole('button', { name: 'Load earlier shared files' }).click();
  await expect(library.getByRole('alert')).toContainText('Earlier shared files unavailable.');
  await page.unroute('**/api/v1/operations/conversation_list');
  const earlier = api.hold('conversation_list', args => args.agent_id === alpha && !!args.before_seq);
  await library.getByRole('button', { name: 'Load earlier shared files' }).click(); await earlier.seen;
  await expect(library.getByRole('button', { name: 'Loading earlier shared files…' })).toBeDisabled();
  earlier.release();
  await expect(library.getByText('Earlier report', { exact: true })).toBeVisible();
  await expect(library.getByRole('region', { name: 'File reference' })).toHaveCount(2);
  await expect(library.getByRole('button', { name: 'Load earlier shared files' })).toHaveCount(0);
  expect(earlierFile.source.type === 'file_reference').toBe(true);
  await expect(message(page)).toHaveValue('Library preserves this draft');
  expect(await history.evaluate(e => e.scrollTop)).toBeGreaterThan(beforeTabs);
  const afterEarlier = await history.evaluate(e => e.scrollTop);
  await page.getByRole('tab', { name: 'Library', exact: true }).press('ArrowRight');
  await expect(page.getByRole('tab', { name: 'Computer', exact: true })).toHaveAttribute('aria-selected', 'true');
  const computer = page.getByRole('tabpanel', { name: 'Computer', exact: true });
  for (const surface of ['Browser', 'Machine']) {
    const card = computer.getByRole('region', { name: surface, exact: true });
    await expect(card.getByText(/Unavailable/)).toBeVisible();
    await expect(card.getByRole('button')).toHaveCount(0);
  }
  await expect(computer.getByText('Terminal unavailable on this server.')).toBeVisible();
  await page.getByRole('tab', { name: 'Computer', exact: true }).press('Home');
  await expect(page.getByRole('tab', { name: 'Details', exact: true })).toBeFocused();
  expect(await history.evaluate(e => e.scrollTop)).toBe(afterEarlier);
  await inspectorTab(page, 'Library');
  await page.screenshot({ path: testInfo.outputPath('library-desktop.png'), fullPage: true });
  await page.setViewportSize({ width: 937, height: 779 });
  await inspectorTab(page, 'Library');
  await noOverflow();
  await page.screenshot({ path: testInfo.outputPath('library-937.png'), fullPage: true });
  await inspectorTab(page, 'Computer');
  await page.screenshot({ path: testInfo.outputPath('computer-937.png'), fullPage: true });
  await page.getByRole('button', { name: 'Close Inspector', exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 960 });
  await agentButton(page, 'onbash').click();
  await expect(page.getByRole('tab', { name: 'Details', exact: true })).toHaveAttribute('aria-selected', 'true');
  await inspectorTab(page, 'Library');
  await expect(page.getByRole('tabpanel', { name: 'Library' }).getByText('Current report', { exact: true })).toHaveCount(0);
  // Only static assets, session/discovery and existing read operations were requested.
  expect(requests.filter(value => {
    const url = new URL(value);
    return url.origin !== api.url || !/^(?:\/|\/assets\/[\w.-]+|\/api\/v1\/(?:session|schema|operations\/(?:config_get|agents_list|agents_get|events_list|conversation_list)))$/.test(url.pathname);
  })).toEqual([]);
  expect(sockets).toEqual([]);
  expect(api.messages).toEqual([]);
});

// These two owning flows exercise the compiled UI with synthetic HTTP/WS peers only.
async function terminalReady(page: Page, terminal: SyntheticTerminal) {
  await expect(page.getByRole('textbox', { name: 'Terminal input', exact: true })).toBeEditable();
  await expect.poll(() => terminal.frames.some(frame => frame.type === 'resize')).toBe(true);
  expect(terminal.current.messages.filter(m => m.type === 'control')).toHaveLength(1);
}
test('synthetic runtime inspector validates session/native observations, fences reads and preserves Terminal/Chat', async ({ page, api }, testInfo) => {
  test.setTimeout(120000);
  const terminal = new SyntheticTerminal(api, alpha);
  await terminal.route(page);
  api.add(alpha, 'Saved runtime inspector history');
  const session = page.getByRole('region', { name: 'Session', exact: true });
  const native = page.getByRole('region', { name: 'Native history', exact: true });
  const retry = session.getByRole('button', { name: 'Retry session information' });
  const runtimeURL = '**/api/v1/operations/runtime_get';
  const poll = () => page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  const read = async (action: () => Promise<unknown> = poll) => {
    const response = page.waitForResponse(r => r.url().endsWith('/operations/runtime_get'));
    await action(); await (await response).finished();
    await expect(session.getByRole('status')).toHaveCount(0);
  };
  const stamp = '2026-10-08T12:00:00.123456Z';
  const initial: RuntimeView = { agent_id: alpha, status: 'disconnected', current_owner: null, observed_at: stamp,
    input_ready: false, native_setup: { state: 'waiting', reason: 'waiting_terminal', observed_at: stamp },
    native_capture: { state: 'waiting', reason: 'waiting_session', observed_at: stamp, source_count: 0, pending_archive_bytes: 0, pending_parse_bytes: 0 } };
  const observe = async (view: RuntimeView) => { api.runtimes.set(alpha, view); await read(); };
  const overflow = async () => {
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    expect(await page.getByRole('complementary').evaluate(e => e.scrollWidth <= e.clientWidth)).toBe(true);
  };
  let schemas = 0;
  page.on('request', r => { if (r.url().endsWith('/api/v1/schema')) schemas++; });

  await openWorkspace(page, api);
  await moreDetails(page);
  await expect(session.getByText('Session information is unavailable on this server.')).toBeVisible();
  await expect(retry).toHaveCount(0);
  await expect.poll(() => calls(api, 'events_list').length).toBeGreaterThan(1);
  expect(calls(api, 'runtime_get')).toHaveLength(0);
  api.operations.push('runtime_get');
  await page.reload();
  await moreDetails(page);
  await expect(session.getByText('Last reported: No session is connected.')).toBeVisible();
  await expect(session.getByText('No observation time available.')).toBeVisible();
  await expect(native.getByText('No setup observation available.')).toBeVisible();
  await expect(native.getByText('No collection observation available.')).toBeVisible();
  await expect(session.getByText('Start and manage the CLI manually in Terminal.')).toBeVisible();
  await message(page).fill('Retained runtime draft');
  await observe(initial);
  await expect(native.getByText('Waiting · Waiting for Terminal')).toBeVisible();
  await expect(native.getByText('Waiting · Waiting for a session')).toBeVisible();
  await observe({ ...initial, native_capture: { ...initial.native_capture!, reason: 'waiting_source' } });
  await expect(native.getByText('Waiting · Waiting for a source')).toBeVisible();

  const ownerId = randomUUID();
  const connected: RuntimeView = { ...initial, status: 'connected', current_owner: {
    owner_id: ownerId, run_id: ownerId, run_generation: 3, conversation_id: api.agents[0].conversation_id,
    terminal_id: terminal.view.terminal_id!, recovery_epoch: api.epoch, environment_generation: 1, boot_id: randomUUID(),
    provider: 'claude', cli_version: '2.1.0 <img src=x onerror="window.injected=true">', native_session_id: randomUUID(), connected_at: '2026-10-08T11:00:00+00:00',
  }, native_setup: { state: 'ready', reason: 'configuration_installed', observed_at: stamp },
    native_capture: { state: 'collecting', reason: 'active', observed_at: stamp, source_count: 4, pending_archive_bytes: 1024, pending_parse_bytes: 2048 } };
  await observe(connected);
  await expect(session.getByText('Last reported: Session connected.')).toBeVisible();
  await expect(session.getByText('Claude', { exact: true })).toBeVisible();
  await expect(session.getByText(connected.current_owner!.cli_version, { exact: true })).toBeVisible();
  expect(await page.evaluate(() => 'injected' in window)).toBe(false);
  await expect(session.locator('img')).toHaveCount(0);
  await expect(session.locator('time').first()).toHaveAttribute('datetime', stamp);
  await expect(session.locator('time').last()).toHaveAttribute('datetime', connected.current_owner!.connected_at);
  await expect(native.getByText('Ready · Configuration installed')).toBeVisible();
  await expect(native.getByText('Collecting · Collection active')).toBeVisible();
  for (const count of ['4', '1,024', '2,048']) await expect(native.getByText(count, { exact: true })).toBeVisible();
  await expect(native.locator('time').last()).toHaveAttribute('datetime', stamp);
  await expect(page.getByRole('complementary').getByText('synthetic-model')).toHaveCount(0);
  await expect(page.getByRole('heading', { name: 'Current run', exact: true })).toHaveCount(0);
  const work = page.locator('.inspector section').filter({ has: page.getByRole('heading', { name: 'Current work' }) });
  await expect(work.getByText('Initializing.', { exact: true })).toBeVisible();
  await expect(work.getByText('unknown', { exact: true })).toHaveCount(0);
  await expect(work.getByText('Capture', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Stored · queued · runtime unavailable', { exact: true })).toHaveCount(0);
  await expect(session.getByText(ownerId, { exact: true })).toBeHidden();
  await session.scrollIntoViewIfNeeded(); await overflow();
  await page.screenshot({ path: testInfo.outputPath('runtime-desktop-connected.png'), fullPage: true });
  await native.scrollIntoViewIfNeeded(); await overflow();
  await page.screenshot({ path: testInfo.outputPath('runtime-desktop-native.png'), fullPage: true });

  const unknown: RuntimeView = { ...connected, status: 'unknown',
    native_setup: { state: 'blocked', reason: 'setup_unavailable', observed_at: stamp },
    native_capture: { ...connected.native_capture!, state: 'gap', reason: 'parser_gap', pending_archive_bytes: 0, pending_parse_bytes: 0 } };
  await observe(unknown);
  await expect(session.getByText('Last reported: Session connection unknown.')).toBeVisible();
  await expect(session.getByText(/identity is retained/)).toBeVisible();
  const details = session.locator('summary');
  await details.focus(); await details.press('Enter');
  await expect(session.getByText(ownerId, { exact: true })).toBeVisible();
  await expect(session.getByText(connected.current_owner!.native_session_id, { exact: true })).toBeVisible();
  await expect(session.getByText('3', { exact: true })).toBeVisible();
  await expect(native.getByText('Blocked · Setup unavailable')).toBeVisible();
  await expect(native.getByText('Gap · Parsing gap reported')).toBeVisible();
  await expect(native.getByText('0', { exact: true })).toHaveCount(2);
  await expect(native.getByText(/do not confirm Chat delivery or complete history coverage/)).toBeVisible();
  await details.press('Enter');
  await page.setViewportSize({ width: 937, height: 779 });
  await page.getByRole('button', { name: 'Details' }).click();
  const drawer = page.getByRole('dialog', { name: 'Inspector', exact: true });
  await moreDetails(page);
  await session.scrollIntoViewIfNeeded(); await overflow();
  expect(await drawer.evaluate(e => e.scrollWidth <= e.clientWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('runtime-937-session.png'), fullPage: true });
  await native.scrollIntoViewIfNeeded(); await overflow();
  await page.screenshot({ path: testInfo.outputPath('runtime-937-native-gap.png'), fullPage: true });
  await page.getByRole('button', { name: 'Close Inspector', exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 960 });
  await moreDetails(page);
  await observe({ ...unknown, native_setup: { state: 'unknown', reason: 'recovery_review', observed_at: null },
    native_capture: { ...unknown.native_capture!, state: 'blocked', reason: 'collection_blocked' } });
  await expect(native.getByText('Unknown · Needs recovery review')).toBeVisible();
  await expect(native.getByText('Blocked · Collection blocked')).toBeVisible();
  await observe({ ...unknown, native_setup: { state: 'disabled', reason: 'unsupported_target', observed_at: null },
    native_capture: { ...unknown.native_capture!, state: 'unknown', reason: 'observation_stale' } });
  await expect(native.getByText('Disabled · Unsupported for this agent')).toBeVisible();
  await expect(native.getByText('Unknown · Observation is stale')).toBeVisible();

  // Hold a bounded poll. A selected-agent change must abort it and retain the new target.
  const late = api.hold('runtime_get', args => args.agent_id === alpha);
  await poll(); await late.seen;
  await expect(session.getByText('Refreshing last-reported session observation…')).toBeVisible();
  await expect(session.locator('time').first()).toHaveAttribute('datetime', stamp);
  await expect(native.getByText('Last-reported observations · refreshing…')).toBeVisible();
  const cancelled = page.waitForEvent('requestfailed', r => r.url().endsWith('/operations/runtime_get'));
  await agentButton(page, 'onbash').click(); await cancelled; late.release();
  await moreDetails(page);
  await expect(session.getByText('Last reported: No session is connected.')).toBeVisible();
  await expect(session.getByText('Claude', { exact: true })).toHaveCount(0);
  await expect(native.getByText('No collection observation available.')).toBeVisible();
  await expect.poll(() => calls(api, 'runtime_get').at(-1)?.args).toEqual({ agent_id: beta });
  api.runtimes.set(alpha, connected);
  await agentButton(page, 'Atlas').click();
  await moreDetails(page);
  await expect(message(page)).toHaveValue('Retained runtime draft');
  await expect(page.getByText('Saved runtime inspector history', { exact: true })).toBeVisible();
  await expect(session.getByText('Last reported: Session connected.')).toBeVisible();
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  await terminalReady(page, terminal);
  const live = terminal.current;
  const connections = terminal.connections.length;
  await page.route(runtimeURL, route => route.fulfill(transientRefusal), { times: 1 });
  await read();
  await expect(session.getByRole('alert')).toHaveText('Session information unavailable. Retry to refresh.');
  await expect(session.getByText('Last reported (stale): Session connected.')).toBeVisible();
  await expect(session.getByText('Claude', { exact: true })).toBeVisible();
  await expect(native.getByText('Last-reported observations · stale')).toBeVisible();
  // The Retry button replaces an automatic retry already in flight.
  const automatic = api.hold('runtime_get');
  await poll(); await automatic.seen;
  const replaced = page.waitForEvent('requestfailed', r => r.url().endsWith('/operations/runtime_get'));
  await read(() => retry.click()); await replaced; automatic.release();
  await expect(session.getByText('Last reported: Session connected.')).toBeVisible();
  expect(terminal.connections).toHaveLength(connections); expect(live.closed).toBe(false);
  expect(live.messages.filter(m => m.type === 'control')).toHaveLength(1);
  expect(terminal.inputs).toHaveLength(0);
  terminal.output('Runtime retry left Terminal connected');
  await expect(page.locator('.xterm-screen')).toContainText('Runtime retry left Terminal connected');
  await expect(page.getByRole('button', { name: 'Terminal', exact: true })).toHaveAttribute('aria-pressed', 'true');

  const invalid = [
    { ...connected, agent_id: beta },
    { ...connected, current_owner: { ...connected.current_owner!, conversation_id: api.agents[1].conversation_id } },
    { ...connected, input_ready: true },
    { ...connected, status: 'ready' },
    { ...connected, observed_at: '2026-02-30T12:00:00Z' },
    { ...connected, observed_at: '2026-10-08T12:00:00-04:00' },
    { ...connected, current_owner: { ...connected.current_owner!, native_session_id: 'not-a-uuid' } },
    { ...connected, current_owner: { ...connected.current_owner!, run_generation: 0 } },
    { ...connected, current_owner: { ...connected.current_owner!, environment_generation: 1.5 } },
    { ...connected, current_owner: { ...connected.current_owner!, connected_at: null } },
    { ...connected, native_setup: { ...connected.native_setup!, state: 'collecting' } },
    { ...connected, native_setup: { ...connected.native_setup!, reason: 'active' } },
    { ...connected, native_capture: { ...connected.native_capture!, reason: 'setup_unavailable' } },
    { ...connected, native_capture: { ...connected.native_capture!, source_count: true } },
    { ...connected, native_capture: { ...connected.native_capture!, pending_archive_bytes: -1 } },
    { ...connected, native_capture: { ...connected.native_capture!, pending_parse_bytes: Number.MAX_SAFE_INTEGER + 1 } },
    { ...connected, recovery_epoch: api.epoch },
  ];
  for (const data of invalid) {
    await page.route(runtimeURL, route => route.fulfill({ json: { ok: true, operation_id: null, outcome: 'completed', data, event_seq: null, error: null } }), { times: 1 });
    await read();
    await expect(session.getByRole('alert')).toHaveText('Session information unavailable. Retry to refresh.');
    await expect(session.locator('details')).toHaveCount(0);
    expect(live.closed).toBe(false);
  }

  // Let runtime's owner epoch detect recovery while the event poll is held.
  const events = api.hold('events_list');
  await poll(); await events.seen;
  const beforeRecovery = schemas;
  api.recover(); api.add(alpha, 'Recovered runtime history');
  terminal.view = { ...terminal.view, recovery_epoch: api.epoch };
  api.runtimes.set(alpha, { ...connected, current_owner: { ...connected.current_owner!, recovery_epoch: api.epoch, provider: 'codex', cli_version: 'recovered-version' } });
  await retry.click();
  await expect.poll(() => schemas).toBe(beforeRecovery + 1);
  events.release();
  await moreDetails(page);
  await expect(session.getByText('recovered-version', { exact: true })).toBeVisible();
  await expect(session.getByText('Codex', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Terminal', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(message(page)).toHaveValue('Retained runtime draft');
  await expect(page.getByText('Recovered runtime history', { exact: true })).toBeVisible();
  await expect(page.getByText('Saved runtime inspector history', { exact: true })).toHaveCount(0);
  expect(calls(api, 'messages_send')).toHaveLength(0);

  // A refused runtime read shows a connection error, preserving drafts and native Session UI on retry.
  await page.route(runtimeURL, route => route.fulfill(connectionRefusal), { times: 1 });
  await poll();
  await expect(page.getByRole('alert')).toHaveText('Request refused (HTTP 401).');
  await expect(page.getByRole('button', { name: 'Retry connection' })).toBeVisible();
  await expect(page.locator('input[type=password]')).toHaveCount(0);
  await page.getByRole('button', { name: 'Retry connection' }).click();
  await moreDetails(page);
  await expect(message(page)).toHaveValue('Retained runtime draft');
  await expect(session.getByText('recovered-version', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('recovered-version');
  const staleRead = api.hold('runtime_get');
  const disconnected = page.waitForEvent('requestfailed', r => r.url().endsWith('/operations/runtime_get'));
  await page.route('**/api/v1/operations/conversation_list', async route => { await staleRead.seen; await route.fulfill(connectionRefusal); }, { times: 1 });
  await poll(); await staleRead.seen;
  await expect(page.getByRole('button', { name: 'Retry connection' })).toBeVisible();
  await disconnected;
  await expect(session).toHaveCount(0);
  await page.getByRole('button', { name: 'Retry connection' }).click();
  await moreDetails(page);
  await expect(session.getByText('recovered-version', { exact: true })).toBeVisible();
  staleRead.release();
  await expect(session.getByText('recovered-version', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(message(page)).toHaveValue('Retained runtime draft');
  expect(calls(api, 'runtime_get').every(c => Object.keys(c.args).join() === 'agent_id')).toBe(true);
});

test('synthetic read recovery retains a live Terminal and qualified runtime observations through transient failures', async ({ page, api }, testInfo) => {
  const terminal = new SyntheticTerminal(api, alpha);
  await terminal.route(page);
  api.operations.push('runtime_get');
  const stamp = '2026-10-08T12:00:00Z';
  api.runtimes.set(alpha, { agent_id: alpha, status: 'connected', observed_at: stamp, input_ready: false,
    current_owner: { owner_id: randomUUID(), run_id: randomUUID(), run_generation: 1,
      conversation_id: api.agents[0].conversation_id, terminal_id: terminal.view.terminal_id!, recovery_epoch: api.epoch,
      environment_generation: 1, boot_id: randomUUID(), provider: 'codex', cli_version: 'synthetic-version',
      native_session_id: randomUUID(), connected_at: stamp },
    native_setup: { state: 'ready', reason: 'configuration_installed', observed_at: stamp },
    native_capture: { state: 'collecting', reason: 'active', observed_at: stamp, source_count: 2, pending_archive_bytes: 10, pending_parse_bytes: 20 } });
  await openWorkspace(page, api);
  await moreDetails(page);
  await message(page).fill('Read recovery draft');
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  const pane = page.getByLabel('Terminal view'), session = page.getByRole('region', { name: 'Session', exact: true });
  const native = page.getByRole('region', { name: 'Native history', exact: true });
  const warning = pane.getByRole('alert').filter({ hasText: 'Terminal observation unavailable.' });
  const input = page.getByRole('textbox', { name: 'Terminal input', exact: true });
  await terminalReady(page, terminal);
  const peer = terminal.current, epoch = terminal.view.control!.epoch;
  const oldControl = structuredClone(terminal.view.control);
  terminal.output('Output before read failure\r\n');
  await expect(page.locator('.xterm-screen')).toContainText('Output before read failure');
  for (const kind of ['unavailable', 'network', '503'] as const) {
    const before = terminal.inputs.length;
    terminal.holdNextInput = true;
    await input.press('a');
    await expect.poll(() => terminal.inputs.length).toBe(before + 1);
    const pending = terminal.inputs.at(-1)!;
    await input.press('b');
    for (const operation of ['terminal_get', 'runtime_get']) {
      await page.route(`**/api/v1/operations/${operation}`, route => kind === 'network' ? route.abort('connectionfailed')
        : route.fulfill(kind === '503' ? transientRefusal : { status: 503, json: { ok: false, error: { code: 'unavailable', message: 'Synthetic unavailable observation' } } }), { times: 1 });
    }
    // The next normal poll is held so socket receipts cannot conceal the HTTP failure.
    const terminalRead = api.hold('terminal_get'), runtimeRead = api.hold('runtime_get');
    try {
      await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      await expect(warning).toBeVisible();
      await expect(session.getByRole('alert')).toHaveText('Session information unavailable. Retry to refresh.');
      await expect(session.getByText('Last reported (stale): Session connected.')).toBeVisible();
      await expect(session.getByText('synthetic-version', { exact: true })).toBeVisible();
      await expect(session.locator('time').first()).toHaveAttribute('datetime', stamp);
      await expect(native.getByText('Collecting · Collection active')).toBeVisible();
      await expect(native.getByText('Last-reported observations · stale')).toBeVisible();
      await expect(input).toBeEditable();
      expect(terminal.inputs).toHaveLength(before + 1);
      // Live state and frame receipts still apply; neither clears the read warning.
      terminal.state({ ...terminal.view, control: oldControl });
      terminal.autoReceipt = true; terminal.receipt(pending);
      await expect.poll(() => terminal.inputs.length).toBe(before + 2);
      expect(terminal.inputs.slice(before)).toEqual([
        pending, { type: 'input', data: Buffer.from('b').toString('base64'), epoch, sequence: pending.sequence + 1 },
      ]);
      terminal.output(`Output during ${kind}\r\n`);
      await expect(page.locator('.xterm-screen')).toContainText(`Output during ${kind}`);
      await expect(page.locator('.xterm-screen')).toContainText('Output before read failure');
      await Promise.all([terminalRead.seen, runtimeRead.seen]);
      await expect(warning).toBeVisible();
      await expect(session.getByText('Last reported (stale): Session connected.')).toBeVisible();
      await expect(native.getByText('Last-reported observations · stale · refreshing…')).toBeVisible();
      expect(terminal.current).toBe(peer); expect(peer.closed).toBe(false);
      expect(terminal.connections).toHaveLength(1);
      expect(peer.messages.filter(m => m.type === 'control')).toHaveLength(1);
      if (kind === 'unavailable') await page.screenshot({ path: testInfo.outputPath('read-recovery-live-warning.png'), fullPage: true });
    } finally { terminalRead.release(); runtimeRead.release(); }
    await expect(warning).toHaveCount(0);
    await expect(session.getByRole('alert')).toHaveCount(0);
    await expect(session.getByText('Last reported: Session connected.')).toBeVisible();
    await expect(native.getByText('Last-reported observations', { exact: true })).toBeVisible();
    expect(terminal.inputs).toHaveLength(before + 2);
  }
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(message(page)).toHaveValue('Read recovery draft');
  await expect.poll(() => peer.closed).toBe(true);
  expect(calls(api, 'messages_send')).toHaveLength(0);
});

test('synthetic read recovery requires a successful read to open or reconnect and tears down invalid observations', async ({ page, api }) => {
  const terminal = new SyntheticTerminal(api, alpha);
  await terminal.route(page);
  await openWorkspace(page, api);
  await inspectorTab(page, 'Computer');
  await expect(page.getByRole('complementary').getByText('Terminal available.', { exact: true })).toBeVisible();
  const url = '**/api/v1/operations/terminal_get';
  let failing = true, refusals = 0;
  await page.route(url, route => {
    if (!failing) return route.continue();
    refusals++; return route.fulfill(transientRefusal);
  });
  const poll = () => page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await poll();
  await expect(page.getByRole('complementary').getByRole('alert')).toHaveText('Terminal observation unavailable. Retry to refresh.');
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  const pane = page.getByLabel('Terminal view');
  await expect(pane.getByText('Last reported: Terminal available.', { exact: true })).toBeVisible();
  await expect.poll(() => refusals).toBeGreaterThan(1);
  expect(terminal.connections).toHaveLength(0);
  await expect(page.getByRole('textbox', { name: 'Terminal input', exact: true })).toHaveCount(0);
  failing = false;
  const fresh = api.hold('terminal_get');
  try {
    await pane.getByRole('button', { name: 'Retry terminal status' }).click(); await fresh.seen;
    expect(terminal.connections).toHaveLength(0);
    await expect(pane.getByRole('alert')).toHaveText('Terminal observation unavailable. Retry to refresh.');
  } finally { fresh.release(); }
  await terminalReady(page, terminal);
  expect(terminal.connections).toHaveLength(1); expect(terminal.inputs).toHaveLength(0);
  terminal.holdNextInput = true;
  await page.getByRole('textbox', { name: 'Terminal input', exact: true }).press('q');
  await expect.poll(() => terminal.inputs.length).toBe(1);
  await page.getByRole('textbox', { name: 'Terminal input', exact: true }).press('r');
  const first = terminal.current;
  first.route.close();
  await expect(pane.getByRole('button', { name: 'Reconnect terminal' })).toBeVisible();
  failing = true;
  await pane.getByRole('button', { name: 'Reconnect terminal' }).click();
  await expect(pane.getByRole('alert').filter({ hasText: 'Terminal observation unavailable.' })).toBeVisible();
  expect(terminal.connections).toHaveLength(1);
  await expect(pane.getByRole('alert').filter({ hasText: 'Terminal connection closed.' })).toBeVisible();
  failing = false; terminal.autoReceipt = true;
  await pane.getByRole('button', { name: 'Reconnect terminal' }).click();
  await terminalReady(page, terminal);
  expect(terminal.connections).toHaveLength(2); expect(terminal.inputs).toHaveLength(0);
  expect(first.messages.filter(m => m.type === 'input')).toHaveLength(1);

  const valid = structuredClone(terminal.view);
  for (const data of [
    { ...valid, status: 'unavailable' }, { ...valid, status: 'stopped' },
    { ...valid, terminal_id: randomUUID() }, { ...valid, dimensions: null },
    { ...valid, control: { ...valid.control!, mode: 'unclaimed', viewer_id: randomUUID() } },
  ]) {
    const peer = terminal.current, count = terminal.connections.length;
    await page.route(url, route => route.fulfill({ json: { ok: true, outcome: 'completed', data } }), { times: 1 });
    await poll();
    await expect.poll(() => peer.closed).toBe(true);
    await expect(pane.getByRole('button', { name: 'Reconnect terminal' })).toBeVisible();
    expect(terminal.connections).toHaveLength(count); expect(peer.messages.filter(m => m.type === 'input')).toHaveLength(0);
    await pane.getByRole('button', { name: 'Reconnect terminal' }).click();
    await terminalReady(page, terminal);
    expect(terminal.connections).toHaveLength(count + 1); expect(terminal.inputs).toHaveLength(0);
  }
});

test('synthetic Terminal views retain Chat drafts/history, release owned control and fit wide/937px desktop without horizontal scroll', async ({ page, api }, testInfo) => {
  const terminal = new SyntheticTerminal(api);
  terminal.view = { ...terminal.view, dimensions: { columns: 132, rows: 60 } };
  await terminal.route(page);
  for (let i = 0; i < 30; i++) api.add(alpha, `Saved Chat record ${i}`);
  await openWorkspace(page, api);
  await inspectorTab(page, 'Computer');
  await message(page).fill('Chat draft stays here');
  const history = page.getByLabel('Conversation with Atlas');
  await history.evaluate(e => { e.scrollTop = 170; });
  await expect.poll(() => calls(api, 'terminal_get').some(c => c.args.agent_id === alpha)).toBe(true);
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Terminal', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByLabel('Terminal view').getByText('Terminal is disabled on this installation.')).toBeVisible();
  expect(terminal.connections).toHaveLength(0);
  const baseView = { ...terminal.view, agent_id: alpha, terminal_id: null, control: null, recording: null, dimensions: null };
  for (const [status, label] of [
    ['waiting_workspace', 'Terminal is waiting for the workspace.'], ['unsupported_target', 'Terminal is not available for this agent yet.'],
    ['stopped', 'Terminal is stopped.'], ['unavailable', 'Terminal is unavailable.'],
  ] as const) {
    api.terminals.set(alpha, { ...baseView, status });
    await page.getByLabel('Terminal view').getByRole('button', { name: 'Retry terminal status' }).click();
    await expect(page.getByLabel('Terminal view').getByText(label, { exact: true })).toBeVisible();
  }
  expect(terminal.connections).toHaveLength(0);
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(message(page)).toHaveValue('Chat draft stays here');
  expect(await history.evaluate(e => e.scrollTop)).toBeCloseTo(170, 0);
  await agentButton(page, 'onbash').click();
  await inspectorTab(page, 'Computer');
  await message(page).fill('onbash draft');
  await expect(page.getByRole('complementary').getByText('Terminal available.')).toBeVisible();
  expect(terminal.connections).toHaveLength(0);
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  await terminalReady(page, terminal);
  expect(terminal.current.route.url()).toBe(api.url.replace('http:', 'ws:') + `/api/v1/terminal/${beta}`);
  expect(terminal.current.messages[0]).toEqual({ type: 'control', action: 'take', expected_epoch: 0 });
  const first = terminal.current;
  await expect(page.locator('.terminal-toolbar, .terminal-note')).toHaveCount(0);
  await expect(page.getByText(/View only|You have control|Input receipts confirm submission only/)).toHaveCount(0);
  await expect(page.getByRole('button', { name: /^(Take|Release) control$/ })).toHaveCount(0);
  const display = page.getByRole('region', { name: 'Terminal display', exact: true });
  const fit = async () => {
    await expect.poll(async () => (await page.locator('.xterm-screen').boundingBox())!.width <= (await display.boundingBox())!.width).toBe(true);
    expect(await display.evaluate(e => e.scrollWidth <= e.clientWidth)).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight)).toBe(true);
    expect(await page.locator('.xterm-rows').evaluate(e => getComputedStyle(e).fontSize)).toBe('13px');
    for (const name of ['Chat', 'Terminal', 'Details']) {
      const button = page.getByRole('button', { name, exact: true });
      await expect(button).toBeInViewport(); await expect(button).toHaveAttribute('title', name);
      const bounds = (await button.boundingBox())!; expect(bounds.width).toBeGreaterThanOrEqual(44); expect(bounds.height).toBeGreaterThanOrEqual(44);
    }
  };
  terminal.output('Synthetic recorded terminal\r\n' + '/very-long-native-path'.repeat(35) + '\r\n');
  await expect(page.locator('.xterm-screen')).toContainText('Synthetic recorded terminal');
  await fit();
  await page.screenshot({ path: testInfo.outputPath('terminal-desktop.png'), fullPage: true });
  const tabsPeer = terminal.current;
  await inspectorTab(page, 'Library');
  await expect(page.getByRole('tabpanel', { name: 'Library' }).getByText('No files shared in the loaded chat pages.')).toBeVisible();
  await page.getByRole('tab', { name: 'Library', exact: true }).press('Home');
  await expect(page.getByRole('tab', { name: 'Details', exact: true })).toBeFocused();
  await page.getByRole('tab', { name: 'Details', exact: true }).press('End');
  await expect(page.getByRole('tab', { name: 'Computer', exact: true })).toBeFocused();
  expect(terminal.connections).toHaveLength(1); expect(terminal.current).toBe(tabsPeer);
  expect(tabsPeer.messages.filter(m => m.type === 'control')).toHaveLength(1);
  const beforeDetails = terminal.frames.length;
  await page.getByRole('button', { name: 'Details' }).click();
  await expect.poll(() => terminal.frames.length).toBeGreaterThan(beforeDetails);
  await fit();
  for (const viewport of [{ width: 937, height: 779 }]) {
    const before = terminal.frames.length;
    await page.setViewportSize(viewport);
    await expect.poll(() => terminal.frames.length).toBeGreaterThan(before);
    await fit();
    await page.screenshot({ path: testInfo.outputPath(`terminal-${viewport.width}.png`), fullPage: true });
  }
  terminal.output(Array.from({ length: 180 }, (_, i) => `Scroll line ${i}\r\n`).join(''));
  await expect(page.locator('.xterm-screen')).toContainText('Scroll line 179');
  const scroller = page.locator('.xterm-scrollable-element');
  await display.hover();
  const styles = await scroller.evaluate(e => {
    const rail = e.querySelector<HTMLElement>('.scrollbar.vertical')!, thumb = rail.querySelector<HTMLElement>('.slider')!;
    return { rail: getComputedStyle(rail).width, thumb: getComputedStyle(thumb).width,
      background: getComputedStyle(rail).backgroundColor, color: getComputedStyle(thumb).backgroundColor,
      arrows: [...e.querySelectorAll('.scra')].filter(n => getComputedStyle(n).display !== 'none').length,
      horizontal: [...e.querySelectorAll('.scrollbar.horizontal')].filter(n => getComputedStyle(n).display !== 'none').length };
  });
  expect(styles).toEqual({ rail: '1px', thumb: '1px', background: 'rgba(0, 0, 0, 0)', color: 'rgb(65, 65, 65)', arrows: 0, horizontal: 0 });
  await writeFile(testInfo.outputPath('terminal-scrollbars.json'), JSON.stringify(styles, null, 2));
  const firstLine = () => page.locator('.xterm-rows > div').first().textContent();
  const bottom = await firstLine();
  await page.mouse.wheel(0, -450);
  await expect.poll(firstLine).not.toBe(bottom);
  const beforeKeyboard = await firstLine();
  await page.getByRole('textbox', { name: 'Terminal input', exact: true }).press('Shift+PageUp');
  await expect.poll(firstLine).not.toBe(beforeKeyboard);
  expect(terminal.inputs).toHaveLength(0);
  await page.screenshot({ path: testInfo.outputPath('terminal-937-scrolled.png'), fullPage: true });
  await page.getByRole('button', { name: 'Details' }).click();
  await inspectorTab(page, 'Computer');
  await expect(page.getByRole('dialog', { name: 'Inspector', exact: true }).getByText(/Local recording: healthy/)).toBeVisible();
  await page.getByRole('dialog', { name: 'Inspector', exact: true }).getByText(/Local recording: healthy/).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('terminal-937-inspector.png'), fullPage: true });
  await page.getByRole('button', { name: 'Close Inspector' }).click();
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(message(page)).toHaveValue('onbash draft');
  await expect.poll(() => terminal.current.closed).toBe(true);
  expect(terminal.view.control!.mode).toBe('unclaimed');
  expect(first.messages.filter(m => m.type === 'control')).toHaveLength(1);
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect.poll(() => terminal.connections.length).toBe(2);
  await terminalReady(page, terminal);
  const second = terminal.current;
  expect(second.viewer).not.toBe(first.viewer);
  expect(second.messages[0]).toMatchObject({ type: 'control', action: 'take', expected_epoch: 2 });
  await agentButton(page, 'Atlas').click();
  await expect(message(page)).toHaveValue('Chat draft stays here');
  await expect.poll(() => second.closed).toBe(true);
  await agentButton(page, 'onbash').click();
  await inspectorTab(page, 'Computer');
  await expect(page.getByRole('button', { name: 'Terminal', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => terminal.connections.length).toBe(3);
  await terminalReady(page, terminal);
  // An old selected-agent observation must not bind the next viewer.
  const gate = api.hold('terminal_get', args => args.agent_id === beta);
  await gate.seen;
  await agentButton(page, 'Atlas').click(); gate.release();
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(page.getByLabel('Terminal view').getByText('Terminal is unavailable.', { exact: true })).toBeVisible();
  expect(terminal.connections).toHaveLength(3);
  api.operations = api.operations.filter(name => name !== 'terminal_get');
  await page.reload();
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(page.getByLabel('Terminal view').getByText('Terminal unavailable on this server.')).toBeVisible();
  expect(terminal.connections).toHaveLength(3);
});

declare global {
  interface Window {
    syntheticTimerGate: { close(): void; open(): void; retained(): number };
    syntheticTimerCanaries: string[];
  }
}

test('synthetic Terminal protocol fences control and human input, bounds queues, and reconnects without replay', async ({ page, api, context }, testInfo) => {
  test.setTimeout(90000);
  // Test-only public scheduler gate: real WS events and browser/Node clocks keep
  // running. No production hooks or private xterm state establish the stall.
  await page.addInitScript(() => {
    const schedule = window.setTimeout.bind(window), cancel = window.clearTimeout.bind(window);
    let closed = false, order = 0;
    type Timer = { handle: number; order: number; retained: boolean; run(): void };
    const pending = new Map<number, Timer>();
    window.setTimeout = ((handler: TimerHandler, delay?: number, ...args: unknown[]) => {
      if (Number(delay ?? 0) > 0) return schedule(handler, delay, ...args);
      const timer: Timer = { handle: 0, order: order++, retained: closed, run: () => {
        if (!pending.has(timer.handle)) return;
        if (closed) { timer.retained = true; return; }
        pending.delete(timer.handle);
        if (typeof handler === 'function') handler.apply(window, args);
        else schedule(handler, 0, ...args);
      } };
      timer.handle = schedule(timer.run, delay);
      pending.set(timer.handle, timer);
      return timer.handle;
    }) as typeof window.setTimeout;
    window.clearTimeout = handle => { if (typeof handle === 'number') pending.delete(handle); cancel(handle); };
    window.syntheticTimerGate = {
      close: () => { closed = true; },
      retained: () => [...pending.values()].filter(timer => timer.retained).length,
      open: () => {
        closed = false;
        for (const timer of [...pending.values()].filter(timer => timer.retained).sort((a, b) => a.order - b.order)) {
          cancel(timer.handle); timer.run();
        }
      },
    };
  });
  const pauseRenderer = () => page.evaluate(() => window.syntheticTimerGate.close());
  const resumeRenderer = () => page.evaluate(() => window.syntheticTimerGate.open());
  const freezePressure = async () => {
    expect(await page.evaluate(() => {
      window.syntheticTimerCanaries = [];
      window.syntheticTimerGate.close();
      setTimeout(value => window.syntheticTimerCanaries.push(value), undefined, 'first');
      const canceled = setTimeout(() => window.syntheticTimerCanaries.push('canceled'));
      clearTimeout(canceled);
      setTimeout(value => window.syntheticTimerCanaries.push(value), 0, 'second');
      return { retained: window.syntheticTimerGate.retained(), ran: window.syntheticTimerCanaries };
    })).toEqual({ retained: 2, ran: [] });
  };
  const thawPressure = async () => {
    expect(await page.evaluate(() => {
      window.syntheticTimerGate.open();
      return window.syntheticTimerCanaries;
    })).toEqual(['first', 'second']);
  };
  const terminal = new SyntheticTerminal(api);
  terminal.autoControl = false;
  await terminal.route(page);
  await openWorkspace(page, api); await agentButton(page, 'onbash').click();
  await inspectorTab(page, 'Computer');
  await message(page).fill('A durable Chat draft');
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(page.getByRole('textbox', { name: 'Terminal input', exact: true })).not.toBeEditable();
  await expect.poll(() => terminal.current.messages.length).toBe(1);
  const input = page.getByRole('textbox', { name: 'Terminal input', exact: true });
  const inputText = () => terminal.inputs.map(f => Buffer.from(f.data, 'base64').toString('utf8')).join('');
  const reconnect = async () => {
    const count = terminal.connections.length, previous = terminal.current;
    terminal.autoReceipt = true;
    await page.getByRole('button', { name: 'Reconnect terminal', exact: true }).click();
    await terminalReady(page, terminal);
    await expect.poll(() => previous.closed).toBe(true);
    expect(terminal.connections).toHaveLength(count + 1);
    expect(terminal.inputs).toHaveLength(0);
    terminal.output(`Viewer ready ${count + 1}\r\n`);
    await expect(page.locator('.xterm-screen')).toContainText(`Viewer ready ${count + 1}`);
  };
  // Keep parser callbacks pending while protocol/control messages continue. All
  // observations below use public DOM and real WS delivery, with no xterm hooks.
  await pauseRenderer();
  const initialGrid = await page.locator('.xterm-screen').boundingBox();
  terminal.output('\x1b[?1049h\x1b[2J\x1b[H\x1b[1;100HOLD-CLAMP');
  // More than the entry cap in adjacent size announcements must coalesce.
  for (let i = 0; i < 1025; i++) terminal.size(100 + i % 2, 30);
  terminal.size(120, 40);
  terminal.output('\x1b[3;100HNEW-GRID');
  const orderedView = structuredClone(terminal.view);
  terminal.view = { ...terminal.view, dimensions: { columns: 20, rows: 5 },
    recording: { ...terminal.view.recording!, local_captured_offset: 121 } };
  terminal.state();
  await expect(page.getByRole('complementary').getByText(/121 bytes captured/)).toBeVisible();
  await expect(page.locator('.xterm-rows > div')).toHaveCount(24);
  expect((await page.locator('.xterm-screen').boundingBox())!.width).toBe(initialGrid!.width);
  expect(terminal.current.messages).toEqual([{ type: 'control', action: 'take', expected_epoch: 0 }]);
  await resumeRenderer();
  await expect(page.locator('.xterm-rows > div')).toHaveCount(40);
  const rowText = (row: number) => page.locator('.xterm-rows > div').nth(row).evaluate(e => e.textContent!.replace(/\u00a0/g, ' ').trimEnd());
  await expect.poll(() => rowText(0)).toBe(' '.repeat(79) + 'O');
  expect(await rowText(1)).toBe('LD-CLAMP');
  expect(await rowText(2)).toBe(' '.repeat(99) + 'NEW-GRID');
  // HTTP observations can also lag the live stream; neither source resizes it.
  const readsBeforeLate = calls(api, 'terminal_get').length;
  await expect.poll(() => calls(api, 'terminal_get').length).toBeGreaterThan(readsBeforeLate);
  await expect(page.locator('.xterm-rows > div')).toHaveCount(40);
  expect((await page.locator('.xterm-screen').boundingBox())!.width).toBeCloseTo(initialGrid!.width * 1.5, 0);
  terminal.view = orderedView; terminal.size(80, 24);
  terminal.output('\x1b[?1049l\x1b[2J\x1b[H');
  await expect(page.locator('.xterm-rows > div')).toHaveCount(24);
  await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: api.url });
  const paste = async (text: string) => {
    await page.evaluate(value => navigator.clipboard.writeText(value), text);
    await input.focus(); await page.keyboard.press('Control+Shift+v');
  };
  await input.focus(); await page.keyboard.type('before ack'); await paste('not admitted');
  expect(terminal.current.messages).toEqual([{ type: 'control', action: 'take', expected_epoch: 0 }]);
  await expect.poll(() => terminal.current.messages.length).toBe(1);
  expect(terminal.current.messages[0]).toEqual({ type: 'control', action: 'take', expected_epoch: 0 });
  terminal.view = { ...terminal.view, control: { mode: 'human', epoch: 1, viewer_id: terminal.current.viewer, last_sequence: 7 } };
  terminal.state();
  await input.focus(); await page.keyboard.type('before reply');
  expect(terminal.inputs).toHaveLength(0);
  terminal.send({ type: 'control', control: terminal.view.control });
  await terminalReady(page, terminal);
  await expect.poll(() => terminal.frames.length).toBe(1);
  expect(terminal.frames[0]).toMatchObject({ type: 'resize', epoch: 1, sequence: 8 });
  const fitted = terminal.frames[0];
  if (fitted.type !== 'resize') throw new Error('Expected the acknowledged viewport resize');
  await expect(page.locator('.xterm-rows > div')).toHaveCount(fitted.rows);
  expect(fitted.columns).toBeGreaterThanOrEqual(20); expect(fitted.columns).toBeLessThanOrEqual(500);
  expect(fitted.rows).toBeGreaterThanOrEqual(5); expect(fitted.rows).toBeLessThanOrEqual(250);
  // The owned viewer follows stream geometry without feeding it back as input.
  const beforeGeometry = terminal.frames.length;
  terminal.size(132, 60);
  terminal.output('Owned stream size\r\n');
  await expect(page.locator('.xterm-screen')).toContainText('Owned stream size');
  await expect(page.locator('.xterm-rows > div')).toHaveCount(60);
  expect(terminal.frames).toHaveLength(beforeGeometry);
  await page.setViewportSize({ width: 1441, height: 960 });
  // One pixel of genuine container change still proposes the same cell count.
  await expect.poll(async () => (await page.locator('.terminal-screen').boundingBox())!.width).toBe(829);
  const settleViewport = () => page.evaluate(() => new Promise<void>(done => requestAnimationFrame(() => requestAnimationFrame(() => done()))));
  await settleViewport();
  expect(terminal.frames).toHaveLength(beforeGeometry);
  // Returning to an in-flight size discards the intervening unsent proposal.
  terminal.autoReceipt = false;
  await page.setViewportSize({ width: 1200, height: 960 });
  await expect.poll(() => terminal.frames.length).toBe(beforeGeometry + 1);
  const inFlightResize = terminal.frames.at(-1)!;
  await page.setViewportSize({ width: 1300, height: 960 });
  await expect.poll(async () => (await page.locator('.terminal-screen').boundingBox())!.width).toBe(688);
  await settleViewport();
  await page.setViewportSize({ width: 1200, height: 960 });
  await expect.poll(async () => (await page.locator('.terminal-screen').boundingBox())!.width).toBe(588);
  await settleViewport();
  terminal.receipt(inFlightResize);
  terminal.output('Resize proposal settled\r\n');
  await expect(page.locator('.xterm-screen')).toContainText('Resize proposal settled');
  expect(terminal.frames).toHaveLength(beforeGeometry + 1);
  terminal.autoReceipt = true;
  await page.setViewportSize({ width: 1441, height: 960 });
  await expect.poll(() => terminal.frames.length).toBe(beforeGeometry + 2);
  terminal.autoControl = true;
  const oldControl = { ...terminal.view.control!, last_sequence: 0 };
  terminal.state({ ...terminal.view, control: oldControl });
  await input.focus(); await page.keyboard.type('aA'); await page.keyboard.press('Control+c');
  await page.keyboard.press('ArrowLeft'); await page.keyboard.press('Enter');
  await page.keyboard.insertText('é🙂');
  await expect.poll(inputText).toBe('aA\x03\x1b[D\ré🙂');
  expect(terminal.inputs[0].sequence).toBeGreaterThan(8);
  // Chrome-generated IME events exercise the pinned emulator's asynchronous composition emission.
  const cdp = await context.newCDPSession(page);
  await cdp.send('Input.imeSetComposition', { text: 'に', selectionStart: 1, selectionEnd: 1 });
  await cdp.send('Input.imeSetComposition', { text: '日本', selectionStart: 2, selectionEnd: 2 });
  await cdp.send('Input.insertText', { text: '日本' });
  await expect.poll(inputText).toBe('aA\x03\x1b[D\ré🙂日本');
  await page.keyboard.type('k');
  await expect.poll(inputText).toBe('aA\x03\x1b[D\ré🙂日本k');
  await cdp.send('Input.imeSetComposition', { text: '消', selectionStart: 1, selectionEnd: 1 });
  await cdp.send('Input.imeSetComposition', { text: '', selectionStart: 0, selectionEnd: 0 });
  terminal.output('\x1b[?2004hPaste enabled\r\n');
  await expect(page.locator('.xterm-screen')).toContainText('Paste enabled');
  await paste('first\nsecond\té');
  await expect.poll(inputText).toBe('aA\x03\x1b[D\ré🙂日本k\x1b[200~first\rsecond\té\x1b[201~');
  const beforeOversize = terminal.inputs.length;
  await paste('é'.repeat(32769));
  await expect(page.getByRole('alert').filter({ hasText: /64 KiB/ })).toBeVisible();
  expect(terminal.inputs).toHaveLength(beforeOversize);
  await paste('F'.repeat(65536));
  await expect(page.getByRole('alert').filter({ hasText: /Input exceeds 64 KiB/ })).toBeVisible();
  expect(terminal.inputs).toHaveLength(beforeOversize);
  // Device answers from the output parser are never raw pane input, even while owned.
  const beforeQueries = terminal.inputs.length;
  const split = Buffer.from('Split Unicode: 日本🙂\r\n');
  terminal.output(split.subarray(0, 16)); terminal.output(split.subarray(16, 19)); terminal.output(split.subarray(19));
  terminal.output('\x1b[c\x1b[5n\x1b[6n\x1b[18t\x1b[?2004lQueries finished\r\n');
  await expect(page.locator('.xterm-screen')).toContainText('Split Unicode: 日本🙂');
  await expect(page.locator('.xterm-screen')).toContainText('Queries finished');
  expect(terminal.inputs).toHaveLength(beforeQueries);
  await paste('plain\nlines\té');
  await expect.poll(inputText).toBe('aA\x03\x1b[D\ré🙂日本k\x1b[200~first\rsecond\té\x1b[201~plain\rlines\té');
  await page.screenshot({ path: testInfo.outputPath('terminal-controlled.png'), fullPage: true });
  // Real keyboard events stay queued behind one receipt, then form one exact
  // byte batch. Input on the other side of a resize must wait for its receipt.
  terminal.holdNextInput = true;
  const beforeBurst = terminal.frames.length;
  await input.focus(); await page.keyboard.type('b');
  await expect.poll(() => terminal.frames.length).toBe(beforeBurst + 1);
  const burstHead = terminal.frames.at(-1)!;
  expect(burstHead).toMatchObject({ type: 'input', data: Buffer.from('b').toString('base64') });
  const burst = 'queued human input 0123456789 '.repeat(12);
  await page.keyboard.type(burst);
  await page.keyboard.type('é🙂');
  await page.keyboard.press('Control+c'); await page.keyboard.press('ArrowLeft');
  terminal.output('\x1b[?2004hBatch paste enabled\r\n');
  await expect(page.locator('.xterm-screen')).toContainText('Batch paste enabled');
  await paste('batch\nlines\té');
  const batchBytes = Buffer.from(burst + 'é🙂\x03\x1b[D\x1b[200~batch\rlines\té\x1b[201~');
  await page.setViewportSize({ width: 1200, height: 960 });
  await expect.poll(async () => (await page.locator('.terminal-screen').boundingBox())!.width).toBe(588);
  await settleViewport();
  await input.focus(); await page.keyboard.type('after resize');
  expect(terminal.frames).toHaveLength(beforeBurst + 1);
  terminal.receipt(burstHead);
  await expect.poll(() => terminal.frames.length).toBe(beforeBurst + 2);
  const batch = terminal.frames.at(-1)!;
  expect(batch).toEqual({ type: 'input', epoch: burstHead.epoch, sequence: burstHead.sequence + 1, data: batchBytes.toString('base64') });
  terminal.receipt(batch);
  await expect.poll(() => terminal.frames.length).toBe(beforeBurst + 3);
  const barrier = terminal.frames.at(-1)!;
  expect(barrier).toMatchObject({ type: 'resize', epoch: burstHead.epoch, sequence: burstHead.sequence + 2 });
  terminal.receipt(barrier);
  await expect.poll(() => terminal.frames.length).toBe(beforeBurst + 4);
  const afterBarrier = terminal.frames.at(-1)!;
  expect(afterBarrier).toEqual({ type: 'input', epoch: burstHead.epoch, sequence: burstHead.sequence + 3, data: Buffer.from('after resize').toString('base64') });
  terminal.receipt(afterBarrier);
  terminal.output('\x1b[?2004lBatch receipts settled\r\n');
  await expect(page.locator('.xterm-screen')).toContainText('Batch receipts settled');
  expect(terminal.frames).toHaveLength(beforeBurst + 4);
  terminal.autoReceipt = true;
  await page.setViewportSize({ width: 1441, height: 960 });
  await expect.poll(() => terminal.frames.length).toBe(beforeBurst + 5);
  // Hold a receipt; at most one frame is sent, with 64 KiB queued and coalesced resize.
  const beforeQueueInputs = terminal.inputs.length;
  terminal.holdNextInput = true;
  await input.focus(); await page.keyboard.type('x');
  await expect.poll(() => terminal.inputs.length).toBe(beforeQueueInputs + 1);
  const held = terminal.frames.at(-1)!;
  const framesBeforeQueue = terminal.frames.length;
  const gridBeforeQueue = await page.locator('.xterm-screen').boundingBox();
  await paste('A'.repeat(32768)); await paste('B'.repeat(32768));
  await page.keyboard.type('Z');
  await expect(page.getByRole('alert').filter({ hasText: /Input queue is full/ })).toBeVisible();
  await page.setViewportSize({ width: 1200, height: 900 });
  await page.setViewportSize({ width: 900, height: 800 });
  await expect.poll(async () => (await page.locator('.terminal-screen').boundingBox())!.width).toBeLessThan(700);
  expect((await page.locator('.xterm-screen').boundingBox())!.width).toBe(gridBeforeQueue!.width);
  expect(terminal.frames).toHaveLength(framesBeforeQueue);
  terminal.receipt(held);
  await expect.poll(() => terminal.frames.length).toBe(framesBeforeQueue + 1);
  terminal.receipt(terminal.frames.at(-1)!);
  await expect.poll(() => terminal.frames.length).toBe(framesBeforeQueue + 2);
  expect(terminal.frames.at(-1)).toMatchObject({ type: 'resize' });
  terminal.receipt(terminal.frames.at(-1)!);
  expect(inputText().endsWith('x' + 'A'.repeat(32768) + 'B'.repeat(32768))).toBe(true);
  expect(terminal.inputs.slice(beforeQueueInputs).map(f => Buffer.from(f.data, 'base64').length)).toEqual([1, 65536]);
  for (let i = 1; i < terminal.frames.length; i++) expect(terminal.frames[i].sequence).toBe(terminal.frames[i - 1].sequence + 1);
  terminal.autoReceipt = true;
  await reconnect();
  const narrowFrames = terminal.frames.length;
  await page.setViewportSize({ width: 937, height: 779 });
  await expect.poll(() => terminal.frames.length).toBeGreaterThan(narrowFrames);
  const narrowFit = terminal.frames.at(-1)!;
  if (narrowFit.type !== 'resize') throw new Error('Expected the 937px control to fit');
  await expect(page.locator('.xterm-rows > div')).toHaveCount(narrowFit.rows);
  expect((await page.locator('.xterm-screen').boundingBox())!.width).toBeLessThan(937);
  await page.screenshot({ path: testInfo.outputPath('terminal-937-controlled.png'), fullPage: true });
  // Clamp proposals at the frozen protocol bounds, using real viewport changes.
  await page.setViewportSize({ width: 6000, height: 5000 });
  await expect.poll(() => terminal.frames.at(-1)).toMatchObject({ type: 'resize', columns: 500, rows: 250 });
  await page.setViewportSize({ width: 1440, height: 960 });
  await expect.poll(() => terminal.frames.at(-1)?.type === 'resize' && terminal.view.dimensions!.rows === fitted.rows).toBe(true);
  // xterm rounds the whole canvas before deriving per-cell metrics. After the
  // large-grid round trip, permit that one CSS pixel of rounding in the fit.
  const fittedGeometry = async () => {
    const viewport = (await page.locator('.terminal-screen').boundingBox())!, grid = (await page.locator('.xterm-screen').boundingBox())!;
    const columns = terminal.view.dimensions!.columns, spare = viewport.width - 1 - grid.width, cell = grid.width / columns;
    return { viewport: viewport.width, grid: grid.width, columns, spare, cell, fits: spare >= 0 && spare < cell + 1 };
  };
  await expect.poll(fittedGeometry).toMatchObject({ fits: true });
  await writeFile(testInfo.outputPath('fit-rounding.json'), JSON.stringify(await fittedGeometry(), null, 2));
  await inspectorTab(page, 'Computer');
  // A newer viewer revokes admission immediately; stale state/receipts cannot restore it.
  terminal.holdNextInput = true;
  await input.focus(); await page.keyboard.type('p');
  await expect.poll(() => terminal.inputs.at(-1)?.data).toBe(Buffer.from('p').toString('base64'));
  const pending = terminal.frames.at(-1)!;
  await page.keyboard.type('queued'.repeat(60));
  await cdp.send('Input.imeSetComposition', { text: '旧', selectionStart: 1, selectionEnd: 1 });
  const older = structuredClone(terminal.view);
  await pauseRenderer();
  terminal.output('Delayed before takeover'); terminal.size(132, 60);
  terminal.view = { ...terminal.view, control: { mode: 'human', epoch: terminal.view.control!.epoch + 1, viewer_id: '30000000-0000-4000-8000-000000000001', last_sequence: 0 } };
  terminal.state();
  await expect(input).not.toBeEditable();
  await expect(page.getByRole('button', { name: 'Reconnect terminal' })).toBeVisible();
  const beforeTakeover = terminal.frames.length;
  terminal.state(older); terminal.receipt(pending);
  await input.focus(); await page.keyboard.type('ignored');
  expect(terminal.frames).toHaveLength(beforeTakeover);
  await resumeRenderer();
  await expect(page.locator('.xterm-rows > div')).toHaveCount(60);
  expect(terminal.frames).toHaveLength(beforeTakeover);
  terminal.autoReceipt = true;
  const replacedViewer = terminal.current, replacementControl = structuredClone(terminal.view.control);
  await reconnect();
  expect(replacedViewer.closed).toBe(true);
  expect(terminal.current.messages.filter(m => m.type === 'control').at(-1)).toMatchObject({ expected_epoch: older.control!.epoch + 1 });
  expect(terminal.view.control!.epoch).toBe(replacementControl!.epoch + 1);
  expect(terminal.inputs).toHaveLength(0);
  expect(replacedViewer.messages.filter(m => m.type === 'control')).toHaveLength(1);
  const beforeOldComposition = terminal.inputs.length;
  // Synthetic late composition-end from the old binding must not revive it.
  await input.dispatchEvent('compositionend', { data: '旧', bubbles: true });
  terminal.output('Old composition cleared\r\n');
  await expect(page.locator('.xterm-screen')).toContainText('Old composition cleared');
  expect(terminal.inputs).toHaveLength(beforeOldComposition);
  await pauseRenderer();
  terminal.output('Disposed pending write'); terminal.size(200, 80);
  terminal.view = { ...terminal.view, recording: { ...terminal.view.recording!, local_captured_offset: 122 } };
  terminal.state();
  await expect(page.getByRole('complementary').getByText(/122 bytes captured/)).toBeVisible();
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(message(page)).toHaveValue('A durable Chat draft');
  await expect.poll(() => terminal.current.closed).toBe(true);
  const hold = structuredClone(terminal.view.control);
  expect(hold!.mode).toBe('unclaimed');
  const priorViewer = terminal.current.viewer;
  terminal.view = { ...terminal.view, dimensions: { columns: 90, rows: 30 } };
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  await terminalReady(page, terminal);
  expect(terminal.current.messages[0]).toMatchObject({ type: 'control', action: 'take', expected_epoch: hold!.epoch });
  expect(terminal.current.viewer).not.toBe(priorViewer);
  expect(terminal.inputs).toHaveLength(0);
  await resumeRenderer();
  await expect(page.locator('.xterm-rows > div')).toHaveCount(terminal.view.dimensions!.rows);
  terminal.output('Fresh viewer after disposal');
  await expect(page.locator('.xterm-screen')).toContainText('Fresh viewer after disposal');
  await expect(page.locator('.xterm-screen')).not.toContainText('Disposed pending write');
  expect(terminal.inputs).toHaveLength(0);
  // A stale auto-take refreshes once and waits for explicit reconnect.
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect.poll(() => terminal.current.closed).toBe(true);
  terminal.conflictNextControl = true;
  const readsBeforeConflict = calls(api, 'terminal_get').length;
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(page.getByRole('alert').filter({ hasText: /Terminal control changed/ })).toBeVisible();
  await expect.poll(() => calls(api, 'terminal_get').length).toBeGreaterThan(readsBeforeConflict);
  await settleViewport();
  expect(terminal.current.messages).toHaveLength(1);
  await reconnect();
  terminal.holdNextInput = true;
  await input.focus(); await page.keyboard.type('u');
  await expect.poll(() => terminal.inputs.length).toBe(1);
  const uncertain = terminal.frames.at(-1)!;
  await page.keyboard.type('never replay');
  terminal.receipt(uncertain, 'uncertain');
  await expect(page.getByRole('alert').filter({ hasText: /Earlier input may have arrived/ })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('terminal-uncertain.png'), fullPage: true });
  await reconnect();
  terminal.holdNextInput = true; await input.focus(); await page.keyboard.type('r');
  await expect.poll(() => terminal.inputs.length).toBe(1);
  terminal.send({ type: 'error', code: 'revision_conflict', message: '<img src=x onerror="window.injected=true">', related_type: 'input', sequence: terminal.frames.at(-1)!.sequence });
  await expect(page.getByRole('button', { name: 'Reconnect terminal' })).toBeVisible();
  expect(await page.evaluate(() => 'injected' in window)).toBe(false);
  await reconnect();
  terminal.current.route.close();
  await expect(page.getByRole('button', { name: 'Reconnect terminal' })).toBeVisible();
  await reconnect();
  terminal.state({ ...terminal.view, terminal_id: '40000000-0000-4000-8000-000000000001' });
  await expect(page.getByRole('button', { name: 'Reconnect terminal' })).toBeVisible();
  // Unsupported protocol and missing/malformed geometry never enable admission.
  for (const invalid of [
    { protocol: 1 }, { protocol: true },
    { terminal: { ...terminal.view, dimensions: null } },
    { terminal: { ...terminal.view, dimensions: { columns: true, rows: 24 } } },
    { terminal: { ...terminal.view, dimensions: { columns: 80.5, rows: 24 } } },
  ]) {
    terminal.hello = (route, viewer) => route.send(JSON.stringify({ type: 'hello', protocol: 2, viewer_id: viewer, terminal: terminal.view, ...invalid }));
    const count = terminal.connections.length;
    await page.getByRole('button', { name: 'Reconnect terminal' }).click();
    await expect.poll(() => terminal.connections.length).toBe(count + 1);
    await expect.poll(() => terminal.current.closed).toBe(true);
    await expect(page.getByRole('alert').filter({ hasText: /Terminal unavailable. Invalid terminal protocol/ })).toBeVisible();
    expect(terminal.current.messages).toEqual([]);
    await expect(page.getByRole('button', { name: 'Take control', exact: true })).toHaveCount(0);
  }
  terminal.hello = null; await reconnect();
  // A duplicate hello is invalid and can never trigger a second take.
  terminal.send({ type: 'hello', protocol: 2, viewer_id: terminal.current.viewer, terminal: terminal.view });
  await expect(page.getByRole('button', { name: 'Reconnect terminal' })).toBeVisible();
  expect(terminal.current.messages.filter(m => m.type === 'control')).toHaveLength(1);
  await reconnect();
  terminal.send({ type: 'state', terminal: { ...terminal.view, socket_path: '/not-a-public-field' } });
  await expect(page.getByRole('button', { name: 'Reconnect terminal' })).toBeVisible();
  await reconnect();
  for (const invalid of [
    { columns: 19, rows: 24 }, { columns: 501, rows: 24 }, { columns: 80, rows: 4 }, { columns: 80, rows: 251 },
    { columns: 80, rows: '24' }, { columns: 80, rows: 24, epoch: 1 },
  ]) {
    terminal.send({ type: 'size', ...invalid });
    await expect(page.getByRole('button', { name: 'Reconnect terminal' })).toBeVisible();
    expect(terminal.inputs).toHaveLength(0);
    await reconnect();
  }
  // Fresh controlled peers have received no keyboard input; reconnect's rendered
  // sentinel precedes this directly verified public timer stall.
  await freezePressure();
  const bytePressurePeer = terminal.current;
  for (let i = 0; i < 18; i++) terminal.output(Buffer.alloc(65536, 65));
  await expect.poll(() => bytePressurePeer.closed).toBe(true);
  await expect(page.getByRole('alert').filter({ hasText: /Live output exceeded the display limit/ })).toBeVisible();
  expect(bytePressurePeer.messages.filter(m => m.type === 'input')).toHaveLength(0);
  expect(bytePressurePeer.messages.filter(m => m.type === 'control')).toHaveLength(1);
  const bytePressureConnections = terminal.connections.length;
  await thawPressure();
  expect(bytePressurePeer.closed).toBe(true);
  expect(terminal.connections).toHaveLength(bytePressureConnections);
  expect(bytePressurePeer.messages.filter(m => m.type === 'input')).toHaveLength(0);
  expect(bytePressurePeer.messages.filter(m => m.type === 'control')).toHaveLength(1);
  await reconnect();
  // Mixed geometry/byte entries have a separate 1,024-entry bound, even well
  // below 1 MiB. The pending callback must not apply queued sizes after closure.
  await freezePressure();
  const entryPressurePeer = terminal.current;
  const rowsBeforeOverflow = await page.locator('.xterm-rows > div').count();
  terminal.output('x');
  for (let i = 0; i < 511; i++) { terminal.size(100 + i % 2, 20); terminal.output('x'); }
  terminal.size(100, 20);
  terminal.view = { ...terminal.view, recording: { ...terminal.view.recording!, local_captured_offset: 123 } };
  terminal.state();
  await expect(page.getByRole('complementary').getByText(/123 bytes captured/)).toBeVisible();
  await terminalReady(page, terminal);
  expect(entryPressurePeer.closed).toBe(false);
  terminal.output('one entry too many');
  await expect.poll(() => entryPressurePeer.closed).toBe(true);
  await expect(page.getByRole('alert').filter({ hasText: /Live output exceeded the display limit/ })).toBeVisible();
  const entryPressureConnections = terminal.connections.length;
  await thawPressure();
  await expect(page.locator('.xterm-rows > div')).toHaveCount(rowsBeforeOverflow);
  expect(entryPressurePeer.closed).toBe(true);
  expect(terminal.connections).toHaveLength(entryPressureConnections);
  expect(entryPressurePeer.messages.filter(m => m.type === 'input')).toHaveLength(0);
  expect(entryPressurePeer.messages.filter(m => m.type === 'control')).toHaveLength(1);
  await reconnect();
  await page.route('**/api/v1/operations/terminal_get', route => route.fulfill(connectionRefusal), { times: 1 });
  await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
  await expect(page.getByRole('button', { name: 'Retry connection' })).toBeVisible();
  await expect(page.getByRole('alert')).toHaveText('Request refused (HTTP 401).');
  await expect(page.locator('input[type=password]')).toHaveCount(0);
  await expect.poll(() => terminal.current.closed).toBe(true);
  expect(terminal.inputs).toHaveLength(0);
});
