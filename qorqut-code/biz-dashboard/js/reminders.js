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
const remSameTime = (a, b) => !!a && !!b && Number.isFinite(Date.parse(a)) && Date.parse(a) === Date.parse(b);
const remLinkText = link => link ? `${({task: 'Work', email: 'Email', project: 'Project', table: 'Table', signoff: 'Signoff', action: 'Action'}[link.type] || link.type)} ${link.id}` : '';
const remWho = reminder => reminder.recipient_name || reminder.recipient_display_name || (reminder.recipient_id && reminder.recipient_id === remSelf() ? 'You' : reminder.recipient_id) || '—';
const remFail = error => /conflict|stale|revision|precondition|state_changed|occurrence/i.test(String(error?.code || ''));
// Uncertain writes remain in memory and are exposed only after a fresh owner-session read.
const remAttempts = new Map();
const remAttemptChanged = () => document.dispatchEvent(new Event('reminder-attempts-changed'));
async function remVerifiedOwner() {
  const session = await loadSession(true);
  if (!session || session.principal_kind !== 'human' || session.is_owner !== true || !session.principal_id)
    throw new Error('Could not verify the owner session. Retry.');
  return session.principal_id;
}
const remAttemptGroup = (owner, reminder, biz, kind) => JSON.stringify([owner, remPath(reminder, 'reminders_get', biz), kind, remId(reminder), reminder?.occurrence_id ?? null]);
async function remAttempt(reminder, biz, kind, op, args) {
  const owner = await remVerifiedOwner(), group = remAttemptGroup(owner, reminder, biz, kind);
  const key = newRequestId();
  return {identity:key, group, owner, occurrence:reminder?.occurrence_id ?? null, path:remPath(reminder, op, biz), op,
    label:reminder?.text || args.text || 'Reminder', scheduled:args.at || reminder?.due_at || reminder?.at || reminder?.recurrence?.schedule || '',
    args:{...args, idempotency_key:key}};
}
const remUncertain = error => !error.code || ['invalid_response','request_timeout'].includes(error.code) || [408,429].includes(error.httpStatus) || error.httpStatus >= 500;
async function remSubmitAttempt(attempt) {
  const owner = await remVerifiedOwner();
  if (owner !== attempt.owner) throw new Error('The reminder belongs to a different owner session.');
  try {
    const data = coreData(await post(attempt.path, attempt.args));
    if (attempt.op === 'reminders_dismiss' && (!data?.reminder || remId(data.reminder) !== attempt.args.reminder_id || data.reminder.revision == null ||
        !['scheduled','due','dismissed','cancelled'].includes(data.reminder.state) ||
        (data.reminder.state === 'due' && data.reminder.occurrence_id === attempt.occurrence)))
      throw Object.assign(new Error('The reminder dismissal could not be confirmed.'), {code:'invalid_response'});
    remAttempts.delete(attempt.identity); remAttemptChanged();
    return data;
  } catch (error) {
    if (remUncertain(error)) remAttempts.set(attempt.identity, attempt);
    else remAttempts.delete(attempt.identity);
    remAttemptChanged();
    throw error;
  }
}
const remPending = (owner, reminder, biz, kind) => [...remAttempts.values()].filter(a => a.group === remAttemptGroup(owner, reminder, biz, kind));
const remMatching = (owner, reminder, biz, kind, op, args) => remPending(owner, reminder, biz, kind).find(a => a.op === op && JSON.stringify(Object.fromEntries(Object.entries(a.args).filter(([key]) => key !== 'idempotency_key'))) === JSON.stringify(args));
const remAttemptLabel = a => [a.label, a.scheduled, a.op.replace('reminders_', ''), a.occurrence ? 'occurrence ' + a.occurrence : ''].filter(Boolean).join(' · ');
const remDismissals = new Map();
function remindersDismissDue(reminder) {
  const key=JSON.stringify([remId(reminder),reminder?.occurrence_id]);
  if (remDismissals.has(key)) return remDismissals.get(key);
  const work=(async()=>{
    if (!remId(reminder) || reminder.revision == null || reminder.state !== 'due' || !reminder.occurrence_id)
      throw new Error('This reminder occurrence is no longer available.');
    if (reminder.business_id != null && !(state.bizs || []).some(b=>(b.id===reminder.business_id || b.business_id===reminder.business_id) && b.slug))
      throw new Error('The reminder project is no longer accessible.');
    const owner=await remVerifiedOwner();
    if (remSelf() !== owner || reminder.recipient_id !== owner) throw new Error('Could not verify the reminder owner.');
    const args={reminder_id:remId(reminder),revision:reminder.revision,occurrence_id:reminder.occurrence_id};
    if (remMatching(owner,reminder,null,'detail','reminders_dismiss',args) || remPending(owner,reminder,null,'detail').length || remPending(owner,reminder,null,'editor').length)
      throw new Error('This reminder has an unconfirmed change. Use Unconfirmed reminder changes to retry.');
    const attempt=await remAttempt(reminder,null,'detail','reminders_dismiss',args);
    if (attempt.owner !== owner || remSelf() !== owner) throw new Error('The owner session changed. Retry.');
    const result=await remSubmitAttempt(attempt);
    if (typeof attentionQueue !== 'undefined') void attentionQueue.refreshSignoffs();
    return result;
  })();
  remDismissals.set(key,work);
  work.finally(()=>remDismissals.delete(key)).catch(()=>{});
  return work;
}


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
  let current = remNormalize(initial), busy = false, verifiedOwner = null, duplicate = null, error = '', gone = false, snoozeOpen = false, confirmCancel = false, disposed = false;
  const box = document.createElement('div');
  box.className = 'rem-detail';
  host.append(box);
  const alive = () => !disposed && box.isConnected;
  const node = (tag, className, text) => { const n = document.createElement(tag); if (className) n.className = className; if (text != null) n.textContent = text; return n; };

  function paint() {
    const focusKey = box.contains(document.activeElement) ? document.activeElement.dataset.remFocus : null;
    box.replaceChildren();
    if (focusKey) queueMicrotask(() => {
      if (alive() && document.activeElement === document.body) box.querySelector(`[data-rem-focus="${focusKey}"]`)?.focus({preventScroll: true});
    });
    const r = current, mutable = !gone && remMutable(r);
    box.append(node('h2', 'rw-reminder-text rem-text', r.text || 'Reminder'));
    const fields = [['State', REM_STATES[r.state] || r.state || '—']];
    if (r.state === 'due' && r.due_at) fields.push(['Due', remFormatTime(r.due_at, r.recurrence?.timezone)]);
    const next = r.next_at || r.at;
    if (next && !(r.state === 'due' && remSameTime(next, r.due_at)) && (r.state !== 'dismissed' && r.state !== 'cancelled' || !r.next_at))
      fields.push([r.state === 'due' && r.recurrence && r.next_at ? 'Next' : 'Scheduled for', remFormatTime(next, r.recurrence?.timezone)]);
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
    for (const attempt of verifiedOwner && remSelf()===verifiedOwner ? remPending(verifiedOwner, initial, opts.biz, 'detail') : []) {
      const pending = node('div', 'rem-pending');
      pending.append(node('p', 'rem-note', `Unconfirmed: ${remAttemptLabel(attempt)}`));
      const check = node('button', 'quiet', 'Retry the same request'); check.type='button'; check.disabled=busy;
      check.onclick=()=>void run(attempt.op, {}, attempt); pending.append(check); box.append(pending);
    }
    if (duplicate && verifiedOwner === duplicate.owner && remSelf() === verifiedOwner) {
      const choice=node('div','rem-pending');
      choice.append(node('p','rem-note','This request is unconfirmed. Retry the same request or cancel.'));
      const retry=node('button','quiet','Retry same request');retry.type='button';retry.disabled=busy;
      retry.onclick=()=>{const attempt=duplicate;duplicate=null;void run(attempt.op,{},attempt);};
      const cancel=node('button','quiet','Cancel');cancel.type='button';cancel.disabled=busy;
      cancel.onclick=()=>{duplicate=null;paint();};choice.append(retry,cancel);box.append(choice);
    }
    if (gone) { box.append(node('p', 'muted', 'This reminder is no longer available.')); return; }
    if (!mutable) return;
    const actions = node('div', 'rem-actions');
    const add = (label, handler, cls = 'quiet', key = label) => { const b = node('button', cls, label); b.dataset.remFocus = key; b.type = 'button'; b.disabled = busy; b.onclick = handler; actions.append(b); return b; };
    if (r.state === 'due' && r.occurrence_id) add('Dismiss', () => run('reminders_dismiss', {occurrence_id: r.occurrence_id}), 'primary');
    add('Snooze…', () => { snoozeOpen = !snoozeOpen; confirmCancel = false; paint(); });
    add('Edit', () => (opts.onEdit || remindersEditor)(current, {biz: opts.biz, onSaved: next => { current = remNormalize(next); error = ''; paint(); opts.onChange?.(current, 'reminders_update'); }}));
    add(confirmCancel ? 'Confirm cancel' : 'Cancel reminder', () => {
      if (!confirmCancel) { confirmCancel = true; snoozeOpen = false; paint(); return; }
      run('reminders_cancel', {});
    }, confirmCancel ? 'danger' : 'quiet', 'cancel');
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
  // One authorized read repaints the mounted view with the refreshed reminder. Losing access or the source removes the actions
  // at once; any other failure is transient, so the last content stays with the failure shown. Resolves true when refreshed.
  async function reload() {
    let data;
    try {
      data = await remCall(current, 'reminders_get', {reminder_id: remId(current)}, opts.biz);
    } catch (failure) {
      if (!alive()) return false;
      if (accessLost(failure) || sourceGone(failure)) { gone = true; paint(); opts.onChange?.(null, 'reminders_get'); }
      else { error = uiFailure(failure, {load: true}); paint(); }
      return false;
    }
    if (!alive()) return false;
    const refreshed = !!data.reminder;
    if (refreshed) { current = remNormalize(data.reminder); gone = false; error = ''; }
    paint();
    opts.onChange?.(current, 'reminders_get');
    return refreshed;
  }
  // The key is reused only to retry the identical failed-in-transit request.
  async function run(op, extra, saved = null) {
    if (busy || (!saved && !remMutable(current))) return;
    const args = {reminder_id: remId(current), revision: current.revision, ...extra};
    busy = true; error = ''; paint();
    try {
      let attempt=saved;
      if (!attempt) {
        const owner=await remVerifiedOwner();
        duplicate=remMatching(owner, current, opts.biz, 'detail', op, args) || null;
        if (duplicate) return;
        attempt=await remAttempt(current, opts.biz, 'detail', op, args);
        if (attempt.owner !== owner) throw new Error('The owner session changed. Retry.');
      }
      const data = await remSubmitAttempt(attempt);
      snoozeOpen = false; confirmCancel = false;
      if (saved || !data.reminder) await reload(); else current = remNormalize(data.reminder);
      opts.onChange?.(current, op);
    } catch (failure) {
      if (alive()) {
        if (accessLost(failure) || sourceGone(failure)) { gone = true; opts.onChange?.(null, op); }
        else if (remFail(failure)) { if (await reload()) error = 'This reminder changed since you opened it. It was refreshed; check and try again.'; }
        else error = failure.message || uiFailure(failure);
      }
    } finally { busy = false; await refreshPending(); if (alive()) paint(); }
  }
  async function refreshPending() {
    verifiedOwner=null; if (alive()) paint();
    try { verifiedOwner=await remVerifiedOwner(); }
    catch (failure) { error=failure.message; }
    if (alive()) paint();
  }
  const onFocus = () => { verifiedOwner=null; if (alive()) paint(); void refreshPending(); };
  window.addEventListener('focus',onFocus);
  paint(); void refreshPending();
  // A legacy projection lacks the occurrence and recurrence; read the full reminder at once.
  if (remId(current)) void reload();
  return {
    reminder: () => current,
    isBusy: () => busy,
    refresh: reload,
    dispose() { disposed = true; window.removeEventListener('focus',onFocus); box.remove(); },
  };
}

// A detail dialog for one reminder; closing it never changes the reminder.
function remindersOpenDetail(reminder, opts = {}) {
  return window.focusExpansion.open({title:'Reminder', mount(host, session) {
    let view, child;
    const show = current => {
      session.setTitle('Reminder');
      host.replaceChildren();
      view = remindersDetailMount(host, current, {...opts, onEdit: (item, editOpts) => {
        view.dispose();
        remindersEditor(item, {...editOpts, host, session, onCancel: () => show(item), onSaved: saved => {
          if (saved) editOpts.onSaved?.(saved); show(saved || item);
        }});
      }});
      session.setBeforeLeave(async () => !view.isBusy());
    };
    show(reminder);
    return () => { view?.dispose(); child?.dispose?.(); opts.onClose?.(); };
  }});
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
      : `<details class="rem-wide rem-more"><summary>Link to an item</summary><div class="rem-link"><label>Link to<select class="inp" name="linktype"><option value="">Nothing</option>${[['task', 'Work'], ['email', 'Email'], ['project', 'Project'], ['table', 'Table']].map(([v, l]) => option(v, l, false)).join('')}</select></label>
      <label data-link-field hidden>Project<select class="inp" name="linkscope"><option value="">Global</option>${(state.bizs || []).filter(b => b.id || b.business_id).map(b => option(b.id || b.business_id, b.name || b.slug, false)).join('')}</select></label>
      <label class="rem-wide" data-link-field hidden>Item ID<input class="inp" name="linkid"></label></div></details>`}
    <p class="ferr rem-wide" role="alert" hidden></p></form>`;
  let initialSchedule = null;
  const scheduleOf = form => JSON.stringify(['repeat', 'at', 'weekday', 'time', 'cron', 'timezone'].map(name => form.elements[name]?.value ?? ''));
  if (!opts.host || !opts.session) {
    return window.focusExpansion.open({title: editing ? 'Edit reminder' : 'New reminder', mount(host, session) {
      remindersEditor(reminder, {...opts, host, session, onCancel: () => session.close(), onSaved: saved => { opts.onSaved?.(saved); void session.close(); }});
    }});
  }
  const el = opts.host, session = opts.session;
  session.setTitle(editing ? 'Edit reminder' : 'New reminder');
  el.innerHTML = body + `<div class="rem-actions"><button type="button" class="quiet rem-editor-cancel">Cancel</button><button type="button" class="primary rem-editor-save">${editing ? 'Save' : 'Create reminder'}</button></div><div class="rem-editor-pending"></div><div class="rem-editor-confirm" hidden><p>Discard your changes?</p><button type="button" class="quiet" data-stay>Keep editing</button><button type="button" class="danger" data-discard>Discard</button></div>`;
  const form = el.querySelector('.rem-form'), confirmBox = el.querySelector('.rem-editor-confirm');
  let busy = false, initial = '', pendingConfirm = null, pendingPromise = null, verifiedOwner = null, duplicate = null;
  const snapshot = () => JSON.stringify([...form.elements].map(f => [f.name, f.type === 'checkbox' ? f.checked : f.value]));
  const guard = async () => {
    if (busy) return false;
    if (pendingPromise) return pendingPromise;
    if (snapshot() === initial) return true;
    confirmBox.hidden = false;
    pendingPromise = new Promise(resolve => { pendingConfirm = resolve; });
    return pendingPromise;
  };
  const answer = value => { confirmBox.hidden = true; const resolve = pendingConfirm; pendingConfirm = null; pendingPromise = null; resolve?.(value); };
  confirmBox.querySelector('[data-stay]').onclick = () => answer(false);
  confirmBox.querySelector('[data-discard]').onclick = () => { initial = snapshot(); answer(true); };
  session.onDispose(() => answer(false));
  session.setBeforeLeave(guard);
  el.querySelector('.rem-editor-cancel').onclick = async () => { if (await guard()) opts.onCancel?.(); };
  el.querySelector('.rem-editor-save').onclick = () => void save(() => { initial = snapshot(); opts.onCancel?.(); }, el);
  const pendingBox = el.querySelector('.rem-editor-pending');
  function paintPending() {
    pendingBox.replaceChildren();
    if (duplicate && duplicate.attempt.owner === verifiedOwner && remSelf() === verifiedOwner) {
      const text=document.createElement('p');text.textContent='This request is unconfirmed. Retry the same request or cancel.';pendingBox.append(text);
      const retry=document.createElement('button');retry.type='button';retry.className='quiet';retry.textContent='Retry same request';retry.disabled=busy;
      retry.onclick=()=>{const attempt=duplicate.attempt;duplicate=null;form.inert=false;void submitAttempt(attempt,true);};pendingBox.append(retry);
      const cancel=document.createElement('button');cancel.type='button';cancel.className='quiet';cancel.textContent='Cancel';cancel.disabled=busy;
      cancel.onclick=()=>{duplicate=null;form.inert=false;paintPending();};pendingBox.append(cancel);
      if (!editing) { const separate=document.createElement('button');separate.type='button';separate.className='quiet';separate.textContent='Create separate';separate.disabled=busy;
        separate.onclick=()=>void createSeparate();pendingBox.append(separate); }
    }
    for (const attempt of verifiedOwner && remSelf()===verifiedOwner ? remPending(verifiedOwner, reminder, opts.biz, 'editor') : []) {
      const note = document.createElement('p'); note.textContent='Unconfirmed: ' + remAttemptLabel(attempt);
      const retry = document.createElement('button'); retry.type='button'; retry.className='quiet'; retry.textContent='Retry the same request'; retry.disabled=busy;
      retry.onclick=()=>void submitAttempt(attempt, true); pendingBox.append(note,retry);
    }
  }
  async function createSeparate() {
    if (busy || !duplicate) return;
    const choice=duplicate; busy=true; form.inert=true; paintPending();
    try {
      const attempt=await remAttempt(reminder, opts.biz, 'editor', choice.op, choice.args);
      if (attempt.owner !== choice.attempt.owner) throw new Error('The owner session changed. Retry.');
      duplicate=null; busy=false; await submitAttempt(attempt);
    } catch (failure) { const message=form.querySelector('p.ferr');message.textContent=failure.message;message.hidden=false; }
    finally { busy=false; form.inert=!!duplicate; if (form.isConnected) paintPending(); }
  }
  async function submitAttempt(attempt, isRetry = false) {
    if (busy) return;
    const button=el.querySelector('.rem-editor-save'), message=form.querySelector('p.ferr');
    button.disabled=true; busy=true; form.inert=true; paintPending();
    try {
      const data=await remSubmitAttempt(attempt);
      const saved=data.reminder ? remNormalize(data.reminder) : null;
      if (!editing && saved && saved.recipient_id && remSelf() && saved.recipient_id !== remSelf()) toast('Reminder set for ' + remWho(saved) + '. Only your own reminders are listed here.');
      if (isRetry) { message.textContent='The earlier request was confirmed. Your current draft is unchanged.'; message.hidden=false; }
      else { initial=snapshot(); busy=false; if (opts.onSaved) opts.onSaved(saved); else opts.onCancel?.(); }
    } catch (failure) {
      const transient = remUncertain(failure);
      message.textContent=remFail(failure) ? 'This reminder changed since you opened it. Close this form and reopen it.' : failure.message && failure.code && !transient ? failure.message : uiFailure(failure);
      message.hidden=false;
    } finally { busy=false; form.inert=false; button.disabled=false; await refreshPending(); }
  }
  async function refreshPending() {
    verifiedOwner=null; if (form.isConnected) paintPending();
    try { verifiedOwner=await remVerifiedOwner(); }
    catch (failure) { const message=form.querySelector('p.ferr'); message.textContent=failure.message; message.hidden=false; }
    if (form.isConnected) paintPending();
  }
  const onFocus = () => { verifiedOwner=null; duplicate=null; form.inert=busy; if (form.isConnected) { paintPending(); void refreshPending(); } };
  window.addEventListener('focus',onFocus); session.onDispose(()=>window.removeEventListener('focus',onFocus));
  void refreshPending();
  const sync = () => form.querySelectorAll('[data-when]').forEach(label => { label.hidden = !label.dataset.when.split(' ').includes(form.elements.repeat.value); });
  form.elements.repeat.onchange = sync; sync();
  if (form.elements.linktype) {
    const syncLink = () => form.querySelectorAll('[data-link-field]').forEach(label => { label.hidden = !form.elements.linktype.value; });
    form.elements.linktype.onchange = syncLink; syncLink();
  }
  initialSchedule = scheduleOf(form);
  form.onsubmit = event => event.preventDefault();
  initial = snapshot();
  if (manager) void remRecipients().then(people => {
    if (!form.isConnected || !session.isCurrent()) return;
    const select = form.elements.recipient, wanted = reminder?.recipient_id || remSelf();
    const list = people.some(p => p.id === wanted) || !wanted ? people : [{id: wanted, display_name: remWho(reminder || {recipient_id: wanted})}, ...people];
    select.innerHTML = list.map(p => option(p.id, (p.display_name || p.id) + (p.is_owner ? ' (you)' : ''), p.id === wanted || (!wanted && p.is_owner))).join('');
    select.disabled = false;
    const baseline = JSON.parse(initial);
    const index = [...form.elements].indexOf(select);
    if (index >= 0) baseline[index] = [select.name, select.value];
    initial = JSON.stringify(baseline);
  }).catch(() => { if (!form.isConnected || !session.isCurrent()) return; const note = form.querySelector('p.ferr'); note.textContent = 'Could not load recipients; the reminder will be for you.'; note.hidden = false; });

  async function save(close, el) {
    const form = el.querySelector('.rem-form'), message = form.querySelector('p.ferr'), button = el.querySelector('.rem-editor-save');
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
    const op = editing ? 'reminders_update' : 'reminders_create';
    if (busy) return;
    busy=true; button.disabled=true; form.inert=true;
    try {
      const owner=await remVerifiedOwner();
      const previous=remMatching(owner, reminder, opts.biz, 'editor', op, args);
      if (previous) { duplicate={attempt:previous,op,args}; verifiedOwner=owner; return; }
      const next=await remAttempt(reminder, opts.biz, 'editor', op, args);
      if(next.owner!==owner)throw new Error('The owner session changed. Retry.');
      busy=false; await submitAttempt(next);
    }
    catch (failure) { fail(failure.message || 'Could not verify the owner session. Retry.'); }
    finally { busy=false; button.disabled=false; form.inert=!!duplicate; if (form.isConnected) paintPending(); }
  }
}

// Recovery stays available even if a successful remote dismissal removed the due chip.
function remindersOpenUnconfirmed() {
  return window.focusExpansion.open({title:'Unconfirmed reminder changes', mount(host, session) {
    let disposed=false, busy=false, owner=null, error='', confirming=null;
    const node=(tag,text)=>{const el=document.createElement(tag); if(text != null)el.textContent=text;return el;};
    async function refresh(preserveError=false) {
      owner=null; paint();
      try { owner=await remVerifiedOwner(); if (!preserveError) error=''; }
      catch (failure) { error=failure.message; }
      if (!disposed && session.isCurrent()) paint();
    }
    function paint() {
      if (disposed) return;
      host.replaceChildren();
      if (error) { const message=node('p',error);message.setAttribute('role','alert');host.append(message);
        const retry=node('button','Retry session check');retry.type='button';retry.onclick=()=>void refresh();host.append(retry); }
      if (!owner) { host.append(node('p','Checking the owner session…'));return; }
      const attempts=[...remAttempts.values()].filter(a=>a.owner===owner);
      if (!attempts.length) host.append(node('p','No unconfirmed reminder changes.'));
      for (const attempt of attempts) {
        const row=node('div');row.className='rem-pending';row.append(node('p',remAttemptLabel(attempt)));
        const retry=node('button','Retry same request');retry.type='button';retry.disabled=busy;
        retry.onclick=async()=>{ if(busy)return;busy=true;error='';paint();
          try { await remSubmitAttempt(attempt); if (typeof attentionQueue !== 'undefined') void attentionQueue.refreshSignoffs(); }
          catch (failure) { error=failure.message || 'Could not confirm the reminder change. Retry.'; }
          finally { busy=false;await refresh(true); }
        };
        const forget=node('button',confirming===attempt.identity?'Confirm forget':'Forget');forget.type='button';forget.disabled=busy;
        forget.onclick=async()=>{
          if(confirming!==attempt.identity){confirming=attempt.identity;paint();return;}
          busy=true;paint();
          try { const current=await remVerifiedOwner(); if(current!==attempt.owner)throw new Error('The owner session changed. Retry.');
            remAttempts.delete(attempt.identity);confirming=null;remAttemptChanged(); }
          catch(failure){error=failure.message;} finally{busy=false;await refresh(true);}
        };
        row.append(retry,forget);
        if(confirming===attempt.identity)row.append(node('p','Forget removes only this local reminder of the request; it does not undo any remote change.'));
        host.append(row);
      }
    }
    const onFocus=()=>{owner=null;paint();void refresh();}; window.addEventListener('focus',onFocus);
    session.setBeforeLeave(async()=>!busy);void refresh();
    return ()=>{disposed=true;window.removeEventListener('focus',onFocus);};
  }});
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

// Show each due occurrence once per page session, including reminders already due when the app opens.
// The reminder itself remains due until the owner explicitly handles it in the detail view.
const reminderNotifications = (() => {
  const seen = new Set();
  let inflight = null, timer;
  async function poll() {
    if (inflight) return inflight;
    inflight = (async () => {
      try {
        for (const reminder of await remindersOwnerDue()) {
          const key = `${remId(reminder)}:${reminder.occurrence_id || reminder.due_at || reminder.at || ''}`;
          if (seen.has(key)) continue;
          seen.add(key);
          notify({text: reminder.text || 'Reminder due', reminder: true, ms: 10000,
            onDismiss: () => remindersDismissDue(reminder),
            onClick: () => remindersOpenDetail(reminder, {onChange: () => {
              if (typeof attentionQueue !== 'undefined') void attentionQueue.refreshSignoffs();
            }})});
        }
      } catch (_) { /* Retry on the next poll without losing seen occurrences. */ }
      finally { inflight = null; clearTimeout(timer); timer = setTimeout(poll, 30000); }
    })();
    return inflight;
  }
  return {start: poll};
})();
if (typeof embeddedAgentPane === 'undefined' || !embeddedAgentPane) {
  reminderNotifications.start();
  document.addEventListener('visibilitychange', () => { if (!document.hidden) void reminderNotifications.start(); });
}

// The Reminders button: the owner's upcoming reminders with creation and per-reminder controls.
function remindersOpenUpcoming(opts = {}) {
  let rows = [], cursor = null, loading = false, failed = '', host, session, detail, generation = 0;
  const changed = () => opts.onChange?.();
  async function load(append = false) {
    if (loading || !host?.isConnected) return;
    loading = true; const own = ++generation;
    if (!append) { cursor = null; rows = []; }
    failed = ''; paint();
    try {
      const data = await remindersOwnerList(['scheduled'], append ? cursor : null, 50);
      if (own !== generation || !host?.isConnected) return;
      const next = (data.reminders || []).map(remNormalize);
      rows = append ? [...new Map([...rows, ...next].map(r => [r.reminder_id, r])).values()] : next;
      cursor = data.next_cursor || null;
    } catch (failure) { if (own === generation) failed = uiFailure(failure, {load: true}); }
    finally { if (own === generation) { loading = false; if (host?.isConnected) paint(); } }
  }
  let showingList = true;
  function paint() {
    if (!showingList) return;
    host.replaceChildren();
    const bar = document.createElement('div'); bar.className = 'rem-toolbar';
    const add = document.createElement('button'); add.type = 'button'; add.className = 'primary'; add.textContent = 'New reminder';
    add.onclick = () => { showingList = false; remindersEditor(null, {host, session, onCancel: showList, onSaved: () => { showList(); void load(); changed(); }}); };
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
      row.onclick = () => showDetail(r);
      list.append(row);
    }
    host.append(list);
    if (cursor) {
      const more = document.createElement('button'); more.type = 'button'; more.className = 'quiet'; more.textContent = 'More'; more.disabled = loading; more.onclick = () => void load(true);
      host.append(more);
    }
  }
  function showList() {
    detail?.dispose(); detail = null; showingList = true;
    session.setTitle('Reminders'); session.setBeforeLeave(async () => true);
    paint();
  }
  function showDetail(reminder) {
    showingList = false; session.setTitle('Reminder'); host.replaceChildren();
    const back = document.createElement('button'); back.type = 'button'; back.className = 'quiet'; back.textContent = 'Back to reminders';
    back.onclick = () => { if (!detail.isBusy()) { showList(); void load(); } };
    host.append(back);
    detail = remindersDetailMount(host, reminder, {onChange: changed, onEdit: (item, editOpts) => {
      detail.dispose(); detail = null;
      remindersEditor(item, {...editOpts, host, session, onCancel: () => showDetail(item), onSaved: saved => { changed(); showDetail(saved || item); }});
    }});
    session.setBeforeLeave(async () => !detail.isBusy());
  }
  return window.focusExpansion.open({title:'Reminders',mount(element, currentSession) {
    host=element; session=currentSession; paint(); void load();
    return () => { generation++; detail?.dispose(); host=null; changed(); };
  }});
}
