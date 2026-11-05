const assert=require('node:assert/strict'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE || 'playwright');
const root=path.resolve(__dirname,'..');
(async()=>{const browser=await chromium.launch({headless:true,executablePath:process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE});try{
 const page=await browser.newPage({viewport:{width:400,height:700}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.route('http://queue.test/**',r=>r.fulfill({body:'<html></html>',contentType:'text/html'}));
 await page.goto('http://queue.test/');await page.clock.install();await page.setContent('<div id="wsbtn"></div><div id="toasts"></div><section class="attention-queue" style="width:300px"></section><section class="focus-signoff-queue"></section>');
 for(const f of ['core'])await page.addScriptTag({path:path.join(root,'js',f+'.js')});
 await page.addStyleTag({path:path.join(root,'app.css')});await page.addStyleTag({path:path.join(root,'css/agent-focus.css')});
 await page.evaluate(()=>{
  window.items=[{id:'signoff:s:1:h',kind:'signoff',signoff_id:'s',title:'Review mockup',category:'input_request',business_id:null,biz:null,viewed_ids:['signoff:s:1:h']},
   ...[1,2,3,4,5,6,7].map(n=>({id:'dm:'+n,kind:'thread',agent_name:'Agent '+n,agent_id:'a'+n,text:'New reply '+n,source:{type:'task',id:'t'+n},viewed_ids:['dm:'+n]}))];
  window.pendingPageSize=50;window.pendingSignoffs=[{id:'s',title:'Review mockup',state:'pending',category:'input_request',review_business_id:null}];
  window.calls=[];window.failSignoff=false;window.savedViewed=new Set();window.failNextSave=false;
  window.holdNextSave=false;window.holdNextRead=false;window.saveWaiters=[];window.readWaiters=[];window.readsReleased=0;
  window.fetch=async(url,options={})=>{
   const body=options.body&&JSON.parse(options.body);calls.push({url,body});
   if(url.startsWith('/api/root/signoffs?')){const offset=Number(new URL(url,'http://queue.test').searchParams.get('cursor')||0);return new Response(JSON.stringify({status:'ok',data:{items:pendingSignoffs.slice(offset,offset+pendingPageSize),next_cursor:offset+pendingPageSize<pendingSignoffs.length?String(offset+pendingPageSize):null}}));}
   if(url!=='/api/root/attention-queue')throw new Error('Unexpected request: '+url);
   if(body){
    if(failNextSave){failNextSave=false;return new Response(JSON.stringify({status:'error',error:{code:'unavailable',message:'Preference save failed'}}),{status:503});}
    if(holdNextSave){holdNextSave=false;await new Promise((resolve,reject)=>{
     const aborted=()=>reject(new DOMException('Request aborted','AbortError'));
     options.signal?.addEventListener('abort',aborted,{once:true});
     saveWaiters.push(()=>{options.signal?.removeEventListener('abort',aborted);resolve();});
    });}
    body.viewed.forEach(id=>savedViewed.add(id));
   }
   items=items.filter(item=>!savedViewed.has(item.id));
   const snapshot=JSON.stringify({status:'ok',data:{items}});
   if(!body && holdNextRead){holdNextRead=false;await new Promise(resolve=>readWaiters.push(resolve));readsReleased++;}
   return new Response(snapshot);
  };
  window.openCalls=0;window.viewboxCalls=0;window.signoffPaints=0;
  window.replyNotifications={open:async item=>{window.opened=item;openCalls++;}};
  window.agviewboxNavigate=async item=>{window.viewboxOpened=item;viewboxCalls++;};
  window.sgCreateView=options=>({paint(){signoffPaints++;options.body().textContent=failSignoff?'Failed to load':'Review mockup';if(!failSignoff)attentionQueue.signoffViewed({id:'s',proposal_revision:1,payload_hash:'h'});},dispose(){}});
 });
 await page.addScriptTag({path:path.join(root,'js/attention-queue.js')});await page.evaluate(()=>{window.disposeQueue=attentionQueue.mount(document.querySelector('.attention-queue'));window.disposeSignoffs=attentionQueue.mountSignoffs(document.querySelector('.focus-signoff-queue'));});
 await page.waitForFunction(()=>document.querySelectorAll('.attention-chip').length===6);
 assert.equal(await page.locator('.attention-more').textContent(),'+1');
 assert.equal(await page.locator('.attention-queue .attention-label:visible').count(),0,'queue previews are icon-only');
 assert.ok(await page.locator('.attention-chip').first().evaluate(el=>el.getBoundingClientRect().width<=54),'six tiles shrink to fit a narrow pane');
 assert.equal(await page.locator('.attention-queue').evaluate(el=>el.getBoundingClientRect().height),64);
 assert.ok(await page.locator('.attention-stack').evaluate(el=>{const c=[...el.querySelectorAll('.attention-chip')].map(x=>x.getBoundingClientRect());return c.slice(1).every((x,i)=>x.left-c[i].right>=6)}),'chips have gaps');
 assert.ok(await page.locator('.attention-queue').evaluate(el=>el.scrollWidth<=el.clientWidth));
 assert.equal(await page.locator('.attention-queue [data-attention-key^="signoff:"]').count(),0,'Signoffs have their own queue');
 await page.evaluate(()=>failSignoff=true);await page.getByRole('button',{name:'Review Review mockup',exact:true}).click();
 assert.equal(await page.evaluate(()=>calls.filter(c=>c.body).length),0,'failed signoff load is not acknowledged');
 await page.getByRole('button',{name:'Close',exact:true}).click();
 await page.evaluate(()=>failSignoff=false);await page.getByRole('button',{name:'Review Review mockup',exact:true}).click();
 await page.waitForFunction(()=>!items.some(i=>i.kind==='signoff'));
 assert.deepEqual(await page.evaluate(()=>calls.find(c=>c.body).body),{viewed:['signoff:s:1:h']});
 await page.getByRole('button',{name:'Close',exact:true}).click();
 assert.equal(await page.locator('[data-signoff-key="s"]').count(),1,'viewing a pending Signoff does not remove its review control');
 await page.locator('.attention-chip').first().click();
 assert.equal(await page.evaluate(()=>opened.agent_id),'a1','opens exact peer');
 assert.equal(await page.evaluate(()=>calls.filter(c=>c.body).length),1,'thread relies on successful native read, not clicking');
 await page.locator('.attention-more').click();assert.equal(await page.locator('.attention-all .attention-chip').count(),7);
 await page.evaluate(async()=>{items.push({id:'global-1',kind:'global',chat:true,agent_id:'marketing',agent_name:'Marketing',text:'Selected result',viewed_ids:['global-1']});await attentionQueue.refresh();});
 await page.locator('.attention-all').getByRole('button',{name:'Marketing · Selected result',exact:true}).click();
 assert.equal(await page.locator('.modal').count(),0,'overflow closes before opening global chat');
 assert.equal(await page.evaluate(()=>opened.agent_id),'marketing');
 await page.evaluate(async()=>{items.push({id:'viewbox:preview:2',kind:'viewbox',agent_id:'dev',agent_name:'Dev',text:'Preview',viewbox_item_id:'preview',viewed_ids:['viewbox:preview:2']});await attentionQueue.refresh();});
 await page.locator('.attention-more').click();
 await page.locator('.attention-all').getByRole('button',{name:'Dev · Preview',exact:true}).click();
 assert.equal(await page.locator('.modal').count(),0,'overflow closes before opening Viewbox');
 assert.deepEqual(await page.evaluate(()=>({agent:viewboxOpened.agent_id,id:viewboxOpened.viewbox_item_id})),{agent:'dev',id:'preview'});
 assert.equal(await page.evaluate(()=>calls.filter(c=>c.body).length),1,'navigation alone does not acknowledge the item');
 await page.evaluate(()=>attentionQueue.viewboxViewed({attention_id:'viewbox:preview:2'}));
 await page.waitForFunction(()=>!items.some(i=>i.kind==='viewbox'));
 assert.deepEqual(await page.evaluate(()=>calls.filter(c=>c.body).at(-1).body),{viewed:['viewbox:preview:2']});
 await page.evaluate(()=>attentionQueue.viewboxViewed({attention_id:'viewbox:preview:2'}));
 assert.equal(await page.evaluate(()=>calls.filter(c=>c.body).length),2,'repeat loads are acknowledged only once');

 const queued=(id,kind='thread',extra={})=>({id,kind,agent_id:'a-'+id,agent_name:'Agent '+id,text:'Reply '+id,source:{type:'task',id:'task-'+id},viewed_ids:[id],...extra});
 const seed=async rows=>{
  await page.evaluate(async rows=>{items=rows;await attentionQueue.refresh();},rows);
  await page.waitForFunction(id=>document.querySelector(`[data-attention-key="${CSS.escape(id)}"]`),rows[0].id);
 };
 const byKey=(id,scope='.attention-queue')=>page.locator(scope).locator(`[data-attention-key="${id}"]`);
 const navigation=()=>page.evaluate(()=>({chat:openCalls,viewbox:viewboxCalls,signoff:signoffPaints,hash:location.hash}));
 const dismissSignoff=queued('signoff:dismiss:1:h','signoff',{signoff_id:'dismiss',title:'Dismiss without deciding'});
 const grouped=queued('dm:group:2','thread',{viewed_ids:['dm:group:1','dm:group:2']});
 const placement=queued('viewbox:dismiss:1','viewbox',{viewbox_item_id:'dismiss-preview'});
 await seed([... [1,2,3,4,5].map(n=>queued('keep-'+n)),grouped,placement,dismissSignoff]);
 const navigationBefore=await navigation(),writesBefore=await page.evaluate(()=>calls.filter(c=>c.body).length);
 assert.equal(await byKey(dismissSignoff.id).count(),0,'Signoffs cannot enter or be dismissed from the attention queue');
 await page.locator('.attention-more').click();
 assert.equal(await byKey(placement.id,'.attention-all').locator('.attention-viewbox svg').count(),1,'Viewbox uses a document icon');
 await byKey(grouped.id,'.attention-all').click({button:'right'});
 await page.waitForFunction(id=>!document.querySelector(`[data-attention-key="${id}"]`),grouped.id);
 assert.deepEqual(await page.evaluate(()=>calls.filter(c=>c.body).at(-1).body),{viewed:grouped.viewed_ids},'dismisses the complete displayed group');
 await byKey(placement.id,'.attention-all').click({button:'right'});
 await page.waitForFunction(id=>!document.querySelector(`[data-attention-key="${id}"]`),placement.id);
 assert.equal(await page.locator('.modal').count(),1,'dismissing an overflow item keeps the list open');
 assert.deepEqual(await navigation(),navigationBefore,'contextmenu never navigates, loads a Signoff, or opens a conversation');
 const dismissWrites=await page.evaluate(start=>calls.filter(c=>c.body).slice(start),writesBefore);
 assert.equal(dismissWrites.length,2);
 assert(dismissWrites.every(call=>call.url==='/api/root/attention-queue'&&Object.keys(call.body).join()==='viewed'),'only queue preferences are written');
 await page.getByRole('button',{name:'Close',exact:true}).click();
 // The server's saved IDs still suppress the same candidates after refresh/remount;
 // the next reply version is independent and remains visible.
 const nextReply=queued('dm:group:3');
 await page.evaluate(async rows=>{items=rows;disposeQueue();disposeQueue=attentionQueue.mount(document.querySelector('.attention-queue'));},[dismissSignoff,grouped,placement,nextReply]);
 await page.waitForFunction(()=>document.querySelectorAll('.attention-chip').length===1);
 assert.equal(await page.locator('.attention-chip').getAttribute('data-attention-key'),nextReply.id);
 assert.deepEqual(await page.evaluate(ids=>ids.map(id=>savedViewed.has(id)),[dismissSignoff.id,...grouped.viewed_ids,placement.id]),[false,true,true,true]);

 await seed([queued('failure')]);
 await page.evaluate(()=>failNextSave=true);
 await byKey('failure').click({button:'right'});
 await page.getByText('Could not clear attention: Preference save failed',{exact:true}).waitFor();
 assert.equal(await byKey('failure').count(),1,'failed dismissal stays available for retry');
 assert.equal(await page.evaluate(()=>savedViewed.has('failure')),false);
 assert.deepEqual(await navigation(),navigationBefore);
 await byKey('failure').click({button:'right'});
 await page.waitForFunction(()=>!document.querySelector('[data-attention-key="failure"]'));

 await seed([queued('parallel-a'),queued('parallel-b'),queued('remaining')]);
 const priorWrites=await page.evaluate(()=>calls.filter(c=>c.body).length);
 assert.equal(await page.evaluate(()=>{
  holdNextSave=true;const tile=document.querySelector('[data-attention-key="parallel-a"]');
  tile.querySelector('.identity-avatar').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}));
  return !!document.querySelector('[data-attention-key="parallel-a"]');
 }),false,'right-clicking the icon removes the tile immediately, before the save responds');
 await page.waitForFunction(()=>saveWaiters.length===1);
 await page.evaluate(()=>{
  attentionQueue.viewboxViewed({attention_id:'parallel-a'});
  document.querySelector('[data-attention-key="parallel-b"]').dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true}));
  void attentionQueue.refresh();
 });
 assert.equal(await page.locator('.attention-chip').count(),1,'other dismissals also clear immediately while their saves wait');
 assert.equal(await page.evaluate(()=>calls.filter(c=>c.body).length),priorWrites+1,'repeated dismissal is deduplicated and different saves are serialized');
 await page.evaluate(()=>saveWaiters.shift()());
 await page.waitForFunction(()=>savedViewed.has('parallel-a')&&savedViewed.has('parallel-b'));
 await page.waitForFunction(()=>document.querySelectorAll('.attention-chip').length===1&&document.querySelector('[data-attention-key="remaining"]'));
 assert.deepEqual(await page.evaluate(start=>calls.filter(c=>c.body).slice(start).map(c=>c.body.viewed),priorWrites),[['parallel-a'],['parallel-b']]);
 await page.evaluate(()=>{holdNextRead=true;void attentionQueue.refresh();});
 await page.waitForFunction(()=>readWaiters.length===1);
 await byKey('remaining').click({button:'right'});
 await page.waitForFunction(()=>!document.querySelector('[data-attention-key="remaining"]'));
 await page.evaluate(()=>readWaiters.shift()());
 await page.waitForFunction(()=>readsReleased===1);
 assert.equal(await page.locator('.attention-chip').count(),0,'a stale poll cannot restore a dismissed alert');

 await seed([queued('timeout'),queued('after-timeout')]);
 await page.evaluate(()=>holdNextSave=true);
 await byKey('timeout').click({button:'right'});
 await page.waitForFunction(()=>saveWaiters.length===1);
 await byKey('after-timeout').click({button:'right'});
 assert.equal(await page.locator('.attention-chip').count(),0,'queued saves never hold dismissed tiles on screen');
 await page.clock.fastForward(15000);
 await page.getByText('Could not clear attention: Save timed out. Right-click again to retry.',{exact:true}).waitFor();
 await page.waitForFunction(()=>savedViewed.has('after-timeout'));
 assert.equal(await byKey('timeout').count(),1,'timed-out dismissals return for retry');
 assert.equal(await byKey('after-timeout').count(),0,'a stalled request no longer blocks later dismissals');
 assert.equal(await page.evaluate(()=>savedViewed.has('timeout')),false);
 await page.evaluate(()=>saveWaiters.shift()());
 await byKey('timeout').click({button:'right'});
 await page.waitForFunction(()=>savedViewed.has('timeout'));
 assert.deepEqual(await navigation(),navigationBefore,'dismissals and retries never navigate or resolve a Signoff');

 await seed([queued('current-chat','global',{chat:true,agent_id:'current-agent'})]);
 await page.evaluate(()=>{
  const chat=document.createElement('section');chat.dataset.globalChat='current-agent';chat.id='current-chat-fixture';
  chat.innerHTML='<button class="gchat-jump" hidden>New messages</button><form class="gchat-composer"><textarea>Unsent draft</textarea></form>';
  window.chatJumps=0;chat.querySelector('.gchat-jump').onclick=()=>chatJumps++;document.body.append(chat);
 });
 const beforeSameChat=await page.evaluate(()=>openCalls);
 await byKey('current-chat').click();
 assert.equal(await page.evaluate(()=>openCalls),beforeSameChat,'already-mounted matching chat is not navigated or remounted');
 assert.equal(await page.evaluate(()=>chatJumps),1,'existing jump handler reveals the latest reply');
 assert.equal(await page.locator('#current-chat-fixture textarea').inputValue(),'Unsent draft');
 assert.equal(await page.locator('#current-chat-fixture textarea').evaluate(el=>el===document.activeElement),true);
 await page.locator('#current-chat-fixture').evaluate(el=>el.hidden=true);
 await byKey('current-chat').click();
 assert.equal(await page.evaluate(()=>openCalls),beforeSameChat+1,'a hidden matching chat still uses normal navigation');
 await page.locator('#current-chat-fixture').evaluate(el=>el.remove());

 // Peer conversations about one Task stay distinct without canonical ids; explicit differing thread ids stay distinct; same ids join.
 await seed([queued('peer-a','thread',{agent_id:'peer-a',source:{type:'task',id:'shared-task',business_id:'biz'}}),queued('peer-b','thread',{agent_id:'peer-b',source:{type:'task',id:'shared-task',business_id:'biz'}}),
  queued('canon-1','thread',{agent_id:'peer-a',thread_id:'t1',source:{type:'task',id:'shared-task',business_id:'biz'}}),queued('canon-2','thread',{agent_id:'peer-a',thread_id:'t2',source:{type:'task',id:'shared-task',business_id:'biz'}}),
  queued('canon-1b','thread',{agent_id:'peer-b',thread_id:'t1',source:{type:'task',id:'shared-task',business_id:'biz'}}),queued('no-agent-1','global',{agent_id:null}),queued('no-agent-2','global',{agent_id:null})]);
 assert.deepEqual(await page.locator('.attention-queue .attention-chip').evaluateAll(els=>els.map(el=>el.dataset.attentionKey).sort()),['canon-1','canon-2','no-agent-1','no-agent-2','peer-a','peer-b'],'peers, canonical threads and agentless items stay distinct');
 const mainReplies=[3,2,1].map(n=>queued('same-chat-'+n,'global',{chat:n===1,agent_id:'same-agent'}));
 const taskReplies=[2,1].map(n=>queued('same-thread-'+n,'thread',{agent_id:'same-agent',source:{type:'task',id:'same-task',business_id:'biz'}}));
 const otherTask=queued('different-thread','thread',{agent_id:'same-agent',source:{type:'task',id:'different-task',business_id:'biz'}});
 await seed([...mainReplies,...taskReplies,otherTask,...[1,2,3,4,5].map(n=>queued('unique-'+n)),queued('signoff-thread','thread',{source:{type:'signoff',id:'s'}})]);
 assert.equal(await page.locator('.attention-queue .attention-chip').count(),6,'six unique destinations are visible');
 assert.equal(await page.locator('.attention-more').textContent(),'+2','overflow counts destinations rather than individual messages');
 assert.equal(await byKey('same-chat-2').count(),0);
 assert.equal(await byKey('same-thread-1').count(),0);
 await byKey('same-thread-2').click();
 assert.deepEqual(await page.evaluate(()=>({agent:opened.agent_id,source:opened.source.id})),{agent:'same-agent',source:'same-task'});
 await byKey('same-chat-3').click();
 assert.deepEqual(await page.evaluate(()=>({id:opened.id,chat:opened.chat})),{id:'same-chat-3',chat:true},'newest preview opens the native conversation');
 await byKey('same-chat-3').click({button:'right'});
 await page.waitForFunction(()=>!document.querySelector('[data-attention-key="same-chat-3"]'));
 assert.deepEqual(await page.evaluate(()=>calls.filter(c=>c.body).at(-1).body),{viewed:mainReplies.map(item=>item.id)},'dismiss the complete deduplicated destination');
 await byKey('same-thread-2').click({button:'right'});
 await page.waitForFunction(()=>!document.querySelector('[data-attention-key="same-thread-2"]'));
 assert.deepEqual(await page.evaluate(()=>calls.filter(c=>c.body).at(-1).body),{viewed:taskReplies.map(item=>item.id)});
 assert.equal(await byKey('different-thread').count(),1,'another thread with that agent keeps its own destination');
 assert.equal(await page.locator('.attention-more').count(),0,'exactly six destinations need no overflow');
 await page.evaluate(async()=>{items.push({id:'overflow-extra',kind:'global',chat:true,agent_id:'extra',agent_name:'Extra',text:'New reply'});await attentionQueue.refresh();});
 await page.addStyleTag({path:path.join(root,'css/agent-workspace.css')});
 await page.evaluate(()=>{
  document.body.classList.add('agent-workspace-app');
  const queue=document.querySelector('.attention-queue'),board=document.createElement('div');board.className='agfocus';board.id='scaled-queue-fixture';
  queue.replaceWith(board);board.append(queue);
 });
 assert.equal(await page.locator('.attention-queue').evaluate(el=>getComputedStyle(el).zoom),'1.1');
 assert.ok(await page.locator('.attention-queue').evaluate(el=>el.scrollWidth<=el.clientWidth),'six scaled tiles fit a narrow Focus pane');
 await page.evaluate(()=>{document.body.classList.remove('agent-workspace-app');const board=document.querySelector('#scaled-queue-fixture');board.replaceWith(board.firstElementChild);});

 await page.evaluate(async()=>{
  pendingPageSize=1;pendingSignoffs=[{id:'page-one',title:'First question',state:'pending',review_business_id:null},{id:'page-two',title:'Second question',state:'pending',review_business_id:'biz'}];
  await attentionQueue.refreshSignoffs();
 });
 assert.equal(await page.locator('.focus-signoff-chip').count(),2,'Signoffs include later pages and business proposals');
 await page.locator('.focus-signoff-chip').last().focus();await page.evaluate(()=>attentionQueue.paintSignoffs());
 assert.equal(await page.locator('.focus-signoff-chip').last().evaluate(el=>el===document.activeElement),true,'unchanged queue preserves focus');
 await page.evaluate(async()=>{pendingSignoffs=[];await attentionQueue.refreshSignoffs();});
 assert.equal(await page.locator('.focus-signoff-queue').isVisible(),false,'resolved queue leaves no empty label or row');
 await page.addStyleTag({path:path.join(root,'css/tasks.css')});
 await page.addStyleTag({path:path.join(root,'css/agentpane.css')});
 await page.evaluate(()=>{
  const pane=document.createElement('section');pane.className='tasks agpad';pane.id='scroll-fixture';
  pane.style.cssText='position:static;display:flex;flex-direction:column;height:360px;width:320px;transform:none;opacity:1;visibility:visible';
  pane.innerHTML='<div class="thead"><div class="ttabs"><button class="ttab">Tab</button></div></div><div class="tbody"><div class="agfocus"><header>Focus</header><section class="attention-queue">Queue</section><div class="agfocus-layout"><div class="agfocus-items">'+Array.from({length:60},(_,i)=>'<div style="height:32px">Task '+i+'</div>').join('')+'</div></div></div></div>';
  document.body.append(pane);
 });
 const before=await page.locator('#scroll-fixture .attention-queue').evaluate(el=>el.getBoundingClientRect().top);
 await page.locator('#scroll-fixture .agfocus-layout').evaluate(el=>el.scrollTop=500);
 const scroll=await page.locator('#scroll-fixture').evaluate(el=>({list:el.querySelector('.agfocus-layout').scrollTop,body:el.querySelector('.tbody').scrollTop,queue:el.querySelector('.attention-queue').getBoundingClientRect().top}));
 assert.ok(scroll.list>0,'Focus work list scrolls');assert.equal(scroll.body,0);assert.equal(scroll.queue,before,'attention stays fixed');
 assert.equal(await page.locator('#scroll-fixture .ttabs').evaluate(el=>getComputedStyle(el).marginBottom),'4px');
 await page.addScriptTag({path:path.join(root,'js/agent-focus.js')});
 await page.evaluate(()=>{
  const board=document.querySelector('#scroll-fixture .agfocus'),list=board.querySelector('.agfocus-items');
  list.querySelectorAll('div').forEach((row,i)=>{row.className='agfocus-card';row.dataset.cardId='row-'+i;row._item={};row.innerHTML='<div class="agfocus-row">Task '+i+'</div>';});
  board.querySelector('.agfocus-layout').scrollTop=0;
  window.disposeOrder=agfocusOrderMount({},board,board.querySelector('.agfocus-layout'));
 });
 await page.locator('#scroll-fixture').scrollIntoViewIfNeeded();
 const grip=await page.locator('#scroll-fixture .agfocus-row').first().boundingBox(),area=await page.locator('#scroll-fixture .agfocus-layout').boundingBox();
 await page.mouse.move(grip.x+grip.width/2,grip.y+grip.height/2);await page.mouse.down();await page.clock.runFor(129);
 await page.mouse.move(grip.x+grip.width/2,area.y+area.height-5,{steps:4});
 await page.waitForFunction(()=>document.querySelector('#scroll-fixture .agfocus-layout').scrollTop>0);
 await page.keyboard.press('Escape');await page.mouse.up();await page.evaluate(()=>disposeOrder());
 assert.deepEqual(errors,[]);console.log('Attention queue layout, navigation, dismissal persistence/failure, and concurrent save/stale poll checks passed');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exit(1);});
