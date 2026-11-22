'use strict';
// Talks to the open Qorqbot app through its own inspection port. Run with the app's Electron in Node mode, so the
// machine needs no Node.js (APP is the extracted AppImage's `qorqbot-desktop` or /opt/Qorqbot/qorqbot-desktop):
//
//   ELECTRON_RUN_AS_NODE=1 APP first_open_page.cjs sign-in PORT SECONDS [SCREENSHOT_PREFIX]
//       succeeds once a dashboard window shows the sign-in screen; writes PREFIX-sign-in.png when it does
//   ELECTRON_RUN_AS_NODE=1 APP first_open_page.cjs shots PORT SCREENSHOT_PREFIX
//       writes PREFIX-window-N.png for every open window and prints what each shows
//   ELECTRON_RUN_AS_NODE=1 APP first_open_page.cjs pid PORT
//       prints the process id of the app's main process
const fs = require('node:fs');

const [mode, port, third, fourth] = process.argv.slice(2);
const WANTED = 'sign in to qorqbot';
const TITLE = "(document.getElementById('account-gate-title') || {}).textContent || ''";
const BODY = "document.body ? document.body.innerText.slice(0, 400) : ''";

/** One DevTools protocol command to one target. */
function command(address, method, params = {}) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(address);
    const timer = setTimeout(() => { socket.close(); reject(new Error('the window did not answer')); }, 10000);
    socket.onopen = () => socket.send(JSON.stringify({ id: 1, method, params }));
    socket.onerror = () => { clearTimeout(timer); reject(new Error('the inspection connection failed')); };
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id !== 1) return;
      clearTimeout(timer);
      socket.close();
      if (message.error) reject(new Error(message.error.message));
      else resolve(message.result);
    };
  });
}

async function evaluate(address, expression) {
  const result = await command(address, 'Runtime.evaluate', { expression, returnByValue: true });
  return String(result?.result?.value ?? '');
}

async function screenshot(address, file) {
  const { data } = await command(address, 'Page.captureScreenshot', { format: 'png' });
  fs.writeFileSync(file, Buffer.from(data, 'base64'));
}

async function windows() {
  const response = await fetch(`http://127.0.0.1:${port}/json/list`);
  return (await response.json()).filter((page) => page.type === 'page');
}

async function signIn(seconds, prefix) {
  const started = Date.now();
  let seen = [];
  while (Date.now() - started < Number(seconds) * 1000) {
    try {
      seen = [];
      for (const page of await windows()) {
        const title = await evaluate(page.webSocketDebuggerUrl, TITLE);
        seen.push({ url: page.url, text: title || await evaluate(page.webSocketDebuggerUrl, BODY) });
        if (page.url.startsWith('http://127.0.0.1:') && title.toLowerCase().includes(WANTED)) {
          if (prefix) await screenshot(page.webSocketDebuggerUrl, `${prefix}-sign-in.png`).catch(() => {});
          console.log(JSON.stringify({ sign_in_screen: true, url: page.url, title, waited_seconds: (Date.now() - started) / 1000 }));
          return;
        }
      }
    } catch (error) {
      seen = [{ error: error.message }];
    }
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  console.log(JSON.stringify({ sign_in_screen: false, windows: seen }));
  process.exitCode = 1;
}

async function shots(prefix) {
  const seen = [];
  let index = 0;
  for (const page of await windows()) {
    index += 1;
    const text = (await evaluate(page.webSocketDebuggerUrl, BODY).catch(() => '')).replace(/\s+/g, ' ').trim();
    seen.push({ url: page.url, text: text.slice(0, 160) });
    await screenshot(page.webSocketDebuggerUrl, `${prefix}-window-${index}.png`).catch(() => {});
  }
  console.log(JSON.stringify({ windows: seen }));
}

async function mainProcess() {
  const version = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
  const { processInfo } = await command(version.webSocketDebuggerUrl, 'SystemInfo.getProcessInfo');
  const main = processInfo.find((process) => process.type === 'browser');
  if (!main) throw new Error('no main process listed');
  console.log(main.id);
}

(async () => {
  if (mode === 'sign-in' && port && third) return signIn(third, fourth);
  if (mode === 'shots' && port && third) return shots(third);
  if (mode === 'pid' && port) return mainProcess();
  throw new Error('usage: first_open_page.cjs sign-in PORT SECONDS [PREFIX] | shots PORT PREFIX | pid PORT');
})().catch((error) => { console.error(error.message); process.exitCode = 1; });
