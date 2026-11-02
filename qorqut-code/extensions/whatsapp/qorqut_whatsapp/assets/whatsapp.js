(()=>{
function sgWhatsApp(preview) {
  const f = sgFrozenFields(preview, 'whatsapp_account');
  if (!f || !sgNonblank(f.get('chat_id')) || !sgNonblank(f.get('text'))) return sgUnavailable('WhatsApp message');
  const id = f.get('chat_id'), name = f.get('chat_name');
  const named = typeof name === 'string' && name.trim() && name.trim() !== id && !/^(unknown|unnamed|none|null|n\/a|\(not available\))$/i.test(name.trim());
  const phone = /^(\d{7,15})@(?:s\.whatsapp\.net|c\.us)$/.exec(id);
  const recipient = named ? name.trim() : phone ? '+' + phone[1] : id.endsWith('@g.us') ? 'Unnamed WhatsApp group' : 'Unnamed WhatsApp contact';
  return { ok: true, hiddenResources: ['whatsapp_account','jobs'], html: `<section class="sg-content sg-whatsapp" aria-label="Proposed WhatsApp message">`
    + sgField('To', `<strong class="sg-recipient-name">${esc(recipient)}</strong>`)
    + sgTextBlock(f.get('text'), 'WhatsApp message text').replace('class="sg-text"', 'class="sg-text" data-sg-edit-field="text"') + `</section>` };
}
registerSignoffRenderer('whatsapp_send', {
  message: true,
  preview: sgWhatsApp,
  values(s) {
    const text=sgFrozenFields(s.preview,'whatsapp_account')?.get('text');
    return typeof text==='string'?{text}:null;
  },
  fieldError(key,value) {
    if(key!=='text' || typeof value!=='string')return 'This field cannot be edited.';
    if(value.includes('\0'))return 'Message text cannot contain null characters.';
    if(!value.trim() || new TextEncoder().encode(value).length>16384)return 'Enter text of at most 16,384 UTF-8 bytes.';
    return '';
  }
});

})();
