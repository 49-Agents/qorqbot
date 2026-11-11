'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function setup(deliveries) {
  const created = [], opened = [], handlers = {}, posts = [], navigations = [], menus = [], toasts = [];
  const host = {id: '', dataset: {}, children: [], innerHTML: '', _identity: null,
    setAttribute() {}, append() {}, addEventListener(type, fn) { handlers[type] = fn; },
    replaceChildren() { this.children = []; this.innerHTML = ''; }};
  Object.defineProperty(host, 'innerHTML', {get() { return this._html || ''; }, set(value) { this._html = value; this.children = value ? [{}] : []; }});
  const pill = {closest: sel => sel === '.sg-toast' ? pill : null, getBoundingClientRect: () => ({left: 10, bottom: 40})};
  const context = {embeddedAgentPane: true, state: {scope: 'global', bizs: []},
    document: {querySelector: sel => sel === '#sg-toast-host' ? (host.created ? host : null) : null,
      createElement: tag => { if (!host.created) { host.created = true; return host; } const n = {tag, children: [], append(...c) { this.children.push(...c); }}; created.push(n); return n; }, body: {append() {}}},
    setInterval() {}, esc: s => String(s ?? ''), sjDateTime: s => s, post: async (url, body) => { posts.push([url, body]); return {}; },
    coreData: x => x, toast: m => toasts.push(m), api: async () => ({}), onChange() {},
    ctxMenu: (x, y, items) => menus.push({x, y, items}), sgCreateView() {}};
  context.window = context;
  context.focusExpansion = {async open(spec) { opened.push(spec); return {}; }};
  vm.createContext(context);
  vm.runInContext(fs.readFileSync(path.join(__dirname, '../js/signoff-attention.js'), 'utf8') +
    '\nthis.sub={sgAttention,sgAttentionPaint};', context);
  context.sgAttentionNavigate = async (slug, id) => { navigations.push([slug, id]); };
  context.sgAttentionRefresh = async () => {};
  context.sub.sgAttention.deliveries = deliveries; context.sub.sgAttention.businesses = []; context.sub.sgAttention.urgent = [];
  context.sub.sgAttentionPaint();
  return {handlers, posts, navigations, menus, toasts, host, pill, opened, created, context};
}
const item = (id, slug, name) => ({id, title: 'Proposal ' + id, business_slug: slug, business_name: name, attention: {reason: 'r'}});

test('an individual approval capsule opens its exact signoff and mutates nothing', async () => {
  const t = setup([{id: 'd1', kind: 'individual', items: [item('s1', 'p', 'Project')]}]);
  assert.match(t.host.innerHTML, /reply-toast sg-toast/);
  assert.match(t.host.innerHTML, /Proposal s1/);
  assert.doesNotMatch(t.host.innerHTML, /approve/i);
  await t.handlers.click({target: t.pill});
  assert.deepEqual(t.navigations.map(n => [...n]), [['p', 's1']]);
  assert.deepEqual(t.posts, []);
});

test('a grouped capsule opens its single project list, or lists projects in Focus', async () => {
  const one = setup([{id: 'd2', kind: 'group', items: [item('a', 'p', 'Project'), item('b', 'p', 'Project')]}]);
  await one.handlers.click({target: one.pill});
  assert.deepEqual(one.navigations.map(n => [...n]), [['p', null]]);
  const many = setup([{id: 'd3', kind: 'group', items: [item('a', 'p', 'Project'), item('b', 'q', 'Other')]}]);
  await many.handlers.click({target: many.pill});
  assert.deepEqual(many.navigations, []);
  assert.deepEqual(many.menus, []);
  assert.equal(many.opened[0].title, 'Signoffs');
  const box = {children: [], append(...c) { this.children.push(...c); }};
  let current = true;
  many.opened[0].mount(box, {isCurrent: () => current});
  const buttons = many.created.filter(n => n.tag === 'button');
  assert.deepEqual(buttons.map(b => b.textContent), ['Proposal a', 'Proposal b']);
  buttons[1].onclick(); await new Promise(r => setImmediate(r));
  assert.deepEqual(many.navigations.map(n => [...n]), [['q', 'b']]);
  current = false;
  buttons[0].onclick(); await new Promise(r => setImmediate(r));
  assert.equal(many.navigations.length, 1);
  assert.deepEqual(many.posts, []);
});

test('dismiss and snooze are reachable from the context menu and keyboard', async () => {
  const t = setup([{id: 'd1', kind: 'individual', items: [item('s1', 'p', 'Project')]}]);
  let prevented = false;
  t.handlers.contextmenu({target: t.pill, clientX: 0, clientY: 0, preventDefault() { prevented = true; }});
  assert.equal(prevented, true);
  assert.deepEqual([t.menus[0].x, t.menus[0].y], [10, 40]);
  assert.deepEqual([...t.menus[0].items.map(i => i.label)], ['Dismiss notification', 'Snooze 4 hours']);
  t.menus[0].items[0].onClick(); await new Promise(r => setImmediate(r));
  t.menus[0].items[1].onClick(); await new Promise(r => setImmediate(r));
  assert.equal(t.posts[0][0], '/me/attention/deliveries/d1/dismiss');
  assert.equal(t.posts[1][0], '/me/attention/signoffs/s1/snooze');
  t.handlers.keydown({target: t.pill, key: 'Enter', preventDefault() {}});
  await new Promise(r => setImmediate(r));
  assert.deepEqual(t.navigations.map(n => [...n]), [['p', 's1']]);
});

test('a menu keeps its own delivery after a refresh shows another one', async () => {
  const t = setup([{id: 'A', kind: 'individual', items: [item('sa', 'p', 'Project')]}]);
  t.handlers.contextmenu({target: t.pill, clientX: 3, clientY: 4, preventDefault() {}});
  const menu = t.menus[0];
  t.context.sub.sgAttention.deliveries = [{id: 'B', kind: 'individual', items: [item('sb', 'p', 'Project')]}];
  t.context.sub.sgAttentionPaint();
  assert.equal(t.host.dataset.delivery, 'B');
  menu.items[0].onClick(); menu.items[0].onClick(); menu.items[1].onClick();
  await new Promise(r => setImmediate(r));
  assert.deepEqual(t.posts.map(p => p[0]), ['/me/attention/deliveries/A/dismiss', '/me/attention/signoffs/sa/snooze']);
});

test('ContextMenu and Shift+F10 open the menu once', () => {
  const t = setup([{id: 'A', kind: 'individual', items: [item('sa', 'p', 'Project')]}]);
  let prevented = 0;
  const key = (k, shiftKey = false) => t.handlers.keydown({target: t.pill, key: k, shiftKey, preventDefault() { prevented++; }});
  key('ContextMenu'); key('F10', true); key('F10');
  assert.equal(t.menus.length, 2); assert.equal(prevented, 2);
  t.handlers.contextmenu({target: t.pill, clientX: 0, clientY: 0, preventDefault() {}});
  assert.equal(t.menus.length, 2);
  assert.deepEqual(t.navigations, []);
});
