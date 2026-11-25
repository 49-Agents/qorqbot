// The owner's timezone: the installation's one clock (calendar, Focus chips, reminders, operational labels, agent text).
// It is stored on the server (Settings > Timezone). The first time it was never set by the owner, this browser's zone fills it once.
const OWNER_TZ_ENDPOINT = '/root/operational-timezone';
const ownerTimezoneState = {adopting: null};

function ownerTimezoneZones() {
  let zones = [];
  try { zones = Intl.supportedValuesOf('timeZone'); } catch (_) { zones = []; }
  return zones.includes('UTC') ? zones : ['UTC', ...zones];
}
function ownerTimezoneBrowserZone() {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ''; } catch (_) { return ''; }
}
// "UTC-04:00" for a zone now; '' for a name this browser does not know.
function ownerTimezoneOffset(zone, at = new Date()) {
  try {
    const name = new Intl.DateTimeFormat('en-US', {timeZone: zone, timeZoneName: 'longOffset'}).formatToParts(at).find(part => part.type === 'timeZoneName')?.value || '';
    return name === 'GMT' ? 'UTC+00:00' : name.replace('GMT', 'UTC');
  } catch (_) { return ''; }
}
// Apply a saved zone to this page at once; the calendar's clock notices the change and redraws.
function ownerTimezoneApply(result) {
  sjConfigure(result.timezone);
  if (state.session) { state.session.operational_timezone = result.timezone; state.session.operational_timezone_source = result.source; }
  document.dispatchEvent(new CustomEvent('operational-timezone-changed', {detail: result}));
}
async function ownerTimezoneSave(zone) {
  const result = coreData(await post(OWNER_TZ_ENDPOINT, {timezone: zone}));
  ownerTimezoneApply(result);
  return result;
}
// First run: the owner never chose, so take this browser's zone, once. A later explicit choice makes the source "owner" and ends this.
function ownerTimezoneAdopt(session) {
  if (!session || session.is_installation_owner !== true || session.operational_timezone_source === 'owner') return null;
  if (ownerTimezoneState.adopting) return ownerTimezoneState.adopting;
  const zone = ownerTimezoneBrowserZone();
  if (!zone) return null;
  ownerTimezoneState.adopting = ownerTimezoneSave(zone).catch(() => null);   // refused or offline: keep the current zone, no retry loop
  return ownerTimezoneState.adopting;
}
