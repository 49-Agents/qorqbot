const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Minimal DOM and provider: no calendar request leaves this process.
function harness() {
  const calls = [], notices = [], subscriptions = [];
  let session, rejectResponse, deferRead = false, resolveRead, successOperation, owner = 'owner-a', requestNumber = 0;
  class Element {
    constructor() { this.children = []; this.dataset = {}; this.isConnected = true; this.value = ''; this.textContent = ''; this.className = ''; }
    append(...children) { for (const child of children) { child.parent = this; this.children.push(child); } }
    setAttribute(name,value) { this[name] = value; }
    contains() { return false; }
    replaceChildren(...children) { this.children = []; this.append(...children); }
    after(child) { this.retry = child; }
    remove() { this.removed = true; if (this.parent) this.parent.children = this.parent.children.filter(x => x !== this); }
    focus() { this.focused = true; }
    querySelectorAll(selector) { return selector === '.mfoot button' ? this.footer.children : []; }
    set innerHTML(value) {
      this.html = value;
      if (value.includes('calendar-focus-content')) {
        const root = this.firstElementChild = new Element(); root.footer = new Element(); root.error = new Element(); root.note = new Element(); root.select = new Element(); root.save = new Element(); root.meetOption = new Element(); root.review = new Element();
        if (value.includes('calendar-form')) {
          const form = root.form = new Element(); form.elements = {};
          for (const match of value.matchAll(/<(input|select|textarea)\b([^>]*)>(?:([^<]*))?/g)) {
            const attrs = match[2], name = /name="([^"]+)"/.exec(attrs)?.[1]; if (!name) continue;
            const field = new Element(); field.name = name; field.checked = /\bchecked\b/.test(attrs); field.disabled = /\bdisabled\b/.test(attrs); field.type = /type="([^"]+)"/.exec(attrs)?.[1] || '';
            field.value = /value="([^"]*)"/.exec(attrs)?.[1] || (match[1] === 'textarea' ? match[3] || '' : ''); form.elements[name] = field;
          }
          // Values from the selected options that matter to this test.
          form.elements.calendar.value = '0'; form.elements.recurrence.value = ''; form.elements.reminders.value = 'default';
          form.reportValidity = () => true;
        }
      }
    }
    get innerHTML() { return this.html || ''; }
  }
  const document = {activeElement:new Element(), createElement:() => new Element()};
  const $ = (selector, el) => {
    if (selector === '.mfoot') return el.footer;
    if (selector === '.mfoot .primary') return el.footer.children.find(x => x.className === 'primary');
    if (selector === '.calendar-detail-error') return el.error;
    if (selector === '.calendar-status-note') return el.note;
    if (selector === 'select[name=personal_status]') return el.select;
    if (selector === '[data-calendar-status-save]') return el.save;
    if (selector === '[data-calendar-status-retry]') return el.note.retry?.removed ? null : el.note.retry;
    if (selector === '[data-meet-option]') return el.meetOption;
    if (selector === '[data-calendar-review]') return el.review;
    if (selector === 'form') return el.form;
    throw Error('Unexpected selector '+selector);
  };
  const occurrence = {connection_id:'conn',calendar_id:'cal',id:'occ',revision:'rev',title:'Meeting',start:'2026-11-05T15:00:00Z',end:'2026-11-05T16:00:00Z',series_id:'series',recurrence:[],attendees:[{email:'me@example.com',status:'needsAction'}],organizer:'owner@example.com',default_reminders:true,reminders:[]};
  const calendar = {id:'cal',connection_id:'conn',writable:true,provider:'google',name:'Work',organizers:['me@example.com']};
  const context = {window:{focusExpansion:{async open(spec) {
    const host = new Element(); let current = true, before = () => true, dispose = () => {};
    session = {host,isCurrent:()=>current,setTitle(t){this.title=t;},setBeforeLeave(fn){before=fn;},onDispose(fn){dispose=fn;},async close(){if (!await before()) return false; current=false; dispose(); return true;},attempt:async()=>before()};
    spec.mount(host,session); return session;
  }}},document,$,console,Intl,Date,Map,Set,Promise,JSON,Object,Array,Number,String,RegExp,Error,
    FormData:class { constructor(form) { this.items = Object.values(form.elements).filter(f => !f.disabled && (f.type !== 'checkbox' || f.checked)).map(f => [f.name,f.value]); } [Symbol.iterator]() {return this.items[Symbol.iterator]();} },
    esc:s=>String(s ?? ''),toast:s=>notices.push(s),coreData:x=>x,accessLost:()=>false,clearEtagCache:()=>{},newRequestId:()=> 'request-'+(++requestNumber),loadSession:async()=>owner ? {principal_id:owner,principal_kind:'human',is_owner:true} : null,AICONS:{},T_VIEW_ICON:{calendar:''},
    api:async()=>({items:[]}),post:async (url,args) => {calls.push({url,args}); if (url.endsWith('calendar_event_get')) { if (deferRead) return new Promise(resolve=>{resolveRead=()=>resolve({event:{...occurrence,id:args.event_id}});}); return {event:{...occurrence,id:args.event_id}}; } if (successOperation && url.endsWith(successOperation)) return {}; if (url.endsWith('calendar_event_respond')) return new Promise((resolve,reject)=>{rejectResponse=reject;}); throw Error('Provider mutation intentionally rejected');}};
  vm.createContext(context); vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/global-calendar.js'),'utf8'),context);
  const run = code => vm.runInContext(code,context);
  const flush = async () => { for(let i=0;i<8;i++) await Promise.resolve(); };
  const button = label => session.host.firstElementChild.footer.children.find(b=>b.textContent===label);
  return {context,run,occurrence,calendar,calls,notices,flush,button,get session(){return session;},success: operation => {successOperation=operation;},setOwner: value => {owner=value;},choose: label => { const prompt=session.host.children.at(-1); const option=prompt.children.find(b=>b.textContent===label); assert.ok(option); option.onclick(); },defer:()=>{deferRead=true;},resolveRead:()=>resolveRead(),respond:()=>rejectResponse(Error('Provider mutation intentionally rejected'))};
}

test('detail, occurrence status, RSVP and pending navigation use Focus', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar];
  h.run(`globalThis.activeSubscriptions = 0; const originalSubscribe = calendarStatuses.subscribe; calendarStatuses.subscribe = fn => { activeSubscriptions++; const stop = originalSubscribe(fn); return () => { activeSubscriptions--; stop(); }; };`);
  await h.run('calendarStatuses.refresh()'); await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
  assert.equal(h.context.activeSubscriptions,1); assert.equal(h.session.title,'Meeting'); assert.equal(h.calls.length,1);
  const root=h.session.host.firstElementChild;
  assert.match(root.html || h.session.host.html,/occurrence only/);
  root.select.value='completed'; root.select.onchange();
  const nested=h.button('Edit').onclick(); await h.flush(); h.choose('Keep editing'); await nested; assert.equal(h.session.title,'Meeting'); assert.equal(root.select.value,'completed');
  const attempt=h.session.attempt(); await h.flush(); h.choose('Keep editing'); assert.equal(await attempt,false);
  await root.save.onclick(); assert.equal(h.calls[1].url,'/root/calendar-statuses');
  assert.equal(h.calls[1].args.event_id,'occ'); assert.equal(h.calls[1].args.occurrence_start,'2026-11-05T15:00:00.000Z');
  root.select.value='pending'; root.select.onchange();
  const response=h.button('Accept').onclick(); await h.flush();
  assert.equal(await h.session.attempt(),false);
  assert.equal(h.calls[2].args.event_id,'occ'); assert.equal(h.calls[2].args.attendee,'me@example.com');
  h.respond(); await response; assert.equal(h.session.isCurrent(),true); assert.match(root.error.textContent,/intentionally rejected/); assert.equal(h.run('calendarWriteGeneration'),0);
  await h.session.close(); assert.equal(h.context.activeSubscriptions,0);
});

test('edit, dirty guard and delete cancellation stay within Focus without writes', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar];
  await h.run('calendarStatuses.refresh()'); await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
  await h.button('Edit').onclick(); assert.equal(h.session.title,'Edit event');
  h.session.host.firstElementChild.form.elements.title.value='Draft';
  const attempt=h.session.attempt(); await h.flush(); assert.equal(await h.session.attempt(),false);
  assert.equal(h.session.host.firstElementChild.form.elements.title.value,'Draft');
  h.choose('Keep editing'); assert.equal(await attempt,false);
  const cancel=h.button('Cancel').onclick(); await h.flush(); h.choose('Keep editing'); await cancel; assert.equal(h.session.title,'Edit event');
  const discard=h.button('Cancel').onclick(); await h.flush(); h.choose('Discard changes'); await discard; await h.flush(); assert.equal(h.session.title,'Meeting');
  await h.button('Delete').onclick(); assert.equal(h.session.title,'Delete event?');
  await h.button('Cancel').onclick(); await h.flush(); assert.equal(h.session.title,'Meeting'); assert.equal(h.run('calendarWriteGeneration'),0);
  await h.button('Open series').onclick(); await h.flush();
  assert.equal(h.calls.at(-1).args.event_id,'series');
  const status = h.session.host.firstElementChild.select; status.value='completed'; status.onchange();
  await h.session.host.firstElementChild.save.onclick();
  assert.equal(h.calls.at(-1).args.event_id,'occ');
  assert.equal(h.calls.at(-1).args.occurrence_start,'2026-11-05T15:00:00.000Z');
  status.value='pending'; status.onchange();
  assert.ok(h.calls.every(c=>c.url.endsWith('calendar_event_get') || c.url === '/root/calendar-statuses'));
  assert.equal(await h.session.close(),true); assert.equal(h.context.document.activeElement.focused,true);
});

test('new event opens and cancels read-only', async () => {
  const h=harness(); h.context.calendars=[h.calendar];
  await h.run('calendarEdit(null, calendars, () => {})'); await h.flush();
  assert.equal(h.session.title,'Add event'); await h.button('Cancel').onclick();
  assert.equal(h.session.isCurrent(),false); assert.equal(h.calls.length,0);
});

test('create and edit failures retain the form and exact provider target', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar];
  await h.run('calendarStatuses.refresh()'); await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
  await h.button('Edit').onclick();
  h.session.host.firstElementChild.form.elements.title.value='Changed';
  await h.button('Save').onclick();
  const update=h.calls.at(-1); assert.match(update.url,/calendar_event_update$/);
  assert.equal(update.args.connection_id,'conn'); assert.equal(update.args.calendar_id,'cal'); assert.equal(update.args.event_id,'occ');
  assert.equal(h.session.title,'Edit event'); assert.match(h.session.host.firstElementChild.error.textContent,/intentionally rejected/);
  const close=h.session.close(); await h.flush(); h.choose('Discard changes'); await close;
  await h.run('calendarEdit(null, calendars, () => {})'); await h.flush();
  const form=h.session.host.firstElementChild.form;
  form.elements.title.value='New'; form.elements.start.value='2026-11-05T10:00'; form.elements.end.value='2026-11-05T10:30';
  await h.button('Save').onclick();
  const create=h.calls.at(-1); assert.match(create.url,/calendar_event_create$/); assert.equal(create.args.connection_id,'conn'); assert.equal(create.args.calendar_id,'cal');
  assert.equal(h.session.title,'Add event'); assert.match(h.session.host.firstElementChild.error.textContent,/intentionally rejected/);
  assert.equal(h.run('calendarWriteGeneration'),0);
});


test('slow read can close and cannot repaint after disposal', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar]; h.defer();
  await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
  assert.match(h.session.host.html,/Loading event/);
  assert.equal(await h.session.close(),true); h.resolveRead(); await h.flush();
  assert.equal(h.session.isCurrent(),false); assert.match(h.session.host.html,/Loading event/);
});

test('invalidation notifies mounted listeners and prevents stale reads from repopulating cache', async () => {
  const h=harness(); let resolveList, listenerCalls=0, callbackCalls=0;
  h.context.calendars=[h.calendar];
  h.context.post = (url) => { assert.match(url,/calendar_events_list$/); return new Promise(resolve => { resolveList=resolve; }); };
  h.context.onChange=()=>{listenerCalls++;}; h.context.onRefresh=()=>{callbackCalls++;};
  h.run("calendarDirectory = {at:Date.now(),data:{calendars,errors:[],configured:true}}; calendarChanges.add(onChange)");
  const read=h.run('calendarTodayLoad()'); await h.flush();
  h.run('calendarTodayShared = {old:true}; calendarChanged(onRefresh)');
  assert.equal(listenerCalls,1); assert.equal(callbackCalls,1); assert.equal(h.run('calendarTodayShared'),null);
  resolveList({events:[],next_cursor:null}); await read;
  assert.equal(h.run('calendarTodayShared'),null);
  h.run('calendarChanges.delete(onChange); calendarChanged(onRefresh)');
  assert.equal(listenerCalls,1); assert.equal(callbackCalls,2);
});


test('isolated successful writes invalidate and notify through each UI path', async () => {
  for (const operation of ['calendar_event_create','calendar_event_update','calendar_event_delete','calendar_event_respond']) {
    const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar];
    let notifications=0, callbacks=0;
    h.context.notify=()=>{notifications++;}; h.context.refresh=()=>{callbacks++;};
    h.run("calendarChanges.add(notify); calendarTodayShared = {old:true}");
    h.success(operation);
    if (operation === 'calendar_event_create') {
      await h.run('calendarEdit(null, calendars, refresh)'); await h.flush();
      const form=h.session.host.firstElementChild.form;
      form.elements.title.value='New'; form.elements.start.value='2026-11-05T10:00'; form.elements.end.value='2026-11-05T10:30';
    } else {
      await h.run('calendarStatuses.refresh()'); await h.run('calendarOpen(event, calendars, refresh)'); await h.flush();
      if (operation === 'calendar_event_update') { await h.button('Edit').onclick(); h.session.host.firstElementChild.form.elements.title.value='Changed'; }
      if (operation === 'calendar_event_delete') await h.button('Delete').onclick();
    }
    const label = operation === 'calendar_event_delete' ? 'Delete' : operation === 'calendar_event_respond' ? 'Accept' : 'Save';
    await h.button(label).onclick();
    assert.match(h.calls.at(-1).url,new RegExp(operation+'$'));
    assert.equal(h.calls.at(-1).args.connection_id,'conn'); assert.equal(h.calls.at(-1).args.calendar_id,'cal');
    if (operation !== 'calendar_event_create') assert.equal(h.calls.at(-1).args.event_id,'occ');
    assert.equal(notifications,1); assert.equal(callbacks,1); assert.equal(h.run('calendarTodayShared'),null); assert.equal(h.session.isCurrent(),false);
  }
});


test('failed deletion leaves confirmation and cache untouched', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar];
  await h.run('calendarStatuses.refresh()'); await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
  await h.button('Delete').onclick(); await h.button('Delete').onclick();
  assert.equal(h.session.title,'Delete event?'); assert.match(h.session.host.firstElementChild.error.textContent,/intentionally rejected/);
  assert.equal(h.run('calendarWriteGeneration'),0);
});

test('lost delete is retained across close and retried with the original request', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar];
  await h.run('calendarStatuses.refresh()'); await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
  await h.button('Delete').onclick(); await h.button('Delete').onclick();
  const first=h.calls.at(-1); assert.match(h.session.host.firstElementChild.error.textContent,/outcome is unconfirmed/i);
  assert.equal(h.run('calendarAttempts.size'),1); await h.session.close();
  h.success('calendar_event_delete'); await h.run('calendarReviewOpen(document.activeElement)'); await h.flush();
  const panel=h.session.host.children.at(-1), row=panel.children[2].children[0];
  await row.children[1].onclick(); const retry=h.calls.at(-1);
  assert.deepEqual(retry.args,first.args); assert.equal(h.run('calendarAttempts.size'),0);
});

test('recovery respects fresh owner and preserves another unsaved edit', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar];
  await h.run('calendarStatuses.refresh()'); await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
  await h.button('Delete').onclick(); await h.button('Delete').onclick(); await h.session.close();
  await h.run('calendarEdit(null, calendars, () => {})'); await h.flush();
  const root=h.session.host.firstElementChild; root.form.elements.title.value='Another draft';
  h.setOwner(null); const before=h.calls.length; await h.button('Save').onclick(); assert.equal(h.calls.length,before); assert.match(root.error.textContent,/owner session/i);
  h.setOwner('owner-b'); await root.review.onclick(); let panel=h.session.host.children.at(-1); assert.equal(panel.children[2].children.length,0); panel.children.at(-1).onclick();
  h.setOwner('owner-a'); await root.review.onclick(); panel=h.session.host.children.at(-1); const row=panel.children[2].children[0];
  h.setOwner('owner-b'); await row.children[1].onclick(); assert.equal(h.calls.length,before); assert.equal(h.run('calendarAttempts.size'),1);
  panel.children.at(-1).onclick(); h.setOwner('owner-a'); await root.review.onclick(); panel=h.session.host.children.at(-1);
  h.success('calendar_event_delete'); await panel.children[2].children[0].children[1].onclick(); panel.children.at(-1).onclick();
  assert.equal(root.form.elements.title.value,'Another draft'); assert.equal(h.session.host.firstElementChild,root); assert.equal(h.run('calendarAttempts.size'),0);
});

test('matching uncertain creates require a choice and remain independent', async () => {
  const h=harness(); h.context.calendars=[h.calendar];
  const fill=()=>{const f=h.session.host.firstElementChild.form.elements; f.title.value='New'; f.start.value='2026-11-05T10:00'; f.end.value='2026-11-05T10:30';};
  await h.run('calendarEdit(null, calendars, () => {})'); await h.flush(); fill(); await h.button('Save').onclick();
  const first=h.calls.at(-1); assert.equal(h.run('calendarAttempts.size'),1);
  const close=h.session.close(); await h.flush(); h.choose('Discard changes'); await close;
  await h.run('calendarEdit(null, calendars, () => {})'); await h.flush(); fill();
  const save=h.button('Save').onclick(); await h.flush(); assert.equal(h.calls.length,1); h.choose('Create separate event'); await save;
  assert.equal(h.run('calendarAttempts.size'),2); assert.notEqual(h.calls.at(-1).args.idempotency_key,first.args.idempotency_key);
});


test('uncertain personal statuses remain separate for recurring occurrences', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar];
  await h.run('calendarStatuses.refresh()');
  for (const start of ['2026-11-05T15:00:00Z','2026-11-06T15:00:00Z']) {
    h.context.event={...h.occurrence,start};
    await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
    const select=h.session.host.firstElementChild.select; select.value='completed'; select.onchange();
    await h.session.host.firstElementChild.save.onclick();
    const close=h.session.close(); await h.flush(); h.choose('Discard changes'); await close;
  }
  assert.equal(h.run('calendarAttempts.size'),2);
  const starts=h.run('[...calendarAttempts.values()].map(a => a.args.target.occurrence_start)');
  assert.deepEqual(Array.from(starts),['2026-11-05T15:00:00.000Z','2026-11-06T15:00:00.000Z']);
});
