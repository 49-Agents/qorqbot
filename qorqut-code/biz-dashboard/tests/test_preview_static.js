// Preview routes the checked-out task-type icons locally; other requests reach the supplied upstream.
const assert = require('node:assert/strict'), fs = require('node:fs'), http = require('node:http'), path = require('node:path');
const {spawn} = require('node:child_process');
const root = path.join(__dirname, '..');
const get = (port, url, method = 'GET') => new Promise((resolve, reject) => {
  const req = http.request({host: '127.0.0.1', port, path: url, method, agent: false}, res => {
    const chunks = []; res.on('data', c => chunks.push(c));
    res.on('end', () => resolve({status: res.statusCode, headers: res.headers, body: Buffer.concat(chunks)}));
  });
  req.on('error', reject); req.end();
});
const listen = server => new Promise(resolve => server.listen(0, '127.0.0.1', () => resolve(server.address().port)));

(async () => {
  const seen = [], sockets = new Set();
  const upstream = http.createServer((req, res) => { seen.push(req.url); res.end('upstream ' + req.url); });
  upstream.on('connection', s => { sockets.add(s); s.on('close', () => sockets.delete(s)); });
  const upstreamPort = await listen(upstream);
  // Reserve a free loopback port for the preview child.
  const probe = http.createServer(), previewPort = await listen(probe);
  await new Promise(resolve => probe.close(resolve));
  const child = spawn(process.execPath, [path.join(root, 'scripts/preview.mjs'), '--host', '127.0.0.1',
    '--port', String(previewPort), '--upstream', `http://127.0.0.1:${upstreamPort}`], {stdio: ['ignore', 'pipe', 'inherit']});
  const exited = new Promise(resolve => child.on('exit', resolve));
  try {
    await new Promise((resolve, reject) => {
      child.stdout.on('data', d => /Preview http/.test(String(d)) && resolve());
      exited.then(() => reject(new Error('preview exited before listening')));
    });
    for (const name of ['customer', 'story', 'email']) {
      const res = await get(previewPort, `/icons/task-types/${name}.svg`);
      assert.equal(res.status, 200, name);
      assert.match(res.headers['content-type'], /^image\/svg\+xml/);
      assert.deepEqual(res.body, fs.readFileSync(path.join(root, 'icons/task-types', `${name}.svg`)), name);
      assert.equal((await get(previewPort, `/icons/task-types/${name}.svg`, 'HEAD')).status, 200);
    }
    assert.equal((await get(previewPort, '/icons/task-types/missing.svg')).status, 404);
    assert.equal((await get(previewPort, '/icons/task-types/..%2f..%2fpackage.json')).status, 404);
    assert.deepEqual(seen, [], 'local namespace never reaches upstream');
    const api = await get(previewPort, '/api/fixture');
    assert.equal(api.status, 200);
    assert.equal(api.body.toString(), 'upstream /api/fixture');
    assert.deepEqual(seen, ['/api/fixture']);
  } finally {
    child.kill();
    await exited;
    upstream.closeAllConnections();
    sockets.forEach(s => s.destroy());
    await new Promise(resolve => upstream.close(resolve));
  }
  console.log('preview static routing ok');
})().catch(e => { console.error(e); process.exit(1); });
