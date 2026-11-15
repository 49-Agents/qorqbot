'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = name => fs.readFileSync(path.join(__dirname, '../js', name), 'utf8');
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const field = (path, after) => ({path, before: null, after});
const filing = (changes, extra = {}) => ({changes: [{kind: 'structured', resource: {resource_type: 'timeline_filing', resource_id: 'filing', label: 'Timeline filing'},
  fields: [field('item_type', 'task'), field('item_id', 't1'), field('item_business_id', null), field('item_title', 'Pay creators'),
    field('changes', changes), field('sources', extra.sources ?? [{quote: 'Pay one provider only.', ref: 'thread:1'}])]}]});

// The renderer is the real block of signoffs.js, run over the helpers it relies on.
function renderer() {
  const text = source('signoffs.js'), start = text.indexOf('const SG_TIMELINE_TYPES'), end = text.indexOf('// Returns {ok, html} for operations');
  assert.ok(start > 0 && end > start);
  const registered = {};
  const context = {esc, registerSignoffRenderer: (name, r) => { registered[name] = r; },
    sgFrozenFields: (preview, type) => { const c = preview.changes.filter(c => c.resource.resource_type === type); return c.length === 1 ? new Map(c[0].fields.map(f => [f.path, f.after])) : null; },
    sgNonblank: v => typeof v === 'string' && v.trim() !== '',
    sgUnavailable: what => ({ok: false, html: `unavailable ${what}`}),
    sgField: (label, html) => `<div data-field="${label}">${html}</div>`};
  vm.createContext(context);
  vm.runInContext(text.slice(start, end), context);
  return registered.timeline_file;
}

test('a timeline filing card shows the item, one line per change and the owner\'s words', () => {
  const r = renderer();
  const card = r.preview(filing([
    {action: 'add', kind: 'decision', text: 'One provider only.'},
    {action: 'remove', kind: 'fact', text: 'The hold stands.', reason: ''},
    {action: 'ask', text: 'Dollars or local currency?', blocking: true},
    {action: 'ask', text: 'Which month next?', blocking: false},
    {action: 'answer', kind: 'decision', text: 'Pay in dollars.', question: 'Which currency?'},
    {action: 'drop', question: 'Old question?'}]));
  assert.equal(card.ok, true);
  assert.deepEqual([...card.hiddenResources], ['timeline_filing']);
  assert.match(card.html, /Task: Pay creators/);
  const lines = [...card.html.matchAll(/<li>(.*?)<\/li>/g)].map(m => m[1]);
  assert.deepEqual(lines, ['Add decision: One provider only.', 'Remove: The hold stands.', 'Ask (blocking): Dollars or local currency?',
    'Ask: Which month next?', 'Answer Which currency?: Pay in dollars.', 'Drop: Old question?']);
  assert.match(card.html, /<blockquote class="sg-timeline-quote">Pay one provider only\.<\/blockquote>/);
  assert.equal(r.values({}), null);
});

test('a timeline filing card escapes text and refuses an incomplete preview', () => {
  const r = renderer();
  assert.match(r.preview(filing([{action: 'add', kind: 'fact', text: '<b>x</b>'}])).html, /Add fact: &lt;b&gt;x&lt;\/b&gt;/);
  assert.equal(r.preview(filing([{action: 'remove', kind: 'fact'}])).ok, false);
  assert.equal(r.preview(filing([{action: 'merge', text: 'x'}])).ok, false);
  assert.equal(r.preview(filing([])).ok, false);
  assert.equal(r.preview({changes: []}).ok, false);
  assert.doesNotMatch(r.preview(filing([{action: 'add', kind: 'fact', text: 'x'}], {sources: []})).html, /Your words/);
});

function queue(list, details = {}) {
  const handlers = {}, posts = [], opened = [];
  const host = {isConnected: true, hidden: false, innerHTML: '', addEventListener(t, fn) { handlers[t] = fn; }, removeEventListener() {}};
  const render = renderer();
  const context = {document: {addEventListener() {}, hidden: false, createElement: () => context.box}, window: {addEventListener() {},
      signoffMessageRenderer: () => render, focusExpansion: {async open(spec) { opened.push(spec); return {}; }}}, esc, setTimeout: () => 0, clearTimeout() {},
    api: async url => url.startsWith('/root/signoffs?') ? {items: list, next_cursor: null} : url.startsWith('/root/signoffs/') ? {signoff: details[url.split('/')[3]]} : {items: []},
    post: async (url, body) => { posts.push([url, body]); const fail = details[url.split('/')[3]]?.fail; if (fail) throw new Error(fail); return {}; },
    coreData: x => x, toast() {}, accessLost: () => false, newRequestId: () => 'key', URLSearchParams, Date, AbortController,
    remAttempts: new Map(), remVerifiedOwner: async () => 'owner', remindersOwnerDue: async () => []};
  vm.createContext(context);
  vm.runInContext(source('attention-queue.js') + '\nthis.queue=attentionQueue;', context);
  return {context, handlers, posts, opened, host};
}
const pending = (id, operation = 'timeline_file') => ({id, operation_name: operation, title: 'Filing ' + id, state: 'pending', proposal_revision: 1,
  payload_hash: 'h' + id, status_revision: 3, review_business_id: null, category: 'internal_change'});
const detail = (id, extra = {}) => ({...pending(id), preview: filing([{action: 'add', kind: 'fact', text: 'Fact ' + id}]), ...extra});
const settle = async (times = 6) => { for (let i = 0; i < times; i++) await new Promise(resolve => setImmediate(resolve)); };

test('two or more filings collapse into one group chip; one filing and other approvals stay as they are', async () => {
  const many = queue([pending('a'), pending('b'), pending('c', 'tasks_update')]);
  const dispose = many.context.queue.mountSignoffs(many.host);
  await settle();
  assert.match(many.host.innerHTML, /data-filings-group/);
  assert.match(many.host.innerHTML, /Filings to review · 2/);
  assert.match(many.host.innerHTML, /data-signoff-key="c"/);
  assert.doesNotMatch(many.host.innerHTML, /data-signoff-key="a"/);
  dispose();
  const one = queue([pending('a')]);
  const disposeOne = one.context.queue.mountSignoffs(one.host);
  await settle();
  assert.doesNotMatch(one.host.innerHTML, /data-filings-group/);
  assert.match(one.host.innerHTML, /data-signoff-key="a"/);
  disposeOne();
});

test('Approve all approves each filing in order through the ordinary route and reports each failure', async () => {
  const t = queue([pending('a'), pending('b'), pending('c')], {a: detail('a'), b: detail('b', {fail: 'Prepared targets changed'}), c: detail('c')});
  const dispose = t.context.queue.mountSignoffs(t.host);
  await settle();
  const group = {closest: sel => sel === '[data-filings-group]' ? group : null};
  t.handlers.click({target: group, currentTarget: t.host});
  await settle();
  assert.equal(t.opened[0].title, 'Filings to review');
  const box = {innerHTML: '', handlers: {}, addEventListener(type, fn) { this.handlers[type] = fn; }, removeEventListener() {}};
  t.context.box = box;
  const close = t.opened[0].mount({append() {}}, {});
  await settle();
  assert.match(box.innerHTML, /Approve all \(3\)/);
  assert.match(box.innerHTML, /Add fact: Fact a/);
  assert.deepEqual(t.posts, []);
  box.handlers.click({target: {closest: sel => sel === '[data-filings-approve-all]' ? {} : null}});
  await settle(12);
  assert.deepEqual(t.posts.map(p => p[0]), ['/root/signoffs/a/approve', '/root/signoffs/b/approve', '/root/signoffs/c/approve']);
  assert.deepEqual({...t.posts[0][1]}, {proposal_revision: 1, payload_hash: 'ha', expected_status_revision: 3, idempotency_key: 'key', feedback: null});
  assert.match(box.innerHTML, /2 approved, 1 not approved\./);
  assert.match(box.innerHTML, /Not approved: Prepared targets changed/);
  close(); dispose();
});
