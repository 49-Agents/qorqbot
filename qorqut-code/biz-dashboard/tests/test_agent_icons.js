// Every view uses the saved icon; primary agents retain their shared mascot.
const assert = require('node:assert/strict'), fs = require('node:fs'), path = require('node:path');
const {AGENT_ICON_IDS, catalogHTML, loadCore} = require('../tools/agent-icons/build_catalog');
const context = loadCore(), run = code => require('node:vm').runInContext(code, context);
assert.equal(AGENT_ICON_IDS.length, 30);
for (const id of AGENT_ICON_IDS) assert.match(String(run("AICONS")[id] || ''), /^<(path|circle|rect|ellipse|polyline|line|polygon)/, `catalog icon ${id} has a bundled SVG`);
assert.equal(fs.readFileSync(path.join(__dirname, '../docs/agent-icon-catalog.html'), 'utf8'), catalogHTML(), 'run node tools/agent-icons/build_catalog.js');
const glyph = meta => { context.meta = meta; return run('agentGlyph(meta,14)'); };
const expected = id => { context.id = id; return run("aicon(id,'currentColor',14)"); };
// The current global identities share the Qorqbot face; saved catalog choices remain untouched.
const colors={ordubashy:'#258fa0','learning-applier':'#9063c8'};
for(const [role,color] of Object.entries(colors)){
  const svg=glyph({role:{id:role},icon:'cat'});
  assert.match(svg,/class="ai qorqbot-agent"/);
  assert.ok(svg.includes(color));
  assert.match(svg,/qorqbot-eye/);
}
// Non-primary agents wear the original Tiny Spirits, selected by the stored registration UUID.
const body = i => run('TINY_SPIRIT_BODIES')[i];
const spirit = (id, color, i, size = 14) => `<svg class="ai qorqbot-agent qorqbot-spirit" width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <g fill="${color}">${body(i)}</g><g fill="#fff">
      <g class="qorqbot-gaze"><g class="qorqbot-eye"><ellipse cx="8.5" cy="12" rx="1.4" ry="2.6"/></g></g>
      <g class="qorqbot-gaze"><g class="qorqbot-eye"><ellipse cx="15.5" cy="12" rx="1.4" ry="2.6"/></g></g>
      </g></svg>`;
const shapeOf = id => { let h = 0; for (const c of id) h = (h * 31 + c.charCodeAt(0)) >>> 0; return h % 3; };
const ids = ['worker-a', 'worker-b', 'worker-c', 'worker-d', 'worker-e'];
assert.deepEqual(new Set(ids.map(shapeOf)), new Set([0, 1, 2]), 'known ids cover all three shapes');
const worker = {role: {id: 'global-worker'}, agent_id: 'worker-a', department: 'general', icon: 'cat'};
for (const id of ids) {
  const color = (context.m = {role: {id: 'global-worker'}, agent_id: id}, run('qorqbotAgentColor(m)'));
  assert.equal(glyph({...worker, agent_id: id}), spirit(id, color, shapeOf(id)), `${id} keeps its original spirit`);
}
// Catalog icons, stale metadata and renames never change the spirit.
assert.equal(glyph(worker), glyph({...worker, icon: 'dog', display_name: 'Renamed', color: '#000'}));
assert.equal(glyph({role_id: 'global-worker', agent_id: 'worker-a', presentation: {icon: 'rocket'}}), glyph(worker));
assert.ok(!/rocket|cat|bot/.test(glyph({...worker, icon: 'rocket'}).replace(/qorqbot/g, '')));
// The retired Marketing role has no special rendering: only global-worker uses the spirit glyph.
const asWorker = {agent_id: 'worker-b', icon: 'rocket'};
assert.equal(glyph({role: {id: 'global-marketing'}, ...asWorker}), expected('rocket'), 'the retired Global Marketing role gets no spirit or department colour');
assert.equal(glyph({role: {id: 'global-worker'}, agent_id: 'worker-b', department: 'marketing'}), glyph({role: {id: 'global-worker'}, agent_id: 'worker-b'}), 'department metadata no longer changes the glyph');
// Hostile metadata never reaches the markup.
const hostile = glyph({...worker, icon: '<svg onload=1>', color: '"><script>alert(1)</script>', department: '"><b>'});
assert.ok(!/onload|script|<b>/.test(hostile));
// Saved profiles win over caller fields and survive display-name edits.
run("globalAgentProfiles = [{agent_id:'worker-c',role:{id:'global-worker'},display_name:'One',presentation:{icon:'rocket',department:'general'}}]");
const saved = glyph({agent_id: 'worker-c', icon: 'cat', display_name: 'Two'});
assert.equal(saved, spirit('x', (context.m = {role: {id: 'global-worker'}, agent_id: 'worker-c'}, run('qorqbotAgentColor(m)')), shapeOf('worker-c')));
assert.equal(glyph({agent_id: 'worker-c'}), saved, 'callers may omit fields');
assert.equal(run("tinySpiritGlyph({agent_id:'worker-c',role:{id:'global-worker'}},31)").includes('width="31"'), true);
assert.equal(run("primaryAgent({role_id:'learning-applier'})"),true);
assert.equal(run("primaryAgent({role:{id:'global-worker'},display_name:'Ordubashy'})"),false);
assert.ok(!glyph({...worker,icon:'<svg onload=1>'}).includes('onload'));
assert.equal(glyph({department:'marketing',icon:'rocket'}),expected('rocket'),'role-less identities render the saved catalog icon; the department is ignored');
assert.equal(glyph({icon:'compass'}),expected('bot'),'retired department icon names fall back to the bot icon');
assert.equal(glyph({role:{id:'ordubashy'}}).includes('qorqbot-spirit'),false,'primary art untouched');
console.log('Agent icon catalog and shared Qorqbot identities passed.');
