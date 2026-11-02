// Tasks and Focus use the same canonical combined thread.
function taskProgressMount(host,task,biz,beforeOpen) {
  // host is a freshly rendered task-detail element each time this is called.
  const conversation=conversationMount(host,{type:'task',id:task.id,biz,revision:task.revision},{related:true});
  conversation.expand();
  return conversation;
}

async function taskProgressOpenReference(biz, ref, beforeOpen) {
  if (ref.unavailable) return;
  if (ref.type === 'task') {
    if (!biz) return modal({title:'Global task', width:700, stack:true, body:'<div class="progress-task-detail"></div>',
      onOpen:el => globalTasksMount($('.progress-task-detail',el), {initialTaskId:ref.id})});
    const task = await tFetchTask(biz,ref.id);
    if (await beforeOpen?.() === false) return;
    return tDetail(task);
  }
  if (await beforeOpen?.() === false) return;
  if (ref.type === 'action') {
    const id=ref.id.startsWith('global/') || ref.id.startsWith('business/')?ref.id:(ref.business_id?`business/${ref.business_id}/`:'global/')+ref.id;
    const {action}=coreData(await post('/root/operations/actions_get',{action_id:id}));
    return modal({title:'Action',stack:true,width:700,body:`<div class="work-source">${esc(action.text)}</div>`});
  }
  if (ref.type === 'signoff') return sgAttentionNavigate(biz || null, ref.id);
  // Materials (e.g. an email conversation) open through the shared context-item route.
  if (ref.type === 'material' && biz) return contextOpenItem(biz,{type:'material',id:ref.id,resource_type:ref.resource_type});
}
