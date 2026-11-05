function sgAttentionTags(item) {
  return ['urgent', 'blocking'].filter(key => item.state === 'pending' && item.attention?.[key])
    .map(key => `<span class="sg-priority-tag">${key === 'urgent' ? 'Urgent' : 'Blocking'}</span>`).join('');
}
// Exact approved platform content is shared by the drawer and Marketing review.
function sgPlatformComplete(s) {
  if (s.platform?.available === false) return false;
  const submission = s.platform?.submission;
  if (!submission || typeof submission.payload !== "object" || !submission.payload || Array.isArray(submission.payload)) return false;
  const p = submission.payload;
  if (submission.tool === "email.send") return Array.isArray(p.to) && p.to.length > 0 && typeof p.subject === "string" && typeof p.body === "string";
  return false;
}
function sgPlatformStatus(s) {
  if (s.state === "pending" && s.execution_state === "awaiting_approval") return "";
  const labels = { awaiting_approval: "Awaiting approval", queued: "Queued", not_started: "Preparing execution",
    dispatched: "Awaiting delivery result", completed: "Completed",
    canceled: "Canceled", failed: "Failed", reconciliation_required: "Needs reconciliation" };
  return `${s.decision_state === "approved" ? "Approved" : s.decision_state === "rejected" ? "Rejected" : "Pending"} · ${labels[s.execution_state] || "Not started"}`;
}
// Account-email Signoffs keep the approval state; the send result is a separate outcome object. Outcome kind, never operation_name, marks them.
const sgEmailStages = { awaiting_approval: "Awaiting approval", approved: "Approved", queued: "Approved · Queued", running: "Approved · Sending",
  reconciling: "Approved · Checking send", submitted: "Approved · Submitted", failed: "Approved · Send failed", unconfirmed: "Approved · Send unconfirmed", cancelled: "Approved · Cancelled" };
function sgOutcomeText(value, max = 120) { return typeof value === "string" ? value.slice(0, max) : ""; }
function sgEmailStatus(s) {
  const outcome = s && s.outcome;
  if (!outcome || outcome.kind !== "account_email" || !["approved", "applied"].includes(s.state)) return null;
  if (Object.hasOwn(sgEmailStages, outcome.stage)) return sgEmailStages[outcome.stage];
  if (outcome.stage === "applied") return `Approved · ${sgOutcomeText(outcome.label) || "Applied"}`;
  return sgOutcomeText(outcome.label) || "Status unavailable";
}
function sgEmailOutcomeHtml(s) {
  const status = sgEmailStatus(s);
  if (status == null) return "";
  const outcome = s.outcome, label = sgOutcomeText(outcome.label), stage = outcome.stage;
  const note = stage === "submitted" ? "Accepted by the mail provider; delivery is not confirmed."
    : stage === "reconciling" ? "The send result is being checked. It is not confirmed yet."
    : stage === "unconfirmed" ? "The send result could not be confirmed. The message may or may not have been sent." : "";
  const codes = [], err = outcome.error && typeof outcome.error === "object" ? outcome.error : null;
  if (err && err.code) codes.push(`Error: ${sgOutcomeText(err.code, 80)}${err.retryable === true ? " (retryable)" : ""}`);
  const d = outcome.diagnostics && typeof outcome.diagnostics === "object" ? outcome.diagnostics : {};
  if (d.engine_error) codes.push(`Engine: ${sgOutcomeText(d.engine_error, 80)}`);
  if (Number.isInteger(d.provider_status) && d.provider_status >= 100 && d.provider_status <= 599) codes.push(`Provider status: ${d.provider_status}`);
  if (d.provider_error_type) codes.push(`Provider error: ${sgOutcomeText(d.provider_error_type, 80)}`);
  return `<div class="sg-outcome" data-stage="${esc(sgOutcomeText(stage, 40))}"><p class="sg-notice" role="status"><strong>${esc(status)}</strong>${label && label !== status ? ` · <span class="sg-outcome-label">${esc(label)}</span>` : ""}</p>`
    + (note ? `<p class="sg-notice sg-outcome-note">${esc(note)}</p>` : "")
    + (codes.length ? `<p class="sg-notice sg-outcome-codes">${codes.map(esc).join(" · ")}</p>` : "") + `</div>`;
}
function sgItemStatus(s, labels) {
  return s.category === "platform_action" ? sgPlatformStatus(s) : sgEmailStatus(s) ?? labels[s.state];
}
function sgPlatformCancellation(s) {
  const execution = s.platform?.execution || {};
  return {
    canCancel: !execution.historical && s.state === 'applied' && ['queued', 'dispatched'].includes(s.execution_state)
      && !execution.cancellation,
    label: 'Cancel execution', notice: '',
  };
}
function sgPlatformPreview(s) {
  if (!sgPlatformComplete(s)) return "";
  const sub = s.platform.submission, p = sub.payload;
  const line = (label, value) => value == null || value === "" || Array.isArray(value) && !value.length ? "" : `<div class="sg-platform-field"><strong>${esc(label)}</strong><span>${esc(Array.isArray(value) ? value.join(", ") : String(value))}</span></div>`;
  const message = (subject, body) => `${subject ? `<h3>${esc(subject)}</h3>` : ""}<pre class="sg-message">${esc(body)}</pre>`;
  const content = line("From", p.from_address) + line("To", p.to) + line("CC", p.cc) + line("BCC", p.bcc) + sgEmailTextHtml(p.subject, p.body);
  return `<div class="sg-platform">${sub.why ? `<div class="sg-rationale"><h3>Purpose</h3><p>${renderSafeLinks(sub.why)}</p></div>` : ""}
    ${content}${line("Attachments", p.media)}${line("Scheduled after", sub.not_before)}${line("Window ends", sub.window_end)}</div>`;
}

// Proposal content shown before any decision. It reads only the frozen preview: nothing here fetches provider data or rewrites text.
const SG_HIDDEN_KEY = /pass(word|wd|phrase|code)|secret|credential|private[_-]?key|authorization|(access|refresh|auth|api|bearer|session)[_-]?(token|key)|(^|[_-])tokens?$|content_base64|attachment[_-]?bytes|(^|[_-])bytes$/i;
const SG_INTERNAL_KEY = /(^|[_-])(hash|digest|sha256|sha512|revision|etag|idempotency[_-]?key|request[_-]?id|principal[_-]?id|nonce|timestamp)s?($|[_-])|(^|[_-])(created|updated|applied|decided|deleted|last_attempt|first_seen|last_seen)_at$/i;
const SG_LABELS = { status: 'Status', state: 'Status', assignee: 'Assignee', assignee_id: 'Assignee', assigned_to: 'Assignee', owner: 'Owner', schedule: 'Schedule', cron: 'Schedule',
  permission: 'Permission', permissions: 'Permissions', tool_policy: 'Permission', role: 'Role', title: 'Title', name: 'Name', description: 'Description', due: 'Due date', due_date: 'Due date' };
const SG_FIRST_CHANGES = 4;
const sgPlain = v => !!v && typeof v === 'object' && !Array.isArray(v);
function sgEqual(a, b) {
  if (a === b) return true;
  if (Array.isArray(a) && Array.isArray(b)) return a.length === b.length && a.every((x, i) => sgEqual(x, b[i]));
  if (sgPlain(a) && sgPlain(b)) { const keys = new Set([...Object.keys(a), ...Object.keys(b)]); return [...keys].every(k => sgEqual(a[k], b[k])); }
  return false;
}
function sgLabel(segment) {
  const key = String(segment).toLowerCase();
  if (SG_LABELS[key]) return SG_LABELS[key];
  const words = String(segment).replace(/_json$/, '').replace(/([a-z0-9])([A-Z])/g, '$1 $2').replace(/[_\-.]+/g, ' ').trim().toLowerCase();
  return words ? words[0].toUpperCase() + words.slice(1) : 'Value';
}
const sgSegments = path => String(path).split('.').filter(part => part && !/^\d+$/.test(part));
const sgTextBlock = (text, label) => `<div class="sg-text" tabindex="0" role="region" aria-label="${esc(label)}">${esc(text)}</div>`;
function sgValue(v) {
  if (v == null) return '<span class="sg-none">None</span>';
  if (typeof v === 'boolean') return v ? 'Yes' : 'No';
  if (typeof v === 'number') return esc(String(v));
  if (typeof v === 'string') return v === '' ? '<span class="sg-none">Empty</span>' : v.length > 240 || v.split('\n').length > 6 ? sgTextBlock(v, 'Full text') : `<span class="sg-val-text">${esc(v)}</span>`;
  if (Array.isArray(v)) return v.length ? `<ul class="sg-val-list">${v.map(x => `<li>${sgValue(x)}</li>`).join('')}</ul>` : '<span class="sg-none">None</span>';
  const entries = Object.entries(v).filter(([k]) => !SG_INTERNAL_KEY.test(k));
  return entries.length ? `<dl class="sg-val-obj">${entries.map(([k, x]) => `<dt>${esc(sgLabel(k))}</dt><dd>${SG_HIDDEN_KEY.test(k) ? '<span class="sg-none">Value hidden</span>' : sgValue(x)}</dd>`).join('')}</dl>` : '<span class="sg-none">None</span>';
}
// Overseer binds its exact saved delta/plan into the explanation. Present that
// frozen material without rewriting the Signoff or fetching a newer revision.
function sgInputExplanation(explanation) {
  const fallback = () => `<div class="sg-rationale"><p>${renderSafeLinks(explanation)}</p></div>`;
  const marker = '\n\nBound material:\n';
  if (typeof explanation !== 'string' || !explanation.includes(marker)) return fallback();
  const split = explanation.lastIndexOf(marker);
  let material;
  try { material = JSON.parse(explanation.slice(split + marker.length)); } catch { return fallback(); }
  if (!sgPlain(material) || !sgPlain(material.targets) || Object.keys(material).length < 2) return fallback();
  const delta = material.delta;
  if (delta !== undefined && (!sgPlain(delta) || delta.schema_version !== 1 ||
      typeof delta.change_direction !== 'string' || !delta.change_direction.trim() || !sgPlain(delta.scope))) return fallback();

  // Unlike generic change previews, do not filter fields in plans, evidence or
  // unfamiliar material: those may affect the owner's decision.
  const value = item => {
    if (item === null) return '<span class="sg-none">None</span>';
    if (typeof item === 'boolean') return item ? 'Yes' : 'No';
    if (Array.isArray(item)) return item.length ? `<ul>${item.map(x => `<li>${value(x)}</li>`).join('')}</ul>` : '<span class="sg-none">None</span>';
    if (sgPlain(item)) return `<dl>${Object.entries(item).map(([key, x]) => `<dt>${esc(sgLabel(key))}</dt><dd>${value(x)}</dd>`).join('')}</dl>`;
    return `<span class="sg-bound-text">${esc(String(item))}</span>`;
  };
  const section = (label, item) => `<section><h4>${esc(label)}</h4>${value(item)}</section>`;
  const nonempty = item => item != null && item !== '' && (!Array.isArray(item) || item.length);
  let content = '';
  if (delta) {
    content += section('Proposed change', delta.change_direction);
    const labels = {description: 'Applies to', conditions: 'When', exceptions: 'Exceptions',
      project_ids: 'Projects', work_types: 'Types of work', workflow_refs: 'Workflows'};
    for (const [key, item] of Object.entries(delta.scope)) {
      if (nonempty(item)) content += section(labels[key] || sgLabel(key), item);
    }
    if (nonempty(delta.target_refs)) content += section('Areas to update', delta.target_refs);
    if (nonempty(delta.uncertainties)) content += section('Open questions', delta.uncertainties);
    if (nonempty(delta.ranking_reason)) content += section('Why review this', delta.ranking_reason);
    // Bookkeeping remains available in the exact saved record below. New fields
    // are shown rather than silently discarded by an older client.
    const known = new Set(['schema_version', 'delta_id', 'revision', 'summary', 'change_direction', 'scope',
      'target_refs', 'uncertainties', 'ranking_reason', 'created_at', 'created_by', 'proposal_ids', 'support_summary']);
    for (const [key, item] of Object.entries(delta)) {
      if (!known.has(key)) content += section(sgLabel(key), item);
    }
    if (nonempty(delta.support_summary)) content += `<details><summary>Supporting observations</summary>${value(delta.support_summary)}</details>`;
  }
  for (const [key, item] of Object.entries(material)) {
    if (key !== 'targets' && key !== 'delta') content += section(sgLabel(key), item);
  }
  return `<div class="sg-rationale"><p>${renderSafeLinks(explanation.slice(0, split))}</p></div>
    <div class="sg-content sg-bound-material">${content}<details class="sg-bound-record"><summary>Saved record</summary>${sgTextBlock(JSON.stringify(material, null, 2), 'Exact saved approval record')}</details></div>`;
}
function sgChangeRows(changes) {
  const rows = [];
  const target = change => change.resource && (typeof change.resource === 'string' ? change.resource : change.resource.label) || (change.resource && change.resource.resource_type ? sgLabel(change.resource.resource_type) : 'Change');
  const add = (to, segments, before, after) => {
    if (segments.some(k => SG_INTERNAL_KEY.test(k)) || sgEqual(before ?? null, after ?? null)) return;
    if (segments.some(k => SG_HIDDEN_KEY.test(k))) { rows.push({ target: to, label: segments.map(sgLabel).join(' › '), hidden: true }); return; }
    if ((before == null || sgPlain(before)) && (after == null || sgPlain(after)) && (sgPlain(before) || sgPlain(after)) && segments.length < 6) {
      for (const k of new Set([...Object.keys(before || {}), ...Object.keys(after || {})])) add(to, [...segments, k], before?.[k], after?.[k]);
      return;
    }
    rows.push({ target: to, label: segments.map(sgLabel).join(' › '), before, after });
  };
  for (const change of changes) {
    const to = target(change);
    if (change.kind === 'text') {
      if (change.before !== change.after) rows.push({ target: to, label: 'Text', text: true, before: change.before, after: change.after });
    } else for (const field of change.fields || []) if (field && typeof field.path === 'string') add(to, sgSegments(field.path), field.before, field.after);
  }
  return rows;
}
function sgRowHtml(r) {
  if (r.hidden) return `<div class="sg-change"><dt>${esc(r.label)}</dt><dd><span class="sg-none">Changed · value hidden</span></dd></div>`;
  if (r.text) {
    const body = r.after == null ? '<span class="sg-none">Removed</span>' : sgTextBlock(r.after, `Proposed text for ${r.target}`);
    return `<div class="sg-change"><dt>${esc(r.label)}</dt><dd>${body}<details class="sg-fulltext"><summary>Show before and after</summary><h4>Before</h4>${r.before == null ? '<span class="sg-none">None</span>' : sgTextBlock(r.before, 'Text before')}<h4>After</h4>${r.after == null ? '<span class="sg-none">Removed</span>' : sgTextBlock(r.after, 'Text after')}</details></dd></div>`;
  }
  return `<div class="sg-change"><dt>${esc(r.label)}</dt><dd><span class="sg-from">${sgValue(r.before)}</span><span class="sg-arrow" role="img" aria-label="changed to">→</span><span class="sg-to">${r.after == null ? '<span class="sg-none">Removed</span>' : sgValue(r.after)}</span></dd></div>`;
}
function sgRowGroups(rows) {
  const groups = [];
  for (const r of rows) { const last = groups[groups.length - 1]; if (last && last.target === r.target) last.rows.push(r); else groups.push({ target: r.target, rows: [r] }); }
  return groups.map(g => `<section class="sg-change-group"><h4>${esc(g.target)}</h4><dl class="sg-changes">${g.rows.map(sgRowHtml).join('')}</dl></section>`).join('');
}
function sgJobRows(changes) {
  const rows = [];
  for (const change of changes) {
    const fields = Object.fromEntries((change.fields || []).filter(f => f && typeof f.path === 'string').map(f => [f.path, f.after]));
    const program = sgPlain(fields.program) ? fields.program : {};
    const kind = fields.program_kind ?? fields.kind ?? (typeof fields.program === 'string' ? fields.program : program.kind);
    const args = fields.arguments_json ?? fields.arguments ?? fields.args ?? program.arguments ?? program.args;
    const to = change.resource && change.resource.label || 'Scheduled job';
    if (typeof kind === 'string' && kind) rows.push({ target: to, label: 'Program', value: kind });
    if (args != null) rows.push({ target: to, label: 'Arguments', value: args });
  }
  return rows;
}
// Generic readable content. Queue jobs only appear when nothing else describes the change.
function sgChangesHtml(preview) {
  const type = change => change && change.resource && change.resource.resource_type;
  const all = preview.changes.filter(Boolean);
  const rows = sgChangeRows(all.filter(change => type(change) !== 'jobs'));
  if (!rows.length) {
    const jobs = all.every(change => type(change) === 'jobs') ? sgJobRows(all) : [];
    return jobs.length ? `<div class="sg-content sg-changes-wrap"><h3>Proposed change</h3>${jobs.map(r => `<section class="sg-change-group"><h4>${esc(r.target)}</h4><dl class="sg-changes"><div class="sg-change"><dt>${esc(r.label)}</dt><dd>${sgValue(r.value)}</dd></div></dl></section>`).join('')}</div>` : '';
  }
  const rest = rows.slice(SG_FIRST_CHANGES);
  return `<div class="sg-content sg-changes-wrap"><h3>Proposed change</h3>${sgRowGroups(rows.slice(0, SG_FIRST_CHANGES))}${rest.length ? `<details class="sg-more-changes"><summary>Show all changes (${rest.length} more)</summary>${sgRowGroups(rest)}</details>` : ''}</div>`;
}
// Frozen structured fields of one resource type; conflicting duplicates or malformed entries make the whole preview unavailable.
function sgFrozenFields(preview, resourceType) {
  const changes = (preview && Array.isArray(preview.changes) ? preview.changes : []).filter(c => c && c.kind === 'structured' && c.resource && c.resource.resource_type === resourceType);
  if (changes.length !== 1 || !Array.isArray(changes[0].fields)) return null;
  const map = new Map();
  for (const f of changes[0].fields) {
    if (!f || typeof f.path !== 'string' || !('after' in f)) return null;
    if (map.has(f.path) && !sgEqual(map.get(f.path), f.after)) return null;
    map.set(f.path, f.after);
  }
  return map;
}
const sgNonblank = v => typeof v === 'string' && v.trim() !== '';
const sgStringList = v => Array.isArray(v) && v.every(sgNonblank);
const sgUnavailable = what => ({ ok: false, html: `<div class="sg-content sg-unavailable" role="alert"><h3>Preview unavailable</h3><p>The exact ${esc(what)} could not be shown, so this proposal cannot be approved from here.</p></div>` });
const sgField = (label, valueHtml) => `<div class="sg-content-row" data-sg-field="${esc(String(label).toLowerCase())}"><span class="sg-content-label">${esc(label)}</span><span class="sg-content-value">${valueHtml}</span></div>`;
// Only selected same-origin extension assets register provider renderers.
const SG_MESSAGE_RENDERERS = new Map();
function registerSignoffRenderer(operation, renderer) {
  if (!/^[a-z][a-z0-9_]+$/.test(operation) || SG_MESSAGE_RENDERERS.has(operation)
      || !renderer || !['preview','values','fieldError'].every(name => typeof renderer[name] === 'function'))
    throw new Error('Invalid or duplicate Signoff renderer');
  SG_MESSAGE_RENDERERS.set(operation, Object.freeze(renderer));
}
window.registerSignoffRenderer = registerSignoffRenderer;
window.signoffMessageRenderer = operation => SG_MESSAGE_RENDERERS.get(operation);
const sgIsEmail = s => SG_MESSAGE_RENDERERS.get(s.operation_name)?.email === true
  || /^(account_email_|email_)(send|reply|forward)$/.test(s.operation_name || '');
function sgEmailTextHtml(subject, body) {
  return sgField('Subject', `<span data-sg-edit-field="subject">${subject ? esc(subject) : '<span class="sg-none">No subject</span>'}</span>`)
    + `<span class="sg-content-label sg-email-body-label">Body</span><div class="sg-text" data-sg-edit-field="body">${body ? esc(body) : '<span class="sg-none">Empty body</span>'}</div>`
    // Shown only by a collapsed inline Chat card: a native button that expands to the shared subject/body editor.
    + `<button type="button" class="sg-email-peek" data-sg-email-peek aria-expanded="false" aria-label="Email body preview. Open the full email to edit"></button>`;
}
function sgMessageValues(s) {
  const custom = SG_MESSAGE_RENDERERS.get(s.operation_name);
  if(custom)return custom.values(s);
  return null;
}

// The same editor/save/revision flow serves each structured message type.
function sgMessageFieldError(s,key,value) {
  const custom=SG_MESSAGE_RENDERERS.get(s.operation_name);
  if(custom)return custom.fieldError(key,value);
  return 'This field cannot be edited.';
}
// Returns {ok, html} for operations with an exact-content renderer, or null for generic changes.
function sgProposal(s, effects) {
  const custom=SG_MESSAGE_RENDERERS.get(s.operation_name);
  if(custom)return custom.preview(s.preview,effects,s.operation_name);
  return null;
}

// Scope-specific review. The server owns preparation, decisions and application.
const sgViews = new Set();
const sgShared = { drafts: new Map(), choices: new Map(), busy: new Map(), receipts: new Map(), refreshed: new Set(), executions: new Map() };
function sgCreateView(options) {
  const labels = { pending: "Pending", approved: "Approved, applying", applied: "Applied", rejected: "Rejected", conflicted: "Conflicted", failed: "Failed", answered: "Answered", cancelled: "Withdrawn" };
  const sg = {
    slug: null, businessId: null, generation: 0, mount: null, session: null,
    items: new Map(), order: [], selected: null, selections: new Map(),
    verified: new Map(), detailErrors: new Map(), detailRequest: 0, detailLoading: null,
    counts: null, nextCursor: null, loading: false, loaded: false,
    listRequest: 0, error: null, ...sgShared,
  };
  // Compact email previews belong to chat references, not other inline review surfaces.
  const compactEmail = options.compactEmail === true;
  const active = options.active;
  const workspace = options.workspace || (() => state.biz);
  const prefix = options.prefix || "";
  const capture = () => ({ slug: sg.slug, generation: sg.generation, mount: sg.mount });
  const mounted = c => c.slug === workspace() && c.slug === sg.slug && c.generation === sg.generation && c.mount === sg.mount && c.mount && c.mount.isConnected;
  const current = c => active() && mounted(c);
  const path = (slug, id = "") => `${options.global ? "/root" : "/biz/" + encodeURIComponent(slug)}/signoffs${id ? "/" + encodeURIComponent(id) : ""}`;
  const binding = s => `${s.id}:${s.proposal_revision}:${s.payload_hash}`;
  const expected = s => s.actionable !== false && (!options.expectedBinding || (!options.readOnly && s.id === options.expectedBinding.signoff_id && s.proposal_revision === options.expectedBinding.proposal_revision && s.payload_hash === options.expectedBinding.payload_hash && !s.superseded_by_id && options.expectedBinding.actionable));
  const busyKey = (slug, s) => `${slug}:${binding(s)}`;
  const owner = () => sg.session && sg.session.principal_kind === "human" && sg.session.is_owner === true && sg.session.principal_id;
  const draftKey = s => owner() && (sg.businessId || options.global) ? `sgdraft:${owner()}:${options.global ? "scope:global" : sg.businessId}:${options.expectedBinding ? `${options.expectedBinding.signoff_id}:${options.expectedBinding.proposal_revision}:${options.expectedBinding.payload_hash}` : binding(s)}` : null;
  const isAuth = error => error && ([401, 403].includes(error.httpStatus) || /^(unauthorized|forbidden|authentication_required|owner_required)$/.test(error.code));
  const stamp = value => {
    const date = new Date(value);
    return value && !isNaN(date) ? sjDateTime(date) : "";
  };
  const age = value => {
    const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 60000));
    return !Number.isFinite(minutes) ? "" : minutes < 1 ? "Just now" : minutes < 60 ? `${minutes}m ago` : minutes < 1440 ? `${Math.floor(minutes / 60)}h ago` : `${Math.floor(minutes / 1440)}d ago`;
  };
  const bizName = id => ((state.bizs || []).find(b => b.id === id || b.business_id === id) || {}).name || "Unavailable business";
  // Nested record-conversation controllers are released with the DOM that hosts them; their source-keyed drafts live on.
  const disposeNested = root => root?.querySelectorAll('.sg-conversation[data-conversation]').forEach(host => host._conversation?.dispose());
  // One cleanup for every denied read of a card inside a record thread: no private content outlives the denial.
  const DENIED_CODES = ['forbidden', 'not_found', 'policy_denied', 'invalid_principal'];
  const dropDenied = error => {
    if (!options.dropOnDenied || !DENIED_CODES.includes(error?.code)) return false;
    disposeNested(sg.mount);
    // A terminal denial outranks every read still in flight: older successes can neither repaint nor clear this state.
    sg.generation++; sg.listRequest++; sg.detailRequest++; sg.loading = false; sg.detailLoading = null;
    sg.items.clear(); sg.order = []; sg.selected = null; sg.verified.clear(); sg.detailErrors.clear();
    sg.error = error;
    render();
    options.onUnavailable?.(error);
    return true;
  };
  const conversationSource = (id, businessId) => {
    const business = options.global ? (state.bizs || []).find(b => b.id === businessId || b.business_id === businessId) : null;
    return { type: "signoff", id, biz: options.global ? business?.slug || null : sg.slug };
  };
  const errorHtml = (error, action) => `<div class="sg-error" role="alert"><span>${esc(error.message || error)}</span>${action ? `<button type="button" class="sg-command" data-sg="${action}">Retry</button>` : ""}</div>`;
  // Message drafts belong to this mounted view and survive background refreshes.
  const messageEdits = new Map(), ownedWrites = new Set();
  // Inline Chat email cards start as a compact preview; expanded ids survive routine rerenders of this view.
  const emailExpanded = new Set();
  const collapseEmail = id => {
    if (!emailExpanded.delete(id)) return;
    // Collapsing never saves or discards: a draft stays, an untouched focus-only edit is simply closed.
    const edit = messageEdits.get(id);
    if (edit && !edit.saving && !edit.receipt && Object.keys(edit.values).every(key => edit.values[key] === edit.original[key])) {
      messageEdits.delete(id);
      const preview = [...(sg.mount?.querySelectorAll('.sg-row') || [])].find(row => row.dataset.id === id)?.querySelector('.sg-preview');
      if (preview) preview._sgHtml = null;
    }
    render();
  };
  const outsidePointer = event => {
    if (!emailExpanded.size || !sg.mount) return;
    const path = event.composedPath();
    for (const id of [...emailExpanded]) {
      const row = [...sg.mount.querySelectorAll('.sg-row')].find(node => node.dataset.id === id);
      if (row && !path.includes(row)) collapseEmail(id);
    }
  };
  if (compactEmail) document.addEventListener('pointerdown', outsidePointer, true);
  const resizeMessages=()=>{if(active())sg.mount?.querySelectorAll('.sg-message-editor').forEach(fitMessage);};
  window.addEventListener('resize',resizeMessages);
  const canEditMessage = s => expected(s) && completePreview(s) && sgMessageValues(s) && Object.keys(sgMessageValues(s)).some(key=>s.editable_fields?.includes(key))
    && s.state === 'pending' && !s.superseded_by_id && owner() && !sg.error && !sg.detailErrors.has(s.id)
    && sg.verified.get(s.id) === s.status_revision && !sg.busy.has(busyKey(sg.slug,s));
  const fitMessage = field => { field.style.height='auto'; field.style.height=field.scrollHeight+'px'; };
  async function readReference(slug, id) {
    if(typeof extensionsLoad === "function")await extensionsLoad();
    let s=coreData(await api(path(slug,id))).signoff;
    if(!s || s.id!==id)throw new Error('The proposal did not match this reference.');
    const seen=new Set([id]);
    while(!options.expectedBinding && sgMessageValues(s) && s.superseded_by_id && !messageEdits.has(s.id)) {
      if(seen.has(s.superseded_by_id) || seen.size>=64)throw new Error('Could not resolve the revised proposal.');
      const next=coreData(await api(path(slug,s.superseded_by_id))).signoff;
      if(!next || next.id!==s.superseded_by_id || next.review_business_id!==s.review_business_id
        || next.operation_name!==s.operation_name)throw new Error('The revised proposal did not match this reference.');
      if(next.preview?.owner_edit?.previous_id!==s.id)break;
      seen.add(next.id);s=next;
    }
    return s;
  }
  function paintMessageEditor(detail,s) {
    const edit=messageEdits.get(s.id), enabled=canEditMessage(s);
    if(!edit && !enabled)return;
    const values=sgMessageValues(s);
    if(!values)return;
    for(const block of detail.querySelectorAll('[data-sg-edit-field]')) {
      const key=block.dataset.sgEditField;
      if(!s.editable_fields?.includes(key) && !edit)continue;
      let field=block;
      if(block.tagName!=='TEXTAREA') {
        field=document.createElement('textarea');field.className='sg-text sg-message-editor';
        field.dataset.sgEditField=key;field.rows=1;
        field.value=values[key];field.setAttribute('aria-label',key==='text'?'WhatsApp message text':key==='subject'?'Email subject':'Email body');field.spellcheck=true;
        field.onfocus=()=>{
          const current=sg.items.get(s.id);
          if(!current || messageEdits.has(s.id) || !canEditMessage(current))return;
          const values=sgMessageValues(current);
          messageEdits.set(s.id,{values:{...values},original:{...values},binding:binding(current),revision:current.status_revision,error:'',saving:false,receipt:null});
          render();
        };
        block.replaceWith(field);
      }
      field.readOnly=!edit || !!edit.saving || !!edit.receipt;
      field.dataset.editable=enabled?'true':'false';
      field.removeAttribute('title');
      if(edit && field.value!==edit.values[key])field.value=edit.values[key];
      fitMessage(field);
    }
  }
  function messageEditControls(s) {
    const edit=messageEdits.get(s.id);
    if(!edit)return '';
    const stale=edit.binding!==binding(s) || edit.revision!==s.status_revision || s.state!=='pending' || s.superseded_by_id;
    return `<p class="sg-notice" role="status">${esc(edit.error || (stale?'This proposal changed. Your text is kept; discard to load the current proposal.':''))}</p><div class="sg-actions"><button type="button" class="sg-approve" data-sg="save-message"${edit.saving || (stale && !edit.receipt)?' disabled':''}>${edit.saving?'Saving…':edit.receipt?'Retry save':'Save'}</button><button type="button" class="sg-reject" data-sg="cancel-message"${edit.saving || edit.receipt?' disabled':''}>Discard</button></div>`;
  }

  async function saveMessage(s) {
    const edit=messageEdits.get(s.id);
    if(!edit || edit.saving)return;
    if(!edit.receipt && (!canEditMessage(s) || edit.binding!==binding(s) || edit.revision!==s.status_revision))return;
    const changes=Object.fromEntries(Object.entries(edit.values).filter(([key,value])=>value!==edit.original[key]));
    for(const [key,value] of Object.entries(changes)) {
      const problem=sgMessageFieldError(s,key,value);
      if(problem){edit.error=problem;render();return;}
    }
    if(!edit.receipt && !Object.keys(changes).length){messageEdits.delete(s.id);render();return;}
    const c=capture();
    edit.receipt ||= {changes,proposal_revision:s.proposal_revision,payload_hash:s.payload_hash,
      expected_status_revision:edit.revision,idempotency_key:newRequestId()};
    edit.saving=true;edit.error='';render();
    try {
      const next=coreData(await post(path(c.slug,s.id)+'/amend',edit.receipt)).signoff;
      if(!next || next.supersedes_id!==s.id || next.operation_name!==s.operation_name || next.review_business_id!==s.review_business_id)throw new Error('The saved revision could not be verified. Retry save to check it.');
      messageEdits.delete(s.id);
      if(current(c)) {
        // Invalidate reads started before this save so they cannot restore the old card.
        sg.listRequest++;sg.detailRequest++;sg.loading=false;sg.detailLoading=null;
        sg.items.delete(s.id);sg.order=sg.order.filter(id=>id!==s.id);sg.verified.delete(s.id);
        merge(next);sg.selected=next.id;sg.selections.set(sg.slug,next.id);sg.verified.set(next.id,next.status_revision);
        render();
      }
      sgViews.forEach(peer=>{if(peer!==view)peer.refresh();});
      if(typeof attentionQueue!=='undefined')void attentionQueue.refreshSignoffs();
    } catch(error) {
      edit.error=error.message || 'Could not save. Your text is kept.';
      if(error.code && error.code!=='invalid_response') {
        edit.receipt=null;
        if(current(c) && error.details?.signoff)merge(error.details.signoff);
      } else edit.error='The response was lost. Retry save to check the same edit.';
    } finally {edit.saving=false;if(current(c))render();}
  }
  function html(el, value) {
    if (el && el._sgHtml !== value) { el.innerHTML = value; el._sgHtml = value; }
  }
  // Decision feedback is its own field: never a private or team channel draft.
  function draft(s) {
    const key = draftKey(s);
    if (!key) return "";
    if (!sg.drafts.has(key)) sg.drafts.set(key, "");
    return sg.drafts.get(key);
  }
  function rejectionNeedsMessage(s) {
    return s.rejection_requires_message;
  }
  function saveDraft(key, value) {
    if (!key) return;
    sg.drafts.set(key, value);
    sgViews.forEach(view => view.syncDraft(key, value));

  }
  function merge(item) {
    if (!item || !item.id || !labels[item.state]) return false;
    const old = sg.items.get(item.id);
    if (old && old.status_revision > item.status_revision) return false;
    if (old && old.payload_hash && binding(old) !== binding(item) && !options.expectedBinding) {
      sg.verified.delete(item.id);
      sg.detailErrors.set(item.id, new Error("The proposal binding changed. Reload the proposal."));
      return false;
    }
    if (!old) sg.order.push(item.id);
    if (old && old.state !== item.state && sg.counts) {
      sg.counts[old.state] = Math.max(0, (sg.counts[old.state] || 0) - 1);
      sg.counts[item.state] = (sg.counts[item.state] || 0) + 1;
    }
    // Background send results change without a new status_revision; the newest outcome object replaces the old one.
    const next = { ...old, ...item };
    if (old && !("outcome" in item)) delete next.outcome;
    sg.items.set(item.id, next);
    return true;
  }
  function previewHtml(s) {
    if (s.category === "platform_action" && s.platform?.available !== false) return sgPlatformPreview(s);
    const preview = s.preview;
    if (s.platform?.available === false) return '<p class="sg-notice">This action’s integration is no longer installed. This is saved history; execution is disabled.</p>'
      + (preview?.rationale ? `<div class="sg-rationale"><p>${renderSafeLinks(preview.rationale)}</p></div>` : '')
      + (Array.isArray(preview?.changes) ? sgChangesHtml(preview) : '');
    if (s.input) return sgInputExplanation(s.input.explanation);
    if (!preview || !Array.isArray(preview.changes)) return "";
    const effects = (preview.effects || []).map(effect => typeof effect === "string" ? effect : effect && effect.description).filter(effect => typeof effect === "string");
    const proposal = sgProposal(s, effects);
    const content = proposal ? proposal.html + (proposal.ok ? sgChangesHtml({ ...preview, changes: preview.changes.filter(c => !(c && c.resource && (proposal.hiddenResources || []).includes(c.resource.resource_type))) }) : "") : sgChangesHtml(preview);
    const shownEffects = proposal && proposal.ok && Array.isArray(proposal.effects) ? proposal.effects : effects;
    return `${s.legacy_review_routing ? '<p class="sg-notice">Created under previous review routing.</p>' : ""}${preview.rationale ? `<div class="sg-rationale${proposal ? " sg-message-rationale" : ""}"><h3>${s.proposer_agent_id ? "Agent's explanation" : "Proposer's explanation"}</h3><p>${renderSafeLinks(preview.rationale)}</p></div>` : ""}${content}
      ${shownEffects.length ? `<div class="sg-effects"><h3>Effects</h3><ul>${shownEffects.map(effect => `<li>${esc(effect)}</li>`).join("")}</ul></div>` : ""}`;
  }
  function completePreview(s) {
    if (s.input) return !!s.preview && Array.isArray(s.input.choices);
    if (s.category === "platform_action") return sgPlatformComplete(s);
    if (!(s.preview && Array.isArray(s.preview.changes) && s.preview.changes.length > 0 && s.preview.changes.every(change =>
      change && (change.kind === "structured" ? Array.isArray(change.fields) : change.kind === "text" && typeof change.diff === "string" && "before" in change && "after" in change)))) return false;
    // Exact-content operations are approvable only when their frozen content is present and valid.
    const effects = (s.preview.effects || []).map(effect => typeof effect === "string" ? effect : effect && effect.description).filter(effect => typeof effect === "string");
    const proposal = sgProposal(s, effects);
    return !proposal || proposal.ok;
  }
  function appliedRefresh(s, slug) {
    const key = busyKey(slug, s);
    if (s.state !== "applied" || sg.refreshed.has(key) || workspace() !== slug) return;
    sg.refreshed.add(key);
    if (["business_archive", "business_create"].includes(s.operation_name)) loadBiz({ preserveNavigation: true });
    if (options.global) { if (typeof rtRefresh === "function") rtRefresh(); return; }
    if (typeof refreshTasksSoon === "function") refreshTasksSoon();
    // Each affected view owns its normal refresh and editor protections.
    if (typeof acRefresh === "function") acRefresh();
    if (typeof rtRefresh === "function" && tView === "routines") rtRefresh();
    if (typeof refreshSideSoon === "function") refreshSideSoon();
  }
  // A rejection that needs a message: inline cards reveal and focus the existing feedback field, other views toast as before.
  function messageRequired() {
    if (!options.inline) { toast('Add a message before rejecting. Use the decision message field.'); return; }
    const row = sg.mount && sg.mount.querySelector('.sg-row');
    if (!row) return;
    row.classList.add('sg-open');
    const toggle = row.querySelector('.sg-inline-toggle');
    if (toggle) { toggle.setAttribute('aria-expanded', 'true'); toggle.textContent = 'Hide details'; }
    row.querySelector('.sg-feedback')?.focus({ preventScroll: true });
  }
  function detailPaint(row, s) {
    const detail = row.querySelector(".sg-detail");
    detail.hidden = s.id !== sg.selected;
    row.querySelector(".sg-rowhead").setAttribute("aria-expanded", String(!detail.hidden));
    if (detail.hidden) return;
    const error = sg.detailErrors.get(s.id), busy = sg.busy.get(busyKey(sg.slug, s));
    const verified = sg.verified.get(s.id) === s.status_revision;
    const canReview = expected(s) && verified && completePreview(s) && owner() && !sg.error && !error && !s.superseded_by_id;
    if (!detail.querySelector(".sg-preview")) {
      detail.innerHTML = `<div class="sg-detailnotice"></div><div class="sg-attention-meta"></div><div class="sg-email-context"></div><div class="sg-preview"></div><div class="sg-choices"></div><div class="sg-savedresponse"></div><div class="sg-feedbackwrap"></div><div class="sg-actionarea"></div><div class="sg-conversation"></div><div class="sg-work" aria-label="Related work"></div>`;
      if (!options.global && !options.inline) workRelatedChips(detail.querySelector('.sg-work'),sg.slug,'signoffs',s.id);
    }
    // A question once tied to an email conversation keeps a historical note; the conversation itself is never fetched.
    const emailContext=detail.querySelector('.sg-email-context');
    if(s.email_context)historicalEmailMount(emailContext);else emailContext.replaceChildren();
    const conversationHost=detail.querySelector(".sg-conversation");
    // Inline review cards omit conversation UI; the surrounding chat owns discussion.
    const channel = conversationSource(s.id, s.review_business_id);
    const conversation = options.inline || options.omitConversationFor === conversationKey(channel) ? null : conversationMount(conversationHost,channel,{
      inlineComposer:!!options.inline,onInput:()=>render(),onToggle:()=>render()});
    conversationHost.hidden = !conversation;
    for(const control of detail.querySelectorAll(".sg-conversation textarea,.sg-conversation .gchat-send"))control.disabled=!!busy || !!conversation?.state.sending || !!conversation?.state.submission;
    detail.setAttribute("aria-busy", String(!!busy || sg.detailLoading === s.id));
    const notice = !expected(s) && options.expectedBinding ? `<p class="sg-notice">This question is read-only or has changed. Open the current change to review it.</p>` : error ? errorHtml(error, "detail") : !verified ? `<p class="sg-notice">${sg.detailLoading === s.id ? "Loading prepared change..." : "This detail is out of date."}</p>` : !completePreview(s) && s.platform?.available !== false ? errorHtml("The proposal could not be loaded completely. Reload it before approving.", "detail") : "";
    html(detail.querySelector(".sg-detailnotice"), notice);
    const attention = s.attention || {};
    const attentionText = [attention.urgent ? 'Urgent' : '', attention.blocking ? 'Blocking' : '', attention.reason || '',
      attention.urgent || attention.blocking ? `Set by ${attention.priority_updated_by_name || 'not recorded'}` : '',
      attention.deadline ? `Deadline: ${stamp(attention.deadline)}` : '',
      attention.snoozed_until && Date.parse(attention.snoozed_until) > Date.now() ? `Snoozed until ${stamp(attention.snoozed_until)}` : ''].filter(Boolean).join(' · ');
    html(detail.querySelector('.sg-attention-meta'), attentionText ? `<p class="sg-notice">${esc(attentionText)}</p>` : '');
    if(!messageEdits.has(s.id) || !detail.querySelector(".sg-message-editor"))html(detail.querySelector(".sg-preview"), sgEmailOutcomeHtml(s) + previewHtml(s));
    // The collapsed class must settle first so an expanding editor is measured while visible.
    const collapsible = compactEmail && sgIsEmail(s);
    row.classList.toggle('sg-email-collapsed', collapsible && !emailExpanded.has(s.id));
    paintMessageEditor(detail,s);
    const peek = detail.querySelector('[data-sg-email-peek]'), bodyField = detail.querySelector('[data-sg-edit-field="body"]');
    if (peek && bodyField) { const text = bodyField.tagName === 'TEXTAREA' ? bodyField.value : bodyField.textContent; if (peek.textContent !== text) peek.textContent = text; }
    let history=detail.querySelector('.sg-edit-history');
    if(!options.inline && s.preview?.owner_edit && !history){
      history=document.createElement('details');history.className='sg-edit-history';
      history.innerHTML='<summary>Message edit history</summary><p>Edited by the owner. The previous proposal is retained.</p><button type="button" class="sg-command" data-previous-message>Show previous message</button><div class="sg-previous-message"></div>';
      detail.querySelector('.sg-preview').after(history);
      history.querySelector('button').onclick=async()=>{
        const host=history.querySelector('.sg-previous-message');
        try{const previous=coreData(await api(path(sg.slug,s.supersedes_id))).signoff;
          if(previous?.id!==s.supersedes_id || previous.review_business_id!==s.review_business_id)throw new Error('History is unavailable.');
          const values=sgMessageValues(previous);host.textContent=values?(values.text ?? ('Subject: '+values.subject+'\n\n'+values.body)):'History is unavailable.';
        }catch(e){host.textContent=e.message;}
      };
    }
    const chosen = sg.choices.get(draftKey(s)) || [];
    const submitted = s.decision?.selected_choices || [];
    const choiceMode = s.input?.selection_mode;
    html(detail.querySelector(".sg-choices"), s.input && choiceMode !== "none" ?
      `<div role="${choiceMode === "single" ? "radiogroup" : "group"}" aria-label="Answer choices">${s.input.choices.map((choice,index) => {
        const checked = (s.state === "pending" ? chosen : submitted).includes(choice.id);
        return `<button type="button" class="sg-choice" role="${choiceMode === "single" ? "radio" : "checkbox"}" aria-checked="${checked}" data-choice="${esc(choice.id)}" ${!canReview || busy || s.state !== "pending" ? "disabled" : ""}>
          ${options.inline ? "" : `<span class="sg-choice-marker ${choiceMode === "single" ? "sg-choice-letter" : "sg-choice-ball"}" aria-hidden="true">${choiceMode === "single" ? String.fromCharCode(65+index) : ""}</span>`}
          <span class="sg-choice-name">${esc(choice.label)}</span>${options.inline ? "" : `<span aria-hidden="true">—</span>
          <span class="sg-choice-explanation" title="${esc(choice.explanation)}">${esc(choice.explanation)}</span>`}</button>`;
      }).join("")}</div>` : "");
    html(detail.querySelector(".sg-savedresponse"), s.decision ? `<p class="sg-response-notes">${esc(s.decision.message_id ? "" : (s.decision.notes ?? s.decision.feedback ?? ""))}</p><p class="sg-meta">Submitted ${esc(stamp(s.decision.at))}</p>` : "");
    const feedback = detail.querySelector(".sg-feedbackwrap");
    if (s.state === "pending" && completePreview(s) && owner() && !s.superseded_by_id && (!options.inline || s.input || rejectionNeedsMessage(s))) {
      const requirement = s.input ? (s.input.notes_required ? "(required)" : "(optional)") : rejectionNeedsMessage(s) ? "(required to reject)" : "(optional)";
      if (!feedback.querySelector("textarea")) {
        feedback.innerHTML = `<label class="sg-label" for="sg-feedback-${prefix}${esc(s.id)}">${s.input ? "Answer" : "Message to send with decision"} ${requirement}</label><textarea id="sg-feedback-${prefix}${esc(s.id)}" class="sg-feedback" rows="3" spellcheck="true"></textarea>`;
        feedback.querySelector("textarea").value = draft(s);
      }
      feedback.querySelector("textarea").disabled = !!busy || !canReview;
    } else feedback.innerHTML = "";
    const disabled = !canReview || !!busy ? " disabled" : "";
    let controls = "";
    const withFeedback = !s.input && !!draft(s).trim();
    const plainDecisionControls = s.state === "pending" && owner() && completePreview(s) && !s.superseded_by_id
      ? `<div class="sg-actions">${verified ? `<button type="button" class="sg-approve" data-sg="approve"${disabled}>${SVG_CHECK}${withFeedback ? "Send and approve" : "Approve"}</button>` : ""}<button type="button" class="sg-reject" data-sg="reject"${disabled}>${SVG_X}${withFeedback ? "Send and reject" : "Reject"}</button></div>` : "";
    if (s.input) controls = s.state === "pending" && owner() && completePreview(s) && !s.superseded_by_id ? `<div class="sg-actions"><button type="button" class="sg-approve" data-sg="respond"${disabled}>Submit</button></div>` : "";
    else controls = plainDecisionControls;
    if (busy) controls += `<p class="sg-notice" role="status">${busy.unknown ? "The response was lost. Checking the saved decision..." : "Saving decision..."}</p>${busy.unknown ? `<button type="button" class="sg-command" data-sg="reconcile"${busy.checking ? " disabled" : ""}>Check decision</button>` : ""}`;
    if (verified && !owner()) controls += `<p class="sg-notice">Signoff review requires an owner session.</p>`;
    if (options.execution && s.category === "platform_action" && s.state !== "pending") {
      const execution = s.platform?.execution || {};
      const pending = sg.executions.get(`${sg.slug}:${s.id}:execution`);
      const disabledExecution = !verified || !owner() || !!sg.error || !!error || !!pending;
      const command = (action, label) => `<button type="button" class="sg-command" data-sg="${action}"${disabledExecution ? " disabled" : ""}>${label}</button>`;
      const cancellation = sgPlatformCancellation(s);
      if (cancellation.canCancel) controls += command("cancel-execution", cancellation.label);
      if (cancellation.notice) controls += `<p class="sg-notice">${esc(cancellation.notice)}</p>`;
      if (!execution.historical && s.state === "applied" && (execution.cancellation?.state === "failed" || execution.error && execution.error.code !== "external_failed" && ["failed", "reconciliation_required"].includes(s.execution_state)))
        controls += command("retry-execution", "Retry interrupted work");
      if (execution.error?.message) controls += `<p class="sg-notice">${esc(execution.error.message)}</p>`;
      if (execution.historical) controls += '<p class="sg-notice">Imported history. Execution is disabled.</p>';
      if (execution.cancellation) controls += `<p class="sg-notice">Cancellation: ${esc(execution.cancellation.outcome || execution.cancellation.state || "requested")}</p>`;
      if (execution.result?.url && /^https?:\/\//.test(execution.result.url)) controls += `<p><a href="${esc(execution.result.url)}" target="_blank" rel="noopener">View result</a></p>`;
      if (pending) controls += `<p class="sg-notice">${pending.unknown ? "Response lost. Check the saved execution request." : "Saving execution request…"}</p>${pending.unknown ? '<button type="button" class="sg-command" data-sg="check-execution">Check execution</button>' : ""}`;
    }
    html(detail.querySelector(".sg-actionarea"), messageEdits.has(s.id) ? messageEditControls(s) : controls);
  }
  function render() {
    if (!active() || !sg.mount || !sg.mount.isConnected) return;
    const scroll = sg.mount.scrollTop;
    const focused = document.activeElement;
    const oldRow = focused && focused.closest && focused.closest(".sg-row");
    const selectedRow = [...sg.mount.querySelectorAll(".sg-row")].find(row => row.dataset.id === sg.selected);
    const anchorTop = selectedRow && selectedRow.getBoundingClientRect().top;
    sg.mount.querySelector(".sg").setAttribute("aria-busy", String(sg.loading));
    html(sg.mount.querySelector(".sg-listnotice"), !sg.slug ? `<p class="sg-empty">Pick a project first.</p>` : sg.error ? errorHtml(sg.error, "list") + (sg.items.size ? `<p class="sg-notice">Showing the last update. Review is disabled until refreshed.</p>` : "") : sg.loading && !sg.loaded ? `<p class="sg-empty">Loading signoffs...</p>` : "");
    const attention = typeof sgAttention !== 'undefined' ? sgAttention : {urgent:[]};
    // The global endpoint returns the same business-scoped proposals as a business
    // queue, so render from that unified result instead of duplicating attention
    // summaries (which used to produce a broken, non-actionable urgent toast).
    // The selected account-email Signoff stays visible after approval so its send outcome can be read; deselecting drops it.
    const retainedEmail = s => !options.execution && s.id === sg.selected && s.state !== "pending" && s.outcome?.kind === "account_email";
    const urgentLocal = s => s.state === 'pending' && (s.attention?.urgent || s.attention?.blocking);
    const groups = { urgent: sg.order.map(id=>sg.items.get(id)).filter(s=>s && urgentLocal(s)), waiting: sg.order.map(id => sg.items.get(id)).filter(s => s && !urgentLocal(s) && (s.state === "pending" || messageEdits.has(s.id) || retainedEmail(s))) };
    if (options.execution) groups.execution = sg.order.map(id => sg.items.get(id)).filter(s => s && s.state !== "pending" && !messageEdits.has(s.id));
    for (const name of Object.keys(groups)) {
      const container = sg.mount.querySelector(`.sg-${name}`);
      const content = container.querySelector(".sg-rows");
      container.hidden = !sg.slug || (!sg.loaded && !sg.items.size);
      html(container.querySelector(".sg-sectionempty"), !options.item && name === "waiting" && !groups[name].length ? `<p class="sg-empty">${attention.urgent.length ? "No other signoffs waiting." : "No signoffs waiting."}</p>` : "");
      groups[name].forEach((s, index) => {
        let row = [...sg.mount.querySelectorAll(".sg-row")].find(node => node.dataset.id === s.id);
        if (!row) {
          row = document.createElement("article"); row.className = "sg-row mkcard"; row.dataset.id = s.id;
          row.innerHTML = `<button type="button" class="sg-rowhead" aria-expanded="false" aria-controls="sg-detail-${prefix}${esc(s.id)}"><span class="sg-rowtitle"></span><span class="sg-state"></span><span class="sg-rowmeta"></span></button><div class="sg-row-actions"></div><div class="sg-detail" id="sg-detail-${prefix}${esc(s.id)}" hidden></div>`;
        }
        if (content.children[index] !== row) content.insertBefore(row, content.children[index] || null);
        row.dataset.state = s.state;
        row.querySelector(".sg-rowtitle").textContent = s.title || s.preview && s.preview.title || s.operation_name;
        html(row.querySelector(".sg-state"), `<span class="sg-state-tags">${sgAttentionTags(s) || (s.state === "pending" ? "" : esc(sgItemStatus(s, labels)))}</span><time datetime="${esc(s.created_at)}" title="${esc(stamp(s.created_at))}">${esc(age(s.created_at))}</time>`);
        if (options.inline) html(row.querySelector(".sg-state"), `<span class="sg-state-tags">${esc(sgItemStatus(s, labels))}</span>`);
        const proposer = s.proposer_display_name || "Unknown proposer";
        const initiator = s.initiated_by_principal_id ? ` / initiated by ${s.initiated_by_display_name || "Unknown initiator"}` : "";
        const business = state.bizs.find(b => b.id === s.review_business_id || b.business_id === s.review_business_id);
        const businessPortrait = business ? taskBusinessLabel(business.name || business.slug)
          : s.review_business_id == null ? taskBusinessLabel('Global', true)
          : taskBusinessLabel(bizName(s.review_business_id));
        const agentName = (business?.agents || []).find(name => name.toLowerCase() === proposer.toLowerCase());
        // Global proposers such as Ordubashy are not in the business's agent list.
        const agent = state.agents.find(a => a.agent_id === s.proposer_agent_id || a.principal_id === s.proposer_principal_id)
          || globalAgentProfile(s.proposer_agent_id) || globalAgentProfile(s.proposer_principal_id);
        if (!agent && s.proposer_agent_id && !globalAgentProfiles.length && !sg.globalProfilesRequested) {
          sg.globalProfilesRequested = true; loadGlobalAgentProfiles().then(render).catch(() => {});
        }
        const appearance = {...(business?.agent_meta?.[agentName] || {}), ...(agent || {})};
        const proposerPortrait = identityAvatar(s.proposer_agent_id ? 'agent' : s.proposer_principal_id === owner() ? 'founder' : 'unknown', proposer, appearance, proposer + initiator, true);
        html(row.querySelector(".sg-rowmeta"), options.inline ? `<span class="sg-inline-kind">Signoff</span>${businessPortrait}` : `${businessPortrait}${proposerPortrait}`);
        row.dataset.input = s.input ? "1" : "";
        if (options.inline) {
          row.classList.add('sg-open');
          const icon = sgIsEmail(s) ? aicon('mail','currentColor',17)
            : (SG_MESSAGE_RENDERERS.get(s.operation_name)?.message || /message/.test(s.operation_name)) ? RW_CHAT_ICON : T_VIEW_ICON.signoffs;
          row.querySelector('.sg-rowtitle').innerHTML = `<span class="sg-reference-icon" aria-hidden="true">${icon}</span><span>${esc(s.title || s.preview?.title || s.operation_name)}</span>`;
        }
        const quickReview = expected(s) && s.state === "pending" && !s.input && owner() && !s.superseded_by_id;
        const quickReady = quickReview && !messageEdits.has(s.id) && !sg.error && !sg.detailErrors.has(s.id) && !sg.busy.has(busyKey(sg.slug, s));
        const quick = row.querySelector(".sg-row-actions");
        html(quick, quickReview ? `<div class="sg-actions"><button type="button" class="sg-reject" data-sg="reject"${quickReady ? "" : " disabled"}>${SVG_X}Reject</button><button type="button" class="sg-review" data-sg="review"${quickReady ? "" : " disabled"}>Review</button></div>` : "");
        quick.hidden = !quickReview;
        detailPaint(row, s);
      });
      [...content.children].forEach(row => { if (!groups[name].some(s => s.id === row.dataset.id)) { disposeNested(row); row.remove(); } });
    }
    const foreign = [];
    const urgentSection = sg.mount.querySelector('.sg-urgent');
    urgentSection.hidden = !groups.urgent.length && !foreign.length;
    html(urgentSection.querySelector('.sg-foreign'), foreign.map(item=>`<button type="button" class="sg-urgent-link" data-attention-id="${esc(item.id)}"><small>${esc(item.business_name)}</small><span>${esc(item.title)}</span>${sgAttentionTags(item)}</button>`).join(''));
    const more = sg.mount.querySelector(".sg-more");
    const loadedPending = [...sg.items.values()].filter(s => s.state === 'pending').length;
    more.hidden = !sg.nextCursor || (options.waitingOnly && Number.isInteger(sg.counts?.pending) && loadedPending >= sg.counts.pending);
    more.disabled = sg.loading;
    if (focused && !focused.isConnected && oldRow) {
      const row = [...sg.mount.querySelectorAll(".sg-row")].find(r => r.dataset.id === oldRow.dataset.id);
      if (row) row.querySelector(".sg-rowhead").focus({ preventScroll: true });
    }
    const anchor = selectedRow && selectedRow.isConnected ? selectedRow.getBoundingClientRect().top : null;
    sg.mount.scrollTop = scroll + (anchorTop != null && anchor != null ? anchor - anchorTop : 0);
  }
  async function loadDetail(id, force = false) {
    if (!active() || !sg.slug) return;
    const c = capture(), request = ++sg.detailRequest;
    const old = sg.items.get(id);
    if (!force && old && completePreview(old) && sg.verified.get(id) === old.status_revision) { render(); return; }
    sg.detailLoading = id; sg.detailErrors.delete(id); render();
    try {
      const s = await readReference(c.slug,id);
      if (!current(c) || request !== sg.detailRequest || sg.selected !== id) return;
      if (!s || (!options.global && s.review_business_id !== sg.businessId)) throw new Error("The proposal did not match this workspace.");
      if(s.id!==id){sg.items.delete(id);sg.order=sg.order.filter(key=>key!==id);sg.selected=s.id;sg.selections.set(sg.slug,s.id);}
      if (merge(s)) {
        sg.verified.set(s.id, s.status_revision);
        if(typeof attentionQueue!=='undefined')attentionQueue.signoffViewed(s);
        const receipt = sg.busy.get(busyKey(c.slug, s));
        if (receipt && s.state !== "pending") finishReceipt(receipt, s);
        appliedRefresh(s, c.slug);
      }
    } catch (error) {
      if (current(c) && request === sg.detailRequest && sg.selected === id) {
        sg.detailErrors.set(id, error); sg.verified.delete(id);
        if (isAuth(error)) sg.session = null;
        dropDenied(error);
      }
    } finally {
      if (mounted(c) && request === sg.detailRequest) {
        sg.detailLoading = null; render();
        if (sg.reveal === id && sg.selected === id) {
          [...sg.mount.querySelectorAll('.sg-row')].find(row=>row.dataset.id===id)?.scrollIntoView({block:'nearest'});
          sg.reveal=null;
        }
      }
    }
  }
  async function refresh(more = false) {
    if (!active() || !sg.slug || sg.loading) return;
    const c = capture(), request = ++sg.listRequest;
    const cursor = more ? sg.nextCursor : null;
    // Always states=pending: SignoffService._list_operation binds updated_cursor
    // to the exact filter set a call used, so a states=pending cursor could never
    // be redeemed by a no-states updated_after call or vice versa -- but this
    // pane never sends updated_after at all, only the browse cursor above, so
    // there's no mixed-filter cursor to protect against here. /live owns the
    // separate no-states change stream (for panes that are closed or between
    // polls); this refresh just re-asks "what's pending now" every cycle and
    // prunes anything that fell out of that answer.
    const query = new URLSearchParams({ limit: "50", states: "pending" });
    if (options.category) query.set("category", options.category);
    if (cursor) query.set("cursor", cursor);
    sg.loading = true; render();
    try {
      if(typeof extensionsLoad === "function")await extensionsLoad();
      if (options.item) {
        const target = options.item();
        const responses = await Promise.all([readReference(c.slug, target.id), sg.session ? Promise.resolve(null) : api("/session")]);
        if (!current(c) || request !== sg.listRequest) return;
        if (responses[1]) state.session = sg.session = coreData(responses[1]);
        const item = responses[0];
        if (!item || item.review_business_id !== target.businessId || (!options.inline && options.category != null && item.category !== options.category)) throw new Error("The proposal did not match this workspace.");
        sg.businessId = target.businessId;
        sg.items.clear(); sg.order = []; sg.verified.clear();
        merge(item); sg.selected = item.id; sg.verified.set(item.id, item.status_revision);
        sg.nextCursor = null; sg.error = null; sg.loaded = true;
        appliedRefresh(item, c.slug);
        if(typeof attentionQueue!=='undefined')attentionQueue.signoffViewed(item);
        return;
      }
      const responses = await Promise.all([api(path(c.slug) + "?" + query), sg.session ? Promise.resolve(null) : api("/session")]);
      if (!current(c) || request !== sg.listRequest) return;
      if (responses[1]) state.session = sg.session = coreData(responses[1]);
      const data = coreData(responses[0]);
      if (!Array.isArray(data.items) || (!options.global && (!data.review_business_id || sg.businessId && data.review_business_id !== sg.businessId))) throw new Error("The signoff list did not match this workspace.");
      sg.businessId = data.review_business_id;
      // Every non-"more" page is the full current pending set, not an
      // incremental delta, so anything tracked but absent here has left
      // "pending" and is dropped -- except the open selection, which the
      // forced loadDetail below re-fetches by id and keeps visible, now
      // resolved, until the reviewer deselects it.
      if (!more) {
        const freshIds = new Set(data.items.map(item => item.id));
        for (const id of [...sg.order]) {
          const retained = messageEdits.has(id) || [...sg.busy.keys()].some(key => key.startsWith(`${sg.slug}:${id}:`)) || sg.executions.has(`${sg.slug}:${id}:execution`);
          if (id !== sg.selected && !freshIds.has(id) && !retained) { sg.items.delete(id); sg.order = sg.order.filter(existing => existing !== id); }
        }
      }
      data.items.forEach(item => {
        if (merge(item) && ["business_archive", "business_create"].includes(item.operation_name)) appliedRefresh(item, c.slug);
      });
      if (!cursor || more) sg.nextCursor = data.next_cursor || null;
      sg.counts = data.counts; sg.error = null; sg.loaded = true;
      if (options.onItems) options.onItems([...sg.items.values()]);
      if (!sg.selected) {
        const previous = sg.selections.get(sg.slug);
        if (previous && sg.items.has(previous)) sg.selected = previous;
      }
      render();
      if (sg.selected) await loadDetail(sg.selected, true);
    } catch (error) {
      if (current(c) && request === sg.listRequest) {
        sg.error = error; if (isAuth(error)) sg.session = null;
        if (options.item) dropDenied(error);
      }
    } finally {
      if (mounted(c) && request === sg.listRequest) { sg.loading = false; render(); }
    }
  }
  function select(id) {
    if (sg.selected === id) return;
    sg.selected = id; sg.selections.set(sg.slug, id);
    const s = sg.items.get(id);
    if (!s) merge({ id, title: "Replacement proposal", state: "pending", status_revision: 0 });
    sg.detailRequest++; render(); loadDetail(id);
  }
  function focusRow(event) {
    const head = event.target.closest(".sg-rowhead");
    if (head) select(head.closest(".sg-row").dataset.id);
  }
  function finishReceipt(receipt, s) {
    sg.busy.delete(receipt.key);
    sg.receipts.set(receipt.key, s);
    sgViews.forEach(peer => { if (peer !== view) peer.refresh(); });
    // The decision note lands in the shared signoff thread; refresh the owner conversation view of it.
    if(receipt.action!=='respond' && receipt.body.feedback) {
      const source=conversationSource(receipt.id, receipt.businessId);
      for(const host of document.querySelectorAll('[data-conversation]'))
        if(host.dataset.conversation===conversationKey(source))host._conversation?.refresh();
    }
    saveDraft(receipt.draftKey, "");
    sg.choices.delete(receipt.draftKey);
    if (window.sgAttentionRefresh) sgAttentionRefresh();
  }
  async function reconcile(receipt) {
    if (receipt.checking) return;
    receipt.checking = true; render();
    try {
      const data = coreData(await api(path(receipt.slug, receipt.id)));
      const s = data.signoff;
      if (!s || s.id !== receipt.id || binding(s) !== receipt.binding || s.review_business_id !== receipt.businessId) throw new Error("The saved decision could not be verified.");
      if (s.state !== "pending") {
        finishReceipt(receipt, s);
        if (workspace() === receipt.slug && sg.slug === receipt.slug) {
          merge(s); sg.verified.set(s.id, s.status_revision); sg.detailErrors.delete(s.id); appliedRefresh(s, receipt.slug);
        }
      } else {
        // Replaying this exact body/key is safe even when the original write still owns its claim.
        await submit(receipt, false);
      }
    } catch (error) {
      if (workspace() === receipt.slug && sg.selected === receipt.id) sg.detailErrors.set(receipt.id, error);
    } finally { receipt.checking = false; render(); }
  }
  async function submit(receipt, autoReconcile = true) {
    try {
      const data = coreData(await post(path(receipt.slug, receipt.id) + "/" + receipt.action, receipt.body));
      const s = data.signoff;
      if (!s || s.id !== receipt.id || binding(s) !== receipt.binding || s.review_business_id !== receipt.businessId) throw new Error("The decision response could not be verified.");
      finishReceipt(receipt, s);
      if (workspace() === receipt.slug && sg.slug === receipt.slug) {
        merge(s); sg.verified.set(s.id, s.status_revision); sg.detailErrors.delete(s.id); appliedRefresh(s, receipt.slug);
      }
    } catch (error) {
      const s = error.details && error.details.signoff;
      if (s && s.id === receipt.id && (!options.expectedBinding || binding(s) === receipt.binding) && s.state !== "pending") {
        finishReceipt(receipt, s);
        if (workspace() === receipt.slug && sg.slug === receipt.slug) {
          merge(s); sg.verified.delete(s.id); sg.detailErrors.set(s.id, error);
          if (active() && sg.selected === s.id) loadDetail(s.id, true);
        }
      } else if (error.code && error.code !== "invalid_response") {
        sg.busy.delete(receipt.key);
        if (workspace() === receipt.slug && sg.slug === receipt.slug) {
          sg.detailErrors.set(receipt.id, error); sg.verified.delete(receipt.id);
          if (isAuth(error)) sg.session = null;
          if (options.expectedBinding && !isAuth(error)) void loadDetail(receipt.id, true);
        }
      } else {
        receipt.unknown = true;
        if (autoReconcile) await reconcile(receipt);
      }
    } finally { render(); }
  }
  async function quickDecide(id, action) {
    const listed = sg.items.get(id);
    if (messageEdits.has(id))return;
    if (action !== "reject" || !active() || !listed || !expected(listed) || !owner() || listed.state !== "pending" || listed.input || listed.superseded_by_id || sg.error) return;
    const c = capture(), key = busyKey(c.slug, listed);
    if (sg.busy.has(key)) return;
    ownedWrites.add(key); sg.busy.set(key, { key, loading: true }); render();
    try {
      const detail = coreData(await api(path(c.slug, id))).signoff;
      if (!current(c)) return;
      if (!detail || detail.id !== id || (!options.global && detail.review_business_id !== sg.businessId)
          || detail.input || !completePreview(detail)) throw new Error("Open this signoff to review and respond.");
      merge(detail); sg.verified.set(id, detail.status_revision); sg.detailErrors.delete(id);
      if(action==='reject' && detail.rejection_requires_message) {
        sg.busy.delete(key);sg.selected=id;render();
        messageRequired();
        return;
      }
      const receipt = { key, slug: c.slug, businessId: detail.review_business_id || null, id, binding: binding(detail),
        draftKey: draftKey(detail), action, body: { proposal_revision: detail.proposal_revision, payload_hash: detail.payload_hash,
          expected_status_revision: detail.status_revision, idempotency_key: newRequestId(), feedback: null }, unknown: false };
      sg.busy.set(key, receipt); render();
      await submit(receipt);
    } catch (error) {
      sg.busy.delete(key);
      if (current(c)) { sg.detailErrors.set(id, error); if (isAuth(error)) sg.session = null; toast(error.message || "Could not save the decision."); }
    } finally { render(); }
  }
  function decide(action) {
    const s = sg.items.get(sg.selected);
    if (!active() || !s || !expected(s) || !owner() || !completePreview(s) || sg.error || sg.detailErrors.has(s.id) || sg.verified.get(s.id) !== s.status_revision || s.superseded_by_id) return;
    if (messageEdits.has(s.id))return;
    if (!["approve", "reject", "respond"].includes(action) || s.state !== "pending") return;
    const key = busyKey(sg.slug, s);
    if (sg.busy.has(key)) return;
    const body = { proposal_revision: s.proposal_revision, payload_hash: s.payload_hash, expected_status_revision: s.status_revision, idempotency_key: newRequestId() };
    if (action === "respond") {
      body.notes = draft(s);
      body.selected_choices = sg.choices.get(draftKey(s)) || [];
      if ((s.input.selection_required && !body.selected_choices.length && !body.notes.trim()) || (s.input.notes_required && !body.notes.trim())) {
        const notice = sg.mount.querySelector('.sg-detail:not([hidden]) .sg-detailnotice');
        html(notice, errorHtml('Complete the required answer before submitting.')); return;
      }
    } else {
      body.feedback = draft(s).trim() || null;
      if(action==='reject' && rejectionNeedsMessage(s) && !body.feedback) {
        messageRequired();return;
      }
    }
    const receipt = { key, slug: sg.slug, businessId: s.review_business_id || null, id: s.id, binding: binding(s), draftKey: draftKey(s), action, body, unknown: false };
    ownedWrites.add(key); sg.busy.set(key, receipt);

    render(); submit(receipt);
  }
  async function executionCommand(action) {
    const s = sg.items.get(sg.selected);
    if (!options.execution || !active() || !s || !owner()) return;
    const key = `${sg.slug}:${s.id}:execution`;
    let receipt = sg.executions.get(key);
    if (receipt && action !== "check-execution") return;
    if (!receipt) {
      if (sg.error || sg.verified.get(s.id) !== s.status_revision || sg.detailErrors.has(s.id)) return;
      const body = { signoff_id: s.id, status_revision: s.status_revision, idempotency_key: newRequestId() };
      if (action === "cancel-execution") {
        const original = {slug:sg.slug, id:s.id, revision:s.status_revision, binding:binding(s), businessId:s.review_business_id, principal:owner()};
        const accepted = options.confirmExecutionCancel
          ? await options.confirmExecutionCancel()
          : window.confirm("Request cancellation? Work that has already started may still need reconciliation.");
        const currentItem = sg.items.get(original.id);
        if (!accepted || !active() || sg.slug !== original.slug || workspace() !== original.slug || sg.selected !== original.id ||
            !currentItem || binding(currentItem) !== original.binding || currentItem.review_business_id !== original.businessId ||
            currentItem.status_revision !== original.revision || sg.verified.get(original.id) !== original.revision ||
            owner() !== original.principal || !original.principal || sg.error || sg.detailErrors.has(original.id) || sg.executions.has(key)) return;
      }
      receipt = { slug: sg.slug, id: s.id, operation: action === "cancel-execution" ? "signoffs_platform_cancel" : "signoffs_platform_retry", body };
      ownedWrites.add(key); sg.executions.set(key, receipt);
    }
    if (receipt.saving) return;
    receipt.saving = true; receipt.unknown = false; render();
    try {
      coreData(await post(`/biz/${encodeURIComponent(receipt.slug)}/operations/${receipt.operation}`, receipt.body));
      sg.executions.delete(key);
      if (sg.slug === receipt.slug && sg.selected === receipt.id) await loadDetail(receipt.id, true);
      sgViews.forEach(peer => peer.refresh());
    } catch (error) {
      if (error.code && error.code !== "invalid_response") {
        sg.executions.delete(key);
        if (sg.slug === receipt.slug) { sg.detailErrors.set(receipt.id, error); sg.verified.delete(receipt.id); }
      } else receipt.unknown = true;
    } finally { receipt.saving = false; render(); }
  }
  function mount() {
    const body = options.body();
    sg.generation++; sg.loading = false; sg.detailLoading = null;
    sg.mount = active() ? body : null;
    if (!sg.mount) return;
    if (options.item) {
      sg.items.clear(); sg.order = []; sg.selected = null; sg.verified.clear();
      sg.detailErrors.clear(); sg.error = null; sg.loaded = false;
    }
    disposeNested(sg.mount);
    body.classList.add("sg-body");
    body.classList.toggle("sg-inline", !!options.inline);
    body.innerHTML = `<div class="sg"><div class="sg-listnotice"></div><section class="sg-urgent"><h2>Urgent / blocking · All projects</h2><div class="sg-sectionempty"></div><div class="sg-rows mkcards"></div><div class="sg-foreign"></div></section><section class="sg-waiting"><div class="sg-sectionempty"></div><div class="sg-rows mkcards"></div></section>${options.execution ? '<section class="sg-execution"><h2>Execution and history</h2><div class="sg-sectionempty"></div><div class="sg-rows mkcards"></div></section>' : ""}<button type="button" class="sg-command sg-more" data-sg="more" hidden>Load more</button></div>`;
    body.onclick = event => {
      if(event.target.matches('.sg-message-editor'))return;
      const peek = event.target.closest('[data-sg-email-peek]');
      if (peek && compactEmail) {
        const id = peek.closest('.sg-row').dataset.id;
        emailExpanded.add(id); render();
        [...body.querySelectorAll('.sg-row')].find(node => node.dataset.id === id)?.querySelector('textarea[data-sg-edit-field="body"]')?.focus({preventScroll:true});
        return;
      }
      const toggle = event.target.closest('.sg-inline-toggle');
      if (toggle) {
        const open = toggle.closest('.sg-row').classList.toggle('sg-open');
        toggle.setAttribute('aria-expanded', String(open)); toggle.textContent = open ? 'Hide details' : 'Details'; return;
      }
      const urgent = event.target.closest('[data-attention-id]');
      if (urgent) { const item=sgAttention.urgent.find(i=>i.id===urgent.dataset.attentionId); if(item) sgAttentionNavigate(item.business_slug,item.id); return; }
      const choice = event.target.closest('[data-choice]');
      if (choice && !choice.disabled) {
        const s = sg.items.get(sg.selected), key = draftKey(s), id = choice.dataset.choice;
        const selected = sg.choices.get(key) || [];
        sg.choices.set(key, s.input.selection_mode === 'single' ? [id] : selected.includes(id) ? selected.filter(x => x !== id) : [...selected,id]);
        render();
        [...body.querySelectorAll('[data-choice]')].find(x => x.dataset.choice === id)?.focus();
        return;
      }
      const command = event.target.closest("[data-sg]");
      if (command && !command.disabled) {
        const commandRow = command.closest(".sg-row");
        const action = command.dataset.sg;
        if(action==='save-message' || action==='cancel-message') {
          const s=sg.items.get(commandRow?.dataset.id),edit=s && messageEdits.get(s.id);
          if(!edit)return;
          if(action==='save-message')void saveMessage(s);
          else if(!edit.saving && !edit.receipt){messageEdits.delete(s.id);commandRow.querySelector('.sg-preview')._sgHtml=null;render();void refresh();}
          return;
        }
        // A closed card never approves: Review opens the prepared detail, and Approve lives only there.
        if (commandRow && action === "review") { const id = commandRow.dataset.id; if (sg.selected === id) loadDetail(id, true); else select(id); return; }
        if (commandRow && commandRow.dataset.id !== sg.selected && action === "reject") {
          quickDecide(commandRow.dataset.id, action); return;
        }
        if (commandRow && commandRow.dataset.id !== sg.selected) {
          sg.selected = commandRow.dataset.id;
          sg.selections.set(sg.slug, sg.selected);
        }
        if (action === "list") refresh();
        else if (action === "more") refresh(true);
        else if (action === "detail") loadDetail(sg.selected, true);
        else if (action === "reconcile") { const s = sg.items.get(sg.selected); const receipt = s && sg.busy.get(busyKey(sg.slug, s)); if (receipt) reconcile(receipt); }
        else if (["cancel-execution", "retry-execution", "check-execution"].includes(action)) executionCommand(action);
        else decide(action);
        return;
      }
      const row = event.target.closest(".sg-row"); if (row) select(row.dataset.id);
    };
    // Focus moving to another place collapses the card; moving within it, or to nothing, does not.
    body.onfocusout = event => {
      const row = event.target.closest?.('.sg-row');
      if (row && event.relatedTarget && !row.contains(event.relatedTarget)) collapseEmail(row.dataset.id);
    };
    body.onkeydown = event => {
      if (event.key === 'Enter' && event.ctrlKey && !event.shiftKey && !event.metaKey && !event.altKey && !event.isComposing && event.target.matches('.sg-feedback')) {
        event.preventDefault(); event.stopPropagation();
        if (!event.repeat) event.target.closest('.sg-detail')?.querySelector('.sg-approve:not(:disabled)')?.click();
        return;
      }
      const choice = event.target.closest('[role="radio"]');
      if (choice && ['ArrowDown','ArrowRight','ArrowUp','ArrowLeft'].includes(event.key)) {
        event.preventDefault();
        const choices = [...choice.parentElement.querySelectorAll('[role="radio"]:not(:disabled)')];
        const delta = ['ArrowDown','ArrowRight'].includes(event.key) ? 1 : -1;
        choices[(choices.indexOf(choice)+delta+choices.length)%choices.length]?.click();
      }
    };
    // Typing a decision message updates the Send and approve/reject labels.
    body.oninput = event => {
      if(event.target.matches('.sg-message-editor')) {
        const edit=messageEdits.get(event.target.closest('.sg-row').dataset.id);
        if(edit && !edit.saving && !edit.receipt){edit.values[event.target.dataset.sgEditField]=event.target.value;edit.error='';fitMessage(event.target);}
        return;
      }
      if (event.target.matches(".sg-feedback")) { const s = sg.items.get(sg.selected); if (s) { saveDraft(draftKey(s), event.target.value); render(); } } };
    render(); refresh();
  }
  function businessChanged() {
    if (sg.slug === workspace()) return;
    sg.slug = workspace(); sg.businessId = null; sg.generation++;
    sg.items.clear(); sg.order = []; sg.selected = null; sg.reveal=null;
    sg.verified.clear(); sg.detailErrors.clear(); sg.detailRequest++;
    sg.counts = null; sg.nextCursor = null; sg.error = null; sg.loaded = false; sg.loading = false;
    mount();
  };
  const view = {
    open(id) { sg.reveal=id; sg.selected=id; sg.selections.set(workspace(),id); if (active()) { refresh(); loadDetail(id,true); } },
    businessChanged,
    render,
    hasUnsaved: () => messageEdits.size > 0,
    hasPendingWrite: () => {
      if ([...messageEdits.values()].some(edit => edit.saving || edit.receipt)) return true;
      if ([...(sg.mount?.querySelectorAll('.sg-conversation[data-conversation]') || [])].some(host => host._conversation?.state.sending)) return true;
      if ([...ownedWrites].some(key => sg.busy.has(key) || sg.executions.has(key))) return true;
      const ids = [...new Set([...(sg.selected ? [sg.selected] : []), ...(options.item?.()?.id ? [options.item().id] : [])])];
      return ids.some(id => [...sg.busy.keys()].some(key => key.startsWith(`${sg.slug}:${id}:`)) ||
        sg.executions.has(`${sg.slug}:${id}:execution`));
    },
    async save() {
      if (!messageEdits.size) return true;
      const s = sg.items.get(sg.selected);
      if (!s || !messageEdits.has(s.id)) return false;
      await saveMessage(s);
      return !messageEdits.has(s.id);
    },
    paint() { if (sg.slug !== workspace()) businessChanged(); else mount(); },
    refresh: () => refresh(),
    syncDraft(key, value) {
      const selected = sg.items.get(sg.selected);
      const row = sg.mount && [...sg.mount.querySelectorAll(".sg-row")].find(row => row.dataset.id === sg.selected);
      const textarea = row && row.querySelector(".sg-feedback");
      if (selected && draftKey(selected) === key && textarea && textarea.value !== value) textarea.value = value;
    },
    suspend() { disposeNested(sg.mount); sg.generation++; sg.mount = null; sg.loading = false; sg.detailLoading = null; },
    dispose() { this.suspend(); window.removeEventListener('resize',resizeMessages); sgViews.delete(view); document.removeEventListener('click', outsideClick); document.removeEventListener('pointerdown', outsidePointer, true); },
    livePaint(slug, live) {
      if (slug !== workspace() || slug !== sg.slug || active() || !live || !Number.isInteger(live.signoffs_pending)) return;
      sg.counts = { ...(sg.counts || {}), pending: live.signoffs_pending };
    },
  };
  const outsideClick = event => {
    if (options.item || !active() || !sg.selected || !sg.mount) return;
    if (event.composedPath().some(node => node.dataset?.action === 'open' || node.dataset?.attentionId)) return;
    const row = event.composedPath().find(node => node.classList?.contains('sg-row'));
    if (row && sg.mount.contains(row)) return;
    sg.selected=null; sg.selections.delete(sg.slug); sg.detailRequest++; render();
  };
  document.addEventListener('click', outsideClick);
  sgViews.add(view);
  return view;
}
const sgDrawer = sgCreateView({
  waitingOnly: true,
  active: () => state.scope !== "global" && typeof tView !== "undefined" && tView === "signoffs" && tPanelOpen(),
  body: () => $("#tbody"),
});
window.sgBusinessChanged = () => sgDrawer.businessChanged();
const sgGlobal = sgCreateView({
  waitingOnly: true,
  global: true,
  workspace: () => state.scope === "global" ? "scope:global" : null,
  active: () => state.scope === "global" && typeof tView !== "undefined" && tView === "signoffs" && tPanelOpen(),
  body: () => $("#tbody"),
  prefix: "global-",
});
window.tPaintSignoffs = () => {
  const global = state.scope === "global";
  (global ? sgDrawer : sgGlobal).suspend();
  (global ? sgGlobal : sgDrawer).paint();
};
// The one Chat Signoff card: native Chat and record threads mount exactly this view for a typed Signoff reference.
// A reference whose business cannot be resolved mounts nothing, so no broader route is ever tried.
function sgChatMount(ref, host, options = {}) {
  const slug = ref.business_slug || (ref.business_id ? (state.bizs || []).find(b => b.id === ref.business_id || b.business_id === ref.business_id)?.slug : null);
  if (ref.business_id && !slug) return null;
  let started = false, nearby = false, disposed = false, observer;
  const signoff = sgCreateView({active: () => started && (nearby || signoff.hasUnsaved()) && host.isConnected && (options.active ? options.active() : true), body: () => host, inline: true, compactEmail: true, execution: true, waitingOnly: false,
    workspace: () => slug || 'scope:global', global: !ref.business_id, omitConversationFor: options.omitConversationFor, dropOnDenied: !!options.dropOnDenied,
    item: () => ({id: ref.id, businessId: ref.business_id || null})});
  // Only nearby cards refresh. Keep the observer after first paint so scrolling
  // through history does not leave every old card fetching forever. Drafts stay
  // active, and returning to a card refreshes it without remounting its editor.
  const start = () => {
    if (disposed || started || !host.isConnected) return;
    started = true; signoff.paint();
  };
  queueMicrotask(() => {
    if (disposed || !host.isConnected) return;
    if (typeof IntersectionObserver === 'undefined') { nearby = true; start(); return; }
    observer = new IntersectionObserver(entries => {
      if (disposed) return;
      const wasNearby = nearby;
      nearby = entries[entries.length - 1].isIntersecting;
      if (nearby && !started) start();
      else if (nearby && !wasNearby) void signoff.refresh();
    }, {rootMargin: '300px 0px'});
    observer.observe(host);
  });
  const dispose = signoff.dispose.bind(signoff);
  signoff.dispose = () => { disposed = true; observer?.disconnect(); dispose(); };
  return signoff;
}
window.sgRefresh = () => Promise.all([...sgViews].map(view => view.refresh()));
window.sgLivePaint = (slug, live) => sgViews.forEach(view => view.livePaint(slug, live));

window.sgOpenItem = id => (state.scope === 'global' ? sgGlobal : sgDrawer).open(id);

window.sgAttentionViewsPaint = () => sgViews.forEach(view => view.render());
