// Shared writing-view tooltips, dates and activity board.
// One shared tooltip for data-tip controls. Hover waits 300ms; keyboard focus
// opens immediately. Position stays inside the viewport and flips below when needed.
let writingTipEl = null, writingTipT = null;
function writingTipEnsure() {
  if (writingTipEl) return writingTipEl;
  writingTipEl = document.createElement("div");
  writingTipEl.className = "jrtip";
  writingTipEl.setAttribute("role", "tooltip");
  document.body.appendChild(writingTipEl);
  return writingTipEl;
}
function writingTipPosition(target) {
  const el = writingTipEl; if (!el) return;
  const tr = target.getBoundingClientRect(), er = el.getBoundingClientRect(), pad = 6;
  let left = tr.left + tr.width / 2 - er.width / 2;
  left = Math.max(pad, Math.min(left, window.innerWidth - er.width - pad));
  let top = tr.top - er.height - 8;               // side="top": bottom-full + mb-2 (8px gap above)
  if (top < pad) top = tr.bottom + 8;              // clamp: flip below if it'd clip off the top
  top = Math.max(pad, Math.min(top, window.innerHeight - er.height - pad));
  el.style.left = `${left}px`; el.style.top = `${top}px`;
}
function writingTipShow(target) {
  const label = target.getAttribute("data-tip"); if (!label) return;
  const el = writingTipEnsure();
  el.textContent = label;
  el.classList.remove("on");
  writingTipPosition(target);
  requestAnimationFrame(() => { writingTipPosition(target); el.classList.add("on"); });
}
function writingTipHide() {
  clearTimeout(writingTipT); writingTipT = null;
  if (writingTipEl) writingTipEl.classList.remove("on");
}
document.addEventListener("mouseover", e => {
  const t = e.target.closest && e.target.closest("[data-tip]"); if (!t || t.contains(e.relatedTarget)) return;
  clearTimeout(writingTipT);
  writingTipT = setTimeout(() => writingTipShow(t), 300);
});
document.addEventListener("mouseout", e => {
  const t = e.target.closest && e.target.closest("[data-tip]"); if (!t || t.contains(e.relatedTarget)) return;
  writingTipHide();
});
document.addEventListener("focusin", e => {
  const t = e.target.closest && e.target.closest("[data-tip]"); if (!t) return;
  clearTimeout(writingTipT);
  writingTipShow(t);
});
document.addEventListener("focusout", e => {
  const t = e.target.closest && e.target.closest("[data-tip]"); if (!t) return;
  writingTipHide();
});
document.addEventListener("scroll", () => writingTipHide(), true);


function relativeTime(iso) {
  const t = Date.parse(iso || ""); if (isNaN(t)) return "";
  const diff = Date.now() - t;
  const m = Math.floor(diff / 60000);
  if (m < 1) return "just now";
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  const d = Math.floor(h / 24);
  if (d < 7) return `${d}d ago`;
  return new Date(t).toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

const WRITING_HEAT_MAX_MONTHS = 6;
const WRITING_HEAT_MIN_WEEKS = 4;
const WRITING_HEAT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const WRITING_HEAT_ROWS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
const writingHeatKey = d => { const p = n => String(n).padStart(2, "0"); return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`; };
const writingHeatFromKey = key => { const [y, m, d] = key.split("-").map(Number); return new Date(y, m - 1, d); };
const writingHeatLevel = n => n <= 0 ? 0 : n === 1 ? 1 : n === 2 ? 2 : n === 3 ? 3 : 4;  // memo-heatmap.tsx:15-21
function writingHeatmapData(entries) {
  const counts = new Map();
  (entries || []).forEach(m => { const k = String(m.date || "").slice(0, 10); if (k) counts.set(k, (counts.get(k) || 0) + 1); });
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const todayKey = writingHeatKey(today);
  let earliest = todayKey;
  for (const k of counts.keys()) if (k < earliest) earliest = k;
  const cap = new Date(today); cap.setMonth(cap.getMonth() - WRITING_HEAT_MAX_MONTHS);
  const capKey = writingHeatKey(cap);
  let startKey = earliest > capKey ? earliest : capKey;
  const minStart = new Date(today); minStart.setDate(minStart.getDate() - WRITING_HEAT_MIN_WEEKS * 7);
  const minStartKey = writingHeatKey(minStart);
  if (startKey > minStartKey) startKey = minStartKey;
  // snap to the start week's Sunday; end on this week's Saturday
  const cursor = writingHeatFromKey(startKey); cursor.setDate(cursor.getDate() - cursor.getDay());
  const end = writingHeatFromKey(todayKey); end.setDate(end.getDate() + (6 - end.getDay()));
  const weeks = Math.max(1, Math.round((end.getTime() - cursor.getTime()) / T_DAY + 1) / 7);
  const columns = [];
  for (let w = 0; w < weeks; w++) {
    const col = [];
    for (let d = 0; d < 7; d++) {
      const key = writingHeatKey(cursor);
      col.push({ key, count: counts.get(key) || 0, future: key > todayKey || key < startKey, month: cursor.getMonth() });
      cursor.setDate(cursor.getDate() + 1);
    }
    columns.push(col);
  }
  // month label on the first column of each new month; a span under 3 columns gets evicted by the
  // next label so a partial first month doesn't glue its label to its neighbour's (memo-heatmap.tsx:86-100)
  const monthLabels = columns.map(() => "");
  let lastMonth = -1, lastAt = -10;
  columns.forEach((col, i) => {
    const mo = col[0].month;
    if (mo === lastMonth) return;
    lastMonth = mo;
    if (lastAt >= 0 && i - lastAt < 3) monthLabels[lastAt] = "";
    monthLabels[i] = WRITING_HEAT_MONTHS[mo];
    lastAt = i;
  });
  return { columns, monthLabels };
}
function writingHeatmapHtml(entries) {
  const { columns, monthLabels } = writingHeatmapData(entries);
  const monthRow = monthLabels.map(l => `<div class="jrhmonth">${l ? `<span>${esc(l)}</span>` : ""}</div>`).join("");
  const rowLabels = WRITING_HEAT_ROWS.map(l => `<div class="jrhrow-lbl">${l}</div>`).join("");
  const cols = columns.map(col => `<div class="jrhcol">${col.map(cell => {
    if (cell.future) return `<div class="jrhcell jrhcell-blank"></div>`;
    const d = writingHeatFromKey(cell.key);
    const label = `${WRITING_HEAT_MONTHS[d.getMonth()]} ${d.getDate()}, ${d.getFullYear()}`;
    const title = `${cell.count} ${cell.count === 1 ? "entry" : "entries"} · ${label}`;
    return `<button type="button" class="jrhcell jrhl${writingHeatLevel(cell.count)}" data-key="${esc(cell.key)}" data-tip="${esc(title)}"></button>`;
  }).join("")}</div>`).join("");
  return `<div class="jrheat">
    <div class="jreyebrow">Activity</div>
    <div class="jrhwrap">
      <div class="jrhmonths">${monthRow}</div>
      <div class="jrhbody">
        <div class="jrhrows">${rowLabels}</div>
        <div class="jrhcols">${cols}</div>
      </div>
    </div>
  </div>`;
}

// One compact card layout for Notes, Stories and People.
function writingCard({id, title, updated, detail = '', icon = '', role, org, summary}) {
  return `<button class="jrcard${role !== undefined || org !== undefined ? ' person-card' : ''}" type="button" data-id="${esc(id)}"><div class="jrcrow1"><span class="jrctitle">${esc(title || 'Untitled')}</span>${icon}</div>${summary ? `<p class="jrcsummary">${esc(summary)}</p>` : ''}${role !== undefined || org !== undefined ? `<div class="person-role" title="${esc(role || '')}">${esc(role || '')}</div><div class="person-org" title="${esc(org || '')}">${esc(org || '')}</div>` : ''}<div class="jrcfoot"><span>${updated ? esc(relativeTime(updated)) : ''}</span><span>${esc(detail)}</span></div></button>`;
}
const writingRecent = rows => [...rows].sort((a,b) => (Date.parse(b.updated_at || b.updatedAt || b.at) || 0) - (Date.parse(a.updated_at || a.updatedAt || a.at) || 0) || String(a.name || a.title || '').localeCompare(String(b.name || b.title || '')));
