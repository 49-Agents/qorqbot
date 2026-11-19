'use strict';
// Opens the "get your agents ready" screen (js/agent-clients.js, `agentClients.preflight`) in the open Qorqbot window, through the
// app's own inspection port, so the check can take a picture of it. Nobody signs in to anything.
//
// The screen normally appears after the Qorqbot account sign-in, which this check cannot pass. The page's own code is run instead:
// first against the real server (`GET /root/agent-clients`; what it answers without a sign-in is recorded), and when that cannot
// be read, against a stand-in answer of "nothing downloaded, nobody signed in", which is what a new Mac would be told. The output
// says which of the two the screen shows. The screen stays open in the window; the caller takes the picture.
//
//   ELECTRON_RUN_AS_NODE=1 Qorqbot.app/Contents/MacOS/Qorqbot first_open_setup_screen.cjs PORT
const [port] = process.argv.slice(2);

function evaluate(address, expression) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(address);
    const timer = setTimeout(() => { socket.close(); reject(new Error('the window did not answer')); }, 15000);
    socket.onopen = () => socket.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { expression, returnByValue: true, awaitPromise: true } }));
    socket.onerror = () => { clearTimeout(timer); reject(new Error('the inspection connection failed')); };
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id !== 1) return;
      clearTimeout(timer);
      socket.close();
      if (message.result?.exceptionDetails) reject(new Error(message.result.exceptionDetails.exception?.description || 'the page raised an error'));
      else resolve(message.result?.result?.value);
    };
  });
}

const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const REAL_READ = `(async () => {
  const response = await fetch('/root/agent-clients', {credentials: 'include'});
  return JSON.stringify({status: response.status, body: (await response.text()).slice(0, 700)});
})()`;
const OPEN = `(() => { try { sessionStorage.removeItem('qorqbot:agent-setup-later'); } catch (_) {} agentClients.preflight(%OPTIONS%); return 'started'; })()`;
const STAND_IN = `{read: async () => ({claude: {installed: false, signed_in: false, install: {phase: 'not_downloaded', progress: null, message: null, version: '2.1.269'}},
                                      codex: {installed: false, signed_in: false, install: {phase: 'not_downloaded', progress: null, message: null, version: '0.154.0'}}})}`;
const SCREEN = `(() => { const gate = document.querySelector('.ac-gate'); return gate ? JSON.stringify({title: (gate.querySelector('#ac-gate-title') || {}).textContent,
  rows: [...gate.querySelectorAll('[data-ac-gate-rows] > *')].map(row => row.innerText.replace(/\\s+/g, ' ').trim().slice(0, 200)),
  buttons: [...gate.querySelectorAll('button')].map(button => button.textContent.trim() + (button.disabled ? ' (disabled)' : ''))}) : ''; })()`;

async function show(address, options) {
  await evaluate(address, OPEN.replace('%OPTIONS%', options));
  for (let waited = 0; waited < 9000; waited += 500) {
    await pause(500);
    const screen = await evaluate(address, SCREEN);
    if (screen) return JSON.parse(screen);
  }
  return null;
}

(async () => {
  const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
  const page = pages.find((item) => item.type === 'page' && item.url.startsWith('http://127.0.0.1:'));
  if (!page) { console.log(JSON.stringify({ setup_screen: false, error: 'no Qorqbot window' })); process.exitCode = 1; return; }
  const address = page.webSocketDebuggerUrl;
  const result = { setup_screen: false };
  try {
    const real = JSON.parse(await evaluate(address, REAL_READ));
    result.real_endpoint = { status: real.status, body: real.body };
  } catch (error) {
    result.real_endpoint = { error: error.message };
  }
  let screen = null;
  try { screen = await show(address, ''); result.data = 'the server\'s own answer'; } catch (error) { result.real_screen_error = error.message; }
  if (!screen) {
    try { screen = await show(address, STAND_IN); result.data = 'a stand-in answer (the server could not be read without a sign-in)'; } catch (error) { result.stand_in_error = error.message; }
  }
  if (screen) { result.setup_screen = true; result.screen = screen; }
  console.log(JSON.stringify(result));
  if (!screen) process.exitCode = 1;
})().catch((error) => { console.log(JSON.stringify({ setup_screen: false, error: error.message })); process.exitCode = 1; });
