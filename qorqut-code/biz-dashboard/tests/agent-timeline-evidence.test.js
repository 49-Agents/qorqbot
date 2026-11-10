const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const read = name => fs.readFileSync(path.join(__dirname, '..', 'js', name), 'utf8');
const missing = () => Object.assign(new Error('Missing record'), {code:'not_found'});
function timeline(overrides = {}) {
  const context = vm.createContext({
    URL, document:{addEventListener() {}}, T_VIEW_LABEL:{}, T_VIEW_ICON:{},
    state:{bizs:[]}, coreData:body=>body.data, SJ_ZONE:'UTC',
    ...overrides,
  });
  vm.runInContext(read('safe-links.js'), context);
  vm.runInContext('const esc = safeLinkEsc;', context);
  vm.runInContext(read('agent-timeline.js'), context);
  return context;
}

test('evidence has no summary tooltip and links prose without allowing unsafe URLs', () => {
  const t = timeline();
  const html = t.agentTimelineRow({text:'Recorded result', detail:'Read [source](https://example.com/receipt).',
    refs:{url:'javascript:alert(1)',task:'task-1'}});
  assert.ok(!html.includes('title="Show evidence"'));
  assert.ok(html.includes('href="https://example.com/receipt"'));
  assert.ok(!html.includes('href="javascript:'));
  assert.ok(html.includes('aria-label="Open task reference"'));
  assert.ok(html.includes('aria-label="Copy url reference"'));
});

test('a globally recorded action opens a project task in its actual scope', async () => {
  const calls = [];
  const t = timeline({workCall:async (slug,operation,args) => {
    calls.push({slug,operation,args});
    if (slug==='project-a') return {task:{id:'task-1',business_id:'project-id',title:'Task'}};
    throw missing();
  }});
  const result = await t.agentTimelineResolve({legacy:{business_id:null},
    businesses:[{id:'project-id',slug:'project-a'}]}, 'task', 'task-1');
  assert.equal(result.slug,'project-a');
  assert.equal(result.businessId,'project-id');
  assert.deepEqual(calls.map(c=>c.slug),[null,'project-a']);
});

test('an ambiguous ID never silently opens the wrong project', async () => {
  const t = timeline({workCall:async () => ({task:{id:'task-1'}})});
  await assert.rejects(t.agentTimelineResolve({businesses:[{id:'project-id',slug:'project-a'}]},'task','task-1'),
    /more than one project/);
});

test('signoffs use their existing HTTP reader instead of an unregistered operation', async () => {
  const requests = [];
  const t = timeline({api:async url => {requests.push(url);return {data:{signoff:{id:'signoff-1'}}};}});
  const result = await t.agentTimelineResolve({},'signoff','signoff-1');
  assert.equal(result.businessId,null);
  assert.deepEqual(requests,['/root/signoffs/signoff-1']);
});

test('documents and uploads resolve through their supported readers', async () => {
  const operations = [];
  const t = timeline({workCall:async (slug,operation) => {
    operations.push(operation);
    if (operation==='assets_get') return {asset:{id:'asset-1',kind:'asset'}};
    throw Object.assign(new Error('Different document kind'),{code:'validation_error'});
  }});
  const result = await t.agentTimelineResolve({},'upload','asset-1');
  assert.equal(result.record.kind,'asset');
  assert.deepEqual(operations,['notes_get','context_get','assets_get']);
});

test('authorization and network failures are not hidden as missing evidence', async () => {
  const t = timeline({workCall:async () => {throw Object.assign(new Error('Access denied'),{code:'forbidden'});}});
  await assert.rejects(t.agentTimelineResolve({},'task','task-1'),/Access denied/);
});
