// Loaded only when the Fastmail extension is selected.
(() => {
function preview(preview, effects, operation) {
  const f = sgFrozenFields(preview, 'fastmail_account');
  const optionalList = k => !f.has(k) || f.get(k) == null || sgStringList(f.get(k));
  const attachments = f && f.get('attachments');
  if (!f || !sgNonblank(f.get('from_address')) || !sgStringList(f.get('to')) || !f.get('to').length || typeof f.get('subject') !== 'string' || typeof f.get('body') !== 'string'
      || !optionalList('cc') || !optionalList('bcc') || (attachments != null && !(Array.isArray(attachments) && attachments.every(a => a && sgNonblank(a.filename))))) return sgUnavailable('email');
  const list = k => f.get(k) && f.get(k).length ? sgField(k === 'cc' ? 'Cc' : 'Bcc', esc(f.get(k).join(', '))) : '';
  const fileLine = a => esc(a.filename);
  const named = prefix => effects.filter(e => e.startsWith(prefix)).map(e => e.slice(prefix.length));
  const sourceBody = effects.find(e => e.startsWith('Source body:\n')), sourceFiles = named('Source attachments: ')[0];
  const forward = named('Forward source message ')[0] ?? (sgNonblank(f.get('forward_message_id')) ? f.get('forward_message_id') : null);
  const isForward = /_forward$/.test(operation) || forward != null;
  if (isForward && (sourceBody == null || sourceFiles == null)) return sgUnavailable('forwarded email content');
  const source = sourceBody != null || sourceFiles != null;
  return { ok: true, effects: effects.filter(effect => !/^(From|To|Cc|Bcc|Subject|Body|Attachments|Source body|Source attachments):|^(Reply|Forward) source message /.test(effect)), hiddenResources: ['fastmail_account', 'jobs'], html: `<section class="sg-content sg-email" aria-label="Proposed email">`
    + sgField('From', esc(f.get('from_address'))) + sgField('To', esc(f.get('to').join(', '))) + list('cc') + list('bcc')
    + sgEmailTextHtml(f.get('subject'), f.get('body'))
    + (attachments && attachments.length ? sgField('Attachments', `<ul class="sg-val-list">${attachments.map(a => `<li>${fileLine(a)}</li>`).join('')}</ul>`) : '')
    + (source ? `<div class="sg-email-source"><h4>${isForward ? 'Forwarded message' : 'Replying to'}</h4>`
      + (sourceBody != null ? `<span class="sg-content-label">Source body</span>${sgTextBlock(sourceBody.slice('Source body:\n'.length), 'Source message body')}` : '')
      + (sourceFiles ? sgField('Source attachments', esc(sourceFiles)) : '') + `</div>` : '') + `</section>` };
}
function values(s) {
  const fields=sgFrozenFields(s.preview,'fastmail_account');
  return fields && typeof fields.get('subject')==='string' && typeof fields.get('body')==='string'
    ? {subject:fields.get('subject'),body:fields.get('body')} : null;
}
function fieldError(key,value) {
  const limits={subject:998,body:50000};
  if(typeof value!=='string' || !Object.hasOwn(limits,key))return 'This field cannot be edited.';
  if(value.includes('\0') || (key==='subject' && /[\r\n]/.test(value)))return 'Subject must be one line; message fields cannot contain null characters.';
  if(!value.trim() || [...value].length>limits[key])
    return `Enter ${key==='subject'?'a subject':'text'} of at most ${limits[key].toLocaleString('en-US')} characters.`;
  return '';
}
// Historical names remain display aliases only; they add no server operation or permission.
for (const operation of ['fastmail_send','fastmail_reply','fastmail_forward',
    'account_email_send','account_email_reply','account_email_forward','email_send','email_reply','email_forward'])
  registerSignoffRenderer(operation, {preview,values,fieldError,email:true,message:true});

const FASTMAIL_READER_PAGE=5,FASTMAIL_READER_CONCURRENCY=2;
async function fastmailReaderCall(operation,args,signal) {
  return coreData(await conversationRequest(post(`/root/extensions/fastmail/operations/${operation}`,{scope:'global',...args},{signal})));
}
function fastmailReaderAddresses(list) {
  return (list || []).map(item=>{
    const a=item?.emailAddress || item || {};
    return a.name && a.address?`${a.name} <${a.address}>`:(a.address || a.name || '');
  }).filter(Boolean).join(', ');
}
// Inline reader for one original email chain. Every read pins the reference's own account; a failure is shown
// and never falls back to another account. State is rebuilt per open, so nothing is reused across references.
function fastmailReaderMount(host,onClose) {
  let R=null,disposed=false;
  host.classList.add('conversation-mail');
  const unavailable='This email is not available. It may be missing, in a different account, or you may not have access.';
  const itemKey=id=>`${R.account}|${id}`;
  function clear() {
    const previous=R;R=null;
    previous?.controller.abort();
    for(const job of previous?.queue || [])job.resolve();
    if(previous)previous.queue=[];
    host.hidden=true;host.replaceChildren();
  }
  function close() {clear();onClose?.();}
  function messageNode(item,id,selected) {
    const box=conversationEl('article','conversation-mail-message');
    if(selected)box.classList.add('selected');
    if(!item || item.status==='loading'){box.append(conversationEl('p','conversation-mail-note','Loading message…'));return box;}
    if(item.status==='error') {
      box.append(conversationEl('p','conversation-mail-note','This message could not be loaded.'));
      const retry=conversationEl('button','quiet','Retry');retry.type='button';
      retry.onclick=()=>{void (selected?fetchSelected():fetchMessage(id,true));};
      box.append(retry);return box;
    }
    const m=item.message || {};
    box.append(conversationEl('h4','conversation-mail-subject',m.subject || '(No subject)'));
    const head=conversationEl('dl','conversation-mail-head');
    const row=(label,value)=>{if(!value)return;head.append(conversationEl('dt',null,label),conversationEl('dd',null,value));};
    row('From',fastmailReaderAddresses([m.from]));
    row('To',fastmailReaderAddresses(m.toRecipients));
    row('Cc',fastmailReaderAddresses(m.ccRecipients));
    row('Date',conversationWhen(m.sentDateTime || m.receivedDateTime));
    box.append(head);
    const html=String(m.body?.contentType || '').toLowerCase()==='html';
    if(html)box.append(conversationEl('p','conversation-mail-note','HTML message shown as plain source; images and links are not loaded.'));
    // textContent only: the body is never parsed as markup, so nothing in it can run or fetch.
    box.append(conversationEl('pre','conversation-mail-body',m.body?.content ?? ''));
    if(m.bodyTruncated)box.append(conversationEl('p','conversation-mail-note','This body was truncated; the original is longer than shown.'));
    return box;
  }
  function chainIds() {
    const ids=R.thread.ids.filter(id=>id!==R.messageId);
    const slots=[];
    ids.forEach((id,index)=>{const item=R.items.get(itemKey(id));const t=item?.status==='ok'?Date.parse(item.message.sentDateTime || item.message.receivedDateTime || ''):NaN;if(!Number.isNaN(t))slots.push({index,id,t});});
    const sorted=[...slots].sort((a,b)=>a.t-b.t || a.index-b.index);
    const out=[...ids];slots.forEach((slot,i)=>{out[slot.index]=sorted[i].id;});
    return out;
  }
  function paint() {
    if(disposed || !R)return;
    const body=conversationEl('div','conversation-mail-scroll');
    const bar=conversationEl('div','conversation-mail-bar');
    const back=conversationEl('button','quiet conversation-mail-close','‹ Back to conversation');back.type='button';back.onclick=close;
    bar.append(back,conversationEl('strong',null,'Original email'));
    body.append(bar);
    const selected=R.items.get(itemKey(R.messageId));
    if(R.selectedError) {
      body.append(conversationEl('p','conversation-mail-note',unavailable));
      const retry=conversationEl('button','quiet','Retry');retry.type='button';retry.onclick=()=>{void fetchSelected();};body.append(retry);
    } else body.append(messageNode(selected,R.messageId,true));
    if(R.thread.status!=='idle') {
      const chain=conversationEl('section','conversation-mail-chain');
      chain.append(conversationEl('h4',null,'Email chain'));
      const ids=chainIds(),loaded=R.thread.ids.filter(id=>R.items.get(itemKey(id))?.status==='ok');
      if(R.thread.status==='none')chain.append(conversationEl('p','conversation-mail-note','The rest of this conversation is not available.'));
      else if(R.thread.status==='error') {
        chain.append(conversationEl('p','conversation-mail-note','The conversation could not be loaded.'));
        const retry=conversationEl('button','quiet','Retry');retry.type='button';retry.onclick=()=>{void loadThread();};chain.append(retry);
      } else {
        for(const id of ids)chain.append(messageNode(R.items.get(itemKey(id)),id,false));
        const failed=R.thread.ids.filter(id=>R.items.get(itemKey(id))?.status==='error').length;
        const truncated=loaded.filter(id=>R.items.get(itemKey(id)).message.bodyTruncated).length;
        const done=R.thread.status==='ok' && !R.thread.hasMore && R.thread.ids.every(id=>R.items.get(itemKey(id))?.status==='ok');
        const parts=[done?`Complete conversation: ${R.thread.ids.length} message${R.thread.ids.length===1?'':'s'}`:`Showing ${loaded.length} of ${R.thread.total ?? R.thread.ids.length} messages`];
        if(failed)parts.push(`${failed} could not be loaded`);
        if(truncated)parts.push(`${truncated} body truncated`);
        chain.append(conversationEl('p','conversation-mail-status',parts.join(' · ')));
        if(R.thread.hasMore) {
          const more=conversationEl('button','quiet','Load more');more.type='button';more.disabled=R.thread.status==='loading';
          more.onclick=()=>{void loadThread();};chain.append(more);
        }
        if(R.thread.status==='loading')chain.append(conversationEl('p','conversation-mail-note','Loading…'));
      }
      body.append(chain);
    }
    host.replaceChildren(body);host.hidden=false;
  }
  // One reader-owned queue bounds message reads to two at a time across paging, retries and the selected message.
  function pump(mine) {
    while(R===mine && !disposed && mine.active<FASTMAIL_READER_CONCURRENCY && mine.queue.length) {
      const job=mine.queue.shift();
      mine.active++;
      readMessage(mine,job.id).finally(()=>{mine.active--;job.resolve();pump(mine);});
    }
  }
  async function readMessage(mine,id) {
    const key=`${mine.account}|${id}`;
    try {
      const data=await fastmailReaderCall('fastmail_message_get',{account_id:mine.account,message_id:id},mine.controller.signal);
      if(R===mine && !disposed)mine.items.set(key,{status:'ok',message:data.message || data});
    } catch(_) {
      if(R===mine && !disposed)mine.items.set(key,{status:'error'});
    }
    if(R===mine && !disposed)paint();
  }
  function fetchMessage(id,force=false) {
    const mine=R,key=itemKey(id),status=mine.items.get(key)?.status;
    if(status==='loading' || (status==='ok' && !force))return Promise.resolve();
    mine.items.set(key,{status:'loading'});paint();
    return new Promise(resolve=>{mine.queue.push({id,resolve});pump(mine);});
  }
  async function fetchSelected() {
    const mine=R;mine.selectedError=false;
    await fetchMessage(mine.messageId,true);
    if(R!==mine || disposed)return;
    if(mine.items.get(itemKey(mine.messageId))?.status!=='ok'){mine.selectedError=true;paint();return;}
    await loadThread();
  }
  async function loadThread() {
    const mine=R,selected=mine.items.get(itemKey(mine.messageId));
    const threadId=selected?.message?.conversationId;
    if(!threadId){mine.thread.status='none';paint();return;}
    if(mine.thread.status==='loading')return;
    mine.thread.status='loading';paint();
    let page;
    try {
      page=await fastmailReaderCall('fastmail_thread_get',{account_id:mine.account,thread_id:threadId,position:mine.thread.next,limit:FASTMAIL_READER_PAGE},mine.controller.signal);
    } catch(_) {
      if(R!==mine || disposed)return;
      mine.thread.status='error';paint();return;
    }
    if(R!==mine || disposed)return;
    const ids=(page.emailIds || []).filter(id=>typeof id==='string');
    mine.thread.ids=[...new Set([...mine.thread.ids,...ids])];
    mine.thread.total=page.total ?? mine.thread.total;
    // A page that returns nothing ends paging instead of repeating the same request.
    mine.thread.hasMore=!!page.has_more && ids.length>0;
    mine.thread.next=(page.position ?? mine.thread.next)+ids.length;
    mine.thread.status='ok';paint();
    const queue=ids.filter(id=>mine.items.get(itemKey(id))?.status!=='ok');
    await Promise.all(queue.map(id=>fetchMessage(id)));
  }
  return {
    isOpen:()=>!!R,
    close:()=>{if(R)close();},
    open(parsed) {
      clear();
      R={controller:new AbortController(),account:parsed.account,messageId:parsed.message,items:new Map(),queue:[],active:0,selectedError:false,thread:{status:'idle',ids:[],next:0,total:null,hasMore:false}};
      void fetchSelected();
    },
    dispose() {disposed=true;clear();host.classList.remove('conversation-mail');}
  };
}
if(typeof registerConversationReferenceReader==='function')registerConversationReferenceReader('fastmail',fastmailReaderMount);
})();
