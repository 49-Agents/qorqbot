// Shared by business and Global tasks. Stored timestamps are UTC; inputs and labels use San Juan time.
function taskSnoozed(task, now = Date.now()) {
  return task.snooze?.indefinite === true || Boolean(task.snooze?.until && Date.parse(task.snooze.until) > now);
}
function taskSnoozeLabel(task) {
  if (!taskSnoozed(task)) return '';
  return task.snooze.indefinite ? 'Snoozed until further notice' : 'Snoozed until ' +
    sjDateTime(task.snooze.until);
}
function taskSnoozeLocal(until) {
  if (!until) return '';
  const parts = sjParts(until);
  return parts ? `${parts.date}T${parts.time}` : '';
}
function taskSnoozePicker(task = {}) {
  const mode = taskSnoozed(task) ? task.snooze.indefinite ? 'indefinite' : 'until' : 'none';
  return `<div class="task-snooze-picker"><label>Snooze<select data-snooze-mode data-initial="${mode}" aria-label="Snooze">
    ${[['none','Not snoozed'],['until','Until a date/time'],['indefinite','Until further notice']].map(([value,label])=>`<option value="${value}" ${value===mode?'selected':''}>${label}</option>`).join('')}</select></label>
    <div class="task-snooze-shortcuts">${[['1','Tomorrow'],['7','Next week'],['pick','Pick date'],['indefinite','Until further notice']].map(([value,label])=>`<button type="button" class="quiet" data-snooze-shortcut="${value}">${label}</button>`).join('')}</div>
    <label data-snooze-date ${mode==='until'?'':'hidden'}>Until (San Juan time)<input data-snooze-until aria-label="Snooze until" type="datetime-local" value="${esc(taskSnoozeLocal(task.snooze?.until))}" ${mode==='until'?'required':''}></label></div>`;
}
function taskSnoozeWire(host) {
  const mode = host.querySelector('[data-snooze-mode]'), date = host.querySelector('[data-snooze-date]'), input = host.querySelector('[data-snooze-until]');
  mode.onchange = () => { date.hidden = mode.value !== 'until'; input.required = mode.value === 'until'; };
  host.querySelectorAll('[data-snooze-shortcut]').forEach(button => button.onclick = () => {
    const choice = button.dataset.snoozeShortcut;
    mode.value = choice === 'indefinite' ? 'indefinite' : 'until'; mode.onchange();
    if (choice === 'pick') { input.focus(); return; }
    if (choice !== 'indefinite') {
      input.value = `${sjAddDays(sjToday(), Number(choice))}T09:00`;
    }
  });
}
// Return undefined when unchanged: ordinary edits must not resubmit an expired timestamp,
// or silently alter its precision. Null is an explicit unsnooze.
function taskSnoozeChange(host, task = {}) {
  const mode = host.querySelector('[data-snooze-mode]').value;
  const input = host.querySelector('[data-snooze-until]');
  const initial = host.querySelector('[data-snooze-mode]').dataset.initial;
  if (mode === initial && (mode !== 'until' || input.value === taskSnoozeLocal(task.snooze?.until))) return undefined;
  if (mode === 'none') return null;
  if (mode === 'indefinite') return {indefinite:true};
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})/.exec(input.value);
  const until = match ? sjInstant(match[1], Number(match[2]), Number(match[3])) : new Date(NaN);
  if (!input.value || !Number.isFinite(until.getTime()) || until.getTime() <= Date.now()) throw new Error('Choose a future snooze date and time.');
  return {until:until.toISOString()};
}
