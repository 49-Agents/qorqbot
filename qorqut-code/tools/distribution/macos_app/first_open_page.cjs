'use strict';
// Reads what the open Qorqbot window shows, through the app's own inspection port, and succeeds once it shows the
// sign-in screen. Run with the app's Electron in Node mode, so the Mac needs no Node.js:
//
//   ELECTRON_RUN_AS_NODE=1 Qorqbot.app/Contents/MacOS/Qorqbot first_open_page.cjs PORT SECONDS
const [port, seconds] = process.argv.slice(2);
const WANTED = 'sign in to qorqbot';
const TITLE = "(document.getElementById('account-gate-title') || {}).textContent || ''";
const BODY = "document.body ? document.body.innerText.slice(0, 400) : ''";

function evaluate(address, expression) {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(address);
    const timer = setTimeout(() => { socket.close(); reject(new Error('the window did not answer')); }, 5000);
    socket.onopen = () => socket.send(JSON.stringify({ id: 1, method: 'Runtime.evaluate', params: { expression, returnByValue: true } }));
    socket.onerror = () => { clearTimeout(timer); reject(new Error('the inspection connection failed')); };
    socket.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id !== 1) return;
      clearTimeout(timer);
      socket.close();
      resolve(String(message.result?.result?.value ?? ''));
    };
  });
}

async function windows() {
  const response = await fetch(`http://127.0.0.1:${port}/json/list`);
  return (await response.json()).filter((page) => page.type === 'page');
}

(async () => {
  const started = Date.now();
  let seen = [];
  while (Date.now() - started < Number(seconds) * 1000) {
    try {
      seen = [];
      for (const page of await windows()) {
        const title = await evaluate(page.webSocketDebuggerUrl, TITLE);
        seen.push({ url: page.url, text: title || await evaluate(page.webSocketDebuggerUrl, BODY) });
        if (page.url.startsWith('http://127.0.0.1:') && title.toLowerCase().includes(WANTED)) {
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
})();
