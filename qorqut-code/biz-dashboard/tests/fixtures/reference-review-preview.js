// Disposable UI adapter. Writes below affect only this page's memory, never a live
// Signoff, an actual delta, its frozen review binding, or a provider.
(() => {
  document.body.classList.add('reference-review-preview');
  document.body.innerHTML = `<header class="reference-preview-top"><span data-logo></span>Qorqut <small>UI preview · Sample data</small></header><main class="reference-preview-layout"><section class="reference-preview-chat"><div class="reference-preview-avatar"><span data-agent></span>Ordubashy</div><div id="reference-preview-chat"></div></section><aside class="reference-preview-side"><div class="reference-preview-side-head"><span>Details</span><button type="button" aria-label="Close details">${SVG_X}</button></div><div id="reference-preview-detail"><p class="reference-preview-empty">Open a reference to see its details here.</p></div></aside></main>`;
  document.querySelector('[data-logo]').innerHTML=agentGlyph({role:{id:'ordubashy'}},26);
  document.querySelector('[data-agent]').innerHTML=agentGlyph({role:{id:'ordubashy'}},24);
  const records=new Map([
    ['preview-signoff',{id:'preview-signoff',title:'Review the launch email',kind:'email',state:'pending',revision:1,context:'Email · To Maya Chen',
      fieldLabels:[{key:'subject',label:'Subject'},{key:'body',label:'Message'}],fields:{subject:'A launch collaboration with Qorqut',body:'Hi Maya,\n\nWe’re launching Qorqut and would love to explore a collaboration with you. Your practical approach to showing new tools feels like a good fit.\n\nWould you be open to a quick conversation next week?\n\nAlp'},
      comments:[{by:'You',time:'4:12 PM',text:'Keep it short and don’t mention price.'},{by:'Ordubashy',time:'4:14 PM',text:'Updated. The draft stays focused on the collaboration.'}],history:['Draft created · revision 1']}],
    ['preview-message',{id:'preview-message',title:'Message Maya',kind:'message',state:'pending',revision:1,context:'WhatsApp · Maya Chen',
      fieldLabels:[{key:'body',label:'Message'}],fields:{body:'Hi Maya, would Tuesday afternoon work for a quick chat?'},comments:[],history:['Draft created · revision 1']}],
    ['preview-delta',{id:'preview-delta',title:'Keep research opinion-free',state:'pending',revision:1,context:'Proposed change · Research guidance',
      fieldLabels:[{key:'change',label:'Proposed change'},{key:'scope',label:'Scope'}],fields:{change:'Present research findings as sourced data and clearly described transformations. Keep hypotheses separate and label them explicitly. Do not present opinions as findings.',scope:'Research reports and research summaries.'},
      evidence:'Direct instruction in a research conversation, supported by two separate report edits. The original observations remain attached to the change.',
      comments:[{by:'Overseer',time:'4:18 PM',text:'This applies to research. Brainstorming would keep its existing guidance.'}],history:['Proposal extracted · revision 1']}]
  ]);
  const listeners=new Map();
  const emit=id=>(listeners.get(id)||new Set()).forEach(f=>f());
  const check=(id,revision)=>{const row=records.get(id);if(row.revision!==revision)throw Error('This record changed. Your draft is kept; discard it to load the current version.');if(row.state!=='pending')throw Error('This review has already been decided.');return row;};
  const store={get:id=>records.get(id),subscribe(id,callback){if(!listeners.has(id))listeners.set(id,new Set());listeners.get(id).add(callback);return()=>listeners.get(id).delete(callback);},
    async save(id,revision,fields){const row=check(id,revision);if(Object.values(fields).some(v=>!v.trim()))throw Error('Please keep the proposal fields filled in.');row.fields=fields;row.revision++;row.history.push(`Edited by you · revision ${row.revision}`);emit(id);},
    async comment(id,text){const row=records.get(id);row.comments.push({by:'You',time:'Just now',text});row.history.push('Comment added by you');emit(id);},
    async decide(id,revision,decision){const row=check(id,revision);row.state=decision;row.revision++;row.history.push(`You ${decision} this review · revision ${row.revision}`);emit(id);}
  };
  let side=null;
  function open(ref){side?.dispose();const host=document.getElementById('reference-preview-detail');host.replaceChildren();if(records.has(ref.id)){side=chatReviewReferenceMount(ref,{store,pane:true,onOpen:()=>{}});host.append(side.el);}else{const p=document.createElement('p');p.className='reference-preview-empty';p.textContent=ref.label;host.append(p);} }
  document.querySelector('.reference-preview-side-head button').onclick=()=>{side?.dispose();side=null;document.getElementById('reference-preview-detail').innerHTML='<p class="reference-preview-empty">Open a reference to see its details here.</p>';};
  const view=mountChatView(document.getElementById('reference-preview-chat'),{
    placeholder:'Private message to Ordubashy…',onReference:open,onTask:open,
    mountReviewReference:ref=>chatReviewReferenceMount(ref,{store,onOpen:open}),
    onDraft:text=>{draft=text;},onSubmit:()=>{if(!draft.trim())return;entries.push({key:'owner-'+entries.length,speaker:'owner',text:draft,position:entries.length+1});draft='';view.setDraft('');paint();}
  });
  let draft='';
  const entries=[
    {key:'intro',speaker:'agent',text:'The outreach draft is ready. You can edit it here and leave comments before deciding.',position:1,typedReferences:[{type:'signoff',id:'preview-signoff',label:'Review the launch email',available:true,business_id:null}]},
    {key:'message',speaker:'agent',text:'Here’s the shorter message version.',position:1.5,typedReferences:[{type:'signoff',id:'preview-message',label:'Message Maya',available:true,business_id:null}]},
    {key:'change',speaker:'agent',text:'I also picked up a possible change to the research guidance.',position:2,typedReferences:[{type:'delta',id:'preview-delta',label:'Keep research opinion-free',available:true,business_id:null}]},
    {key:'links',speaker:'agent',text:'The related records are here.',position:3,typedReferences:[{type:'task',id:'preview-person',label:'Maya Chen',task_type:'customer',available:true,business_id:null},{type:'table',id:'preview-table',label:'Influencer partnerships',available:true,business_id:null},{type:'task',id:'preview-task',label:'Prepare the launch page',available:true,business_id:null}]}
  ];
  function paint(){view.render({entries,composer:{inputDisabled:false,sendDisabled:false}});}
  paint();
  window.referenceReviewPreview={store,records,view};
})();
