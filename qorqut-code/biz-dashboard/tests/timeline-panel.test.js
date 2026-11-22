'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const read=name=>fs.readFileSync(require.resolve('../js/'+name),'utf8');
function load(extra={}) {
  const ctx={window:{},document:{},T_VIEW_LABEL:{},T_VIEW_ICON:{},state:{bizs:[{id:'p1',slug:'example'}]},coreData:x=>x,...extra};
  vm.createContext(ctx);
  vm.runInContext(read('timeline-panel.js')+'\n'+read('workflow-panel.js')
    +'\nglobalThis.api={timelineHtml,timelinePanelMount,globalTimelineBlockMount,timelineJournalRows,timelineJournalRow,timelineJournalMount};',ctx);
  return {api:ctx.api,ctx};
}
const entry=(id,text,more={})=>({id,text,created_at:'2026-11-13T10:00:00.000000Z',sequence:1,...more});
const STATE={goal:entry('g','Pay every creator.'),decisions:[entry('d','One provider <only>.')],facts:[entry('f','The hold was cleared.')],
  progress:entry('p','9 of 14 paid.',{created_at:'2026-11-15T09:00:00Z'}),
  questions:[{id:'q1',text:'Dollars or local currency?',blocking:true,raised_by:'coordinator'},{id:'q2',text:'Which month next?',blocking:false,raised_by:'owner'}]};
const DATA={item:{type:'task',id:'t1',business_id:null},title:'Pay creators',revision:7,state:STATE,further:{}};
const HISTORY={entries:[
  {id:'a',sequence:1,action:'add',kind:'decision',text:'Old way.',created_at:'2026-11-13T10:00:00Z',removed_by:'r',sources:[{quote:'Do it <the new> way.',ref:'thread:3'}],refs:[]},
  {id:'r',sequence:2,action:'remove',kind:'decision',text:'Replaced.',created_at:'2026-11-14T10:00:00Z',removes_id:'a',sources:[],refs:[]},
  {id:'b',sequence:3,action:'add',kind:'fact',text:'Kept fact.',created_at:'2026-11-15T10:00:00Z',sources:[],refs:[]}],
  links:{subtasks:[{type:'task',id:'t2',business_id:null,title:'Pay <A>',status:'open'}],approvals:[{type:'signoff',id:'s1',title:'Approve batch',state:'pending'}],reports:[]},next_cursor:null};

test('the state shows goal, decisions, facts, progress and open questions with blocking marked, all escaped',()=>{
  const {api}=load();
  const html=api.timelineHtml(DATA,{});
  for(const text of ['Pay every creator.','One provider &lt;only&gt;.','The hold was cleared.','9 of 14 paid.','15 Nov','Dollars or local currency?','Which month next?'])
    assert.ok(html.includes(text),text);
  assert.equal(html.includes('<only>'),false);assert.equal(/holds now|tlp-title/.test(html),false);
  assert.equal((html.match(/Blocks work/g)||[]).length,1);
  assert.ok(html.indexOf('Blocks work')<html.indexOf('Dollars or local'));
  assert.ok(html.includes('data-tlp-switch'));assert.ok(html.includes('>History<'));
});
test('an item with no timeline shows nothing, and one with only removed entries says nothing is current',()=>{
  const {api}=load();
  const none={goal:null,decisions:[],facts:[],progress:null,questions:[]};
  assert.equal(api.timelineHtml({...DATA,state:none,revision:0},{}),'');
  assert.equal(api.timelineHtml(null,{}),'');
  const gone=api.timelineHtml({...DATA,state:none,revision:3},{});
  assert.ok(gone.includes('Nothing current.'));assert.ok(gone.includes('data-tlp-switch'));
});
test('the history lists every entry oldest first, strikes removed ones with what removed them and shows the quoted words',()=>{
  const {api}=load();
  const html=api.timelineHtml(DATA,{open:true,history:HISTORY});
  assert.ok(html.includes('Hide history'));
  assert.equal(html.includes('Dollars or local'),false);
  assert.ok(html.includes('<s>Old way.</s>'));
  assert.ok(html.includes('Removed 14 Nov: Replaced.'));
  assert.ok(html.includes('<blockquote class="tlp-quote">Do it &lt;the new&gt; way.</blockquote>'));
  assert.equal(html.includes('Do it <the new>'),false);
  assert.ok(html.indexOf('Old way.')<html.indexOf('Kept fact.'));
  assert.equal(html.includes('tlp-removal'),false);
  assert.ok(html.includes('Subtasks'));assert.ok(html.includes('Pay &lt;A&gt;'));assert.ok(html.includes('data-tlp-open="t2"'));
  assert.ok(html.includes('Approvals'));assert.ok(html.includes('Approve batch'));assert.equal(html.includes('Reports'),false);
  assert.ok(api.timelineHtml(DATA,{open:true,history:null}).includes('Loading…'));
  const orphan=api.timelineHtml(DATA,{open:true,history:{entries:[HISTORY.entries[1]],links:{},next_cursor:'3'}});
  assert.ok(orphan.includes('tlp-removal'));assert.ok(orphan.includes('data-tlp-more'));
});
test('quoted words on a removal show in both forms, and removed_by marks an entry whose removal is not loaded',()=>{
  const {api}=load();
  const removal={...HISTORY.entries[1],sources:[{quote:'Please <drop> it.',ref:'thread:5'}]};
  const folded=api.timelineHtml(DATA,{open:true,history:{...HISTORY,entries:[HISTORY.entries[0],removal,HISTORY.entries[2]]}});
  assert.ok(folded.includes('Please &lt;drop&gt; it.'));assert.equal(folded.includes('<drop>'),false);
  const orphan=api.timelineHtml(DATA,{open:true,history:{entries:[removal],links:{},next_cursor:null}});
  assert.ok(orphan.includes('Please &lt;drop&gt; it.'));
  const early=api.timelineHtml(DATA,{open:true,history:{entries:[HISTORY.entries[0]],links:{},next_cursor:'2'}});
  assert.ok(early.includes('<s>Old way.</s>'));assert.ok(early.includes('tlp-removed'));assert.ok(early.includes('tlp-gone'));
  const standing=api.timelineHtml(DATA,{open:true,history:{entries:[{...HISTORY.entries[0],removed_by:null}],links:{},next_cursor:null}});
  assert.equal(standing.includes('<s>'),false);
});
test('closed questions are listed under a heading with the answering entry when it is loaded',()=>{
  const {api}=load();
  const history={...HISTORY,closed_questions:[
    {id:'c1',text:'Which <month>?',state:'answered',blocking:false,raised_by:'coordinator',answer_entry_id:'b',closed_at:'2026-11-15T10:00:00Z'},
    {id:'c2',text:'Dropped one?',state:'dropped',blocking:false,raised_by:'owner',answer_entry_id:null,closed_at:'2026-11-15T11:00:00Z'},
    {id:'c3',text:'Answer not loaded?',state:'answered',blocking:false,raised_by:'owner',answer_entry_id:'zz',closed_at:'2026-11-15T12:00:00Z'}]};
  const html=api.timelineHtml(DATA,{open:true,history});
  assert.ok(html.includes('Closed questions'));assert.ok(html.includes('Which &lt;month&gt;?'));assert.equal(html.includes('<month>'),false);
  assert.ok(html.includes('Answered'));assert.ok(html.includes('Dropped'));
  assert.ok(html.includes('<p class="tlp-gone">Kept fact.</p>'));
  assert.equal(api.timelineHtml(DATA,{open:true,history:HISTORY}).includes('Closed questions'),false);
});
test('links without a title get a neutral label, and a null goal or progress renders as nothing',()=>{
  const {api}=load();
  const history={entries:[],links:{subtasks:[],approvals:[{type:'signoff',id:'s1',state:'pending'}],reports:[{type:'signoff',id:'s2',state:'sent'}]},next_cursor:null};
  const html=api.timelineHtml(DATA,{open:true,history});
  assert.ok(html.includes('<li>Approval '));assert.ok(html.includes('<li>Report '));assert.equal(html.includes('Untitled'),false);
  const state=api.timelineHtml({...DATA,state:{goal:null,decisions:[],facts:[entry('f','Only a fact.')],progress:null,questions:[]}},{});
  assert.ok(state.includes('Only a fact.'));assert.equal(state.includes('null'),false);assert.equal(state.includes('Goal'),false);assert.equal(state.includes('Progress'),false);
});
test('the history error clears on a later success, and a failed context reload shows the error line instead of old content',async()=>{
  const {api}=load();
  const el=host();let fail=true;
  const panel=api.timelinePanelMount(el,{readHistory:async()=>{if(fail)throw new Error('boom');return {...HISTORY,revision:7};}});
  panel.update(DATA);el.click('data-tlp-switch');await settle();
  assert.ok(el.innerHTML.includes('tlp-error'));
  fail=false;panel.update({...DATA,revision:8});await settle();
  assert.equal(el.innerHTML.includes('tlp-error'),false);assert.ok(el.innerHTML.includes('Old way.'));
  panel.fail();
  assert.equal(el.hidden,false);assert.ok(el.innerHTML.includes('tlp-error'));assert.equal(el.innerHTML.includes('Pay every creator.'),false);assert.equal(el.innerHTML.includes('Old way.'),false);
  panel.update(DATA);assert.equal(el.innerHTML.includes('tlp-error'),false);assert.ok(el.innerHTML.includes('Pay every creator.'));
});
test('the record reader owns the timeline: a Global Task is read with an explicit null business, and a thread carries no timeline',()=>{
  const reader=read('record-workspace.js');
  assert.match(reader,/type: 'task', id: source\.id, business_id: source\.business_id \?\? null/);
  assert.match(reader,/item\.timeline\.refresh\(\)/);
  assert.equal(/timelinePanel/.test(read('conversation.js')),false);
});
function host() {
  const h={hidden:true,innerHTML:'',handlers:{},addEventListener(type,fn){this.handlers[type]=fn;},setAttribute(){},contains:()=>true,replaceChildren(){this.innerHTML='';}};
  h.click=attr=>h.handlers.click({target:{closest:()=>({hasAttribute:name=>name===attr,dataset:{tlpOpen:'t2',tlpBusiness:''}})}});
  return h;
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
test('the panel reads the history only when asked, again on a new revision, and hides when the timeline is emptied',async()=>{
  const {api}=load();
  const calls=[],opened=[],el=host();
  const panel=api.timelinePanelMount(el,{readHistory:async args=>{calls.push(args);return {...HISTORY,entries:HISTORY.entries,revision:7};},openTask:ref=>opened.push(ref)});
  panel.update(DATA);
  assert.equal(el.hidden,false);assert.equal(calls.length,0);
  el.click('data-tlp-switch');await settle();
  assert.equal(calls.length,1);assert.ok(el.innerHTML.includes('<s>Old way.</s>'));
  panel.update({...DATA});await settle();assert.equal(calls.length,1);
  panel.update({...DATA,revision:8});await settle();assert.equal(calls.length,2);
  el.click('data-tlp-open');assert.deepEqual(JSON.parse(JSON.stringify(opened)),[{id:'t2',business_id:null}]);
  el.click('data-tlp-switch');assert.ok(el.innerHTML.includes('Dollars or local'));
  panel.update({...DATA,state:{goal:null,decisions:[],facts:[],progress:null,questions:[]},revision:0});
  assert.equal(el.hidden,true);assert.equal(el.innerHTML,'');
});
test('a failed history read says so in plain words',async()=>{
  const {api}=load();
  const el=host();
  const panel=api.timelinePanelMount(el,{readHistory:async()=>{throw new Error('boom');}});
  panel.update(DATA);el.click('data-tlp-switch');await settle();
  assert.ok(el.innerHTML.includes('Couldn&#39;t load the history. Try again'));assert.equal(el.innerHTML.includes('Loading'),false);assert.equal(el.innerHTML.includes('boom'),false);
});
test('the Global block reads the Global item at most every twenty seconds and shows nothing when it is empty',async()=>{
  const reads=[];
  const {api}=load();
  const el=host();
  const block=api.globalTimelineBlockMount(el,{read:async(extra)=>{reads.push(extra||{});return reads.length===1?{...DATA,item:{type:'global',id:'global',business_id:null}}:{...DATA,state:{goal:null,decisions:[],facts:[],progress:null,questions:[]},revision:0};}});
  await block.refresh(true);
  assert.equal(el.hidden,false);assert.ok(el.innerHTML.includes('Pay every creator.'));
  await block.refresh();assert.equal(reads.length,1);
  await block.refresh(true);assert.equal(el.hidden,true);
  block.dispose();
});
test('the Global block asks for the Global item through the owner read',async()=>{
  const posts=[];
  const {api}=load({post:async(path,body)=>{posts.push([path,body]);return {data:{...DATA}};},coreData:x=>x.data});
  const el=host();
  const block=api.globalTimelineBlockMount(el);
  await block.refresh(true);
  assert.deepEqual(JSON.parse(JSON.stringify(posts)),[['/root/operations/timeline_view',{scope:'global',item:{type:'global',id:'global',business_id:null}}]]);
});
test('open questions are question Signoffs in the sign-off strip, with their own icon and no separate card',()=>{
  const queue=read('attention-queue.js'),signoffs=read('signoffs.js'),workspace=read('record-workspace.js');
  assert.doesNotMatch(queue,/timeline_questions_list|agentQuestionOpen|data-question-key/);
  assert.match(queue,/const askKind=s=>s\.asks===true \? 'question'/);assert.match(queue,/const asks=s=>!!askKind\(s\);/);
  assert.match(queue,/toRead\(s\) \? 'read' : asks\(s\) \? askKind\(s\)/);assert.match(queue,/kind==='question' \? '<text [^']*font-weight="600"[^']*>\?<\/text>'/);assert.match(queue,/request:'M12 20a8 8/); assert.match(queue,/s\.operation_name==='timeline_file' && timelineIcon\(\)\)/);assert.match(queue,/T_VIEW_ICON\.timeline/);
  assert.match(signoffs,/function sgIsAsk\(s\) \{ return s\.category === 'input_request' && \['question', 'request'\]\.includes\(s\.input\?\.kind\); \}/);
  assert.match(signoffs,/sgIsAsk\(s\) \? 'Send' :/);assert.doesNotMatch(signoffs,/Clear selection/);
  assert.doesNotMatch(workspace,/record\.question|openThread:/);
  assert.equal(fs.existsSync(require.resolve('../index.html').replace('index.html','js/agent-questions.js')),false);
});
test('the pane has no separate questions view, and the panel and block are wired',()=>{
  const pane=read('agentpane.js'),conversation=read('conversation.js'),globalChat=read('global-chat.js'),reader=read('record-workspace.js');
  assert.doesNotMatch(pane,/"questions"/);assert.doesNotMatch(pane,/agentQuestionsMount|agentQuestionsTick/);
  assert.match(reader,/timelineJournalMount\(item\.timelineHost\.querySelector\('\.agent-timeline'\)/);assert.match(reader,/history: true, limit: 50/);assert.doesNotMatch(reader,/timelinePanelMount|class="tlp"/);assert.doesNotMatch(conversation,/timelinePanelMount/);
  assert.doesNotMatch(globalChat,/globalTimelineBlockMount\(/);
  const html=fs.readFileSync(require.resolve('../index.html'),'utf8');
  for(const asset of ['/js/timeline-panel.js','/css/timeline-panel.css','/css/agent-timeline.css'])assert.ok(html.includes(asset),asset);
  assert.equal(html.includes('/js/agent-questions.js'),false);
});

// The journal: the reader's Timeline pane, in the look of the rail Timeline page.
const JOURNAL={...DATA,...HISTORY,state:{...STATE,questions:[{id:'q1',text:'Dollars or <local>?',blocking:true,created_at:'2026-11-16T08:00:00Z'},{id:'q2',text:'Older question, no date.',blocking:false}]},
  closed_questions:[{id:'q0',text:'Which month?',state:'answered',answer_entry_id:'b',closed_at:'2026-11-15T10:00:00Z'},{id:'q9',text:'Which bank?',state:'dropped',closed_at:'2026-11-12T10:00:00Z'}]};
test('the journal lists entries and questions newest first, folds a removal into the entry it removed, and drops no text',()=>{
  const {api}=load();
  const rows=api.timelineJournalRows(JOURNAL);
  assert.deepEqual(JSON.parse(JSON.stringify(rows.map(row=>row.text))),['Older question, no date.','Dollars or <local>?','Kept fact.','Which month?','Old way.','Which bank?']);
  assert.deepEqual(JSON.parse(JSON.stringify(rows.map(row=>row.meta))),[['Open question'],['Open question','Blocks work'],['Fact'],['Question answered'],['Decision','Removed 14 Nov: Replaced.'],['Question dropped']]);
  const old=rows.find(row=>row.text==='Old way.');assert.equal(old.struck,true);assert.deepEqual(JSON.parse(JSON.stringify(old.quotes)),['Do it <the new> way.']);
  assert.deepEqual(JSON.parse(JSON.stringify(rows.find(row=>row.text==='Which month?').notes)),['Kept fact.']);
  // A removal whose target is not among the entries stays a row of its own.
  const orphan=api.timelineJournalRows({entries:[HISTORY.entries[1]]});
  assert.deepEqual(JSON.parse(JSON.stringify(orphan.map(row=>[row.text,row.meta]))),[['Removed an earlier entry: Replaced.',['Removed']]]);
  assert.equal(api.timelineJournalRows(null).length,0);
});
test('a journal row uses the rail Timeline markup: time, text, meta, and the quoted words behind the chevron, all escaped',()=>{
  const {api}=load({AGENT_TIMELINE_CHEVRON:'<svg data-chevron></svg>'});
  const [plain,,,,struck]=api.timelineJournalRows(JOURNAL).map(api.timelineJournalRow);
  assert.match(plain,/^<li class="agent-timeline-row" data-entry-id="q:q2"><time><\/time><div class="agent-timeline-copy"><p class="agent-timeline-text">Older question, no date\.<\/p><p class="agent-timeline-meta">Open question<\/p><\/div><\/li>$/);
  assert.ok(struck.includes('<time datetime="2026-11-13T10:00:00Z">13 Nov</time>'));
  assert.ok(struck.includes('<details class="agent-timeline-entry"><summary><div class="agent-timeline-copy"><p class="agent-timeline-text"><s>Old way.</s></p><p class="agent-timeline-meta">Decision · Removed 14 Nov: Replaced.</p></div><span class="agent-timeline-expand"><svg data-chevron></svg></span></summary>'));
  assert.ok(struck.includes('<div class="agent-timeline-evidence" aria-label="Evidence"><p>“Do it &lt;the new&gt; way.”</p></div>'));
  assert.equal(/tlp-/.test(struck+plain),false);
  // With the dashboard's clock helpers the time reads as it does on the rail Timeline page.
  const clock=load({acOccurred:a=>`${a.occurred.date||'Undated'} · ${a.occurred.precision||''}`,sjDate:s=>String(s).slice(0,10)}).api;
  assert.ok(clock.timelineJournalRow(clock.timelineJournalRows(JOURNAL)[4]).includes('>2026-11-13 · instant</time>'));
  assert.ok(clock.timelineJournalRow(clock.timelineJournalRows(JOURNAL)[0]).includes('<time>Undated · </time>'));
});
function journalHost() {
  const node=()=>({hidden:false,textContent:'',innerHTML:'',querySelector(){return node();},querySelectorAll(){return [];}});
  const parts={'[data-status]':node(),'.agent-timeline-error':node(),ol:node()};const alert=node();parts['.agent-timeline-error'].querySelector=()=>alert;
  return {parts,alert,handlers:{},innerHTML:'',querySelector(sel){return parts[sel];},addEventListener(type,fn){this.handlers[type]=fn;},replaceChildren(){this.innerHTML='';}};
}
test('the journal reads every page before it paints, counts its rows, and a failed read offers Retry',async()=>{
  const {api}=load();
  const el=journalHost(),calls=[];let fail=false;
  const journal=api.timelineJournalMount(el,{read:async args=>{calls.push(args);if(fail)throw new Error('boom');
    return args.cursor?{entries:[HISTORY.entries[2]],next_cursor:null}:{...JOURNAL,entries:HISTORY.entries.slice(0,2),next_cursor:'2'};}});
  assert.ok(el.innerHTML.includes('class="agent-timeline-caption"'));assert.ok(el.innerHTML.includes('class="agent-timeline-list"'));
  await journal.refresh();
  assert.deepEqual(JSON.parse(JSON.stringify(calls)),[{},{cursor:'2'}]);
  assert.equal(el.parts['[data-status]'].textContent,'6 entries');assert.ok(el.parts.ol.innerHTML.includes('Kept fact.'));assert.equal(el.parts['.agent-timeline-error'].hidden,true);
  fail=true;await journal.refresh();
  assert.equal(el.parts.ol.innerHTML,'');assert.equal(el.parts['.agent-timeline-error'].hidden,false);assert.equal(el.alert.textContent,"Couldn't load the timeline. Try again");
  fail=false;el.handlers.click({target:{closest:sel=>sel==='[data-retry]'?{}:null}});await settle();await settle();
  assert.equal(el.parts['.agent-timeline-error'].hidden,true);assert.equal(el.parts['[data-status]'].textContent,'6 entries');
  const empty=journalHost(),none=api.timelineJournalMount(empty,{read:async()=>({...DATA,state:{goal:null,decisions:[],facts:[],progress:null,questions:[]},entries:[],next_cursor:null})});
  await none.refresh();assert.equal(empty.parts['[data-status]'].textContent,'Nothing on the timeline yet.');assert.equal(empty.parts.ol.hidden,true);
  journal.dispose();assert.equal(el.innerHTML,'');
});
