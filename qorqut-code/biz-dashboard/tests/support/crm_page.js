// Shared browser setup for the CRM tests: a page that can reach only the local mock origin, with the app booted.
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
async function launch(server, {viewport = {width: 1400, height: 1000}, route = '/#/global/agents'} = {}) {
  const origin = await server.listen();
  const browser = await chromium.launch({headless: true, executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE, args: ['--no-sandbox']});
  const page = await browser.newPage({viewport});
  const errors = [], dialogs = [];
  page.setDefaultTimeout(12000);
  page.on('pageerror', error => errors.push(error.message));
  page.on('dialog', dialog => { dialogs.push(dialog.message()); dialog.dismiss(); });
  await page.route('**/*', handler => { const url = new URL(handler.request().url()); return url.origin === origin ? handler.continue() : handler.abort(); });
  await page.addInitScript(() => { window.EventSource = class { addEventListener() {} close() {} }; });
  await page.goto(origin + route);
  await page.waitForFunction(() => typeof renderTables === 'function' && state.bizs?.length);
  return {browser, page, origin, errors, dialogs};
}
const until = async (check, what) => { for (let i = 0; i < 280; i++) { if (await check()) return; await new Promise(r => setTimeout(r, 25)); } throw new Error('Timed out waiting for ' + what); };
module.exports = {launch, until};
