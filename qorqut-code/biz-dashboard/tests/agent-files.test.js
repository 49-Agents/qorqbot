'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Browser checks are optional. */ }
const browserAvailable = !!chromium && fs.existsSync(chromium.executablePath());
const browserTest = (name, fn) => test(name, {skip: !browserAvailable && 'Local Playwright Chromium is unavailable'}, fn);
const dashboard = path.join(__dirname, '..');
const read = file => fs.readFileSync(path.join(dashboard, file), 'utf8');

test('Files is offered and restored only in Ordubashy’s pane', () => {
  const context = vm.createContext({window:{}, T_VIEW_LABEL:{}, T_VIEW_ICON:{}, setInterval(){}});
  vm.runInContext(read('js/agentpane.js'), context);
  assert.ok(context.agentPaneViews('global', 'ordubashy').includes('files'));
  for (const role of ['global-worker', 'learning-applier', null]) {
    assert.ok(!context.agentPaneViews('global', role, ['files']).includes('files'));
    assert.equal(context.agentPaneViewAllowed(role, 'files'), false);
  }
});

async function fixture() {
  const browser = await chromium.launch({headless:true});
  const page = await browser.newPage({viewport:{width:960, height:780}});
  const errors = [], requests = [];
  page.on('pageerror', error => errors.push(error.message));
  const dir = (name, parent = '', extra = {}) => ({name, path:parent ? parent + '/' + name : name, kind:'directory', ...extra});
  const file = (name, parent) => ({name, path:parent + '/' + name, kind:'file', size:200});
  const project = {name:'Example project', description:'Current plans and project notes.', open_tasks:12, signoffs:3, subprojects:null};
  const hostile = '<img src=x onerror=alert(1)>';
  const folders = {
    '':[dir('projects'), dir('agents')],
    projects:[dir('example', 'projects', {project}), dir('archived', 'projects', {project:null}), dir(hostile, 'projects', {project:null})],
    'projects/example':[dir('code', 'projects/example'), dir('hq', 'projects/example')],
    'projects/example/hq':[file('notes.md', 'projects/example/hq')],
    'projects/example/code':Array.from({length:105}, (_, index) => file(`file-${String(index).padStart(3, '0')}.txt`, 'projects/example/code')),
    agents:[],
  };
  await page.route('http://files.test/**', async route => {
    const request = route.request(), url = new URL(request.url());
    requests.push({method:request.method(), path:url.pathname, query:url.search});
    if (url.pathname === '/api/root/files/directory') {
      const selected = url.searchParams.get('path'), offset = url.searchParams.has('cursor') ? 100 : 0;
      let entries = folders[selected] || [];
      if (selected === '' && url.searchParams.get('hidden') === '1') entries = [...entries, dir('.hidden')];
      return route.fulfill({json:{status:'ok', data:{path:selected, entries:entries.slice(offset, offset + 100), total:entries.length,
        project:selected.startsWith('projects/example') ? project : null, next_cursor:entries.length > offset + 100 ? 'next' : null}}});
    }
    if (url.pathname === '/api/root/files/metadata') return route.fulfill({json:{status:'ok', data:{name:'notes.md', path:'projects/example/hq/notes.md', size:200,
      modified_at:'2026-01-01T00:00:00Z', current:true, managed:null, binary:false, preview_kind:'markdown', urls:{view:'/files/view?path=projects/example/hq/notes.md', download:'/api/root/files/download?path=projects/example/hq/notes.md', copy:'/files/view?path=projects/example/hq/notes.md'}}}});
    if (url.pathname === '/api/root/files/content') return route.fulfill({json:{status:'ok', data:{kind:'markdown', size:200, truncated:false,
      html:'<h1>Project notes</h1><p>Readable preview.</p><script>parent.alert(1)</script><img src=x onerror=parent.alert(1)><a href="javascript:alert(1)">unsafe</a>', text:'# Project notes\n\nReadable preview.'}}});
    if (url.pathname === '/') return route.fulfill({contentType:'text/html', body:`<!doctype html><html data-theme="dark"><head><meta charset="utf-8">
      <link rel="stylesheet" href="/app.css"><link rel="stylesheet" href="/css/tasks.css"><link rel="stylesheet" href="/css/agent-workspace.css"><link rel="stylesheet" href="/css/agent-files.css">
      <style>.agpad{width:100%;height:100vh;min-height:0} .agfiles-host{height:100%}</style></head><body class="agent-workspace-app">
      <div class="agpad tasks"><div class="tbody" id="agpad-body"></div></div><script>
      window.T_VIEW_LABEL={};window.T_VIEW_ICON={};window.$=(s,r=document)=>r.querySelector(s);
      window.esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      window.toast=()=>{};window.p={el:document.querySelector('.agpad')};
      </script><script src="/js/agent-files.js"></script><script>agentFilesMount(p);</script></body></html>`});
    const relative = url.pathname === '/files/view' ? 'file-viewer.html' : url.pathname.slice(1);
    const target = path.join(dashboard, relative);
    if (target.startsWith(dashboard + path.sep) && fs.existsSync(target)) return route.fulfill({path:target});
    return route.fulfill({status:404, body:'Missing fixture'});
  });
  await page.goto('http://files.test/');
  await page.locator('[data-row="projects/example"]').waitFor();
  return {browser, page, errors, requests};
}

browserTest('tree expands real directory responses, paginates, filters and handles keyboard navigation', async () => {
  const h = await fixture();
  try {
    const {page} = h;
    assert.match(await page.locator('[data-row="projects/example"] .agfiles-metrics').innerText(), /12.*3.*—/s);
    assert.equal(await page.locator('[data-row="projects/example"] .agfiles-name').evaluate(node => getComputedStyle(node).color), 'rgb(247, 247, 247)');
    assert.equal(await page.locator('.agfiles-host').evaluate(node => getComputedStyle(node).padding), '0px');
    assert.equal(await page.locator('.agfiles img').count(), 0);
    assert.match(await page.locator('[data-row="projects/archived"]').innerText(), /—/);
    await page.locator('.agfiles-tree [data-select="projects/example"]').click();
    await page.locator('.agfiles-tree [data-select="projects/example/code"]').click();
    await page.locator('[data-row="projects/example/code/file-099.txt"]').waitFor();
    await page.locator('.agfiles-tree [data-more="projects/example/code"]').click();
    await page.locator('[data-row="projects/example/code/file-104.txt"]').waitFor();
    assert.equal(await page.locator('.agfiles-tree [data-more="projects/example/code"]').count(), 0);
    const search = page.getByRole('searchbox');
    await search.fill('file-104');
    assert.equal(await page.locator('.agfiles-tree [data-row]').count(), 1);
    await search.fill('');
    const project = page.locator('[data-row="projects/example"]');
    await project.focus(); await page.keyboard.press('ArrowLeft');
    assert.equal(await project.getAttribute('aria-expanded'), 'false');
    await page.keyboard.press('ArrowRight');
    assert.equal(await project.getAttribute('aria-expanded'), 'true');
    await page.keyboard.press('ArrowDown');
    assert.equal(await page.evaluate(() => document.activeElement.dataset.row), 'projects/example/code');
    await page.locator('[data-hidden]').check();
    await page.locator('[data-row=".hidden"]').waitFor();
    assert.deepEqual(h.errors, []);
    assert.ok(h.requests.every(request => request.method === 'GET'));
  } finally { await h.browser.close(); }
});

browserTest('file preview reuses the inert reader, supports Source and follows pane appearance and size', async () => {
  const h = await fixture();
  try {
    const {page} = h;
    await page.locator('.agfiles-tree [data-select="projects/example"]').click();
    await page.locator('.agfiles-tree [data-select="projects/example/hq"]').click();
    await page.locator('.agfiles-tree [data-select="projects/example/hq/notes.md"]').click();
    const frame = page.frameLocator('.agfiles-viewer');
    await frame.locator('.fv-md h1').waitFor();
    assert.equal(await frame.locator('.fv-md h1').innerText(), 'Project notes');
    assert.equal(await frame.locator('.fv-md script, .fv-md img, .fv-md a[href^="javascript:"]').count(), 0);
    await frame.getByRole('button', {name:'Source', exact:true}).click();
    assert.match(await frame.locator('.fv-text').innerText(), /# Project notes/);
    await page.setViewportSize({width:380, height:780});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true);
    assert.equal(await page.locator('.agfiles-layout').evaluate(node => getComputedStyle(node).gridTemplateColumns.split(' ').length), 1);
    await page.evaluate(() => { document.documentElement.dataset.theme='light'; document.documentElement.style.setProperty('--surface', 'rgb(240, 240, 240)'); });
    await frame.locator('body').evaluate(node => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
    assert.equal(await frame.locator('body').evaluate(node => getComputedStyle(node).backgroundColor), 'rgb(240, 240, 240)');
    await page.evaluate(() => { p.workspaceDispose(); document.querySelector('#agpad-body').replaceChildren(); agentFilesMount(p); });
    await page.frameLocator('.agfiles-viewer').locator('.fv-md h1').waitFor();
    assert.deepEqual(h.errors, []);
  } finally { await h.browser.close(); }
});

browserTest('failed folders can be retried and a detached read cannot replace a newer pane', async () => {
  const h = await fixture();
  try {
    const {page} = h;
    await page.evaluate(() => {
      window.actualFetch = window.fetch;
      window.fetch = (url, options) => String(url).includes('path=agents')
        ? Promise.resolve(new Response(JSON.stringify({status:'error', error:{message:'Folder is unavailable'}}), {status:403}))
        : actualFetch(url, options);
    });
    await page.locator('.agfiles-tree [data-select="agents"]').click();
    await page.getByRole('alert').filter({hasText:'Folder is unavailable'}).first().waitFor();
    await page.evaluate(() => { window.fetch = actualFetch; });
    await page.locator('.agfiles-detail [data-retry="agents"]').click();
    await page.locator('.agfiles-detail').getByText('This folder is empty.').waitFor();
    await page.evaluate(() => {
      window.fetch = (url, options) => String(url).includes('path=projects%2Fexample')
        ? new Promise(resolve => { window.finishRead = () => resolve(new Response(JSON.stringify({status:'ok', data:{entries:[], total:0}}))); })
        : actualFetch(url, options);
    });
    await page.locator('.agfiles-tree [data-select="projects/example"]').click();
    await page.waitForFunction(() => typeof window.finishRead === 'function');
    await page.evaluate(() => {
      p.workspaceDispose();
      document.querySelector('#agpad-body').textContent = 'New pane content';
      finishRead();
    });
    await page.evaluate(() => new Promise(resolve => requestAnimationFrame(resolve)));
    assert.equal(await page.locator('#agpad-body').innerText(), 'New pane content');
    assert.deepEqual(h.errors, []);
  } finally { await h.browser.close(); }
});
