#!/usr/bin/env node
// Synthetic frozen UI responses, real dashboard Signoff controller and Chromium DOM.
const assert = require('node:assert/strict');
const path = require('node:path');
const fixture = require('./fixtures/overseer-ui-v1.json');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
(async () => {
  const browser = await chromium.launch({headless:true, executablePath:'/usr/bin/google-chrome', args:['--no-sandbox','--disable-dev-shm-usage']});
  try {
    const page = await browser.newPage();
    const errors = []; page.on('pageerror', e => { errors.push(e.message); console.error(e.message); });
    await page.goto('about:blank');
    await page.setContent('<div id="agpad-body"></div><textarea id="chat">unsent chat</textarea>');
    await page.evaluate(f => {
      window.f = f; window.sent = []; window.stale = false; window.scenario = ''; window.rejectOnce = false;
      window.T_VIEW_LABEL = {}; window.T_VIEW_ICON = {};
      window.state = {bizs:[],agents:[],scope:'global'};
      window.esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      window.html = (node, value) => { if (node && node._html !== value) { node.innerHTML = value; node._html = value; } };
      window.coreData = x => x.data;
      window.api = async url => {
        if (url === '/session') return {data:{principal_id:f.ids.owner,principal_kind:'human',is_owner:true}};
        const key = '/api' + url;
        if (window.scenario === 'accepted' && key.includes('/deltas/') && !key.includes('/evidence/')) return structuredClone(f.accepted_scenario.delta);
        if (window.scenario === 'historical' && key.includes('/deltas/') && !key.includes('/evidence/')) return structuredClone(f.historical_scenario.delta_revision_1);
        if (window.scenario === 'failed' && key.includes('/applications/') && !key.includes('/plans')) return structuredClone(f.failed_scenario.application);
        if (window.stale && key.startsWith('/api/root/signoffs/')) return f.stale_scenario.signoff;
        const response = f.responses[key] || f.responses[key.replace(/\?revision=1$/, '')]; if (!response) throw Error('Fixture missing: '+key); return structuredClone(response);
      };
      window.post = async (url, body) => { window.sent.push({url,body}); if (window.rejectOnce) { window.rejectOnce = false; window.stale = true; const err = Error('Stale answer'); err.code = 'stale_revision'; throw err; } return structuredClone(f.signoff_answer.response); };
      window.newRequestId = () => 'test-request'; window.sjDateTime = d => d.toISOString();
      window.globalAgentProfiles = []; window.globalAgentProfile = () => null; window.loadGlobalAgentProfiles = async () => {};
      window.taskBusinessLabel = () => ''; window.identityAvatar = () => ''; window.renderSafeLinks = esc;
      window.SVG_X = ''; window.SVG_CHECK = ''; window.toast = () => {}; window.$ = s => document.querySelector(s);
      // Inline proposals now carry the record's comment composer; in the real page these two helpers come from
      // js/conversation.js. This pane's synthetic page stubs both so no real conversation is mounted.
      window.conversationKey = s => `${s.type}:${s.biz || 'global'}:${s.id}`;
      window.conversationMount = () => null;
    }, fixture);
    for (const file of ['signoffs.js','overseer.js']) await page.addScriptTag({path:path.join(__dirname,'..','js',file)});
    await page.evaluate(() => { window.p = {el:document.body,workspaceRole:'learning-applier'}; overseerMount(p); });
    await page.getByText('Review batch', {exact:true}).waitFor();
    assert.equal(await page.locator('.overseer-list button').count(),5);
    await page.locator('.overseer-list button').first().click();
    await page.getByText('Owner input', {exact:true}).waitFor();
    await page.locator('.sg-choice').first().waitFor();
    await page.locator('.sg-choice').first().click();
    await page.locator('.sg-feedback').fill('owner reasoning');
    await page.locator('[data-sg="respond"]').click();
    await page.waitForFunction(() => window.sent.length === 1);
    const sent = await page.evaluate(() => window.sent[0]);
    assert.equal(sent.url, '/root/signoffs/' + fixture.signoff_answer.path.split('/').at(-2) + '/respond');
    assert.equal(sent.body.proposal_revision, 1); assert.deepEqual(sent.body.selected_choices,['accept']); assert.equal(sent.body.notes,'owner reasoning');
    assert.equal(await page.locator('#chat').inputValue(),'unsent chat');
    // Fresh mount with changed canonical tuple stays read-only against original binding.
    await page.evaluate(() => { p.workspaceDispose(); document.querySelector('#agpad-body').replaceChildren(); window.stale = true; overseerMount(p); });
    await page.locator('.overseer-list button').first().click();
    await page.getByText('This question is read-only or has changed. Open the current change to review it.').waitFor();
    assert.equal(await page.locator('[data-sg="respond"]').isDisabled(),true);
    assert.equal((await page.evaluate(() => window.sent.length)),1);
    // A stale write refreshes the canonical tuple without discarding the original draft.
    await page.evaluate(() => { p.workspaceDispose(); document.querySelector('#agpad-body').replaceChildren(); window.stale = false; window.rejectOnce = true; overseerMount(p); });
    await page.locator('.overseer-list button').first().click();
    await page.locator('.sg-choice').first().click();
    await page.locator('.sg-feedback').fill('retain this draft');
    await page.locator('[data-sg="respond"]').click();
    await page.getByText('This question is read-only or has changed. Open the current change to review it.').waitFor({timeout:2000});
    assert.equal(await page.locator('.sg-feedback').inputValue(), 'retain this draft');
    assert.equal(await page.locator('.sg-choice').first().getAttribute('aria-checked'), 'true');
    assert.equal(await page.locator('[data-sg="respond"]').isDisabled(),true);
    // A historic snapshot retains the saved wording and separate current decision.
    await page.evaluate(() => { p.workspaceDispose(); document.querySelector('#agpad-body').replaceChildren(); window.stale = false; window.scenario = 'historical'; overseerMount(p); });
    await page.locator('.overseer-list button').first().click();
    await page.getByText(/Displayed revision 1 · Historical/).waitFor();
    assert.equal(await page.locator('[data-sg="respond"]').count(),0);
    assert.equal(await page.locator('#chat').inputValue(),'unsent chat');
    // The evidence detail explicitly reports unavailable; no cached excerpt becomes source content.
    await page.evaluate(() => { p.workspaceDispose(); document.querySelector('#agpad-body').replaceChildren(); window.scenario = ''; overseerMount(p); });
    await page.locator('.overseer-list button').first().click();
    await page.locator('[data-evidence] summary').click();
    await page.getByText('Evidence unavailable', {exact:true}).first().waitFor();
    assert.ok(!(await page.locator('[data-evidence]').innerText()).includes('When doing research'));
    assert.equal(await page.locator('[data-evidence] pre').count(),0);
    // Accepted work remains queued and shows the actual owner words separately.
    await page.evaluate(() => { p.workspaceDispose(); document.querySelector('#agpad-body').replaceChildren(); window.scenario = 'accepted'; f.responses['/api/overseer/applications?queue=unimplemented'] = f.accepted_scenario.queue; overseerMount(p); });
    await page.getByRole('button', {name:'Accepted queue'}).click();
    await page.getByRole('button', {name:/queued/}).waitFor();
    await page.getByRole('button', {name:'Deltas'}).click();
    await page.getByRole('button', {name:/Keep research evidence separate from opinion/}).click();
    await page.getByText(/Current decision: accepted/).waitFor();
    assert.ok((await page.locator('.overseer-content').innerText()).includes('Owner:'));
    // Failed post-merge checks retain both facts in application history.
    await page.evaluate(() => { p.workspaceDispose(); document.querySelector('#agpad-body').replaceChildren(); window.scenario = 'failed'; f.responses['/api/overseer/applications'] = f.failed_scenario.all_applications; overseerMount(p); });
    await page.getByRole('button', {name:'Application history'}).click();
    await page.getByRole('button', {name:/checks failed/}).click();
    await page.getByText(/Merge: 222222222222/).waitFor();
    assert.ok((await page.locator('.overseer-content').innerText()).includes('Checks: failed'));
    // A plan Signoff can jointly accept the revised delta and its saved plan.
    await page.evaluate(() => {
      p.workspaceDispose(); document.querySelector('#agpad-body').replaceChildren(); window.scenario = ''; window.stale = false;
      const detail = structuredClone(f.failed_scenario.application), d = detail.data;
      const ref = structuredClone(f.responses['/api/overseer/deltas/' + f.ids.delta].data.review_input);
      d.application.phase = 'planning'; d.application.accepted_revision = 1; d.application.plan_revision = 2;
      d.application.merge = null; d.application.checks = {state:'not_run',receipts:[]};
      d.plan.revision = 2; d.plan.delta_revision = 2; d.plan.replaces_accepted_revision = 1;
      d.displayed_plan_revision = 2; d.current_plan_revision = 2; d.historical = false;
      Object.assign(ref, {purpose:'plan',target_revision:2,application_id:f.ids.application,application_revision:d.application.revision,plan_revision:2,actionable:true});
      d.waiting_input = ref; d.application.waiting_input_ref = ref; d.available_actions = ['open_signoff'];
      f.responses['/api/overseer/applications/' + f.ids.application] = detail;
      f.responses['/api/overseer/applications'] = {data:{schema_version:1,items:[d.application],complete:true,next_cursor:null,availability:'available',gaps:[]}};
      overseerMount(p);
    });
    await page.getByRole('button', {name:'Application history'}).click();
    await page.getByRole('button', {name:/checks not_run/}).click();
    await page.locator('[data-sg="respond"]').waitFor();
    assert.equal(await page.locator('[data-sg="respond"]').isEnabled(), true);
    await page.locator('.sg-choice').first().click();
    await page.locator('.sg-feedback').fill('Approve revised intent and plan');
    await page.locator('[data-sg="respond"]').click();
    await page.waitForFunction(() => window.sent.at(-1)?.body.notes === 'Approve revised intent and plan');
    const planAnswer = await page.evaluate(() => window.sent.at(-1).body);
    assert.equal(planAnswer.proposal_revision, 1); assert.equal(planAnswer.payload_hash, fixture.signoff_answer.body.payload_hash);
    assert.equal(planAnswer.expected_status_revision, 1); assert.deepEqual(planAnswer.selected_choices, ['accept']);
    // A mismatched plan target and a historical plan each stay read-only.
    for (const mode of ['mismatch','historical_plan','wrong_application','wrong_application_revision','wrong_plan']) {
      await page.evaluate(mode => {
        p.workspaceDispose(); document.querySelector('#agpad-body').replaceChildren();
        const d = f.responses['/api/overseer/applications/' + f.ids.application].data;
        d.waiting_input.target_revision = mode === 'mismatch' ? 3 : 2; d.historical = mode === 'historical_plan';
        d.waiting_input.application_id = mode === 'wrong_application' ? f.ids.delta : f.ids.application;
        d.waiting_input.application_revision = mode === 'wrong_application_revision' ? d.application.revision + 1 : d.application.revision;
        d.waiting_input.plan_revision = mode === 'wrong_plan' ? 3 : 2;
        overseerMount(p);
      }, mode);
      await page.getByRole('button', {name:'Application history'}).click();
      await page.getByRole('button', {name:/checks not_run/}).click();
      await page.locator('[data-sg="respond"]').waitFor();
      assert.equal(await page.locator('[data-sg="respond"]').isDisabled(), true, mode);
    }
    // A denied evidence read removes the excerpt and retries on the next open.
    await page.evaluate(() => {
      p.workspaceDispose(); document.querySelector('#agpad-body').replaceChildren();
      const original = api; window.evidenceAttempts = 0;
      window.api = async path => {
        if (path.includes('/evidence/')) {
          window.evidenceAttempts++; window.evidencePath = path;
          if (window.evidenceAttempts === 1) { const err = Error('Source access revoked'); err.code = 'forbidden'; err.httpStatus = 403; throw err; }
          return {data:{schema_version:1,availability:'available',content:{text:'Newly admitted source'},gaps:[]}};
        }
        return original(path);
      };
      overseerMount(p);
    });
    await page.locator('.overseer-list button').first().click();
    await page.locator('[data-evidence] summary').click();
    await page.getByText(/Source access revoked/).waitFor();
    const denied = await page.locator('[data-evidence]').innerText();
    assert.ok(denied.includes('unavailable')); assert.ok(!denied.includes('When doing research'));
    await page.locator('[data-evidence] summary').click();
    await page.locator('[data-evidence] summary').click();
    await page.getByText(/Newly admitted source/).waitFor();
    assert.equal(await page.evaluate(() => window.evidenceAttempts), 2);
    assert.match(await page.evaluate(() => window.evidencePath), /\?revision=1$/);
    assert.deepEqual(errors, []);
    console.log('P06 frozen batch, bound answers, revised plan, stale and evidence retry cases passed');
  } finally { await browser.close(); }
})().catch(e => { console.error(e); process.exitCode=1; });
