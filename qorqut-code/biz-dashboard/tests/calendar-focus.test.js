const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

// Minimal DOM and provider: no calendar request leaves this process.
function harness() {
  const calls = [], notices = [], subscriptions = [];
  let session, allowDiscard = false, rejectResponse;
  class Element {
    constructor() { this.children = []; this.dataset = {}; this.isConnected = true; this.value = ''; this.textContent = ''; this.className = ''; }
    append(child) { this.children.push(child); }
    after(child) { this.retry = child; }
    remove() { this.removed = true; }
    focus() { this.focused = true; }
    querySelectorAll(selector) { return selector === '.mfoot button' ? this.footer.children : []; }
    set innerHTML(value) {
      this.html = value;
      if (value.includes('calendar-focus-content')) {
        const root = this.firstElementChild = new Element(); root.footer = new Element(); root.error = new Element(); root.note = new Element(); root.select = new Element(); root.save = new Element(); root.meetOption = new Element();
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
    esc:s=>String(s ?? ''),toast:s=>notices.push(s),confirm:()=>allowDiscard,coreData:x=>x,accessLost:()=>false,clearEtagCache:()=>{},newRequestId:()=> 'request-id',AICONS:{},T_VIEW_ICON:{calendar:''},
    api:async()=>({items:[]}),post:async (url,args) => {calls.push({url,args}); if (url.endsWith('calendar_event_get')) return {event:{...occurrence,id:args.event_id}}; if (url.endsWith('calendar_event_respond')) return new Promise((resolve,reject)=>{rejectResponse=reject;}); throw Error('Provider mutation intentionally rejected');}};
  vm.createContext(context); vm.runInContext(fs.readFileSync(path.join(__dirname,'../js/global-calendar.js'),'utf8'),context);
  const run = code => vm.runInContext(code,context);
  const flush = async () => { for(let i=0;i<8;i++) await Promise.resolve(); };
  const button = label => session.host.firstElementChild.footer.children.find(b=>b.textContent===label);
  return {context,run,occurrence,calendar,calls,notices,flush,button,get session(){return session;},allow:()=>{allowDiscard=true;},respond:()=>rejectResponse(Error('Provider mutation intentionally rejected'))};
}

test('detail, occurrence status, RSVP and pending navigation use Focus', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar];
  h.run(`globalThis.activeSubscriptions = 0; const originalSubscribe = calendarStatuses.subscribe; calendarStatuses.subscribe = fn => { activeSubscriptions++; const stop = originalSubscribe(fn); return () => { activeSubscriptions--; stop(); }; };`);
  await h.run('calendarStatuses.refresh()'); await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
  assert.equal(h.context.activeSubscriptions,1); assert.equal(h.session.title,'Meeting'); assert.equal(h.calls.length,1);
  const root=h.session.host.firstElementChild;
  assert.match(root.html || h.session.host.html,/occurrence only/);
  root.select.value='completed'; root.select.onchange(); assert.equal(await h.session.attempt(),false);
  await root.save.onclick(); assert.equal(h.calls[1].url,'/root/calendar-statuses');
  assert.equal(h.calls[1].args.event_id,'occ'); assert.equal(h.calls[1].args.occurrence_start,'2026-11-05T15:00:00.000Z');
  root.select.value='pending'; root.select.onchange();
  const response=h.button('Accept').onclick(); await h.flush();
  assert.equal(await h.session.attempt(),false);
  assert.equal(h.calls[2].args.event_id,'occ'); assert.equal(h.calls[2].args.attendee,'me@example.com');
  h.respond(); await response; assert.equal(h.session.isCurrent(),true); assert.match(root.error.textContent,/intentionally rejected/);
  await h.session.close(); assert.equal(h.context.activeSubscriptions,0);
});

test('edit, dirty guard and delete cancellation stay within Focus without writes', async () => {
  const h=harness(); h.context.event=h.occurrence; h.context.calendars=[h.calendar];
  await h.run('calendarStatuses.refresh()'); await h.run('calendarOpen(event, calendars, () => {})'); await h.flush();
  await h.button('Edit').onclick(); assert.equal(h.session.title,'Edit event');
  h.session.host.firstElementChild.form.elements.title.value='Draft';
  assert.equal(await h.session.attempt(),false); await h.button('Cancel').onclick(); assert.equal(h.session.title,'Edit event');
  h.allow(); await h.button('Cancel').onclick(); await h.flush(); assert.equal(h.session.title,'Meeting');
  await h.button('Delete').onclick(); assert.equal(h.session.title,'Delete event?');
  await h.button('Cancel').onclick(); await h.flush(); assert.equal(h.session.title,'Meeting');
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
  h.allow(); await h.session.close();
  await h.run('calendarEdit(null, calendars, () => {})'); await h.flush();
  const form=h.session.host.firstElementChild.form;
  form.elements.title.value='New'; form.elements.start.value='2026-11-05T10:00'; form.elements.end.value='2026-11-05T10:30';
  await h.button('Save').onclick();
  const create=h.calls.at(-1); assert.match(create.url,/calendar_event_create$/); assert.equal(create.args.connection_id,'conn'); assert.equal(create.args.calendar_id,'cal');
  assert.equal(h.session.title,'Add event'); assert.match(h.session.host.firstElementChild.error.textContent,/intentionally rejected/);
});
