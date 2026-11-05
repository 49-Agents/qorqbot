// Reply-notification and Attention refresh coalescing. Synthetic fetch fixtures and a paused fake clock:
// timers fire only through page.clock.runFor, held responses are armed and released explicitly, and no
// expected count depends on wall-clock time.
const assert=require('node:assert/strict'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
const root=path.resolve(__dirname,'..');
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE,args:['--no-sandbox']});try{
 const page=await browser.newPage(),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://refresh.test/**',r=>r.fulfill({body:'<html></html>',contentType:'text/html'}));
 await page.goto('http://refresh.test/');
 await page.clock.install();await page.clock.pauseAt(new Date(Date.now()+1000));
 await page.setContent('<div id="wsbtn"></div><div id="toasts" class="toasts"></div><section class="attention-queue"></section><section class="focus-signoff-queue"></section><button data-global-agent="a1"><span class="nm">A1</span></button>');
 for(const f of ['core'])await page.addScriptTag({path:path.join(root,'js',f+'.js')});
 await page.addStyleTag({path:path.join(root,'app.css')});
 await page.evaluate(()=>{
  window.replies=[];window.queue=[];window.signoffRows=[1,2].map(n=>({id:'s'+n,title:'Review '+n,state:'pending',category:'input_request',review_business_id:null}));
  window.counts={reply:0,queue:0,signoffs:0};window.active={reply:0,queue:0,signoffs:0};window.maxActive={reply:0,queue:0,signoffs:0};
  window.armed={};window.arrival={};window.release={};window.fail={reply:false,queue:false};window.changed=0;window.hiddenTab=false;
  Object.defineProperty(document,'hidden',{configurable:true,get:()=>hiddenTab});
  const kindOf=url=>url==='/api/root/reply-notifications'?'reply':url==='/api/root/attention-queue'?'queue':url.startsWith('/api/root/signoffs?')?'signoffs':null;
  window.fetch=async url=>{
   const kind=kindOf(url);if(!kind)throw new Error('Unexpected request: '+url);
   counts[kind]++;active[kind]++;maxActive[kind]=Math.max(maxActive[kind],active[kind]);
   try{
    if(fail[kind])return new Response(JSON.stringify({status:'error',error:{code:'unavailable',message:'down'}}),{status:503});
    let data;
    if(kind==='signoffs'){const offset=Number(new URL(url,'http://refresh.test').searchParams.get('cursor')||0);data={items:signoffRows.slice(offset,offset+1),next_cursor:offset+1<signoffRows.length?String(offset+1):null};}
    else data={items:JSON.parse(JSON.stringify(kind==='reply'?replies:queue))};
    const snapshot=JSON.stringify({status:'ok',data});
    // A held read has already taken its snapshot: later data can only arrive through a follow-up read.
    if(armed[kind]){const arrive=armed[kind];armed[kind]=null;arrive();await new Promise(resolve=>{release[kind]=resolve;});}
    return new Response(snapshot);
   }finally{active[kind]--;}
  };
  // Arm synchronously; arrival[kind] resolves when the next read of that kind reaches fetch.
  window.hold=kind=>{arrival[kind]=new Promise(resolve=>{armed[kind]=resolve;});};
  // Let queued microtasks and message-channel tasks run (real tasks, independent of the paused clock).
  window.settle=async()=>{for(let tick=0;tick<25;tick++)await new Promise(r=>{const c=new MessageChannel();c.port1.onmessage=r;c.port2.postMessage(0);});};
  document.addEventListener('reply-notifications-changed',()=>changed++);
 });
 for(const f of ['reply-notifications','attention-queue'])await page.addScriptTag({path:path.join(root,'js',f+'.js')});
 const get=expr=>page.evaluate(expr),settle=()=>page.evaluate(()=>settle());
 const count=kind=>page.evaluate(k=>counts[k],kind);
 const run=async ms=>{await page.clock.runFor(ms);await settle();};

 // Initial load dispatches once and paints.
 await page.evaluate(()=>{replies=[{id:'dm:1',kind:'thread',source:{type:'task',id:'t1'},agent_id:'a1',agent_name:'A1',text:'Hi',unread:true}];queue=[{id:'dm:1',kind:'thread',agent_id:'a1',agent_name:'A1',text:'Hi',source:{type:'task',id:'t1'},viewed_ids:['dm:1']}];});
 await page.evaluate(()=>replyNotifications.start());
 assert.equal(await get(()=>changed),1,'initial load dispatches');
 assert.equal(await page.locator('.chat-unread-dot').count(),1);
 await page.evaluate(()=>{window.disposeQueue=attentionQueue.mount(document.querySelector('.attention-queue'));});
 await settle();
 assert.equal(await page.locator('.attention-chip').count(),1);
 assert.equal(await count('queue'),1);

 // Unchanged repeated replies (including reordered keys) neither dispatch nor reach Attention.
 await page.evaluate(async()=>{replies=[Object.fromEntries(Object.entries(replies[0]).reverse())];for(let i=0;i<3;i++)await replyNotifications.refresh();});
 await settle();
 assert.equal(await get(()=>changed),1,'unchanged data does not dispatch');
 assert.equal(await count('queue'),1,'unchanged replies cause no Attention request');
 await page.evaluate(()=>{document.body.insertAdjacentHTML('beforeend','<button data-global-agent="a1" id="late"><span class="nm">Late</span></button>');replyNotifications.paint();});
 assert.equal(await page.locator('#late .chat-unread-dot').count(),1,'paint serves newly mounted navigation');

 // A field-only change dispatches and refreshes Attention once.
 await page.evaluate(async()=>{replies[0].text='Changed';await replyNotifications.refresh();});
 await settle();
 assert.equal(await get(()=>changed),2,'field change dispatches');
 assert.equal(await count('queue'),2);

 // A scheduled timer firing while a read is held adds no trailing read.
 let before=await count('reply');
 await page.evaluate(()=>{hold('reply');window.held=replyNotifications.refresh();});
 await page.evaluate(()=>arrival.reply);
 await run(10050);
 assert.equal(await count('reply'),before+1,'timer during a held read starts nothing');
 await page.evaluate(()=>{release.reply();return held;});
 await settle();
 assert.equal(await count('reply'),before+1,'timer noise leaves no trailing read');
 assert.equal(await get(()=>maxActive.reply),1);

 // A burst during one read: one active plus exactly one trailing read, which sees the late update.
 before=await count('reply');
 await page.evaluate(()=>{hold('reply');window.pending=[replyNotifications.refresh()];});
 await page.evaluate(()=>arrival.reply);
 await page.evaluate(()=>{for(let i=0;i<5;i++)pending.push(replyNotifications.refresh());});
 assert.equal(await count('reply'),before+1,'burst starts nothing while the read is held');
 await page.evaluate(()=>{replies=[...replies,{id:'dm:2',kind:'thread',source:{type:'task',id:'t2'},agent_id:'a2',agent_name:'A2',text:'Late',unread:true}];release.reply();return Promise.all(pending);});
 assert.equal(await count('reply'),before+2,'burst coalesces to one active and one trailing request');
 assert.equal(await get(()=>replyNotifications.unreadAgent('a2')),true,'update during a read is shown');
 assert.equal(await get(()=>maxActive.reply),1,'requests never overlap');

 // Cadence: 30 s hidden, no refresh while hidden, one refresh when visible again, no stale duplicate timer.
 await page.evaluate(()=>{hiddenTab=true;return replyNotifications.refresh();});
 before=await count('reply');
 await run(29900);assert.equal(await count('reply'),before,'hidden cadence has not elapsed');
 await run(200);assert.equal(await count('reply'),before+1,'hidden cadence is 30 s');
 await page.evaluate(()=>document.dispatchEvent(new Event('visibilitychange')));await settle();
 assert.equal(await count('reply'),before+1,'a hidden visibility event refreshes nothing');
 await page.evaluate(()=>{hiddenTab=false;document.dispatchEvent(new Event('visibilitychange'));});await settle();
 assert.equal(await count('reply'),before+2,'visible again refreshes once');
 await run(10050);
 assert.equal(await count('reply'),before+3,'visible cadence is 10 s with a single timer');
 await run(10050);
 assert.equal(await count('reply'),before+4);

 // Failed polls retain the last known state and still retry.
 await page.evaluate(async()=>{fail.reply=true;await replyNotifications.refresh();});
 assert.equal(await get(()=>replyNotifications.unreadAgent('a2')),true,'failure retains state');
 before=await count('reply');await run(10050);
 assert.equal(await count('reply'),before+1,'failed poll retries on the timer');
 await page.evaluate(()=>{fail.reply=false;});

 // Attention: a timer and a burst of triggers during one held read = one active + one trailing read.
 await run(10050);
 let q0=await count('queue');
 await page.evaluate(()=>{hold('queue');window.pending=[attentionQueue.refresh()];});
 await page.evaluate(()=>arrival.queue);
 await run(10050);
 assert.equal(await count('queue'),q0+1,'timer during a held Attention read starts nothing');
 await page.evaluate(()=>{for(let i=0;i<4;i++){pending.push(attentionQueue.refresh());window.dispatchEvent(new Event('focus'));document.dispatchEvent(new Event('reply-notifications-changed'));}});
 assert.equal(await count('queue'),q0+1,'burst starts nothing while the read is held');
 await page.evaluate(()=>{queue=[...queue,{id:'dm:9',kind:'thread',agent_id:'a9',agent_name:'A9',text:'During read',source:{type:'task',id:'t9'},viewed_ids:['dm:9']}];release.queue();return Promise.all(pending);});
 await settle();
 assert.equal(await count('queue'),q0+2,'attention burst coalesces');
 assert.equal(await get(()=>maxActive.queue),1);
 assert.equal(await page.locator('[data-attention-key="dm:9"]').count(),1,'attention update during read is shown');

 // Failed Attention poll keeps items and retries on the timer.
 await page.evaluate(async()=>{fail.queue=true;await attentionQueue.refresh();});
 assert.equal(await page.locator('[data-attention-key="dm:9"]').count(),1);
 assert.equal(await page.locator('[data-attention-retry]').count(),1);
 q0=await count('queue');await run(10050);
 assert.equal(await count('queue'),q0+1,'failed Attention poll retries');
 await page.evaluate(()=>{fail.queue=false;});

 // Pending Signoffs: a held paginated read plus focus/refresh bursts = one active cycle and one follow-up cycle.
 await run(10050);
 let s0=await count('signoffs');
 await page.evaluate(()=>{hold('signoffs');window.disposeSignoffs=attentionQueue.mountSignoffs(document.querySelector('.focus-signoff-queue'));});
 await page.evaluate(()=>arrival.signoffs);
 await page.evaluate(()=>{window.pending=[];for(let i=0;i<3;i++){pending.push(attentionQueue.refreshSignoffs());window.dispatchEvent(new Event('focus'));}});
 assert.equal(await count('signoffs'),s0+1,'Signoff burst starts nothing while page one is held');
 await page.evaluate(()=>{release.signoffs();return Promise.all(pending);});
 await settle();
 assert.equal(await count('signoffs'),s0+4,'one active and one follow-up cycle of two pages each');
 assert.equal(await get(()=>maxActive.signoffs),1,'Signoff reads never overlap');
 assert.equal(await page.locator('[data-signoff-key]').count(),2,'both pages are shown');
 // The pending 60 s timer comes due while an explicitly started paginated cycle is held: no extra request, no trailing cycle.
 await page.evaluate(()=>attentionQueue.refreshSignoffs());
 s0=await count('signoffs');
 await run(59900);
 assert.equal(await count('signoffs'),s0,'the settled Signoff timer is not due yet');
 await page.evaluate(()=>{hold('signoffs');window.held=attentionQueue.refreshSignoffs();});
 await page.evaluate(()=>arrival.signoffs);
 await run(200);
 assert.equal(await count('signoffs'),s0+1,'old Signoff timer deadline during a held page starts nothing');
 await page.evaluate(()=>{release.signoffs();return held;});
 await settle();
 assert.equal(await count('signoffs'),s0+2,'exactly one two-page cycle completes');
 assert.equal(await get(()=>maxActive.signoffs),1,'Signoff reads never overlap across the timer deadline');
 assert.equal(await page.locator('[data-signoff-key]').count(),2,'both pages are shown after the held cycle');
 s0=await count('signoffs');await run(60050);
 assert.equal(await count('signoffs'),s0+2,'Signoff timer runs one cycle at the 60 s cadence');
 // Unmounting the last Signoff consumer in flight: no follow-up cycle and no later timer.
 s0=await count('signoffs');
 await page.evaluate(()=>{hold('signoffs');window.held=attentionQueue.refreshSignoffs();});
 await page.evaluate(()=>arrival.signoffs);
 await page.evaluate(()=>{attentionQueue.refreshSignoffs();disposeSignoffs();release.signoffs();return held;});
 await settle();
 assert.equal(await count('signoffs'),s0+2,'only the in-flight cycle completes after unmount');
 await run(120000);
 assert.equal(await count('signoffs'),s0+2,'no Signoff timer after the last unmount');

 // Unmounting the last Attention consumer in flight: no queued follow-up and no later timer.
 q0=await count('queue');
 await page.evaluate(()=>{hold('queue');window.held=attentionQueue.refresh();});
 await page.evaluate(()=>arrival.queue);
 await page.evaluate(()=>{attentionQueue.refresh();disposeQueue();release.queue();return held;});
 await settle();
 assert.equal(await count('queue'),q0+1,'unmount in flight drops the queued follow-up');
 await run(60000);
 assert.equal(await count('queue'),q0+1,'no Attention timer after the last unmount');
 assert.deepEqual(errors,[],errors.join('; '));
 console.log('Dashboard refresh coalescing: unchanged replies, field changes, held-read bursts, timers, cadence, Signoffs, failures, unmount passed.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
