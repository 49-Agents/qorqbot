// The timeline of a Task, a Project or Global: goal, decisions, facts, latest progress and open questions, with a History
// switch that shows every entry (removed ones struck, the owner's quoted words, links to subtasks, approvals and reports).
// Read-only: only Ordubashy writes timelines. An item with no timeline shows nothing.
const TIMELINE_MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
const TIMELINE_KINDS = {goal: 'Goal', decision: 'Decision', fact: 'Fact', progress: 'Progress'};
const timelineEsc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[ch]));
function timelineDay(stamp) {
  const match = /^\d{4}-(\d\d)-(\d\d)/.exec(String(stamp || ''));
  return match && TIMELINE_MONTHS[Number(match[1]) - 1] ? `${Number(match[2])} ${TIMELINE_MONTHS[Number(match[1]) - 1]}` : '';
}
// True when nothing is current and nothing was ever filed.
function timelineEmpty(data) {
  const s = data?.state;
  return !s || !(s.goal || s.decisions?.length || s.facts?.length || s.progress || s.questions?.length || data.revision > 0);
}
const timelineStanding = state => !!(state.goal || state.decisions?.length || state.facts?.length || state.progress || state.questions?.length);
const timelineLine = row => `<li>${timelineEsc(row.text)}${timelineDay(row.created_at) ? ` <span class="tlp-day">${timelineEsc(timelineDay(row.created_at))}</span>` : ''}</li>`;
function timelineStateHtml(state) {
  if (!timelineStanding(state)) return '<p class="tlp-none">Nothing current.</p>';
  const list = (label, rows) => rows?.length ? `<section class="tlp-block"><h4>${label}</h4><ul>${rows.map(timelineLine).join('')}</ul></section>` : '';
  const one = (label, row) => row ? `<section class="tlp-block"><h4>${label}</h4><p>${timelineEsc(row.text)}${timelineDay(row.created_at) && label === 'Progress' ? ` <span class="tlp-day">${timelineEsc(timelineDay(row.created_at))}</span>` : ''}</p></section>` : '';
  const questions = state.questions?.length ? `<section class="tlp-block"><h4>Open questions</h4><ul>${state.questions.map(q =>
    `<li class="${q.blocking ? 'tlp-blocking' : ''}">${q.blocking ? '<span class="tlp-flag">Blocks work</span> ' : ''}${timelineEsc(q.text)}</li>`).join('')}</ul></section>` : '';
  return one('Goal', state.goal) + list('Decisions', state.decisions) + list('Facts', state.facts) + one('Progress', state.progress) + questions;
}
function timelineHistoryHtml(history) {
  const entries = history?.entries || [], byId = new Map(entries.map(row => [row.id, row]));
  const quotesOf = row => (row.sources || []).map(source => `<blockquote class="tlp-quote">${timelineEsc(source.quote)}</blockquote>`).join('');
  // A removal whose target is loaded is shown under that target; its quoted words go with it.
  const folded = row => row.action === 'remove' && byId.has(row.removes_id);
  const rows = entries.filter(row => !folded(row)).map(row => {
    const quotes = quotesOf(row);
    if (row.action === 'remove')
      return `<li class="tlp-removal"><span class="tlp-kind">Removed</span> an earlier entry${row.text ? `: ${timelineEsc(row.text)}` : ''} <span class="tlp-day">${timelineEsc(timelineDay(row.created_at))}</span>${quotes}</li>`;
    // ``removed_by`` is the server's answer for the whole item; the removal entry itself may not be loaded yet.
    const removed = !!row.removed_by, by = removed && byId.get(row.removed_by);
    const gone = removed ? `<p class="tlp-gone">Removed${by && timelineDay(by.created_at) ? ` ${timelineEsc(timelineDay(by.created_at))}` : ''}${by?.text ? `: ${timelineEsc(by.text)}` : ''}</p>${by ? quotesOf(by) : ''}` : '';
    return `<li class="${removed ? 'tlp-removed' : ''}"><span class="tlp-kind">${timelineEsc(TIMELINE_KINDS[row.kind] || row.kind)}</span> ${removed ? `<s>${timelineEsc(row.text)}</s>` : timelineEsc(row.text)} <span class="tlp-day">${timelineEsc(timelineDay(row.created_at))}</span>${gone}${quotes}</li>`;
  });
  const closed = history?.closed_questions?.length ? `<section class="tlp-block"><h4>Closed questions</h4><ul>${history.closed_questions.map(q => {
    const answer = q.state === 'answered' && q.answer_entry_id ? byId.get(q.answer_entry_id) : null;
    return `<li><span class="tlp-kind">${q.state === 'answered' ? 'Answered' : 'Dropped'}</span> ${timelineEsc(q.text)}${answer?.text ? `<p class="tlp-gone">${timelineEsc(answer.text)}</p>` : ''}</li>`;
  }).join('')}</ul></section>` : '';
  const links = history?.links || {};
  const group = (label, items, open) => items?.length ? `<section class="tlp-block"><h4>${label}</h4><ul>${items.map(item => {
    const name = timelineEsc(item.title || (item.type === 'task' ? 'Untitled' : label.slice(0, -1))), note = timelineEsc(item.status || item.state || '');
    return `<li>${open && item.type === 'task' ? `<button type="button" class="quiet tlp-open" data-tlp-open="${timelineEsc(item.id)}" data-tlp-business="${timelineEsc(item.business_id ?? '')}">${name}</button>` : name}${note ? ` <span class="tlp-day">${note}</span>` : ''}</li>`;
  }).join('')}</ul></section>` : '';
  return (rows.length ? `<ol class="tlp-history" aria-label="Every entry, oldest first">${rows.join('')}</ol>` : '<p class="tlp-none">Nothing has been filed yet.</p>')
    + (history?.next_cursor ? `<button type="button" class="quiet tlp-more" data-tlp-more>${history.busy ? 'Loading…' : 'Load more'}</button>` : '')
    + closed + group('Subtasks', links.subtasks, true) + group('Approvals', links.approvals, false) + group('Reports', links.reports, false);
}
// The whole panel as markup; ``view`` is {history, open, error}. Empty string when the item has no timeline.
function timelineHtml(data, view = {}) {
  if (timelineEmpty(data)) return view.error ? `<p class="tlp-error" role="alert">${timelineEsc(view.error)}</p>` : '';
  const open = !!view.open;
  return `<div class="tlp-head"><button type="button" class="quiet tlp-switch" data-tlp-switch aria-pressed="${open}">${open ? 'Hide history' : 'History'}</button></div>`
    + (open ? (view.history ? timelineHistoryHtml(view.history) : view.error ? '' : '<p class="tlp-none">Loading…</p>') : timelineStateHtml(data.state))
    + (view.error ? `<p class="tlp-error" role="alert">${timelineEsc(view.error)}</p>` : '');
}
// Owns one host element. ``readHistory({cursor})`` returns the history read of the same item; ``openTask`` opens a subtask.
function timelinePanelMount(host, {readHistory, openTask} = {}) {
  let data = null, history = null, open = false, error = '', contextError = '', generation = 0, disposed = false;
  const paint = () => {
    const html = timelineHtml(data, {open, history, error: contextError || error});
    host.hidden = !html;
    host.innerHTML = html;
  };
  async function load(more) {
    if (!open || !readHistory) return;
    const mine = ++generation;
    if (more && history) { history = {...history, busy: true}; paint(); }
    try {
      const page = await readHistory(more && history?.next_cursor ? {cursor: history.next_cursor} : {});
      if (disposed || mine !== generation) return;
      history = more && history ? {entries: [...history.entries, ...(page.entries || [])], links: history.links, closed_questions: history.closed_questions, next_cursor: page.next_cursor || null, revision: page.revision}
        : {entries: page.entries || [], links: page.links || {}, closed_questions: page.closed_questions || [], next_cursor: page.next_cursor || null, revision: page.revision};
      error = '';
    } catch (_) {
      if (disposed || mine !== generation) return;
      error = "Couldn't load the history. Try again";
      if (history) history = {...history, busy: false};
    }
    paint();
  }
  host.addEventListener('click', event => {
    const target = event.target.closest?.('button');
    if (!target || !host.contains(target)) return;
    if (target.hasAttribute('data-tlp-switch')) { open = !open; error = ''; if (open) { history = null; paint(); void load(false); } else { generation++; paint(); } }
    else if (target.hasAttribute('data-tlp-more')) void load(true);
    else if (target.hasAttribute('data-tlp-open')) openTask?.({id: target.dataset.tlpOpen, business_id: target.dataset.tlpBusiness || null});
  });
  return {
    update(next) {
      const changed = !data || next?.revision !== data.revision;
      data = next || null;
      contextError = '';
      if (timelineEmpty(data)) { open = false; history = null; generation++; }
      paint();
      if (open && changed) void load(false);
    },
    // The timeline could not be read again: show the panel's own error line, not the old content.
    fail(message) {
      data = null; history = null; open = false; error = ''; generation++;
      contextError = message || "Couldn't load the timeline. Try again";
      paint();
    },
    dispose() { disposed = true; generation++; host.replaceChildren(); host.hidden = true; },
  };
}
// The same timeline as a journal, in the look of the rail Timeline page (css/agent-timeline.css): every entry, open question and
// closed question on one line of dots, newest first, the owner's quoted words behind the row's chevron. A removed entry stays on
// its own date, struck, and says when and why it was removed.
const timelineWhen = stamp => typeof acOccurred === 'function' && typeof sjDate === 'function'
  ? acOccurred({occurred: stamp ? {date: sjDate(stamp), precision: 'instant', instant: stamp} : {}}) : timelineDay(stamp);
function timelineJournalRows(data) {
  const entries = data?.entries || [], byId = new Map(entries.map(row => [row.id, row]));
  const quotesOf = row => (row?.sources || []).map(source => source.quote).filter(Boolean);
  const rows = [];
  entries.forEach((row, order) => {
    if (row.action === 'remove') {
      if (!byId.has(row.removes_id)) rows.push({id: row.id, at: row.created_at, order, text: `Removed an earlier entry${row.text ? `: ${row.text}` : ''}`, meta: ['Removed'], quotes: quotesOf(row)});
      return;
    }
    const removed = !!row.removed_by, by = removed ? byId.get(row.removed_by) : null;
    rows.push({id: row.id, at: row.created_at, order, text: row.text, struck: removed,
      meta: [TIMELINE_KINDS[row.kind] || row.kind, ...(removed ? [`Removed${by && timelineDay(by.created_at) ? ` ${timelineDay(by.created_at)}` : ''}${by?.text ? `: ${by.text}` : ''}`] : [])],
      quotes: [...quotesOf(row), ...quotesOf(by)]});
  });
  (data?.state?.questions || []).forEach((q, order) => rows.push({id: `q:${q.id}`, at: q.created_at || '', order: entries.length + order, text: q.text,
    meta: ['Open question', ...(q.blocking ? ['Blocks work'] : [])], quotes: []}));
  (data?.closed_questions || []).forEach((q, order) => {
    const answer = q.state === 'answered' && q.answer_entry_id ? byId.get(q.answer_entry_id) : null;
    rows.push({id: `q:${q.id}`, at: q.closed_at || '', order: -1 - order, text: q.text, meta: [q.state === 'answered' ? 'Question answered' : 'Question dropped'], notes: answer?.text ? [answer.text] : [], quotes: []});
  });
  // Newest first; an open question with no date is current, so it leads.
  const time = row => { const at = Date.parse(row.at); return Number.isFinite(at) ? at : Infinity; };
  return rows.sort((a, b) => time(b) - time(a) || b.order - a.order);
}
function timelineJournalRow(row) {
  const chevron = typeof AGENT_TIMELINE_CHEVRON === 'string' ? AGENT_TIMELINE_CHEVRON : '';
  const text = timelineEsc(row.text), meta = (row.meta || []).filter(Boolean), evidence = [...(row.notes || []), ...(row.quotes || []).map(quote => `“${quote}”`)];
  const content = `<div class="agent-timeline-copy"><p class="agent-timeline-text">${row.struck ? `<s>${text}</s>` : text}</p>${meta.length ? `<p class="agent-timeline-meta">${meta.map(timelineEsc).join(' · ')}</p>` : ''}</div>`;
  return `<li class="agent-timeline-row" data-entry-id="${timelineEsc(row.id)}"><time${row.at ? ` datetime="${timelineEsc(row.at)}"` : ''}>${timelineEsc(timelineWhen(row.at))}</time>${evidence.length
    ? `<details class="agent-timeline-entry"><summary>${content}<span class="agent-timeline-expand">${chevron}</span></summary><div class="agent-timeline-evidence" aria-label="Evidence">${evidence.map(line => `<p>${timelineEsc(line)}</p>`).join('')}</div></details>`
    : content}</li>`;
}
// Owns one ``section.agent-timeline``. ``read(args)`` is the history read of the item; ``refresh`` reads it again, every page.
function timelineJournalMount(host, {read} = {}) {
  let generation = 0, disposed = false, painted = null;
  host.innerHTML = `<div class="agent-timeline-caption"><p role="status" aria-live="polite" data-status>Loading…</p></div>
    <div class="agent-timeline-error" hidden><p role="alert"></p><button type="button" class="quiet" data-retry>Retry</button></div>
    <ol class="agent-timeline-list" aria-label="Timeline entries, newest first"></ol>`;
  const status = host.querySelector('[data-status]'), problem = host.querySelector('.agent-timeline-error'), list = host.querySelector('ol');
  async function refresh() {
    const mine = ++generation;
    try {
      let data = await read({}), cursor = data?.next_cursor;
      // The server pages oldest first; the journal leads with the newest, so it reads to the end before it paints.
      for (let page = 1; cursor && page < 40; page++) {
        const next = await read({cursor});
        data = {...data, entries: [...(data.entries || []), ...(next.entries || [])]}; cursor = next.next_cursor;
      }
      if (disposed || mine !== generation) return;
      const rows = timelineJournalRows(data), html = rows.map(timelineJournalRow).join('');
      if (html !== painted) {
        const open = new Set([...list.querySelectorAll('details[open]')].map(node => node.closest('[data-entry-id]')?.dataset.entryId));
        list.innerHTML = painted = html;
        for (const node of list.querySelectorAll('details')) if (open.has(node.closest('[data-entry-id]')?.dataset.entryId)) node.open = true;
      }
      problem.hidden = true; list.hidden = !rows.length;
      status.textContent = rows.length ? `${rows.length} ${rows.length === 1 ? 'entry' : 'entries'}` : 'Nothing on the timeline yet.';
    } catch (_) {
      if (disposed || mine !== generation) return;
      painted = null; list.innerHTML = ''; list.hidden = true; status.textContent = '';
      problem.querySelector('p').textContent = "Couldn't load the timeline. Try again"; problem.hidden = false;
    }
  }
  host.addEventListener('click', event => { if (event.target.closest?.('[data-retry]')) void refresh(); });
  return {refresh, dispose() { disposed = true; generation++; host.replaceChildren(); }};
}
if (typeof window !== 'undefined') Object.assign(window, {timelineHtml, timelinePanelMount, timelineJournalMount});
