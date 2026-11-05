// Regression: switching the agent pane between Focus, Activity and the Attention board must not repaint,
// refetch or reset the live main conversation when no record is paired. A real paired record still restores
// it, and a stale clear generation is ignored. Real dashboard code on a disposable synthetic fixture only.
const assert=require('node:assert/strict');
const {startCrmServer}=require('./support/crm_server');
const {launch,until}=require('./support/crm_page');

const binding={binding_id:'b1',agent_id:'ordu',provider:'claude',conversation_id:'conv-1',generation:1,transport:'terminal'};
const entries=Array.from({length:36},(_,i)=>({id:'m'+i,kind:'message',speaker:i%3?'agent':'user',
  text:'Conversation entry '+i+' with enough text to exercise the existing chat scroll area. '.repeat(3),
  timestamp:null,position:(i+1)*100,partial:false,identity:'native_id',completion_sequence:null,completion_id:null,system:null}));

(async()=>{
  const fixture=startCrmServer();
  fixture.agentChats.set('ordu',{agent_id:'ordu',name:'Ordubashy',
    session:{state:'live',reason:'live',can_send:true,working:false,provider:'claude',conversation_id:'conv-1',transport:'terminal',detail:'Live',binding},
    revision:1,cursor:entries.length,
    conversation:{conversation_id:'conv-1',provider:'claude',complete:true,finalized:false,error:null,captured_length:1},
    conversations:[{conversation_id:'conv-1',provider:'claude',updated_at:null,current:true}],
    entries,older:null,rendered_boundary:0,inputs:[],last_read_sequence:0,newest_sequence:0});
  const {browser,page,errors}=await launch(fixture);
  try {
    await page.route('**/api/root/operations/attention_get',route=>route.fulfill({contentType:'application/json',
      body:JSON.stringify({status:'ok',data:{attention:{state:'unfocused'},items:[],next_cursor:null}})}));
    await page.locator('.global-agent-session [data-global-chat="ordu"]').waitFor();
    await until(async()=>await page.locator('.gchat-msg').count()>0,'chat entries');
    await page.locator('#agpad .ttab[data-v="focus"]').waitFor();
    assert.equal(await page.evaluate(()=>_agpad.view),'focus','Ordubashy opens on Focus');
    // Pin a stable identity for the conversation and its first rendered entry, then leave a real draft.
    await page.evaluate(()=>{
      const scroll=document.querySelector('.gchat-scroll');
      scroll.scrollTop=scroll.scrollHeight;
      window.__chatRoot=document.querySelector('.global-agent-session .gchat');
      window.__chatScroll=scroll;
      window.__chatEntry=document.querySelector('.gchat-msg');
      window.__chatRoot.dataset.stability='kept';
      window.__chatEntry.dataset.stability='kept';
    });
    await page.locator('.gchat-composer textarea').fill('draft that must survive pane switches');
    const snapshot=async()=>page.evaluate(()=>{
      const root=document.querySelector('.global-agent-session .gchat');
      return {sameRoot:root===window.__chatRoot,rootKept:root?.dataset.stability==='kept',
        sameEntry:document.querySelector('.gchat-msg')===window.__chatEntry,
        entryKept:window.__chatEntry?.isConnected===true&&window.__chatEntry.dataset.stability==='kept',
        entries:document.querySelectorAll('.gchat-msg').length,
        top:document.querySelector('.gchat-scroll')?.scrollTop||0,
        draft:document.querySelector('.gchat-composer textarea')?.value||''};
    });
    const stateReads=()=>fixture.requests.filter(request=>request.path==='/api/root/agents/ordu/chat').length;
    await until(async()=>(await snapshot()).draft==='draft that must survive pane switches','draft saved');
    const baseline=stateReads();
    const before=await snapshot();
    assert.equal(before.sameRoot,true);
    assert.equal(before.entryKept,true);
    assert.ok(before.entries>10);
    assert.ok(before.top>0,'the conversation can scroll');
    const unchanged=async label=>{
      const now=await snapshot();
      assert.deepEqual(now,before,label);
      assert.equal(stateReads(),baseline,label+': no chat state refetch');
    };
    // Actual header tab switches: Focus <-> Attention board, both directions.
    await page.locator('#agpad .ttab[data-v="attentionBoard"]').click();
    await page.locator('[data-attention-board]').waitFor();
    await unchanged('focus -> Attention board keeps the live chat');
    await page.locator('#agpad .ttab[data-v="focus"]').click();
    await page.waitForFunction(()=>document.querySelector('#agpad .agfocus')&&_agpad.focusMount?.isConnected);
    await unchanged('Attention board -> focus keeps the live chat');
    // The visible Activity/Inbox tab, then the exact activity view, and back to Focus each time.
    await page.locator('#agpad .ttab[data-v="status"]').click();
    await page.waitForFunction(()=>_agpad.view!=='focus');
    await unchanged('focus -> Activity tab keeps the live chat');
    await page.locator('#agpad .ttab[data-v="focus"]').click();
    await page.waitForFunction(()=>_agpad.view==='focus'&&_agpad.focusMount?.isConnected);
    await unchanged('Activity tab -> focus keeps the live chat');
    // The Global agent pane folds Activity into the status overview; enable its existing Activity view key
    // for the exact focus -> activity switch, then exercise both directions through the real agpadSetView.
    await page.evaluate(()=>{if(!_agpad.views.includes('activity'))_agpad.views.push('activity');agpadSetView(_agpad,'activity');});
    await page.waitForFunction(()=>_agpad.view==='activity');
    await unchanged('focus -> activity keeps the live chat');
    await page.evaluate(()=>agpadSetView(_agpad,'focus'));
    await page.waitForFunction(()=>_agpad.view==='focus'&&_agpad.focusMount?.isConnected);
    await unchanged('activity -> focus keeps the live chat');
    // A real paired record replaces the main conversation; a stale clear is ignored; closing restores it.
    assert.equal(await page.evaluate(()=>window.agentWorkspacePairedRecord(window,{type:'task',id:'paired-task',business_id:null})),true);
    await page.locator('.gchat-paired-record').waitFor();
    assert.equal(await page.locator('.global-agent-session .gchat').count(),0,'the paired record owns the pane');
    assert.equal(await page.evaluate(()=>globalAgentViewActive.clearPairedSource(0)),false,'a stale generation cannot clear the paired record');
    assert.equal(await page.locator('.gchat-paired-record').count(),1);
    assert.equal(await page.evaluate(()=>window.agentWorkspacePairedRecord(window,null)),true);
    await page.waitForFunction(()=>!!document.querySelector('.global-agent-session [data-global-chat="ordu"]'));
    await until(async()=>stateReads()===baseline+1,'restored conversation refetch');
    await page.waitForTimeout(50);
    const restored=await snapshot();
    assert.equal(restored.sameRoot,true,'the original chat shell returns');
    assert.equal(restored.draft,'draft that must survive pane switches','the conversation draft survives the paired record');
    assert.equal(stateReads(),baseline+1,'closing the paired record restores the conversation once');
    assert.deepEqual(errors,[]);
    console.log('Attention/Chat stability: Focus<->Activity<->Attention switches keep the main chat node, entries, scroll and draft without a refetch; a real paired record still restores it and stale generations are ignored.');
  } finally {await browser.close();await fixture.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});