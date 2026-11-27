'use strict';
// Real-browser check of a record type in the Customer and Story dashboards a record opens in from Explorer, a CRM row, a
// chat or Focus reference and the Task list: the Task detail's own Type editor shows the type's label and its values,
// editable and saved; with the type off the record shows as its base type and its leftover values read-only by field id;
// with no type active and none on the record the dashboard is as before.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
let chromium;
try { ({chromium} = require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable = process.env.CHROME_PATH || chromium?.executablePath();
const available = !!chromium && fs.existsSync(executable);
const skip = !available && 'Local Playwright Chromium is unavailable';
const root = path.join(__dirname, '..');
// The record type, Customer field and Type editor code of tasks.js, which cannot load whole outside the dashboard.
const tasksSource = fs.readFileSync(path.join(root, 'js/tasks.js'), 'utf8');
const between = (from, to) => { const a = tasksSource.indexOf(from), b = tasksSource.indexOf(to, a); assert.ok(a >= 0 && b > a, from); return tasksSource.slice(a, b); };
const typeSource = between('// Record types extensions add on top', 'function taskEmailOpen(');
const CONNECTION = {id: 'connection', label: 'Connection', plural: 'Connections', based_on: 'customer', extension: 'network', fields: [
  {id: 'met_at', label: 'Where we met', type: 'string'}, {id: 'closeness', label: 'Closeness', type: 'enum', values: ['close', 'friendly', 'distant']},
  {id: 'last_contact', label: 'Last contact', type: 'date'}]};
const VALUES = {met_at: 'Lisbon', closeness: 'friendly', last_contact: '2026-11-20'};

async function open(browser, types, task, mount = 'recordCustomerMount') {
  const page = await browser.newPage({viewport: {width: 700, height: 1200}});
  const errors = []; page.on('pageerror', e => errors.push(String(e)));
  await page.setContent('<main id="host" class="record-panel" style="width:460px;padding:10px"></main>');
  for (const file of ['app.css', 'css/tasks.css', 'css/record-panel.css', 'css/editable-field.css']) await page.addStyleTag({path: path.join(root, file)});
  await page.evaluate(types => {
    window.esc = v => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
    window.served = types; window.saves = []; window.revision = 1;
    window.workCall = async (project, name, args) => {
      if (name === 'tasks_config_get') return {record_types: window.served, customer_states: []};
      if (name === 'tasks_update') { window.saves.push(args.changes); window.revision++; return {task: {id: 't1', revision: window.revision, ...args.changes}}; }
      return {items: [], task: {id: 't1', revision: window.revision}};
    };
    window.uiReads = {read: (key, loader) => loader()};
    window.uiFailure = e => String((e && e.message) || e); window.newRequestId = () => 'k' + Math.random();
    window.taskTypeIcon = () => ''; window.TASK_TYPE_LABELS = {task: 'Task', story: 'Story', customer: 'Customer', email: 'Email'};
    window.modal = () => {};
  }, types);
  await page.addScriptTag({path: path.join(root, 'js/editable-field.js')});
  await page.addScriptTag({content: typeSource});
  await page.evaluate(() => { window.taskLinksMount = () => ({refresh() {}, dispose() {}}); window.taskLinkOpen = () => {}; });
  await page.addScriptTag({path: path.join(root, 'js/record-panel.js')});
  // The page loaded the type list at start, as main.js does; the dashboard is then opened as the record workspace opens it.
  await page.evaluate(async ({task, mount}) => {
    await taskRecordTypesLoad();
    window.labels = [];
    window.ctx = {task: {id: 't1', revision: 1, title: 'Ayla Demir', overview: '', ...task}, project: null, current: () => true,
      apply(f) { Object.assign(this.task, f); }, openTask() {}, typeEditor: true, typeShown() { window.labels.push(taskTypeLabel(window.ctx.task)); }};
    window.part = window[mount](document.querySelector('#host'), window.ctx);
    await new Promise(resolve => setTimeout(resolve, 0));
  }, {task, mount});
  return {page, errors};
}
const shown = page => page.evaluate(() => {
  const section = document.querySelector('.record-type-section'), select = section?.querySelector('[data-task-type]');
  const box = [...(section?.querySelectorAll('[data-task-record]') || [])].find(item => !item.hidden);
  return {hidden: !section || section.hidden, options: select ? [...select.options].map(o => o.textContent) : [], chosen: select?.selectedOptions[0]?.textContent,
    legend: box?.querySelector('legend')?.textContent, values: box ? Object.fromEntries([...box.querySelectorAll('[data-record-field]')].map(i => [i.dataset.recordField, i.value])) : null,
    leftover: [...document.querySelectorAll('.record-type-leftover dt')].map((dt, i) => [dt.textContent, document.querySelectorAll('.record-type-leftover dd')[i].textContent]),
    overviewShown: !!section?.querySelector('[data-task-overview-field]:not([hidden])'), customerShown: !!section?.querySelector('[data-task-customer]:not([hidden])')};
});

test('a saved Connection opened in the Customer dashboard shows its label and values, editable', {skip}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await open(browser, [CONNECTION], {task_type: 'customer', record_type: 'connection', fields: {...VALUES, other: 7}});
    assert.deepEqual(errors, []);
    const view = await shown(page);
    assert.equal(view.hidden, false);
    // Only Customer and the types on it: the base never changes here, and nothing of the Customer form is drawn twice.
    assert.deepEqual(view.options, ['Customer', 'Connection']);
    assert.equal(view.chosen, 'Connection'); assert.equal(view.legend, 'Connection details');
    assert.deepEqual(view.values, VALUES);
    assert.equal(view.overviewShown, false); assert.equal(view.customerShown, false);
    assert.deepEqual(view.leftover, [['other', '7']]);
    assert.deepEqual(await page.evaluate(() => window.labels), ['Connection']);
    assert.equal(await page.locator('[data-record-type-save]').locator('..').isHidden(), true);
    await page.selectOption('[data-record-field="closeness"]', 'close');
    assert.equal(await page.evaluate(() => window.part.dirty()), true);
    await page.locator('[data-record-type-save]').click();
    await page.waitForFunction(() => window.saves.length === 1);
    assert.deepEqual(await page.evaluate(() => window.saves[0]), {fields: {...VALUES, other: 7, closeness: 'close'}});
    assert.equal(await page.evaluate(() => window.part.dirty()), false);
    assert.deepEqual((await shown(page)).values, {...VALUES, closeness: 'close'});
    // Back to Customer clears the type; the values stay with the record.
    await page.selectOption('.record-type-section [data-task-type]', 'customer');
    await page.locator('[data-record-type-save]').click();
    await page.waitForFunction(() => window.saves.length === 2);
    assert.deepEqual(await page.evaluate(() => window.saves[1]), {record_type: null});
    assert.deepEqual(await page.evaluate(() => window.labels.at(-1)), 'Customer');
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('with the type off the record shows as a Customer with its leftover values read-only', {skip}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    const {page, errors} = await open(browser, [], {task_type: 'customer', record_type: 'connection', fields: VALUES});
    const view = await shown(page);
    assert.equal(view.hidden, false);
    // A select with the base type as its only choice is not drawn.
    assert.deepEqual(view.options, []); assert.equal(view.legend, undefined);
    assert.deepEqual(view.leftover, Object.entries(VALUES));
    assert.equal(await page.locator('.record-type-leftover input, .record-type-leftover select').count(), 0);
    assert.equal(await page.evaluate(() => window.part.dirty()), false);
    assert.deepEqual(await page.evaluate(() => window.labels), ['Customer']);
    assert.deepEqual(errors, []);
  } finally { await browser.close(); }
});

test('a type switched on after the page loaded shows on the next dashboard; none active and none on the record shows nothing new', {skip}, async () => {
  const browser = await chromium.launch({headless: true, executablePath: executable});
  try {
    // The page read an empty list at start; the dashboard's own read finds the type.
    const {page, errors} = await open(browser, [], {task_type: 'customer', record_type: 'connection', fields: VALUES});
    await page.evaluate(async types => { window.served = types; window.part.dispose(); window.part = recordCustomerMount(document.querySelector('#host'), window.ctx); await new Promise(r => setTimeout(r, 0)); }, [CONNECTION]);
    const view = await shown(page);
    assert.equal(view.chosen, 'Connection'); assert.deepEqual(view.values, VALUES); assert.deepEqual(view.leftover, []);
    assert.equal(await page.evaluate(() => window.labels.at(-1)), 'Connection', await page.evaluate(() => JSON.stringify(window.labels)));
    // A plain Customer, and a Story, with no type active: no section at all.
    const plain = await open(browser, [], {task_type: 'customer', fields: {priority: 2}});
    assert.equal((await shown(plain.page)).hidden, true);
    assert.equal(await plain.page.locator('.record-type-section:not([hidden])').count(), 0);
    const story = await open(browser, [CONNECTION], {task_type: 'story'}, 'recordStoryMount');
    assert.equal((await shown(story.page)).hidden, true);
    // A Story whose base has a type: the Story dashboard offers it the same way.
    const project = {...CONNECTION, id: 'milestone', label: 'Milestone', based_on: 'story', fields: [{id: 'when', label: 'When', type: 'date'}]};
    const typed = await open(browser, [CONNECTION, project], {task_type: 'story', record_type: 'milestone', fields: {when: '2026-12-01'}}, 'recordStoryMount');
    const storyView = await shown(typed.page);
    assert.deepEqual(storyView.options, ['Story', 'Milestone']); assert.deepEqual(storyView.values, {when: '2026-12-01'});
    assert.deepEqual([...errors, ...plain.errors, ...story.errors, ...typed.errors], []);
  } finally { await browser.close(); }
});
