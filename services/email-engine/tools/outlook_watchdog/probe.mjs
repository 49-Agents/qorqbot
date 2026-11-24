/** Read-only CDP/UI health probe. Never navigates, sends mail or closes the browser. */
import {pathToFileURL} from 'node:url';
import {join} from 'node:path';

const HOSTS = new Set(['outlook.cloud.microsoft', 'outlook.office.com', 'outlook.office365.com']);
const LOGIN_HOSTS = new Set(['login.microsoftonline.com', 'login.live.com', 'login.microsoft.com']);
const bounded = async (promise, ms) => {
  let timer;
  try { return await Promise.race([promise,
    new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('probe_timeout')), ms); })]); }
  finally { clearTimeout(timer); }
};

async function probe(input) {
  let chromium;
  try {
    ({chromium} = await import(pathToFileURL(join(input.bridge_root, 'node_modules/playwright-core/index.mjs')).href));
  } catch { return 'configuration_error'; }
  let browser;
  try { browser = await chromium.connectOverCDP(input.cdp_url, {timeout: 8000}); }
  catch { return 'connection_failed'; }
  return inspect(browser, input.address);
}

export async function inspect(browser, address) {
  const context = browser.contexts()[0];
  if (!context) return 'connection_failed';
  // Probe pages concurrently so one unrelated hung renderer cannot consume
  // the whole deadline or override a responding, explicitly named sender tab.
  const states = await Promise.all(context.pages().map(async page => {
    let host;
    try { host = new URL(page.url()).hostname; } catch { return {}; }
    if (LOGIN_HOSTS.has(host)) return {login: true};
    if (!HOSTS.has(host)) return {};
    let state = {};
    try {
      state = await bounded(page.evaluate(address => ({
        named: window.name === 'qorqut-email:' + address,
        login: !!document.querySelector('input[type="password"], input[type="email"]'),
      }), address.toLowerCase()), 2000);
      state.signedIn = await bounded(page.getByRole('treeitem',
        {name: address, exact: true}).count(), 2000) === 1;
    } catch { state.failed = true; }
    return state;
  }));
  const named = states.filter(state => state.named);
  const relevant = named.length ? named : states;
  if (relevant.some(state => state.failed)) return 'connection_failed';
  if (named.length ? named.every(state => state.signedIn) : states.some(state => state.signedIn)) return 'healthy';
  if (relevant.some(state => state.login) || states.some(state => state.login && !('named' in state))) return 'login_required';
  return 'mailbox_not_ready';
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  let text = '';
  try {
    for await (const chunk of process.stdin) {
      text += chunk;
      if (text.length > 16384) throw new Error('oversize');
    }
    const status = await bounded(probe(JSON.parse(text)), 14000);
    process.stdout.write(JSON.stringify({status}), () => process.exit(0));
  } catch {
    process.stdout.write(JSON.stringify({status: 'connection_failed'}), () => process.exit(0));
  }
}
