// Retired owner drawer routes. No views, mounts, polling or Tab-Tab controls remain.
// Journal and Calendar aliases resolve to Ordubashy; other historical drawer URLs settle on Agents.
const OV_VIEWS = [];
function ovDescriptor() { return null; }
function ovLegacy(hash) {
  if (embeddedAgentPane) return undefined;
  const parts=String(hash || '').split('?')[0].split('/');
  if(parts[0]!=='#' || parts[1]!=='global')return undefined;
  if(['notes','journal'].includes(parts[2]))return {view:'journal'};
  if(parts[2]==='calendar')return {view:'calendar'};
  if(['overview','charts','tables','costs','fleet','incidents','system_dev','reports','routines','signoffs'].includes(parts[2]))return {view:null};
}
function ovClearDescriptor() {
  if(embeddedAgentPane)return;
  const url=new URL(location.href);
  ['drawer','table_scope','table_id','table_mode'].forEach(key=>url.searchParams.delete(key));
  if(url.search!==location.search)history.replaceState(history.state,'',url.pathname+url.search+url.hash);
}
function ovInitialView() {
  const journal=['notes','journal'].includes(new URLSearchParams(location.search).get('drawer'));
  ovClearDescriptor();
  if(journal && !embeddedAgentPane)history.replaceState(history.state,'',location.pathname+location.search+'#/global/notes');
  return null;
}
async function ovOpenLegacy(legacy) {
  ovClearDescriptor();
  if(legacy.view==='journal' || legacy.view==='calendar')return selectGlobal(legacy.view);
  history.replaceState(history.state,'',location.pathname+location.search+'#/global/agents');
  return selectGlobal('agents');
}
function ovOpen() { return false; }
function ovViewChanged() {}
function ovDrawerToggled() {}
function ovDispose() {}
function ovPaint() {}
