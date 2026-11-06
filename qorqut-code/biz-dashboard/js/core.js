const $ = (s, el = document) => el.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const embeddedAgentPane = new URLSearchParams(location.search).has('agent-pane-embed');
const ls = (k, v) => { try {
  // Embedded page navigation must not replace the surrounding app's last route.
  if (embeddedAgentPane && ['workspace-route', 'global-tab', 'tab', 'context-tab', 'tasks', 'tview'].includes(k)) k = 'agent-pane:' + k;
  return v === undefined ? localStorage.getItem(k) : localStorage.setItem(k, v);
} catch (e) { return null; } };
const COLORS = { red: "#b5482b", amber: "#c8912a", green: "#4d6b53", teal: "#3f7f7a", blue: "#3d5a80", plum: "#7a4f7a", ink: "#f5f0e8" };
// 24 lucide-style icons an agent can pick, keyed by kebab-case lucide name; each value is the
// inner markup for a 24x24 stroke viewBox (paths/shapes only, no outer <svg>)
const AICONS = {
  list: '<path d="M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01"/>',
  mail: '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="m22 7-8.97 5.7a1.94 1.94 0 0 1-2.06 0L2 7"/>',
  flag: '<path d="M5 21V3m0 1c5-4 9 4 14 0v10c-5 4-9-4-14 0"/>',
  triangle: '<path d="M12 3 22 21H2Z"/>',
  circle: '<circle cx="12" cy="12" r="9"/>',
  square: '<rect x="3" y="3" width="18" height="18" rx="1"/>',
  lightbulb: '<path d="M9 18h6M10 21h4M8.5 14.5a6 6 0 1 1 7 0c-.9.7-1.5 1.5-1.5 3.5h-4c0-2-.6-2.8-1.5-3.5Z"/>',
  archive: '<rect x="3" y="3" width="18" height="4" rx="1"/><path d="M5 7v13h14V7M10 11h4"/>',
  paperclip: '<path d="m21 11-9 9a6 6 0 0 1-8-8L14 2a4 4 0 0 1 6 6L10 18a2 2 0 0 1-3-3l9-9"/>',
  "share-2": '<circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><path d="m8.6 10.5 6.8-4M8.6 13.5l6.8 4"/>',
  "refresh-cw": '<path d="M3 12a9 9 0 0 1 15.36-6.36L21 8"/><path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-15.36 6.36L3 16"/><path d="M8 16H3v5"/>',
  "arrow-left": '<path d="m12 19-7-7 7-7"/><path d="M5 12h14"/>',
  bot: '<path d="M12 8V4H8"/><rect width="16" height="12" x="4" y="8" rx="2"/><path d="M2 14h2"/><path d="M20 14h2"/><path d="M15 13v2"/><path d="M9 13v2"/>',
  hand: '<path d="M18 11V6a2 2 0 0 0-4 0v5"/><path d="M14 10V4a2 2 0 0 0-4 0v6"/><path d="M10 10V5a2 2 0 0 0-4 0v8"/><path d="M6 9a2 2 0 0 0-4 0v5c0 4.4 3.6 8 8 8h2c4.4 0 8-3.6 8-8v-3a2 2 0 0 0-4 0Z"/>',
  brain: '<path d="M12 5a3 3 0 1 0-5.997.125 4 4 0 0 0-2.526 5.77 4 4 0 0 0 .556 6.588A4 4 0 1 0 12 18Z"/><path d="M12 5a3 3 0 1 1 5.997.125 4 4 0 0 1 2.526 5.77 4 4 0 0 1-.556 6.588A4 4 0 1 1 12 18Z"/><path d="M15 13a4.5 4.5 0 0 1-3-4 4.5 4.5 0 0 1-3 4"/><path d="M17.599 6.5a3 3 0 0 0 .399-1.375"/><path d="M6.003 5.125A3 3 0 0 0 6.401 6.5"/><path d="M3.477 10.896a4 4 0 0 1 .585-.396"/><path d="M19.938 10.5a4 4 0 0 1 .585.396"/><path d="M6 18a4 4 0 0 1-1.967-.516"/><path d="M19.967 17.484A4 4 0 0 1 18 18"/>',
  sparkles: '<path d="M9.937 15.5A2 2 0 0 0 8.5 14.063l-6.135-1.582a.5.5 0 0 1 0-.962L8.5 9.936A2 2 0 0 0 9.937 8.5l1.582-6.135a.5.5 0 0 1 .963 0L14.063 8.5A2 2 0 0 0 15.5 9.937l6.135 1.581a.5.5 0 0 1 0 .964L15.5 14.063a2 2 0 0 0-1.437 1.437l-1.582 6.135a.5.5 0 0 1-.963 0z"/><path d="M20 3v4"/><path d="M22 5h-4"/><path d="M4 17v2"/><path d="M5 18H3"/>',
  zap: '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02A1 1 0 0 0 11 14z"/>',
  rocket: '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91-.09z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/><path d="M9 12H4s.55-3.03 2-4c1.62-1.08 5 0 5 0"/><path d="M12 15v5s3.03-.55 4-2c1.08-1.62 0-5 0-5"/>',
  flame: '<path d="M8.5 14.5A2.5 2.5 0 0 0 11 12c0-1.38-.5-2-1-3-1.072-2.143-.224-4.054 2-6 .5 2.5 2 4.9 4 6.5 2 1.6 3 3.5 3 5.5a7 7 0 1 1-14 0c0-1.153.433-2.294 1-3a2.5 2.5 0 0 0 2.5 2.5z"/>',
  ghost: '<path d="M9 10h.01"/><path d="M15 10h.01"/><path d="M12 2a8 8 0 0 0-8 8v12l3-3 2.5 2.5L12 19l2.5 2.5L17 19l3 3V10a8 8 0 0 0-8-8z"/>',
  cat: '<path d="M12 5c.67 0 1.35.09 2 .26 1.78-2 5.03-2.84 6.42-2.26 1.4.58-.42 7-.42 7 .57 1.07 1 2.24 1 3.44C21 17.9 16.97 21 12 21s-9-3-9-7.56c0-1.25.5-2.4 1-3.44 0 0-1.89-6.42-.5-7 1.39-.58 4.72.23 6.5 2.23A9.04 9.04 0 0 1 12 5z"/><path d="M8 14v.5"/><path d="M16 14v.5"/><path d="M11.25 16.25h1.5L12 17Z"/>',
  dog: '<path d="M11.25 16.25h1.5L12 17Z"/><path d="M16 14v.5"/><path d="M4.42 11.247A13.152 13.152 0 0 0 4 14.556C4 18.728 7.582 21 12 21s8-2.272 8-6.444c0-1.061-.162-2.2-.493-3.309"/><path d="M8 14v.5"/><path d="M15.997 12.44a3 3 0 0 1 2.526 3.026"/><path d="M8.003 12.44A3 3 0 0 0 5.477 15.466"/><path d="M18.5 8a2.5 2.5 0 0 0-2.5-2.5V4a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v1.5A2.5 2.5 0 0 0 5.5 8"/>',
  bird: '<path d="M16 7h.01"/><path d="M3.4 18H12a8 8 0 0 0 8-8V7a4 4 0 0 0-7.28-2.3L2 20"/><path d="m20 7 2 .5-2 .5"/><path d="M10 18v3"/><path d="M14 17.75V21"/><path d="M7 18a6 6 0 0 0 3.84-10.61"/>',
  fish: '<path d="M6.5 12c.94-3.46 4.94-6 8.5-6 3.56 0 6.06 2.54 7 6-.94 3.47-3.44 6-7 6s-7.56-2.53-8.5-6Z"/><path d="M18 12v.5"/><path d="M16 17.93a9.77 9.77 0 0 1 0-11.86"/><path d="M7 10.67C7 8 5.58 5.97 2.73 5.5c-1 1.5-1 5 .23 6.5-1.24 1.5-1.24 5-.23 6.5C5.58 18.03 7 16 7 13.33"/><path d="M10.46 7.26C10.2 5.88 9.17 4.24 8 3h5.8a2 2 0 0 1 1.98 1.67l.23 1.4"/><path d="m16.01 17.93-.23 1.4A2 2 0 0 1 13.8 21H9.5a5.96 5.96 0 0 0 1.49-3.98"/>',
  bug: '<rect width="8" height="14" x="8" y="6" rx="4"/><path d="m19 7-3 2"/><path d="m5 7 3 2"/><path d="m19 19-3-2"/><path d="m5 19 3-2"/><path d="M20 13h-4"/><path d="M4 13h4"/><path d="m10 4 1 2"/><path d="m14 4-1 2"/>',
  cpu: '<rect width="16" height="16" x="4" y="4" rx="2"/><rect width="6" height="6" x="9" y="9" rx="1"/><path d="M15 2v2"/><path d="M15 20v2"/><path d="M2 15h2"/><path d="M2 9h2"/><path d="M20 15h2"/><path d="M20 9h2"/><path d="M9 2v2"/><path d="M9 20v2"/>',
  terminal: '<polyline points="4 17 10 11 4 5"/><line x1="12" x2="20" y1="19" y2="19"/>',
  code: '<polyline points="16 18 22 12 16 6"/><polyline points="8 6 2 12 8 18"/>',
  "pen-tool": '<path d="M15.707 21.293a1 1 0 0 1-1.414 0l-1.586-1.586a1 1 0 0 1 0-1.414l5.586-5.586a1 1 0 0 1 1.414 0l1.586 1.586a1 1 0 0 1 0 1.414z"/><path d="m18 13-1.375-6.874a1 1 0 0 0-.746-.776L3.235 2.028a1 1 0 0 0-1.207 1.207L5.35 15.879a1 1 0 0 0 .776.746L13 18"/><path d="m2.3 2.3 7.286 7.286"/><circle cx="11" cy="11" r="2"/>',
  megaphone: '<path d="m3 11 18-5v12L3 14v-3z"/><path d="M11.6 16.8a3 3 0 1 1-5.8-1.6"/>',
  calculator: '<rect width="16" height="20" x="4" y="2" rx="2"/><line x1="8" x2="16" y1="6" y2="6"/><line x1="16" x2="16" y1="14" y2="18"/><path d="M16 10h.01"/><path d="M12 10h.01"/><path d="M8 10h.01"/><path d="M12 14h.01"/><path d="M8 14h.01"/><path d="M12 18h.01"/><path d="M8 18h.01"/>',
  "chart-column": '<path d="M3 3v16a2 2 0 0 0 2 2h16"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/>',
  database: '<ellipse cx="12" cy="5" rx="9" ry="3"/><path d="M3 5V19A9 3 0 0 0 21 19V5"/><path d="M3 12A9 3 0 0 0 21 12"/>',
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  globe: '<circle cx="12" cy="12" r="10"/><path d="M12 2a14.5 14.5 0 0 0 0 20 14.5 14.5 0 0 0 0-20"/><path d="M2 12h20"/>',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/>',
  wrench: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z"/>',
};
const aicon = (name, colorHex, sizePx) => `<svg class="ai" width="${sizePx}" height="${sizePx}" viewBox="0 0 24 24" fill="none" stroke="${colorHex}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${AICONS[name]}</svg>`;
// A business agent without a chosen icon wears its business's letter, like the business avatar.
const agentWearsBusiness = m => m?.icon === 'business' || ((m?.role?.id || m?.kind) === 'business-agent' && !m?.icon);
function agentBusinessLetter(m) {
  const id = m?.business_id || m?.scope?.business_id, slug = m?.business_slug;
  const s = typeof state === 'undefined' ? {bizs: []} : state;  // partial script contexts
  const b = s.bizs.find(x => (id && (x.id || x.business_id) === id) || (slug && x.slug === slug))
    || (!id && !slug && s.scope !== 'global' ? s.bizs.find(x => x.slug === s.biz) : null);
  return String(m?.business_name || b?.name || b?.slug || '·').charAt(0).toUpperCase();
}
// Qorqbot's original mascot geometry, reused from its dark SVG. Its face is white for every user,
// with black eyes and mustache; role colors are presentation only and saved agent identity and
// icon settings are untouched.
const QORQBOT_AGENT_COLORS = {ordubashy:'#ffffff', 'learning-applier':'#9063c8'};
function qorqbotAgentColor(m) {
  const role=m?.role?.id || m?.role_id || m?.kind;
  if(QORQBOT_AGENT_COLORS[role])return QORQBOT_AGENT_COLORS[role];
  if(role!=='global-worker')return null;
  const palette=['#448bc6','#4b9b75','#ba7253','#8e70b6','#529697'];
  const key=String(m?.agent_id || m?.id || m?.principal_id || m?.display_name || m?.name || 'worker');
  let hash=0;for(const char of key)hash=(hash*31+char.charCodeAt(0))>>>0;
  return palette[hash%palette.length];
}
// The round silhouette and classic mustache, in the original face's coordinate space.
const QORQBOT_BODIES = {
  round: 'M31 86 C31 60 52 37 79 37 C106 36 129 59 130 86 C131 113 109 136 81 136 C54 137 31 115 31 86 Z'
};
const QORQBOT_MUSTACHES = {
  classic: 'M91 101 C86 96 82 97 75 101 C64 107 54 103 53 90 C46 108 58 120 73 115 C82 113 88 108 91 105 C95 108 101 113 109 114 C123 117 134 106 127 89 C126 102 117 106 107 101 C100 97 96 96 91 101 Z'
};
function qorqbotAgentVariant(m) {
  const role=m?.role?.id || m?.role_id || m?.kind;
  if(role==='ordubashy')return {body:'round',mustache:'classic',eyeOffset:-10,featureColor:'#000'};
  if(role==='learning-applier')return {body:'brain',mustache:'classic'};
  return {body:'round',mustache:'classic'};
}
function qorqbotLearningGlyph(color,size) {
  return `<svg class="ai qorqbot-agent" width="${size}" height="${size}" viewBox="25 30 110 112" aria-hidden="true" focusable="false">
    <path class="qorqbot-brain" fill="${color}" d="M80 45 C66 32 47 39 44 55 C27 56 24 76 35 87 C25 105 40 125 58 123 C67 136 82 134 86 124 C103 134 122 120 121 105 C138 94 132 73 119 67 C119 47 99 34 80 45 Z"/>
    <g fill="none" stroke="#dac4f4" stroke-opacity=".65" stroke-width="3.5" stroke-linecap="round" stroke-linejoin="round">
      <path d="M80 45 C74 53 82 60 78 66"/>
    </g>
    <g transform="rotate(-7 81 89)">
      <g fill="#fff"><g class="qorqbot-gaze"><g class="qorqbot-eye"><ellipse cx="61" cy="85" rx="4" ry="7"/></g></g><g class="qorqbot-gaze"><g class="qorqbot-eye"><ellipse cx="103" cy="85" rx="4" ry="7"/></g></g></g>
      <g class="qorqbot-glasses" fill="none" stroke="#30233f" stroke-width="4.5" stroke-linecap="round" stroke-linejoin="round">
        <rect x="44" y="72" width="34" height="28" rx="10"/><rect x="86" y="72" width="34" height="28" rx="10"/>
        <path d="M78 82 Q82 78 86 82 M37 77 L44 79 M120 79 L127 76"/>
      </g>
    </g>
  </svg>`;
}
function qorqbotAgentGlyph(color,size,variant={body:'round',mustache:'classic'}) {
  if(variant.body==='brain')return qorqbotLearningGlyph(color,size);
  return `<svg class="ai qorqbot-agent" width="${size}" height="${size}" viewBox="25 30 110 112" aria-hidden="true" focusable="false">
    <path fill="${color}" d="${QORQBOT_BODIES[variant.body] || QORQBOT_BODIES.round}"/>
    <g transform="${variant.body==='round' ? '' : 'translate(79 85) scale(.84) translate(-100 -79)'}">
    <g fill="#000" transform="translate(${variant.eyeOffset || 0} 0) rotate(-22 80 86)">
      <g transform="translate(90 73) rotate(-12)"><g class="qorqbot-gaze"><g class="qorqbot-eye"><path d="M0 -12 C8 -7 8 7 0 12 C-8 7 -8 -7 0 -12 Z"/></g></g></g>
      <g transform="translate(116.5 72) rotate(-12)"><g class="qorqbot-gaze"><g class="qorqbot-eye"><path d="M0 -10.5 C6 -6 6 6 0 10.5 C-6 6 -6 -6 0 -10.5 Z"/></g></g></g>
    </g>
    <g fill="#000" transform="translate(5 0) rotate(-22 80 86)"><path transform="translate(91 103) scale(${variant.mustache==='classic' ? .65 : .58}) translate(-91 -103)" d="${QORQBOT_MUSTACHES[variant.mustache] || QORQBOT_MUSTACHES.classic}"/></g>
    </g>
  </svg>`;
}
// Tiny Spirits: the original non-primary nav artwork, shared by every agent surface. The stored
// registration ID selects the body and colour; catalog icon/colour fields never reach the markup.
const TINY_SPIRIT_BODIES = [
  '<path d="M12 2 C6 1 2 6 2 12 C2 18 6 22 12 22 C19 22 22 17 22 11 C22 5 18 1 12 2 Z"/>',
  '<path d="M12 1 C6 1 3 6 3 13 V18 Q3 22 7 22 H17 Q21 22 21 18 V13 C21 6 18 1 12 1 Z"/>',
  '<rect x="1" y="4" width="22" height="16" rx="8"/>'
];
function tinySpiritGlyph(meta, size = 18) {
  const color = qorqbotAgentColor(meta) || '#448bc6';
  let hash = 0;
  for (const char of String(meta?.agent_id)) hash = (hash * 31 + char.charCodeAt(0)) >>> 0;
  const body = TINY_SPIRIT_BODIES[hash % 3], px = Number(size) || 18;
  return `<svg class="ai qorqbot-agent qorqbot-spirit" width="${px}" height="${px}" viewBox="0 0 24 24" aria-hidden="true" focusable="false">
      <g fill="${color}">${body}</g><g fill="#fff">
      <g class="qorqbot-gaze"><g class="qorqbot-eye"><ellipse cx="8.5" cy="12" rx="1.4" ry="2.6"/></g></g>
      <g class="qorqbot-gaze"><g class="qorqbot-eye"><ellipse cx="15.5" cy="12" rx="1.4" ry="2.6"/></g></g>
      </g></svg>`;
}
const agentRole = m => m?.role?.id || m?.role_id || m?.kind;
const primaryAgent = m => ['ordubashy', 'learning-applier'].includes(agentRole(m));
const agentGlyph = (m, size) => {
  // Profile presentation is authoritative; callers may carry only an ID or stale activity metadata.
  const saved = globalAgentProfile(m?.agent_id || m?.principal_id);
  m = {...m, ...m?.presentation, ...saved};
  const roleId = agentRole(m);
  if (roleId === 'global-worker') return tinySpiritGlyph(m, size);
  const mascotColor=qorqbotAgentColor(m);
  if(mascotColor)return qorqbotAgentGlyph(mascotColor,size,qorqbotAgentVariant(m));
  if (agentWearsBusiness(m)) return `<svg class="ai" width="${size}" height="${size}" viewBox="0 0 24 24" aria-hidden="true"><text x="12" y="12" dy=".36em" text-anchor="middle" font-size="15" font-weight="600" fill="currentColor">${esc(agentBusinessLetter(m))}</text></svg>`;
  const icon = m?.icon || null;
  return aicon(AICONS[icon] ? icon : 'bot', 'currentColor', size);
};

// Shared Global profiles also identify creators of business tasks. Cache the
// directory, not names: different scopes may have agents with the same name.
let globalAgentProfiles = [];
let globalAgentProfilesRequest;
function loadGlobalAgentProfiles() {
  if (!globalAgentProfilesRequest) globalAgentProfilesRequest = api('/root/agents').then(response => {
    const agents = coreData(response).agents;
    if (!Array.isArray(agents)) throw new Error('Global agent profiles unavailable');
    globalAgentProfiles = agents;
    return agents;
  }).catch(error => { globalAgentProfilesRequest = null; throw error; });
  return globalAgentProfilesRequest;
}
function globalAgentProfile(id) {
  if (!id) return null;
  const agent = globalAgentProfiles.find(a => a.agent_id === id || a.principal_id === id);
  return agent ? {...agent, ...agent.presentation} : null;
}

// An embedded pane keeps its route without history entries: stepping Back through them would reload the
// framed app shell, which the server refuses to frame. Everywhere else this is a plain hash navigation.
function routeTo(hash) {
  const target = hash.startsWith('#') ? hash : '#' + hash;
  if (!embeddedAgentPane) { location.hash = target; return; }
  if (location.hash === target) return;
  history.replaceState(null, '', location.pathname + location.search + target);
  window.dispatchEvent(new HashChangeEvent('hashchange'));
  if (target.startsWith('#/global/context/')) agentWorkspaceReportParent({route: target.slice('#/global/context/'.length)});
}

// lazy: the render functions live in later script files
function identityAvatar(kind, name, meta = {}, label = name, tooltip = false) {
  const content = kind === 'agent' ? agentGlyph(meta, 14) : kind === 'person' ? '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="8" r="4"/><path d="M4 21a8 8 0 0 1 16 0"/></svg>' : esc(kind === 'founder' ? 'A' : kind === 'business' ? String(name || '·').charAt(0).toUpperCase() : '?');
  return `<span class="identity-avatar identity-${kind}" role="img" ${tooltip ? `title="${esc(label || name)}"` : ''} aria-label="${esc(label || name || 'Unknown identity')}">${content}</span>`;
}

// Task/signoff scope identity: names for businesses, one globe for Global.
function taskBusinessLabel(name, global = false) {
  if (global) return '<span class="identity-avatar identity-global" role="img" aria-label="Global" title="Global"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" aria-hidden="true"><circle cx="12" cy="12" r="9"/><ellipse cx="12" cy="12" rx="4" ry="9"/><path d="M3 12h18"/></svg></span>';
  const full = String(name || 'Unknown project'), chars = Array.from(full);
  const text = chars.length > 15 ? chars.slice(0, 14).join('') + '…' : full;
  return `<span class="business-label identity-business" title="${esc(full)}">${esc(text)}</span>`;
}

// Navigation glyphs shared by Tasks, Tables and the global page icons.
const NAV_PROGRESS_ICON = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18h6m-5 3h4M9 15c0-2-3-3-3-7a6 6 0 0 1 12 0c0 4-3 5-3 7H9Z"/></svg>';
const NAV_SETTINGS_ICON = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>';

// A business opens on its Tasks. Business agents are not resident, so an old
// saved Agents tab lands there too.
const TABS = { tasks: c => renderBusinessTasks(c), context: c => renderContext(c),
  progress: c => renderBusinessProgress(c),
  settings: c => renderSettings(c) };
let tab0 = ls("tab");
if (tab0 === "agents") tab0 = "tasks";
const contextTab0 = {tables: "_tables"}[tab0];
if (contextTab0) tab0 = "context";
const state = { scope: "business", globalTab: "agents", biz: null, bizs: [], tab: TABS[tab0] ? tab0 : "tasks", agent: null, agents: [], ctx: contextTab0 || null };
// Per-URL {etag, body} cache for GET reads (Stage 2 PR4): a matching
// If-None-Match lets the server return 304 without redoing the read, so an
// unchanged pane costs a small header round trip instead of a full body.
// LRU-bounded (200 entries) and cleared on business switch (selectBiz /
// selectGlobal) -- a slot for a business the tab just left is never worth
// keeping over room for the one it is entering.
const ETAG_CACHE_MAX = 200;
const etagCache = new Map();
// Simultaneous ordinary GETs share the wire response, never mutable JSON.
// Caller-specific options (especially cancellation) retain their own request.
const apiReadFlights = new Map();
let apiReadGeneration = 0;
function etagCacheTouch(key, entry) {
  etagCache.delete(key);
  etagCache.set(key, entry);
  if (etagCache.size > ETAG_CACHE_MAX) etagCache.delete(etagCache.keys().next().value);
}
function clearEtagCache() { etagCache.clear(); apiReadFlights.clear(); apiReadGeneration++; }
// Cached bodies are served again on later hits; clone so repeat callers never share
// mutable state with each other or with the object sitting in the cache.
const cloneJson = x => (typeof structuredClone === "function" ? structuredClone(x) : JSON.parse(JSON.stringify(x)));
const api = async (p, opt = {}) => {
  const url = "/api" + p;
  const method = (opt.method || "GET").toUpperCase();
  const cached = method === "GET" ? etagCache.get(url) : null;
  const generation = apiReadGeneration;
  const requestOptions = cached ? { ...opt, headers: { ...(opt.headers || {}), "If-None-Match": cached.etag } } : opt;
  let r;
  if (method === 'GET' && !Object.keys(opt).length) {
    let flight = apiReadFlights.get(url);
    if (!flight) {
      flight = fetch(url, requestOptions);
      apiReadFlights.set(url, flight);
      const clear = () => { if (apiReadFlights.get(url) === flight) apiReadFlights.delete(url); };
      flight.then(clear, clear);
    }
    r = (await flight).clone();
  } else r = await fetch(url, requestOptions);
  let body;
  if (cached && r.status === 304) {
    if (generation === apiReadGeneration) etagCacheTouch(url, cached);
    body = cloneJson(cached.body);
  } else {
    body = await r.json();
    const etag = method === "GET" && r.ok && r.headers.get("etag");
    if (etag && generation === apiReadGeneration) etagCacheTouch(url, { etag, body });
  }
  // Legacy views consume error text; core adapters still need the structured failure.
  if (body && typeof body === "object") {
    Object.defineProperty(body, "httpStatus", { value: r.status, enumerable: false });
    if (body.status === "error" && body.error && typeof body.error === "object") {
      Object.defineProperty(body, "coreError", { value: body.error, enumerable: false });
      body.error = body.error.message || "The request failed.";
    }
  }
  return body;
};
const post = (p, body, options = {}) => api(p, {...options, method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
function newRequestId() {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  // HTTP/Tailscale origins expose getRandomValues but not randomUUID.
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 15) | 64;
  bytes[8] = (bytes[8] & 63) | 128;
  const hex = Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}
function coreData(result) {
  if (!result || !["ok", "accepted"].includes(result.status) || result.error || !result.data) {
    const source = result && (result.coreError || result.error);
    const error = new Error(typeof source === "string" ? source : (source && source.message) || "The server returned an invalid response.");
    error.code = (source && source.code) || "invalid_response";
    error.details = (source && source.details) || {};
    error.httpStatus = result && result.httpStatus;
    throw error;
  }
  if (result.data.operational_timezone && typeof sjConfigure === 'function') sjConfigure(result.data);
  return result.data;
}
// A read that fails because the caller lost access (or the source is gone) is terminal: cached content for it must not stay
// visible. Everything else (network, 5xx, malformed response) is transient and keeps the last successful content.
const accessLost = error => [401, 403].includes(error?.httpStatus) || ['forbidden', 'policy_denied', 'invalid_principal'].includes(error?.code);
const sourceGone = error => error?.httpStatus === 404 || error?.code === 'not_found';
// Short, local feedback for a failed request. The backend's own wording (exceptions, codes, field names) stays in the
// backend; the only words passed through are the app's own signoff notice. `load` picks the read wording.
function uiFailure(error, {load = false} = {}) {
  const code = String(error?.code || '');
  if (code === 'signoff_required') return error.message;
  if (/conflict|stale|revision|precondition|state_changed/i.test(code)) return 'Changed since you opened it. Refresh';
  if (!code || code === 'invalid_response' || error?.httpStatus >= 500) return load ? "Couldn't load. Retry" : "Couldn't confirm. Try again";
  return load ? "Couldn't load. Retry" : "Couldn't save. Try again";
}
// One shared owner-session read for every view that needs the caller's identity.
// Until it lands, views assume the installation owner: the safe default, since
// that keeps the owner's private channel and the read-only agent discussion.
state.session = null;
let sessionRequest = null;
function loadSession(force = false) {
  if (!force && state.session) return Promise.resolve(state.session);
  if (!sessionRequest) sessionRequest = api("/session")
    .then(body => { state.session = coreData(body); return state.session; })
    .catch(() => null)
    .finally(() => { sessionRequest = null; });
  return sessionRequest;
}
const evUrl = p => "/api" + p;
// ---------------------------------------------------------------- notifications
// One queue for every toast: centered at the top, up to NOTE_MAX on screen, the rest wait in
// FIFO order. A note stays NOTE_MS (hovering holds it, a click dismisses it), then leaves and the
// next one takes its slot. A backlog longer than NOTE_QUEUE_MAX folds its tail into one "N more" note so a
// burst never floods the view. `notify({text, sub?, color?, ms?, reminder?, onClick?})`; `toast(text)` is the short form.
const NOTE_MAX = 3, NOTE_MS = 4200, NOTE_QUEUE_MAX = 6;
const _notes = { queue: [], live: 0, replies: new Map() };
function notify(n) {
  if (typeof n === "string") n = { text: n };
  if (!n || !n.text) return;
  // A reply card is keyed by agent: a newer reply updates the pending or visible card instead of stacking.
  if (n.replyAgentId) {
    const shown = _notes.replies.get(n.replyAgentId);
    if (shown) { shown.update(n); return; }
    const pending = _notes.queue.find(note => note.replyAgentId === n.replyAgentId);
    if (pending) { Object.assign(pending, n); return; }
  }
  _notes.queue.push(n);
  const ordinary = _notes.queue;
  if (ordinary.length > NOTE_QUEUE_MAX && !ordinary.some(note=>note.onClick)) {
    const keep = ordinary.slice(0, NOTE_QUEUE_MAX - 1), rest = ordinary.slice(NOTE_QUEUE_MAX - 1);
    const folded = rest.reduce((k, x) => k + (x.n || 1), 0);
    _notes.queue = keep.concat([{ text: `${folded} more`, sub: "in the log", n: folded, fold: true }]);
  }
  notePump();
}
// Compact reply card: the shared agent glyph, the agent name and one preview line. Text is escaped; the
// icon comes only from the trusted glyph renderer.
function replyToastHTML(n) {
  return `<span class="reply-icon">${agentGlyph({agent_id:n.replyAgentId}, 28)}</span><span class="reply-copy"><span class="reply-name">${esc(n.text)}</span><span class="reply-preview">${esc(n.sub || "")}</span></span>`;
}
// Reminder card: same compact card as a reply, with a bell in place of the agent glyph and a Reminder label.
function reminderToastHTML(n) {
  return `<span class="reminder-icon" aria-hidden="true"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"><path d="M18 8a6 6 0 0 0-12 0c0 7-3 7-3 9h18c0-2-3-2-3-9Z"/><path d="M10 21h4"/></svg></span><span class="reply-copy"><span class="reply-name">Reminder</span><span class="reply-preview">${esc(n.text)}</span></span>`;
}
function notePump() {
  const box = $("#toasts"); if (!box) return;
  while (_notes.live < NOTE_MAX && _notes.queue.length) {
    const n = _notes.queue.shift(); _notes.live++;
    const el = document.createElement("div");
    el.className = "toast in" + (n.fold ? " fold" : "") + (n.reminder ? " reminder-toast" : "") + (n.replyAgentId || n.reminder ? " reply-toast" : "");
    if (n.color) el.style.setProperty("--c", n.color);
    if (n.reminder) { el.setAttribute("aria-label", `Reminder: ${n.text}`); el.title = n.text; }
    el.innerHTML = n.reminder ? reminderToastHTML(n) : n.replyAgentId ? replyToastHTML(n) : `<div class="tx">${esc(n.text)}</div>${n.sub ? `<div class="tsub">${esc(n.sub)}</div>` : ""}`;
    box.appendChild(el);
    requestAnimationFrame(() => requestAnimationFrame(() => el.classList.remove("in")));
    let t, gone = false, deadline = 0, remaining = n.ms || NOTE_MS, held = 0, dismissing = false;
    const go = () => { if (gone) return; gone = true; clearTimeout(t); if (n.replyAgentId && _notes.replies.get(n.replyAgentId)?.el === el) _notes.replies.delete(n.replyAgentId); el.classList.add("out"); setTimeout(() => { el.remove(); _notes.live--; notePump(); }, 260); };
    const arm = (ms = n.ms || NOTE_MS) => { clearTimeout(t); remaining = ms; deadline = Date.now() + ms; t = setTimeout(go, ms); };
    if (n.replyAgentId || n.reminder) {
      // Hover and keyboard focus each hold the card; the remaining time resumes once both end.
      const hold = () => { if (held++ === 0) { remaining = Math.max(0, deadline - Date.now()); clearTimeout(t); } };
      const release = () => { held = Math.max(0, held - 1); if (held === 0 && !gone && !dismissing) arm(remaining); };
      el.addEventListener("mouseenter", hold); el.addEventListener("mouseleave", release);
      el.addEventListener("focusin", hold); el.addEventListener("focusout", release);
      if (n.replyAgentId) _notes.replies.set(n.replyAgentId, { el, update(next) {
        Object.assign(n, next);
        el.innerHTML = replyToastHTML(n);
        remaining = n.ms || NOTE_MS;
        if (held === 0) arm(remaining);
      } });
    } else {
      el.addEventListener("mouseenter", () => clearTimeout(t));
      el.addEventListener("mouseleave", () => arm());
    }
    const activate=()=>{if(gone)return;go();if(n.onClick)Promise.resolve().then(()=>n.onClick()).catch(error=>toast(error.message));};
    el.addEventListener("click", activate);
    if (n.reminder && n.onDismiss) el.addEventListener("contextmenu", event => {
      event.preventDefault(); if (gone || dismissing) return;
      dismissing = true; clearTimeout(t);
      Promise.resolve().then(() => n.onDismiss()).then(go).catch(error => { toast(error.message || 'Could not dismiss reminder.'); })
        .finally(() => { dismissing = false; if (!gone && held === 0) arm(remaining); });
    });
    if(n.onClick){el.tabIndex=0;el.setAttribute('role','button');el.addEventListener('keydown',event=>{if(['Enter',' '].includes(event.key)){event.preventDefault();activate();}});}
    arm();
  }
}
const toast = m => notify(m);

// ---------------------------------------------------------------- byte formatting
// 204800 -> "200 KB" | 18874368 -> "18 MB" | 900 -> "900 B"
function fmtBytes(n) {
  n = Number(n) || 0;
  if (n < 1024) return `${n} B`;
  const f = x => (x < 10 ? x.toFixed(1).replace(/\.0$/, "") : String(Math.round(x)));
  if (n < 1048576) return `${f(n / 1024)} KB`;
  if (n < 1073741824) return `${f(n / 1048576)} MB`;
  return `${f(n / 1073741824)} GB`;
}
// ---------------------------------------------------------------- modal
// the upload glyph: the tasks panel's Uploads tab icon, and the leading icon on every upload button
const ICO_UPLOAD = `<svg class="ico-upload" xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path><polyline points="17 8 12 3 7 8"></polyline><line x1="12" y1="3" x2="12" y2="15"></line></svg>`;
const SVG_X = `<svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"><path d="M3.5 3.5l7 7M10.5 3.5l-7 7"/></svg>`;
const SVG_CHECK = `<svg width="12" height="12" viewBox="0 0 12 12" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M2.5 6.2 4.8 8.5 9.5 3.5"/></svg>`;

let _modal = null;
// beforeClose (optional, async) can refuse a user-initiated close (X, Escape, backdrop); isDirty (optional) marks a dialog whose
// unsaved work a replacing dialog must not discard, so the new one stacks above it instead.
function modal({ title = "", body = "", actions = [], width = 520, onOpen, onClose, stack = false, beforeClose, isDirty } = {}) {
  // A replacing dialog stacks (rather than disposing anything) whenever any dialog in the open chain holds unsaved work, so a clean
  // child can never authorize discarding a dirty ancestor.
  const chainDirty = () => { for (let m = _modal; m; m = m.parent) if (m.el?.isConnected && m.isDirty?.()) return true; return false; };
  const parent = stack || chainDirty() ? _modal : null;
  const returnFocus = document.activeElement;
  if (!parent) while (_modal) _modal.close();
  const parentOverlay = parent?.el.closest('.overlay');
  if (parentOverlay) {parentOverlay.inert = true;parent.el.setAttribute('aria-hidden','true');}
  const ov = document.createElement("div");
  ov.className = "overlay";
  ov.innerHTML = `<div class="modal" style="width:${Number(width) || 520}px" role="dialog" aria-modal="true">
    <div class="mhead"><div class="mt">${title}</div><button class="mx" type="button" aria-label="Close">${SVG_X}</button></div>
    <div class="mbody">${body}</div>
    ${actions.length ? `<div class="mfoot"></div>` : ""}
  </div>`;
  const el = ov.firstElementChild;
  let closed = false;
  const close = () => {
    if (closed) return; closed = true;
    document.removeEventListener("keydown", onKey, true);
    ov.remove();
    if (_modal && _modal.el === el) _modal = parent?.el.isConnected ? parent : null;
    if (parentOverlay) {parentOverlay.inert = false;parent.el.removeAttribute('aria-hidden');}
    // A Global task editor mounted inside this dialog tears down its record surface and conversation with it.
    for (const node of el.querySelectorAll('*')) node._globalTasks?.dispose?.();
    if (returnFocus && returnFocus !== document.body && returnFocus.isConnected && !returnFocus.closest?.('.overlay[inert]')) { try { returnFocus.focus({ preventScroll: true }); } catch (e) {} }
    onClose?.();
  };
  // an open dropdown owns Escape (both listeners sit on document capture, so stopPropagation alone does not help)
  let asking = false;
  const requestClose = async () => {
    if (!beforeClose) return close();
    if (asking) return;
    asking = true;
    try { if (await beforeClose()) close(); } finally { asking = false; }
  };
  const onKey = e => { if (e.key === "Escape" && !_menu && _modal?.el === el) { e.stopPropagation(); requestClose(); } };
  const foot = el.querySelector(".mfoot");
  actions.forEach(a => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = a.primary ? "primary" : "ghost";
    if (a.icon) { b.innerHTML = a.icon; b.append(a.label || ""); } else { b.textContent = a.label || ""; }
    b.onclick = () => { try { a.onClick && a.onClick(close, el); } catch (err) { console.error(err); } };
    foot.appendChild(b);
  });
  el.querySelector(".mx").onclick = requestClose;
  ov.onmousedown = e => { if (e.target === ov) requestClose(); };
  document.addEventListener("keydown", onKey, true);
  document.body.appendChild(ov);
  _modal = { el, close, isDirty, parent };
  const f = el.querySelector("input, textarea, select, .selbtn");
  if (f) try { f.focus(); } catch (e) {}
  if (onOpen) try { onOpen(el); } catch (e) { console.error(e); }
  return { el, close };
}

// ---------------------------------------------------------------- dropdown
let _menu = null, _menuSequence = 0;
function dropdown(anchorEl, items, onPick, current, options = {}) {
  if (_menu) _menu.close();
  const list = (items || []).map(it => (typeof it === "string" ? { value: it, label: it } : it));
  const m = document.createElement("div");
  m.id = `app-menu-${++_menuSequence}`;
  m.setAttribute("role", "listbox");
  m.className = "menu" + (options.className ? " " + options.className : "");
  if (options.width) m.style.width = options.width + "px";
  m.innerHTML = list.map((it, i) => `<div class="mi ${it.value === current ? "on" : ""}" data-i="${i}" id="${m.id}-${i}" role="option" aria-selected="${it.value === current}" aria-disabled="${!!it.disabled}">
    ${options.noCheck ? "" : `<span class="ck">${SVG_CHECK}</span>`}<span class="lb">${it.html != null ? it.html : esc(it.label != null ? it.label : it.value)}</span></div>`).join("")
    || `<div class="mi" style="opacity:.5;cursor:default">nothing</div>`;
  document.body.appendChild(m);
  if (anchorEl && anchorEl.classList) anchorEl.classList.add("open");

  const r = anchorEl ? anchorEl.getBoundingClientRect() : { left: 20, right: 200, top: 20, bottom: 20, width: 180 };
  m.style.minWidth = Math.min(Math.max(r.width, 160), window.innerWidth - 24) + "px";
  const h = m.offsetHeight, w = m.offsetWidth;
  let top = r.bottom + 6;
  if (top + h > window.innerHeight - 12) top = Math.max(12, r.top - h - 6);
  let left = r.left;
  if (left + w > window.innerWidth - 12) left = Math.max(12, window.innerWidth - w - 12);
  m.style.top = top + "px"; m.style.left = left + "px";

  let hi = Math.max(0, list.findIndex(it => it.value === current));
  const rows = [...m.querySelectorAll(".mi[data-i]")];
  if (anchorEl) anchorEl.setAttribute('aria-controls',m.id);
  const paint = () => {
    rows.forEach((el, i) => el.classList.toggle("hi", i === hi));
    if (anchorEl && rows[hi]) anchorEl.setAttribute('aria-activedescendant',rows[hi].id);
  };
  paint();

  let closed = false;
  const close = () => {
    if (closed) return; closed = true;
    document.removeEventListener("keydown", onKey, true);
    document.removeEventListener("mousedown", onDown, true);
    window.removeEventListener("resize", close); window.removeEventListener("scroll", onScroll, true);
    if (anchorEl && anchorEl.classList) anchorEl.classList.remove("open");
    m.remove(); if (_menu && _menu.el === m) _menu = null;
    if (anchorEl) { anchorEl.removeAttribute('aria-controls'); anchorEl.removeAttribute('aria-activedescendant'); }
    if (options.onClose) options.onClose();
  };
  const pick = i => { const it = list[i]; if (!it || it.disabled) return; close(); if (it && onPick) onPick(it.value, it); };
  const nextEnabled = (index, direction) => {
    for (let n=0;n<rows.length;n++) { index=(index+direction+rows.length)%rows.length; if (!list[index].disabled) return index; }
    return Math.max(0,index);
  };
  let typed = '', typedAt = 0;
  const onKey = e => {
    if (options.allowTyping && e.key.length === 1) return;
    if (!rows.length) { if (['Escape','Tab'].includes(e.key)) close(); return; }
    if (e.key === "Tab") { close(); return; }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); close(); }
    else if (e.key === "ArrowDown") { e.preventDefault(); e.stopPropagation(); hi = nextEnabled(hi, 1); paint(); rows[hi].scrollIntoView({ block: "nearest" }); }
    else if (e.key === "ArrowUp") { e.preventDefault(); e.stopPropagation(); hi = nextEnabled(hi, -1); paint(); rows[hi].scrollIntoView({ block: "nearest" }); }
    else if (e.key === "Enter" || e.key === " ") { e.preventDefault(); e.stopPropagation(); pick(hi); }
    else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      e.preventDefault(); e.stopPropagation();
      typed = (Date.now()-typedAt<700 ? typed : '') + e.key.toLowerCase(); typedAt=Date.now();
      const match=list.findIndex(it=>!it.disabled && String(it.label ?? it.value).toLowerCase().startsWith(typed));
      if(match>=0){hi=match;paint();rows[hi].scrollIntoView({block:'nearest'});}
    }
  };
  const onScroll = e => { if (!m.contains(e.target)) close(); };
  const onDown = e => { if (!m.contains(e.target) && e.target !== anchorEl && !(anchorEl && anchorEl.contains(e.target))) close(); };
  rows.forEach((el, i) => { el.onclick = () => pick(i); el.onmouseenter = () => { if (!list[i].disabled) { hi = i; paint(); } }; });
  document.addEventListener("keydown", onKey, true);
  setTimeout(() => { if (!closed) document.addEventListener("mousedown", onDown, true); }, 0);
  window.addEventListener("resize", close); window.addEventListener("scroll", onScroll, true);
  _menu = { el: m, close };
  return { el: m, close };
}

// ---------------------------------------------------------------- tooltip
let _tipEl = null, _tipT = null;
function _tipHide() { clearTimeout(_tipT); if (_tipEl) { _tipEl.remove(); _tipEl = null; } }
function _tipQuietControl(el) {
  const control = el?.closest?.('button,[role="button"],[role="tab"],a[aria-label],input[type="button"],input[type="submit"]');
  // Table cell hints describe data, not an interface control. Prose and other
  // non-control hints retain their existing behavior inside embedded screens.
  if (!control || control.classList.contains('data-cell')) return null;
  const quiet = control.closest('.agpad,.agsplit > .listcol,[role="navigation"],[role="tablist"],.ttabs')
    || document.body?.classList.contains('agent-pane-embedded');
  return quiet ? control : null;
}
function tip(el, htmlOrFn) {
  if (!el) return;
  el._tipHtml = htmlOrFn;
  if (el._tipBound) return el;
  el._tipBound = true;
  const show = () => {
    if (_tipQuietControl(el)) { _tipHide(); return; }
    clearTimeout(_tipT);
    _tipT = setTimeout(() => {
      if (!el.isConnected) return;
      const src = el._tipHtml;
      let html; try { html = typeof src === "function" ? src(el) : src; } catch (e) { html = ""; }
      if (html == null || html === "") return;
      _tipHide();
      const t = document.createElement("div");
      t.className = "tip"; t.setAttribute("role", "tooltip"); t.innerHTML = html;
      document.body.appendChild(t); _tipEl = t;
      const r = el.getBoundingClientRect(), w = t.offsetWidth, h = t.offsetHeight, gap = 8;
      let left, top;
      if (r.left > window.innerWidth * 0.6) { left = r.left - w - gap; top = r.top; }
      else { left = r.left; top = r.bottom + gap; }
      if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - gap);
      left = Math.min(Math.max(8, left), window.innerWidth - w - 8);
      top = Math.min(Math.max(8, top), window.innerHeight - h - 8);
      t.style.left = left + "px"; t.style.top = top + "px";
    }, 120);
  };
  el._tipShow = show;
  el.addEventListener("mouseenter", show);
  el.addEventListener("focusin", show);
  el.addEventListener("focusout", _tipHide);
  el.addEventListener("mouseleave", _tipHide);
  el.addEventListener("mousedown", _tipHide);
  return el;
}

// ---------------------------------------------------------------- businesses
let workspaceNavigation = 0;
async function loadBiz({ preserveNavigation = false } = {}) {
  await loadSession();
  try {
    const businesses = await api("/biz");
    if (!Array.isArray(businesses)) throw new Error(businesses && businesses.error || "Could not load projects.");
    state.bizs = businesses;
    if (preserveNavigation) {
      if (state.biz && !businesses.some(b => b.slug === state.biz)) await selectGlobal();
      return;
    }
    const destination = location.hash || ls('workspace-route') || '';
    const legacy = ovLegacy(destination);
    if (legacy !== undefined) ovOpenLegacy(legacy);
    else if (destination.startsWith("#/global")) {
      location.hash = destination;
      selectGlobal(destination.split("/")[2]);
    } else if (!state.biz) {
      const business = state.bizs.find(b => b.slug === destination.slice(1));
      if (business) selectBiz(business.slug);
      else selectGlobal('agents');
    }
  } catch (error) {
    $("#content").innerHTML = `<div class="empty"><h2>Could not load the dashboard</h2><p>${esc(error.message)}</p><button class="btn" id="business-retry">Try again</button></div>`;
    $("#business-retry").onclick = loadBiz;
  }
}
async function selectBiz(slug) {
  // An embedded pane never changes the surrounding app's scope; the pane's Business management view takes the business.
  if (embeddedAgentPane) return window.parent.agentWorkspaceEscape?.({business: slug});
  if (typeof agentWorkspaceCanLeave === 'function' && !await agentWorkspaceCanLeave(_agpad)) return;
  if(!await ntFlush())return;
  const b = state.bizs.find(x => x.slug === slug); if (!b) return;
  workspaceNavigation++;
  // An old business agent link opens the business Tasks; no business agent is selected or started.
  if(location.hash==='#'+slug && new URLSearchParams(location.search).has('agent')){
    const url=new URL(location.href);url.searchParams.delete('agent');history.replaceState(null,'',url);
    state.tab='tasks';
  }
  state.scope = "business";
  state.biz = slug;
  clearEtagCache();
  syncWorkspaceNav();
  if (typeof sgBusinessChanged === "function") sgBusinessChanged();
  state.ctx = null; location.hash = slug;
  ls('workspace-route', location.hash);
  renderTab(); refreshSide();
}
const WORKSPACE_ORDER_KEY = 'workspace-business-order:v1';
const workspaceBusinessId = b => b.id || b.business_id || b.slug;
function workspaceBusinesses() {
  let order;
  try { order = JSON.parse(localStorage.getItem(WORKSPACE_ORDER_KEY) || '[]'); } catch (_) { order = []; }
  if (!Array.isArray(order)) order = [];
  const rank = new Map(order.map((id, i) => [id, i]));
  return [...state.bizs].sort((a, b) => (rank.get(workspaceBusinessId(a)) ?? Infinity) - (rank.get(workspaceBusinessId(b)) ?? Infinity));
}
async function goTab(name) {
  if (typeof agentWorkspaceCanLeave === 'function' && !await agentWorkspaceCanLeave(_agpad)) return;
  if (name === "agents") name = "tasks";
  if (state.scope === "global") return;
  const contextPage = name === "tables" ? "_tables" : null;
  if (contextPage) name = "context";
  if (!Object.hasOwn(TABS, name)) return;
  if (state.tab === name && !contextPage) return;
  if (contextPage) state.ctx = contextPage;
  state.tab = name; ls("tab", state.tab);
  await renderTab();
}

// Only explicitly page-owned reads use this signal; writes and persistent drawer reads keep their lifecycle.
let pageReads = new AbortController();
const pageRead = (path, options = {}) => api(path, {...options, signal: options.signal || pageReads.signal});
const pageReadPost = (path, body, signal = pageReads.signal) => post(path, body, {signal});
function renderTab() {
  pageReads.abort(); pageReads = new AbortController();
  globalAgentsGeneration++;
  if (typeof rtSuspend === 'function') rtSuspend();
  if (typeof ctxClearGraph === "function") ctxClearGraph();
  if (typeof agpadDispose === "function") agpadDispose();
  if (typeof agentViewDispose === "function") agentViewDispose();
  const c = $("#content"); c.innerHTML = "";
  if (typeof teardownTerm === "function") teardownTerm();
  if (typeof agentWorkspaceRenderEmbedded === 'function' && agentWorkspaceRenderEmbedded(c)) return;
  if (state.scope === "global") return renderGlobal(c);
  if (!state.biz) return;
  return (TABS[state.tab] || TABS.tasks)(c);
}
