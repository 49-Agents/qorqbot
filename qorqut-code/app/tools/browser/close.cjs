// Operator supervisor shutdown only; agent detach never calls Browser.close.
const { chromium } = require('playwright-core');
(async () => {
  const browser = await chromium.connectOverCDP(process.argv[2], { timeout: 3000 });
  const session = await browser.newBrowserCDPSession();
  await session.send('Browser.close');
})().then(() => process.exit(0), () => process.exit(1));
