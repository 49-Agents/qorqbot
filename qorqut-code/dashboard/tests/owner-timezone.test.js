'use strict';
const {test} = require('node:test'), assert = require('node:assert/strict'), fs = require('node:fs'), vm = require('node:vm'), path = require('node:path');
const read = name => fs.readFileSync(path.join(__dirname, '../js', name), 'utf8');
function page(source) {
  const posts = [], events = [];
  const context = {Intl, Date, Promise, CustomEvent: class { constructor(type, init) { this.type = type; this.detail = init?.detail; } },
    document: {dispatchEvent: event => events.push(event)}, state: {session: null},
    coreData: data => data, post: async (url, body) => { posts.push([url, body]); return {timezone: body.timezone, source: 'owner'}; }};
  vm.runInNewContext(read('san-juan-time.js') + '\n' + read('owner-timezone.js') + '\nthis.api = {ownerTimezoneAdopt, ownerTimezoneSave, ownerTimezoneOffset, sjZone: () => SJ_ZONE, browser: ownerTimezoneBrowserZone};', context);
  return {api: context.api, posts, events, context};
}
test('first run fills the owner timezone from the browser once, only for the owner and only when never set', async () => {
  const {api, posts, events, context} = page();
  context.state.session = {is_installation_owner: true, operational_timezone: 'UTC', operational_timezone_source: 'default'};
  await api.ownerTimezoneAdopt(context.state.session);
  await api.ownerTimezoneAdopt(context.state.session);
  assert.equal(posts.length, 1);
  assert.equal(JSON.stringify(posts[0]), JSON.stringify(['/root/operational-timezone', {timezone: api.browser()}]));
  assert.equal(api.sjZone(), api.browser());
  assert.equal(context.state.session.operational_timezone_source, 'owner');
  assert.equal(events[0].type, 'operational-timezone-changed');
  const other = page();
  assert.equal(other.api.ownerTimezoneAdopt({is_installation_owner: true, operational_timezone_source: 'owner'}), null);
  assert.equal(other.api.ownerTimezoneAdopt({is_installation_owner: false, operational_timezone_source: 'default'}), null);
  assert.equal(other.posts.length, 0);
});
test('a chosen zone applies to the page at once and shows its offset', async () => {
  const {api, context} = page();
  context.state.session = {};
  await api.ownerTimezoneSave('America/Puerto_Rico');
  assert.equal(api.sjZone(), 'America/Puerto_Rico');
  assert.equal(api.ownerTimezoneOffset('America/Puerto_Rico'), 'UTC-04:00');
  assert.equal(api.ownerTimezoneOffset('Not/AZone'), '');
});
