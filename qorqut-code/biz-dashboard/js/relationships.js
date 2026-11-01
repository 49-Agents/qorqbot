// Shared context catch-up for every item. Reads never acknowledge a relationship.
const contextRoleLabel = role => String(role || 'related to').replace(/_/g, ' ');
const contextItemType = item => item.material_type ? item.material_type.charAt(0).toUpperCase()+item.material_type.slice(1) : item.subtype === 'conversation' ? 'Conversation' : ({material:'Material',topic:'Topic',story:'Story',task:'Task',table:'Table',product:'Product',person:'Person',agent:'Agent'}[item.type] || item.type);
const contextMaterialLabel = item => [item.origin,...(item.tags || [])].filter(Boolean).join(' · ');

async function contextOpenItem(biz, item, beforeOpen) {
  if (await beforeOpen?.() === false) return;
  if (state.biz !== biz || state.scope === 'global') await selectBiz(biz);
  if (item.type === 'topic' && item.path?.startsWith('context/')) {
    state.ctx=item.path.slice(8).replace(/\/_topic\.md$/, '');
    await goTab('context'); return;
  }
  if (item.type === 'person' && item.path?.startsWith('context/_people/')) {
    state.ctx=item.path.slice(8).replace(/\.md$/, '');
    await goTab('context'); return;
  }
  if (item.type === 'person') { await editPersonDetails(biz, item.id); return; }
  if (item.type === 'product') { await productsOpen(biz, item.id); return; }
  if (item.type === 'story') {
    if (state.biz !== biz) await selectBiz(biz);
    storyPending={biz,id:item.id}; openTasks(false); await goTab('stories'); return;
  }
  await storyOpenLink({biz, error:e=>toast(e.message)}, {resource_type:item.resource_type === 'context_entities' ? 'entity' : item.resource_type,
    resource_id:item.id, current:{...item, kind:item.kind || item.subtype}, snapshot_json:item});
}

async function contextRelatedChips(host, biz, item, beforeOpen) {
  let data;
  try { data = await storyCall(biz, 'context_reviews_list', {item, include_reviewed:true, limit:50}); }
  catch (error) {
    if (!host.isConnected) return;
    const message=document.createElement('span');message.className='muted';message.textContent=error.message;
    const retry=document.createElement('button');retry.className='quiet';retry.textContent='Retry related context';
    retry.onclick=()=>contextRelatedChips(host,biz,item,beforeOpen);
    host.replaceChildren(message,retry);return;
  }
  if (!host.isConnected) return;
  host.replaceChildren();
  const pending = data.items.filter(row=>row.pending).length;
  const label = document.createElement('span'); label.className='muted';
  label.textContent = `${data.items.length}${data.next_cursor ? '+' : ''} related · ${pending}${data.next_cursor ? '+' : ''} pending`;
  const button = document.createElement('button'); button.className='quiet'; button.textContent='Related context';
  button.onclick=()=>modal({stack:true, title:'Related context', body:'<div class="context-review-panel"></div>',
    onOpen:el=>contextReviewPanel(el.querySelector('.context-review-panel'),biz,item,{beforeOpen:async()=>{
      if(await beforeOpen?.()===false)return false;
      _modal?.close();
    },onChange:()=>contextRelatedChips(host,biz,item,beforeOpen)})});
  host.append(label,button);
}

async function contextReviewPanel(host, biz, item, options={}) {
  let cursor=null, generation=0;
  host.innerHTML='<div class="context-review-toolbar"><label><input type="checkbox" class="context-reviewed"> Include reviewed</label><button class="quiet context-refresh">Refresh</button></div><div class="ferr context-review-error" role="alert" hidden></div><div class="context-review-rows"></div><button class="quiet context-review-more" hidden>More context</button>';
  const list=host.querySelector('.context-review-rows'),error=host.querySelector('.context-review-error');
  const reviewed=host.querySelector('.context-reviewed'),moreButton=host.querySelector('.context-review-more');
  const fail=e=>{if(host.isConnected){error.textContent=e.message || String(e);error.hidden=false;}};
  const interacting=()=> (document.activeElement?.tagName==='TEXTAREA' && host.contains(document.activeElement)) ||
    host.querySelector('.context-review-history:not([hidden])') ||
    [...host.querySelectorAll('textarea')].some(input=>input.value) || list.children.length>50;
  const load=async(more=false,automatic=false)=>{
    const request=++generation;
    try{
      const data=await storyCall(biz,'context_reviews_list',{item,include_reviewed:reviewed.checked,limit:50,...(more&&cursor?{cursor}:{})});
      if(!host.isConnected || request!==generation || (automatic&&interacting()))return;
      cursor=data.next_cursor;error.hidden=true;
      if(!more)list.replaceChildren();
      if(!data.items.length&&!more)list.innerHTML='<p class="muted">'+(reviewed.checked?'No related context.':'No pending context.')+'</p>';
      data.items.forEach(row=>render(row,data.item_revision));
      moreButton.hidden=!cursor;
    }catch(e){fail(e);}
  };
  function render(row,itemRevision){
    const source=row.source_item,article=document.createElement('article');article.className='context-relation';
    article.dataset.relationshipId=row.relationship_id;
    let pendingRequest;
    const latest=row.latest_review;
    article.innerHTML=`<div class="context-relation-heading"><button class="quiet context-source" ${source?'':'disabled'}>${esc(source?.label || 'Unavailable context')}</button><span class="context-review-state">${row.pending?'Pending':'Reviewed'}</span></div>
      <div class="context-relation-role">${esc(row.direction==='incoming'?'Incoming':'Outgoing')} · ${esc(contextRoleLabel(row.role))}${source?' · '+esc(contextItemType(source)):''}</div>
      ${source?.material_type?`<div class="context-relation-role context-material-label">${esc(contextMaterialLabel(source))}</div>`:''}
      ${row.context?`<p>${esc(row.context)}</p>`:''}${source?.summary?`<p>${esc(source.summary)}</p>`:''}
      ${latest?`<div class="context-last-review"><strong>${esc(latest.result==='pending'?'Reopened':latest.result==='incorporated'?'Incorporated':'No change needed')}</strong> · ${esc(latest.actor?.name || '')} · ${esc(storyDate(latest.created_at))}<p>${esc(latest.explanation)}</p></div>`:''}
      <label class="context-review-explanation">Explanation<textarea class="inp" rows="2" maxlength="4000" ${row.available?'':'disabled'}></textarea></label>
      <div class="context-review-actions">${row.pending?'<button class="btn" data-result="incorporated">Incorporated</button><button class="quiet" data-result="no_change">No change needed</button>':'<button class="quiet" data-result="pending">Reopen</button>'}<button class="quiet context-history">Review history</button></div><div class="context-review-history" hidden></div>`;
    article.querySelector('.context-source').onclick=()=>contextOpenItem(biz,source,options.beforeOpen).catch(fail);
    const text=article.querySelector('textarea'),buttons=[...article.querySelectorAll('[data-result]')];
    const enable=()=>buttons.forEach(button=>button.disabled=!row.available||!text.value.trim());
    text.oninput=enable;enable();
    buttons.forEach(button=>button.onclick=async()=>{
      const explanation=text.value.trim();if(!explanation)return;
      buttons.forEach(control=>control.disabled=true);text.disabled=true;
      const observed=Object.fromEntries(['relationship_id','relationship_revision','review_revision','source_token'].map(key=>[key,row[key]]));
      try{
        if(await options.beforeReview?.()===false){text.disabled=false;enable();return;}
        const operation=button.dataset.result==='pending'?'context_review_reopen':'context_review';
        const args=button.dataset.result==='pending'?{item,...observed,explanation}:{item,items:[{...observed,result:button.dataset.result,explanation}]};
        const signature=JSON.stringify({operation,args,itemRevision});
        if(pendingRequest?.signature!==signature)pendingRequest={signature,key:newRequestId()};
        await storyCall(biz,operation,args,{expected_revision:itemRevision,idempotency_key:pendingRequest.key});
        await load();await options.onChange?.();
      }catch(e){fail(e);text.disabled=false;enable();}
    });
    article.querySelector('.context-history').onclick=async()=>{
      const history=article.querySelector('.context-review-history');history.hidden=!history.hidden;
      if(history.hidden)return;
      try{
        history.replaceChildren();
        const loadHistory=async(cursor)=>{
          const data=await storyCall(biz,'context_reviews_history',{item,relationship_id:row.relationship_id,limit:50,...(cursor?{cursor}:{})});
          if(!article.isConnected)return;
          history.insertAdjacentHTML('beforeend',data.reviews.map(review=>`<div><strong>${esc(review.result==='pending'?'Reopened':review.result==='incorporated'?'Incorporated':'No change needed')}</strong> · ${esc(review.actor?.name || '')} · ${esc(storyDate(review.created_at))}<p>${esc(review.explanation)}</p></div>`).join('')||'<p class="muted">No previous reviews.</p>');
          if(data.next_cursor){
            const more=document.createElement('button');more.className='quiet';more.textContent='Older reviews';
            more.onclick=async()=>{more.disabled=true;try{await loadHistory(data.next_cursor);more.remove();}catch(e){more.disabled=false;fail(e);}};
            history.appendChild(more);
          }
        };
        await loadHistory();
      }catch(e){fail(e);}
    };
    list.appendChild(article);
  }
  reviewed.onchange=()=>load();moreButton.onclick=()=>load(true);host.querySelector('.context-refresh').onclick=()=>load();
  host.refreshContext=()=>{
    if(interacting())return;
    return load(false,true);
  };
  await load();
}
