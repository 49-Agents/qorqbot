'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Browser checks are optional. */ }
const browserAvailable = !!chromium && fs.existsSync(process.env.CHROME_PATH || chromium.executablePath());
const browserTest = (name, fn) => test(name, {skip: !browserAvailable && 'Local Playwright Chromium is unavailable'}, fn);

async function fixture(width) {
  const browser = await chromium.launch({headless:true, executablePath:process.env.CHROME_PATH || undefined});
  const page = await browser.newPage();
  await page.setViewportSize({width,height:520});
  await page.setContent('<div class="agfocus" data-attention-board><header><div data-attention-focus></div><p data-attention-error hidden></p></header><div data-attention-list></div></div>');
  await page.addStyleTag({path:path.join(__dirname,'../app.css')});
  await page.addStyleTag({path:path.join(__dirname,'../css/agent-focus.css')});
  await page.evaluate(() => {
    window.state = {bizs:[]};
    window.identityAvatar = (kind, name) => `<span class="identity-avatar identity-${kind}" role="img" aria-label="${name}"><svg viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/></svg></span>`;
    window.SVG_X = '<svg></svg>'; window.post = async () => ({}); window.coreData = x => x; window.newRequestId = () => 'k';
    window.toast = () => {}; window.taskStateObserve = (_s, t) => t; window.taskStateBind = () => {}; window.taskStateSet = () => {};
    window.customerStateText = () => ''; window.agfocusContext = () => {}; window.agfocusUnread = () => {};
    window.tNext = s => s; window.workerMounts = 0; window.globalAgentProfile = () => null;
  });
  await page.addScriptTag({path:path.join(__dirname,'../js/attention-board.js')});
  await page.evaluate(() => {
    window.agfocusRowHtml = () => '<div class="trow agfocus-row"><span class="agfocus-notification"></span><span class="agfocus-marker"></span><span class="agfocus-scope"></span><div class="task-headline"><p class="ttl"></p><span class="agfocus-flag" hidden></span></div><div class="agfocus-actions"></div></div>';
    window.attentionStateMarker = () => {};
    const real = window.taskWorkerMount; window.taskWorkerMount = (...a) => { workerMounts++; return real(...a); };
    const task = (id, title, workers) => ({task_ref:{id, business_id:null}, membership_id:'m'+id, membership_revision:1, task:{id, title, status:'open', worker_ids:workers,
      workers:workers.map(worker => ({id:worker, display_name:worker, kind:'agent', is_owner:false})), revision:1}});
    window.p = {agentId:'a1', attentionBoard:{board:document.querySelector('[data-attention-board]'), data:{owner_id:'me', attention:{state:'unfocused'},
      items:[task('1','Mine',['me']), task('2','Agent only',['agent-1']), task('3','Unassigned',[]), task('4','Shared',['me','agent-1'])]}, filter:'agents'}};
    attentionBoardPaint(p);
  });
  return {browser, page};
}
const titles = page => page.$$eval('[data-attention-list] .ttl', els => els.map(e => e.textContent));

browserTest('Todo owner toggle switches between my tasks and everyone', async () => {
  for (const width of [1024, 360]) {
    const {browser, page} = await fixture(width);
    try {
      assert.equal(await page.locator('select').count(), 0);
      const toggle = page.locator('button.attention-owner-toggle');
      assert.equal(await toggle.count(), 1);
      assert.equal(await toggle.getAttribute('aria-pressed'), 'false');
      assert.equal(await toggle.locator('.identity-avatar').count(), 0);
      assert.equal(await toggle.locator('> svg[aria-hidden="true"]').count(), 1);
      assert.deepEqual(await titles(page), ['Mine', 'Agent only', 'Unassigned', 'Shared']);
      await toggle.focus(); await page.keyboard.press('Enter');
      assert.equal(await page.locator('button.attention-owner-toggle').getAttribute('aria-pressed'), 'true');
      assert.deepEqual(await titles(page), ['Mine', 'Shared']);
      assert.equal(await page.evaluate(() => p.attentionBoard.filter), 'owner');
      await page.locator('button.attention-owner-toggle').focus(); await page.keyboard.press('Space');
      assert.equal(await page.locator('button.attention-owner-toggle').getAttribute('aria-pressed'), 'false');
      assert.equal((await titles(page)).length, 4);
      const box = await page.locator('button.attention-owner-toggle').boundingBox();
      assert.ok(box.width <= 40 && box.height <= 28);
      if (width === 1024) await page.screenshot({path:process.env.TODO_SHOT || undefined, type:'png'}).catch(() => {});
    } finally { await browser.close(); }
  }
});

browserTest('Todo displays assigned workers without offering assignment controls', async () => {
  const {browser, page} = await fixture(1024);
  try {
    // Read-only avatars preserve worker visibility; unassigned tasks have no plus button.
    assert.equal(await page.locator('[data-attention-list] [data-task-workers]').count(), 4);
    assert.equal(await page.evaluate(() => workerMounts), 4);
    const empty = page.locator('[data-attention-member="m3"] [data-task-workers]');
    assert.equal(await empty.count(), 1);
    assert.equal(await empty.isVisible(), false);
    assert.equal(await empty.textContent(), '');
    assert.equal(await empty.getAttribute('aria-label'), 'Workers: none');
    const shared = page.locator('[data-attention-member="m4"] [data-task-workers]');
    assert.equal(await shared.locator('.identity-avatar').count(), 2);
    assert.equal(await shared.getAttribute('aria-label'), 'Workers: me, agent-1');
    assert.equal(await shared.evaluate(el => el.tagName), 'SPAN');
    assert.equal(await shared.getAttribute('role'), 'img');
    assert.equal(await page.locator('button[data-task-workers]').count(), 0);
    await page.evaluate(() => { window.assignmentCalls=0; window.taskOpens=0; window.workCall=async()=>{assignmentCalls++;throw Error('Unexpected worker editor');}; window.attentionBoardOpen=async()=>{taskOpens++;}; });
    await shared.click();
    assert.equal(await page.evaluate(() => assignmentCalls), 0);
    assert.equal(await page.locator('.task-worker-picker').count(), 0);
    assert.equal(await page.evaluate(() => taskOpens), 1);
    assert.deepEqual(await titles(page), ['Mine', 'Agent only', 'Unassigned', 'Shared']);
    assert.equal(await page.evaluate(() => typeof taskWorkerMount), 'function');
  } finally { await browser.close(); }
});
