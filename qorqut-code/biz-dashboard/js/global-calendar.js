// One time grid over provider calendars; events stay in Google and Fastmail.
const calendarZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone;
const calendarDateString = date => `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
const calendarAddDays = (date, days) => { const next = new Date(date); next.setDate(next.getDate()+days); return next; };
const calendarTarget = event => ({connection_id:event.connection_id, calendar_id:event.calendar_id, event_id:event.id, revision:event.revision});
const calendarCall = async (operation, args = {}, key) => coreData(await post(`/root/operations/${operation}`, {...args, ...(key ? {idempotency_key:key} : {})}));
const calendarTime = date => date.toLocaleTimeString([], {hour:'numeric', minute:'2-digit'});
const calendarDateLabel = (date, now = new Date()) => {
  const key = calendarDateString(date);
  for (const [offset,label] of [[0,'Today'],[1,'Tomorrow'],[-1,'Yesterday']]) {
    if (key === calendarDateString(calendarAddDays(now,offset))) return label;
  }
  return date.toLocaleDateString('en-GB', {day:'numeric', month:'short'});
};
const calendarEventWhen = event => {
  if (event.start.length === 10) {
    const first = new Date(event.start+'T12:00:00'), last = calendarAddDays(new Date(event.end+'T12:00:00'),-1);
    return `${calendarDateLabel(first)}${event.start === calendarDateString(last) ? '' : ' – '+calendarDateLabel(last)} · All day`;
  }
  const start = new Date(event.start), end = new Date(event.end);
  return `${calendarDateLabel(start)} · ${calendarTime(start)} – ${calendarDateString(start) === calendarDateString(end) ? '' : calendarDateLabel(end)+' · '}${calendarTime(end)}`;
};
const calendarProviderClass = calendar => calendar?.provider === 'fastmail' ? 'calendar-business' : 'calendar-personal';
const calendarIcon = name => `<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${AICONS[name] || ''}</svg>`;

// Split a timed event at local midnight, retaining its position on each day's clock.
function calendarDaySpan(event, day) {
  if (!event.start || event.start.length === 10 || event.status === 'cancelled') return null;
  const start = new Date(event.start), end = new Date(event.end), next = calendarAddDays(day,1);
  if (!Number.isFinite(+start) || !Number.isFinite(+end) || end <= day || start >= next || end <= start) return null;
  const from = start < day ? 0 : start.getHours()*60 + start.getMinutes();
  const to = end >= next ? 1440 : end.getHours()*60 + end.getMinutes();
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

// Each pane owns its navigation and lifecycle; embedding never changes the route.
function calendarMount(host, {isCurrent = () => true, embedded = false} = {}) {
  let calendarDate = new Date(), calendarViewDays = embedded ? 1 : 3, calendarGeneration = 0, active = true;
  let calendarMonth = new Date(calendarDate.getFullYear(), calendarDate.getMonth(), 1), dispose = () => {};
  const render = async () => {
    dispose();
    const generation = ++calendarGeneration;
    const countForWidth = () => host.clientWidth <= 600 ? 1 : calendarViewDays;
    const count = countForWidth();
    const first = new Date(calendarDate.getFullYear(), calendarDate.getMonth(), calendarDate.getDate());
    const last = calendarAddDays(first,count), dates = Array.from({length:count},(_,i) => calendarAddDays(first,i));
    host.innerHTML = `<section class="pane calendar-page${embedded ? ' calendar-embedded' : ''}" aria-label="Calendar" style="--calendar-columns:${count}">
      <header class="calendar-header"><div class="calendar-brand"><h1>Calendar</h1><time class="calendar-clock" aria-label="Current time"></time></div>
        <div class="calendar-header-actions"><button type="button" class="calendar-icon" data-calendar-refresh aria-label="Refresh calendars">${calendarIcon('refresh-cw')}</button><button type="button" class="primary" data-calendar-add disabled><span aria-hidden="true">＋</span> Add event</button></div></header>
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
    const reload = () => { if (current()) return render(); };
    const chooseDate = date => { calendarDate = date; calendarMonth = new Date(date.getFullYear(),date.getMonth(),1); reload(); };
    const paintDates = () => {
      $('.calendar-range',page).textContent = count === 1 ? calendarDateLabel(first) : `${calendarDateLabel(first)} – ${calendarDateLabel(calendarAddDays(last,-1))}`;
      $('.calendar-dayheads',page).innerHTML = '<span></span>'+dates.map(date => `<div class="calendar-dayhead">${esc(date.toLocaleDateString('en-GB',{weekday:'short'}))}</div>`).join('');
      $('.calendar-month-heading h2',page).textContent = calendarMonth.toLocaleDateString('en-GB',{month:'long'});
      const offset = (calendarMonth.getDay()+6)%7, total = new Date(calendarMonth.getFullYear(),calendarMonth.getMonth()+1,0).getDate();
      const month = $('.calendar-month',page);
      month.innerHTML = ['M','T','W','T','F','S','S'].map(day => `<span class="calendar-weekday">${day}</span>`).join('') + (offset ? `<span style="grid-column:span ${offset}"></span>` : '') +
        Array.from({length:total},(_,i) => { const date = new Date(calendarMonth.getFullYear(),calendarMonth.getMonth(),i+1), key = calendarDateString(date);
          return `<button type="button" class="calendar-monthday ${date >= first && date < last ? 'in-range' : ''}" data-calendar-date="${key}" aria-label="${esc(calendarDateLabel(date))}" aria-pressed="${key === calendarDateString(first)}">${i+1}</button>`;
        }).join('');
      month.querySelectorAll('button').forEach(button => button.onclick = () => chooseDate(new Date(button.dataset.calendarDate+'T12:00:00')));
    };
    $('.calendar-view',page).value = String(calendarViewDays);
    $('.calendar-view',page).onchange = event => { calendarViewDays = Number(event.target.value); reload(); };
    $('[data-calendar-prev]',page).onclick = () => chooseDate(calendarAddDays(first,-count));
    $('[data-calendar-next]',page).onclick = () => chooseDate(last);
    $('[data-calendar-refresh]',page).onclick = reload;
    for (const [selector,offset] of [['[data-month-prev]',-1],['[data-month-next]',1]]) $(selector,page).onclick = () => {
      calendarMonth = new Date(calendarMonth.getFullYear(),calendarMonth.getMonth()+offset,1); paintDates();
    };
    paintDates();
    let clockDay = calendarDateString(new Date()), clockTimer, observer;
    const cleanup = () => { clearInterval(clockTimer); observer?.disconnect(); };
    dispose = cleanup;
    const tick = () => {
      if (!current()) { cleanup(); return; }
      const now = new Date(), clock = $('.calendar-clock',page); clock.dateTime = now.toISOString(); clock.textContent = calendarTime(now);
      const day = calendarDateString(now); if (day !== clockDay) { clockDay = day; paintDates(); }
    };
    tick(); clockTimer = setInterval(tick,1000);
    observer = new ResizeObserver(() => { if (!current()) cleanup(); else { sizePage(); if (countForWidth() !== count) reload(); } });
    observer.observe(host);

    const status = $('.calendar-status',page), allEvents = [], failures = new Map();
    let calendars = [], loading = true;
    const eventButton = (event,allDay) => calendarEventButton(event,calendars,reload,allDay);
    const paint = () => {
      if (!current()) return;
      status.textContent = [...failures.values()].join(' · ') || (loading ? 'Loading events…' : '');
      calendarPaintGrid(page,dates,allEvents,eventButton);
    };
    paint();
    try {
      const result = await calendarCall('calendar_calendars_list');
      if (!current()) return;
      calendars = result.calendars;
      result.errors.forEach(e => failures.set(e.connection_id,`${e.connection_id}: ${e.message}`));
      $('.calendar-sources > div',page).innerHTML = calendars.map(calendar => `<div class="calendar-source ${calendarProviderClass(calendar)}"><span class="calendar-dot"></span><span>${esc(calendar.name)} · ${esc(calendar.provider === 'google' ? 'Google' : calendar.provider === 'fastmail' ? 'Fastmail' : calendar.provider)}</span></div>`).join('');
      if (!result.configured) { status.textContent = 'Connect Google and Fastmail calendars to get started.'; return; }
      $('[data-calendar-add]',page).disabled = !calendars.some(c => c.writable);
      $('[data-calendar-add]',page).onclick = () => calendarEdit(null,calendars,reload,calendarDate);
      await Promise.all(calendars.map(async calendar => {
        let cursor, more;
        const failureKey = `${calendar.connection_id}:${calendar.id}`;
        const load = async () => {
          if (more) more.disabled = true;
          try {
            const result = await calendarCall('calendar_events_list',{connection_id:calendar.connection_id,calendar_id:calendar.id,
              start:first.toISOString(),end:last.toISOString(),limit:200,...(cursor ? {cursor} : {})});
            if (!current()) return;
            result.events.forEach(event => { const index = allEvents.findIndex(old => old.id === event.id && old.connection_id === event.connection_id && old.calendar_id === event.calendar_id); if (index < 0) allEvents.push(event); else allEvents[index] = event; });
            cursor = result.next_cursor; failures.delete(failureKey);
            if (!more) { more = document.createElement('button'); more.type = 'button'; more.onclick = load; $('.calendar-more',page).append(more); }
            more.textContent = `Load more from ${calendar.name}`; more.hidden = !cursor; more.disabled = false;
          } catch (error) { if (current()) { failures.set(failureKey,`${calendar.name}: ${error.message}`); if (more) more.disabled = false; } }
          paint();
        };
        await load();
      }));
      loading = false; paint();
    } catch (error) { if (current()) status.textContent = error.message; }
  };
  void render();
  return () => { active = false; dispose(); };
}

function calendarEventButton(event, calendars, refresh, allDay = false) {
  const calendar = calendars.find(c => c.id === event.calendar_id && c.connection_id === event.connection_id), pending = event.attendees?.some(a => calendar?.organizers?.some(email => email.toLowerCase() === a.email.toLowerCase()) && a.status === 'needsAction');
  const button = document.createElement('button'); button.type = 'button';
  button.className = `calendar-event ${calendarProviderClass(calendar)}${allDay ? ' calendar-all-day-event' : ''}`;
  const when = allDay ? 'All day' : `${calendarTime(new Date(event.start))} – ${calendarTime(new Date(event.end))}`;
  const label = `${event.title || 'Untitled event'} · ${when} · ${calendar?.name || ''}${pending ? ' · Invitation' : ''}`;
  button.setAttribute('aria-label',label); button.title = label;
  button.innerHTML = `<strong>${esc(event.title || 'Untitled event')}</strong>`;
  button.onclick = () => calendarOpen(event,calendars,refresh); return button;
}

function calendarPaintGrid(page, dates, allEvents, eventButton, hourHeight = 64) {
  page.style.setProperty('--calendar-hour',hourHeight+'px');
  const spans = dates.map(day => calendarEventLanes(allEvents.map(event => calendarDaySpan(event,day)).filter(Boolean)));
  const flat = spans.flat();
  const startHour = Math.min(8,...flat.map(item => Math.floor(item.start/60)));
  const endHour = Math.max(19,...flat.map(item => Math.ceil(item.end/60)));
  page.style.setProperty('--calendar-hours',endHour-startHour);
  const grid = $('.calendar-week',page);
  grid.innerHTML = `<div class="calendar-axis">${Array.from({length:endHour-startHour+1},(_,i) => { const hour = startHour+i, date = new Date(dates[0]); date.setHours(hour,0,0,0); return `<span class="calendar-tick" style="top:${i*hourHeight}px">${esc(calendarTime(date))}</span>`; }).join('')}</div>`;
  const allDayRow = $('.calendar-all-day',page); allDayRow.innerHTML = '<span class="calendar-all-day-label">All day</span>'; let hasAllDay = false;
  dates.forEach((date,index) => {
    const key = calendarDateString(date), column = document.createElement('section'); column.className = 'calendar-day'; column.dataset.calendarDay = key;
    column.setAttribute('aria-label',date.toLocaleDateString('en-GB',{weekday:'long',day:'numeric',month:'long'}));
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

// Compact event queue above Focus attention; the Calendar tab keeps its time grid.
function calendarTodayMount(host, openCalendar) {
  host.className = 'calendar-page calendar-today';
  host.setAttribute('aria-label', "Today's calendar");
  host.innerHTML = `<header class="calendar-today-heading"><button type="button" class="quiet" data-calendar-open aria-label="Open today's calendar"><span>Today</span><span aria-hidden="true">↗</span></button><time class="calendar-clock" aria-label="Current time"></time></header>
    <p class="calendar-status" role="status">Loading calendar…</p><ul class="calendar-today-queue" aria-label="Today's events" hidden></ul>`;
  $('[data-calendar-open]',host).onclick = openCalendar;
  const status = $('.calendar-status',host), queue = $('.calendar-today-queue',host);
  let active = true, pending = false, loadedDay, snapshot, timer;
  const current = () => active && host.isConnected;
  const refresh = async () => {
    if (!current() || pending) return;
    pending = true;
    const now = new Date(), day = new Date(now.getFullYear(),now.getMonth(),now.getDate()), key = calendarDateString(day);
    const clock = $('.calendar-clock',host); clock.dateTime = now.toISOString(); clock.textContent = calendarTime(now);
    try {
      const result = await calendarCall('calendar_calendars_list');
      if (!current()) return;
      const calendars = result.calendars, events = [], errors = result.errors.map(e => `${e.connection_id}: ${e.message}`);
      await Promise.all(calendars.map(async calendar => {
        let cursor;
        try {
          do {
            const page = await calendarCall('calendar_events_list',{connection_id:calendar.connection_id,calendar_id:calendar.id,
              start:day.toISOString(),end:calendarAddDays(day,1).toISOString(),limit:200,...(cursor ? {cursor} : {})});
            if (!current()) return;
            events.push(...page.events); cursor = page.next_cursor;
          } while (cursor);
        } catch (error) { errors.push(`${calendar.name}: ${error.message}`); }
      }));
      if (!current()) return;
      const visibleEvents = events.filter(event => event.status !== 'cancelled');
      visibleEvents.sort((a,b) => {
        const allDayA = a.start.length === 10, allDayB = b.start.length === 10;
        return Number(allDayB)-Number(allDayA) || (allDayA ? a.start.localeCompare(b.start) : new Date(a.start)-new Date(b.start)) || a.id.localeCompare(b.id);
      });
      status.textContent = errors.join(' · ') || (!result.configured ? 'Connect your calendars to see today.' : visibleEvents.length ? '' : 'No events today.');
      queue.hidden = !result.configured || !visibleEvents.length;
      queue.dataset.calendarDay = key;
      const nextSnapshot = JSON.stringify([key,calendars,visibleEvents]);
      if (snapshot !== nextSnapshot) {
        const left = loadedDay === key ? queue.scrollLeft : 0;
        queue.replaceChildren(...visibleEvents.map(event => {
          const allDay = event.start.length === 10;
          const item = document.createElement('li'), button = calendarEventButton(event,calendars,refresh,allDay);
          button.classList.add('calendar-queue-event');
          const time = document.createElement('span');
          time.textContent = allDay ? 'All day' : calendarTime(new Date(event.start));
          button.prepend(time); item.append(button); return item;
        }));
        queue.scrollLeft = left;
        loadedDay = key; snapshot = nextSnapshot;
      }
    } catch (error) { if (current()) status.textContent = error.message; }
    finally { pending = false; }
  };
  const dispose = () => { active = false; clearInterval(timer); document.removeEventListener('visibilitychange',visible); };
  timer = setInterval(() => { if (!current()) dispose(); else if (!document.hidden) void refresh(); },60000);
  const visible = () => { if (!document.hidden) void refresh(); };
  document.addEventListener('visibilitychange',visible);
  void refresh();
  return {refresh,dispose};
}

async function calendarOpen(event, calendars, refresh) {
  try {
    const result = await calendarCall('calendar_event_get', {connection_id:event.connection_id, calendar_id:event.calendar_id, event_id:event.id});
    event = result.event;
    const calendar = calendars.find(c => c.id === event.calendar_id && c.connection_id === event.connection_id);
    const attendee = event.attendees.find(a => calendar?.organizers?.some(address => address.toLowerCase() === a.email.toLowerCase()));
    const recurring = !!event.series_id || event.recurrence.length > 0;
    const responseActions = attendee ? ['accepted','tentative','declined'].map(response => { const key = newRequestId(); return ({
      label:({accepted:'Accept',tentative:'Maybe',declined:'Decline'})[response], onClick:async (close, el) => {
        el.querySelectorAll('.mfoot button').forEach(button => button.disabled = true);
        try { await calendarCall('calendar_event_respond', {...calendarTarget(event), attendee:attendee.email, response}, key); close(); refresh(); }
        catch (error) { $('.calendar-detail-error',el).textContent = error.message; el.querySelectorAll('.mfoot button').forEach(button => button.disabled = false); }
      }}); }) : [];
    modal({title:esc(event.title || 'Event'), width:600, body:`<div class="calendar-detail">
      <p>${esc(calendarEventWhen(event))}</p><p>${esc(calendar?.name || '')} · ${esc(event.organizer || '')}</p>
      ${recurring ? `<p class="muted">${calendar?.provider === 'fastmail' || event.recurrence.length ? 'Recurring series. Changes here apply to the series.' : 'Occurrence of a recurring event.'}</p>` : ''}
      ${event.location ? `<p>${esc(event.location)}</p>` : ''}
      ${event.meet_url && /^https:\/\/meet\.google\.com\//.test(event.meet_url) ? `<p><a href="${esc(event.meet_url)}" target="_blank" rel="noopener">Join Google Meet</a></p>` : ''}
      ${event.description ? `<p class="calendar-description">${esc(event.description)}</p>` : ''}
      ${event.attendees.map(a => `<p>${esc(a.email)} · ${esc(a.status)}</p>`).join('')}
      <p class="calendar-detail-error" role="alert"></p></div>`, actions:[
      {label:'Close', onClick:close => close()}, ...responseActions,
      ...(event.series_id && event.series_id !== event.id ? [{label:'Open series', onClick:close => { close(); calendarOpen({...event,id:event.series_id},calendars,refresh); }}] : []),
      ...(calendar?.writable ? [{label:'Delete', onClick:close => { close(); calendarDelete(event,refresh); }},
        {label:'Edit', primary:true, onClick:close => { close(); calendarEdit(event,calendars,refresh); }}] : [])]});
  } catch (error) { toast(error.message); }
}

function calendarDelete(event, refresh) {
  const key = newRequestId();
  modal({title:'Delete event?',body:`<p>${esc(event.title)}${event.recurrence.length ? ' (whole series)' : ''}</p><p>Guests receive the provider’s cancellation notice.</p><p class="calendar-detail-error" role="alert"></p>`,actions:[
    {label:'Cancel',onClick:close => close()}, {label:'Delete',primary:true,onClick:async (close,el) => {
      const button = $('.mfoot .primary',el); button.disabled = true;
      try { await calendarCall('calendar_event_delete',calendarTarget(event),key); close(); refresh(); }
      catch (error) { $('.calendar-detail-error',el).textContent = error.message; button.disabled = false; }
    }}]});
}

function calendarEdit(event, calendars, refresh, calendarDate = new Date()) {
  const writable = calendars.filter(c => c.writable);
  const active = event ? calendars.find(c => c.id === event.calendar_id && c.connection_id === event.connection_id) : writable[0];
  const localInput = value => value?.length === 10 ? value : value ? calendarDateString(new Date(value))+'T'+new Date(value).toTimeString().slice(0,5) : '';
  const start = event?.start || new Date(calendarDate.getFullYear(), calendarDate.getMonth(),calendarDate.getDate(),10).toISOString();
  const end = event?.end || new Date(new Date(start).getTime()+30*60000).toISOString();
  const repeatOptions = [['','Does not repeat'],['RRULE:FREQ=DAILY','Every day'],['RRULE:FREQ=WEEKLY','Every week'],['RRULE:FREQ=MONTHLY','Every month']];
  const recurrence = event?.recurrence.join('\n') || '';
  if (!repeatOptions.some(([value]) => value === recurrence)) repeatOptions.push([recurrence,'Current repeat pattern']);
  const reminder = event?.default_reminders || !event ? 'default' : event.reminders.join(',');
  const reminderOptions = [['default','Calendar default'],['','None'],['5','5 minutes before'],['10','10 minutes before'],['30','30 minutes before'],['60','1 hour before'],['1440','1 day before']];
  if (event && !event.default_reminders) reminderOptions.shift();
  if (!reminderOptions.some(([value]) => value === reminder)) reminderOptions.push([reminder,'Current reminders']);
  const selection = c => calendars.indexOf(c);
  let initial, lastPayload, key;
  modal({title:event ? 'Edit event' : 'Add event',width:620,body:`<form class="calendar-form">
    <label>Calendar<select class="inp" aria-label="Calendar" name="calendar" ${event ? 'disabled' : ''}>${writable.map(c => `<option value="${selection(c)}" ${c === active ? 'selected' : ''}>${esc(c.name)} (${esc(c.provider)})</option>`).join('')}</select></label>
    <label>Invitation from<select class="inp" aria-label="Invitation from" name="organizer" ${event ? 'disabled' : ''}></select></label>
    <label>Title<input class="inp" name="title" required maxlength="1000" value="${esc(event?.title || '')}"></label>
    <label class="calendar-inline"><input type="checkbox" name="allDay" ${start.length === 10 ? 'checked' : ''}> All day</label>
    <div class="calendar-form-pair"><label>Start<input class="inp" name="start" required type="${start.length === 10 ? 'date' : 'datetime-local'}" value="${localInput(start)}"></label>
    <label>End <small>(all-day end date excluded)</small><input class="inp" name="end" required type="${start.length === 10 ? 'date' : 'datetime-local'}" value="${localInput(end)}"></label></div>
    <label>Location or meeting link<input class="inp" name="location" value="${esc(event?.location || '')}"></label>
    <label>Guests<input class="inp" name="attendees" placeholder="Email addresses, separated by commas" value="${esc(event?.attendees.map(a => a.email).join(', ') || '')}"></label>
    <div class="calendar-form-pair"><label>Repeat<select class="inp" aria-label="Repeat" name="recurrence">${repeatOptions.map(([value,label]) => `<option value="${esc(value)}" ${value === recurrence ? 'selected' : ''}>${label}</option>`).join('')}</select></label>
    <label>Reminder<select class="inp" aria-label="Reminder" name="reminders">${reminderOptions.map(([value,label]) => `<option value="${esc(value)}" ${value === reminder ? 'selected' : ''}>${label}</option>`).join('')}</select></label></div>
    <label class="calendar-inline" data-meet-option><input type="checkbox" name="meet"> Add Google Meet link</label>
    <label>Description<textarea class="inp" name="description">${esc(event?.description || '')}</textarea></label>
    <p class="calendar-detail-error" role="alert"></p></form>`, actions:[
    {label:'Cancel',onClick:close => close()}, {label:'Save',primary:true,onClick:async (close,el) => {
      const form = $('form',el), f = form.elements;
      if (!form.reportValidity()) return;
      const values = Object.fromEntries(new FormData(form));
      const selected = event ? active : calendars[Number(f.calendar.value)];
      const changes = {};
      for (const name of ['title','location','description']) if (!event || values[name] !== initial[name]) changes[name] = values[name];
      if (!event || values.start !== initial.start || values.end !== initial.end || values.allDay !== initial.allDay) {
        changes.start = f.allDay.checked ? values.start : new Date(values.start).toISOString();
        changes.end = f.allDay.checked ? values.end : new Date(values.end).toISOString();
        if (!f.allDay.checked) changes.timezone = calendarZone();
      }
      if (!event || values.attendees !== initial.attendees) changes.attendees = values.attendees.split(',').map(s => s.trim()).filter(Boolean);
      if (!event || values.recurrence !== initial.recurrence) changes.recurrence = values.recurrence ? values.recurrence.split('\n') : [];
      if (changes.recurrence?.length && !f.allDay.checked && !changes.timezone) changes.timezone = event?.timezone || calendarZone();
      if ((!event || values.reminders !== initial.reminders) && values.reminders !== 'default') changes.reminders = values.reminders ? values.reminders.split(',').map(Number) : [];
      if (f.meet.checked && selected.provider === 'google') changes.google_meet = true;
      if (event && !Object.keys(changes).length) { close(); return; }
      const args = event ? {...calendarTarget(event),changes} : {connection_id:selected.connection_id, calendar_id:selected.id, organizer:f.organizer.value,...changes};
      const payload = JSON.stringify(args); if (payload !== lastPayload) { key = newRequestId(); lastPayload = payload; }
      const button = $('.mfoot .primary',el); button.disabled = true;
      try { await calendarCall(event ? 'calendar_event_update' : 'calendar_event_create',args,key); close(); refresh(); }
      catch (error) { $('.calendar-detail-error',el).textContent = error.message; button.disabled = false; }
    }}],onOpen:el => {
      const form = $('form',el), f = form.elements;
      const update = () => {
        const selected = event ? active : calendars[Number(f.calendar.value)];
        f.organizer.innerHTML = (event ? [event.organizer] : selected.organizers).map(address => `<option>${esc(address)}</option>`).join('');
        $('[data-meet-option]',el).hidden = selected.provider !== 'google' || !!event?.meet_url;
        f.meet.checked = false;
      };
      update(); f.calendar.onchange = update;
      f.allDay.onchange = () => { for (const name of ['start','end']) { const old = f[name].value; f[name].type = f.allDay.checked ? 'date' : 'datetime-local'; f[name].value = f.allDay.checked ? old.slice(0,10) : old+'T10:00'; } };
      initial = Object.fromEntries(new FormData(form));
      form.onsubmit = e => { e.preventDefault(); $('.mfoot .primary',el).click(); };
    }});
}
