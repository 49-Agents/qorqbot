'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const read=name=>fs.readFileSync(require.resolve('../js/'+name),'utf8');
function load(extra={}) {
  const ctx={window:{},document:{},T_VIEW_LABEL:{},T_VIEW_ICON:{},state:{bizs:[{id:'p1',slug:'example'}]},coreData:x=>x,...extra};
  vm.createContext(ctx);
  vm.runInContext(read('timeline-panel.js')+'\n'+read('workflow-panel.js')+'\n'+read('agent-questions.js')
    +'\nglobalThis.api={timelineHtml,timelinePanelMount,globalTimelineBlockMount,agentQuestionsHtml,agentQuestionsReference};',ctx);
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
  for(const text of ['What holds now','Pay every creator.','One provider &lt;only&gt;.','The hold was cleared.','9 of 14 paid.','15 Nov','Dollars or local currency?','Which month next?'])
    assert.ok(html.includes(text),text);
  assert.equal(html.includes('<only>'),false);
  assert.equal((html.match(/Blocks work/g)||[]).length,1);
  assert.ok(html.indexOf('Blocks work')<html.indexOf('Dollars or local'));
  assert.ok(html.includes('data-tlp-switch'));assert.ok(html.includes('>History<'));
});
test('an item with no timeline shows nothing, and one with only removed entries says nothing holds now',()=>{
  const {api}=load();
  const none={goal:null,decisions:[],facts:[],progress:null,questions:[]};
  assert.equal(api.timelineHtml({...DATA,state:none,revision:0},{}),'');
  assert.equal(api.timelineHtml(null,{}),'');
  const gone=api.timelineHtml({...DATA,state:none,revision:3},{});
  assert.ok(gone.includes('Nothing holds now.'));assert.ok(gone.includes('data-tlp-switch'));
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
const ROWS=[
  {id:'q1',text:'Pay <F> in dollars?',blocking:true,source:{type:'task',id:'t1',business_id:null},title:'Pay creators',business_name:'Global',created_at:'2026-11-13T10:00:00Z'},
  {id:'q2',text:'Which month?',blocking:false,source:{type:'project',id:'p1',business_id:'p1'},title:'Example project',business_name:'Example project',created_at:'2026-11-14T10:00:00Z'},
  {id:'q3',text:'Open the books?',blocking:true,source:{type:'global',id:'global',business_id:null},title:'Global',business_name:'Global',created_at:'2026-11-15T10:00:00Z'}];
test('the questions page lists each question with its item, blocking marked, and links all but Global',()=>{
  const {api}=load();
  assert.equal(api.agentQuestionsHtml([]),'');
  const html=api.agentQuestionsHtml(ROWS);
  assert.ok(html.includes('Pay &lt;F&gt; in dollars?'));assert.equal(html.includes('<F>'),false);
  assert.equal((html.match(/Blocks work/g)||[]).length,2);
  assert.ok(html.includes('Task · Pay creators'));assert.ok(html.includes('Project · Example project'));
  assert.equal((html.match(/data-question-open/g)||[]).length,2);
  assert.ok(html.includes('data-question-open="0"'));assert.ok(html.includes('13 Nov'));
});
test('a question opens the thread of its Project by slug and its Task by the existing reference',()=>{
  const {api}=load({taskRecordReference:task=>({type:'task',id:task.id,business_id:task.business_id,label:task.label})});
  assert.deepEqual(JSON.parse(JSON.stringify(api.agentQuestionsReference(ROWS[1]))),{type:'project',id:'p1',business_id:'p1',business_slug:'example',label:'Example project'});
  assert.deepEqual(JSON.parse(JSON.stringify(api.agentQuestionsReference(ROWS[0]))),{type:'task',id:'t1',business_id:null,label:'Pay creators'});
  assert.equal(api.agentQuestionsReference(ROWS[2]),null);
  assert.equal(api.agentQuestionsReference({...ROWS[1],source:{type:'project',id:'other',business_id:'other'}}),null);
});
test('the pane offers the questions view to Ordubashy only, and the page, panel and block are wired',()=>{
  const pane=read('agentpane.js'),conversation=read('conversation.js'),globalChat=read('global-chat.js');
  assert.match(pane,/view === "questions"\) return roleId === "ordubashy"/);
  assert.match(pane,/leading\.push\("timeline", "questions"\)/);
  assert.match(pane,/agentQuestionsMount\(p\)/);assert.match(pane,/agentQuestionsTick\(p\)/);
  assert.match(conversation,/timelinePanelMount\(timelineEl/);assert.match(conversation,/timelinePanel\?\.update\(/);
  assert.match(globalChat,/globalTimelineBlockMount\(block/);
  const html=fs.readFileSync(require.resolve('../index.html'),'utf8');
  for(const asset of ['/js/timeline-panel.js','/js/agent-questions.js','/css/timeline-panel.css'])assert.ok(html.includes(asset),asset);
});
