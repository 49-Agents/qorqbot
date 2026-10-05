import { test as base, expect, type Page } from '@playwright/test';
import { SyntheticAPI, alpha, beta, owner } from './synthetic-api';
import { SyntheticTerminal } from './synthetic-terminal';
import { randomUUID } from 'node:crypto';
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
async function signIn(page: Page, api: SyntheticAPI) {
  await page.goto(api.url);
  await page.getByLabel('Console token').fill('synthetic-console-token');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Atlas', exact: true }).first()).toBeVisible();
}
const message = (page: Page) => page.getByRole('textbox', { name: /^Message / });
const agentButton = (page: Page, name: string) => page.getByRole('navigation', { name: 'Agents' }).getByRole('button', { name: new RegExp(`^${name} Initializing`) });
const calls = (api: SyntheticAPI, name: string) => api.calls.filter(c => c.name === name);
const transientRefusal = { status: 503, contentType: 'application/json', body: JSON.stringify({ error: { code: 'unavailable', message: 'Synthetic transient refusal' } }) };

test('session lifecycle, expired drafts, incompatible version and absent capabilities', async ({ page, api, context }) => {
  await signIn(page, api);
  expect(calls(api, 'agents_list').some(c => c.args.after_id && c.args.recovery_epoch === api.epoch)).toBeTruthy();
  const cookies = await context.cookies();
  expect(cookies.find(c => c.name === 'qorqut_session')).toMatchObject({ httpOnly: true, sameSite: 'Strict' });
  await message(page).fill('An unsent private draft');
  api.authenticated = false;
  await expect(page.getByLabel('Console token')).toBeVisible();
  await expect(page.getByText('Your session expired. Unsent drafts remain on this page.')).toBeVisible();
  await page.getByLabel('Console token').fill('synthetic-console-token');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(message(page)).toHaveValue('An unsent private draft');
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage }))).not.toContain('private draft');
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('synthetic-console-token');
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.getByLabel('Console token')).toHaveValue('');
  await signIn(page, api);
  await expect(message(page)).toHaveValue('');
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
  await page.getByRole('button', { name: 'Retry connection' }).click();
  await expect(message(page)).toHaveValue('Draft during recovery');
  expect(schemaRequests).toBe(2);
  expect(calls(api, 'messages_send')).toHaveLength(1);
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
});

test('creation and fixed icons, duplicate names, rename cancellation and revision conflict', async ({ page, api }, testInfo) => {
  await signIn(page, api);
  await page.getByRole('button', { name: 'Create agent', exact: true }).click();
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
  await expect(page.getByRole('complementary', { name: 'Agent details' }).getByText(/Provider|Model|Start requested|CLI configuration/)).toHaveCount(0);
  expect(Object.keys(createArgs).sort()).toEqual(['display_name', 'icon_key', 'idempotency_key']);
  expect(api.agents.filter(a => a.display_name === 'Atlas')).toHaveLength(2);
  const created = api.agents.find(a => a.id !== alpha && a.id !== beta)!;
  await page.getByRole('complementary', { name: 'Agent details' }).getByRole('button', { name: 'Rename', exact: true }).click();
  let rename = page.getByRole('textbox', { name: 'Rename Atlas' });
  await rename.fill('Never committed'); await rename.press('Escape');
  expect(calls(api, 'agents_update')).toHaveLength(0);
  await page.getByRole('complementary').getByRole('button', { name: 'Rename', exact: true }).click();
  await rename.fill('Blur cancels'); await message(page).click();
  expect(calls(api, 'agents_update')).toHaveLength(0);
  await page.getByRole('complementary').getByRole('button', { name: 'Rename', exact: true }).click();
  api.conflictUpdate = true;
  await rename.fill('Attempted name'); await rename.press('Enter');
  await expect(page.getByRole('heading', { name: 'Changed elsewhere', exact: true }).first()).toBeVisible();
  await expect(page.getByText('Attempted name: “Attempted name”.')).toBeVisible();
  await page.getByRole('button', { name: 'Apply attempted change to current revision' }).click();
  await expect(page.getByRole('heading', { name: 'Attempted name', exact: true }).first()).toBeVisible();
  const updates = calls(api, 'agents_update');
  expect(updates[0].args.expected_revision).toBe(1); expect(updates[1].args.expected_revision).toBe(2);
  expect(updates[0].args.idempotency_key).not.toBe(updates[1].args.idempotency_key);
  expect(created.id).toBe(updates[1].args.agent_id);
  await page.getByRole('button', { name: 'Change icon', exact: true }).click();
  await page.getByRole('radio', { name: 'anchor', exact: true }).check();
  await page.getByRole('button', { name: 'Save icon', exact: true }).click();
  await expect.poll(() => created.icon_key).toBe('anchor');
  expect(calls(api, 'agents_update').at(-1)!.args).toMatchObject({ agent_id: created.id, expected_revision: 3, icon_key: 'anchor' });
  await agentButton(page, 'Attempted name').dblclick();
  rename = page.getByRole('textbox', { name: 'Rename Attempted name' });
  await rename.fill('Keyboard rename'); await rename.press('Enter');
  await expect(page.getByRole('heading', { name: 'Keyboard rename', exact: true }).first()).toBeVisible();

  const priorUpdates = calls(api, 'agents_update').length;
  await message(page).fill('Keep this active draft');
  api.conflictUpdate = true;
  await page.getByRole('button', { name: 'Rename onbash', exact: true }).click();
  await page.getByRole('textbox', { name: 'Rename onbash', exact: true }).fill('Background agent');
  await page.getByRole('textbox', { name: 'Rename onbash', exact: true }).press('Enter');
  const attemptedText = 'Attempted name: “Background agent”.';
  await expect(page.getByRole('navigation').getByText(attemptedText, { exact: true })).toBeVisible();
  await expect(page.getByText(attemptedText, { exact: true })).toHaveCount(1);
  await expect(agentButton(page, 'Changed elsewhere')).toHaveAttribute('aria-pressed', 'false');
  await expect(page.locator('.conversation-title h1')).toHaveText('Keyboard rename');
  await expect(message(page)).toHaveValue('Keep this active draft');
  await page.screenshot({ path: testInfo.outputPath('desktop-row-conflict.png'), fullPage: true });

  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Open agents' }).click();
  const drawer = page.getByRole('dialog', { name: 'Agents', exact: true });
  await expect(drawer.getByText(attemptedText, { exact: true })).toBeVisible();
  const reapply = drawer.getByRole('button', { name: 'Apply attempted change to current revision' });
  await reapply.focus();
  await expect(reapply).toBeFocused();
  expect(await drawer.evaluate(e => e.scrollWidth <= e.clientWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('mobile-row-conflict.png'), fullPage: true });
  await reapply.press('Enter');
  await expect(drawer.getByText(attemptedText, { exact: true })).toHaveCount(0);
  await expect(agentButton(page, 'Background agent')).toHaveAttribute('aria-pressed', 'false');
  const backgroundUpdates = calls(api, 'agents_update').slice(priorUpdates);
  expect(backgroundUpdates).toHaveLength(2);
  expect(backgroundUpdates[0].args).toMatchObject({ agent_id: beta, expected_revision: 1, display_name: 'Background agent' });
  expect(backgroundUpdates[1].args).toMatchObject({ agent_id: beta, expected_revision: 2, display_name: 'Background agent' });
  expect(backgroundUpdates[0].args.idempotency_key).not.toBe(backgroundUpdates[1].args.idempotency_key);
  await page.getByRole('button', { name: 'Close Agents', exact: true }).click();
  await expect(page.locator('.conversation-title h1')).toHaveText('Keyboard rename');
  await expect(message(page)).toHaveValue('Keep this active draft');
});

test('selection and late reads/mutations retain each draft and original target', async ({ page, api }) => {
  api.add(alpha, 'Atlas history'); api.add(beta, 'onbash history');
  const gate = api.hold('conversation_list', args => args.agent_id === alpha);
  await signIn(page, api); await gate.seen;
  await message(page).fill('Atlas draft');
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
  await expect(page.getByText('Stored · queued · runtime unavailable').first()).toBeVisible();
  const late = api.hold('messages_send');
  await message(page).fill('Late session result');
  await page.getByRole('button', { name: 'Send', exact: true }).click(); await late.seen;
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page.getByLabel('Console token')).toBeVisible();
  await signIn(page, api);
  await message(page).fill('New session draft');
  late.release();
  await expect.poll(() => api.messages.length).toBe(2);
  await expect(message(page)).toHaveValue('New session draft');
});

test('ambiguous send retains same-key retries across session and application refusals, IME and UTF-8 limit', async ({ page, api }, testInfo) => {
  await signIn(page, api);
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
  await page.route('**/api/v1/operations/messages_send', async route => {
    api.authenticated = false;
    await route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: 'authentication_required' }) });
  }, { times: 1 });
  await page.getByRole('button', { name: 'Retry original request' }).click();
  await expect(page.getByLabel('Console token')).toBeVisible();
  await page.getByLabel('Console token').fill('synthetic-console-token');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(message(page)).toHaveValue('same request');
  await expect(page.getByText(/Confirmation uncertain/)).toBeVisible();
  await expect(page.getByRole('button', { name: 'Dismiss error' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Apply attempted change to current revision' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: 'Send', exact: true })).toBeDisabled();
  await message(page).press('Enter');
  expect(attempts).toHaveLength(2);
  await expect(message(page)).toHaveValue('same request');
  await page.screenshot({ path: testInfo.outputPath('uncertainty-after-sign-in.png'), fullPage: true });
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
  await signIn(page, api);
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

test('escaped content and desktop/mobile drawers, focus and screenshots', async ({ page, api }, testInfo) => {
  api.add(alpha, '<img src=x onerror="window.injected=true">\n  Indented & untouched', { occurred_at: '2026-10-07T12:00:00.000000Z' });
  api.add(alpha, 'No card should be inferred from browser_request_input or /workspace/report.pdf');
  api.add(alpha, 'future content', { kind: 'future_card' });
  const nativeText = '  Native café 👩‍💻 e\u0301 <script>window.injected=true</script>\n\tbrowser_request_input & /workspace/report.pdf  \n';
  const nativeUser = api.addNative(alpha, nativeText, 'user', 'claude');
  const nativeAgent = api.addNative(alpha, nativeText, 'agent', 'codex');
  expect(nativeUser.entry_id).not.toBe(nativeAgent.entry_id);
  api.publication = { pending_entries: 3, state: 'blocked', error: { code: 'storage_blocked', message: 'Publication needs inspection.' } };
  await signIn(page, api);
  await expect(page.locator('.message-body').first()).toContainText('<img src=x onerror="window.injected=true">');
  expect(await page.evaluate(() => 'injected' in window)).toBe(false);
  await expect(page.locator('.history img, .history iframe')).toHaveCount(0);
  await expect(page.getByText('Unsupported content: future_card')).toBeVisible();
  await expect(page.getByText(/Recorded /).first()).toBeVisible();
  const expectNativeEntries = async () => {
    await expect(page.locator('.entry')).toHaveCount(5);
    for (const [entry, sender] of [[nativeUser, 'Owner'], [nativeAgent, 'Atlas']] as const) {
      const row = page.locator(`[data-entry-id="${entry.entry_id}"]`);
      await expect(row).toBeVisible();
      await expect(row.locator('header strong')).toHaveText(sender);
      expect(await row.locator('.message-body').textContent()).toBe(nativeText);
      await expect(row.locator('.message-body > *')).toHaveCount(0);
      await expect(row.locator('.delivery')).toBeEmpty();
      await expect(row.locator('header time')).toHaveAttribute('datetime', entry.occurred_at);
      await expect(row.locator('.recorded time')).toHaveAttribute('datetime', entry.recorded_at);
    }
    await expect(page.getByText('Stored · queued · runtime unavailable', { exact: true })).toHaveCount(3);
    await expect(page.locator('.composer-note')).toHaveText('Start and manage the CLI in Terminal. Messages are saved for delivery.');
    expect(await page.evaluate(() => 'injected' in window)).toBe(false);
  };
  await expectNativeEntries();
  await expect.poll(() => calls(api, 'conversation_list').some(c => c.args.after_seq === nativeAgent.conversation_seq && c.args.recovery_epoch === api.epoch)).toBe(true);
  await page.reload();
  await expectNativeEntries();
  await expect(page.getByText('Blocked · 3 pending entries')).toBeVisible();
  await expect(page.getByText('storage_blocked: Publication needs inspection.')).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('desktop.png'), fullPage: true });
  await page.getByRole('button', { name: 'Close inspector' }).click();
  await expect(page.getByRole('complementary')).toHaveCount(0);
  await page.getByRole('button', { name: 'Open inspector' }).click();
  await expect(page.getByRole('complementary')).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('navigation')).toHaveCount(0);
  await message(page).fill('A mobile draft');
  await page.getByRole('button', { name: 'Open agents' }).click();
  const navigation = page.getByRole('dialog', { name: 'Agents', exact: true });
  await expect(navigation).toBeVisible();
  for (let i = 0; i < 8; i++) { await page.keyboard.press('Tab'); expect(await navigation.evaluate(e => e.contains(document.activeElement))).toBe(true); }
  await page.keyboard.press('Escape');
  await expect(page.getByRole('button', { name: 'Open agents' })).toBeFocused();
  await page.getByRole('button', { name: 'Open inspector' }).click();
  await expect(page.getByRole('dialog', { name: 'Inspector', exact: true })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('mobile-inspector.png'), fullPage: true });
  await page.getByRole('button', { name: 'Close Inspector', exact: true }).click();
  await expect(page.getByRole('button', { name: 'Open inspector' })).toBeFocused();
  await expect(message(page)).toHaveValue('A mobile draft');
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('mobile.png'), fullPage: true });
});

test('file-reference cards validate saved observations, copy exact paths and wrap on desktop/mobile', async ({ page, api }, testInfo) => {
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
  await signIn(page, api);
  await agentButton(page, 'onbash').click();
  await expect(page.locator('.file-card')).toHaveCount(2);
  for (const [entry, sender] of [[ownerEntry, 'Owner'], [agentEntry, 'onbash']] as const) {
    await expect(row(entry).locator('header strong')).toHaveText(sender);
    await expect(row(entry).locator('header time')).toHaveAttribute('datetime', entry.occurred_at);
    await expect(row(entry).locator('header time')).toContainText('Observed');
    await expect(row(entry).locator('.delivery')).toBeEmpty();
    await expect(row(entry).locator('.file-card a, .file-card [role="button"], .file-card button:disabled')).toHaveCount(0);
    await expect(row(entry).locator('.file-card button')).toHaveCount(1);
  }
  expect(ownerEntry.source.type === 'file_reference' && ownerEntry.source.reference.recovery_epoch).not.toBe(api.epoch);
  await expect(row(ownerEntry).locator('.recorded time')).toHaveAttribute('datetime', ownerEntry.recorded_at);
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
  await page.setViewportSize({ width: 390, height: 844 });
  await ownerCopy.focus();
  await expect(ownerCopy).toBeFocused();
  await noOverflow();
  await page.screenshot({ path: testInfo.outputPath('file-reference-mobile-owner.png'), fullPage: true });
  await agentCopy.focus();
  await expect(agentCopy).toBeFocused();
  await noOverflow();
  await page.screenshot({ path: testInfo.outputPath('file-reference-mobile-agent.png'), fullPage: true });

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
  await page.getByRole('button', { name: 'Open agents' }).click();
  await agentButton(page, 'onbash').click();
  await expect(page.locator('.file-card')).toHaveCount(3);
  for (const entry of [ownerEntry, agentEntry, boundary]) await expect(row(entry).getByRole('status')).toBeEmpty();
  await expect(page.locator('.unsupported')).toHaveCount(malformed.length);
  expect(await page.evaluate(() => 'injected' in window)).toBe(false);
  // Only static assets, session/discovery and existing read operations were requested.
  expect(requests.filter(value => {
    const url = new URL(value);
    return url.origin !== api.url || !/^(?:\/|\/assets\/[\w.-]+|\/api\/v1\/(?:session|schema|operations\/(?:config_get|agents_list|agents_get|events_list|conversation_list)))$/.test(url.pathname);
  })).toEqual([]);
  expect(sockets).toEqual([]);
  expect(api.messages).toEqual([]);
});

// These two owning flows exercise the compiled UI with synthetic HTTP/WS peers only.
// Measure the actual glyph range against the scroll viewport, not merely its row.
async function terminalTextVisible(page: Page, text: string) {
  return page.locator('.terminal-screen').evaluate((viewport, text) => {
    const row = [...viewport.querySelectorAll('.xterm-rows > div')].find(e => e.textContent?.includes(text));
    if (!row) return false;
    const start = row.textContent!.indexOf(text), end = start + text.length;
    const range = document.createRange(), walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
    let offset = 0, begun = false;
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
      const length = node.textContent!.length;
      if (!begun && start < offset + length) { range.setStart(node, start - offset); begun = true; }
      if (begun && end <= offset + length) { range.setEnd(node, end - offset); break; }
      offset += length;
    }
    const glyphs = range.getBoundingClientRect(), bounds = viewport.getBoundingClientRect();
    return glyphs.left >= bounds.left && glyphs.right <= bounds.left + viewport.clientWidth
      && glyphs.top >= bounds.top && glyphs.bottom <= bounds.top + viewport.clientHeight;
  }, text);
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

  await signIn(page, api);
  await expect(session.getByText('Session information is unavailable on this server.')).toBeVisible();
  await expect(retry).toHaveCount(0);
  await expect.poll(() => calls(api, 'events_list').length).toBeGreaterThan(1);
  expect(calls(api, 'runtime_get')).toHaveLength(0);
  api.operations.push('runtime_get');
  await page.reload();
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
  await expect(work.getByText('unknown', { exact: true })).toBeVisible();
  await expect(work.getByText('Capture', { exact: true })).toHaveCount(0);
  await expect(page.getByText('Stored · queued · runtime unavailable', { exact: true })).toBeVisible();
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
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole('button', { name: 'Open inspector' }).click();
  const drawer = page.getByRole('dialog', { name: 'Inspector', exact: true });
  await session.scrollIntoViewIfNeeded(); await overflow();
  expect(await drawer.evaluate(e => e.scrollWidth <= e.clientWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('runtime-mobile-session.png'), fullPage: true });
  await native.scrollIntoViewIfNeeded(); await overflow();
  await page.screenshot({ path: testInfo.outputPath('runtime-mobile-native-gap.png'), fullPage: true });
  await page.getByRole('button', { name: 'Close Inspector', exact: true }).click();
  await page.setViewportSize({ width: 1440, height: 960 });
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
  await expect(session.getByText('Last reported: No session is connected.')).toBeVisible();
  await expect(session.getByText('Claude', { exact: true })).toHaveCount(0);
  await expect(native.getByText('No collection observation available.')).toBeVisible();
  await expect.poll(() => calls(api, 'runtime_get').at(-1)?.args).toEqual({ agent_id: beta });
  api.runtimes.set(alpha, connected);
  await agentButton(page, 'Atlas').click();
  await expect(message(page)).toHaveValue('Retained runtime draft');
  await expect(page.getByText('Saved runtime inspector history', { exact: true })).toBeVisible();
  await expect(session.getByText('Last reported: Session connected.')).toBeVisible();
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(page.getByText('View only', { exact: true })).toBeVisible();
  const live = terminal.current;
  const connections = terminal.connections.length;
  await page.route(runtimeURL, route => route.fulfill(transientRefusal), { times: 1 });
  await read();
  await expect(session.getByRole('alert')).toHaveText('Session information unavailable. Retry to refresh.');
  await expect(session.getByText('Claude', { exact: true })).toHaveCount(0);
  await expect(native.getByText('No native history observation available.')).toBeVisible();
  // The Retry button replaces an automatic retry already in flight.
  const automatic = api.hold('runtime_get');
  await poll(); await automatic.seen;
  const replaced = page.waitForEvent('requestfailed', r => r.url().endsWith('/operations/runtime_get'));
  await read(() => retry.click()); await replaced; automatic.release();
  await expect(session.getByText('Last reported: Session connected.')).toBeVisible();
  expect(terminal.connections).toHaveLength(connections); expect(live.closed).toBe(false);
  expect(live.messages).toEqual([]);
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
  await expect(session.getByText('recovered-version', { exact: true })).toBeVisible();
  await expect(session.getByText('Codex', { exact: true })).toBeVisible();
  await expect(page.getByRole('button', { name: 'Terminal', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(message(page)).toHaveValue('Retained runtime draft');
  await expect(page.getByText('Recovered runtime history', { exact: true })).toBeVisible();
  await expect(page.getByText('Saved runtime inspector history', { exact: true })).toHaveCount(0);
  expect(calls(api, 'messages_send')).toHaveLength(0);

  // A runtime-specific 401 takes the normal expiry path and retains unsent drafts.
  await page.route(runtimeURL, route => route.fulfill({ status: 401, json: { error: 'authentication_required' } }), { times: 1 });
  await poll();
  await expect(page.getByLabel('Console token')).toBeVisible();
  await expect(page.getByText('Your session expired. Unsent drafts remain on this page.')).toBeVisible();
  await page.getByLabel('Console token').fill('synthetic-console-token');
  await page.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(message(page)).toHaveValue('Retained runtime draft');
  await expect(session.getByText('recovered-version', { exact: true })).toBeVisible();
  expect(await page.evaluate(() => JSON.stringify({ ...localStorage, ...sessionStorage }))).not.toContain('recovered-version');
  const logoutRead = api.hold('runtime_get');
  await poll(); await logoutRead.seen;
  const loggedOut = page.waitForEvent('requestfailed', r => r.url().endsWith('/operations/runtime_get'));
  await page.getByRole('button', { name: 'Sign out', exact: true }).click(); await loggedOut; logoutRead.release();
  await expect(page.getByLabel('Console token')).toBeVisible();
  await expect(session).toHaveCount(0);
  expect(calls(api, 'runtime_get').every(c => Object.keys(c.args).join() === 'agent_id')).toBe(true);
});

test('synthetic Terminal views retain Chat drafts/history, report availability and scroll native grids on desktop/mobile', async ({ page, api }, testInfo) => {
  const terminal = new SyntheticTerminal(api);
  terminal.view = { ...terminal.view, dimensions: { columns: 132, rows: 60 } };
  await terminal.route(page);
  for (let i = 0; i < 30; i++) api.add(alpha, `Saved Chat record ${i}`);
  await signIn(page, api);
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
  await message(page).fill('onbash draft');
  await expect(page.getByRole('complementary').getByText('Terminal available.')).toBeVisible();
  expect(terminal.connections).toHaveLength(0);
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(page.getByText('View only', { exact: true })).toBeVisible();
  expect(terminal.current.route.url()).toBe(api.url.replace('http:', 'ws:') + `/api/v1/terminal/${beta}`);
  expect(terminal.current.messages).toEqual([]);
  const initialControl = structuredClone(terminal.view.control);
  terminal.output('Synthetic recorded terminal\r\n$ \x1b[2;115HRIGHT-132-EDGE\x1b[59;1HBOTTOM-60-END\x1b[60;112HLOWER-RIGHT-132-60\x1b[3;1H');
  await expect(page.locator('.xterm-screen')).toContainText('Synthetic recorded terminal');
  await expect(page.locator('.xterm-rows > div')).toHaveCount(60);
  const display = page.getByRole('region', { name: 'Terminal display', exact: true });
  const font = await page.locator('.xterm-rows').evaluate(e => getComputedStyle(e).fontSize);
  expect(font).toBe('13px');
  await page.screenshot({ path: testInfo.outputPath('terminal-desktop.png'), fullPage: true });
  const wide = await page.locator('.xterm-screen').boundingBox();
  const viewportBefore = await display.boundingBox();
  await page.getByRole('button', { name: 'Close inspector' }).click();
  await expect.poll(async () => (await display.boundingBox())!.width).toBeGreaterThan(viewportBefore!.width);
  expect((await page.locator('.xterm-screen').boundingBox())!.width).toBe(wide!.width);
  expect(terminal.current.messages).toEqual([]);
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('button', { name: 'Open agents' })).toBeVisible();
  await expect.poll(async () => (await display.boundingBox())!.width).toBeLessThan(390);
  expect((await page.locator('.xterm-screen').boundingBox())!.width).toBe(wide!.width);
  await expect(page.locator('.xterm-rows > div')).toHaveCount(60);
  expect(await terminalTextVisible(page, 'RIGHT-132-EDGE')).toBe(false);
  expect(await terminalTextVisible(page, 'BOTTOM-60-END')).toBe(false);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('terminal-mobile.png'), fullPage: true });
  // Real browser keyboard scrolling of the focusable native overflow region.
  await display.focus();
  for (let i = 0; i < 35; i++) await page.keyboard.press('ArrowRight');
  await expect.poll(() => terminalTextVisible(page, 'RIGHT-132-EDGE')).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('terminal-mobile-right.png'), fullPage: true });
  await page.keyboard.press('End');
  await expect.poll(() => terminalTextVisible(page, 'LOWER-RIGHT-132-60')).toBe(true);
  await page.screenshot({ path: testInfo.outputPath('terminal-mobile-bottom-right.png'), fullPage: true });
  for (let i = 0; i < 35; i++) await page.keyboard.press('ArrowLeft');
  await expect.poll(() => terminalTextVisible(page, 'BOTTOM-60-END')).toBe(true);
  await expect(page.getByRole('button', { name: 'Take control', exact: true })).toBeInViewport();
  await expect(page.locator('.terminal-note')).toBeInViewport();
  expect(await page.locator('.xterm-rows').evaluate(e => getComputedStyle(e).fontSize)).toBe(font);
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth && document.documentElement.scrollHeight <= innerHeight)).toBe(true);
  expect(terminal.current.messages).toEqual([]);
  expect(terminal.view.control).toEqual(initialControl);
  await (await import('node:fs/promises')).writeFile(testInfo.outputPath('mobile-native-grid.json'), JSON.stringify(await display.evaluate(e => ({
    viewport: { width: e.clientWidth, height: e.clientHeight }, grid: { width: e.scrollWidth, height: e.scrollHeight },
    scroll: { left: e.scrollLeft, top: e.scrollTop }, rows: e.querySelectorAll('.xterm-rows > div').length,
    font: getComputedStyle(e.querySelector('.xterm-rows')!).fontSize,
  })), null, 2));
  await page.screenshot({ path: testInfo.outputPath('terminal-mobile-bottom.png'), fullPage: true });
  await page.getByRole('button', { name: 'Open inspector' }).click();
  await expect(page.getByRole('dialog', { name: 'Inspector', exact: true }).getByText(/Local recording: healthy/)).toBeVisible();
  await page.getByRole('dialog', { name: 'Inspector', exact: true }).getByText(/Local recording: healthy/).scrollIntoViewIfNeeded();
  await page.screenshot({ path: testInfo.outputPath('terminal-mobile-inspector.png'), fullPage: true });
  await page.getByRole('button', { name: 'Close Inspector' }).click();
  await page.getByRole('button', { name: 'Chat', exact: true }).click();
  await expect(message(page)).toHaveValue('onbash draft');
  await expect.poll(() => terminal.current.closed).toBe(true);
  expect(terminal.current.messages).toEqual([]);
  await page.setViewportSize({ width: 1440, height: 960 });
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect.poll(() => terminal.connections.length).toBe(2);
  const second = terminal.current;
  await agentButton(page, 'Atlas').click();
  await expect(message(page)).toHaveValue('Chat draft stays here');
  await expect.poll(() => second.closed).toBe(true);
  await agentButton(page, 'onbash').click();
  await expect(page.getByRole('button', { name: 'Terminal', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect.poll(() => terminal.connections.length).toBe(3);
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
  await terminal.route(page);
  await signIn(page, api); await agentButton(page, 'onbash').click();
  await message(page).fill('A durable Chat draft');
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(page.getByText('View only', { exact: true })).toBeVisible();
  const input = page.getByRole('textbox', { name: 'Terminal input', exact: true });
  const inputText = () => terminal.inputs.map(f => Buffer.from(f.data, 'base64').toString('utf8')).join('');
  const take = async () => {
    await page.getByRole('button', { name: 'Take control', exact: true }).click();
    await expect(page.getByRole('button', { name: 'Release control', exact: true })).toBeVisible();
    await expect.poll(() => terminal.frames.some(f => f.type === 'resize')).toBe(true);
  };
  const reconnect = async () => {
    const count = terminal.connections.length;
    await page.getByRole('button', { name: 'Reconnect terminal', exact: true }).click();
    await expect(page.getByText('View only', { exact: true })).toBeVisible();
    expect(terminal.connections).toHaveLength(count + 1);
    expect(terminal.current.messages).toEqual([]);
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
  expect(terminal.current.messages).toEqual([]);
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
  await input.focus(); await page.keyboard.type('read only'); await paste('not admitted');
  expect(terminal.current.messages).toEqual([]);
  terminal.autoControl = false;
  await page.getByRole('button', { name: 'Take control', exact: true }).click();
  await expect.poll(() => terminal.current.messages.length).toBe(1);
  expect(terminal.current.messages[0]).toEqual({ type: 'control', action: 'take', expected_epoch: 0 });
  terminal.view = { ...terminal.view, control: { mode: 'human', epoch: 1, viewer_id: terminal.current.viewer, last_sequence: 7 } };
  terminal.state();
  await input.focus(); await page.keyboard.type('before reply');
  expect(terminal.inputs).toHaveLength(0);
  terminal.send({ type: 'control', control: terminal.view.control });
  await expect(page.getByRole('button', { name: 'Release control', exact: true })).toBeVisible();
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
  await page.getByRole('button', { name: 'Release control', exact: true }).click();
  await expect(page.getByText('View only', { exact: true })).toBeVisible();
  expect(terminal.view.control!.mode).toBe('unclaimed');
  const releasedGrid = await page.locator('.xterm-screen').boundingBox();
  const releasedFrames = terminal.frames.length;
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(page.getByRole('button', { name: 'Open agents' })).toBeVisible();
  expect((await page.locator('.xterm-screen').boundingBox())!.width).toBe(releasedGrid!.width);
  expect(terminal.frames).toHaveLength(releasedFrames);
  await take();
  await expect.poll(() => terminal.frames.length).toBe(releasedFrames + 1);
  const mobileFit = terminal.frames.at(-1)!;
  if (mobileFit.type !== 'resize') throw new Error('Expected mobile control to fit');
  await expect(page.locator('.xterm-rows > div')).toHaveCount(mobileFit.rows);
  expect((await page.locator('.xterm-screen').boundingBox())!.width).toBeLessThan(390);
  await page.screenshot({ path: testInfo.outputPath('terminal-mobile-controlled.png'), fullPage: true });
  // Clamp proposals at the frozen protocol bounds, using real viewport changes.
  await page.setViewportSize({ width: 160, height: 250 });
  await expect.poll(() => terminal.frames.at(-1)).toMatchObject({ type: 'resize', columns: 20, rows: 5 });
  await page.setViewportSize({ width: 6000, height: 5000 });
  await expect.poll(() => terminal.frames.at(-1)).toMatchObject({ type: 'resize', columns: 500, rows: 250 });
  await page.setViewportSize({ width: 1440, height: 960 });
  await expect.poll(() => terminal.frames.at(-1)).toMatchObject({ type: 'resize', columns: fitted.columns, rows: fitted.rows });
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
  await expect(page.getByText('View only', { exact: true })).toBeVisible();
  const beforeTakeover = terminal.frames.length;
  terminal.state(older); terminal.receipt(pending);
  await input.focus(); await page.keyboard.type('ignored');
  expect(terminal.frames).toHaveLength(beforeTakeover);
  await resumeRenderer();
  await expect(page.locator('.xterm-rows > div')).toHaveCount(60);
  expect(terminal.frames).toHaveLength(beforeTakeover);
  terminal.autoReceipt = true;
  await take();
  expect(terminal.current.messages.filter(m => m.type === 'control').at(-1)).toMatchObject({ expected_epoch: older.control!.epoch + 1 });
  expect(terminal.inputs.at(-1)?.data).toBe(Buffer.from('p').toString('base64'));
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
  const priorViewer = terminal.current.viewer;
  terminal.view = { ...terminal.view, dimensions: { columns: 90, rows: 30 } };
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  await expect(page.getByText('View only', { exact: true })).toBeVisible();
  expect(terminal.view.control).toEqual(hold);
  expect(terminal.current.viewer).not.toBe(priorViewer);
  expect(terminal.current.messages).toEqual([]);
  await resumeRenderer();
  await expect(page.locator('.xterm-rows > div')).toHaveCount(30);
  terminal.output('Fresh viewer after disposal');
  await expect(page.locator('.xterm-screen')).toContainText('Fresh viewer after disposal');
  await expect(page.locator('.xterm-screen')).not.toContainText('Disposed pending write');
  expect(terminal.current.messages).toEqual([]);
  // A stale take refreshes observations and awaits another explicit click.
  terminal.conflictNextControl = true;
  const readsBeforeConflict = calls(api, 'terminal_get').length;
  await page.getByRole('button', { name: 'Take control', exact: true }).click();
  await expect(page.getByRole('alert').filter({ hasText: /Control changed/ })).toBeVisible();
  await expect.poll(() => calls(api, 'terminal_get').length).toBeGreaterThan(readsBeforeConflict);
  await settleViewport();
  expect(terminal.current.messages).toHaveLength(1);
  await take();
  terminal.holdNextInput = true;
  await input.focus(); await page.keyboard.type('u');
  await expect.poll(() => terminal.inputs.length).toBe(1);
  const uncertain = terminal.frames.at(-1)!;
  await page.keyboard.type('never replay');
  terminal.receipt(uncertain, 'uncertain');
  await expect(page.getByRole('alert').filter({ hasText: /Earlier input may have arrived/ })).toBeVisible();
  await page.screenshot({ path: testInfo.outputPath('terminal-uncertain.png'), fullPage: true });
  await reconnect();
  terminal.autoReceipt = true; await take();
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
  terminal.send({ type: 'state', terminal: { ...terminal.view, socket_path: '/not-a-public-field' } });
  await expect(page.getByRole('button', { name: 'Reconnect terminal' })).toBeVisible();
  await reconnect();
  for (const invalid of [
    { columns: 19, rows: 24 }, { columns: 501, rows: 24 }, { columns: 80, rows: 4 }, { columns: 80, rows: 251 },
    { columns: 80, rows: '24' }, { columns: 80, rows: 24, epoch: 1 },
  ]) {
    terminal.send({ type: 'size', ...invalid });
    await expect(page.getByRole('button', { name: 'Reconnect terminal' })).toBeVisible();
    expect(terminal.current.messages).toEqual([]);
    await reconnect();
  }
  // Fresh view-only peers have received no keyboard input; reconnect's rendered
  // sentinel precedes this directly verified public timer stall.
  await freezePressure();
  const bytePressurePeer = terminal.current;
  for (let i = 0; i < 18; i++) terminal.output(Buffer.alloc(65536, 65));
  await expect.poll(() => bytePressurePeer.closed).toBe(true);
  await expect(page.getByRole('alert').filter({ hasText: /Live output exceeded the display limit/ })).toBeVisible();
  expect(bytePressurePeer.messages).toEqual([]);
  const bytePressureConnections = terminal.connections.length;
  await thawPressure();
  expect(bytePressurePeer.closed).toBe(true);
  expect(terminal.connections).toHaveLength(bytePressureConnections);
  expect(bytePressurePeer.messages).toEqual([]);
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
  await expect(page.getByText('View only', { exact: true })).toBeVisible();
  expect(entryPressurePeer.closed).toBe(false);
  terminal.output('one entry too many');
  await expect.poll(() => entryPressurePeer.closed).toBe(true);
  await expect(page.getByRole('alert').filter({ hasText: /Live output exceeded the display limit/ })).toBeVisible();
  const entryPressureConnections = terminal.connections.length;
  await thawPressure();
  await expect(page.locator('.xterm-rows > div')).toHaveCount(rowsBeforeOverflow);
  expect(entryPressurePeer.closed).toBe(true);
  expect(terminal.connections).toHaveLength(entryPressureConnections);
  expect(entryPressurePeer.messages).toEqual([]);
  await reconnect();
  api.authenticated = false;
  await expect(page.getByLabel('Console token')).toBeVisible();
  await expect.poll(() => terminal.current.closed).toBe(true);
  expect(terminal.current.messages).toEqual([]);
});
