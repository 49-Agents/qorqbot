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
    querySelectorAll(selector) { return selector === '.mfoot button' ? this.footer.children : selector === '[data-calendar-status-choice]' ? this.pills || [] : []; }
    set innerHTML(value) {
      this.html = value;
      if (value.includes('calendar-focus-content')) {
        const root = this.firstElementChild = new Element(); root.footer = new Element(); root.error = new Element(); root.note = new Element(); root.pills = ['pending','completed','cancelled','rescheduled'].map(v => { const pill = new Element(); pill.dataset.calendarStatusChoice = v; pill['aria-pressed'] = 'false'; return pill; }); root.meetOption = new Element(); root.review = new Element();
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
    if (selector === '[data-calendar-status-retry]') return el.note.retry?.removed ? null : el.note.retry;
    if (selector === '[data-meet-option]') return el.meetOption;
    if (selector === '[data-calendar-review]') return el.review;
    if (selector === 'form') return el.form;
    throw Error('Unexpected selector '+selector);
  };
  const occurrence = {connection_id:'conn',calendar_id:'cal',id:'occ',revision:'rev',title:'Meeting',start:'2026-11-05T15:00:00Z',end:'2026-11-05T16:00:00Z',series_id:'series',recurrence:[],attendees:[{email:'me@example.com',status:'needsAction'}],organizer:'owner@example.com',default_reminders:true,reminders:[]};
  const calendar = {id:'cal',connection_id:'conn',writable:true,provider:'google',name:'Work',organizers:['me@example.com']};
  const windowListeners = new Set();
  const context = {window:{addEventListener:(name,fn)=>{if(name==='focus') windowListeners.add(fn);},removeEventListener:(name,fn)=>windowListeners.delete(fn),focusExpansion:{async open(spec) {
    const host = new Element(); let current = true, before = () => true, disposers = [];
    session = {host,isCurrent:()=>current,setTitle(t){this.title=t;},setBeforeLeave(fn){before=fn;},onDispose(fn){disposers.push(fn);},async close(){if (!await before()) return false; current=false; disposers.forEach(fn=>fn()); return true;},attempt:async()=>before()};
    spec.mount(host,session); return session;
  }}},document,$,console,Intl,URL,Date,Map,Set,Promise,JSON,Object,Array,Number,String,RegExp,Error,
    FormData:class { constructor(form) { this.items = Object.values(form.elements).filter(f => !f.disabled && (f.type !== 'checkbox' || f.checked)).map(f => [f.name,f.value]); } [Symbol.iterator]() {return this.items[Symbol.iterator]();} },
    esc:s=>String(s ?? ''),toast:s=>notices.push(s),coreData:x=>x,accessLost:()=>false,clearEtagCache:()=>{},newRequestId:()=> 'request-'+(++requestNumber),loadSession:async()=>owner ? {principal_id:owner,principal_kind:'human',is_owner:true} : null,AICONS:{},T_VIEW_ICON:{calendar:''},
    api:async()=>({items:[]}),post:async (url,args) => {calls.push({url,args}); if (url.endsWith('calendar_event_get')) { if (deferRead) return new Promise(resolve=>{resolveRead=()=>resolve({event:{...occurrence,id:args.event_id}});}); return {event:{...occurrence,id:args.event_id}}; } if (successOperation && url.endsWith(successOperation)) return {}; if (url.endsWith('calendar_event_respond')) return new Promise((resolve,reject)=>{rejectResponse=reject;}); throw Error('Provider mutation intentionally rejected');}};
  vm.createContext(context);
  for (const file of ['san-juan-time','global-calendar']) vm.runInContext(fs.readFileSync(path.join(__dirname,`../js/${file}.js`),'utf8'),context);
  const run = code => vm.runInContext(code,context);
  const flush = async () => { for(let i=0;i<8;i++) await Promise.resolve(); };
  const button = label => session.host.firstElementChild.footer.children.find(b=>b.textContent===label);
  return {context,run,occurrence,calendar,calls,notices,flush,button,get session(){return session;},focusWindow:()=>{for (const fn of windowListeners) fn();},success: operation => {successOperation=operation;},setOwner: value => {owner=value;},choose: label => { const prompt=session.host.children.at(-1); const option=prompt.children.find(b=>b.textContent===label); assert.ok(option); option.onclick(); },defer:()=>{deferRead=true;},resolveRead:()=>resolveRead(),respond:()=>rejectResponse(Error('Provider mutation intentionally rejected'))};
}

const pill = (root, status) => root.pills.find(p => p.dataset.calendarStatusChoice === status);

test('editing app-local times preserves unchanged instants and the provider recurrence zone', async () => {
  const h = harness();
  h.context.calendars = [h.calendar];
  h.context.event = {...h.occurrence,start:'2026-11-01T06:30:45Z',end:'2026-11-01T08:00:00Z',timezone:'America/Los_Angeles'};
  h.run("sjConfigure('America/New_York')");
  await h.run('calendarEdit(event,calendars,()=>{})'); await h.flush();
  const f = h.session.host.firstElementChild.form.elements;
  assert.equal(f.start.value,'2026-11-01T01:30');
  assert.equal(f.end.value,'2026-11-01T03:00');
  f.end.value = '2026-11-01T04:00';
  await h.button('Save').onclick();
  const changes = h.calls.at(-1).args.changes;
  assert.equal(changes.start,'2026-11-01T06:30:45Z');
  assert.equal(changes.end,'2026-11-01T09:00:00.000Z');
  assert.equal(changes.timezone,'America/Los_Angeles');
});

test('new timed events use the app zone and invalid DST times never reach the provider', async () => {
  const h = harness(); h.context.calendars = [h.calendar];
  h.run("sjConfigure('America/New_York')");
  await h.run("calendarEdit(null,calendars,()=>{},'2026-03-08')"); await h.flush();
  const root = h.session.host.firstElementChild, f = root.form.elements;
  assert.equal(f.start.value,'2026-03-08T10:00');
  f.title.value = 'New'; f.start.value = '2026-03-08T02:30'; f.end.value = '2026-03-08T03:30';
  await h.button('Save').onclick();
  assert.equal(h.calls.length,0);
  assert.match(root.error.textContent,/daylight-saving gap/);
  f.start.value = '2026-03-08T03:00';
  await h.button('Save').onclick();
  assert.equal(h.calls.at(-1).args.start,'2026-03-08T07:00:00.000Z');
  assert.equal(h.calls.at(-1).args.end,'2026-03-08T07:30:00.000Z');
  assert.equal(h.calls.at(-1).args.timezone,'America/New_York');
});

test('detail, occurrence status, RSVP and pending navigation use Focus', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar];
  h.run(`globalThis.activeSubscriptions = 0; const originalSubscribe = calendarStatuses.subscribe; calendarStatuses.subscribe = fn => { activeSubscriptions++; const stop = originalSubscribe(fn); return () => { activeSubscriptions--; stop(); }; };`);
  await h.run('calendarStatuses.refresh()'); await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
  assert.equal(h.context.activeSubscriptions,1); assert.equal(h.session.title,'Meeting'); assert.equal(h.calls.length,1);
  const root=h.session.host.firstElementChild;
  assert.match(root.html || h.session.host.html,/occurrence only/);
  assert.equal(pill(root,'pending')['aria-pressed'],'true'); assert.equal(h.calls.length,1);
  await pill(root,'pending').onclick(); assert.equal(h.calls.length,1);
  await pill(root,'completed').onclick(); assert.equal(h.calls[1].url,'/root/calendar-statuses');
  assert.equal(h.calls[1].args.event_id,'occ'); assert.equal(h.calls[1].args.occurrence_start,'2026-11-05T15:00:00.000Z');
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
  await pill(h.session.host.firstElementChild,'completed').onclick();
  assert.equal(h.calls.at(-1).args.event_id,'occ');
  assert.equal(h.calls.at(-1).args.occurrence_start,'2026-11-05T15:00:00.000Z');
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

test('invalidation notifies mounted listeners and runs the refresh callback', async () => {
  const h=harness(); let listenerCalls=0, callbackCalls=0;
  h.context.onChange=()=>{listenerCalls++;}; h.context.onRefresh=()=>{callbackCalls++;};
  h.run('calendarChanges.add(onChange)');
  const before=h.run('calendarWriteGeneration');
  h.run('calendarChanged(onRefresh)');
  assert.equal(listenerCalls,1); assert.equal(callbackCalls,1); assert.equal(h.run('calendarWriteGeneration'),before+1);
  h.run('calendarChanges.delete(onChange); calendarChanged(onRefresh)');
  assert.equal(listenerCalls,1); assert.equal(callbackCalls,2);
});


test('isolated successful writes invalidate and notify through each UI path', async () => {
  for (const operation of ['calendar_event_create','calendar_event_update','calendar_event_delete','calendar_event_respond']) {
    const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar];
    let notifications=0, callbacks=0;
    h.context.notify=()=>{notifications++;}; h.context.refresh=()=>{callbacks++;};
    h.run("calendarChanges.add(notify)");
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
    assert.equal(notifications,1); assert.equal(callbacks,1); assert.equal(h.session.isCurrent(),false);
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
    await pill(h.session.host.firstElementChild,'completed').onclick();
    assert.equal(await h.session.close(),true);
  }
  assert.equal(h.run('calendarAttempts.size'),2);
  const starts=h.run('[...calendarAttempts.values()].map(a => a.args.target.occurrence_start)');
  assert.deepEqual(Array.from(starts),['2026-11-05T15:00:00.000Z','2026-11-06T15:00:00.000Z']);
});

test('ordinary update, delete and RSVP controls offer the stored request after reopening', async () => {
  for (const operation of ['calendar_event_update','calendar_event_delete','calendar_event_respond']) {
    const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar]; await h.run('calendarStatuses.refresh()');
    const open = async () => { await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
      if (operation === 'calendar_event_update') { await h.button('Edit').onclick(); h.session.host.firstElementChild.form.elements.title.value='Changed'; }
      if (operation === 'calendar_event_delete') await h.button('Delete').onclick(); };
    const label=operation === 'calendar_event_update' ? 'Save' : operation === 'calendar_event_delete' ? 'Delete' : 'Accept';
    await open(); const firstClick=h.button(label).onclick(); await h.flush(); if(operation === 'calendar_event_respond') h.respond(); await firstClick;
    const first=h.calls.at(-1), close=h.session.close(); await h.flush();
    if(operation === 'calendar_event_update') h.choose('Discard changes'); await close;
    await open(); const prior=h.calls.length, click=h.button(label).onclick(); await h.flush();
    assert.equal(h.calls.length,prior); h.success(operation); h.choose('Retry same request'); await click;
    assert.deepEqual(h.calls.at(-1).args,first.args); assert.equal(h.run('calendarAttempts.size'),0);
  }
});

test('ordinary status save offers the retained occurrence request after reopening', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar]; await h.run('calendarStatuses.refresh()');
  const open=async()=>{await h.run('calendarOpen(event, calendars, () => {})'); await h.flush(); const root=h.session.host.firstElementChild; return root;};
  let root=await open(); await pill(root,'completed').onclick(); const first=h.calls.at(-1);
  assert.equal(await h.session.close(),true);
  root=await open(); const prior=h.calls.length, save=pill(root,'completed').onclick(); await h.flush(); assert.equal(h.calls.length,prior);
  h.choose('Retry same request'); await save; assert.deepEqual(h.calls.at(-1).args,first.args); assert.equal(h.run('calendarAttempts.size'),1);
});

test('a refresh exception cannot turn a confirmed write into an uncertain attempt', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar]; h.context.throwRefresh=()=>{throw Error('paint failed');};
  await h.run('calendarStatuses.refresh()'); await h.run('calendarOpen(event, calendars, throwRefresh)'); await h.flush();
  h.success('calendar_event_delete'); await h.button('Delete').onclick(); await h.button('Delete').onclick();
  assert.equal(h.run('calendarAttempts.size'),0); assert.equal(h.session.isCurrent(),false);
});

test('window focus immediately hides recovery rows until owner revalidation', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar];
  await h.run('calendarStatuses.refresh()'); await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
  await h.button('Delete').onclick(); await h.button('Delete').onclick(); await h.session.close();
  await h.run('calendarReviewOpen(document.activeElement)'); await h.flush(); const panel=h.session.host.children.at(-1), list=panel.children[2];
  assert.equal(list.children.length,1); h.setOwner(null); h.focusWindow(); assert.equal(list.children.length,0); await h.flush();
  assert.equal(panel.children[3].hidden,false); assert.match(panel.children[1].textContent,/owner session/i);
  h.setOwner('owner-b'); await panel.children[3].onclick(); assert.equal(list.children.length,0);
  h.setOwner('owner-a'); h.focusWindow(); await h.flush(); assert.equal(list.children.length,1);
});

test('detail renders labelled facts, safe links, mailto display and guest statuses', () => {
  const h=harness(); const esc=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); h.context.esc=esc;
  const html=h.run(`calendarDetailBody(${JSON.stringify({...h.occurrence,organizer:'MAILTO:boss@example.com',location:'Room 4',description:'Line1\n  see https://ok.example/a?b=1&c=2. and javascript:alert(1) <b>x</b>',attendees:[{email:'mailto:me@example.com',status:'needsAction'},{email:'x@example.com',status:'weird<'}]})},{name:'Work'},{email:'mailto:me@example.com',status:'needsAction'},true,{})`);
  assert.match(html,/<dt>Organizer<\/dt><dd>boss@example\.com<\/dd>/); assert.doesNotMatch(html,/mailto:/i);
  assert.match(html,/<a href="https:\/\/ok\.example\/a\?b=1&amp;c=2" target="_blank" rel="noopener noreferrer">https:\/\/ok\.example\/a\?b=1&amp;c=2<\/a>\./);
  assert.match(html,/javascript:alert\(1\) &lt;b&gt;x&lt;\/b&gt;/); assert.doesNotMatch(html,/href="javascript/);
  assert.match(html,/Guests \(2\)/); assert.match(html,/Awaiting response/); assert.match(html,/weird&lt;/);
  assert.match(html,/role="group" aria-label="Your tracking status"/); assert.equal((html.match(/data-calendar-status-choice=/g) || []).length,4); assert.doesNotMatch(html,/Save status|<select/);
  assert.match(html,/Your tracking status/); assert.match(html,/Line1\n  see/);
});

test('status pills: busy lock, definite-failure retry identity, conflict adoption and unknown state', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar];
  const base=h.context.post; let release, mode='hold', busyStates=[];
  const item=(status,revision)=>({connection_id:'conn',calendar_id:'cal',event_id:'occ',occurrence_start:'2026-11-05T15:00:00.000Z',status,revision});
  h.context.post=async (url,args)=>{
    if (url !== '/root/calendar-statuses') return base(url,args);
    h.calls.push({url,args});
    if (mode==='hold') return new Promise(resolve=>{release=()=>resolve({item:item(args.status,(args.expected_revision||0)+1)});});
    if (mode==='reject') throw Object.assign(new Error('Bad status'),{httpStatus:422});
    if (mode==='conflict') throw Object.assign(new Error('stale'),{httpStatus:409});
    return {item:item(args.status,(args.expected_revision||0)+1)};
  };
  h.context.api=async()=>({items:h.context.stored||[]});
  await h.run('calendarStatuses.refresh()'); await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
  const root=h.session.host.firstElementChild, note=root.note, writes=()=>h.calls.filter(c=>c.url==='/root/calendar-statuses');
  const pressed=()=>root.pills.filter(p=>p['aria-pressed']==='true').map(p=>p.dataset.calendarStatusChoice);
  assert.deepEqual(pressed(),['pending']); assert.equal(writes().length,0);
  // Busy: one write, every pill disabled, new status not claimed before confirmation.
  const first=pill(root,'completed').onclick(); await h.flush(); await pill(root,'cancelled').onclick(); await pill(root,'completed').onclick();
  assert.equal(writes().length,1); assert.ok(root.pills.every(p=>p.disabled)); assert.deepEqual(pressed(),['pending']);
  mode='ok'; release(); await first; assert.deepEqual(pressed(),['completed']); assert.ok(root.pills.every(p=>!p.disabled)); assert.match(note.textContent,/Saved: Completed/);
  await pill(root,'completed').onclick(); assert.equal(writes().length,1);
  // Definite failure keeps the confirmed pill and a second click reuses the same request id.
  mode='reject'; await pill(root,'cancelled').onclick(); assert.deepEqual(pressed(),['completed']); assert.match(note.textContent,/Click the status again to retry/);
  mode='ok'; await pill(root,'cancelled').onclick();
  const [failed,retried]=writes().slice(-2); assert.equal(retried.args.request_id,failed.args.request_id); assert.equal(retried.args.expected_revision,failed.args.expected_revision); assert.deepEqual(pressed(),['cancelled']);
  // Conflict adopts the confirmed current value and asks to choose again.
  h.context.stored=[item('rescheduled',9)]; mode='conflict'; await pill(root,'pending').onclick();
  assert.deepEqual(pressed(),['rescheduled']); assert.match(note.textContent,/Changed elsewhere. Now Rescheduled; choose again/);
});

test('status pills: unknown state selects nothing, disables pills and offers Retry', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar]; h.context.api=async()=>{throw Error('offline');};
  await h.run('calendarStatuses.refresh()'); await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
  const root=h.session.host.firstElementChild;
  assert.ok(root.pills.every(p=>p.disabled && p['aria-pressed']==='false')); assert.match(root.note.textContent,/Status unknown/);
  await root.pills[1].onclick(); assert.equal(h.calls.filter(c=>c.url==='/root/calendar-statuses').length,0);
  assert.ok(root.note.retry); h.context.api=async()=>({items:[]}); await root.note.retry.onclick();
  assert.deepEqual(root.pills.filter(p=>p['aria-pressed']==='true').map(p=>p.dataset.calendarStatusChoice),['pending']); assert.ok(root.pills.every(p=>!p.disabled));
});
