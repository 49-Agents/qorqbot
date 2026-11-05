// Shared agent activity helpers (status pane, roster presence, current work). Compact session snapshots; polling never opens a terminal or loads task histories.
const AGENT_FLEET_POLL_MS = 60000;

const agentActivityState = value => ({running:'Running', idle:'Idle', offline:'Offline', unknown:'Unknown'})[value] || 'Unknown';
function agentTimeDetail(value, empty = 'unavailable') {
  const at = Date.parse(value);
  if (!Number.isFinite(at)) return empty;
  return `${agpadInboxAge(value, empty)} · ${sjDateTime(at, {hour:'numeric', minute:'2-digit'})}`;
}
function agentObservedHTML(agent) {
  const sessions = agent.sessions || [];
  const active = sessions.filter(session => ['running','idle'].includes(session.state));
  const providers = [...new Set((active.length ? active : sessions).map(session => session.runtime).filter(Boolean))];
  return `<span class="fleet-state" data-state="${esc(agent.state || 'unknown')}">${agentActivityState(agent.state || 'unknown')}</span><span class="agent-providers">${providers.map(agentProviderBadge).join('')}</span>`;
}
function agentInboxHTML(agent) {
  const inbox = agent && agent.inbox;
  if (!inbox || typeof inbox !== 'object') return '';
  const view = agentDeliveryView(inbox);
  const last = view.at ? agentTimeDetail(view.at, '') : '';
  return `<div class="fleet-delivery" data-inbox-state="${esc(view.state)}"><div class="fleet-delivery-line"><span class="fleet-inbox-state" data-state="${esc(view.state)}">${esc(view.label)}</span><span class="fleet-delivery-detail">${esc([view.reason, last && 'Last attempt ' + last].filter(Boolean).join(' · '))}</span></div></div>`;
}

// Provider marks from Simple Icons v14 (CC0); brand rights remain with their owners.
const AGENT_PROVIDER_LOGOS = {"claude": "<svg aria-hidden=\"true\" fill=\"currentColor\" role=\"img\" viewBox=\"0 0 24 24\" xmlns=\"http://www.w3.org/2000/svg\"><path d=\"m4.7144 15.9555 4.7174-2.6471.079-.2307-.079-.1275h-.2307l-.7893-.0486-2.6956-.0729-2.3375-.0971-2.2646-.1214-.5707-.1215-.5343-.7042.0546-.3522.4797-.3218.686.0608 1.5179.1032 2.2767.1578 1.6514.0972 2.4468.255h.3886l.0546-.1579-.1336-.0971-.1032-.0972L6.973 9.8356l-2.55-1.6879-1.3356-.9714-.7225-.4918-.3643-.4614-.1578-1.0078.6557-.7225.8803.0607.2246.0607.8925.686 1.9064 1.4754 2.4893 1.8336.3643.3035.1457-.1032.0182-.0728-.164-.2733-1.3539-2.4467-1.445-2.4893-.6435-1.032-.17-.6194c-.0607-.255-.1032-.4674-.1032-.7285L6.287.1335 6.6997 0l.9957.1336.419.3642.6192 1.4147 1.0018 2.2282 1.5543 3.0296.4553.8985.2429.8318.091.255h.1579v-.1457l.1275-1.706.2368-2.0947.2307-2.6957.0789-.7589.3764-.9107.7468-.4918.5828.2793.4797.686-.0668.4433-.2853 1.8517-.5586 2.9021-.3643 1.9429h.2125l.2429-.2429.9835-1.3053 1.6514-2.0643.7286-.8196.85-.9046.5464-.4311h1.0321l.759 1.1293-.34 1.1657-1.0625 1.3478-.8804 1.1414-1.2628 1.7-.7893 1.36.0729.1093.1882-.0183 2.8535-.607 1.5421-.2794 1.8396-.3157.8318.3886.091.3946-.3278.8075-1.967.4857-2.3072.4614-3.4364.8136-.0425.0304.0486.0607 1.5482.1457.6618.0364h1.621l3.0175.2247.7892.522.4736.6376-.079.4857-1.2142.6193-1.6393-.3886-3.825-.9107-1.3113-.3279h-.1822v.1093l1.0929 1.0686 2.0035 1.8092 2.5075 2.3314.1275.5768-.3218.4554-.34-.0486-2.2039-1.6575-.85-.7468-1.9246-1.621h-.1275v.17l.4432.6496 2.3436 3.5214.1214 1.0807-.17.3521-.6071.2125-.6679-.1214-1.3721-1.9246L14.38 17.959l-1.1414-1.9428-.1397.079-.674 7.2552-.3156.3703-.7286.2793-.6071-.4614-.3218-.7468.3218-1.4753.3886-1.9246.3157-1.53.2853-1.9004.17-.6314-.0121-.0425-.1397.0182-1.4328 1.9672-2.1796 2.9446-1.7243 1.8456-.4128.164-.7164-.3704.0667-.6618.4008-.5889 2.386-3.0357 1.4389-1.882.929-1.0868-.0062-.1579h-.0546l-6.3385 4.1164-1.1293.1457-.4857-.4554.0608-.7467.2307-.2429 1.9064-1.3114Z\"/></svg>", "openai": "<svg aria-hidden=\"true\" fill=\"currentColor\" role=\"img\" viewBox=\"0 0 24 24\" xmlns=\"http://www.w3.org/2000/svg\"><path d=\"M22.2819 9.8211a5.9847 5.9847 0 0 0-.5157-4.9108 6.0462 6.0462 0 0 0-6.5098-2.9A6.0651 6.0651 0 0 0 4.9807 4.1818a5.9847 5.9847 0 0 0-3.9977 2.9 6.0462 6.0462 0 0 0 .7427 7.0966 5.98 5.98 0 0 0 .511 4.9107 6.051 6.051 0 0 0 6.5146 2.9001A5.9847 5.9847 0 0 0 13.2599 24a6.0557 6.0557 0 0 0 5.7718-4.2058 5.9894 5.9894 0 0 0 3.9977-2.9001 6.0557 6.0557 0 0 0-.7475-7.0729zm-9.022 12.6081a4.4755 4.4755 0 0 1-2.8764-1.0408l.1419-.0804 4.7783-2.7582a.7948.7948 0 0 0 .3927-.6813v-6.7369l2.02 1.1686a.071.071 0 0 1 .038.052v5.5826a4.504 4.504 0 0 1-4.4945 4.4944zm-9.6607-4.1254a4.4708 4.4708 0 0 1-.5346-3.0137l.142.0852 4.783 2.7582a.7712.7712 0 0 0 .7806 0l5.8428-3.3685v2.3324a.0804.0804 0 0 1-.0332.0615L9.74 19.9502a4.4992 4.4992 0 0 1-6.1408-1.6464zM2.3408 7.8956a4.485 4.485 0 0 1 2.3655-1.9728V11.6a.7664.7664 0 0 0 .3879.6765l5.8144 3.3543-2.0201 1.1685a.0757.0757 0 0 1-.071 0l-4.8303-2.7865A4.504 4.504 0 0 1 2.3408 7.872zm16.5963 3.8558L13.1038 8.364 15.1192 7.2a.0757.0757 0 0 1 .071 0l4.8303 2.7913a4.4944 4.4944 0 0 1-.6765 8.1042v-5.6772a.79.79 0 0 0-.407-.667zm2.0107-3.0231l-.142-.0852-4.7735-2.7818a.7759.7759 0 0 0-.7854 0L9.409 9.2297V6.8974a.0662.0662 0 0 1 .0284-.0615l4.8303-2.7866a4.4992 4.4992 0 0 1 6.6802 4.66zM8.3065 12.863l-2.02-1.1638a.0804.0804 0 0 1-.038-.0567V6.0742a4.4992 4.4992 0 0 1 7.3757-3.4537l-.142.0805L8.704 5.459a.7948.7948 0 0 0-.3927.6813zm1.0976-2.3654l2.602-1.4998 2.6069 1.4998v2.9994l-2.5974 1.4997-2.6067-1.4997Z\"/></svg>"};
function agentProviderBadge(runtime) {
  const provider = runtime === 'claude-code' ? 'claude' : runtime === 'codex' ? 'openai' : null;
  return provider ? `<span class="agent-provider agent-provider-${provider}" aria-label="${provider === 'claude' ? 'Claude' : 'OpenAI'}">${AGENT_PROVIDER_LOGOS[provider]}</span>` : '';
}

// A worker's authored assignment is its one `current_work` record; its native sessions only report presence.
// Other agents keep their per-session declared work.
function agentWorkSessions(agent) {
  const work = agent.current_work;
  if (!work || typeof work !== 'object') return agent.sessions || [];
  return work.item ? [{session_id:'current-work', last_seen_at:agent.last_seen_at, work_updated_at:work.updated_at, work_reviewed_at:work.reviewed_at, work_stale:work.stale === true, items:[work.item]}] : [];
}
function agentWorkHTML(agent, statusPane = false) {
  return agentWorkSessions(agent).flatMap(session => (session.items || []).map((item, index) => {
    const business = item.business_name || state.bizs.find(b => (b.id || b.business_id) === item.business_id)?.name || (item.business_id ? item.business_slug || agent.business_name || 'Project' : 'Global');
    const tags = `${item.blocked || item.blocked_reason ? '<span class="sg-priority-tag">Blocked</span>' : ''}${item.urgent || item.priority === 'urgent' ? '<span class="sg-priority-tag">Urgent</span>' : ''}${session.work_stale === true ? '<span class="sg-priority-tag fleet-stale-tag">Stale declared work</span>' : ''}${session.inbox_owner === true ? '<span class="fleet-session-tag">Inbox owner</span>' : session.inbox_owner === false ? '<span class="fleet-session-tag">Inbox observer</span>' : ''}`;
    const blockers = item.blockers || item.blocked_by || [];
    const blockerLabel = blocker => {
      if (blocker && typeof blocker === 'object') return blocker.title || blocker.summary || blocker.reason || (blocker.task_id ? 'Unavailable task blocker' : 'Unavailable blocker');
      const text = String(blocker || '').trim();
      return text && !/^(?:task[:_])?[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(text) ? text : 'Unavailable blocker';
    };
    const review = session.last_reviewed_at || session.work_reviewed_at || session.reviewed_at;
    const progress = item.last_progress_at || session.last_progress_at || session.progress_at;
    const checkpoint = session.checkpoint || session.checkpoint_summary;
    const checkpointHTML = index === 0 && checkpoint ? `<div class="fleet-checkpoint">Checkpoint ${esc(checkpoint.state || (checkpoint.outstanding_count ? `${checkpoint.outstanding_count} finding${checkpoint.outstanding_count === 1 ? '' : 's'}` : 'reviewed'))}${checkpoint.last_checked_at ? ` · ${esc(agentTimeDetail(checkpoint.last_checked_at, 'time unavailable'))}` : ''}</div>` : '';
    return `<article class="fleet-work" role="${item.task_id && !statusPane ? 'button' : 'group'}" tabindex="0" aria-expanded="${statusPane ? 'false' : 'true'}" ${item.task_id ? `data-work-task="${esc(session.session_id)}"` : ''} data-work-session="${esc(session.session_id)}" data-work-index="${index}" data-work-key="${esc(JSON.stringify([session.session_id,item.task_id || index]))}">
      <div class="fleet-work-top"><span class="fleet-work-business" ${!item.business_id?'aria-label="Global"':''}>${item.business_id?esc(business):aicon('globe','currentColor',13)}</span><span class="fleet-work-agent">${identityAvatar('agent',agent.display_name || agent.name,{...globalAgentProfile(agent.agent_id),...agent,role:{id:agent.role_id}})}</span><span class="fleet-work-tags">${tags}</span></div>
      ${item.task_id && statusPane?'<button type="button" class="quiet fleet-work-menu" aria-label="Manage work card">⋯</button>':''}
      <h3>${esc(item.summary || item.title || 'Current work')}</h3>${item.blocked_reason ? `<p class="fleet-blocked-reason">${esc(item.blocked_reason)}</p>` : ''}<div class="fleet-work-details" ${statusPane ? 'hidden' : ''}>${checkpointHTML}
      ${blockers.length ? `<div class="fleet-blockers"><strong>Blocker${blockers.length === 1 ? '' : 's'}</strong>${blockers.slice(0, 3).map(blocker => `<span>${esc(blockerLabel(blocker))}</span>`).join('')}</div>` : ''}
      <div class="fleet-work-meta"><span>Presence ${esc(agentTimeDetail(session.last_seen_at || agent.last_seen_at, 'not observed'))}</span><span>Declared ${esc(agentTimeDetail(session.work_updated_at, 'not recorded'))}</span><span>Reviewed ${esc(agentTimeDetail(review, 'not reviewed'))}</span><span>Progress ${esc(agentTimeDetail(progress, 'no progress note'))}</span></div></div></article>`;
  })).join('') || '<span class="muted">No current work declared.</span>';
}

function agentWorkExpand(node,expanded) {
  const host=node.closest('[data-current-work]') || node.parentElement; if (!host) return;
  host._expanded ||= new Set();
  expanded?host._expanded.add(node.dataset.workKey):host._expanded.delete(node.dataset.workKey);
  node.setAttribute('aria-expanded',String(expanded));
  node.querySelector('.fleet-work-details').hidden=!expanded;
}
function agentWorkWire(host,agent,p) {
  host._expanded ||= new Set();
  host.querySelectorAll('.fleet-work').forEach(node=>{
    node.querySelectorAll('[title]').forEach(el=>el.removeAttribute('title'));
    if(p)agentWorkExpand(node,host._expanded.has(node.dataset.workKey));
    const expand=()=>agentWorkExpand(node,true);
    node.onclick=async event=>{
      if(!p){event.preventDefault();event.stopPropagation();}
      else if(event.target.closest('button,a,input,textarea,select'))return;
      if(p){expand();return;}
      const item=agentWorkSessions(agent).find(s=>s.session_id===node.dataset.workSession)?.items[Number(node.dataset.workIndex)];
      if(!item?.task_id)return;
      try {
        if(item.business_id){
          const biz=item.business_slug || state.bizs.find(b=>(b.id || b.business_id)===item.business_id)?.slug;
          if(!biz)throw new Error('This task’s project is unavailable.');
          const task=await tFetchTask(biz,item.task_id);
          if(host.isConnected)tDetail(task);
        } else modal({title:'Global task',width:700,stack:true,body:'<div class="fleet-task-detail"></div>',onOpen:el=>globalTasksMount($('.fleet-task-detail',el),{initialTaskId:item.task_id})});
      } catch(error){toast(uiFailure(error, {load: true}));}
    };
    node.onkeydown=event=>{if(event.target===node && ['Enter',' '].includes(event.key)){event.preventDefault();if(p)expand();else node.click();}};
    const menu=node.querySelector('.fleet-work-menu');
    if(menu)menu.onclick=()=>{
      const popup=dropdown(menu,[{value:'details',label:'Expand details'}],async()=>{
      const item=agentWorkSessions(agent).find(s=>s.session_id===node.dataset.workSession)?.items[Number(node.dataset.workIndex)];
      if(!item || !p || !agpadActive(p))return;
      const box=host.closest('.agpad-current'),body=$('#agpad-body',p.el);
      const shell=document.createElement('section');shell.className='agfocus-detail';
      shell.innerHTML='<header class="agfocus-detail-header"><button type="button" class="quiet">← Agent status</button></header><div class="agfocus-detail-body"></div>';
      const content=shell.querySelector('.agfocus-detail-body');
      const detail={dispose:()=>shell.remove(),current:()=>p.focusDetail===detail && shell.isConnected && p.view==='status'};
      const back=()=>{if(!detail.current())return;detail.dispose();p.focusDetail=null;box.hidden=false;agentCurrentWorkPaint(p);};
      shell.querySelector('button').onclick=back;box.hidden=true;body.appendChild(shell);p.focusDetail=detail;
      try {
        if(item.business_id){
          const biz=item.business_slug || state.bizs.find(b=>(b.id || b.business_id)===item.business_id)?.slug;
          if(!biz)throw new Error('This task’s project is unavailable.');
          const task=await tFetchTask(biz,item.task_id);
          if(detail.current())tDetail(task,{host:content,onClose:back});
        }else globalTasksMount(content,{initialTaskId:item.task_id,onBack:back});
      }catch(error){if(detail.current())content.textContent=uiFailure(error, {load: true});}
      });
    };
  });
}
document.addEventListener('click',event=>{
  document.querySelectorAll('.agpad-current .fleet-work[aria-expanded="true"]').forEach(node=>{
    if(!node.contains(event.target) && !event.target.closest('.menu'))agentWorkExpand(node,false);
  });
},true);

function agentCurrentWorkMount(p) {
  const box = document.createElement('section'); box.className = 'agpad-current'; box.setAttribute('aria-label','Current agent work');
  box.innerHTML = '<div class="agpad-current-heading"><h3>Agent status</h3><button type="button" class="agent-refresh" data-activity-refresh aria-label="Refresh agent activity">'+aicon('refresh-cw','currentColor',14)+'</button></div><div data-activity-state>Loading activity…</div><div data-current-work></div><p class="ferr small" role="alert" hidden></p>';
  $('#agpad-body', p.el).appendChild(box);
  $('[data-activity-refresh]',box).onclick = () => agentCurrentWorkRefresh(p);
  agentCurrentWorkPaint(p);
}

function agentCurrentWorkPaint(p) {
  const box = $('.agpad-current',p.el); if (!box || p.focusDetail) return;
  const error = $('[role="alert"]',box); error.textContent = p.activityError || ''; error.hidden = !p.activityError;
  $('[data-activity-refresh]',box).disabled = !!p.activityPending;
  if (!p.activity) { $('[data-activity-state]',box).textContent = p.activityError ? 'Status unavailable' : 'Loading activity…'; return; }
  const agent = p.activity;
  $('[data-activity-state]',box).innerHTML = `${agentObservedHTML(agent)}${agentInboxHTML(agent)}`;
  const body = $('[data-current-work]',box), html = agentWorkHTML(agent, true);
  if (body._html !== html) { body._html = html; body.innerHTML = html; }
  agentWorkWire(body,agent,p);
}

async function agentCurrentWorkRefresh(p) {
  if (!agpadActive(p) || p.activityPending) return;
  const id = p.agentId || state.agents.find(agent => agent.name === p.agent)?.agent_id;
  if (!id) { p.activityError = 'Registered agent unavailable.'; p.nextActivity = Date.now()+AGENT_FLEET_POLL_MS; agentCurrentWorkPaint(p); return; }
  p.activityPending = true; agentCurrentWorkPaint(p);
  try {
    const data = coreData(await api('/me/agents/activity?agent_id='+encodeURIComponent(id)));
    if (!agpadActive(p, true)) return;
    const agent = data.agents?.find(a => a.agent_id === id);
    if (!agent) throw new Error('Agent activity is unavailable.');
    p.activity = agent; p.activityError = '';
  } catch (error) { if (agpadActive(p, true)) p.activityError = (p.activity ? 'Showing the last update. ' : '')+(error.message || 'Could not refresh activity.'); }
  finally { p.activityPending = false; p.nextActivity = Date.now()+AGENT_FLEET_POLL_MS; if (agpadActive(p) && p.view==='status') agentCurrentWorkPaint(p); }
}

// Navigation presence reflects agent activity, not whether the browser's terminal stream is connected.
let agentNavNext = 0, agentNavPending = false;
const agentNavStates = new Map();
const agentNavSnapshots = new Map();
const AGENT_NAV_STALE_MS = 30000;
let agentNavObserved = 0;
function agentPaneIndicators(p = typeof _agpad==='undefined' ? null : _agpad) {
  if(!p?.el?.isConnected)return;
  const id=p.agentId || state.agents?.find(agent=>agent.name===p.agent)?.agent_id;
  const agent=agentNavSnapshots.get(id) || (p.activity?.agent_id===id ? p.activity : null),inbox=agent?.inbox;
  const delivery=agentDeliveryView(inbox),connected=!delivery.attention;
  const activity=agent?.state || 'unknown';
  const inboxLabel=delivery.label;
  const combined=typeof agpadCombinedOverview==='function' && agpadCombinedOverview(p);
  const targets=combined
    ? [['.ttab[data-v="status"]',activity,`${agentActivityState(activity)}. ${inboxLabel}`,false],
       ['[data-agent-overview="activity"]',activity,agentActivityState(activity),false],
       ['[data-agent-overview="inbox"]','idle',inboxLabel,connected]]
    : [['[data-v="inbox"]','idle',inboxLabel,connected],['[data-v="status"]',activity,agentActivityState(activity),false]];
  for(const [selector,value,label,hidden] of targets) {
    const tab=p.el.querySelector(selector);if(!tab)continue;
    let dot=tab.querySelector('.agpad-tab-dot');
    if(!dot){dot=document.createElement('span');dot.className='agpad-tab-dot';dot.setAttribute('aria-hidden','true');tab.appendChild(dot);}
    dot.dataset.state=value;dot.hidden=hidden;
    tab.setAttribute('aria-description',label);
  }
}
function agentNavPaint() {
  document.querySelectorAll('[data-agent-presence]').forEach(dot => {
    // Running is trusted only while a successful observation is recent.
    const seen = agentNavStates.get(dot.dataset.agentPresence) || 'unknown';
    const value = seen === 'running' && Date.now() - agentNavObserved > AGENT_NAV_STALE_MS ? 'unknown' : seen;
    dot.dataset.state = value;
    dot.hidden = value !== 'running';
    dot.setAttribute('aria-label', 'Working');
  });
}
async function agentNavRefresh(force = false) {
  const dots = document.querySelectorAll('[data-agent-presence]');
  // Stale running status clears even while a request is stuck or the page is hidden.
  agentNavPaint();
  if (!dots.length || document.hidden || agentNavPending || (!force && Date.now() < agentNavNext)) return;
  agentNavPending = true;
  const rows = [];
  try {
    let cursor;
    do {
      const data = coreData(await api('/me/agents/activity?limit=200'+(cursor?'&cursor='+encodeURIComponent(cursor):'')));
      if (!Array.isArray(data.agents)) throw new Error('Unavailable');
      rows.push(...data.agents); cursor = data.next_cursor;
    } while (cursor);
    agentNavStates.clear();agentNavSnapshots.clear(); agentNavObserved=Date.now(); rows.forEach(agent => {agentNavStates.set(agent.agent_id,agent.state);agentNavSnapshots.set(agent.agent_id,agent);});
  } catch (error) {
    // A transient failure keeps the last successful presence; lost access clears it.
    if (accessLost(error)) { agentNavStates.clear(); agentNavSnapshots.clear(); }
  }
  finally { agentNavPending=false; agentNavNext=Date.now()+4000; agentNavPaint();agentPaneIndicators(); }
}
// Activity events refresh immediately; a short visible-page poll covers native
// state transitions that do not create operational records. No per-agent streams.
if (typeof onChange === 'function') onChange(['agents', 'action_logs'], null, () => agentNavRefresh(true));
document.addEventListener('visibilitychange', () => { if (!document.hidden) void agentNavRefresh(true); });
setInterval(agentNavRefresh, 5000);
