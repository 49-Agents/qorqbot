// Agent tasks use the drawer's shared task UI.
// Task clicks open the existing drawer detail; business writes stay in tasks.js.
const AGPAD_LOG_N = 200, AGPAD_POLL_MS = 12000;
const AGENT_PANE_RATIO_KEY = "agent-pane-ratio";
const AGENT_PANE_DEFAULT_RATIO = 1 / 2.15;
const AGENT_PANE_MIN_TERMINAL = 260, AGENT_PANE_MIN_OVERVIEW = 240;
let _agpad = null;

// ------------------------------------------------------------ workspace registry
// The pane's views are declared by core's workspace_types read; the built-in
// declaration keeps Focus available when that read is unavailable.
const WORKSPACE_BASE_VIEWS = new Set(["tasks", "inbox", "activity", "status", "globalTasks"]);
const WORKSPACE_SEED = [
  { key: "focus", display_name: "Focus", supported_roles: ["ordubashy", "global-worker"], renderer: "focus_board",
    presentation_actions: ["focus_show", "focus_clear", "focus_browser_input", "focus_present", "focus_edit",
      "focus_move", "focus_set_archived", "focus_reply"] },
];
// Renderers register themselves (core's Focus below, installed extensions through
// their same-origin assets). A registration only supplies behavior and static
// display metadata; availability still comes from the server's workspace_types.
const WORKSPACE_RENDERERS = new Map();
function registerWorkspaceRenderer(renderer, definition) {
  const fn = value => value === undefined || typeof value === "function";
  if (typeof renderer !== "string" || !/^[a-z][a-z0-9_]{0,63}$/.test(renderer)) throw new Error("Workspace renderer name is malformed.");
  if (WORKSPACE_RENDERERS.has(renderer)) throw new Error(`Workspace renderer ${renderer} is already registered.`);
  if (!definition || typeof definition !== "object" || typeof definition.mount !== "function"
      || !fn(definition.tick) || !fn(definition.dispose)
      || (definition.leading !== undefined && typeof definition.leading !== "boolean")
      || !definition.views || typeof definition.views !== "object" || Array.isArray(definition.views)) {
    throw new Error(`Workspace renderer ${renderer} has a malformed definition.`);
  }
  const views = Object.entries(definition.views);
  for (const [key, view] of views) {
    if (!/^[a-z][a-z0-9_]{0,63}$/.test(key) || !view || typeof view.label !== "string" || !view.label || typeof view.icon !== "string") {
      throw new Error(`Workspace renderer ${renderer} has a malformed view.`);
    }
  }
  WORKSPACE_RENDERERS.set(renderer, { mount: definition.mount, tick: definition.tick, dispose: definition.dispose, leading: !!definition.leading });
  if (typeof T_VIEW_LABEL === "object" && typeof T_VIEW_ICON === "object") {
    for (const [key, view] of views) { T_VIEW_LABEL[key] = view.label; T_VIEW_ICON[key] = view.icon; }
  }
}
window.registerWorkspaceRenderer = registerWorkspaceRenderer;
// Each renderer owns its cache and poll gate.
registerWorkspaceRenderer("focus_board", {
  mount: p => {
    // A remount keeps this pane's same-page data and its poll gate; the existing interval and invalidations decide when to read.
    if (!p.focus || p.focusPageKey !== agfocusPageKey(p)) { p.focus = null; p.focusPageKey = null; p.nextFocus = 0; }
    agfocusMount(p);
    const scroll = p.focusScroll; p.focusScroll = 0;
    const list = $(".agfocus-layout", p.el);
    if (scroll && p.focus && list) list.scrollTop = scroll;
  }, tick: p => agfocusTick(p), leading: true,
  views: { focus: { label: "Pinned", icon: typeof T_VIEW_ICON === "object" ? T_VIEW_ICON.focus || "" : "" } },
});

// Installed extensions' UI assets: the owner-only manifest names same-origin files
// beneath /extensions/<id>/assets/, each loaded once. A failure leaves that
// extension's workspace on the ordinary unsupported state.
let extensionLoadPending = null;
const EXTENSION_ASSET = /^\/extensions\/([a-z][a-z0-9_-]{0,63})\/assets\/[A-Za-z0-9._-]+\.(js|css)$/;
function extensionAssets(entry, kind) {
  const ext = kind === "scripts" ? "js" : "css";
  const list = entry.assets && entry.assets[kind];
  if (list === undefined) return [];
  if (!Array.isArray(list)) throw new Error("Extension asset list is malformed.");
  return [...new Set(list)].map(path => {
    const match = typeof path === "string" && !path.includes("..") ? EXTENSION_ASSET.exec(path) : null;
    if (!match || match[1] !== entry.id || match[2] !== ext) throw new Error(`Extension asset ${path} is not allowed.`);
    return path;
  });
}
function extensionAssetAdd(path, kind) {
  return new Promise((resolve, reject) => {
    const node = document.createElement(kind === "scripts" ? "script" : "link");
    if (kind === "scripts") node.src = path; else { node.rel = "stylesheet"; node.href = path; }
    node.onload = () => resolve();
    node.onerror = () => reject(new Error(`Extension asset ${path} did not load.`));
    (document.head || document.body).appendChild(node);
  });
}
function extensionsLoad() {
  if (extensionLoadPending) return extensionLoadPending;
  extensionLoadPending = (async () => {
    const entries = coreData(await api("/root/extensions")).extensions;
    if (!Array.isArray(entries)) return;
    const seen = new Set();
    for (const entry of entries) {
      if (!entry || typeof entry.id !== "string" || seen.has(entry.id)) continue;
      seen.add(entry.id);
      try {
        const scripts = extensionAssets(entry, "scripts"), styles = extensionAssets(entry, "styles");
        await Promise.all(styles.map(path => extensionAssetAdd(path, "styles")));
        for (const path of scripts) await extensionAssetAdd(path, "scripts");
      } catch (_) { /* that extension stays unavailable */ }
    }
  })().catch(() => {});
  return extensionLoadPending;
}
let workspaceDeclaration = null, workspaceDeclarationAsked = false, workspaceDeclarationPending = null;

function normalizeWorkspaceType(value) {
  if (!value || typeof value !== "object" || typeof value.key !== "string" || !value.key
      || value.key === "threads" || value.renderer === "threads") return null;
  const strings = list => Array.isArray(list) ? list.filter(item => typeof item === "string") : [];
  return { key: value.key, display_name: typeof value.display_name === "string" ? value.display_name : value.key,
    supported_roles: strings(value.supported_roles), renderer: typeof value.renderer === "string" ? value.renderer : "",
    presentation_actions: strings(value.presentation_actions) };
}
function workspaceTypes() { return (workspaceDeclaration || WORKSPACE_SEED).filter(type => type.key !== "threads" && type.renderer !== "threads"); }
function workspaceType(key) { return workspaceTypes().find(type => type.key === key) || null; }
function workspaceRenderer(type) { return type && WORKSPACE_RENDERERS.get(type.renderer)?.mount || null; }
function workspaceViewsFor(roleId) { return roleId ? workspaceTypes().filter(type => type.supported_roles.includes(roleId)) : []; }
// Anything the pane's base views do not own is a workspace view, mountable only
// through the registry (or shown as an honest unsupported placeholder).
function agpadWorkspaceView(view) { return !WORKSPACE_BASE_VIEWS.has(view); }

// The old Business (Project management) entry and extension-declared campaign_review type never add a
// right-pane tab or restore a stale pane selection; Business keeps its stable key so links work. Project data
// stays reachable through the existing Global Projects page and Project records.
const AGPAD_RETIRED_PANE_VIEWS = new Set(["business", "campaign_review"]);
// Browser appears for any agent with a configured browser binding; the full
// Desktop remains Ordubashy's alone (core also enforces this on the binding).
// Viewbox is offered to registered agents except Learning Applier (the owner-only board is keyed by agent id).
function agentPaneViewAllowed(roleId, view) {
  return view !== "threads" && !(view === "journal" && roleId !== "ordubashy") && !AGPAD_RETIRED_PANE_VIEWS.has(view)
    && !(view === "viewbox" && roleId === "learning-applier");
}
function agentPaneViews(scope, roleId, base = [], browser = false, agentId = null) {
  const leading = [], trailing = [];
  for (const type of workspaceViewsFor(roleId)) (WORKSPACE_RENDERERS.get(type.renderer)?.leading ? leading : trailing).push(type.key);
  const personal = typeof agentWorkspaceViews === 'function' ? agentWorkspaceViews(scope, roleId) : [];
  leading.unshift(...personal.filter(view => view !== 'journal'));
  // Ordubashy has one Browser entry whose Browser/Desktop mode is chosen in the viewer.
  if (roleId === "ordubashy") leading.push("attentionBoard");
  if (roleId === "learning-applier") leading.push("changes");
  if (browser || roleId === "ordubashy") leading.push("browser");
  if (roleId === "ordubashy") leading.push("desktop");
  return [...new Set([...leading, ...base, ...personal.filter(view => view === 'journal'), ...(agentId ? ["viewbox"] : []), ...trailing, "status"])]
    .filter(view => agentPaneViewAllowed(roleId, view));
}
// The workspace preview groups the existing Activity and Inbox renderers behind
// one header entry. Their internal view keys keep polling and deep links intact.
const agpadOverviewView = view => ["status", "inbox", "activity"].includes(view);
function agpadCombinedOverview(p) { return !!document.body?.classList?.contains("agent-workspace-app"); }
// The header shows one Browser entry for Ordubashy's Browser and Desktop modes.
// For any agent with the Focus workspace, Browser, Desktop and Viewbox are details of its Focus: the Focus strip opens them, so they have no header entries.
const AGPAD_FOCUS_SURFACES = new Set(["browser", "desktop", "viewbox"]);
function agpadFocusSurfaces(p) { return !!p.agentId && p.views.includes("focus"); }
function agpadHeadViews(p) {
  const views = (agpadFocusSurfaces(p) ? p.views.filter(view => !AGPAD_FOCUS_SURFACES.has(view))
    : p.workspaceRole === "ordubashy" ? p.views.filter(view => view !== "desktop") : p.views)
    .filter(view => agentPaneViewAllowed(p.workspaceRole, view));
  if (!agpadCombinedOverview(p)) return views;
  const header = [...new Set(views.map(view => agpadOverviewView(view) ? "status" : view))];
  if (header.includes("focus") && header.includes("viewbox")) {
    header.splice(header.indexOf("viewbox"), 1);
    header.splice(header.indexOf('focus') + 1, 0, "viewbox");
  }
  return header;
}
function agpadHeadActive(p, view) {
  if (agpadCombinedOverview(p) && agpadOverviewView(view)) return "status";
  if (agpadFocusSurfaces(p) && AGPAD_FOCUS_SURFACES.has(view)) return "focus";
  return view === "desktop" && p.workspaceRole === "ordubashy" ? "browser" : view;
}
function agpadModeKey(p) { return p.agentId ? `agpad:mode:${p.agentId}` : null; }
// Selecting the Browser entry reopens the mode the owner last used for this agent.
function agpadTabView(p, view) {
  if (agpadCombinedOverview(p) && view === "status") return p.overviewView || "status";
  if (view !== "browser" || p.workspaceRole !== "ordubashy" || !p.views.includes("desktop")) return view;
  const key = agpadModeKey(p);
  return (p.browserMode || (key && ls(key))) === "desktop" ? "desktop" : "browser";
}
function agpadBrowserTab(p) { return typeof agbrowserTab === "function" && agbrowserTab(p); }

function agpadOverviewPaint(p) {
  p.el.querySelectorAll("[data-agent-overview]").forEach(button => {
    button.setAttribute("aria-pressed", String(button.dataset.agentOverview === "inbox" ? p.view === "inbox" : p.view !== "inbox"));
  });
}

function agpadOverviewMount(p) {
  if (!agpadCombinedOverview(p)) return;
  const tab = p.el.querySelector('.ttab[data-v="status"]');
  if (tab) {
    tab.setAttribute("aria-label", "Activity and inbox");
    tab.innerHTML = T_VIEW_ICON.activity;
  }
  if (!agpadOverviewView(p.view)) return;
  const toggle = document.createElement("div");
  toggle.className = "agpad-overview-switch";
  toggle.setAttribute("role", "group");
  toggle.setAttribute("aria-label", "Agent activity and inbox");
  toggle.innerHTML = ["activity", "inbox"].map(view => `<button type="button" class="quiet" data-agent-overview="${view}" aria-controls="agpad-body" aria-pressed="${view === "inbox" ? p.view === "inbox" : p.view !== "inbox"}">${view === "inbox" ? "Inbox" : "Activity"}</button>`).join("");
  $("#agpad-body", p.el).before(toggle);
  toggle.querySelectorAll("[data-agent-overview]").forEach(button => {
    button.onclick = () => {
      const next = button.dataset.agentOverview === "inbox" ? "inbox" : p.overviewActivityView || "status";
      if (p.view === next) return;
      agpadSetView(p, next);
      p.el.querySelector(`[data-agent-overview="${button.dataset.agentOverview}"]`)?.focus({preventScroll: true});
    };
  });
}

function workspaceTypesLoad() {
  if (workspaceDeclarationAsked) return workspaceDeclarationPending;
  workspaceDeclarationAsked = true;
  // One best-effort read per page load; the built-in declaration is the fallback.
  // Extension assets load first so their renderers exist when the declared types appear.
  workspaceDeclarationPending = extensionsLoad().then(() => post("/root/operations/workspace_types", {})).then(result => {
    const types = coreData(result)?.types;
    if (Array.isArray(types)) workspaceDeclaration = types.map(normalizeWorkspaceType).filter(Boolean);
    return workspaceDeclaration;
  }).catch(() => workspaceDeclaration).finally(() => {
    workspaceDeclarationPending = null;
    if (_agpad) agpadWorkspaceViewsSync(_agpad);
  });
  return workspaceDeclarationPending;
}

function agpadWorkspaceViewsSync(p) {
  if (!p.workspaceRole) return;
  const views = agentPaneViews(p.workspaceScope, p.workspaceRole, p.workspaceBase, agpadBrowserTab(p), p.agentId);
  if (views.join("\u0000") === p.views.join("\u0000")) return;
  const before = agpadHeadViews(p).join("\u0000");
  p.views = views;
  // A nested Browser/Desktop/Viewbox surface survives view changes that leave the Focus header as it was.
  if (agpadFocusSurfaces(p) && p.view === "focus" && before === agpadHeadViews(p).join("\u0000")) return;
  agpadSetView(p, views.includes(p.view) ? p.view : views[0]);
}

function agpadWorkspaceMount(p, view) {
  if (!agentPaneViewAllowed(p.workspaceRole, view)) return;
  if (view === "attentionBoard" && p.workspaceRole === "ordubashy") { attentionBoardMount(p); return; }
  if (view === "changes" && p.workspaceRole === "learning-applier") { overseerMount(p); return; }
  if (typeof agentWorkspaceMount === 'function' && agentWorkspaceMount(p, view)) return;
  if (view === "desktop" && p.workspaceRole === "ordubashy") { agfocusDesktopMount(p); return; }
  if (view === "browser" && typeof agbrowserMount === "function") { agbrowserMount(p); return; }
  if (view === "viewbox" && typeof agviewboxMount === "function") { agviewboxMount(p); return; }
  const type = workspaceType(view);
  if (type && p.workspaceRole && !type.supported_roles.includes(p.workspaceRole)) { workspaceUnsupportedMount(p, type); return; }
  const mount = workspaceRenderer(type);
  if (mount) { mount(p); return; }
  workspaceUnsupportedMount(p, type);
}

function workspaceUnsupportedMount(p, type) {
  const body = $("#agpad-body", p.el);
  if (!body) return;
  const key = type ? type.key : p.view;
  const reason = !type ? `No workspace type named “${key}” is installed.`
    : p.workspaceRole && !type.supported_roles.includes(p.workspaceRole) ? `${type.display_name} is not available to this agent's role.`
    : !workspaceRenderer(type) ? `This dashboard has no renderer for ${type.display_name}${type.renderer ? ` (${type.renderer})` : ""}.`
    : `${type.display_name} cannot be shown here.`;
  body.innerHTML = `<section class="agpad-unsupported" role="status"><div class="global-eyebrow">Workspace</div>
    <h2>Unsupported workspace</h2><p>${esc(reason)}</p><p class="muted">Nothing is shown for “${esc(key)}” instead of an empty pane.</p>
    <button type="button" class="quiet" data-workspace-back>Back to ${esc(workspaceBaseLabel(p))}</button></section>`;
  const back = $("[data-workspace-back]", body);
  const first = p.views.find(view => WORKSPACE_BASE_VIEWS.has(view) && view !== "status") || "status";
  if (back) back.onclick = () => agpadSetView(p, first);
}

function workspaceBaseLabel(p) {
  return T_VIEW_LABEL[p.views.find(view => WORKSPACE_BASE_VIEWS.has(view) && view !== "status") || "status"] || "the pane";
}

// ?workspace=<key> opens that view once, the way ?focus_item opens a card today.
function agTakeLinkedWorkspace(hash) {
  if (hash && location.hash && location.hash !== hash) return null;
  const requested = new URLSearchParams(location.search || "").get("workspace");
  if (!requested) return null;
  const url = new URL(location.href);
  url.searchParams.delete("workspace");
  history.replaceState(null, "", url);
  return requested;
}

// A linked view must already be one of the pane's views or an installed workspace type. While the workspace
// declaration is still loading, an unrecognized key waits on that read (the default view stays usable) and is
// honored only if it turns out to be installed and the owner has not moved elsewhere. Anything else is ignored.
function agpadRequestView(p, view) {
  if (typeof view !== "string" || !/^[a-z][a-z0-9_]{0,63}$/.test(view)) return false;
  if (!agentPaneViewAllowed(p.workspaceRole, view)) return false;
  // Calendar and Tables live in the shared drawer: retired keys never add a tab (links translate first).
  if (view === "calendar" || view === "tables") return false;
  // The retired System Dev keys likewise never add a tab.
  if (view === "system_dev" || view === "incidents") return false;
  if (!p.views.includes(view) && !workspaceType(view)) {
    const loading = p.workspaceRole ? workspaceTypesLoad() : null;
    if (!loading) return false;
    if (!p.viewShown) agpadSetView(p, p.views[0]);
    const pending = p.pendingView = {view};
    loading.then(() => {
      if (p.pendingView !== pending) return;
      p.pendingView = null;
      if (_agpad === p && workspaceType(view)) agpadRequestView(p, view);
    });
    return true;
  }
  if (!p.views.includes(view)) p.views.push(view);
  agpadSetView(p, view);
  return true;
}

function agpadWorkspaceTick(p) {
  if ((p.view === "viewbox" || p.surface?.kind === "viewbox") && typeof agviewboxTick === "function") agviewboxTick(p);
  for (const view of p.views) {
    const type = workspaceType(view);
    const tick = type && WORKSPACE_RENDERERS.get(type.renderer)?.tick;
    if (typeof tick === "function") tick(p);
  }
}

function agentPaneDividerMount(root) {
  const divider = root?.querySelector?.("[data-agent-pane-divider]");
  const chat = root?.querySelector?.(".chat.termmode");
  const terminal = root?.querySelector?.(".global-agent-session") || chat;
  const pane = root?.querySelector?.(".agpad");
  if (!root || !divider || !chat || !terminal || !pane || !root.style) return () => {};

  let ratio = Number(ls(AGENT_PANE_RATIO_KEY));
  if (!Number.isFinite(ratio) || ratio <= 0 || ratio >= 1) ratio = AGENT_PANE_DEFAULT_RATIO;
  let preferredRatio = ratio, shield = null, dragging = false;
  const resize = typeof ResizeObserver === "function" ? new ResizeObserver(() => {
    if (!dragging) apply(preferredRatio);
  }) : null;
  const span = () => {
    const c = terminal.getBoundingClientRect(), p = pane.getBoundingClientRect();
    return { start: c.left, width: Math.max(0, c.width + p.width), divider: divider.getBoundingClientRect() };
  };
  const bounds = () => {
    const width = span().width;
    if (!width) return [0, 1];
    const low = AGENT_PANE_MIN_TERMINAL / width;
    const high = 1 - AGENT_PANE_MIN_OVERVIEW / width;
    return low <= high ? [low, high] : [.5, .5];
  };
  const apply = (next, save = false) => {
    const [low, high] = bounds();
    ratio = Math.min(high, Math.max(low, Number(next) || AGENT_PANE_DEFAULT_RATIO));
    root.style.setProperty("--ag-terminal-fr", `${ratio}fr`);
    root.style.setProperty("--ag-pane-fr", `${1 - ratio}fr`);
    divider.setAttribute("aria-valuemin", String(Math.round(low * 100)));
    divider.setAttribute("aria-valuemax", String(Math.round(high * 100)));
    divider.setAttribute("aria-valuenow", String(Math.round(ratio * 100)));
    if (save) { preferredRatio = ratio; ls(AGENT_PANE_RATIO_KEY, String(ratio)); }
    return ratio;
  };
  apply(ratio);

  const finish = () => {
    if (!dragging) return;
    dragging = false;
    divider.classList.remove("dragging");
    root.classList.remove("agent-pane-dragging");
    shield?.remove(); shield = null;
    preferredRatio = ratio;
    ls(AGENT_PANE_RATIO_KEY, String(ratio));
  };
  const move = event => {
    if (!dragging) return;
    const { start, width, divider: d } = span();
    if (width) apply((event.clientX - start - d.width / 2) / width);
  };
  const begin = event => {
    if (event.button !== 0) return;
    event.preventDefault();
    dragging = true;
    divider.classList.add("dragging");
    root.classList.add("agent-pane-dragging");
    shield = document.createElement("div");
    shield.className = "agent-pane-drag-shield";
    shield.addEventListener("pointermove", move);
    shield.addEventListener("pointerup", finish, { once: true });
    shield.addEventListener("pointercancel", finish, { once: true });
    document.body.appendChild(shield);
    divider.setPointerCapture?.(event.pointerId);
    window.addEventListener("blur", finish);
  };
  const key = event => {
    const step = .03;
    let next;
    if (event.key === "ArrowLeft") next = ratio - step;
    else if (event.key === "ArrowRight") next = ratio + step;
    else if (event.key === "Home") next = bounds()[0];
    else if (event.key === "End") next = bounds()[1];
    else return;
    event.preventDefault();
    apply(next, true);
  };
  divider.addEventListener("pointerdown", begin);
  divider.addEventListener("pointermove", move);
  divider.addEventListener("pointerup", finish);
  divider.addEventListener("pointercancel", finish);
  divider.addEventListener("keydown", key);
  root._agentPaneDividerRefresh = () => apply(preferredRatio);
  resize?.observe(root);
  return () => {
    finish();
    resize?.disconnect();
    window.removeEventListener("blur", finish);
    divider.removeEventListener("pointerdown", begin);
    divider.removeEventListener("pointermove", move);
    divider.removeEventListener("pointerup", finish);
    divider.removeEventListener("pointercancel", finish);
    divider.removeEventListener("keydown", key);
    if (root._agentPaneDividerRefresh) delete root._agentPaneDividerRefresh;
  };
}

function agpadDispose() {
  if (_agpad) agpadSurfaceDispose(_agpad);
  _agpad?.focusDetail?.dispose?.();
  if (_agpad?.workspaceFrame && typeof agentWorkspacePairedDetach === 'function') agentWorkspacePairedDetach(_agpad.workspaceFrame.contentWindow);
  else if (_agpad?.view === 'focus' && typeof agentWorkspacePairedRecord === 'function') agentWorkspacePairedRecord(window, null, null);
  _agpad?.workspaceDispose?.();
  for (const renderer of WORKSPACE_RENDERERS.values()) if (_agpad && renderer.dispose) renderer.dispose(_agpad);
  _agpad?.dividerDispose?.();
  _agpad = null;
}

function agpadGroups(tasks, name) {
  const actor = `agent:${name}`;
  const created = t => t.created_by ? t.created_by_kind === 'agent' && String(t.created_by_name || '').toLowerCase() === name.toLowerCase() : t.notes?.[0]?.by === actor;
  const unfinished = tasks.filter(t => !tClosed(t));
  const open = unfinished.filter(t => !taskSnoozed(t));
  const owned = open.filter(t => tAssignees(t).includes(actor));
  return {
    open: owned,
    // Lists carry creator attribution without downloading the entire note history.
    // Retain the first-note proxy only for older payloads without a creator ID.
    waiting: open.filter(t => !tAssignees(t).includes(actor) && created(t)),
    snoozed: unfinished.filter(t => taskSnoozed(t) && (tAssignees(t).includes(actor) || created(t))),
  };
}

// Completed reads may retain data while hidden; starting a poll still requires
// the default visible check and the exact same mounted agent pane.
function agpadActive(p, allowHidden = false) {
  return p && _agpad === p && p.agent && p.open && (allowHidden || !document.hidden) &&
    (p.isCurrent ? p.isCurrent() : state.tab === "agents" && state.biz === p.biz && state.agent === p.agent) &&
    p.el.isConnected && $("#agpad") === p.el;
}

function agpadMount(name, el, options = {}) {
  if (_agpad) agpadSurfaceDispose(_agpad);
  _agpad?.focusDetail?.dispose?.();
  if (_agpad?.workspaceFrame && typeof agentWorkspacePairedDetach === 'function') agentWorkspacePairedDetach(_agpad.workspaceFrame.contentWindow);
  else if (_agpad?.view === 'focus' && typeof agentWorkspacePairedRecord === 'function') agentWorkspacePairedRecord(window, null, null);
  _agpad?.workspaceDispose?.();
  _agpad?.dividerDispose?.();
  if (_agpad) _agpad.focusDetail = null;
  const workspaceScope = options.workspaceScope || (state.scope === "global" ? "global" : "business");
  const workspaceBase = options.views || ["inbox"];
  const p = _agpad = {
    biz: state.biz, agent: name, el, open: !!name && ls(`agpad:${state.biz}`) !== "off",
    view: "inbox",
    logs: null, logError: false, nextLog: 0, pending: false,
    tasks: null, source: state.tasks, tasksStarted: false, taskError: false, bodyHtml: null,
    views: ["inbox"], ...options,
    workspaceScope, workspaceBase, workspaceRole: options.workspaceRole || null,
  };
  p.open = !!name; // The pane stays available without a hide/show control.
  // Links to the retired Marketing "desktop" tab land in the agent's Browser view.
  let initialView = options.initialView || (typeof agentWorkspaceDefault === 'function' && agentWorkspaceDefault(p)) || agpadRememberedView(p);
  if (initialView === "desktop" && p.workspaceRole !== "ordubashy" && workspaceScope === "global") initialView = "browser";
  if (initialView === "browser") p.browserRequested = true;
  p.views = agentPaneViews(workspaceScope, p.workspaceRole, workspaceBase, agpadBrowserTab(p), p.agentId);
  if (p.workspaceRole) workspaceTypesLoad();
  if (typeof agbrowserDiscover === "function") agbrowserDiscover(p);
  p.dividerDispose = agentPaneDividerMount(el.closest?.(".agsplit"));
  agpadVisibility(p);
  if (!name) { el.innerHTML = ""; return; }
  el.onclick = e => {
    const row = e.target.closest(".trow");
    // Focus rows reuse the row look, but they are not tasks: their clicks open their own details.
    if (row && !row.closest(".agfocus, .attention-board") && agpadActive(p)) tOpenTask(row.dataset.id);
  };
  // A linked view is honored only when it is a usable key; anything else leaves
  // the pane on its normal first view instead of an empty pane.
  if (!initialView || !agpadRequestView(p, initialView)) agpadSetView(p, (typeof agentWorkspaceFallback === 'function' && agentWorkspaceFallback(p)) || p.views[0]);
}

// Browser/Desktop is remembered per registered agent, never shared between agents.
const AGPAD_REMEMBERED_VIEWS = new Set(["browser", "desktop"]);
function agpadViewKey(p) { return p.agentId ? `agpad:view:${p.agentId}` : null; }
function agpadRememberedView(p) {
  const key = agpadViewKey(p), view = key && ls(key);
  return AGPAD_REMEMBERED_VIEWS.has(view) ? view : null;
}
function agpadRememberView(p, view) {
  const key = agpadViewKey(p);
  if (key) ls(key, AGPAD_REMEMBERED_VIEWS.has(view) ? view : "");
}

function agpadSetView(p, view, leaveChecked = false) {
  if (_agpad !== p || !p.views.includes(view) || !agentPaneViewAllowed(p.workspaceRole, view)) return;
  if (agpadFocusSurfaces(p)) {
    // Browser, Desktop and Viewbox items open inside the mounted Focus shell; Focus itself closes them.
    if (AGPAD_FOCUS_SURFACES.has(view)) return agpadSurfaceRequest(p, view, leaveChecked);
    if (view === "focus" && p.view === "focus" && p.surface) { agpadSurfaceClose(p); return; }
  }
  if (!leaveChecked && p.workspaceFrame && typeof agentWorkspaceCanLeave === 'function') {
    const frame = p.workspaceFrame;
    return agentWorkspaceCanLeave(p).then(ok => {
      if (ok && _agpad === p && p.workspaceFrame === frame) agpadSetView(p, view, true);
    });
  }
  if (p.workspaceFrame && typeof agentWorkspacePairedDetach === 'function') agentWorkspacePairedDetach(p.workspaceFrame.contentWindow);
  else if (p.view === 'focus' && typeof agentWorkspacePairedRecord === 'function') agentWorkspacePairedRecord(window, null, null);
  if (p.view === 'focus' && view !== p.view) p.focusScroll = $('.agfocus-layout', p.el)?.scrollTop || 0;
  agpadSurfaceDispose(p);
  p.focusDetail?.dispose?.();
  p.focusDetail = null;
  p.workspaceDispose?.();
  p.workspaceDispose = null;
  const from = p.view;
  const overviewSwitch = view !== from && agpadCombinedOverview(p) && agpadOverviewView(from) && agpadOverviewView(view)
    && p.el.querySelector(".agpad-overview-switch");
  if (from === "desktop") p.desktopRequest = (p.desktopRequest || 0) + 1;
  if (from === "browser") p.browserRequest = (p.browserRequest || 0) + 1;
  if (view !== from) p.pendingView = null;
  p.view = view; p.viewShown = true;
  if (typeof agentWorkspaceViewChanged === 'function') agentWorkspaceViewChanged(p);
  if (agpadCombinedOverview(p) && agpadOverviewView(view)) {
    p.overviewView = view;
    if (view !== "inbox") p.overviewActivityView = view;
  }
  if (view === "browser" || view === "desktop") {
    p.browserMode = view;
    const modeKey = agpadModeKey(p);
    if (modeKey) ls(modeKey, view);
  }
  agpadRememberView(p, view);
  p.bodyHtml = null;
  if (overviewSwitch) {
    // The subview owns only the body. Preserve the main tab strip's nodes,
    // horizontal scroll and focus instead of remounting it on every toggle.
    const body = $("#agpad-body", p.el);
    body.replaceChildren(); body.scrollTop = 0;
    agpadOverviewPaint(p);
  } else {
    p.el.innerHTML = tHead({ view: agpadHeadActive(p, view), views: agpadHeadViews(p), bodyId: "agpad-body", taskTools: false });
    agpadOverviewMount(p);
    tWireHead(p.el, agpadHeadActive(p, from), next => {
      const view = agpadTabView(p, next);
      if (view !== p.view || (view === "focus" && p.surface)) agpadSetView(p, view);
    });
    // The selected Focus entry still closes a nested surface and returns to the Focus items.
    const focusTab = agpadFocusSurfaces(p) ? $('.ttab[data-v="focus"]', p.el) : null;
    if (focusTab) focusTab.onclick = () => { if (p.view !== "focus" || p.surface) agpadSetView(p, "focus"); };
  }
  if (view === 'status') agentCurrentWorkMount(p);
  if(typeof agentPaneIndicators==='function')agentPaneIndicators(p);
  if (view === "activity" && !p.logs) p.nextLog = 0;
  if (view === "inbox" && !p.inbox) p.nextInbox = 0;
  // Workspace views (Focus, any discovered type) are mounted by
  // their own renderer; an undeclared view gets the honest placeholder.
  if (agpadWorkspaceView(view)) agpadWorkspaceMount(p, view);
  if (view === 'globalTasks') globalTasksMount($('#agpad-body', p.el), {agentId: p.agentId, compact: true});
  agpadPaint(p);
  agpadTick();
}

// ------------------------------------------------------------ Focus nested surfaces
// For panes with Focus, Browser, Desktop and a Viewbox item render inside the mounted Focus shell, in place of the items list.
// The surface has its own mount context, so closing or switching it never touches the Focus workspace's own teardown.
function agpadShownView(p) { return p.surface ? p.surface.kind : p.view; }

function agpadSurfaceRequest(p, kind, leaveChecked) {
  if (!AGPAD_FOCUS_SURFACES.has(kind) || !p.views.includes(kind) || !agentPaneViewAllowed(p.workspaceRole, kind)) return;
  const open = () => { if (_agpad === p && p.view === "focus" && p.focusMount?.isConnected) agpadSurfaceOpen(p, kind); };
  if (p.view === "focus" && p.focusMount?.isConnected) { open(); return; }
  const moved = agpadSetView(p, "focus", leaveChecked);
  if (moved?.then) return moved.then(open);
  open();
}

function agpadSurfaceOpen(p, kind) {
  if (!AGPAD_FOCUS_SURFACES.has(kind) || !p.views.includes(kind) || !agentPaneViewAllowed(p.workspaceRole, kind)) return;
  const board = p.focusMount, host = board?.querySelector(":scope > .agfocus-surface");
  if (!host || (kind === "desktop" && p.workspaceRole !== "ordubashy")) return;
  p.focusDetail?.back?.();
  const layout = board.querySelector(".agfocus-layout"), switching = !!p.surface;
  if (!switching) p.surfaceScroll = layout?.scrollTop || 0;
  agpadSurfaceDispose(p);
  const body = $(".agfocus-surface-body", host), surface = {kind, dead: false, cleanups: []};
  surface.ctx = {kind, host: body, minHeight: 240,
    current: () => !surface.dead && p.surface === surface && p.view === "focus" && agpadActive(p) && body.isConnected,
    onDispose: fn => surface.cleanups.push(fn),
    reset: () => surface.cleanups.splice(0).reverse().forEach(fn => fn())};
  p.surface = surface;
  board.classList.add("agfocus-surface-open");
  host.hidden = false;
  p.pendingView = null;
  if (kind === "browser" || kind === "desktop") {
    p.browserMode = kind;
    const modeKey = agpadModeKey(p);
    if (modeKey) ls(modeKey, kind);
  }
  agpadRememberView(p, kind);
  if (kind === "desktop") agfocusDesktopMount(p, surface.ctx);
  else if (kind === "browser" && typeof agbrowserMount === "function") agbrowserMount(p, false, surface.ctx);
  else if (kind === "viewbox" && typeof agviewboxMount === "function") agviewboxMount(p, surface.ctx);
  agpadSurfacePaint(p);
  if (!switching) $("[data-surface-back]", host)?.focus({preventScroll: true});
}

// Everything the surface owns (frames, observers, listeners, requests) is released here; Focus's own DOM and state stay.
function agpadSurfaceDispose(p) {
  const surface = p.surface;
  if (!surface) return;
  p.surface = null;
  surface.dead = true;
  for (const fn of surface.cleanups.splice(0).reverse()) { try { fn(); } catch (_) { /* a failed cleanup must not keep the rest alive */ } }
  surface.ctx.host.replaceChildren();
  surface.ctx.host.className = "agfocus-surface-body";
}

function agpadSurfaceClose(p) {
  const surface = p.surface, board = p.focusMount;
  if (!surface) return;
  agpadSurfaceDispose(p);
  agpadRememberView(p, "focus");
  agpadSurfacePaint(p);
  if (!board) return;
  board.classList.remove("agfocus-surface-open");
  $(":scope > .agfocus-surface", board).hidden = true;
  const layout = board.querySelector(".agfocus-layout");
  if (layout) layout.scrollTop = p.surfaceScroll || 0;
  agfocusPaint(p);
  const opener = p.focusSurfaces?.chip(surface.kind) || $('.ttab[data-v="focus"]', p.el);
  opener?.focus({preventScroll: true});
}

function agpadSurfacePaint(p) {
  p.focusSurfaces?.setActive(p.surface ? p.surface.kind : null, p.surface?.kind === "viewbox" ? p.vb?.selected?.id : null);
}


function agpadVisibility(p) {
  p.el.closest(".agsplit").classList.toggle("pad-off", !p.open);
  p.el.hidden = !p.open;
  p.el.closest(".agsplit")._agentPaneDividerRefresh?.();
  if (p.open) moveTInd(p.el, p.view);
}

function agpadPaint(p) {
  if (p.view === 'status' || agpadWorkspaceView(p.view)) return;
  if (p.view === 'globalTasks') return;
  if (p.view === "inbox") { agpadPaintInbox(p); return; }
  let html;
  if (p.view === "activity") {
    html = p.logs ? tActivityHtml(p.logs, `No actions in the latest ${AGPAD_LOG_N} project log entries.`) : tEmptyHtml("Loading activity…");
    if (p.logError) html = tEmptyHtml(p.logs ? "Could not refresh activity. Showing the last update." : "Activity unavailable. Retrying…") + (p.logs ? html : "");
  } else if (!p.tasks) {
    html = tEmptyHtml(p.taskError ? "Tasks unavailable. Waiting for the next refresh…" : "Loading tasks…");
  } else {
    const groups = agpadGroups(p.tasks, p.agent);
    html = tSection("open", groups.open, { label: "Open tasks", canAdd: false, detailsOnly: true }) +
      tSection("waiting", groups.waiting, { label: "Waiting on", canAdd: false, detailsOnly: true }) +
      (groups.snoozed.length ? tSection("snoozed", groups.snoozed, { label: "Snoozed", canAdd: false, detailsOnly: true }) : '');
  }
  if (p.bodyHtml === html) return;
  p.bodyHtml = html;
  const body = $("#agpad-body", p.el), scroll = body.scrollTop;
  body.innerHTML = html;
  body.scrollTop = scroll;
  if (p.view === "tasks") tWireRows(body, true);
  else if (p.logs) logTips(body, p.logs);
}

async function agpadRefreshLog(p) {
  if (!agpadActive(p) || p.view !== "activity" || p.pending) return;
  p.pending = true;
  try {
    const rows = await api(`/biz/${encodeURIComponent(p.biz)}/log?n=${AGPAD_LOG_N}`);
    if (!agpadActive(p, true)) return;
    if (!Array.isArray(rows)) throw new Error("Log unavailable");
    p.logs = rows.filter(e => e.actor === `agent:${p.agent}`);
    p.logError = false;
  } catch (e) {
    if (agpadActive(p, true)) p.logError = true;
  } finally {
    p.pending = false;
    p.nextLog = Date.now() + AGPAD_POLL_MS;
    if (agpadActive(p) && p.view === "activity") agpadPaint(p);
  }
}

function agpadTick() {
  const p = _agpad;
  if (!agpadActive(p)) return;
  if(typeof agentPaneIndicators==='function')agentPaneIndicators(p);
  if (p.view === "attentionBoard") attentionBoardTick(p);
  if (p.view === 'status' && Date.now() >= (p.nextActivity || 0)) agentCurrentWorkRefresh(p);
  agpadWorkspaceTick(p);
  if (p.view === 'status' || agpadWorkspaceView(p.view)) return;
  if (p.view === 'globalTasks') return;
  if (p.view === "inbox") {
    if (Date.now() >= (p.nextInbox || 0)) agpadRefreshInbox(p);
    return;
  }
  // A newly mounted pane must not read the previous business's task array.
  // Ask the existing shared refresh once, then follow its cache (including
  // optimistic task edits). All subsequent task requests belong to tasks.js.
  if (!p.tasksStarted) {
    p.tasksStarted = true;
    refreshTasks().then(() => {
      if (!agpadActive(p)) return;
      p.taskError = state.tasks === p.source && p.tasks === null;
      agpadTick();
    }).catch(() => {
      if (agpadActive(p)) { p.taskError = true; agpadPaint(p); }
    });
  }
  if (state.tasks !== p.source) { p.source = p.tasks = state.tasks; p.taskError = false; }
  agpadPaint(p);
  if (p.view === "activity" && Date.now() >= p.nextLog) agpadRefreshLog(p);
}

// A cheap local reconciliation keeps edits in the shared task cache visible.
// Refresh only the visible agent pane.
setInterval(agpadTick, 1000);

function agpadInboxAge(at, unavailable = 'Time unavailable') {
  const checked = Date.parse(at);
  if (!Number.isFinite(checked)) return unavailable;
  const minutes = Math.max(0, Math.floor((Date.now() - checked) / 60000));
  const [amount, unit] = minutes >= 1440 ? [Math.floor(minutes / 1440), 'day']
    : minutes >= 60 ? [Math.floor(minutes / 60), 'hour'] : [minutes, 'minute'];
  return `${amount} ${unit}${amount === 1 ? '' : 's'} ago`;
}

// Backend delivery of the inbox advisory (data.delivery, the same semantics as agents[].inbox): Qorqut types at most
// one short notice into the agent's exact native session. Submission is never evidence that the agent read anything,
// so no state here means read or acknowledged; only an exact acknowledgement by the agent does.
const AGENT_DELIVERY_STATES = {
  idle: 'Nothing waiting for a notice',
  pending: 'Notice pending',
  deferred: 'Notice deferred',
  submitted: 'Notice submitted · processing unconfirmed',
  uncertain: 'Notice may have been typed · not resent',
  stale: 'Session changed · a new notice will follow',
  unavailable: 'Notice delivery unavailable',
  unknown: 'Delivery status unavailable',
};
const AGENT_DELIVERY_REASONS = {
  offline: 'No live session; it is delivered after someone starts one manually',
  nudge_backend_not_running: 'The delivery service is not running',
  nudge_status_unavailable: 'The delivery status could not be read',
  session_not_current: 'The session is not the current one',
};
function agentDeliveryView(delivery) {
  const value = delivery && typeof delivery === 'object' ? delivery : {};
  const stateName = AGENT_DELIVERY_STATES[value.state] ? value.state : 'unknown';
  const reason = typeof value.reason === 'string' && value.reason ? (AGENT_DELIVERY_REASONS[value.reason] || value.reason.replace(/^not_typed:/, '').replace(/_/g, ' ')) : '';
  const attention = ['pending', 'deferred', 'uncertain', 'stale', 'unavailable', 'unknown'].includes(stateName);
  return {state: stateName, label: AGENT_DELIVERY_STATES[stateName], reason, at: value.last_attempt_at || null, attention};
}

// Current participants route replies; originals attribute authorship (core C7).
function messageParticipant(message, side) {
  return message['current_' + side + '_name'] || message[side + '_name'] || 'Unavailable';
}
function messageOriginally(message, side) {
  const current = message['current_' + side + '_id'];
  if (!current || current === message[side + '_id']) return '';
  return `<span class="agpad-message-original">Originally ${side === 'sender' ? 'from' : 'to'} ${esc(message[side + '_name'] || 'Unavailable')}</span>`;
}
// Two original participants now one principal: history only, never a reply.
function messageCollapsed(message) {
  return !!message.current_sender_id && message.current_sender_id === message.current_recipient_id
    && message.sender_id !== message.recipient_id;
}

function agpadPaintInbox(p) {
  const body = $('#agpad-body', p.el), data = p.inbox;
  if (!body) return;
  const error = p.inboxError ? '<p class="agpad-inbox-status" role="alert">Inbox unavailable. Retrying…</p>' : '';
  const agentId = p.agentId || state.agents.find(agent => agent.name === p.agent)?.agent_id;
  const card = (item, subscription = false) => item.message ? `<details class="agpad-inbox-item agpad-message" data-message-id="${esc(item.message.id)}"${messageCollapsed(item.message) ? ' data-read-only="true"' : ''}>
    <summary><strong>Message from ${esc(messageParticipant(item.message, 'sender'))}</strong><span>${esc(agpadInboxAge(item.created_at))}${item.message.reply_to ? ' · Reply' : ''}${messageCollapsed(item.message) ? ' · Read-only history' : ''}</span>${messageOriginally(item.message, 'sender')}<span>${esc(messagePreview(item.message.body,item.message.summary))}</span></summary>
    <div class="agpad-message-body">${esc(item.message.body)}</div>
    <span>To ${esc(messageParticipant(item.message, 'recipient'))}</span>${messageOriginally(item.message, 'recipient')}</details>` : `<button type="button" class="agpad-inbox-item" data-agent-id="${esc(agentId || '')}" data-inbox-source="${esc(item.source_id)}" data-source-type="${esc(item.source_type)}" data-business="${esc(item.business_slug || '')}">
    <strong>${esc(item.summary || 'Source item')}</strong><span>${esc(({focus_cards:'Pinned Reference',tasks:'Task',signoffs:'Signoff',documents:'Document',email_conversations:'Email',data_tables:'Table',action_logs:'Action',requests:'Request',agent_profiles:'Agent',principals:'Person',context_entities:'Reference'})[item.source_type] || 'Update')}${subscription ? (item.include_related ? ' · Includes linked items' : ' · Item only') + (item.managed_by === 'thread_follow' ? ' · Following discussion' : item.managed_by === 'related_work' ? ' · Related work' : '') : ' · ' + esc(item.event_type.split(',').map(type => ({relationship_changed:'Links changed',assignment_changed:'Assignment changed',response_submitted:'Response submitted',action_outcome:'Action result',note_added:'Note added',note:'Note added',message_changed:'Message received'})[type] || type.replaceAll('_', ' ')).join(', '))}</span>
    ${subscription ? '' : `<span>${esc(item.actor_name)} · ${esc(agpadInboxAge(item.created_at))}</span>`}</button>`;
  const html = error + (!data ? tEmptyHtml(p.inboxError ? 'No inbox data available.' : 'Loading inbox…') :
    `<div class="agpad-inbox-top"><h3 class="agpad-inbox-heading">Pending updates</h3>${(view => `<div class="agpad-inbox-status" data-delivery-state="${esc(view.state)}"><span class="agpad-delivery-dot" role="img" aria-label="${esc(view.label)}" title="${esc(view.label)}"></span><span>${esc(view.label)}${view.reason ? ' · ' + esc(view.reason) : ''}${view.at ? ' · last attempt ' + esc(agpadInboxAge(view.at, '')) : ''}</span></div>`)(agentDeliveryView(data.delivery))}</div>${data.items.length ? data.items.map(item => card(item)).join('') : tEmptyHtml('No accessible pending updates.')}
    ${data.next_cursor ? '<button type="button" class="quiet" id="agpad-inbox-more">More updates</button>' : ''}
    <h3 class="agpad-inbox-heading">Subscriptions</h3>${data.subscriptions.length ? data.subscriptions.map(item => card(item, true)).join('') : tEmptyHtml('No subscriptions.')}`);
  if (p.bodyHtml === html) return;
  p.bodyHtml = html;
  const scroll = body.scrollTop;
  const expandedMessages=new Set([...body.querySelectorAll('.agpad-message[open]')].map(el=>el.dataset.messageId));
  body.innerHTML = html; body.scrollTop = scroll;
  body.querySelectorAll('.agpad-message').forEach(el=>{el.open=expandedMessages.has(el.dataset.messageId);});
  body.querySelectorAll('[data-inbox-source]').forEach(button => {
    button.onclick = () => agpadOpenInboxSource(button.dataset).catch(error => toast(error.message || 'Source unavailable'));
  });
  const more = $('#agpad-inbox-more', body);
  if (more) more.onclick = () => agpadRefreshInbox(p, true);
}

async function agpadRefreshInbox(p, more = false) {
  if (!agpadActive(p) || p.view !== 'inbox' || p.inboxPending) return;
  p.inboxPending = true;
  try {
    const agentId = p.agentId || state.agents.find(agent => agent.name === p.agent)?.agent_id;
    if (!agentId) throw new Error('Registered agent unavailable');
    let cursor = more ? p.inbox?.next_cursor : null, data, items = more ? [...p.inbox.items] : [];
    const pages = more ? 1 : Math.max(1, Math.ceil((p.inbox?.items.length || 0) / 20));
    for (let page = 0; page < pages; page++) {
      data = coreData(await api(`/me/agents/${encodeURIComponent(agentId)}/inbox${cursor ? '?after=' + cursor : ''}`));
      // Finish reads for this same pane even after its subview or browser tab
      // becomes hidden. Painting still belongs only to the visible Inbox.
      if (!agpadActive(p, true)) return;
      if (!Array.isArray(data.items) || !Array.isArray(data.subscriptions)) throw new Error('Invalid inbox response');
      items.push(...data.items); cursor = data.next_cursor;
      if (!cursor) break;
    }
    p.inbox = { ...data, items: [...new Map(items.map(item => [item.id, item])).values()] };
    p.inboxError = false;
  } catch (_) {
    if (agpadActive(p, true)) p.inboxError = true;
  } finally {
    p.inboxPending = false; p.nextInbox = Date.now() + AGPAD_POLL_MS;
    if (agpadActive(p) && p.view === 'inbox') agpadPaintInbox(p);
  }
}

async function agpadOpenInboxSource({ inboxSource: id, sourceType: type, business: biz, agentId }) {
  if (type === 'focus_cards') {
    const {item}=coreData(await post('/root/operations/focus_get',{scope:'global',item_id:id}));
    if(item.unavailable)throw Error('This Pinned item is no longer available.');
    modal({title:esc(item.label),body:'<div id="focus-inbox-conversation"></div>',actions:[]});
    // The card's canonical binding owns the discussion, so this modal keys the
    // same state as Focus: one draft, cursor and submission per bound object.
    const binding=item.conversation || {type:'reference',id:item.id};
    // The modal body is freshly created above, so this is always an explicit open.
    conversationMount(document.querySelector('#focus-inbox-conversation'),
      {type:binding.type,id:binding.id,biz:item.resolved_source?.business_slug || item.context_business_slug || null,revision:item.source_revision},{
      privateGlobal:true,
      privateSource:{type:binding.type,id:binding.id,...(binding.business_id?{business_id:binding.business_id}:{})}
    }).expand();
    return;
  }
  if (type === 'signoffs') return sgAttentionNavigate(biz || null, id);
  if (!biz && type === 'documents') {
    const { document: source } = coreData(await api(`/me/agents/${encodeURIComponent(agentId)}/inbox/documents/${encodeURIComponent(id)}`));
    return modal({ title: esc(source.metadata?.title || source.path), body: `<div class="work-source">${esc(source.body || '')}</div>`, actions: [] });
  }
  if (type === 'agent_profiles') return agentProfileOpen(id);
  if (biz && (state.scope === 'global' || state.biz !== biz)) await selectBiz(biz);
  if (biz && state.biz !== biz) return;
  if (type === 'data_tables') return tablesOpen(biz || null, id);
  if (type === 'principals' || type === 'context_entities') {
    const { item } = await workCall(biz, 'context_items_get', { item: { type: type === 'principals' ? 'person' : 'entity', id }, ...(biz ? {} : { scope: 'global' }) });
    if (type === 'context_entities' && /^https?:\/\//.test(item.url || '')) return workOpenLink({biz}, {resource_type:'entity',resource_id:id,current:item});
    return modal({title:esc(item.label),body:`<p>${esc([item.role,item.org,item.about,item.summary].filter(Boolean).join(' · '))}</p>`,actions:[]});
  }
  if (type === 'tasks') return tOpenTask(id);
  const { item } = await workCall(biz, 'context_items_get', { item: workItem(type, id), ...(biz ? {} : { scope: 'global' }) });
  return workOpenLink({ biz }, { resource_type: type, resource_id: id, current: { ...item, kind: item.kind || item.subtype } });
}
