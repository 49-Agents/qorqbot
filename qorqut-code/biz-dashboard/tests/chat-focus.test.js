'use strict';
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
let chromium;
try { ({chromium}=require('playwright')); } catch (_) { /* Optional local browser dependency. */ }
const executable=process.env.CHROME_PATH || chromium?.executablePath();
const available=!!chromium && fs.existsSync(executable);
const browserTest=(name,run)=>test(name,{skip:!available && 'Local Playwright Chromium is unavailable'},run);
const root=path.join(__dirname,'..');

async function fixture(width=900) {
  const browser=await chromium.launch({headless:true,executablePath:executable});
  const page=await browser.newPage({viewport:{width,height:700}});
  const errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.setContent('<main id="pane" style="display:flex;flex-direction:column;height:650px;width:100%;min-width:0;--ag-ctl:30px"></main>');
  await page.clock.install();
  for(const file of ['app.css','css/tasks.css','css/agent-focus.css','css/agent-workspace.css','css/agentpane.css','css/chat-view.css','css/workflow-panel.css','css/global-chat.css'])await page.addStyleTag({path:path.join(root,file)});
  await page.evaluate(()=>{
    window.esc=value=>String(value ?? '').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    window.$=(selector,host=document)=>host.querySelector(selector);
    window.identityAvatar=()=>'<span class="identity-avatar">A</span>';
    window.state={bizs:[]};window.coreData=value=>value;window.evUrl=value=>value;
    window.taskStateObserve=(_scope,task)=>task;
    window.onChange=()=>()=>{};window.accessLost=e=>['forbidden','invalid_principal'].includes(e?.code);
    window._agpad={agentId:'ord',attentionAgentId:'another-todo-agent'};
    window.EventSource=class extends EventTarget {
      static CLOSED=2;
      constructor(){super();window.stream=this;}
      close(){}
    };
    window.binding={provider:'codex',conversation_id:'conversation',binding_id:'binding'};
    window.working=true;window.focusReads=[];window.todoReads=[];window.actionReads=[];window.override=null;window.todoOverride=null;
    window.attentions={
      ord:{state:'focused',focus:{type:'task',id:'launch',label:'Coordinate launch'},intent:'Check the remaining tasks.'},
      atlas:{state:'focused',focus:{type:'task',id:'research',label:'Verify sources'},intent:'Compare the original documents.'}
    };
    window.todoItem=(id,title,status='open',business=null)=>({task_ref:{type:'task',id,business_id:business},task:{id,title,status}});
    window.todos={ord:[todoItem('launch','Coordinate launch','working'),todoItem('review','Review the changes'),todoItem('check','Check the layout'),todoItem('report','Share the result'),todoItem('extra','Later task')],atlas:[todoItem('compare','Compare documents')]};
    window.api=async url=>url.includes('/commands')?{commands:[]}:{session:{state:'live',can_send:true,working,binding},conversation:binding,entries:[],inputs:[],cursor:0};
    window.post=async(url,args,options)=>{
      if(url.endsWith('/actions_list')){
        actionReads.push(structuredClone(args));
        return {actions:[]};
      }
      if(!url.endsWith('/todo_get'))return {};
      if(args.projection==='summary'){
        todoReads.push(structuredClone(args));
        if(todoOverride)return todoOverride(args,options);
        const items=structuredClone(todos[args.agent_id] || []),start=Number(args.cursor || 0),end=start+args.limit;
        return {agent_id:args.agent_id,items:items.slice(start,end),next_cursor:end<items.length?String(end):null};
      }
      focusReads.push(structuredClone(args));
      if(override)return override(args,options);
      return {agent_id:args.agent_id,attention:structuredClone(attentions[args.agent_id])};
    };
  });
  for(const file of ['read-cache','safe-links','san-juan-time','chat-view','slash-commands','attention-board','workflow-panel','global-chat'])await page.addScriptTag({path:path.join(root,'js',file+'.js')});
  await page.evaluate(()=>{
    window.mount=(id='ord',role='ordubashy')=>{
      window.controller?.dispose();
      window.controller=mountGlobalChat(document.querySelector('#pane'),{agent_id:id,name:id,role:{id:role}},{openTerminal:()=>{}});
    };
    mount();
  });
  if(width>700)await page.waitForFunction(()=>document.querySelector('.gchat-focus-label')?.textContent==='Coordinate launch');
  return {browser,page,errors};
}

browserTest('bottom pane follows the chat agent and sits between the feed and composer',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    assert.equal(await page.locator('.gchat-focus-intent').textContent(),'Check the remaining tasks.');
    const dots=await page.locator('.gchat-working-dots').boundingBox();
    const focus=await page.locator('.wfpanel').boundingBox();
    const focusLabel=await page.locator('.gchat-focus-label').boundingBox();
    const feed=await page.locator('.gchat-scroll').boundingBox();
    const composer=await page.locator('.gchat-composer').boundingBox();
    assert.ok(feed.y+feed.height<=focus.y);
    assert.ok(focus.y+focus.height<=composer.y);
    assert.ok(feed.height>200);
    assert.equal(await page.locator('.gchat-header').count(),0);
    assert.deepEqual(await page.locator('.wfpanel-label').allTextContents(),['Coordinate launch','Review the changes','Check the layout','Share the result','Later task']);
    assert.ok(dots.y+ dots.height<=composer.y);
    assert.ok(Math.abs(dots.y+dots.height/2-(focusLabel.y+focusLabel.height/2))<1);
    assert.ok(dots.x>focusLabel.x+focusLabel.width);
    assert.ok(Math.abs(focus.x+focus.width-(dots.x+dots.width)-15)<1);
    assert.equal(await page.locator('.wfpanel .gchat-working-dots').count(),1);
    assert.equal(await page.locator('.gchat-session').isVisible(),false);
    assert.equal(await page.locator('.gchat-working-tag .identity-avatar').count(),0);
    assert.equal(await page.locator('.gchat-working-dots i').count(),3);
    assert.equal(await page.locator('.gchat-action-text,[data-chat-context]').count(),0);
    assert.deepEqual(await page.evaluate(()=>focusReads.map(read=>[read.agent_id,read.projection])),[['ord','focus']]);
    assert.deepEqual(await page.evaluate(()=>actionReads),[]);
    await page.evaluate(()=>{_agpad.attentionAgentId='atlas';});
    await page.clock.runFor(12000);
    assert.equal(await page.locator('.gchat-focus-label').textContent(),'Coordinate launch');
    assert.equal(await page.evaluate(()=>focusReads.every(read=>read.agent_id==='ord')),true);
    await page.locator('.gchat-composer textarea').fill('Unsent draft');
    await page.evaluate(()=>{working=false;stream.dispatchEvent(new MessageEvent('chat',{data:JSON.stringify({kind:'changed',cursor:1})}));});
    await page.waitForFunction(()=>document.querySelector('.gchat-working-tag').hidden);
    assert.equal(await page.locator('.gchat-context').isVisible(),true);
    assert.equal(await page.locator('.gchat-composer textarea').inputValue(),'Unsent draft');
    await page.evaluate(()=>mount('atlas','global-worker'));
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label')?.textContent==='Verify sources');
    assert.equal(await page.locator('.gchat-focus-intent').textContent(),'Compare the original documents.');
    await page.waitForFunction(()=>document.querySelector('.wfpanel-label')?.textContent==='Compare documents');
    assert.deepEqual(await page.evaluate(()=>actionReads),[]);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('missing or unavailable focus is explicit and an error never retains an old intent',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{attentions.ord={state:'unfocused',focus:null,intent:''};});
    await page.clock.runFor(12000);
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent==='No focus set');
    assert.equal(await page.locator('.gchat-focus-intent').textContent(),'No intent recorded');
    await page.evaluate(()=>{attentions.ord={state:'focused',focus:{unavailable:true},intent:'Must not be shown'};});
    await page.clock.runFor(12000);
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent==='Focus unavailable');
    assert.equal(await page.locator('.gchat-focus-intent').textContent(),'Intent unavailable');
    await page.evaluate(()=>{override=async()=>{throw Object.assign(Error('Denied'),{code:'forbidden'});};});
    await page.clock.runFor(12000);
    assert.equal(await page.locator('.gchat-focus-intent').textContent(),'Intent unavailable');
    assert.equal(await page.locator('.gchat-action-text,[data-chat-context]').count(),0);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('hidden chats stop context polling and switching agents discards late responses',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    const count=await page.evaluate(()=>focusReads.length+todoReads.length+actionReads.length);
    await page.evaluate(()=>{document.querySelector('#pane').hidden=true;});
    await page.clock.runFor(24000);
    assert.equal(await page.evaluate(()=>focusReads.length+todoReads.length+actionReads.length),count);
    await page.evaluate(()=>{working=false;stream.dispatchEvent(new MessageEvent('chat',{data:JSON.stringify({kind:'changed',cursor:1})}));});
    await page.waitForFunction(()=>document.querySelector('.gchat-working-tag').hidden);
    await page.evaluate(()=>{document.querySelector('#pane').hidden=false;});
    assert.equal(await page.locator('.gchat-context').isVisible(),true);
    await page.evaluate(()=>{
      override=(args,options)=>new Promise(resolve=>{window.finishOld=()=>resolve({agent_id:args.agent_id,attention:{state:'focused',focus:{label:'Old focus'},intent:'Old intent'}});});
    });
    await page.clock.runFor(12000);
    await page.waitForFunction(()=>typeof finishOld==='function');
    await page.evaluate(()=>{override=null;mount('atlas','global-worker');});
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label')?.textContent==='Verify sources');
    // The shared read may finish for other consumers; the disposed chat must ignore it.
    await page.evaluate(()=>finishOld());
    assert.equal(await page.locator('.gchat-focus-intent').textContent(),'Compare the original documents.');
    assert.deepEqual(await page.evaluate(()=>actionReads),[]);
    await page.evaluate(()=>mount('learning','learning-applier'));
    const after=await page.evaluate(()=>focusReads.length+todoReads.length+actionReads.length);
    await page.clock.runFor(24000);
    assert.equal(await page.evaluate(()=>focusReads.length+todoReads.length+actionReads.length),after);
    assert.equal(await page.locator('.gchat-context').count(),0);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('long focus text stays readable on a narrow chat and is rendered only as text',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{document.querySelector('#pane').style.width='320px';attentions.ord={state:'focused',focus:{label:'A'.repeat(300)},intent:'<img src=x onerror=alert(1)> '+ 'A long recorded intent. '.repeat(35)};});
    await page.clock.runFor(12000);
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent.length===300);
    assert.equal(await page.locator('.gchat-context img').count(),0);
    assert.match(await page.locator('.gchat-focus-intent').textContent(),/^<img/);
    assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true);
    const box=await page.locator('.gchat-context').boundingBox();
    assert.ok(box.height<=180);
    assert.ok((await page.locator('.wfpanel').boundingBox()).height<=260);
    const composer=await page.locator('.gchat-composer').boundingBox();
    assert.ok(composer.y+composer.height<=650);
    await page.locator('.gchat-context').focus();await page.keyboard.press('End');
    assert.ok(await page.locator('.gchat-context').evaluate(el=>el.scrollHeight>el.clientHeight));
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('focus paints while chat history is pending and remounts immediately from the shared snapshot',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      controller.dispose();uiReads.invalidate('agent-focus:',{drop:true});
      api=()=>new Promise(()=>{});focusReads=[];mount();
    });
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label')?.textContent==='Coordinate launch');
    assert.equal(await page.evaluate(()=>focusReads.length),1);
    assert.equal(await page.evaluate(()=>{mount();return document.querySelector('.gchat-focus-label').textContent;}),'Coordinate launch');
    assert.equal(await page.evaluate(()=>focusReads.length),1);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('Todo includes current and completed tasks and handles empty and unavailable lists',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      todos.ord=[todoItem('launch','Current task','working'),todoItem('launch','Same ID in another project','open','project'),todoItem('done','Completed task','done'),todoItem('cancelled','Cancelled task','cancelled')];
    });
    await page.clock.runFor(12000);
    await page.waitForFunction(()=>document.querySelector('.wfpanel-label')?.textContent==='Current task');
    assert.deepEqual(await page.locator('.wfpanel-label').allTextContents(),['Current task','Same ID in another project','Completed task','Cancelled task']);
    await page.getByRole('button',{name:'Hide todo',exact:true}).click();
    assert.equal(await page.locator('.wfpanel-steps').isVisible(),false);
    assert.equal(await page.locator('.gchat-focus-label').isVisible(),true);
    await page.keyboard.press('Enter');
    assert.equal(await page.locator('.wfpanel-steps').isVisible(),true);
    await page.evaluate(()=>{todos.ord=[];});
    await page.clock.runFor(12000);
    await page.waitForFunction(()=>document.querySelector('.wfpanel-empty')?.textContent==='No tasks on Todo.');
    await page.evaluate(()=>{todoOverride=async()=>{throw Error('Unavailable');};});
    await page.clock.runFor(12000);
    await page.waitForFunction(()=>document.querySelector('.wfpanel-empty')?.textContent==='Todo unavailable');
    assert.equal(await page.locator('.gchat-focus-label').textContent(),'Coordinate launch');
    assert.equal(await page.evaluate(()=>todoReads.every(read=>read.agent_id==='ord' && read.projection==='summary' && read.limit===100)),true);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('workflow focus retains its steps and blocker inside the bounded bottom pane',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      attentions.ord={state:'focused',focus:{type:'workflow',id:'release',label:'Release UI'},intent:'Finish the approved release.',workflow:{blocked:true,blocker_reason:'Waiting for review',steps:[
        {label:'Check changes',status:'completed'}, {label:'Review changes',status:'active',current:true},
        ...Array.from({length:12},(_,i)=>({label:'Next step '+i,status:'pending'}))
      ]}};
    });
    await page.clock.runFor(12000);
    await page.waitForFunction(()=>document.querySelector('.wfpanel-list-heading')?.textContent==='Workflow steps');
    assert.equal(await page.locator('.wfpanel-step[aria-current=step]').getAttribute('aria-label'),'Review changes: Blocked');
    assert.equal(await page.locator('.wfpanel-error').textContent(),'Waiting for review');
    assert.equal(await page.locator('.wfpanel-step .ball.done').count(),1);
    assert.equal(await page.locator('.wfpanel-step .ball.blocked').count(),1);
    assert.ok((await page.locator('.wfpanel-label').allTextContents()).includes('Later task'));
    assert.ok((await page.locator('.wfpanel').boundingBox()).height<=260);
    assert.ok(await page.locator('.wfpanel-steps').evaluate(el=>el.scrollHeight>el.clientHeight));
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('all Todo pages remain scrollable within the height limit and refresh preserves scroll',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{todos.ord=Array.from({length:205},(_,i)=>todoItem('task-'+i,'Task '+i));todoReads=[];});
    await page.clock.fastForward(12000);
    await page.waitForFunction(()=>document.querySelectorAll('.wfpanel-label').length===205);
    assert.deepEqual(await page.evaluate(()=>todoReads.map(read=>read.cursor || null)),[null,'100','200']);
    assert.ok((await page.locator('.wfpanel').boundingBox()).height<=260);
    assert.ok((await page.locator('.gchat-scroll').boundingBox()).height>200);
    assert.ok(await page.locator('.wfpanel-steps').evaluate(el=>el.scrollHeight>el.clientHeight));
    await page.locator('.wfpanel-steps').evaluate(el=>{el.scrollTop=el.scrollHeight;});
    const previous=await page.locator('.wfpanel-steps').evaluate(el=>el.scrollTop);
    assert.ok(previous>0);
    await page.evaluate(()=>{todos.ord[204].task.title='Updated last task';});
    await page.clock.fastForward(12000);
    await page.waitForFunction(()=>document.querySelector('.wfpanel-label:last-child') && [...document.querySelectorAll('.wfpanel-label')].at(-1).textContent==='Updated last task');
    assert.equal(await page.locator('.wfpanel-steps').evaluate(el=>el.scrollTop),previous);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('Todo follows the main board groups and keeps server order within each group',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      // The API puts blocked first; completed items arrive newest first.
      todos.ord=[todoItem('blocked','Blocked','blocked'),todoItem('input','Needs input','needs_input'),
        todoItem('working','Working','working'),todoItem('open-1','First open'),todoItem('open-2','Second open'),
        todoItem('waiting','Waiting','waiting'),todoItem('done-new','Recently done','done'),
        todoItem('done-old','Earlier done','done'),todoItem('cancelled','Cancelled','cancelled')];
    });
    await page.clock.fastForward(12000);
    await page.waitForFunction(()=>document.querySelectorAll('.wfpanel-label').length===9);
    assert.deepEqual(await page.locator('.wfpanel-label').allTextContents(),[
      'Working','Needs input','First open','Second open','Blocked','Waiting','Recently done','Earlier done','Cancelled'
    ]);
    await page.evaluate(()=>{taskStateObserve=(_scope,task)=>task.id==='open-2'?{...task,status:'working'}:task;});
    await page.clock.fastForward(12000);
    await page.waitForFunction(()=>document.querySelectorAll('.wfpanel-label')[1]?.textContent==='Second open');
    assert.deepEqual(await page.locator('.wfpanel-label').allTextContents(),[
      'Working','Second open','Needs input','First open','Blocked','Waiting','Recently done','Earlier done','Cancelled'
    ]);
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('mobile hides the entire pane and stops reads; resizing to desktop restores it',async()=>{
  const {browser,page,errors}=await fixture(390);
  try {
    assert.equal(await page.locator('.wfpanel').isVisible(),false);
    assert.equal(await page.locator('.gchat-context').isVisible(),false);
    assert.equal(await page.locator('.gchat-session .gchat-working-tag').isVisible(),true);
    await page.clock.fastForward(24000);
    assert.equal(await page.evaluate(()=>focusReads.length+todoReads.length),0);
    await page.setViewportSize({width:701,height:700});
    await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent==='Coordinate launch');
    assert.equal(await page.locator('.wfpanel').isVisible(),true);
    assert.equal(await page.locator('.wfpanel .gchat-working-tag').isVisible(),true);
    await page.waitForFunction(()=>document.querySelectorAll('.wfpanel-label').length===5);
    await page.locator('.gchat-composer textarea').fill('Keep my draft');
    await page.setViewportSize({width:700,height:700});
    assert.equal(await page.locator('.wfpanel').isVisible(),false);
    assert.equal(await page.locator('.gchat-session .gchat-working-tag').isVisible(),true);
    const reads=await page.evaluate(()=>focusReads.length+todoReads.length);
    await page.clock.fastForward(24000);
    assert.equal(await page.evaluate(()=>focusReads.length+todoReads.length),reads);
    assert.equal(await page.locator('.gchat-composer textarea').inputValue(),'Keep my draft');
    assert.deepEqual(errors,[]);
  } finally {await browser.close();}
});

browserTest('Todo resizes vertically by drag and keyboard, persists, and resets without changing width',async()=>{
  const {browser,page,errors}=await fixture();
  try {
    await page.evaluate(()=>{
      const saved=new Map();Object.defineProperty(window,'localStorage',{value:{getItem:key=>saved.get(key) || null,setItem:(key,value)=>saved.set(key,String(value)),removeItem:key=>saved.delete(key)},configurable:true});
    });
    const panel=page.locator('.wfpanel'),grip=page.getByRole('separator',{name:'Resize Todo height'});
    const before=await panel.boundingBox(),handle=await grip.boundingBox();
    await page.mouse.move(handle.x+handle.width/2,handle.y+5);await page.mouse.down();
    await page.mouse.move(handle.x+handle.width/2+80,handle.y-85,{steps:5});await page.mouse.up();
    const grown=await panel.boundingBox();assert.ok(grown.height>before.height+70);assert.equal(grown.width,before.width);
    assert.ok((await page.locator('.gchat-composer').boundingBox()).y+50<=700);
    const saved=await page.evaluate(()=>Number(localStorage.getItem('chat-todo-height')));assert.ok(saved>before.height);
    await page.evaluate(()=>mount());await page.waitForFunction(()=>document.querySelector('.gchat-focus-label').textContent==='Coordinate launch');
    assert.ok(Math.abs((await panel.boundingBox()).height-saved)<1);
    await grip.focus();await page.keyboard.press('ArrowDown');
    assert.ok((await panel.boundingBox()).height<saved-20);
    await page.locator('.wfpanel-head').click();assert.equal(await grip.isVisible(),false);
    assert.ok((await panel.boundingBox()).height<150);
    await page.locator('.wfpanel-head').click();assert.equal(await grip.isVisible(),true);
    await grip.dblclick();assert.equal(await page.evaluate(()=>localStorage.getItem('chat-todo-height')),null);
    assert.ok((await panel.boundingBox()).height<=260);
    await page.setViewportSize({width:390,height:700});assert.equal(await panel.isVisible(),false);
    assert.deepEqual(errors,[]);
  }finally{await browser.close();}
});
