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
})();
