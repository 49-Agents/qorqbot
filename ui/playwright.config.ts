import { defineConfig } from '@playwright/test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

export default defineConfig({
  testDir: './tests',
  workers: 1,
  fullyParallel: false,
  timeout: 45000,
  expect: { timeout: 8000 },
  outputDir: process.env.QORQUT_UI_TEST_ARTIFACTS || mkdtempSync(join(tmpdir(), 'qorqut-ui-checks-')),
  reporter: 'list',
  use: {
    browserName: 'chromium',
    launchOptions: { executablePath: '/usr/bin/google-chrome' },
    viewport: { width: 1440, height: 960 },
    screenshot: 'only-on-failure',
    // Credentials are entered only in synthetic tests. Do not capture network traces.
    trace: 'off',
  },
});
