// Qorqut's one operational human time convention: America/Puerto_Rico. Stored and wire instants stay
// UTC; every operational date/time display and day bucket converts here, never with the browser's zone.
// Technical zones (cron schedules, provider formats, receipts) keep their own explicit labels.
const SJ_ZONE = "America/Puerto_Rico", SJ_LABEL = "San Juan time";
const _sjFormat = new Intl.DateTimeFormat("en-US", { timeZone: SJ_ZONE, year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hourCycle: "h23" });
function sjParts(value) {
  if (value == null || value === "") return null;
  const d = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(d.getTime())) return null;
  const p = Object.fromEntries(_sjFormat.formatToParts(d).map(part => [part.type, part.value]));
  return { date: `${p.year}-${p.month}-${p.day}`, hour: Number(p.hour), time: `${p.hour}:${p.minute}` };
}
const sjDate = value => sjParts(value)?.date || "";          // San Juan calendar date of an instant
const sjTime = value => sjParts(value)?.time || "";          // San Juan HH:MM of an instant
const sjToday = (now = Date.now()) => sjDate(now);
// Literal calendar dates (YYYY-MM-DD) carry no zone: arithmetic and labels never shift them.
const sjAddDays = (date, days) => { const d = new Date(date + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + days); return d.toISOString().slice(0, 10); };
const sjDayLabel = (date, options = {}) => new Date(date + "T12:00:00Z").toLocaleDateString(undefined, { month: "short", day: "numeric", timeZone: "UTC", ...options });
// An instant as a San Juan date and time, e.g. "Mar 1, 2026, 11:30 PM".
function sjDateTime(value, options = { dateStyle: "medium", timeStyle: "short" }) {
  const d = value instanceof Date ? value : new Date(value);
  return value && Number.isFinite(d.getTime()) ? d.toLocaleString(undefined, { ...options, timeZone: SJ_ZONE }) : "";
}
// A San Juan wall-clock date/hour/minute as its UTC instant (zone offsets read from Intl, never assumed).
function sjInstant(date, hour = 0, minute = 0) {
  const guess = Date.UTC(+date.slice(0, 4), +date.slice(5, 7) - 1, +date.slice(8, 10), hour, minute);
  const shown = sjParts(guess), wall = Date.UTC(+shown.date.slice(0, 4), +shown.date.slice(5, 7) - 1, +shown.date.slice(8, 10), shown.hour, +shown.time.slice(3));
  return new Date(guess - (wall - guess));
}
