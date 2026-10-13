import { test as base, expect } from '@playwright/test';
import { SyntheticAPI, beta } from './synthetic-api';
import { SyntheticTerminal } from './synthetic-terminal';

const test = base.extend<{ api: SyntheticAPI }>({
  api: async ({}, use) => { const api = await new SyntheticAPI().start(); try { await use(api); } finally { await api.close(); } },
});

// QA observation UI27: the tty discards input that races its interrupt/suspend signal handling, so
// an explicit Ctrl+C/Ctrl+Z ends its batch. The frames below are a protocol fixture, not shell proof.
test('Ctrl+C and Ctrl+Z end a queued input batch; later keys wait for the receipt without replay', async ({ page, api }) => {
  const terminal = new SyntheticTerminal(api);
  await terminal.route(page);
  await page.goto(api.url);
  await page.getByRole('navigation', { name: 'Agents' }).getByRole('button', { name: /^onbash/ }).click();
  await page.getByRole('button', { name: 'Terminal', exact: true }).click();
  const input = page.getByRole('textbox', { name: 'Terminal input', exact: true });
  await expect(input).toBeEditable();
  await expect.poll(() => terminal.frames.some(frame => frame.type === 'resize')).toBe(true);
  const text = (frame: { type: string; data?: string }) => Buffer.from(frame.data!, 'base64').toString('utf8');

  terminal.holdNextInput = true;
  await input.focus(); await page.keyboard.type('x');
  await expect.poll(() => terminal.inputs.length).toBe(1);
  const held = terminal.inputs[0];
  await page.keyboard.type('ab'); await page.keyboard.press('Control+c');
  await page.keyboard.type('cd'); await page.keyboard.press('Control+z');
  await page.keyboard.type('éf'); await page.keyboard.press('Enter');
  expect(terminal.inputs).toHaveLength(1);

  terminal.receipt(held);
  await expect.poll(() => terminal.inputs.length).toBe(2);
  expect(text(terminal.inputs[1])).toBe('ab\x03');
  expect(terminal.inputs).toHaveLength(2);
  terminal.receipt(terminal.inputs[1]);
  await expect.poll(() => terminal.inputs.length).toBe(3);
  expect(text(terminal.inputs[2])).toBe('cd\x1a');
  expect(terminal.inputs).toHaveLength(3);
  terminal.receipt(terminal.inputs[2]);
  await expect.poll(() => terminal.inputs.length).toBe(4);
  expect(text(terminal.inputs[3])).toBe('éf\r');
  terminal.receipt(terminal.inputs[3]);
  expect(terminal.inputs.map(text).join('')).toBe('xab\x03cd\x1aéf\r');
  expect(terminal.inputs.map(frame => frame.sequence)).toEqual([...terminal.inputs.map(frame => frame.sequence)].sort((a, b) => a - b));
  await expect(page.getByRole('alert')).toHaveCount(0);
});
