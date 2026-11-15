'use strict';
// The owner's own messages on an action timeline. They are read through owner_inputs_list and drawn as their own rows,
// with a speech-bubble mark instead of the action dot. "Then" lines are what the records say followed a message (the
// reply and record changes before the owner's next message there): matched by time and place, not by meaning.
const OWNER_TIMELINE_ICON = '<svg viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linejoin="round" aria-hidden="true"><path d="M3 3.5h10a1 1 0 0 1 1 1v6a1 1 0 0 1-1 1H8l-3 2.5v-2.5H3a1 1 0 0 1-1-1v-6a1 1 0 0 1 1-1Z"/></svg>';
const OWNER_TIMELINE_KEY = 'qorqut.timeline.ownerMessages';
const OWNER_TIMELINE_GROUP_MS = 10 * 60000, OWNER_TIMELINE_PAGES = 5, OWNER_TIMELINE_THEN = 3;

// Showing your messages is a per-browser convenience; it is on unless turned off here.
function ownerTimelineShown() {
  try { return localStorage.getItem(OWNER_TIMELINE_KEY) !== 'off'; } catch (_) { return true; }
}
function ownerTimelineSetShown(shown) {
  try { localStorage.setItem(OWNER_TIMELINE_KEY, shown ? 'on' : 'off'); } catch (_) { /* still applies to this page */ }
  ownerTimelineOverride = shown;
  document.dispatchEvent(new CustomEvent('owner-timeline-toggle', {detail: {shown}}));
}
let ownerTimelineOverride = null;
function ownerTimelineIsShown() { return ownerTimelineOverride ?? ownerTimelineShown(); }
function ownerTimelineToggleHtml() {
  const shown = ownerTimelineIsShown();
  return `<button type="button" class="owner-timeline-toggle" role="switch" aria-checked="${shown}" data-owner-timeline-toggle>
    <span class="owner-timeline-switch" aria-hidden="true"></span>Your messages</button>`;
}
// One delegated click handler serves every switch on the page.
document.addEventListener('click', event => {
  const button = event.target.closest?.('[data-owner-timeline-toggle]');
  if (button) ownerTimelineSetShown(!ownerTimelineIsShown());
});
document.addEventListener('owner-timeline-toggle', event => {
  for (const button of document.querySelectorAll('[data-owner-timeline-toggle]')) button.setAttribute('aria-checked', String(event.detail.shown));
});

// Every message in [since, until], oldest first. `agentId` limits chat messages to one agent's chat.
async function ownerTimelineLoad({since, until, agentId, signal} = {}) {
  const items = [], cursors = new Set();
  let cursor = null, pages = 0;
  do {
    const data = coreData(await post('/root/operations/owner_inputs_list', {scope: 'global', since, until, limit: 100, text_limit: 600,
      ...(agentId ? {agent_id: agentId} : {}), ...(cursor ? {cursor} : {})}, signal ? {signal} : undefined));
    if (!Array.isArray(data.items)) throw Error('Invalid messages response');
    items.push(...data.items);
    cursor = data.next_cursor || null;
    if (cursor && cursors.has(cursor)) throw Error('Message pagination did not advance.');
    if (cursor) cursors.add(cursor);
  } while (cursor && ++pages < OWNER_TIMELINE_PAGES);
  return items;
}

// When an action happened, for ordering beside messages.
function ownerTimelineActionAt(action) {
  const o = action.occurred || {};
  if (o.precision === 'instant' && o.instant) return Date.parse(o.instant);
  if (o.date && typeof sjInstant === 'function') return sjInstant(o.date, o.precision === 'hour' ? o.hour : 0).getTime();
  return Date.parse(action.recorded_at || action.created_at || '') || 0;
}
function ownerTimelinePlace(item) {
  if (item.channel === 'chat') {
    if (item.via === 'telegram') return 'on Telegram';
    return item.agent?.role === 'ordubashy' || !item.agent?.name ? 'in Chat' : `in ${item.agent.name}'s chat`;
  }
  const title = item.source?.title;
  if (item.channel === 'note') return title ? `note on “${title}”` : 'note';
  return item.source?.type === 'signoff' && title ? `in “${title}”` : title ? `on “${title}”` : 'in a thread';
}
function ownerTimelinePlaceKey(item) {
  return item.channel === 'chat' ? `chat:${item.agent?.id || ''}:${item.via || ''}` : `${item.channel}:${item.source?.type}:${item.source?.id}`;
}
function ownerTimelineTime(ms) {
  const at = new Date(ms).toISOString(), day = sjParts(at).date, today = sjToday();
  const label = day === today ? 'Today' : day === sjAddDays(today, -1) ? 'Yesterday' : sjDayLabel(day, {year: 'numeric'});
  return `${label} · ${sjTime(at)}`;
}

// Actions and messages, newest first. A run of messages to one place, with no action between and each within ten minutes
// of the next, is one entry.
function ownerTimelineMerge(actions, messages) {
  const entries = actions.map(action => ({kind: 'action', at: ownerTimelineActionAt(action), action}));
  const sorted = [...messages].map(item => ({item, at: Date.parse(item.at) || 0})).sort((a, b) => a.at - b.at);
  const actionTimes = entries.map(entry => entry.at).sort((a, b) => a - b);
  let group = null;
  for (const {item, at} of sorted) {
    const between = group && actionTimes.some(time => time > group.last && time < at);
    if (group && !between && ownerTimelinePlaceKey(item) === group.place && at - group.last <= OWNER_TIMELINE_GROUP_MS) {
      group.items.push(item); group.last = at; continue;
    }
    group = {kind: 'messages', at, last: at, place: ownerTimelinePlaceKey(item), items: [item]};
    entries.push(group);
  }
  // Ties keep a message above the action it led to.
  return entries.sort((a, b) => b.at - a.at || (a.kind === 'messages' ? -1 : 1));
}

function ownerTimelineQuote(item) {
  return item.redacted ? '<span class="owner-timeline-private">Private message</span>' : `“${esc(item.text || '')}${item.truncated ? '…' : ''}”`;
}
// What followed any message of the entry, oldest first.
function ownerTimelineThen(items) {
  const lines = [];
  let more = 0;
  for (const item of items) {
    const followed = item.followed_by || {};
    if (followed.reply) lines.push({at: followed.reply.at, text: `Replied: ${followed.reply.preview || ''}`});
    for (const record of followed.records || []) lines.push({at: record.at, text: record.change || 'Record changed'});
    more += followed.more_records || 0;
  }
  lines.sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  const extra = Math.max(0, lines.length - OWNER_TIMELINE_THEN) + more;
  return lines.length ? `<div class="owner-timeline-then">${lines.slice(0, OWNER_TIMELINE_THEN).map(line =>
    `<p><span>Then ${esc(sjTime(line.at))}</span> ${esc(line.text)}</p>`).join('')}${extra > 0 ? `<p><span>+ ${extra} more</span></p>` : ''}</div>` : '';
}
function ownerTimelineRow(entry) {
  const [first, ...rest] = entry.items;
  const more = rest.length ? `<details class="owner-timeline-more"><summary>+ ${rest.length} more message${rest.length === 1 ? '' : 's'}</summary>
    ${rest.map(item => `<p class="owner-timeline-text">${ownerTimelineQuote(item)}</p>`).join('')}</details>` : '';
  return `<li class="owner-timeline-row" data-owner-message="${esc(first.id)}">
    <span class="owner-timeline-icon">${OWNER_TIMELINE_ICON}</span>
    <time datetime="${esc(first.at)}">${esc(ownerTimelineTime(entry.at))} · You · ${esc(ownerTimelinePlace(first))}</time>
    <p class="owner-timeline-text" title="${esc(first.redacted ? '' : first.text || '')}">${ownerTimelineQuote(first)}</p>
    ${more}${ownerTimelineThen(entry.items)}
  </li>`;
}

// One action row of a compact timeline (the navigation report's style).
function ownerTimelineActionRow(action) {
  const attribution = action.attribution === 'recorded' ? `<small>Recorded by ${esc(action.recorded_by?.name || 'unknown')}</small>` : '';
  return `<li><time>${esc(acOccurred(action))}</time><p>${esc(action.text || action.summary || 'Recorded action')}</p>${attribution}</li>`;
}

// The daily report's own timeline: every recorded action and your messages in the 24 hours before the report was posted.
// It is read once, when the report is first shown; it never changes the report.
const OWNER_TIMELINE_REPORT_HOURS = 24;
async function ownerTimelineReportMount(host) {
  if (!host || host.dataset.mounted) return;
  host.dataset.mounted = '1';
  const end = Date.parse(host.dataset.until || ''), start = end - OWNER_TIMELINE_REPORT_HOURS * 3600000;
  host.innerHTML = `<div class="owner-timeline-report-head"><h3>Timeline</h3>${ownerTimelineToggleHtml()}</div>
    <p class="owner-timeline-report-status" role="status">Loading the timeline…</p><ol class="agent-nav-events owner-timeline-report-list"></ol>`;
  const status = host.querySelector('[role="status"]'), list = host.querySelector('ol');
  if (!Number.isFinite(end)) { status.textContent = 'The timeline for this report is unavailable.'; return; }
  let actions = [], messages = [], messagesFailed = false;
  const paint = () => {
    const shown = ownerTimelineIsShown() ? messages : [];
    const entries = ownerTimelineMerge(actions, shown);
    list.innerHTML = entries.map(entry => entry.kind === 'action' ? ownerTimelineActionRow(entry.action) : ownerTimelineRow(entry)).join('');
    status.textContent = entries.length ? (messagesFailed && ownerTimelineIsShown() ? 'Your messages are unavailable.' : '')
      : 'Nothing was recorded in the 24 hours before this report.';
    status.hidden = !status.textContent;
  };
  const toggled = () => { if (!host.isConnected) { document.removeEventListener('owner-timeline-toggle', toggled); return; } paint(); };
  document.addEventListener('owner-timeline-toggle', toggled);
  const since = new Date(start).toISOString(), until = new Date(end).toISOString();
  const readActions = async () => {
    const found = new Map(), cursors = new Set();
    let cursor = null, pages = 0;
    do {
      const data = coreData(await post('/root/operations/actions_list', {since: sjParts(since).date, until: sjParts(until).date, limit: 200, ...(cursor ? {cursor} : {})}));
      if (!Array.isArray(data.actions)) throw Error('Invalid actions response');
      for (const action of data.actions) found.set(action.action_id, action);
      cursor = data.next_cursor || null;
      if (cursor && cursors.has(cursor)) throw Error('Action pagination did not advance.');
      if (cursor) cursors.add(cursor);
    } while (cursor && ++pages < 10);
    return [...found.values()].filter(action => { const at = ownerTimelineActionAt(action); return at >= start && at <= end; });
  };
  const [actionResult, messageResult] = await Promise.allSettled([readActions(), ownerTimelineLoad({since, until})]);
  if (!host.isConnected) return;
  if (actionResult.status === 'rejected') { status.textContent = 'The timeline for this report could not be loaded.'; host.dataset.mounted = ''; return; }
  actions = actionResult.value;
  if (messageResult.status === 'fulfilled') messages = messageResult.value; else messagesFailed = true;
  paint();
}
