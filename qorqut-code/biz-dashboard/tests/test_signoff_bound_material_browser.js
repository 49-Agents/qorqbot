#!/usr/bin/env node
// Saved Overseer material in chat and the Signoff pane; synthetic data only.
const assert = require('node:assert/strict');
const path = require('node:path');
const fixture = require('./fixtures/overseer-ui-v1.json');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root = path.join(__dirname, '..');
const signoff = structuredClone(Object.entries(fixture.responses).find(([key]) => key.includes('/root/signoffs/'))[1].data.signoff);
const material = {
  targets: {delta_id: 'fixture-delta', delta_revision: 1, application_id: null, plan_revision: null},
  delta: {schema_version: 1, delta_id: 'fixture-delta', revision: 1, summary: 'Review research guidance',
    change_direction: 'Present research findings separately from hypotheses.\nLabel every hypothesis and cite its evidence.',
    scope: {description: 'Research reports for Project Atlas', conditions: ['When preparing a research report'],
      exceptions: ['Brainstorming may include clearly labelled opinions.'], project_ids: [], workflow_refs: [], work_types: []},
    target_refs: ['research/skills/report/SKILL.md'], uncertainties: [],
    ranking_reason: 'An explicit instruction supported by two independent work episodes.',
    support_summary: {episode_count: 2, proposal_count: 3, episode_refs: ['episode-one', 'episode-two']},
    proposal_ids: ['proposal-one'], created_by: 'fixture-agent', created_at: '2026-01-01T00:00:00Z'}
};
const explanation = 'Acceptance queues separate owner-led application.\n\nBound material:\n' + JSON.stringify(material);
signoff.input.explanation = signoff.preview.input.explanation = explanation;
(async () => {
  const browser = await chromium.launch({headless: true, executablePath: process.env.CHROMIUM_EXECUTABLE || '/usr/bin/google-chrome', args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1000, height: 1000}});
    page.setDefaultTimeout(5000);
    const errors = []; page.on('pageerror', e => errors.push(e.message));
    await page.setContent('<style>:root{--fg:#eee;--fg-2:#aaa;--fg-3:#999;--line-2:#444;--wash:#161616;--sans:Arial}body{background:#111;color:#eee;font:14px Arial}#inline,#pane{width:450px;max-width:100%;margin:auto}button,textarea{background:#111;color:#eee}</style><div id="inline"></div><div id="pane"></div><div id="probe"></div>');
    await page.addStyleTag({path: path.join(root, 'css/signoffs.css')});
    await page.evaluate(({signoff, owner}) => {
      window.saved = signoff; window.posts = []; window.views = [];
      window.state = {bizs: [], agents: []}; window.coreData = x => x.data;
      window.esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      window.renderSafeLinks = esc;
      window.html = (node, value) => { if (node && node._html !== value) { node.innerHTML = value; node._html = value; } };
      window.api = async url => {
        if (url === '/session') return {data: {principal_id: owner, principal_kind: 'human', is_owner: true}};
        if (url === '/root/signoffs/' + saved.id) return {data: {signoff: structuredClone(saved)}};
        throw Error('Unexpected read: ' + url);
      };
      window.post = async (url, body) => { posts.push({url, body}); saved.state = 'answered'; saved.status_revision++;
        return {data: {signoff: structuredClone(saved)}}; };
      window.newRequestId = () => 'synthetic-request'; window.sjDateTime = d => d.toISOString();
      window.taskBusinessLabel = () => ''; window.identityAvatar = () => '';
      window.globalAgentProfiles = []; window.globalAgentProfile = () => null; window.loadGlobalAgentProfiles = async () => {};
      window.SVG_X = ''; window.SVG_CHECK = ''; window.toast = () => {};
      window.conversationKey = s => s.id; window.conversationMount = () => null;
    }, {signoff, owner: fixture.ids.owner});
    await page.addScriptTag({path: path.join(root, 'js/signoffs.js')});
    await page.evaluate(() => {
      for (const id of ['inline', 'pane']) {
        const host = document.getElementById(id);
        const view = sgCreateView({active: () => true, body: () => host, workspace: () => 'scope:global', global: true,
          inline: id === 'inline', category: 'input_request', prefix: id, item: () => ({id: saved.id, businessId: null})});
        views.push(view); view.paint();
      }
    });
    for (const id of ['inline', 'pane']) {
      const card = page.locator('#' + id);
      await card.locator('.sg-bound-material').waitFor().catch(async error => { throw Error(error.message + '\n' + await card.innerText() + '\n' + errors.join('\n')); });
      const text = await card.innerText();
      for (const required of [material.delta.change_direction, material.delta.scope.description, material.delta.scope.exceptions[0], material.delta.target_refs[0]]) assert.ok(text.includes(required));
      assert.ok(!text.includes('Bound material:') && !text.includes('fixture-delta') && !text.includes('schema_version'));
      assert.equal(await card.locator('.sg-bound-record').getAttribute('open'), null);
    }
    assert.equal(await page.locator('#inline .sg-choice-explanation').count(), 0);
    assert.equal(await page.locator('#inline .sg-choice').count(), 4);
    assert.deepEqual(await page.evaluate(() => saved), signoff, 'rendering changed the saved approval');
    await page.locator('#inline .sg-bound-record summary').click();
    assert.deepEqual(JSON.parse(await page.locator('#inline .sg-bound-record .sg-text').innerText()), material);
    await page.locator('#inline .sg-bound-record summary').click();
    if (process.env.SIGNOFF_BOUND_SCREENSHOT) await page.locator('#inline').screenshot({path: process.env.SIGNOFF_BOUND_SCREENSHOT});
    await page.setViewportSize({width: 360, height: 1000});
    assert.equal(await page.locator('#inline').evaluate(el => el.scrollWidth <= el.clientWidth), true, 'mobile card overflows');

    // Never discard unknown, malformed or future material. Ordinary questions stay ordinary.
    for (const text of ['Ordinary question', 'Review\n\nBound material:\n{broken', 'Review\n\nBound material:\n[]',
      'Review\n\nBound material:\n' + JSON.stringify({...material, delta: {...material.delta, schema_version: 2}})]) {
      const rendered = await page.evaluate(text => { const p = document.getElementById('probe'); p.innerHTML = sgInputExplanation(text); return p.innerText; }, text);
      assert.equal(rendered, text);
    }
    const extended = structuredClone(material);
    extended.delta.future_restriction = 'Only after final owner approval';
    extended.plan = {steps: ['Edit the research skill', 'Check a synthetic report'], revision: 2, limitations: ['Do not change email guidance']};
    extended.failure_evidence = {message: 'Check failed <img src=x onerror="window.injected=true">', status: 'failed'};
    extended.delta.change_direction += '\n' + 'Long instruction. '.repeat(100) + 'FINAL RULE';
    const rendered = await page.evaluate(material => {
      const p = document.getElementById('probe'); p.innerHTML = sgInputExplanation('Review\n\nBound material:\n' + JSON.stringify(material));
      return {text: p.innerText, image: !!p.querySelector('img'), injected: !!window.injected};
    }, extended);
    for (const required of ['FINAL RULE', 'Only after final owner approval', 'Do not change email guidance', 'Check failed <img']) assert.ok(rendered.text.includes(required));
    assert.equal(rendered.image, false); assert.equal(rendered.injected, false);

    await page.locator('#inline [data-choice="accept"]').click();
    await page.locator('#inline [data-sg="respond"]').click();
    await page.waitForFunction(() => posts.length === 1);
    const post = await page.evaluate(() => posts[0]);
    assert.equal(post.url, '/root/signoffs/' + signoff.id + '/respond');
    assert.equal(post.body.payload_hash, signoff.payload_hash);
    assert.equal(post.body.proposal_revision, signoff.proposal_revision);
    assert.equal(post.body.expected_status_revision, signoff.status_revision);
    assert.deepEqual(post.body.selected_choices, ['accept']);
    assert.deepEqual(errors, []);
    console.log('PASS readable saved material in chat/pane, exact record and approval binding, mobile layout, safe fallback and escaping');
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
