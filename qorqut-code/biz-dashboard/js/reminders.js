// Shared reminders: one API wrapper, one detail view and one editor for the owner strip (Pinned and Attention),
// the Reminders button's upcoming list and the record reader. Mutations carry the fetched revision and occurrence.
const REM_STATES = {scheduled: 'Scheduled', due: 'Due', dismissed: 'Dismissed', cancelled: 'Cancelled'};
const REM_WEEKDAYS = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const REM_TEXT_MAX = 240;

const remId = reminder => reminder?.reminder_id ?? reminder?.id ?? null;
// Legacy plain results carry `id`; keep one shape for the views below.
const remNormalize = reminder => reminder ? {...reminder, reminder_id: remId(reminder)} : reminder;
const remMutable = reminder => !!remId(reminder) && reminder.revision != null && ['scheduled', 'due'].includes(reminder.state);
const remZone = () => { try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch (_) { return 'UTC'; } };
const remSelf = () => state.session?.principal_id || null;
const remIsManager = () => state.session?.is_owner === true;

// A reminder in a business scope is read and written through that business's route; Global uses the root route.
function remPath(reminder, op, biz = null) {
  const slug = biz || (reminder?.business_id != null
    ? (state.bizs || []).find(b => b.id === reminder.business_id || b.business_id === reminder.business_id)?.slug : null);
  return slug ? `/biz/${encodeURIComponent(slug)}/operations/${op}` : `/root/operations/${op}`;
}
async function remCall(reminder, op, args = {}, biz = null) {
  return coreData(await post(remPath(reminder, op, biz), args));
}

const remPad = n => String(n).padStart(2, '0');
// datetime-local text -> ISO 8601 with the browser's explicit offset for that wall time.
function remOffsetISO(local) {
  const date = new Date(local);
  if (!local || !Number.isFinite(date.getTime())) return null;
  const offset = -date.getTimezoneOffset(), sign = offset < 0 ? '-' : '+', abs = Math.abs(offset);
  return `${local.length === 16 ? local + ':00' : local}${sign}${remPad(Math.floor(abs / 60))}:${remPad(abs % 60)}`;
}
function remLocalInput(value) {
  const date = new Date(value);
  if (!value || !Number.isFinite(date.getTime())) return '';
  return `${date.getFullYear()}-${remPad(date.getMonth() + 1)}-${remPad(date.getDate())}T${remPad(date.getHours())}:${remPad(date.getMinutes())}`;
}
function remFormatTime(value, zone) {
  if (!value || !Number.isFinite(Date.parse(value))) return '—';
  const options = {weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZoneName: 'short'};
  try { return new Intl.DateTimeFormat('en-US', {...options, ...(zone ? {timeZone: zone} : {})}).format(new Date(value)); }
  catch (_) { return new Intl.DateTimeFormat('en-US', options).format(new Date(value)); }
}
// Simple daily/weekly schedules map onto the existing five-field cron; anything else is edited as custom cron.
function remCronParts(schedule) {
  const match = /^(\d{1,2}) (\d{1,2}) \* \* (\*|[0-6])$/.exec(String(schedule || '').trim());
  if (!match || +match[1] > 59 || +match[2] > 23) return null;
  return {time: `${remPad(+match[2])}:${remPad(+match[1])}`, weekday: match[3] === '*' ? null : Number(match[3])};
}
function remRecurrenceText(recurrence) {
  if (!recurrence) return 'One time';
  const parts = remCronParts(recurrence.schedule), zone = recurrence.timezone ? ` (${recurrence.timezone})` : '';
  if (!parts) return `Repeats · ${recurrence.schedule}${zone}`;
  return parts.weekday == null ? `Daily at ${parts.time}${zone}` : `Every ${REM_WEEKDAYS[parts.weekday]} at ${parts.time}${zone}`;
}
const remLinkText = link => link ? `${({task: 'Work', email: 'Email', project: 'Project', table: 'Table', signoff: 'Signoff', action: 'Action'}[link.type] || link.type)} ${link.id}` : '';
const remWho = reminder => reminder.recipient_name || reminder.recipient_display_name || (reminder.recipient_id && reminder.recipient_id === remSelf() ? 'You' : reminder.recipient_id) || '—';
const remFail = error => /conflict|stale|revision|precondition|state_changed|occurrence/i.test(String(error?.code || ''));

// Snooze presets use the viewer's own clock and write an explicit offset.
const REM_SNOOZES = [['1h', '1 hour'], ['3h', '3 hours'], ['tomorrow', 'Tomorrow 9:00'], ['week', 'Next week']];
function remSnoozeTime(option, now = new Date()) {
  if (option === '1h' || option === '3h') return new Date(now.getTime() + (option === '1h' ? 1 : 3) * 3600000);
  const next = new Date(now); next.setHours(9, 0, 0, 0); next.setDate(next.getDate() + (option === 'week' ? 7 : 1));
  return next;
}
const remInputOf = date => `${date.getFullYear()}-${remPad(date.getMonth() + 1)}-${remPad(date.getDate())}T${remPad(date.getHours())}:${remPad(date.getMinutes())}`;

// ---------------------------------------------------------------- detail (shared by modal and record reader)
// Controller for one reminder. `opts.onChange(reminder|null, op)` runs after an applied or refreshed result.
function remindersDetailMount(host, initial, opts = {}) {
  let current = remNormalize(initial), busy = false, retry = null, error = '', gone = false, snoozeOpen = false, confirmCancel = false, disposed = false;
  const box = document.createElement('div');
  box.className = 'rem-detail';
  host.append(box);
  const alive = () => !disposed && box.isConnected;
  const node = (tag, className, text) => { const n = document.createElement(tag); if (className) n.className = className; if (text != null) n.textContent = text; return n; };

  function paint() {
    box.replaceChildren();
    const r = current, mutable = !gone && remMutable(r);
    box.append(node('p', 'rw-reminder-text rem-text', r.text || 'Reminder'));
    const fields = [['State', REM_STATES[r.state] || r.state || '—']];
    if (r.state === 'due' && r.due_at) fields.push(['Due', remFormatTime(r.due_at, r.recurrence?.timezone)]);
    const next = r.state === 'due' ? r.next_at : (r.next_at || r.at);
    if (next && r.state !== 'dismissed' && r.state !== 'cancelled') fields.push([r.state === 'due' && r.recurrence ? 'Next' : 'Scheduled for', remFormatTime(next, r.recurrence?.timezone)]);
    else if (r.at && !next) fields.push(['Scheduled for', remFormatTime(r.at, r.recurrence?.timezone)]);
    fields.push(['Repeat', remRecurrenceText(r.recurrence)], ['For', remWho(r)]);
    if (r.time_critical) fields.push(['Priority', 'Time-critical']);
    const list = node('dl', 'rem-fields');
    for (const [label, value] of fields) list.append(node('dt', '', label), node('dd', '', value));
    if (r.linked_item && r.link_available !== false) {
      const open = typeof agentOpenRecordReference === 'function';
      const cell = node('dd', '');
      if (open) {
        const b = node('button', 'quiet rem-link-open', remLinkText(r.linked_item)); b.type = 'button';
        b.setAttribute('aria-label', 'Open linked ' + remLinkText(r.linked_item));
        b.onclick = () => void Promise.resolve(agentOpenRecordReference(r.linked_item)).catch(() => toast('The linked item could not be opened.'));
        cell.append(b);
      } else cell.textContent = remLinkText(r.linked_item);
      list.append(node('dt', '', 'Linked'), cell);
    } else if (r.link_available === false) list.append(node('dt', '', 'Linked'), node('dd', 'muted', 'Not available to you'));
    box.append(list);
    const message = node('p', 'ferr rem-error', error); message.setAttribute('role', 'alert'); message.hidden = !error;
    box.append(message);
    if (gone) { box.append(node('p', 'muted', 'This reminder is no longer available.')); return; }
    if (!mutable) return;
    const actions = node('div', 'rem-actions');
    const add = (label, handler, cls = 'quiet') => { const b = node('button', cls, label); b.type = 'button'; b.disabled = busy; b.onclick = handler; actions.append(b); return b; };
    if (r.state === 'due' && r.occurrence_id) add('Dismiss', () => run('reminders_dismiss', {occurrence_id: r.occurrence_id}), 'primary');
    add('Snooze…', () => { snoozeOpen = !snoozeOpen; confirmCancel = false; paint(); });
    add('Edit', () => remindersEditor(current, {biz: opts.biz, onSaved: next => { current = remNormalize(next); error = ''; paint(); opts.onChange?.(current, 'reminders_update'); }}));
    add(confirmCancel ? 'Confirm cancel' : 'Cancel reminder', () => {
      if (!confirmCancel) { confirmCancel = true; snoozeOpen = false; paint(); return; }
      run('reminders_cancel', {});
    }, confirmCancel ? 'danger' : 'quiet');
    box.append(actions);
    if (confirmCancel) box.append(node('p', 'muted rem-note', r.recurrence ? 'This stops the whole repeating reminder.' : 'This cancels the reminder.'));
    if (snoozeOpen) box.append(snoozePicker());
  }
  function snoozePicker() {
    const picker = node('div', 'rem-snooze');
    picker.append(node('p', 'muted rem-note', current.recurrence ? 'Skips repeats until then, then resumes the schedule.' : 'Returns at the chosen time.'));
    const row = node('div', 'rem-snooze-presets');
    for (const [key, label] of REM_SNOOZES) {
      const b = node('button', 'quiet', label); b.type = 'button'; b.disabled = busy;
      b.onclick = () => choose(remSnoozeTime(key));
      row.append(b);
    }
    picker.append(row);
    const custom = node('div', 'rem-snooze-custom');
    const input = node('input', 'inp'); input.type = 'datetime-local'; input.setAttribute('aria-label', 'Snooze until'); input.value = remInputOf(remSnoozeTime('1h'));
    const go = node('button', 'quiet', 'Snooze'); go.type = 'button'; go.disabled = busy;
    go.onclick = () => choose(new Date(input.value));
    custom.append(input, go);
    picker.append(custom);
    return picker;
  }
  function choose(date) {
    if (!Number.isFinite(date.getTime()) || date.getTime() <= Date.now()) { error = 'Choose a future date and time.'; paint(); return; }
    void run('reminders_snooze', {at: remOffsetISO(remInputOf(date))});
  }
  async function reload() {
    try {
      const data = await remCall(current, 'reminders_get', {reminder_id: remId(current)}, opts.biz);
      if (!alive()) return;
      if (data.reminder) current = remNormalize(data.reminder);
      opts.onChange?.(current, 'reminders_get');
    } catch (failure) {
      if (!alive()) return;
      if (accessLost(failure) || sourceGone(failure)) { gone = true; opts.onChange?.(null, 'reminders_get'); }
    }
  }
  // The key is reused only to retry the identical failed-in-transit request.
  async function run(op, extra) {
    if (busy || !remMutable(current)) return;
    const args = {reminder_id: remId(current), revision: current.revision, ...extra}, signature = op + JSON.stringify(args);
    const key = retry?.signature === signature ? retry.key : newRequestId();
    busy = true; error = ''; paint();
    try {
      const data = await remCall(current, op, {...args, idempotency_key: key}, opts.biz);
      retry = null; snoozeOpen = false; confirmCancel = false;
      if (data.reminder) current = remNormalize(data.reminder); else await reload();
      opts.onChange?.(current, op);
    } catch (failure) {
      const transient = !failure.code || failure.code === 'invalid_response' || failure.httpStatus >= 500;
      retry = transient ? {signature, key} : null;
      if (alive()) {
        if (accessLost(failure) || sourceGone(failure)) { gone = true; opts.onChange?.(null, op); }
        else if (remFail(failure)) { await reload(); error = 'This reminder changed since you opened it. It was refreshed; check and try again.'; }
        else error = uiFailure(failure);
      }
    } finally { busy = false; if (alive()) paint(); }
  }
  paint();
  // A legacy projection lacks the occurrence and recurrence; read the full reminder at once.
  if (remId(current)) void reload();
  return {
    reminder: () => current,
    refresh: reload,
    dispose() { disposed = true; box.remove(); },
  };
}

// A detail dialog for one reminder; closing it never changes the reminder.
function remindersOpenDetail(reminder, opts = {}) {
  let view;
  modal({title: 'Reminder', width: 520, stack: true, body: '<div class="rem-dialog"></div>',
    onOpen: el => { view = remindersDetailMount(el.querySelector('.rem-dialog'), reminder, opts); },
    onClose: () => { view?.dispose(); opts.onClose?.(); }});
}

// ---------------------------------------------------------------- editor (create and edit)
async function remRecipients() {
  const data = coreData(await post('/root/operations/assignees_list', {}));
  return Array.isArray(data.assignees) ? data.assignees : [];
}
function remindersEditor(reminder = null, opts = {}) {
  const editing = !!reminder, zone = reminder?.recurrence?.timezone || remZone();
  const parts = reminder?.recurrence ? remCronParts(reminder.recurrence.schedule) : null;
  const repeat = !reminder?.recurrence ? 'once' : parts ? (parts.weekday == null ? 'daily' : 'weekly') : 'custom';
  const link = reminder?.linked_item || opts.linked_item || null;
  const manager = remIsManager();
  const option = (value, label, selected) => `<option value="${esc(value)}" ${selected ? 'selected' : ''}>${esc(label)}</option>`;
  const body = `<form class="rem-form" novalidate>
    <label class="rem-wide">Reminder<textarea class="inp" name="text" maxlength="${REM_TEXT_MAX}" required>${esc(reminder?.text || '')}</textarea></label>
    ${manager ? '<label class="rem-wide">For<select class="inp" name="recipient" disabled><option>Loading…</option></select></label>' : ''}
    <label>Repeat<select class="inp" name="repeat">${option('once', 'Once', repeat === 'once')}${option('daily', 'Daily', repeat === 'daily')}${option('weekly', 'Weekly', repeat === 'weekly')}${option('custom', 'Custom schedule', repeat === 'custom')}</select></label>
    <label data-when="once">When<input class="inp" type="datetime-local" name="at" value="${esc(repeat === 'once' ? remLocalInput(reminder?.at) : '')}"></label>
    <label data-when="weekly">Day<select class="inp" name="weekday">${REM_WEEKDAYS.map((day, index) => option(String(index), day, parts?.weekday === index)).join('')}</select></label>
    <label data-when="daily weekly">Time<input class="inp" type="time" name="time" value="${esc(parts?.time || '09:00')}"></label>
    <label data-when="custom">Schedule (cron)<input class="inp" name="cron" value="${esc(repeat === 'custom' ? reminder.recurrence.schedule : '')}" placeholder="0 9 * * 1-5"></label>
    <label data-when="daily weekly custom">Timezone<input class="inp" name="timezone" value="${esc(zone)}" placeholder="America/New_York"></label>
    <label class="rem-check rem-wide"><input type="checkbox" name="critical" ${reminder?.time_critical ? 'checked' : ''}> Time-critical (may interrupt unrelated focus)</label>
    ${link ? `<p class="muted rem-wide">Linked: ${esc(remLinkText(link))}<label class="rem-inline"><input type="checkbox" name="unlink"> Remove link</label></p>`
      : `<div class="rem-wide rem-link"><label>Link to<select class="inp" name="linktype"><option value="">Nothing</option>${[['task', 'Work'], ['email', 'Email'], ['project', 'Project'], ['table', 'Table']].map(([v, l]) => option(v, l, false)).join('')}</select></label>
      <label>Project<select class="inp" name="linkscope"><option value="">Global</option>${(state.bizs || []).filter(b => b.id || b.business_id).map(b => option(b.id || b.business_id, b.name || b.slug, false)).join('')}</select></label>
      <label class="rem-wide">Item ID<input class="inp" name="linkid"></label></div>`}
    <p class="ferr rem-wide" role="alert" hidden></p></form>`;
  let attempt = null, initialSchedule = null;
  const scheduleOf = form => JSON.stringify(['repeat', 'at', 'weekday', 'time', 'cron', 'timezone'].map(name => form.elements[name]?.value ?? ''));
  modal({title: editing ? 'Edit reminder' : 'New reminder', width: 560, stack: true, body, actions: [
    {label: 'Cancel', onClick: close => close()},
    {label: editing ? 'Save' : 'Create reminder', primary: true, onClick: (close, el) => void save(close, el)},
  ], onOpen: el => {
    const form = el.querySelector('.rem-form');
    const sync = () => form.querySelectorAll('[data-when]').forEach(label => { label.hidden = !label.dataset.when.split(' ').includes(form.elements.repeat.value); });
    form.elements.repeat.onchange = sync; sync();
    initialSchedule = scheduleOf(form);
    form.onsubmit = event => event.preventDefault();
    if (manager) void remRecipients().then(people => {
      if (!form.isConnected) return;
      const select = form.elements.recipient, wanted = reminder?.recipient_id || remSelf();
      const list = people.some(p => p.id === wanted) || !wanted ? people : [{id: wanted, display_name: remWho(reminder || {recipient_id: wanted})}, ...people];
      select.innerHTML = list.map(p => option(p.id, (p.display_name || p.id) + (p.is_owner ? ' (you)' : ''), p.id === wanted || (!wanted && p.is_owner))).join('');
      select.disabled = false;
    }).catch(() => { const note = form.querySelector('p.ferr'); note.textContent = 'Could not load recipients; the reminder will be for you.'; note.hidden = false; });
  }});

  async function save(close, el) {
    const form = el.querySelector('.rem-form'), message = form.querySelector('p.ferr'), button = el.querySelector('.mfoot .primary');
    const fail = text => { message.textContent = text; message.hidden = false; };
    const f = form.elements, text = f.text.value.trim(), kind = f.repeat.value;
    message.hidden = true;
    if (!text) return fail('Enter the reminder text.');
    if (text.length > REM_TEXT_MAX) return fail(`Keep the text within ${REM_TEXT_MAX} characters.`);
    // An edit that leaves the schedule controls alone never sends at/recurrence, so the stored time and calendar are untouched.
    const reschedule = !editing || scheduleOf(form) !== initialSchedule;
    let at = null, recurrence = null;
    if (reschedule && kind === 'once') {
      at = remOffsetISO(f.at.value);
      if (!at) return fail('Choose when to be reminded.');
      if (new Date(at).getTime() <= Date.now()) return fail('Choose a future date and time.');
    } else if (reschedule) {
      const timezone = f.timezone.value.trim();
      if (!timezone) return fail('Enter a timezone, for example ' + remZone() + '.');
      let schedule;
      if (kind === 'custom') schedule = f.cron.value.trim().replace(/\s+/g, ' ');
      else {
        const [hh, mm] = (f.time.value || '').split(':').map(Number);
        if (!Number.isInteger(hh) || !Number.isInteger(mm)) return fail('Choose a time.');
        schedule = `${mm} ${hh} * * ${kind === 'weekly' ? f.weekday.value : '*'}`;
      }
      if (schedule.split(' ').length !== 5) return fail('Enter a five-field schedule such as 0 9 * * 1-5.');
      recurrence = {schedule, timezone};
    }
    const critical = f.critical.checked;
    let linked;
    if (link) linked = f.unlink?.checked ? null : undefined;
    else if (f.linktype.value) {
      const id = f.linkid.value.trim();
      if (!id) return fail('Enter the ID of the item to link, or choose Nothing.');
      linked = {type: f.linktype.value, id, business_id: f.linkscope.value || null};
    }
    const recipient = f.recipient && !f.recipient.disabled ? f.recipient.value : null;
    let args;
    if (editing) {
      args = {reminder_id: remId(reminder), revision: reminder.revision};
      if (text !== reminder.text) args.text = text;
      if (critical !== !!reminder.time_critical) args.time_critical = critical;
      if (recurrence) args.recurrence = recurrence;
      else if (at) { args.at = at; if (reminder.recurrence) args.recurrence = null; }
      if (linked !== undefined) args.linked_item = linked;
      if (recipient && recipient !== reminder.recipient_id) args.recipient_id = recipient;
      if (Object.keys(args).length === 2) { close(); return; }
    } else {
      args = {text, ...(recurrence ? {recurrence} : {at}), time_critical: critical};
      if (recipient && recipient !== remSelf()) args.recipient_id = recipient;
      if (linked) args.linked_item = linked;
    }
    const signature = JSON.stringify(args), key = attempt?.signature === signature ? attempt.key : newRequestId();
    button.disabled = true;
    try {
      const data = await remCall(reminder, editing ? 'reminders_update' : 'reminders_create', {...args, idempotency_key: key}, opts.biz);
      attempt = null; close();
      const saved = data.reminder ? remNormalize(data.reminder) : null;
      if (!editing && saved && saved.recipient_id && remSelf() && saved.recipient_id !== remSelf()) toast('Reminder set for ' + remWho(saved) + '. Only your own reminders are listed here.');
      opts.onSaved?.(saved);
    } catch (failure) {
      const transient = !failure.code || failure.code === 'invalid_response' || failure.httpStatus >= 500;
      attempt = transient ? {signature, key} : null;
      fail(remFail(failure) ? 'This reminder changed since you opened it. Close this form and reopen it.' : failure.message && failure.code && !transient ? failure.message : uiFailure(failure));
    } finally { button.disabled = false; }
  }
}

// ---------------------------------------------------------------- owner reads (self only)
// The owner's own reminders in every readable scope; never a recipient chosen from the selected agent.
async function remindersOwnerList(states, cursor = null, limit = 100) {
  await loadSession();
  const recipient = remSelf();
  return coreData(await post('/root/operations/reminders_list', {states, all_scopes: true, limit, ...(recipient ? {recipient_id: recipient} : {}), ...(cursor ? {cursor} : {})}));
}
async function remindersOwnerDue() {
  const rows = [], seen = new Set();
  let cursor = null;
  do {
    const data = await remindersOwnerList(['due'], cursor);
    if (!Array.isArray(data.reminders)) throw new Error('Invalid reminder list');
    rows.push(...data.reminders.map(remNormalize).filter(r => r.state === 'due'));
    cursor = data.next_cursor || null;
    if (cursor && seen.has(cursor)) throw new Error('Repeated reminder page');
    seen.add(cursor);
  } while (cursor);
  return [...new Map(rows.map(r => [r.reminder_id, r])).values()].sort((a, b) => String(a.due_at || a.at).localeCompare(String(b.due_at || b.at)));
}

// The Reminders button: the owner's upcoming reminders with creation and per-reminder controls.
function remindersOpenUpcoming(opts = {}) {
  let rows = [], cursor = null, loading = false, failed = '', host, generation = 0;
  const changed = () => opts.onChange?.();
  async function load(append = false) {
    if (loading || !host?.isConnected) return;
    loading = true; const own = ++generation;
    if (!append) { cursor = null; rows = []; }
    failed = ''; paint();
    try {
      const data = await remindersOwnerList(['scheduled'], append ? cursor : null, 50);
      if (own !== generation || !host.isConnected) return;
      const next = (data.reminders || []).map(remNormalize);
      rows = append ? [...new Map([...rows, ...next].map(r => [r.reminder_id, r])).values()] : next;
      cursor = data.next_cursor || null;
    } catch (failure) { if (own === generation) failed = uiFailure(failure, {load: true}); }
    finally { if (own === generation) { loading = false; if (host.isConnected) paint(); } }
  }
  function paint() {
    host.replaceChildren();
    const bar = document.createElement('div'); bar.className = 'rem-toolbar';
    const add = document.createElement('button'); add.type = 'button'; add.className = 'primary'; add.textContent = 'New reminder';
    add.onclick = () => remindersEditor(null, {onSaved: () => { void load(); changed(); }});
    bar.append(add); host.append(bar);
    if (failed) {
      const message = document.createElement('p'); message.className = 'ferr'; message.setAttribute('role', 'alert'); message.textContent = failed;
      const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'quiet'; retry.textContent = 'Retry'; retry.onclick = () => void load();
      host.append(message, retry);
    }
    if (!rows.length) { const empty = document.createElement('p'); empty.className = 'muted'; empty.textContent = loading ? 'Loading reminders…' : failed ? '' : 'No upcoming reminders.'; host.append(empty); }
    const list = document.createElement('div'); list.className = 'rem-list';
    for (const r of rows) {
      const row = document.createElement('button'); row.type = 'button'; row.className = 'rem-row';
      row.setAttribute('aria-label', `${r.text}, ${remFormatTime(r.at, r.recurrence?.timezone)}`);
      const title = document.createElement('span'); title.className = 'rem-row-text'; title.textContent = r.text;
      const meta = document.createElement('span'); meta.className = 'rem-row-meta muted';
      meta.textContent = [remFormatTime(r.next_at || r.at, r.recurrence?.timezone), r.recurrence ? remRecurrenceText(r.recurrence) : '', r.time_critical ? 'Time-critical' : ''].filter(Boolean).join(' · ');
      row.append(title, meta);
      row.onclick = () => remindersOpenDetail(r, {onChange: () => { void load(); changed(); }});
      list.append(row);
    }
    host.append(list);
    if (cursor) {
      const more = document.createElement('button'); more.type = 'button'; more.className = 'quiet'; more.textContent = 'More'; more.disabled = loading; more.onclick = () => void load(true);
      host.append(more);
    }
  }
  modal({title: 'Reminders', width: 560, stack: true, body: '<div class="rem-upcoming"></div>',
    onOpen: el => { host = el.querySelector('.rem-upcoming'); paint(); void load(); },
    onClose: () => { generation++; host = null; changed(); }});
}
