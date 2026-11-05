// Isolated component acceptance; core tests exercise real authority/file receipts.
// Component coverage: workingMemoryMount is mounted directly in a test page; this does not prove Working Memory is reachable from navigation (none is exposed).
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), http = require('node:http');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const repo = path.resolve(__dirname, '..');
const agent = '11111111-1111-4111-8111-111111111111';
const records = new Map(), histories = new Map(), receipts = new Map(), requests = [];
let lose = false, writes = 0, hold = null;
const row = (body, revision = 1) => ({id: 'memory', body, revision, content_hash: String(revision).repeat(64),
  updated_at: '2026-09-12T12:00:00Z', sources: [], observations: []});
for (const [scope, body] of [['/root', 'Global original <img src=x onerror=alert(1)>'], ['/biz/a', 'Business A original']]) {
  records.set(scope, row(body)); histories.set(scope, [row(body)]);
}
const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.includes('/operations/')) {
    let raw = ''; for await (const chunk of req) raw += chunk;
    const body = JSON.parse(raw), [scope, op] = url.pathname.split('/operations/');
    requests.push({scope, op, body});
    assert.equal(body.agent_id, agent);
    const reply = value => { res.setHeader('Content-Type', 'application/json'); res.end(JSON.stringify(value)); };
    if (op === 'working_memory_get') {
      if (hold && scope === '/biz/a') { const wait = hold; hold = null; await wait; }
      const current = records.get(scope);
      return reply({status: 'ok', data: {document: body.version ? histories.get(scope).find(r => r.revision === body.version) : current,
        history: [...histories.get(scope)].reverse().map(r => ({version: r.revision, created_at: r.updated_at})), history_before: null}});
    }
    if (receipts.has(body.idempotency_key)) return reply(receipts.get(body.idempotency_key));
    const current = records.get(scope);
    if (current.revision !== body.expected_revision || current.content_hash !== body.expected_content_hash) {
      res.statusCode = 409; return reply({status: 'error', error: {code: 'revision_conflict', message: 'Newer version exists'}});
    }
    const next = {...row(body.body, current.revision + 1), sources: body.sources};
    records.set(scope, next); histories.get(scope).push(next); writes++;
    const result = {status: 'ok', data: {revision: next.revision, saved: true}};
    receipts.set(body.idempotency_key, result);
    if (lose) { lose = false; res.writeHead(503, {'Content-Type':'text/plain'}).end('Response lost'); return; }
    return reply(result);
  }
  if (url.pathname === '/') {
    res.setHeader('Content-Type', 'text/html');
    return res.end(`<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/app.css"></head><body><div id="host" style="max-width:450px;margin:30px auto"></div>
      <script>
      window.state={bizs:[{slug:'a',name:'Business A'}]};
      window.esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      window.newRequestId=()=>crypto.randomUUID();
      window.post=async(url,body)=>{const r=await fetch(url,{method:'POST',body:JSON.stringify(body)});const data=await r.json();if(data.status==='error'){const e=new Error(data.error.message);e.code=data.error.code;e.httpStatus=r.status;throw e;}return data;};
      window.coreData=v=>v.data;
      </script><script src="/js/working-memory.js"></script>
      <script>window.mount=business=>workingMemoryMount(document.querySelector('#host'),'${agent}',business,{globalAgent:true});mount(null);</script></body></html>`);
  }
  const file = path.join(repo, url.pathname);
  if (!file.startsWith(repo + path.sep) || !fs.existsSync(file)) { res.statusCode = 404; return res.end(); }
  res.setHeader('Content-Type', file.endsWith('.js') ? 'text/javascript' : 'text/css'); res.end(fs.readFileSync(file));
});

(async () => {
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  const browser = await chromium.launch({headless:true, ...(process.env.CHROMIUM_PATH ? {executablePath:process.env.CHROMIUM_PATH} : {})});
  try {
    const page = await browser.newPage({viewport:{width:1000,height:900}}), errors = [];
    page.on('pageerror', error => errors.push(error.message));
    await page.goto(`http://127.0.0.1:${server.address().port}`);
    await page.locator('.wm-body').waitFor();
    assert.match(await page.locator('.wm-body').inputValue(), /Global original/);
    assert.equal(await page.locator('#host img').count(), 0);
    await page.locator('.wm-body').fill('Owner draft retained');
    await page.locator('.wm-scope').selectOption('a');
    await page.waitForFunction(() => document.querySelector('.wm-body')?.value === 'Business A original');
    await page.locator('.wm-scope').selectOption('');
    await page.waitForFunction(() => document.querySelector('.wm-body')?.value === 'Owner draft retained');
    lose = true;
    await page.locator('.wm-save').click();
    await page.getByRole('button', {name:'Retry same save',exact:true}).waitFor();
    assert.equal(await page.locator('.wm-body').isDisabled(), true);
    await page.locator('.wm-save').click();
    await page.waitForFunction(() => document.querySelector('.wm-revision')?.textContent.includes('Revision 2'));
    assert.equal(writes, 1);
    await page.locator('.wm-changes summary').click();
    await page.locator('.wm-diff del').waitFor();
    assert.match(await page.locator('.wm-diff del').textContent(), /Global original/);
    assert.match(await page.locator('.wm-diff ins').textContent(), /Owner draft retained/);
    assert.equal(await page.locator('.wm-diff img').count(), 0);
    const saves = requests.filter(r=>r.op==='working_memory_update');
    assert.deepEqual(saves[0], saves[1]);
    await page.locator('.wm-history summary').click();
    assert.ok((await page.locator('.wm-versions').textContent()).includes('Version 1'),
              JSON.stringify({html:await page.locator('#host').innerHTML(), errors}));
    await page.getByRole('button', {name:/Version 1 ·/}).click();
    await page.locator('.wm-restore').waitFor({state:'visible'});
    await page.locator('.wm-restore').click();
    await page.locator('.wm-save').click();
    await page.waitForFunction(() => document.querySelector('.wm-revision')?.textContent.includes('Revision 3'));
    assert.match(records.get('/root').body, /Global original/);
    await page.locator('.wm-body').fill('Do not lose stale draft');
    records.set('/root', row('Another session wrote this', 4));
    await page.locator('.wm-save').click();
    await page.waitForFunction(() => document.querySelector('.wm-error')?.textContent.includes('Newer version'));
    assert.equal(await page.locator('.wm-body').inputValue(), 'Do not lose stale draft');
    assert.equal(await page.locator('.wm-save').isDisabled(), true);
    await page.locator('.wm-reload').click();
    await page.waitForFunction(() => document.querySelector('.wm-body')?.value === 'Another session wrote this');
    let release; hold = new Promise(resolve => {release = resolve;});
    await page.evaluate(() => { mount('a'); });
    await page.waitForTimeout(50);
    await page.evaluate(() => { mount(null); });
    await page.waitForFunction(() => document.querySelector('.wm-body')?.value === 'Another session wrote this');
    release();
    await page.waitForTimeout(100);
    assert.equal(await page.locator('.wm-body').inputValue(), 'Another session wrote this');
    await page.setViewportSize({width:375,height:812});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    if (process.env.WORKING_MEMORY_SCREENSHOT) await page.screenshot({path:process.env.WORKING_MEMORY_SCREENSHOT,fullPage:true});
    assert.deepEqual(errors, []);
    console.log('Working memory browser acceptance passed: scopes, drafts, replay, restore, conflicts, stale responses, narrow layout.');
  } finally { await browser.close(); await new Promise(resolve => server.close(resolve)); }
})().catch(error => { console.error(error); process.exitCode = 1; server.close(); });
