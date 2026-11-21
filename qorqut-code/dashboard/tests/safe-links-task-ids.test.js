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
    assert.equal(project, `/?open_task=${taskId}&project_id=${PROJECT}#/global/agents`);
    assert.equal(safeLinkUrl(global), global);
    assert.equal(safeLinkUrl(project), project);
    assert.deepEqual(plain(openTaskRoute(`?open_task=${taskId}&project_id=${PROJECT}`)), {taskId, projectId: PROJECT});
  }
});

test('a Task link with the id alone is found from its id', () => {
  for (const taskId of ['ab3cd', '3f2c4d5e-1a2b-4c3d-8e9f-0a1b2c3d4e5f', 'seo-autopilot']) {
    assert.deepEqual(plain(openTaskRoute(`?open_task=${taskId}`)), {taskId});
    assert.equal(safeLinkUrl(`/?open_task=${taskId}#/global/agents`), `/?open_task=${taskId}#/global/agents`);
  }
  // A stated scope or project names where the Task is.
  assert.deepEqual(plain(openTaskRoute('?open_task=ab3cd&scope=global')), {taskId: 'ab3cd', projectId: null});
  assert.deepEqual(plain(openTaskRoute(`?open_task=ab3cd&project_id=${PROJECT}`)), {taskId: 'ab3cd', projectId: PROJECT});
  assert.equal(openTaskRoute('?other=1'), null);
});

test('a table link names its project only when it states one', () => {
  const {tableViewRoute} = context, TABLE = '3f2c4d5e-1a2b-4c3d-8e9f-0a1b2c3d4e5f';
  assert.deepEqual(plain(tableViewRoute(`?table_id=${TABLE}`)), {tableId: TABLE});
  assert.deepEqual(plain(tableViewRoute(`?table_id=${TABLE}&project_id=${PROJECT}`)), {tableId: TABLE, projectId: PROJECT});
  for (const search of [`?table_id=${TABLE}&project_id=x`, `?table_id=x`, `?project_id=${PROJECT}`, `?table_id=${TABLE}&table_id=${TABLE}`,
                        `?table_id=${TABLE}&scope=global`]) assert.equal(tableViewRoute(search), null, search);
});

test('malformed Task links stay refused', () => {
  const refused = [
    '?open_task=&scope=global',                                   // empty id
    '?open_task=%20%20&scope=global',                             // blank id
    '?open_task=ab%0Acd&scope=global',                            // control character
    `?open_task=${'a'.repeat(201)}&scope=global`,                 // longer than any stored id
    '?open_task=ab3cd&scope=project',                            // scope other than global
    '?open_task=ab3cd&project_id=ab3cd',                         // a Project is named by its UUID
    `?open_task=ab3cd&scope=global&project_id=${PROJECT}`,       // both scopes
    '?open_task=ab3cd&open_task=k7m2p&scope=global',              // repeated id
    '?open_task=ab3cd&scope=global&next=x',                       // unknown key
  ];
  for (const search of refused) assert.deepEqual(plain(openTaskRoute(search)), {invalid: true}, search);
  assert.equal(openTaskHref('ab\ncd', null), null);
  assert.equal(openTaskHref('ab3cd', 'not-a-project'), null);
  assert.equal(safeLinkUrl('/?open_task=ab3cd&scope=global#/elsewhere'), null);
  assert.equal(safeLinkUrl('/?open_task=ab3cd&scope=global'), null);
});
