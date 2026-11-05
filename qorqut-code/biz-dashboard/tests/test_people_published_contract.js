// Current published Context DTOs, cursor integrity and explicitly stored People classification.
const assert = require('node:assert/strict');
const fs = require('node:fs'), path = require('node:path'), vm = require('node:vm');
const root = path.resolve(__dirname, '..');
const fixtures = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures/context-published/main.json'), 'utf8'));
const pages = fixtures.filter(x => x.operation === 'context_find' && x.request.query === 'Quartz' && x.request.limit === 50);
assert.ok(pages.length);
let calls = [], change = false;
const box = {console, AbortController, pageReads: {signal: new AbortController().signal},
  coreData: value => value, pageReadPost: async (url, args, signal) => {
    calls.push({url, args, signal});
    const hit = pages.find(x => (x.request.cursor || null) === (args.cursor || null));
    assert.ok(hit, 'follow an exact returned cursor');
    const response = structuredClone(hit.response);
    const data = response.data || response;
    if (change && args.cursor) data.revision = 'd'.repeat(40);
    return data;
  }};
vm.createContext(box);
for (const file of ['context-published', 'context']) vm.runInContext(fs.readFileSync(path.join(root, 'js', file + '.js'), 'utf8'), box);
(async () => {
  const result = await vm.runInContext("ctxReadFindAll({kind:'root'}, 'Quartz')", box);
  assert.ok(result.items.length);
  assert.ok(calls.every(x => x.url === '/root/operations/context_find'));
  const expectedCursors = [null];
  for (let page = pages.find(x => !x.request.cursor); (page.response.data || page.response).page.next_cursor;) {
    const next = (page.response.data || page.response).page.next_cursor;
    expectedCursors.push(next); page = pages.find(x => x.request.cursor === next);
  }
  assert.ok(calls.every(x => x.args.query === 'Quartz' && x.args.limit === 50 && x.signal === box.pageReads.signal), 'query, current page limit and signal on every page');
  assert.deepEqual(calls.map(x => x.args.cursor || null), expectedCursors, 'each page sends the cursor the previous page returned');
  calls = [];
  const business = await vm.runInContext("ctxReadFindAll({kind:'business', id:'studio/one'}, 'Quartz')", box);
  assert.deepEqual(business.items.map(x => x.ref), result.items.map(x => x.ref));
  assert.ok(calls.length && calls.every(x => x.url === '/biz/studio%2Fone/operations/context_find'), 'business scope encodes its id');
  assert.deepEqual(calls.map(x => x.args.cursor || null), expectedCursors);
  await assert.rejects(vm.runInContext("ctxReadFindAll({kind:'business'}, 'Quartz')", box), /scope is required/);
  calls = [];
  await vm.runInContext("ctxReadFindAll({kind:'root'}, 'Quartz')", box);
  box.sample = structuredClone(result.items.find(x => x.kind === 'node'));
  assert.ok(box.sample);
  box.sample.attributes = {basic_entity_type: ['person'], name: ['Ada'], role: ['Founder']};
  let person = vm.runInContext('pubPerson(sample)', box);
  assert.equal(person.name, 'Ada'); assert.equal(person.role, 'Founder'); assert.equal(person.ref, box.sample.ref);
  box.sample.attributes.basic_entity_type = ['company'];
  assert.equal(vm.runInContext('pubPerson(sample)', box), null, 'do not infer a person from a human-looking name');
  box.sample.kind = 'person'; box.sample.attributes.basic_entity_type = ['person'];
  assert.equal(vm.runInContext('pubPerson(sample)', box), null, 'retired DTO kinds are not accepted');
  if (calls.length > 1) {
    change = true;
    await assert.rejects(vm.runInContext("ctxReadFindAll({kind:'root'}, 'Quartz')", box), /published again/);
  }
  box.pageReads.signal = AbortSignal.abort();
  await assert.rejects(vm.runInContext("ctxReadFindAll({kind:'root'}, 'Quartz')", box), /cancelled/);
  console.log('PASS published People: current DTO, exact cursors, explicit classification, revision and cancellation');
})().catch(error => { console.error(error); process.exitCode = 1; });
