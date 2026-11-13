// One time grid over provider calendars; events stay in the provider.
const calendarZone = () => SJ_ZONE;
// Navigation and all-day events use literal dates; only timed events are converted to the app zone.
const calendarDateString = date => typeof date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(date) ? date : sjDate(date);
const calendarAddDays = (date, days) => sjAddDays(calendarDateString(date),days);
const calendarDateFormat = (date, options, locale = 'en-GB') => new Date(calendarDateString(date)+'T12:00:00Z').toLocaleDateString(locale,{...options,timeZone:'UTC'});
const calendarMonthStart = date => calendarDateString(date).slice(0,7)+'-01';
const calendarMonthShift = (month, offset) => new Date(Date.UTC(Number(month.slice(0,4)),Number(month.slice(5,7))-1+offset,1)).toISOString().slice(0,10);
function calendarDayStart(date) {
  const key = calendarDateString(date);
  // Some zones advance their clocks at midnight. Queries start at the first existing minute.
  for (let minute = 0; minute < 1440; minute++) {
    const start = sjInstant(key,Math.floor(minute/60),minute%60);
    if (Number.isFinite(+start)) return start;
  }
  return calendarDayStart(calendarAddDays(key,1)); // A wholly skipped civil date has no timed events.
}
const calendarLocalInput = value => {
  if (!value || value.length === 10) return value || '';
  const parts = sjParts(value);
  return parts ? `${parts.date}T${parts.time}` : '';
};
function calendarInputInstant(value) {
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}):(\d{2})$/.exec(value);
  const instant = match ? sjInstant(match[1],Number(match[2]),Number(match[3])) : new Date(NaN);
  if (!Number.isFinite(+instant)) throw new Error(`Choose a valid time in ${calendarZone()}; this time may fall in a daylight-saving gap.`);
  return instant.toISOString();
}
const calendarTarget = event => ({connection_id:event.connection_id, calendar_id:event.calendar_id, event_id:event.id, revision:event.revision});
const calendarCall = async (operation, args = {}, key) => {
  try { return coreData(await post(`/root/operations/${operation}`, {...args, ...(key ? {idempotency_key:key} : {})})); }
  catch (error) { if (accessLost(error)) calendarDirectory = null; throw error; }
};
// Calendar names/IDs change much less often than events. Share one short-lived
// directory across views instead of rediscovering every provider on each poll.
// Explicit refresh bypasses it; event reads and all writes still go to the server.
const CALENDAR_DIRECTORY_FRESH_MS = 5 * 60000;
let calendarDirectory = null, calendarDirectoryFlight = null;
function calendarDirectoryLoad(force = false) {
  if (calendarDirectoryFlight) return calendarDirectoryFlight;
  if (!force && calendarDirectory && Date.now() - calendarDirectory.at < CALENDAR_DIRECTORY_FRESH_MS) return Promise.resolve(calendarDirectory.data);
  const flight = calendarDirectoryFlight = calendarCall('calendar_calendars_list').then(data => {
    calendarDirectory = data.errors.length ? null : {data, at:Date.now()};
    return data;
  }, error => { calendarDirectory = null; throw error; });
  const clear = () => { if (calendarDirectoryFlight === flight) calendarDirectoryFlight = null; };
  flight.then(clear, clear);
  return flight;
}
const calendarTime = (date, timeZone = calendarZone()) => date.toLocaleTimeString([], {hour:'numeric', minute:'2-digit',timeZone});
const calendarDateLabel = (date, now = new Date()) => {
  const key = calendarDateString(date);
  for (const [offset,label] of [[0,'Today'],[1,'Tomorrow'],[-1,'Yesterday']]) {
    if (key === calendarDateString(calendarAddDays(now,offset))) return label;
  }
  return calendarDateFormat(key,{day:'numeric', month:'short'});
};
const calendarEventWhen = event => {
  if (event.start.length === 10) {
    const first = event.start, last = calendarAddDays(event.end,-1);
    return `${calendarDateLabel(first)}${event.start === calendarDateString(last) ? '' : ' – '+calendarDateLabel(last)} · All day`;
  }
  const start = new Date(event.start), end = new Date(event.end);
  return `${calendarDateLabel(start)} · ${calendarTime(start)} – ${calendarDateString(start) === calendarDateString(end) ? '' : calendarDateLabel(end)+' · '}${calendarTime(end)}`;
};
const calendarIcon = name => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${AICONS[name] || ''}</svg>`;

// Personal status is an owner preference over a provider event; it never writes to the provider.
const calendarStatusLabels = {pending:'Pending',completed:'Completed',cancelled:'Cancelled',rescheduled:'Rescheduled'};
const calendarStatusMarks = {
  completed:'<path d="M5 12.5l4.5 4.5L19 7.5"/>', cancelled:'<path d="M6 6l12 12M18 6L6 18"/>',
  rescheduled:'<path d="M20 12a8 8 0 1 1-2.5-5.8"/><path d="M20 4v4.5h-4.5"/>'};
const calendarStatusMark = status => calendarStatusMarks[status] ? `<i class="calendar-status-mark" aria-hidden="true"><svg viewBox="0 0 24 24" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">${calendarStatusMarks[status]}</svg></i>` : '';
const calendarIsRecurring = event => !!event.series_id || (event.recurrence?.length || 0) > 0;

// Display-only helpers for the event detail; addresses used for RSVP and identity are never rewritten.
const calendarDisplayAddress = value => String(value ?? '').replace(/^mailto:/i,'');
const calendarGuestLabels = {needsAction:'Awaiting response',accepted:'Accepted',tentative:'Maybe',declined:'Declined'};
const calendarGuestLabel = status => calendarGuestLabels[status] || String(status ?? '');
// Escapes text and links only http(s) URLs that parse; anything else stays plain text.
function calendarLinkify(text) {
  const source = String(text ?? ''); let out = '', last = 0;
  for (const match of source.matchAll(/https?:\/\/[^\s<>"']+/gi)) {
    let label = match[0], trailing = '';
    while (/[.,;:!?)\]]$/.test(label)) { trailing = label.slice(-1)+trailing; label = label.slice(0,-1); }
    let href = null;
    try { const url = new URL(label); if (url.protocol === 'http:' || url.protocol === 'https:') href = url.href; } catch (_) {}
    out += esc(source.slice(last,match.index));
    out += href ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>${esc(trailing)}` : esc(match[0]);
    last = match.index+match[0].length;
  }
  return out+esc(source.slice(last));
}
// Human date and time in the app timezone; literal all-day dates never shift.
function calendarWhenParts(event) {
  const day = date => calendarDateFormat(date,{weekday:'short', day:'numeric', month:'short', year:'numeric'},[]);
  if (event.start.length === 10) {
    const first = event.start, last = calendarAddDays(event.end,-1);
    return {date:event.start === calendarDateString(last) ? day(first) : `${day(first)} – ${day(last)}`, time:'All day'};
  }
  const start = new Date(event.start), end = new Date(event.end);
  if (isNaN(start) || isNaN(end)) return {time:calendarEventWhen(event)};
  const zone = new Intl.DateTimeFormat([], {timeZone:calendarZone(),timeZoneName:'short'}).formatToParts(start).find(part => part.type === 'timeZoneName')?.value || '';
  const sameDay = calendarDateString(start) === calendarDateString(end);
  return {date:sameDay ? day(start) : `${day(start)} – ${day(end)}`, time:`${calendarTime(start)} – ${calendarTime(end)}${zone ? ' '+zone : ''}`};
}
function calendarDetailBody(event, calendar, attendee, recurring, statusTarget) {
  const row = (label, value) => value ? `<div class="calendar-meta-row"><dt>${label}</dt><dd>${value}</dd></div>` : '';
  const when = calendarWhenParts(event);
  const meet = event.meet_url && /^https:\/\/meet\.google\.com\//.test(event.meet_url) ? `<a href="${esc(event.meet_url)}" target="_blank" rel="noopener noreferrer">Join Google Meet</a>` : '';
  const where = [event.location ? calendarLinkify(event.location) : '', meet].filter(Boolean).join('<br>');
  const organizer = calendarDisplayAddress(event.organizer);
  const guests = event.attendees || [];
  const recurrence = recurring ? `<p class="muted calendar-recurrence">${event.recurrence_edit_scope === 'series' || event.recurrence.length ? 'Recurring series. Changes here apply to the series.' : 'Occurrence of a recurring event.'}</p>` : '';
  return `<div class="calendar-detail">
      <dl class="calendar-meta">
        ${row('When',`${when.date ? `<span class="calendar-when-date">${esc(when.date)}</span>` : ''}<strong class="calendar-when-time">${esc(when.time)}</strong>`)}
        ${row('Where',where)}
        ${row('Calendar',esc(calendar?.name || ''))}
        ${row('Organizer',esc(organizer))}
        ${row('Invitation',attendee ? esc(calendarGuestLabel(attendee.status)) : '')}
      </dl>
      ${recurrence}
      ${event.description ? `<section class="calendar-about"><h3>About</h3><div class="calendar-description">${calendarLinkify(event.description)}</div></section>` : ''}
      ${guests.length ? `<details class="calendar-guests"><summary>Guests (${guests.length})</summary><ul>${guests.map(a => `<li><span>${esc(calendarDisplayAddress(a.email))}</span><small>${esc(calendarGuestLabel(a.status))}</small></li>`).join('')}</ul></details>` : ''}
      <div class="calendar-personal-status"><span class="calendar-status-label" aria-hidden="true">Your tracking status</span>
        <div class="calendar-status-pills" role="group" aria-label="Your tracking status">${Object.entries(calendarStatusLabels).map(([value,label]) => `<button type="button" class="calendar-status-pill" data-calendar-status-choice="${value}" aria-pressed="false">${label}</button>`).join('')}</div>
        <p class="muted calendar-status-help">Only for you; guests aren’t notified.${statusTarget.occurrence_start ? ` Applies to the ${esc(calendarDateLabel(statusTarget.occurrence_start))} occurrence only.` : ''}</p>
        <p class="calendar-status-note" role="status"></p></div>
      <p class="calendar-detail-error" role="alert"></p></div>`;
}
// connection + calendar + event + occurrence; nonrecurring events use an empty occurrence so moving them keeps the label.
function calendarStatusTarget(event) {
  let occurrence_start = '';
  if (calendarIsRecurring(event) && event.start) occurrence_start = event.start.length === 10 ? event.start : new Date(event.start).toISOString();
  return {connection_id:event.connection_id, calendar_id:event.calendar_id, event_id:event.id, occurrence_start};
}
const calendarStatusKey = t => JSON.stringify([t.connection_id,t.calendar_id,t.event_id,t.occurrence_start]);
const calendarStatusConflict = error => error?.httpStatus === 409 || /conflict|stale|revision|precondition/i.test(String(error?.code || ''));

// One owner-wide cache shared by every mounted Focus pane and Calendar drawer.
const calendarStatuses = (() => {
  const items = new Map(), savedAt = new Map(), listeners = new Set();
  let known = false, denied = false, error = '', epoch = 0, generation = 0, inflight = null;
  const notify = () => [...listeners].forEach(listener => { try { listener(); } catch (e) { console.error(e); } });
  const reset = () => { items.clear(); savedAt.clear(); known = false; generation++; inflight = null; clearEtagCache(); };
  const valid = item => item && typeof item.connection_id === 'string' && typeof item.calendar_id === 'string' && typeof item.event_id === 'string'
    && typeof item.occurrence_start === 'string' && calendarStatusLabels[item.status] && Number.isInteger(item.revision) && item.revision > 0;
  const lost = e => { reset(); denied = true; error = 'Personal statuses are unavailable; sign in again.'; };
  const store = {
    get state() { return {known, denied, error}; },
    get: target => items.get(calendarStatusKey(target)) || null,
    // null means unknown (never loaded), not Pending.
    statusOf(event) { if (!known) return null; return items.get(calendarStatusKey(calendarStatusTarget(event)))?.status || 'pending'; },
    subscribe(listener) { listeners.add(listener); return () => listeners.delete(listener); },
    refresh() {
      if (inflight) return inflight;
      const gen = generation, started = ++epoch;
      const run = (async () => {
        try {
          const data = coreData(await api('/root/calendar-statuses'));
          if (gen !== generation) return;
          if (!Array.isArray(data.items) || !data.items.every(valid)) throw new Error('The server returned an invalid response.');
          const next = new Map(data.items.map(item => [calendarStatusKey(item),item]));
          // A save finished after this read began is newer than the read.
          for (const [key,local] of items) if ((savedAt.get(key) || 0) > started && local.revision > (next.get(key)?.revision || 0)) next.set(key,local);
          items.clear(); next.forEach((item,key) => items.set(key,item));
          known = true; denied = false; error = '';
        } catch (e) {
          if (gen !== generation) return;
          if (accessLost(e)) lost(e);
          else error = known ? `Couldn’t refresh statuses; showing the last known ones. ${e.message}` : `Personal statuses unavailable. ${e.message}`;
        } finally { if (inflight === run) inflight = null; }
        notify();
      })();
      inflight = run; return run;
    },
    async save(target, status, expectedRevision, requestId) {
      const gen = generation;
      let item;
      try {
        item = coreData(await post('/root/calendar-statuses',{...target,status,expected_revision:expectedRevision,request_id:requestId})).item;
      } catch (e) { if (accessLost(e)) { lost(e); notify(); } throw e; }
      if (!valid(item) || calendarStatusKey(item) !== calendarStatusKey(target) || item.status !== status) {
        const bad = new Error('The server returned an invalid response.'); bad.code = 'invalid_response'; throw bad;
      }
      if (gen === generation) {
        const key = calendarStatusKey(item);
        if (item.revision >= (items.get(key)?.revision || 0)) items.set(key,item);
        savedAt.set(key,++epoch); known = true; denied = false; error = '';
        notify();
      }
      return item;
    }};
  return store;
})();
const calendarStatusNotice = () => calendarStatuses.state.error;

// Split a timed event at midnight in the app zone, retaining its position on each day's clock.
function calendarDaySpan(event, day) {
  if (!event.start || event.start.length === 10 || event.status === 'cancelled') return null;
  const start = sjParts(event.start), end = sjParts(event.end), key = calendarDateString(day);
  if (!start || !end || end.date < key || start.date > key || new Date(event.end) <= new Date(event.start)) return null;
  const from = start.date < key ? 0 : start.hour*60 + Number(start.time.slice(3));
  const to = end.date > key ? 1440 : end.hour*60 + Number(end.time.slice(3));
  if (to === 0 && +new Date(event.end) === +calendarDayStart(key)) return null;
  return {event, start:from, end:Math.max(from+1,to)};
}

// Independent overlapping blocks occupy separate lanes; this is display only.
function calendarEventLanes(spans) {
  const result = [], sorted = [...spans].sort((a,b) => a.start-b.start || b.end-a.end);
  let group = [], ends = [], groupEnd = -1;
  const finish = () => { group.forEach(item => result.push({...item, columns:ends.length})); group = []; ends = []; };
  for (const span of sorted) {
    if (span.start >= groupEnd) finish();
    let lane = ends.findIndex(end => end <= span.start);
    if (lane < 0) lane = ends.length;
    ends[lane] = span.end; group.push({...span,lane}); groupEnd = Math.max(groupEnd,span.end);
  }
  finish(); return result;
}

// Retained only in this page session, partitioned by the freshly verified owner.
const calendarAttempts = new Map(), calendarAttemptListeners = new Set();
const calendarAttemptsChanged = () => { for (const fn of [...calendarAttemptListeners]) { try { fn(); } catch (error) { console.error(error); } } };
async function calendarOwner() {
  const session = await loadSession(true);
  if (session?.is_owner !== true || session.principal_kind !== 'human' || typeof session.principal_id !== 'string' || !session.principal_id.trim()) throw new Error('Verify your owner session and try again.');
  return session.principal_id;
}
const calendarDefiniteFailure = error => error?.httpStatus >= 400 && error.httpStatus < 500 && ![408,429].includes(error.httpStatus) && error.code !== 'invalid_response';
const calendarOwnerAttempts = owner => [...calendarAttempts.values()].filter(attempt => attempt.owner === owner);
async function calendarRunAttempt(attempt, refresh) {
  const owner = await calendarOwner();
  if (attempt.owner && attempt.owner !== owner) throw new Error('This request belongs to a different owner session.');
  attempt.owner = owner;
  try {
    if (attempt.operation === 'personal_status') await calendarStatuses.save(attempt.args.target,attempt.args.status,attempt.args.revision,attempt.key);
    else await calendarCall(attempt.operation,attempt.args,attempt.key);
  } catch (error) {
    if (calendarDefiniteFailure(error)) calendarAttempts.delete(attempt.id);
    else calendarAttempts.set(attempt.id,attempt);
    calendarAttemptsChanged();
    if (!calendarDefiniteFailure(error)) error.message = `The outcome is unconfirmed. Review unconfirmed calendar changes to retry the same request. ${error.message || ''}`;
    throw error;
  }
  calendarAttempts.delete(attempt.id); calendarAttemptsChanged();
  if (attempt.operation !== 'personal_status') {
    try { calendarChanged(refresh); } catch (error) { console.error(error); }
  }

}
const calendarAttempt = (operation,args,key,event,calendar) => ({id:newRequestId(),operation,args,key,
  title:event?.title || args.title || 'Untitled event', start:event?.start || args.start || '', calendar:calendar?.name || args.calendar_id || '',
  occurrence:event ? calendarStatusTarget(event).occurrence_start : ''});

async function calendarSubmitAttempt(attempt, refresh, surface) {
  const owner = await calendarOwner();
  if (attempt.owner && attempt.owner !== owner) throw new Error('Owner session changed. Try again.');
  attempt.owner = owner;
  const previous = calendarOwnerAttempts(owner).find(old => old.operation === attempt.operation && old.occurrence === attempt.occurrence && JSON.stringify(old.args) === JSON.stringify(attempt.args));
  if (previous) {
    const choice = await surface.chooseAttempt(attempt.operation === 'calendar_event_create');
    if (choice === 'cancel') return false;
    if (choice === 'retry') { await calendarRunAttempt(previous,refresh); return true; }
  }
  await calendarRunAttempt(attempt,refresh);
  return true;
}

// A provider write invalidates every mounted calendar view and the shared excerpt cache.
const calendarChanges = new Set();
let calendarWriteGeneration = 0;
function calendarChanged(refresh) {
  calendarWriteGeneration++;
  for (const listener of [...calendarChanges]) { try { listener(); } catch (error) { console.error(error); } }
  refresh?.();
}

// Each pane owns its navigation and lifecycle; embedding never changes the route.
function calendarMount(host, {isCurrent = () => true, embedded = false} = {}) {
  let calendarDate = calendarDateString(new Date()), calendarViewDays = embedded ? 1 : 3, calendarGeneration = 0, active = true, repaint = () => {};
  let calendarMonth = calendarMonthStart(calendarDate), dispose = () => {};
  const render = async (force = false) => {
    dispose();
    const generation = ++calendarGeneration;
    const countForWidth = () => host.clientWidth <= 600 ? 1 : calendarViewDays;
    const count = countForWidth();
    const first = calendarDate, zone = calendarZone();
    const last = calendarAddDays(first,count), dates = Array.from({length:count},(_,i) => calendarAddDays(first,i));
    host.innerHTML = `<section class="pane calendar-page${embedded ? ' calendar-embedded' : ''}" aria-label="Calendar" style="--calendar-columns:${count}">
      <header class="calendar-header"><div class="calendar-brand"><h1>Calendar</h1><time class="calendar-clock" aria-label="Current time"></time></div>
        <div class="calendar-header-actions"><button type="button" class="ghost" data-calendar-unconfirmed hidden>Unconfirmed calendar changes</button><button type="button" class="calendar-icon" data-calendar-refresh aria-label="Refresh calendars">${calendarIcon('refresh-cw')}</button><button type="button" class="primary" data-calendar-add disabled><span aria-hidden="true">＋</span> Add event</button></div></header>
      <div class="calendar-layout"><aside class="calendar-sidebar" aria-label="Calendar navigation">
        <div class="calendar-month-heading"><h2></h2><div><button type="button" class="calendar-icon" data-month-prev aria-label="Previous month">‹</button><button type="button" class="calendar-icon" data-month-next aria-label="Next month">›</button></div></div>
        <div class="calendar-month" aria-label="Choose a date"></div><div class="calendar-sources"><h2>My calendars</h2><div></div></div>
      </aside><main class="calendar-main"><div class="calendar-toolbar"><h2 class="calendar-range"></h2><div class="calendar-controls">
        <button type="button" class="calendar-icon" data-calendar-prev aria-label="Previous days">‹</button><button type="button" class="calendar-icon" data-calendar-next aria-label="Next days">›</button>
        <select class="inp calendar-view" aria-label="Calendar view"><option value="1">Day</option><option value="3">3 days</option><option value="7">Week</option></select></div></div>
        <div class="calendar-status" role="status">Loading calendars…</div><div class="calendar-dayheads"></div><div class="calendar-all-day" hidden></div><div class="calendar-week" aria-label="Hourly calendar grid"></div><div class="calendar-more"></div>
      </main></div></section>`;
    const page = $('.calendar-page',host);
    const sizePage = () => {
      page.classList.toggle('calendar-narrow',host.clientWidth <= 880);
      page.classList.toggle('calendar-compact',host.clientWidth <= 600);
    };
    sizePage();
    const current = () => active && page.isConnected && generation === calendarGeneration && isCurrent();
    const reviewButton = $('[data-calendar-unconfirmed]',page);
    let recoveryGeneration = 0;
    const updateUnconfirmed = async () => {
      const check = ++recoveryGeneration;
      try { const owner = await calendarOwner(); if (current() && check === recoveryGeneration) reviewButton.hidden = !calendarOwnerAttempts(owner).length; }
      catch (_) { if (current() && check === recoveryGeneration) reviewButton.hidden = true; }
    };
    reviewButton.onclick = () => calendarReviewOpen(reviewButton);
    calendarAttemptListeners.add(updateUnconfirmed);
    dispose = () => { calendarAttemptListeners.delete(updateUnconfirmed); };
    void updateUnconfirmed();
    const reload = (force = false) => { if (current()) return render(force === true); };
    const chooseDate = date => { calendarDate = date; calendarMonth = calendarMonthStart(date); reload(); };
    const paintDates = () => {
      $('.calendar-range',page).textContent = count === 1 ? calendarDateLabel(first) : `${calendarDateLabel(first)} – ${calendarDateLabel(calendarAddDays(last,-1))}`;
      $('.calendar-dayheads',page).innerHTML = '<span></span>'+dates.map(date => `<div class="calendar-dayhead">${esc(calendarDateFormat(date,{weekday:'short'}))}</div>`).join('');
      $('.calendar-month-heading h2',page).textContent = calendarDateFormat(calendarMonth,{month:'long'});
      const offset = (new Date(calendarMonth+'T12:00:00Z').getUTCDay()+6)%7, total = Number(calendarAddDays(calendarMonthShift(calendarMonth,1),-1).slice(8));
      const month = $('.calendar-month',page);
      month.innerHTML = ['M','T','W','T','F','S','S'].map(day => `<span class="calendar-weekday">${day}</span>`).join('') + (offset ? `<span style="grid-column:span ${offset}"></span>` : '') +
        Array.from({length:total},(_,i) => { const date = calendarAddDays(calendarMonth,i);
          return `<button type="button" class="calendar-monthday ${date >= first && date < last ? 'in-range' : ''}" data-calendar-date="${date}" aria-label="${esc(calendarDateLabel(date))}" aria-pressed="${date === first}">${i+1}</button>`;
        }).join('');
      month.querySelectorAll('button').forEach(button => button.onclick = () => chooseDate(button.dataset.calendarDate));
    };
    $('.calendar-view',page).value = String(calendarViewDays);
    $('.calendar-view',page).onchange = event => { calendarViewDays = Number(event.target.value); reload(); };
    $('[data-calendar-prev]',page).onclick = () => chooseDate(calendarAddDays(first,-count));
    $('[data-calendar-next]',page).onclick = () => chooseDate(last);
    $('[data-calendar-refresh]',page).onclick = () => reload(true);
    for (const [selector,offset] of [['[data-month-prev]',-1],['[data-month-next]',1]]) $(selector,page).onclick = () => {
      calendarMonth = calendarMonthShift(calendarMonth,offset); paintDates();
    };
    paintDates();
    let clockDay = calendarDateString(new Date()), clockTimer, observer;
    const cleanup = () => { clearInterval(clockTimer); observer?.disconnect(); };
    const disposeReview = dispose;
    dispose = () => { cleanup(); disposeReview(); };
    const tick = () => {
      if (!current()) { cleanup(); return; }
      if (zone !== calendarZone()) { reload(); return; }
      const now = new Date(), clock = $('.calendar-clock',page); clock.dateTime = now.toISOString(); clock.textContent = calendarTime(now); clock.title = zone;
      const day = calendarDateString(now); if (day !== clockDay) { clockDay = day; paintDates(); }
    };
    tick(); clockTimer = setInterval(tick,1000);
    observer = new ResizeObserver(() => { if (!current()) cleanup(); else { sizePage(); if (countForWidth() !== count) reload(); } });
    observer.observe(host);

    const status = $('.calendar-status',page), allEvents = [], failures = new Map();
    let calendars = [], loading = true, fixedMessage = '';
    const eventButton = (event,allDay) => calendarEventButton(event,calendars,reload,allDay);
    const paint = () => {
      if (!current()) return;
      status.textContent = fixedMessage || [...failures.values(),calendarStatusNotice()].filter(Boolean).join(' · ') || (loading ? 'Loading events…' : '');
      calendarPaintGrid(page,dates,allEvents,eventButton);
    };
    repaint = paint;
    paint();
    void calendarStatuses.refresh();
    try {
      const result = await calendarDirectoryLoad(force);
      if (!current()) return;
      calendars = result.calendars;
      result.errors.forEach(e => failures.set(e.connection_id,`${e.connection_id}: ${e.message}`));
      $('.calendar-sources > div',page).innerHTML = calendars.map(calendar => `<div class="calendar-source"><span class="calendar-dot"></span><span>${esc(calendar.name)} · ${esc(calendar.provider_name || calendar.provider)}</span></div>`).join('');
      if (!result.configured) { fixedMessage = 'Connect a calendar to get started.'; paint(); return; }
      $('[data-calendar-add]',page).disabled = !calendars.some(c => c.writable);
      $('[data-calendar-add]',page).onclick = e => calendarEdit(null,calendars,reload,calendarDate,e.currentTarget);
      await Promise.all(calendars.map(async calendar => {
        let cursor, more;
        const failureKey = `${calendar.connection_id}:${calendar.id}`;
        const load = async () => {
          if (more) more.disabled = true;
          try {
            const result = await calendarCall('calendar_events_list',{connection_id:calendar.connection_id,calendar_id:calendar.id,
              start:calendarDayStart(first).toISOString(),end:calendarDayStart(last).toISOString(),limit:200,...(cursor ? {cursor} : {})});
            if (!current()) return;
            result.events.forEach(event => { const index = allEvents.findIndex(old => calendarStatusKey(calendarStatusTarget(old)) === calendarStatusKey(calendarStatusTarget(event))); if (index < 0) allEvents.push(event); else allEvents[index] = event; });
            cursor = result.next_cursor; failures.delete(failureKey);
            if (!more) { more = document.createElement('button'); more.type = 'button'; more.onclick = load; $('.calendar-more',page).append(more); }
            more.textContent = `Load more from ${calendar.name}`; more.hidden = !cursor; more.disabled = false;
          } catch (error) { if (current()) { failures.set(failureKey,`${calendar.name}: ${error.message}`); if (more) more.disabled = false; } }
          paint();
        };
        await load();
      }));
      loading = false; paint();
    } catch (error) { if (current()) { fixedMessage = error.message; paint(); } }
  };
  const unsubscribe = calendarStatuses.subscribe(() => { if (active) repaint(); });
  const changed = () => { if (active && isCurrent()) void render(); };
  calendarChanges.add(changed);
  void render();
  return () => { active = false; calendarChanges.delete(changed); unsubscribe(); dispose(); };
}

function calendarEventButton(event, calendars, refresh, allDay = false) {
  const calendar = calendars.find(c => c.id === event.calendar_id && c.connection_id === event.connection_id), pending = event.attendees?.some(a => calendar?.organizers?.some(email => email.toLowerCase() === a.email.toLowerCase()) && a.status === 'needsAction');
  const button = document.createElement('button'); button.type = 'button';
  const personal = calendarStatuses.statusOf(event), shown = personal && personal !== 'pending' ? personal : '';
  button.className = `calendar-event${allDay ? ' calendar-all-day-event' : ''}${shown ? ` calendar-status-${shown}` : ''}`;
  const when = allDay ? 'All day' : `${calendarTime(new Date(event.start))} – ${calendarTime(new Date(event.end))}`;
  const label = `${event.title || 'Untitled event'} · ${when} · ${calendar?.name || ''}${pending ? ' · Invitation' : ''}${shown ? ` · ${calendarStatusLabels[shown]}` : ''}`;
  button.setAttribute('aria-label',label); button.title = label;
  button.innerHTML = `${calendarStatusMark(shown)}<strong>${esc(event.title || 'Untitled event')}</strong>`;
  button.onclick = () => calendarOpen(event,calendars,refresh,event,button); return button;
}

function calendarPaintGrid(page, dates, allEvents, eventButton, hourHeight = 64) {
  page.style.setProperty('--calendar-hour',hourHeight+'px');
  const spans = dates.map(day => calendarEventLanes(allEvents.map(event => calendarDaySpan(event,day)).filter(Boolean)));
  const flat = spans.flat();
  const startHour = Math.min(8,...flat.map(item => Math.floor(item.start/60)));
  const endHour = Math.max(19,...flat.map(item => Math.ceil(item.end/60)));
  page.style.setProperty('--calendar-hours',endHour-startHour);
  const grid = $('.calendar-week',page);
  // Axis labels are wall-clock hours, including the skipped/repeated hour on DST transition days.
  grid.innerHTML = `<div class="calendar-axis">${Array.from({length:endHour-startHour+1},(_,i) => `<span class="calendar-tick" style="top:${i*hourHeight}px">${esc(calendarTime(new Date(Date.UTC(2000,0,1,startHour+i)),'UTC'))}</span>`).join('')}</div>`;
  const allDayRow = $('.calendar-all-day',page); allDayRow.innerHTML = '<span class="calendar-all-day-label">All day</span>'; let hasAllDay = false;
  dates.forEach((date,index) => {
    const key = calendarDateString(date), column = document.createElement('section'); column.className = 'calendar-day'; column.dataset.calendarDay = key;
    column.setAttribute('aria-label',calendarDateFormat(date,{weekday:'long',day:'numeric',month:'long'}));
    for (const span of spans[index]) {
      const button = eventButton(span.event), height = (span.end-span.start)/60*hourHeight;
      button.style.top = `${(span.start/60-startHour)*hourHeight}px`; button.style.height = `${height}px`;
      button.style.left = `calc(${span.lane/span.columns*100}% + 3px)`; button.style.width = `calc(${100/span.columns}% - 6px)`;
      if (height < 44) button.classList.add('calendar-short-event');
      if (height >= 68) { const time = document.createElement('span'); time.textContent = `${calendarTime(new Date(span.event.start))} – ${calendarTime(new Date(span.event.end))}`; button.append(time); }
      column.append(button);
    }
    grid.append(column);
    const allDayCell = document.createElement('div'); allDayCell.className = 'calendar-all-day-cell';
    allEvents.filter(event => event.status !== 'cancelled' && event.start?.length === 10 && event.start <= key && key < event.end)
      .forEach(event => { hasAllDay = true; allDayCell.append(eventButton(event,true)); });
    allDayRow.append(allDayCell);
  });
  allDayRow.hidden = !hasAllDay;
  return {startHour,endHour};
}

// Calendar events for the shared attention strip, read from a week back to two hours ahead. An event joins the strip one
// hour before it starts (an all-day event at the start of its day) and stays, past or not, until the owner discards it.
// The strip filters on `appear`, so one read serves every repaint. Provider events are never changed by discarding.
const CALENDAR_QUEUE_LEAD_MS = 3600000, CALENDAR_QUEUE_BACK_MS = 7 * 86400000, CALENDAR_QUEUE_AHEAD_MS = 2 * 3600000;
// The key leads with the event's date in the app zone so the server can retire old discards without reading a provider.
const calendarQueueKey = event => `calendar:${event.start.length === 10 ? event.start : calendarDateString(new Date(event.start))}:${JSON.stringify([event.connection_id,event.calendar_id,event.id,event.start])}`;
const calendarQueueAppears = event => event.start.length === 10 ? +calendarDayStart(event.start) : +new Date(event.start) - CALENDAR_QUEUE_LEAD_MS;
async function calendarQueueLoad(now = Date.now()) {
  const start = new Date(now - CALENDAR_QUEUE_BACK_MS).toISOString(), end = new Date(now + CALENDAR_QUEUE_AHEAD_MS).toISOString();
  const result = await calendarDirectoryLoad();
  const calendars = result.calendars, events = [], errors = result.errors.map(e => `${e.connection_id}: ${e.message}`);
  await Promise.all(calendars.map(async calendar => {
    let cursor;
    try {
      do {
        const page = await calendarCall('calendar_events_list',{connection_id:calendar.connection_id,calendar_id:calendar.id,start,end,limit:200,...(cursor ? {cursor} : {})});
        events.push(...page.events); cursor = page.next_cursor;
      } while (cursor);
    } catch (error) {
      if (accessLost(error)) throw error;
      errors.push(`${calendar.name}: ${error.message}`);
    }
  }));
  const rows = new Map();
  for (const event of events) {
    if (event.status === 'cancelled' || typeof event.start !== 'string') continue;
    const appear = calendarQueueAppears(event);
    if (!Number.isFinite(appear)) continue;
    const key = calendarQueueKey(event), calendar = calendars.find(c => c.id === event.calendar_id && c.connection_id === event.connection_id);
    rows.set(key, {key, event, appear, label:`${event.title || 'Untitled event'} · ${calendarEventWhen(event)}${calendar?.name ? ' · ' + calendar.name : ''}`});
  }
  const items = [...rows.values()].sort((a,b) => a.appear - b.appear || a.key.localeCompare(b.key));
  return {items, calendars, configured:result.configured, errors};
}

// Calendar content owns one Focus expansion and replaces its children for nested work.
async function calendarSurface(title, origin, mount) {
  if (!window.focusExpansion?.open) { toast('Focus is unavailable.'); return null; }
  return window.focusExpansion.open({title, mount(host, session) {
    let disposed = false, busy = 0, dirty = () => false, cleanup = () => {}, prompt = null;
    const leave = () => {
      if (busy || disposed) return false;
      if (!dirty()) return true;
      if (prompt) return false;
      const box = document.createElement('div');
      box.className = 'calendar-leave-choice'; box.setAttribute('role','group');
      box.setAttribute('aria-label','Unsaved calendar changes');
      const message = document.createElement('p'); message.textContent = 'Discard your unsaved changes?';
      const keep = document.createElement('button'); keep.type = 'button'; keep.textContent = 'Keep editing';
      const discard = document.createElement('button'); discard.type = 'button'; discard.textContent = 'Discard changes';
      box.append(message,keep,discard);
      const content = host.firstElementChild, previousFocus = document.activeElement;
      if (content) content.inert = true;
      host.append(box);
      let resolve; const promise = new Promise(done => { resolve = done; });
      const finish = accepted => { if (!prompt) return; prompt = null; box.remove(); if (content) content.inert = false; if (accepted) dirty = () => false; else if (previousFocus?.isConnected && content?.contains(previousFocus)) previousFocus.focus(); resolve(accepted); };
      prompt = {promise, finish}; keep.onclick = () => finish(false); discard.onclick = () => finish(true); keep.focus();
      return promise;
    };
    session.setBeforeLeave(leave);
    const surface = {
      session,
      current: () => !disposed && session.isCurrent(),
      setDirty: fn => { dirty = fn; },
      async show(spec) {
        cleanup(); cleanup = () => {}; dirty = () => false;
        if (!surface.current()) return;
        session.setTitle(spec.title);
        host.innerHTML = `<section class="calendar-focus-content"><div class="calendar-focus-body">${spec.body}</div><div class="mfoot calendar-focus-actions"></div></section>`;
        const el = host.firstElementChild, footer = $('.mfoot',el);
        for (const action of spec.actions || []) {
          const button = document.createElement('button'); button.type = 'button';
          button.textContent = action.label; if (action.primary) button.className = 'primary';
          button.onclick = async () => {
            if (busy) return;
            let next = false;
            if (!action.navigation) busy++;
            try { await action.onClick(() => { next = true; },el); }
            finally { if (!action.navigation) busy--; }
            if (next && surface.current()) await spec.onBack();
          };
          footer.append(button);
        }
        cleanup = spec.onOpen?.(el) || (() => {});
      },
      async back(callback) { if (await leave() && surface.current()) await callback(); },
      async chooseAttempt(isCreate) {
        return new Promise(resolve => {
          const content = host.firstElementChild, box = document.createElement('div'); box.className = 'calendar-leave-choice'; box.setAttribute('role','group'); box.setAttribute('aria-label','Unconfirmed matching calendar change');
          const text = document.createElement('p'); text.textContent = 'A matching calendar change is unconfirmed. Retry the original request or cancel.';
          const review = document.createElement('button'); review.type = 'button'; review.textContent = 'Retry same request';
          const separate = document.createElement('button'); separate.type = 'button'; separate.textContent = 'Create separate event';
          const cancel = document.createElement('button'); cancel.type = 'button'; cancel.textContent = 'Cancel';
          const finish = value => { box.remove(); if (content) content.inert = false; resolve(value); };
          review.onclick = () => finish('retry'); separate.onclick = () => finish('separate'); cancel.onclick = () => finish('cancel');
          box.append(text,review); if (isCreate) box.append(separate); box.append(cancel); if (content) content.inert = true; host.append(box); review.focus();
        });
      },
      async review(onBack = null) {
        let owner;
        try { owner = await calendarOwner(); } catch (error) { toast(error.message); return; }
        if (!surface.current()) return;
        const content = host.firstElementChild, panel = document.createElement('section');
        panel.className = 'calendar-recovery'; panel.setAttribute('aria-label','Unconfirmed calendar changes');
        const title = document.createElement('h2'); title.textContent = 'Unconfirmed calendar changes'; panel.append(title);
        const note = document.createElement('p'); note.textContent = 'These changes may already have happened. Retry only the same request, or forget its local reminder.'; panel.append(note);
        const list = document.createElement('div'); panel.append(list);
        const sessionRetry = document.createElement('button'); sessionRetry.type = 'button'; sessionRetry.textContent = 'Retry owner session'; sessionRetry.hidden = true; panel.append(sessionRetry);
        const back = document.createElement('button'); back.type = 'button'; back.textContent = 'Back'; panel.append(back);
        let panelActive = true, verification = 0;
        const finish = () => { panelActive = false; verification++; window.removeEventListener('focus',reverify); panel.remove(); if (content) content.inert = false; back.onclick = null; onBack?.(); };
        session.onDispose(() => { panelActive = false; verification++; window.removeEventListener('focus',reverify); });
        back.onclick = finish;
        if (content) content.inert = true; host.append(panel);
        const paint = () => {
          list.replaceChildren();
          for (const attempt of calendarOwnerAttempts(owner)) {
            const row = document.createElement('div'); row.className = 'calendar-recovery-row';
            const label = document.createElement('p'); label.textContent = `${attempt.title} · ${attempt.start} · ${attempt.calendar} · ${attempt.operation}`;
            const retry = document.createElement('button'); retry.type = 'button'; retry.textContent = 'Retry same request';
            const forget = document.createElement('button'); forget.type = 'button'; forget.textContent = 'Forget';
            const explanation = document.createElement('small'); explanation.textContent = 'Forgetting removes only this local reminder; it does not undo or cancel the calendar operation.';
            const error = document.createElement('p'); error.setAttribute('role','alert');
            retry.onclick = async () => {
              if (busy) return; busy++; const check = verification; retry.disabled = forget.disabled = back.disabled = true;
              try { if (await calendarOwner() !== owner) { list.replaceChildren(); note.textContent = 'Owner session changed. Reopen this list.'; return; } await calendarRunAttempt(attempt, () => {}); if (surface.current() && panelActive && check === verification) paint(); }
              catch (e) { if (panelActive && check === verification) { if (!calendarAttempts.has(attempt.id)) paint(); else error.textContent = e.message; } }
              finally { busy--; retry.disabled = forget.disabled = back.disabled = false; }
            };
            forget.onclick = async () => {
              try { if (await calendarOwner() !== owner) { list.replaceChildren(); note.textContent = 'Owner session changed. Reopen this list.'; return; } calendarAttempts.delete(attempt.id); calendarAttemptsChanged(); paint(); }
              catch (e) { error.textContent = e.message; }
            };
            row.append(label,retry,forget,explanation,error); list.append(row);
          }
        };
        const reverify = async () => {
          const check = ++verification; list.replaceChildren(); sessionRetry.hidden = true;
          note.textContent = 'Verifying owner session…';
          try {
            const verified = await calendarOwner();
            if (!panelActive || check !== verification) return;
            owner = verified; note.textContent = 'These changes may already have happened. Retry only the same request, or forget its local reminder.'; paint();
          } catch (error) {
            if (!panelActive || check !== verification) return;
            note.textContent = error.message; sessionRetry.hidden = false;
          }
        };
        sessionRetry.onclick = reverify; window.addEventListener('focus',reverify);
        paint(); back.focus();
      },
      async close() { await session.close(); },
      setBusy(value) { busy += value ? 1 : -1; }
    };
    session.onDispose(() => { disposed = true; prompt?.finish(false); cleanup(); if (origin?.isConnected) origin.focus(); });
    void mount(surface);
  }});
}

function calendarReviewOpen(origin) {
  return calendarSurface('Unconfirmed calendar changes',origin, async surface => {
    await surface.show({title:'Unconfirmed calendar changes',body:'',actions:[]});
    await surface.review(() => surface.close());
  });
}

async function calendarOpen(event, calendars, refresh, clicked = event, origin = document.activeElement) {
  const statusTarget = calendarStatusTarget(clicked);
  return calendarSurface(event.title || 'Event',origin, async surface => {
    let detailGeneration = 0;
    const detail = async requested => {
      const generation = ++detailGeneration;
      await surface.show({title:'Event',body:'<p role="status">Loading event…</p>',actions:[]});
      try {
        const result = await calendarCall('calendar_event_get', {connection_id:requested.connection_id, calendar_id:requested.calendar_id, event_id:requested.id});
        if (!surface.current() || generation !== detailGeneration) return;
        event = result.event;
        const calendar = calendars.find(c => c.id === event.calendar_id && c.connection_id === event.connection_id);
        const attendee = event.attendees.find(a => calendar?.organizers?.some(address => address.toLowerCase() === a.email.toLowerCase()));
        const recurring = !!event.series_id || event.recurrence.length > 0;
        const responseActions = attendee ? ['accepted','tentative','declined'].map(response => { const key = newRequestId(), attempt = calendarAttempt('calendar_event_respond',{...calendarTarget(event),attendee:attendee.email,response},key,clicked,calendar); return ({
          label:({accepted:'Accept',tentative:'Maybe',declined:'Decline'})[response], onClick:async (close, el) => {
            el.querySelectorAll('.mfoot button').forEach(button => button.disabled = true);
            try { if (await calendarSubmitAttempt(attempt,refresh,surface)) close(); else el.querySelectorAll('.mfoot button').forEach(button => button.disabled = false); }
            catch (error) { $('.calendar-detail-error',el).textContent = error.message; el.querySelectorAll('.mfoot button').forEach(button => button.disabled = false); }
          }}); }) : [];
        await surface.show({title:event.title || 'Event', body:calendarDetailBody(event,calendar,attendee,recurring,statusTarget),
          onOpen:el => calendarStatusEditor(el,statusTarget,surface,clicked,calendar), onBack:() => surface.close(), actions:[
          ...responseActions,
          ...(event.series_id && event.series_id !== event.id ? [{label:'Open series', navigation:true, onClick:() => surface.back(() => detail({...event,id:event.series_id}))}] : []),
          ...(calendar?.writable ? [{label:'Delete', navigation:true, onClick:() => surface.back(() => calendarDelete(event,refresh,surface,() => detail(event),clicked,calendar))},
            {label:'Edit', primary:true, navigation:true, onClick:() => surface.back(() => calendarEdit(event,calendars,refresh,new Date(),null,surface,() => detail(event),clicked))}] : [])]});
      } catch (error) {
        if (surface.current() && generation === detailGeneration) await surface.show({title:'Event',body:`<p class="calendar-detail-error" role="alert">${esc(error.message)}</p>`,onBack:() => surface.close(),actions:[{label:'Retry',onClick:() => detail(requested)}]});
      }
    };
    await detail(event);
  });
}

// Personal status editor inside the Focus detail; returns its store unsubscribe.
function calendarStatusEditor(el, target, surface, clicked, calendar) {
  const pills = Array.from(el.querySelectorAll('[data-calendar-status-choice]')), note = $('.calendar-status-note',el);
  let displayed = calendarStatuses.get(target), busy = false, lastPayload, requestId, attempt;
  const status = () => displayed?.status || 'pending', revision = () => displayed?.revision || 0;
  // No draft exists, so the editor always follows the confirmed store value.
  const adopt = () => {
    const {known,denied} = calendarStatuses.state, fresh = calendarStatuses.get(target);
    if (!known || denied || (fresh?.revision || 0) === revision()) return;
    displayed = fresh; note.textContent = '';
  };
  const sync = () => {
    const {known,denied,error} = calendarStatuses.state, usable = known && !denied;
    if (usable && !busy) adopt();
    if (usable && note.textContent.startsWith('Status unknown')) note.textContent = '';
    if (!usable) { for (const pill of pills) { pill.disabled = true; pill.setAttribute('aria-pressed','false'); } note.textContent = denied ? error : `Status unknown. ${error || 'Not loaded yet.'}`; ensureRetry(); return; }
    $('[data-calendar-status-retry]',el)?.remove();
    for (const pill of pills) { pill.disabled = busy; pill.setAttribute('aria-pressed',String(pill.dataset.calendarStatusChoice === status())); }
  };
  const ensureRetry = () => {
    if (calendarStatuses.state.denied || $('[data-calendar-status-retry]',el)) return;
    const retry = document.createElement('button'); retry.type = 'button'; retry.className = 'ghost'; retry.textContent = 'Retry'; retry.dataset.calendarStatusRetry = '';
    retry.onclick = async () => { retry.disabled = true; await calendarStatuses.refresh(); retry.disabled = false; sync(); };
    note.after(retry);
  };
  const choose = async chosen => {
    const {known,denied} = calendarStatuses.state;
    if (busy || !known || denied || chosen === status()) return;
    const payload = JSON.stringify([target,chosen,revision()]);
    if (payload !== lastPayload) { requestId = newRequestId(); lastPayload = payload; attempt = calendarAttempt('personal_status',{target,status:chosen,revision:revision()},requestId,clicked,calendar); }
    busy = true; surface.setBusy(true); note.textContent = 'Saving…'; sync();
    try {
      if (!await calendarSubmitAttempt(attempt,null,surface)) { note.textContent = 'Retry cancelled; the outcome remains unconfirmed.'; return; } displayed = calendarStatuses.get(target);
      note.textContent = `Saved: ${calendarStatusLabels[displayed.status]}.`;
    } catch (error) {
      if (calendarStatuses.state.denied) note.textContent = calendarStatuses.state.error;
      else if (calendarStatusConflict(error)) {
        await calendarStatuses.refresh();
        displayed = calendarStatuses.get(target);
        note.textContent = calendarStatuses.state.known && !calendarStatuses.state.error ? `Changed elsewhere. Now ${calendarStatusLabels[status()]}; choose again to change it.` : 'Changed elsewhere and the current status couldn’t be loaded. Retry.';
      } else {
        const definite = error.httpStatus >= 400 && error.httpStatus < 500 && ![408,429].includes(error.httpStatus) && error.code !== 'invalid_response';
        note.textContent = definite ? `Not saved: ${error.message} Click the status again to retry.` : `Couldn’t confirm the save. ${error.message || ''} Click the status again to retry.`;
      }
    } finally { busy = false; surface.setBusy(false); sync(); }
  };
  for (const pill of pills) pill.onclick = () => choose(pill.dataset.calendarStatusChoice);
  sync();
  return calendarStatuses.subscribe(() => { if (!busy) sync(); });
}

function calendarDelete(event, refresh, surface, back, clicked = event, calendar = null) {
  const key = newRequestId(), attempt = calendarAttempt('calendar_event_delete',calendarTarget(event),key,clicked,calendar);
  return surface.show({title:'Delete event?',onBack:() => surface.close(),body:`<p>${esc(event.title)}${event.recurrence.length ? ' (whole series)' : ''}</p><p>Guests receive the provider’s cancellation notice.</p><p class="calendar-detail-error" role="alert"></p>`,actions:[
    {label:'Cancel',navigation:true,onClick:() => surface.back(back)}, {label:'Delete',primary:true,onClick:async (close,el) => {
      const button = $('.mfoot .primary',el); button.disabled = true;
      try { if (await calendarSubmitAttempt(attempt,refresh,surface)) close(); else button.disabled = false; }
      catch (error) { $('.calendar-detail-error',el).textContent = error.message; button.disabled = false; }
    }}]});
}

function calendarEdit(event, calendars, refresh, calendarDate = new Date(), origin = document.activeElement, surface = null, back = null, clicked = event) {
  if (!surface) return calendarSurface(event ? 'Edit event' : 'Add event',origin, current => calendarEdit(event,calendars,refresh,calendarDate,origin,current,() => current.close()));
  const writable = calendars.filter(c => c.writable);
  const active = event ? calendars.find(c => c.id === event.calendar_id && c.connection_id === event.connection_id) : writable[0];
  const start = event?.start || sjInstant(calendarDateString(calendarDate),10).toISOString();
  const end = event?.end || new Date(new Date(start).getTime()+30*60000).toISOString();
  const repeatOptions = [['','Does not repeat'],['RRULE:FREQ=DAILY','Every day'],['RRULE:FREQ=WEEKLY','Every week'],['RRULE:FREQ=MONTHLY','Every month']];
  const recurrence = event?.recurrence.join('\n') || '';
  if (!repeatOptions.some(([value]) => value === recurrence)) repeatOptions.push([recurrence,'Current repeat pattern']);
  const reminder = event?.default_reminders || !event ? 'default' : event.reminders.join(',');
  const reminderOptions = [['default','Calendar default'],['','None'],['5','5 minutes before'],['10','10 minutes before'],['30','30 minutes before'],['60','1 hour before'],['1440','1 day before']];
  if (event && !event.default_reminders) reminderOptions.shift();
  if (!reminderOptions.some(([value]) => value === reminder)) reminderOptions.push([reminder,'Current reminders']);
  const selection = c => calendars.indexOf(c);
  let initial, lastPayload, key, attempt;
  return surface.show({title:event ? 'Edit event' : 'Add event',onBack:() => surface.close(),body:`<form class="calendar-form">
    <label>Calendar<select class="inp" aria-label="Calendar" name="calendar" ${event ? 'disabled' : ''}>${writable.map(c => `<option value="${selection(c)}" ${c === active ? 'selected' : ''}>${esc(c.name)} (${esc(c.provider)})</option>`).join('')}</select></label>
    <label>Invitation from<select class="inp" aria-label="Invitation from" name="organizer" ${event ? 'disabled' : ''}></select></label>
    <label>Title<input class="inp" name="title" required maxlength="1000" value="${esc(event?.title || '')}"></label>
    <label class="calendar-inline"><input type="checkbox" name="allDay" ${start.length === 10 ? 'checked' : ''}> All day</label>
    <div class="calendar-form-pair"><label>Start<input class="inp" name="start" required type="${start.length === 10 ? 'date' : 'datetime-local'}" value="${calendarLocalInput(start)}"></label>
    <label>End <small>(all-day end date excluded)</small><input class="inp" name="end" required type="${start.length === 10 ? 'date' : 'datetime-local'}" value="${calendarLocalInput(end)}"></label></div>
    <label>Location or meeting link<input class="inp" name="location" value="${esc(event?.location || '')}"></label>
    <label>Guests<input class="inp" name="attendees" placeholder="Email addresses, separated by commas" value="${esc(event?.attendees.map(a => a.email).join(', ') || '')}"></label>
    <div class="calendar-form-pair"><label>Repeat<select class="inp" aria-label="Repeat" name="recurrence">${repeatOptions.map(([value,label]) => `<option value="${esc(value)}" ${value === recurrence ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
    <label>Reminder<select class="inp" aria-label="Reminder" name="reminders">${reminderOptions.map(([value,label]) => `<option value="${esc(value)}" ${value === reminder ? 'selected' : ''}>${label}</option>`).join('')}</select></label></div>
    <label class="calendar-inline" data-meet-option><input type="checkbox" name="meet"> Add Google Meet link</label>
    <label>Description<textarea class="inp" name="description">${esc(event?.description || '')}</textarea></label>
    <p class="calendar-detail-error" role="alert"></p><button type="button" class="ghost" data-calendar-review>Review unconfirmed changes</button></form>`, actions:[
    {label:'Cancel',navigation:true,onClick:() => surface.back(back)}, {label:'Save',primary:true,onClick:async (close,el) => {
      const form = $('form',el), f = form.elements;
      if (!form.reportValidity()) return;
      const values = Object.fromEntries(new FormData(form));
      const selected = event ? active : calendars[Number(f.calendar.value)];
      const changes = {};
      for (const name of ['title','location','description']) if (!event || values[name] !== initial[name]) changes[name] = values[name];
      if (!event || values.start !== initial.start || values.end !== initial.end || values.allDay !== initial.allDay) {
        try {
          for (const name of ['start','end']) {
            // Retain unchanged instants, including seconds and the second occurrence of a repeated DST minute.
            changes[name] = f.allDay.checked ? values[name] : event && values[name] === initial[name] && values.allDay === initial.allDay ? event[name] : calendarInputInstant(values[name]);
          }
          if (new Date(changes.end) <= new Date(changes.start)) throw new Error('Event end must follow start.');
        } catch (error) { $('.calendar-detail-error',el).textContent = error.message; return; }
        if (!f.allDay.checked) changes.timezone = event?.timezone || calendarZone();
      }
      if (!event || values.attendees !== initial.attendees) changes.attendees = values.attendees.split(',').map(s => s.trim()).filter(Boolean);
      if (!event || values.recurrence !== initial.recurrence) changes.recurrence = values.recurrence ? values.recurrence.split('\n') : [];
      if (changes.recurrence?.length && !f.allDay.checked && !changes.timezone) changes.timezone = event?.timezone || calendarZone();
      if ((!event || values.reminders !== initial.reminders) && values.reminders !== 'default') changes.reminders = values.reminders ? values.reminders.split(',').map(Number) : [];
      if (f.meet.checked && selected.provider === 'google') changes.google_meet = true;
      if (event && !Object.keys(changes).length) { surface.setDirty(() => false); close(); return; }
      const args = event ? {...calendarTarget(event),changes} : {connection_id:selected.connection_id, calendar_id:selected.id, organizer:f.organizer.value,...changes};
      const payload = JSON.stringify(args);
      let owner;
      try { owner = await calendarOwner(); } catch (error) { $('.calendar-detail-error',el).textContent = error.message; return; }
      if (!event || payload !== lastPayload) { key = newRequestId(); lastPayload = payload; attempt = calendarAttempt(event ? 'calendar_event_update' : 'calendar_event_create',args,key,clicked,selected); attempt.owner = owner; }
      const button = $('.mfoot .primary',el); button.disabled = true;
      try { if (await calendarSubmitAttempt(attempt,refresh,surface)) { surface.setDirty(() => false); close(); } else button.disabled = false; }
      catch (error) { $('.calendar-detail-error',el).textContent = error.message; button.disabled = false; }
    }}],onOpen:el => {
      const form = $('form',el), f = form.elements;
      $('[data-calendar-review]',el).onclick = () => surface.review();
      const update = () => {
        const selected = event ? active : calendars[Number(f.calendar.value)];
        f.organizer.innerHTML = (event ? [event.organizer] : selected.organizers).map(address => `<option>${esc(address)}</option>`).join('');
        $('[data-meet-option]',el).hidden = selected.provider !== 'google' || !!event?.meet_url;
        f.meet.checked = false;
      };
      update(); f.calendar.onchange = update;
      f.allDay.onchange = () => { for (const name of ['start','end']) { const old = f[name].value; f[name].type = f.allDay.checked ? 'date' : 'datetime-local'; f[name].value = f.allDay.checked ? old.slice(0,10) : old+'T10:00'; } };
      initial = Object.fromEntries(new FormData(form));
      surface.setDirty(() => JSON.stringify(Object.fromEntries(new FormData(form))) !== JSON.stringify(initial));
      form.onsubmit = e => { e.preventDefault(); $('.mfoot .primary',el).click(); };
    }});
}
