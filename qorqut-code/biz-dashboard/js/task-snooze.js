// Shared by business and Global tasks. One shared `snoozed_until` timestamp (UTC) hides a task until then;
// inputs and labels use San Juan time. Snoozing never changes the state, due date or archive flag.
function taskSnoozed(task, now = Date.now()) {
  return Boolean(task.snoozed_until && Date.parse(task.snoozed_until) > now);
}
function taskSnoozeLabel(task) {
  return taskSnoozed(task) ? 'Snoozed until ' + sjDateTime(task.snoozed_until) : '';
}
function taskSnoozeLocal(until) {
  if (!until) return '';
  const parts = sjParts(until);
  return parts ? `${parts.date}T${parts.time}` : '';
}
function taskSnoozePicker(task = {}) {
  const mode = taskSnoozed(task) ? 'until' : 'none';
  return `<div class="task-snooze-picker"><label>Snooze<select data-snooze-mode data-initial="${mode}" aria-label="Snooze">
    ${[['none','Not snoozed'],['until','Until a date/time']].map(([value,label])=>`<option value="${value}" ${value===mode?'selected':''}>${label}</option>`).join('')}</select></label>
    <div class="task-snooze-shortcuts">${[['1','Tomorrow'],['7','Next week'],['pick','Pick date']].map(([value,label])=>`<button type="button" class="quiet" data-snooze-shortcut="${value}">${label}</button>`).join('')}</div>
    <label data-snooze-date ${mode==='until'?'':'hidden'}>Until (San Juan time)<input data-snooze-until aria-label="Snooze until" type="datetime-local" value="${esc(taskSnoozeLocal(task.snoozed_until))}" ${mode==='until'?'required':''}></label></div>`;
}
function taskSnoozeWire(host) {
  const mode = host.querySelector('[data-snooze-mode]'), date = host.querySelector('[data-snooze-date]'), input = host.querySelector('[data-snooze-until]');
  mode.onchange = () => { date.hidden = mode.value !== 'until'; input.required = mode.value === 'until'; };
  host.querySelectorAll('[data-snooze-shortcut]').forEach(button => button.onclick = () => {
    const choice = button.dataset.snoozeShortcut;
    mode.value = 'until'; mode.onchange();
    if (choice === 'pick') { input.focus(); return; }
    input.value = `${sjAddDays(sjToday(), Number(choice))}T09:00`;
  });
}
// Return undefined when unchanged: ordinary edits must not resubmit an expired timestamp,
// or silently alter its precision. Null is an explicit unsnooze; otherwise the new `snoozed_until`.
function taskSnoozeChange(host, task = {}) {
  const mode = host.querySelector('[data-snooze-mode]').value;
  const input = host.querySelector('[data-snooze-until]');
  const initial = host.querySelector('[data-snooze-mode]').dataset.initial;
  if (mode === initial && (mode !== 'until' || input.value === taskSnoozeLocal(task.snoozed_until))) return undefined;
  if (mode === 'none') return null;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/.exec(input.value);
  const until = match ? sjInstant(match[1], Number(match[2]), Number(match[3])) : new Date(NaN);
  if (!input.value || !Number.isFinite(until.getTime()) || until.getTime() <= Date.now()) throw new Error('Choose a future snooze date and time.');
  return until.toISOString();
}
