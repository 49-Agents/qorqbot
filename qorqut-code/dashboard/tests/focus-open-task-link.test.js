'use strict';
// /?open_task=<id> with the id alone reads the Task by its id and opens it with the project the Task reports; the stated forms read as before.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const plain = value => JSON.parse(JSON.stringify(value));
const PROJECT = '7c9e6679-7425-40de-944b-e07fc1f90ae7';

function fixture({task, projects = [{id: PROJECT, slug: 'acme'}]}) {
  const calls = [], opened = [], errors = [];
  const context = vm.createContext({
    URL, URLSearchParams, document: {addEventListener() {}}, history: {replaceState() {}},
    state: {projects}, agpadActive: () => true,
    coreData: result => result.data,
    post: async (url, args) => { calls.push(['post', url, args]); return {data: {task}}; },
    crmCall: async (slug, operation, args) => { calls.push(['crm', slug, operation, args]); return {task}; },
    rwRecordFromItem: item => item.source,
  });
  for (const file of ['safe-links.js', 'agent-focus.js']) vm.runInContext(fs.readFileSync(path.join(__dirname, '..', 'js', file), 'utf8'), context);
  // The file declares these; the test replaces them with stubs.
  Object.assign(context, {agfocusPaint() {}, agfocusShowDock() {}, agfocusWorkspaceMode: async () => true});
  const pane = {view: 'focus', focusMount: {isConnected: true}, focusWorkspace: {open: async record => { opened.push(JSON.parse(JSON.stringify(record))); }}, errors};
  const open = search => context.agfocusOpenTaskLink(pane, new URL(`http://dashboard.test/${search}`));
  return {calls, opened, pane, open};
}

test('an id alone is read from the root route and opens with the project the Task reports', async () => {
  const {calls, opened, open} = fixture({task: {id: 'ab3cd', title: 'Launch', project_id: PROJECT}});
  await open('?open_task=ab3cd');
  assert.deepEqual(plain(calls), [['post', '/root/operations/tasks_get', {task_id: 'ab3cd'}]]);
  assert.deepEqual(opened.map(({type, id, project_id, project_slug}) => ({type, id, project_id, project_slug})),
    [{type: 'task', id: 'ab3cd', project_id: PROJECT, project_slug: 'acme'}]);
});

test('an id alone finds a Global Task, and refuses a Task whose project is not known to the dashboard', async () => {
  const global = fixture({task: {id: 'ab3cd', title: 'Launch', project_id: null}});
  await global.open('?open_task=ab3cd');
  assert.deepEqual(global.opened.map(record => [record.project_id, record.project_slug]), [[null, null]]);
  const unknown = fixture({task: {id: 'ab3cd', title: 'Launch', project_id: PROJECT}, projects: []});
  await unknown.open('?open_task=ab3cd');
  assert.deepEqual(plain(unknown.opened), []);
  assert.equal(unknown.pane.focusLinkError, 'This record is unavailable.');
});

test('the stated forms read as they did', async () => {
  const global = fixture({task: {id: 'ab3cd', title: 'Launch', project_id: null}});
  await global.open('?open_task=ab3cd&scope=global');
  assert.deepEqual(plain(global.calls), [['crm', null, 'tasks_get', {task_id: 'ab3cd'}]]);
  const project = fixture({task: {id: 'ab3cd', title: 'Launch', project_id: PROJECT}});
  await project.open(`?open_task=ab3cd&project_id=${PROJECT}`);
  assert.deepEqual(plain(project.calls), [['crm', 'acme', 'tasks_get', {task_id: 'ab3cd'}]]);
  assert.deepEqual(project.opened.map(record => record.project_id), [PROJECT]);
  const invalid = fixture({task: {}});
  await invalid.open('?open_task=ab3cd&scope=project');
  assert.deepEqual(plain(invalid.calls), []);
  assert.equal(invalid.pane.focusLinkError, 'This record link is not valid.');
});
