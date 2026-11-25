'use strict';
// Real-browser check of the shared click-to-edit field in the Story overview and the Customer panel: plain text at rest,
// editable on click, Save / Discard only while the value differs, and aligned paddings.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const root = path.join(__dirname, '..');
// The Customer field helpers of tasks.js, which cannot load whole outside the dashboard.
const tasksSource = fs.readFileSync(path.join(root, 'js/tasks.js'), 'utf8');
const customerSource = tasksSource.slice(tasksSource.indexOf('const CUSTOMER_PROFILE_FIELDS'), tasksSource.indexOf('// `overviewOwned`'));

test('Story overview is a click-to-edit field with Save and Discard only while changed', {skip: !available && 'Local Playwright Chromium is unavailable'}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage({viewport: {width: 700, height: 900}});
    const errors = []; page.on('pageerror', e => errors.push(String(e)));
    await page.setContent('<main id="host" class="record-panel" style="width:400px;padding:10px"></main>');
    for (const file of ['app.css', 'css/tasks.css', 'css/record-panel.css', 'css/editable-field.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.evaluate(() => {
      window.esc = v => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
      window.saves = []; window.revision = 1;
      window.workCall = async (project, name, args) => {
        if (name === 'tasks_update') { window.saves.push(args.changes); window.revision++; return {task: {id: 't1', revision: window.revision, ...args.changes}}; }
        return {items: [], task: {id: 't1', revision: window.revision}, customer_states: []};
      };
      window.uiFailure = e => String((e && e.message) || e); window.newRequestId = () => 'k' + Math.random();
      window.taskTypeIcon = () => ''; window.TASK_TYPE_LABELS = {task: 'Task'};
    });
    await page.addScriptTag({path: path.join(root, 'js/editable-field.js')});
    await page.addScriptTag({content: customerSource});
    await page.evaluate(() => { window.taskLinksMount = () => ({refresh() {}, dispose() {}}); window.taskLinkOpen = () => {}; });
    await page.addScriptTag({path: path.join(root, 'js/record-panel.js')});
    const info = await page.evaluate(() => {
      const host = document.querySelector('#host');
      window.ctx = {task: {id: 't1', revision: 1, overview: 'First line\nSecond line\nThird line', task_type: 'story'}, project: 'p', current: () => true, apply(f) { Object.assign(this.task, f); }, openTask() {}};
      window.part = recordStoryMount(host, window.ctx);
      const field = host.querySelector('[data-story-overview]'), actions = host.querySelector('.ef-actions');
      const cs = getComputedStyle(field), head = host.querySelector('.record-section-head h4').getBoundingClientRect(), box = field.getBoundingClientRect();
      return {readonly: field.readOnly, hidden: actions.hidden, fit: field.scrollHeight <= field.clientHeight + 1, pad: [cs.paddingTop, cs.paddingLeft, cs.paddingRight], border: [cs.borderLeftWidth, cs.borderTopWidth], resize: cs.resize, overflow: cs.overflow,
        left: Math.round(box.left), headLeft: Math.round(head.left), height: Math.round(box.height)};
    });
    assert.deepEqual(errors, []);
    assert.equal(info.readonly, true); assert.equal(info.hidden, true); assert.equal(info.fit, true);
    assert.deepEqual(info.pad, ['6px', '12px', '12px']); assert.deepEqual(info.border, ['2px', '0px']);
    assert.equal(info.resize, 'none'); assert.equal(info.overflow, 'hidden'); assert.equal(info.left, info.headLeft);
    const field = page.locator('[data-story-overview]'), actions = page.locator('.record-story-overview .ef-actions');
    await field.click();
    assert.equal(await field.evaluate(e => e.readOnly), false);
    assert.equal(await actions.isHidden(), true);
    await page.keyboard.press('End'); await page.keyboard.type(' more\nand a fourth line');
    assert.equal(await actions.isVisible(), true);
    const grown = await field.evaluate(e => e.scrollHeight <= e.clientHeight + 1);
    assert.equal(grown, true);
    const alignment = await page.evaluate(() => { const f = document.querySelector('[data-story-overview]').getBoundingClientRect(), b = document.querySelector('[data-story-save]').getBoundingClientRect(), a = document.querySelector('.record-story-overview .ef-actions'); return {fieldRight: Math.round(f.right), saveRight: Math.round(b.right), minHeight: b.height, gap: getComputedStyle(a).columnGap, mt: getComputedStyle(a).marginTop}; });
    assert.equal(alignment.saveRight, alignment.fieldRight); assert.equal(alignment.gap, '6px'); assert.equal(alignment.mt, '8px');
    // Editing back to the saved text hides the buttons again.
    await field.fill('First line\nSecond line\nThird line');
    assert.equal(await actions.isHidden(), true);
    await field.fill('Changed');
    assert.equal(await actions.isVisible(), true);
    await page.keyboard.press('Escape');
    assert.equal(await field.inputValue(), 'First line\nSecond line\nThird line');
    assert.equal(await field.evaluate(e => e.readOnly), true); assert.equal(await actions.isHidden(), true);
    await field.click(); await field.fill('Saved text');
    await page.locator('[data-story-save]').click();
    await page.waitForFunction(() => window.saves.length === 1);
    assert.deepEqual(await page.evaluate(() => window.saves[0]), {overview: 'Saved text'});
    assert.equal(await actions.isHidden(), true); assert.equal(await field.evaluate(e => e.readOnly), true);
    assert.equal(await page.evaluate(() => window.part.dirty()), false);
    await field.focus(); await field.fill('x'); await page.locator('[data-story-discard]').click();
    assert.equal(await field.inputValue(), 'Saved text'); assert.equal(await actions.isHidden(), true);
  } finally { await browser.close(); }
});

test('Customer panel fields share the field and one Save / Discard pair', {skip: !available && 'Local Playwright Chromium is unavailable'}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage({viewport: {width: 700, height: 900}});
    const errors = []; page.on('pageerror', e => errors.push(String(e)));
    await page.setContent('<main id="host" class="record-panel" style="width:400px;padding:10px"></main>');
    for (const file of ['app.css', 'css/tasks.css', 'css/record-panel.css', 'css/editable-field.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.evaluate(() => {
      window.esc = v => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
      window.saves = []; window.revision = 1;
      window.workCall = async (project, name, args) => {
        if (name === 'tasks_update') { window.saves.push(args.changes); window.revision++; return {task: {id: 't1', revision: window.revision, ...args.changes}}; }
        return {items: [], task: {id: 't1', revision: window.revision}, customer_states: []};
      };
      window.uiFailure = e => String((e && e.message) || e); window.newRequestId = () => 'k' + Math.random();
      window.taskTypeIcon = () => ''; window.TASK_TYPE_LABELS = {task: 'Task'};
    });
    await page.addScriptTag({path: path.join(root, 'js/editable-field.js')});
    await page.addScriptTag({content: customerSource});
    await page.evaluate(() => { window.taskLinksMount = () => ({refresh() {}, dispose() {}}); window.taskLinkOpen = () => {}; });
    await page.addScriptTag({path: path.join(root, 'js/record-panel.js')});
    await page.evaluate(() => {
      window.ctx = {task: {id: 't1', revision: 1, title: 'Acme', overview: 'Summary', task_type: 'customer', customer_kind: 'company', customer_profile: {company: 'Acme Inc'}}, project: 'p', current: () => true, apply(f) { Object.assign(this.task, f); }, openTask() {}};
      window.part = recordCustomerMount(document.querySelector('#host'), window.ctx);
    });
    const actions = page.locator('[data-customer-save]').locator('..');
    assert.equal(await actions.isHidden(), true);
    const rest = await page.evaluate(() => [...document.querySelectorAll('#host .editable-field')].map(e => [e.readOnly, e.getBoundingClientRect().left]));
    assert.ok(rest.length >= 8); assert.ok(rest.every(([ro]) => ro));
    await page.locator('[data-customer-name]').click(); await page.locator('[data-customer-name]').fill('Acme Corp');
    assert.equal(await actions.isVisible(), true);
    await page.locator('[data-customer-save]').click();
    await page.waitForFunction(() => window.saves.length === 1);
    assert.deepEqual(await page.evaluate(() => window.saves[0]), {title: 'Acme Corp'});
    assert.equal(await actions.isHidden(), true);
    await page.locator('[data-customer-summary]').click(); await page.locator('[data-customer-summary]').fill('Other');
    await page.locator('[data-customer-discard]').click();
    assert.equal(await page.locator('[data-customer-summary]').inputValue(), 'Summary'); assert.equal(await actions.isHidden(), true);
    const customer = await page.evaluate(() => {
      const host = document.querySelector('#host'), b = e => e.getBoundingClientRect(), style = e => { const c = getComputedStyle(e); return [c.textTransform, c.fontSize, c.fontWeight, c.letterSpacing].join('|'); };
      const labels = [...host.querySelectorAll('.ef-stack label, fieldset label')].filter(l => l.offsetParent);
      const kind = host.querySelector('[data-customer-kind]'), company = host.querySelector('[data-customer-profile="company"]');
      const notes = host.querySelector('[data-customer-notes]'), summaryLabel = host.querySelector('[data-customer-summary]').closest('label');
      const button = host.querySelector('[data-customer-save-state]'), state = host.querySelector('[data-customer-state]');
      const heading = host.querySelector('h4.ef-section-title'), nameLabel = host.querySelector('[data-customer-name]').closest('label');
      const fam = [...host.querySelectorAll('.editable-field, .ef-control')].map(e => getComputedStyle(e).fontFamily);
      const sansValue = getComputedStyle(document.documentElement).getPropertyValue('--sans').trim();
      return {count: labels.length, styles: [...new Set(labels.map(style))], kindLabelTop: b(kind.closest('label')).top, companyLabelTop: b(company.closest('label')).top, kindTop: b(kind).top, companyTop: b(company).top,
        notesToSummary: b(summaryLabel).top - b(notes).bottom, buttonGap: b(button).top - b(state).bottom, buttonRight: b(button).right, stateRight: b(state).right, headingGap: b(nameLabel).top - b(heading).bottom,
        fonts: [...new Set(fam)], sansValue};
    });
    assert.ok(customer.count >= 9); assert.equal(customer.styles.length, 1, customer.styles.join(' / '));
    assert.equal(customer.kindLabelTop, customer.companyLabelTop); assert.equal(customer.kindTop, customer.companyTop);
    assert.equal(customer.notesToSummary, 12); assert.equal(customer.buttonGap, 8); assert.equal(customer.buttonRight, customer.stateRight); assert.equal(customer.headingGap, 12);
    assert.deepEqual(customer.fonts, [customer.sansValue]);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('Task detail: every field shares the look and the approved gaps hold at rest and while changed', {skip: !available && 'Local Playwright Chromium is unavailable'}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const page = await browser.newPage({viewport: {width: 900, height: 1400}});
    const errors = []; page.on('pageerror', e => errors.push(String(e)));
    await page.setContent('<main id="host" class="record-panel" style="width:620px;padding:20px"></main>');
    for (const file of ['app.css', 'css/global-tasks.css', 'css/tasks.css', 'css/editable-field.css', 'css/record-panel.css', 'css/record-workspace.css']) await page.addStyleTag({path: path.join(root, file)});
    await page.evaluate(() => {
      window.esc = v => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;');
      window.workCall = async () => ({items: [], tags: []}); window.uiFailure = e => String((e && e.message) || e); window.newRequestId = () => 'k'; window.toast = () => {};
      window.state = {project: 'p', projects: [{slug: 'p', id: 'p1', name: 'P'}], tasks: [], myTasks: []};
      window.TASK_TYPE_LABELS = {task: 'Task', story: 'Story', customer: 'Customer', email: 'Email'};
      for (const name of ['NAV_PROGRESS_ICON', 'AICONS', 'ICO_UPLOAD', 'ls']) window[name] = name === name.toUpperCase() ? '' : () => '';
    });
    for (const file of ['js/task-snooze.js', 'js/task-state.js', 'js/tags.js', 'js/editable-field.js', 'js/tasks.js']) await page.addScriptTag({path: path.join(root, file)});
    await page.evaluate(async () => {
      window.taskProgressMount = () => ({dispose() {}}); window.workRelatedChips = () => {}; window.refreshTasks = () => {}; window.taskLinksMount = () => ({refresh() {}, dispose() {}});
      await tDetail({id: 't1', title: 'A task title that is fairly long', status: 'open', revision: 1, project: {slug: 'p', id: 'p1', name: 'P'}, project_id: 'p1', task_type: 'task', overview: 'Some overview',
        tags: [], created: '2026-01-01T00:00', updated: '2026-01-01T00:00', by: 'x', children: [], blocked_by: [], blocks: []}, {host: document.querySelector('#host')});
    });
    const measure = () => page.evaluate(() => {
      const q = s => document.querySelector(s), box = e => e.getBoundingClientRect();
      const field = q('[data-task-overview]'), label = field.closest('label'), actions = q('.task-type-detail > .ef-actions'), next = q('.tdetail .flds');
      const range = document.createRange(); range.selectNodeContents([...label.childNodes].find(n => n.nodeType === 3 && n.textContent.trim()));
      const last = actions.hidden ? box(field).bottom : box(actions).bottom;
      return {labelGap: box(field).top - range.getBoundingClientRect().bottom, labelLeft: range.getBoundingClientRect().left, fieldLeft: box(field).left,
        fieldToActions: actions.hidden ? null : box(actions).top - box(field).bottom, nextGap: box(next).top - last,
        saveRight: actions.hidden ? null : box(actions.querySelector('.ef-save')).right, fieldRight: box(field).right, contentRight: box(q('.tdetail')).right};
    });
    // Every editable control looks the same: no box, a 2px left edge, the same padding and font; labels use the section heading style.
    const looks = await page.evaluate(() => ['[data-task-type]', '#td-due', '.tag-picker-input', '.task-child-add input', '[data-task-overview]'].map(s => {
      const el = document.querySelector(s), c = getComputedStyle(el);
      return {s, border: [c.borderTopWidth, c.borderRightWidth, c.borderBottomWidth, c.borderLeftWidth, c.borderLeftStyle], radius: c.borderRadius, bg: c.backgroundColor, padTop: c.paddingTop, padBottom: c.paddingBottom, padLeft: c.paddingLeft, font: c.fontSize + ' ' + c.fontFamily + ' ' + c.fontWeight};
    }));
    const [first, ...others] = looks;
    for (const look of others) { const {s, ...rest2} = look; const {s: _, ...rest1} = first; assert.deepEqual(rest2, rest1, s); }
    assert.deepEqual(first.border, ['0px', '0px', '0px', '2px', 'solid']); assert.equal(first.padTop, '6px'); assert.equal(first.padLeft, '12px');
    const rest = await measure();
    await page.locator('[data-task-overview]').click(); await page.locator('[data-task-overview]').fill('changed');
    const changed = await measure();
    assert.ok(Math.abs(rest.labelGap - 6) < 0.5, 'label to field 6px, got ' + rest.labelGap);
    assert.equal(rest.labelLeft, rest.fieldLeft);
    assert.equal(changed.fieldToActions, 8); assert.equal(changed.saveRight, changed.fieldRight);
    assert.equal(rest.nextGap, changed.nextGap, 'next block sits the same distance below the last visible element');
    assert.equal(changed.nextGap, 14);
    // The title's Save / Discard row ends at the same right edge as the Overview's, though the snooze icon sits beside the title.
    await page.locator('.dtitle').click(); await page.locator('.dtitle').fill('New title');
    const title = await page.evaluate(() => { const b = e => e.getBoundingClientRect(), t = document.querySelector('.dtitle'), a = document.querySelector('.dtitle-wrap .ef-actions');
      return {saveRight: b(a.querySelector('.ef-save')).right, fieldTop: b(t).bottom, actionsTop: b(a).top, titleRight: b(t).right, snoozeLeft: b(document.querySelector('#td-snooze')).left}; });
    assert.equal(title.saveRight, changed.saveRight); assert.equal(title.actionsTop - title.fieldTop, 8); assert.ok(title.titleRight < title.snoozeLeft);
    const fonts = await page.evaluate(() => ['[data-task-type]', '#td-due', '.tag-picker-input', '.task-child-add input', '[data-task-overview]', '.dtitle'].map(sel => {
      const c = getComputedStyle(document.querySelector(sel)); return {sel, family: c.fontFamily, spacing: c.letterSpacing, transform: c.textTransform};
    }));
    const sans = await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--sans').trim());
    for (const f of fonts) { assert.equal(f.family, sans, f.sel); assert.ok(!/mono/i.test(f.family), f.sel); assert.equal(f.spacing, 'normal', f.sel); assert.equal(f.transform, 'none', f.sel); }
    const rows = await page.evaluate(() => ['#td-due', '.tag-picker-input', '.task-child-add input'].map(sel => { const r = document.querySelector(sel).getBoundingClientRect(); return {sel, left: r.left, height: r.height}; }));
    assert.equal(new Set(rows.map(r => r.left)).size, 1, JSON.stringify(rows)); assert.equal(new Set(rows.map(r => r.height)).size, 1, JSON.stringify(rows));
    const labels = await page.evaluate(() => [document.querySelector('[data-task-overview]').closest('label'), document.querySelector('.tdetail .k')].map(e => { const c = getComputedStyle(e); return [c.textTransform, c.fontSize, c.letterSpacing, c.fontWeight]; }));
    assert.deepEqual(labels[0], labels[1]);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});
