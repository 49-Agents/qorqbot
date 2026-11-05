// Reply cards (keyed replacement, timing, pause, click) and navigation presence dots, with a controlled clock.
const assert=require('node:assert/strict'),path=require('node:path');
const {chromium}=require(process.env.PLAYWRIGHT_MODULE||'playwright');
(async()=>{const browser=await chromium.launch({headless:true,args:['--no-sandbox']});try{
 const page=await browser.newPage({viewport:{width:900,height:600}}),root=path.resolve(__dirname,'..');
 await page.route('http://fixture.test/**',route=>route.fulfill({body:'<html></html>',contentType:'text/html'}));
 await page.goto('http://fixture.test/');
 await page.clock.install();
 await page.setContent('<div id="toasts" class="toasts"></div><div id="nav" class="agent-workspace-app"></div>');
 for(const f of ['js/core.js','js/agent-fleet.js','js/global-agents.js','js/reply-notifications.js'])await page.addScriptTag({path:path.join(root,f)});
 await page.addStyleTag({path:path.join(root,'app.css')});
 await page.addStyleTag({path:path.join(root,'css/agent-workspace-nav.css')});
 await page.evaluate(()=>{
  window.opened=[];window.activity=[];window.fail=false;window.requests=0;
  globalAgentProfiles=[{agent_id:'dev',role:{id:'ordubashy'},display_name:'Ordubashy'},{agent_id:'learner',role:{id:'learning-applier'}},
   {agent_id:'atlas',role:{id:'global-worker'},presentation:{icon:'rocket',department:'general'}},{agent_id:'fake',role:{id:'global-worker'},display_name:'Ordubashy'}];
  globalAgentProfilesRequest=Promise.resolve(globalAgentProfiles);
  window.commUrls=[];window.commFail=false;window.commFeed=[{items:[],next_cursor:10,has_more:false}];window.hold=null;
  window.fetch=async url=>{if(String(url).includes('reply-notifications')){commUrls.push(String(url));if(commFail)throw new Error('offline');return new Response(JSON.stringify({status:'ok',data:{items:[],communications:commFeed.shift()}}),{status:200});}
   requests++;if(hold)await hold;if(fail)throw new Error('offline');return new Response(JSON.stringify({status:'ok',data:{agents:activity}}),{status:200});};
 });
 const toasts=()=>page.locator('.toast.reply-toast');
 const reply=(id,text,tag)=>page.evaluate(([id,text,tag])=>notify({replyAgentId:id,text:id,sub:text,ms:6000,onClick:()=>window.opened.push(tag)}),[id,text,tag]);
 // Same agent: visible card updates in place, no duplicate.
 await reply('dev','first','a');await reply('dev','second','b');
 assert.equal(await toasts().count(),1);
 assert.equal(await toasts().locator('.reply-preview').innerText(),'second');
 // Queue: fill three visible, then queue two for one agent -> single pending entry.
 await reply('learner','l','l');await page.evaluate(()=>notify({text:'Plain',onClick:()=>{}}));
 await reply('atlas','x1','x');await reply('atlas','x2','y');
 assert.equal(await page.evaluate(()=>_notes.queue.filter(n=>n.replyAgentId==='atlas').length),1);
 assert.equal(await page.evaluate(()=>_notes.queue.find(n=>n.replyAgentId==='atlas').sub),'x2');
 assert.equal(await page.locator('.toast').count(),3,'max three visible');
 // 6s lifetime, then queued card takes the slot.
 await page.clock.fastForward(5900);assert.equal(await toasts().count(),2);
 await page.clock.fastForward(400);
 await page.clock.fastForward(300);
 assert.equal(await page.locator('.toast.reply-toast .reply-name:text("dev")').count(),0);
 assert.equal(await page.locator('.toast.reply-toast .reply-preview:text("x2")').count(),1,'queued latest preview shown');
 // Hover pauses remaining time; update while hovered stays paused; leave resumes.
 await reply('atlas','x3','z');
 await page.locator('.toast.reply-toast').last().hover();
 await page.clock.fastForward(10000);
 assert.equal(await page.locator('.toast.reply-toast .reply-preview:text("x3")').count(),1,'hover holds the card');
 await page.mouse.move(5,5);await page.clock.fastForward(5900);
 assert.equal(await page.locator('.toast.reply-toast .reply-preview:text("x3")').count(),1);
 await page.clock.fastForward(400);
 await page.clock.fastForward(300);
 assert.equal(await page.locator('.toast.reply-toast .reply-preview:text("x3")').count(),0,'resumes and expires');
 await page.clock.fastForward(1000);
 // Keyboard focus pause + Enter opens the latest destination.
 await page.evaluate(()=>{document.querySelector('#toasts').innerHTML='';_notes.live=0;_notes.queue=[];_notes.replies.clear();});
 await reply('dev','one','old');
 await page.locator('.toast.reply-toast').focus();
 await page.clock.fastForward(20000);assert.equal(await toasts().count(),1,'focus holds the card');
 await reply('dev','two','latest');
 await page.keyboard.press('Enter');
 await page.waitForFunction(()=>window.opened.includes('latest'));
 assert.equal(await page.evaluate(()=>window.opened.filter(x=>x==='old').length),0);
 await page.clock.fastForward(400);
 // After dismissal a new reply creates a fresh card.
 await reply('dev','three','n');assert.equal(await toasts().count(),1);
 // Card geometry and content.
 const box=await toasts().first().evaluate(el=>{const s=getComputedStyle(el),i=el.querySelector('.reply-icon svg').getBoundingClientRect();
  return {w:el.getBoundingClientRect().width,pad:s.padding,rad:s.borderRadius,before:getComputedStyle(el,'::before').content,icon:i.width,text:el.innerText}});
 assert.equal(box.w,328);assert.equal(box.pad,'13px 15px');assert.equal(box.rad,'14px');assert.equal(box.before,'none');assert.equal(box.icon,28);
 assert.equal(box.text.trim().split('\n').length,2);
 await page.setViewportSize({width:300,height:500});
 assert.ok(await toasts().first().evaluate(el=>el.getBoundingClientRect().right<=300));
 // Escaping.
 await reply('dev','<img src=x onerror=1>','e');
 assert.equal(await page.locator('.reply-toast img').count(),0);
 // Generic notification keeps old style.
 await page.evaluate(()=>notify({text:'Plain note'}));
 assert.equal(await page.locator('.toast:not(.reply-toast) .tx').count(),1);

 // ---- Agent-to-agent micro notices
await page.evaluate(()=>{document.querySelector('#toasts').innerHTML='';_notes.live=0;_notes.queue=[];_notes.replies.clear();});
 const event=(id,from,to)=>({event_id:id,id:'m'+id,sender_id:from,recipient_id:to,sender_name:from,recipient_name:to});
 await page.evaluate(()=>replyNotifications.refresh());
 assert.ok(!(await page.evaluate(()=>commUrls[0])).includes('communications_after'),'first request has no cursor');
 assert.equal(await page.locator('.comm-toast').count(),0,'baseline shows nothing');
 await page.evaluate(([a,b,c,d])=>{commFeed.push({items:[a,b],next_cursor:12,has_more:true},{items:[c,d,a],next_cursor:14,has_more:false});},[event(11,'atlas','dev'),event(12,'atlas','dev'),event(13,'dev','learner'),event(14,'fake','atlas')]);
 await page.evaluate(()=>replyNotifications.refresh());
 assert.ok((await page.evaluate(()=>commUrls)).slice(-2).every((u,i)=>u.endsWith('communications_after='+(i?12:10))),'drained both pages with advancing cursor');
 const comm=page.locator('.comm-toast');
 assert.equal(await comm.count(),3,'max three visible; duplicates suppressed');
 assert.equal(await page.evaluate(()=>_notes.queue.filter(n=>n.communication).length),1,'fourth distinct event queued, not folded');
 assert.equal(await page.locator('.toast.reply-toast').count(),0,'no reply popup or badge for workers');
 const first=comm.first();
 assert.equal(await first.getAttribute('aria-label'),'atlas \u2192 dev');
 assert.equal(await first.evaluate(el=>el.innerText.trim()),'\u2192','no visible text beyond the arrow');
 assert.equal(await first.locator('svg').count(),2);
 assert.equal(await first.locator('svg').first().evaluate(el=>el.getBoundingClientRect().width),22);
 const cs=await first.evaluate(el=>{const s=getComputedStyle(el);return [s.padding,s.borderRadius,s.gap]});
 assert.deepEqual(cs,['8px 10px','12px','8px']);
 // 3s lifetime with hover hold.
 await first.hover();await page.clock.fastForward(10000);assert.equal(await comm.count(),3,'hover holds');
 await page.mouse.move(5,5);await page.clock.fastForward(3400);await page.clock.fastForward(300);
 assert.equal(await page.locator('.comm-toast').count(),1,'others expired after 3s and the queued one took a slot');
 await page.clock.fastForward(3400);await page.clock.fastForward(300);
 // Failure keeps the cursor for retry.
 await page.evaluate(()=>{commFail=true;});await page.evaluate(()=>replyNotifications.refresh());
 await page.evaluate(()=>{commFail=false;commFeed.push({items:[],next_cursor:14,has_more:false});});await page.evaluate(()=>replyNotifications.refresh());
 assert.ok((await page.evaluate(()=>commUrls)).slice(-1)[0].endsWith('communications_after=14'));
 // Distinct events for the same pair each produce a notice; generic notifications are untouched.
 await page.evaluate(([a,b])=>commFeed.push({items:[a,b],next_cursor:16,has_more:false}),[event(15,'atlas','dev'),event(16,'atlas','dev')]);
 await page.evaluate(()=>replyNotifications.refresh());
 assert.equal(await page.locator('.comm-toast').count(),2);
 await page.clock.fastForward(4000);await page.clock.fastForward(300);
 await page.evaluate(()=>{for(let i=0;i<12;i++)notify({text:'Plain '+i});});
 assert.ok(await page.evaluate(()=>_notes.queue.some(n=>n.fold)),'generic notes still fold');
 await page.clock.fastForward(60000);await page.clock.fastForward(5000);await page.clock.fastForward(5000);
 await page.evaluate(()=>{document.querySelector('#toasts').innerHTML='';_notes.live=0;_notes.queue=[];_notes.replies.clear();});

 // ---- Navigation presence and shared icons
 await page.evaluate(()=>{document.querySelector('#nav').innerHTML='<div class="global-agents"><div class="global-roster">'+
  ['dev','learner','atlas','fake'].map(id=>`<button class="item agent" data-global-agent="${id}"><span class="nm">${globalAgentNavAvatar(globalAgentProfiles.find(a=>a.agent_id===id))}</span><span class="agent-nav-working" data-agent-presence="${id}" hidden><i></i><i></i><i></i></span></button>`).join('')+'</div></div>';});
 const visible=()=>page.evaluate(()=>[...document.querySelectorAll('[data-agent-presence]')].filter(d=>!d.hidden).map(d=>d.dataset.agentPresence));
 await page.evaluate(()=>{activity=[{agent_id:'atlas',state:'running'},{agent_id:'dev',state:'idle'},{agent_id:'learner',state:'offline'},{agent_id:'fake',state:'running'}];return agentNavRefresh(true);});
 assert.deepEqual(await visible(),['atlas','fake']);
 const dots=await page.locator('[data-agent-presence="atlas"]').evaluate(el=>{const r=el.getBoundingClientRect(),i=el.querySelector('i').getBoundingClientRect();return {w:i.width,anim:getComputedStyle(el.querySelector('i')).animationName,inside:r.bottom<=el.parentElement.getBoundingClientRect().bottom}});
 assert.equal(dots.w,3);assert.equal(dots.anim,'agent-nav-pulse');assert.ok(dots.inside);
 // Nav icon is the same artwork as the card icon (size aside).
 const art=await page.evaluate(()=>{const strip=h=>{const t=document.createElement('div');t.innerHTML=h;const v=t.firstElementChild;v.removeAttribute('width');v.removeAttribute('height');return v.outerHTML;};return [strip(document.querySelector('[data-global-agent="atlas"] .nm svg').outerHTML),strip(agentGlyph({agent_id:'atlas'},28))]});
 assert.equal(art[0],art[1]);
 const spirits=await page.evaluate(()=>{const strip=h=>h.replace(/ width="[^"]*" height="[^"]*"/,'');const svg=h=>{const t=document.createElement('div');t.innerHTML=h;return strip(t.querySelector('svg.qorqbot-spirit').outerHTML)};
  const nav=svg(document.querySelector('[data-global-agent="atlas"] .nm').innerHTML);
  return [nav,svg(identityAvatar('agent','Atlas',{agent_id:'atlas',icon:'cat',display_name:'Renamed'})),svg(communicationToastHTML({sender_id:'atlas',recipient_id:'atlas'})),svg(agentGlyph({agent_id:'atlas'},22)),
   document.querySelectorAll('[data-global-agent="atlas"] .qorqbot-eye').length,document.querySelectorAll('[data-global-agent="atlas"] .qorqbot-gaze').length,document.querySelectorAll('[data-global-agent="dev"] .qorqbot-spirit,[data-global-agent="learner"] .qorqbot-spirit').length]});
 assert.equal(spirits[0],spirits[1]);assert.equal(spirits[0],spirits[2]);assert.equal(spirits[0],spirits[3]);
 assert.deepEqual(spirits.slice(4),[2,2,0],'eye gaze hooks present; primary art is not a spirit');
 const sizes=await page.evaluate(()=>[...document.querySelectorAll('.nm svg')].map(s=>s.getAttribute('width')));
 assert.deepEqual(sizes,['31.68','31.68','18','18']);
 // Stop hides; no requests while hidden; stale running clears after 30s of failures.
 await page.evaluate(()=>{activity=[{agent_id:'atlas',state:'idle'}];return agentNavRefresh(true);});
 assert.deepEqual(await visible(),[]);
 await page.evaluate(()=>{activity=[{agent_id:'atlas',state:'running'}];return agentNavRefresh(true);});
 assert.deepEqual(await visible(),['atlas']);
 await page.evaluate(()=>{fail=true;});
 await page.clock.fastForward(10000);assert.deepEqual(await visible(),['atlas'],'brief failure keeps last state');
 await page.clock.fastForward(25000);
 assert.deepEqual(await visible(),[],'stale running cleared');
 await page.evaluate(()=>{fail=false;activity=[{agent_id:'atlas',state:'running'}];});
 await page.clock.fastForward(5000);
 assert.deepEqual(await visible(),['atlas'],'recovers on observation');
 await page.evaluate(()=>{Object.defineProperty(document,'hidden',{value:true,configurable:true});window.requests=0;});
 await page.clock.fastForward(20000);
 assert.equal(await page.evaluate(()=>requests),0,'no requests while hidden');
 await page.evaluate(()=>{Object.defineProperty(document,'hidden',{value:false,configurable:true});});
 // A hung activity request cannot keep a stale running dot alive, and no second request starts.
 await page.evaluate(()=>{activity=[{agent_id:'atlas',state:'running'}];return agentNavRefresh(true);});
 assert.deepEqual(await visible(),['atlas']);
 await page.evaluate(()=>{window.hold=new Promise(()=>{});requests=0;agentNavNext=0;void agentNavRefresh(true);});
 await page.clock.fastForward(36000);
 assert.deepEqual(await visible(),[],'stale dots clear while the request hangs');
 assert.equal(await page.evaluate(()=>requests),1,'only the one stuck request, no second');
 await page.evaluate(()=>{window.hold=null;});
 // Reduced motion.
 await page.evaluate(()=>{Object.defineProperty(document,'hidden',{value:false,configurable:true});});
 await page.emulateMedia({reducedMotion:'reduce'});
 assert.equal(await page.locator('[data-agent-presence="atlas"] i').first().evaluate(el=>getComputedStyle(el).animationName),'none');
 await page.emulateMedia({reducedMotion:'no-preference'});
 await page.setViewportSize({width:900,height:300});await page.evaluate(()=>{document.querySelector("#toasts").innerHTML="";_notes.live=0;_notes.queue=[];_notes.replies.clear();notify({replyAgentId:'dev',text:'Ordubashy',sub:'The launch notes are ready for you.',ms:6000});notify({replyAgentId:'learner',text:'Learning Applier',sub:'Your latest preferences are saved.',ms:6000});notify({text:'Ordubashy \u2192 Atlas',communication:{sender_id:'dev',recipient_id:'atlas'},ms:3000});});
 await page.clock.fastForward(100);await page.waitForTimeout(400);if(process.env.SHOT)await page.screenshot({path:process.env.SHOT});
 console.log('Reply cards and navigation presence passed.');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
