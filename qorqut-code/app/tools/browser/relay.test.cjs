const { test } = require('node:test');
const assert = require('node:assert/strict');
const http = require('node:http');
const net = require('node:net');
const { spawn } = require('node:child_process');
const { once } = require('node:events');
const path = require('node:path');

async function listen(server) {
  server.listen(0, '127.0.0.1');
  await once(server, 'listening');
  return server.address().port;
}

async function relay(t, upstreamProxyUrl) {
  const child = spawn(process.execPath, [path.join(__dirname, 'relay.cjs')], { stdio: ['pipe', 'pipe', 'pipe'] });
  t.after(async () => { if (child.exitCode === null) { child.kill(); await once(child, 'exit'); } });
  child.stdin.end(JSON.stringify({ upstreamProxyUrl }) + '\n');
  const [line] = await once(child.stdout, 'data');
  return JSON.parse(line).port;
}

function request(port, url) {
  return new Promise((resolve, reject) => {
    const req = http.get({ host: '127.0.0.1', port, path: url, agent: false }, res => {
      let body = '';
      res.on('data', chunk => { body += chunk; });
      res.on('end', () => resolve({ code: res.statusCode, body }));
    });
    req.setTimeout(3000, () => req.destroy(new Error('timeout')));
    req.on('error', reject);
  });
}

test('business relays authenticate independently; upstream failure never goes direct', { timeout: 15000 }, async t => {
  let originHits = 0;
  const origin = http.createServer((req, res) => { originHits++; res.end('DIRECT'); });
  const originPort = await listen(origin);
  t.after(() => origin.close());
  const seen = [];
  const makeProxy = name => http.createServer((req, res) => {
    seen.push({ name, proxyAuth: req.headers['proxy-authorization'], originAuth: req.headers.authorization });
    if (req.headers['proxy-authorization'] !== 'Basic ' + Buffer.from(name + ':secret').toString('base64')) {
      res.writeHead(407, { 'Proxy-Authenticate': 'Basic realm="proxy"' });
      return res.end();
    }
    if (req.url.endsWith('/origin-auth')) {
      res.writeHead(401, { 'WWW-Authenticate': 'Basic realm="website"' });
    }
    res.end(name);
  });
  const a = makeProxy('a'), b = makeProxy('b');
  const ap = await listen(a), bp = await listen(b);
  t.after(() => { a.closeAllConnections(); a.close(); b.closeAllConnections(); b.close(); });
  const ar = await relay(t, `http://a:secret@127.0.0.1:${ap}`);
  const br = await relay(t, `http://b:secret@127.0.0.1:${bp}`);
  const target = `http://127.0.0.1:${originPort}/`;
  assert.equal((await request(ar, target)).body, 'a');
  assert.equal((await request(br, target)).body, 'b');
  assert.equal((await request(ar, target + 'origin-auth')).code, 401);
  assert(seen.every(item => item.originAuth === undefined));
  const wrong = await relay(t, `http://a:wrong@127.0.0.1:${ap}`);
  assert.notEqual((await request(wrong, target)).code, 200);
  a.closeAllConnections();
  await new Promise(resolve => a.close(resolve));
  assert.notEqual((await request(ar, target)).code, 200);
  assert.equal((await request(br, target)).body, 'b');
  assert.equal(originHits, 0);
});

test('HTTPS CONNECT goes through authenticated upstream tunnel', { timeout: 10000 }, async t => {
  let observed;
  const proxy = http.createServer();
  const sockets = new Set();
  proxy.on('connect', (req, socket, head) => {
    observed = { url: req.url, auth: req.headers['proxy-authorization'] };
    sockets.add(socket);
    socket.on('close', () => sockets.delete(socket));
    socket.write('HTTP/1.1 200 Connection established\r\n\r\n');
    socket.on('data', data => socket.write(data));
    if (head.length) socket.write(head);
  });
  const upstream = await listen(proxy);
  t.after(() => { for (const s of sockets) s.destroy(); proxy.close(); });
  const port = await relay(t, `http://tenant:secret@127.0.0.1:${upstream}`);
  const socket = net.connect(port, '127.0.0.1');
  t.after(() => socket.destroy());
  await once(socket, 'connect');
  socket.write('CONNECT example.invalid:443 HTTP/1.1\r\nHost: example.invalid:443\r\n\r\n');
  assert.match((await once(socket, 'data'))[0].toString(), /200/);
  socket.write('tunnel-payload');
  assert.equal((await once(socket, 'data'))[0].toString(), 'tunnel-payload');
  assert.deepEqual(observed, { url: 'example.invalid:443', auth: 'Basic ' + Buffer.from('tenant:secret').toString('base64') });
});

test('missing upstream fails without publishing a listener', { timeout: 5000 }, async () => {
  const child = spawn(process.execPath, [path.join(__dirname, 'relay.cjs')], { stdio: ['pipe', 'pipe', 'pipe'] });
  let output = '';
  child.stdout.on('data', data => { output += data; });
  child.stdin.end('{}\n');
  const [code] = await once(child, 'exit');
  assert.notEqual(code, 0);
  assert.equal(output, '');
});
