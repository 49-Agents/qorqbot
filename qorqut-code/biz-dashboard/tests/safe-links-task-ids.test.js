'use strict';
// Task links in written prose open short generated Task ids as well as older UUID and slug ids, and still refuse junk.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const context = vm.createContext({URL, URLSearchParams, document: {addEventListener() {}}});
vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js', 'safe-links.js'), 'utf8'), context);
const {openTaskRoute, openTaskHref, safeLinkUrl} = context;
const PROJECT = '7c9e6679-7425-40de-944b-e07fc1f90ae7';
const plain = value => JSON.parse(JSON.stringify(value));

test('short, UUID and slug Task ids open in Global and in a Project', () => {
  for (const taskId of ['ab3cd', 'k7m2p9', '3f2c4d5e-1a2b-4c3d-8e9f-0a1b2c3d4e5f', 'seo-autopilot']) {
    const global = openTaskHref(taskId, null), project = openTaskHref(taskId, PROJECT);
    assert.equal(global, `/?open_task=${taskId}&scope=global#/global/agents`);
    assert.equal(project, `/?open_task=${taskId}&business_id=${PROJECT}#/global/agents`);
    assert.equal(safeLinkUrl(global), global);
    assert.equal(safeLinkUrl(project), project);
    assert.deepEqual(plain(openTaskRoute(`?open_task=${taskId}&business_id=${PROJECT}`)), {taskId, businessId: PROJECT});
  }
});

test('malformed Task links stay refused', () => {
  const refused = [
    '?open_task=&scope=global',                                   // empty id
    '?open_task=%20%20&scope=global',                             // blank id
    '?open_task=ab%0Acd&scope=global',                            // control character
    `?open_task=${'a'.repeat(201)}&scope=global`,                 // longer than any stored id
    '?open_task=ab3cd&scope=business',                            // scope other than global
    '?open_task=ab3cd&business_id=ab3cd',                         // a Project is named by its UUID
    `?open_task=ab3cd&scope=global&business_id=${PROJECT}`,       // both scopes
    '?open_task=ab3cd',                                           // no scope
    '?open_task=ab3cd&open_task=k7m2p&scope=global',              // repeated id
    '?open_task=ab3cd&scope=global&next=x',                       // unknown key
  ];
  for (const search of refused) assert.deepEqual(plain(openTaskRoute(search)), {invalid: true}, search);
  assert.equal(openTaskHref('ab\ncd', null), null);
  assert.equal(openTaskHref('ab3cd', 'not-a-project'), null);
  assert.equal(safeLinkUrl('/?open_task=ab3cd&scope=global#/elsewhere'), null);
  assert.equal(safeLinkUrl('/?open_task=ab3cd&scope=global'), null);
});
