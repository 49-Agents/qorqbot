// Shared by business and Global tasks. Stored timestamps are UTC; inputs use local time.
function taskSnoozed(task, now = Date.now()) {
  return task.snooze?.indefinite === true || Boolean(task.snooze?.until && Date.parse(task.snooze.until) > now);
}
function taskSnoozeLabel(task) {
  if (!taskSnoozed(task)) return '';
  return task.snooze.indefinite ? 'Snoozed until further notice' : 'Snoozed until ' +
    new Date(task.snooze.until).toLocaleString(undefined, {dateStyle:'medium',timeStyle:'short'});
}
function taskSnoozeLocal(until) {
  if (!until) return '';
  const d = new Date(until), pad = n => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}
function taskSnoozePicker(task = {}) {
  const mode = taskSnoozed(task) ? task.snooze.indefinite ? 'indefinite' : 'until' : 'none';
  return `<div class="task-snooze-picker"><label>Snooze<select data-snooze-mode data-initial="${mode}" aria-label="Snooze">
    ${[['none','Not snoozed'],['until','Until a date/time'],['indefinite','Until further notice']].map(([value,label])=>`<option value="${value}" ${value===mode?'selected':''}>${label}</option>`).join('')}</select></label>
    <label data-snooze-date ${mode==='until'?'':'hidden'}>Until (your local time)<input data-snooze-until aria-label="Snooze until" type="datetime-local" value="${esc(taskSnoozeLocal(task.snooze?.until))}" ${mode==='until'?'required':''}></label></div>`;
}
function taskSnoozeWire(host) {
  const mode = host.querySelector('[data-snooze-mode]'), date = host.querySelector('[data-snooze-date]'), input = host.querySelector('[data-snooze-until]');
  mode.onchange = () => { date.hidden = mode.value !== 'until'; input.required = mode.value === 'until'; };
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
  const until = new Date(input.value);
  if (!input.value || !Number.isFinite(until.getTime()) || until.getTime() <= Date.now()) throw new Error('Choose a future snooze date and time.');
  return {until:until.toISOString()};
}
