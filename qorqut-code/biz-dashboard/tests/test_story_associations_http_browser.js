// The real HTTP-to-dashboard acceptance check for a Story's recursive associated Actions and Signoffs.
//
// The actual composed owner application serves the actual combined dashboard. No product response is intercepted,
// stubbed or fabricated. This driver records the real tasks_associated_records requests and their real HTTP responses,
// then asserts that the same Story record renders those descendant-associated records exactly once. The only routing
// hook blocks requests that leave the synthetic origin; it never answers a product read.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const {chromium} = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

const base = process.env.STORY_ASSOCIATIONS_BASE;
const fixture = JSON.parse(fs.readFileSync(process.env.STORY_ASSOCIATIONS_FIXTURE, 'utf8'));
const origin = new URL(base).origin;
const story = fixture.story_associations;

const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds));
const decode = body => { try { return JSON.parse(body || 'null'); } catch (_) { return null; } };
const waitFor = async (check, what) => {
  for (let attempt = 0; attempt < 400; attempt++) { if (await check()) return; await delay(25); }
  throw new Error('Timed out waiting for ' + what);
};

(async () => {
  const browser = await chromium.launch({headless: true, args: ['--no-sandbox']});
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 950}});
    page.setDefaultTimeout(30000);
    const errors = [], notes = [], calls = [], associated = [];
    page.on('pageerror', error => errors.push('pageerror: ' + error.message));
    page.on('requestfailed', request => {
      const url = request.url();
      if (new URL(url).origin !== origin) return notes.push('blocked external request: ' + url);
      if (String(request.failure()?.errorText || '').includes('ERR_ABORTED')) return notes.push('cancelled request: ' + url);
      errors.push('requestfailed: ' + url + ' (' + (request.failure()?.errorText || '') + ')');
    });
    page.on('request', request => {
      const url = new URL(request.url());
      if (url.origin !== origin || request.method() !== 'POST') return;
      calls.push({path: url.pathname, body: decode(request.postData())});
    });
    page.on('response', async response => {
      const url = new URL(response.url());
      if (url.origin !== origin) return;
      if (url.pathname.endsWith('/operations/tasks_associated_records')) {
        let body = null;
        try { body = await response.json(); } catch (_) { /* the assertions report a missing body */ }
        associated.push({path: url.pathname, request: decode(response.request().postData()),
          status: response.status(), body});
      }
      if (response.status() >= 400) {
        // The agent pane's own best-effort workspace declaration falls back to its built-in views; it is not a
        // product response used by this check.
        if (url.pathname.endsWith('/operations/workspace_types') && response.status() === 403) {
          notes.push('agent-pane workspace declaration fell back to its built-in views (workspace_types 403)');
        } else {
          errors.push('http ' + response.status() + ': ' + url.pathname);
        }
      }
    });
    await page.route('**/*', route => new URL(route.request().url()).origin === origin
      ? route.continue() : route.abort());

    const pane = () => page.locator('.agfocus-dock .rw-record:not([hidden])').first();
    const focusCard = label => page.locator('.agfocus-items .agfocus-card')
      .filter({has: page.locator('.ttl', {hasText: label})}).first();
    const recordSection = title => pane().locator('.record-section')
      .filter({has: page.locator('h4', {hasText: new RegExp('^' + title + '\\b')})}).first();
    const associatedReads = type => associated.filter(read => read.request && read.request.record_type === type);
    const waitAssociated = async type => {
      for (let attempt = 0; attempt < 400; attempt++) {
        const successful = associatedReads(type).filter(read =>
          read.status === 200 && read.request.task_id === story.story.id);
        if (successful.length) return successful.at(-1);
        await delay(25);
      }
      const attempts = associatedReads(type);
      throw new Error('Timed out waiting for the Story ' + type + ' tasks_associated_records read; '
        + (attempts.length
          ? 'the last attempt named task ' + JSON.stringify(attempts.at(-1).request.task_id)
            + ' and returned HTTP ' + attempts.at(-1).status + ' ' + JSON.stringify(attempts.at(-1).body)
          : 'the dashboard made no such request (' + associated.length + ' associated-record requests observed)'));
    };
    const checkRead = (read, type, expected, excluded, sourceTaskIds) => {
      assert.equal(read.path.endsWith('/operations/tasks_associated_records'), true,
        'the read is the new tasks_associated_records operation');
      assert.equal(read.request.task_id, story.story.id, 'the read names the Story');
      assert.equal(read.request.record_type, type, 'the read names the requested record type');
      assert.notEqual(read.request.include_descendants, false, 'the read includes the Story descendants');
      assert.ok(read.body && read.body.status === 'ok' && read.body.data, 'the real operation response body');
      const items = read.body.data.items;
      assert.ok(Array.isArray(items), 'tasks_associated_records returns an items array');
      const matching = items.filter(item => item.type === type && item.id === expected.id);
      assert.equal(matching.length, 1, 'the ' + type + ' is associated exactly once (found ' + matching.length + ')');
      assert.equal(matching[0].label, expected.label, 'the response keeps the canonical record label');
      assert.ok(Array.isArray(matching[0].task_ids) && matching[0].task_ids.length > 0,
        'the associated record names its source Tasks');
      for (const identity of sourceTaskIds) {
        assert.ok(matching[0].task_ids.includes(identity), 'task_ids includes the descendant ' + identity);
      }
      if (excluded) {
        assert.equal(items.filter(item => item.id === excluded.id).length, 0,
          'the unrelated ' + type + ' is excluded from the Story');
      }
      assert.equal(items.filter(item => item.type !== type).length, 0, 'record_type filters the response');
    };

    // Open the actual Focus workspace on the synthetic Project, then navigate to the Story through Project work.
    await page.goto(base + '/#/global/agents');
    await page.locator('[data-focus-add]').waitFor();
    await page.locator('[data-focus-add]').click();
    const add = page.locator('.modal');
    await add.waitFor();
    await add.locator('[name="kind"][value="project"]').check();
    await add.locator('[name="project"]').selectOption(story.project_id);
    await add.getByRole('button', {name: 'Add'}).click();
    await add.waitFor({state: 'detached'});
    const harborCard = focusCard(story.project_name);
    await harborCard.waitFor();
    await harborCard.locator('.ttl').click();
    await pane().waitFor();
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.rw-project-name').waitFor();
    await pane().locator('[data-project-section="work"]').click();
    const storyRow = pane().locator('.rw-project-row')
      .filter({has: page.locator('.rw-project-open', {hasText: story.story.title})}).first();
    await storyRow.waitFor();
    await storyRow.locator('.rw-project-open').click();
    await pane().waitFor();

    // The Story opens on its own conversation; the dashboard tab then reads and renders the associated records.
    await pane().locator('.rw-chat').getByText(story.story.message, {exact: true}).first().waitFor();
    await pane().locator('[data-rw-tab="dashboard"]').click();
    await pane().locator('.record-overview').waitFor();
    assert.match(await pane().locator('.record-overview').textContent(), /Harbor rework story overview/);

    const actionRead = await waitAssociated('action');
    const signoffRead = await waitAssociated('signoff');
    checkRead(actionRead, 'action', story.action, story.unrelated_action, [story.child.id, story.grandchild.id]);
    checkRead(signoffRead, 'signoff', story.signoff, null, [story.child.id]);

    // The Story's own Actions/Signoffs sections render each descendant-associated record exactly once, and the
    // unrelated Task's Action appears nowhere in this record.
    const actionsSection = recordSection('Actions');
    const signoffsSection = recordSection('Signoffs');
    const actionRow = actionsSection.locator('.record-link-open').filter({hasText: story.action.label});
    await actionRow.waitFor();
    assert.equal(await actionsSection.locator('.record-link-open').filter({hasText: story.action.label}).count(), 1,
      'the descendant-associated Action is rendered exactly once');
    assert.equal(await signoffsSection.locator('.record-link-open').filter({hasText: story.signoff.label}).count(), 1,
      'the descendant-associated Signoff is rendered exactly once');
    assert.equal((await actionsSection.textContent()).includes(story.unrelated_action.label), false,
      'the unrelated Action is not in the Story Actions section');
    assert.equal((await actionsSection.textContent()).includes(story.signoff.label), false,
      'the Signoff is not in the Story Actions section');
    assert.equal((await signoffsSection.textContent()).includes(story.action.label), false,
      'the Action is not in the Story Signoffs section');
    assert.equal((await pane().textContent()).includes(story.unrelated_action.label), false,
      'the unrelated Action is nowhere in the Story record');

    // Opening the child from the Story's own child list retains the child's conversation and Back navigation.
    const childTarget = pane().getByText(story.child.title, {exact: true}).first();
    await childTarget.waitFor();
    await childTarget.click();
    await waitFor(async () => (await pane().locator('.rw-title').textContent().catch(() => null)) === story.child.title,
      'the child record');
    assert.equal(await pane().locator('[data-rw-tab="chat"]').getAttribute('aria-selected'), 'true',
      'the child opens on its own conversation');
    await pane().locator('.rw-chat').getByText(story.child.message, {exact: true}).first().waitFor();
    assert.equal(await pane().locator('.rw-chat').getByText(story.story.message, {exact: true}).count(), 0,
      'the child keeps its own conversation, not the parent Story conversation');
    assert.equal(await pane().locator('.rw-back').isVisible(), true, 'the child record keeps in-pane navigation');
    await pane().locator('.rw-back').click();
    await waitFor(async () => (await pane().locator('.rw-title').textContent().catch(() => null)) === story.story.title,
      'Back to the Story');
    await pane().locator('.record-overview').waitFor();

    assert.deepEqual(errors, [], 'the composed Story check made no failed same-origin product request');
    console.log('Story associated records qualified through the real dashboard and tasks_associated_records responses: '
      + JSON.stringify({requests: calls.length, associated: associated.map(read => ({
        task_id: read.request.task_id, record_type: read.request.record_type, status: read.status}))}));
  } finally {
    await browser.close();
  }
})().catch(error => { console.error(error.stack || String(error)); process.exit(1); });