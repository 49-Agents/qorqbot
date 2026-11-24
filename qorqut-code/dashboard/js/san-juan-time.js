// Operational time follows the installation's API setting. UTC is the fresh-install fallback.
// Stored action dates and explicit provider/scheduler zones retain their own meaning.
let SJ_ZONE = 'UTC', SJ_LABEL = 'UTC time';
const _sjFormats = new Map();
function sjFormatter(zone) {
  if (!_sjFormats.has(zone)) _sjFormats.set(zone, new Intl.DateTimeFormat('en-US', {timeZone: zone,
    year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', hourCycle:'h23'}));
  return _sjFormats.get(zone);
}
function sjConfigure(config) {
  const zone = typeof config === 'string' ? config : config?.operational_timezone || config?.timezone || config?.real_time?.zone;
  if (zone == null) return;
  if (typeof zone !== 'string' || !zone) throw new Error('Invalid operational timezone');
  sjFormatter(zone); // Validate before replacing the current setting.
  SJ_ZONE = zone; SJ_LABEL = zone + ' time';
}
function sjParts(value, zone = SJ_ZONE) {
  if (value == null || value === '') return null;
  const d = value instanceof Date ? value : new Date(value);
  if (!Number.isFinite(d.getTime())) return null;
  const p = Object.fromEntries(sjFormatter(zone).formatToParts(d).map(part => [part.type, part.value]));
  return {date:`${p.year}-${p.month}-${p.day}`, hour:Number(p.hour), time:`${p.hour}:${p.minute}`};
}
const sjDate = (value, zone = SJ_ZONE) => sjParts(value, zone)?.date || '';
const sjTime = (value, zone = SJ_ZONE) => sjParts(value, zone)?.time || '';
const sjToday = (now = Date.now()) => sjDate(now);
// Literal calendar dates carry no zone: arithmetic and labels never shift them.
const sjAddDays = (date, days) => { const d = new Date(date + 'T12:00:00Z'); d.setUTCDate(d.getUTCDate() + days); return d.toISOString().slice(0, 10); };
const sjDayLabel = (date, options = {}) => new Date(date + 'T12:00:00Z').toLocaleDateString(undefined, {month:'short', day:'numeric', timeZone:'UTC', ...options});
function sjDateTime(value, options = {dateStyle:'medium', timeStyle:'short'}, zone = SJ_ZONE) {
  const d = value instanceof Date ? value : new Date(value);
  return value && Number.isFinite(d.getTime()) ? d.toLocaleString(undefined, {...options, timeZone:zone}) : '';
}
// Convert a local minute to an instant. Try offsets on both sides of a transition,
// reject nonexistent wall times, and select the first occurrence of a repeated minute.
function sjInstant(date, hour = 0, minute = 0) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date);
  if (!match || !Number.isInteger(hour) || hour < 0 || hour > 23 || !Number.isInteger(minute) || minute < 0 || minute > 59) return new Date(NaN);
  const guess = Date.UTC(+match[1], +match[2] - 1, +match[3], hour, minute), offsets = new Set();
  for (const delta of [-2, -1, 0, 1, 2]) {
    const at = guess + delta * 86400000, shown = sjParts(at);
    const wall = Date.UTC(+shown.date.slice(0,4), +shown.date.slice(5,7)-1, +shown.date.slice(8,10), shown.hour, +shown.time.slice(3));
    offsets.add(wall - at);
  }
  const matches = [...offsets].map(offset => guess - offset).filter(at => {
    const shown = sjParts(at);
    return shown.date === date && shown.hour === hour && +shown.time.slice(3) === minute;
  });
  return new Date(matches.length ? Math.min(...matches) : NaN);
}
