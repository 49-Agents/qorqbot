// Tasks and Focus use the same canonical combined thread.
function taskProgressMount(host,task,biz,beforeOpen) {
  // host is a freshly rendered task-detail element each time this is called.
  const conversation=conversationMount(host,{type:'task',id:task.id,biz,revision:task.revision});
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
  if (ref.type === 'signoff') return sgAttentionNavigate(biz || null, ref.id);
  if (ref.type === 'dev_ticket') { location.hash = '/global/overview/incidents'; return; }
  // Materials (e.g. an email conversation) open through the shared context-item route.
  if (ref.type === 'material' && biz) return contextOpenItem(biz,{type:'material',id:ref.id,resource_type:ref.resource_type});
}
