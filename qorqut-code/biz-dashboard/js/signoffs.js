// Exact approved platform content is shared by the drawer and Marketing review.
function sgPlatformComplete(s) {
  const submission = s.platform?.submission;
  if (!submission || typeof submission.payload !== "object" || !submission.payload || Array.isArray(submission.payload)) return false;
  const p = submission.payload;
  if (submission.tool === "email.batch") return Array.isArray(p.recipients) && p.recipients.length > 0 && p.recipients.every(r => r && typeof r.email === "string" && typeof r.subject === "string" && typeof r.body === "string");
  if (submission.tool === "email.send") return Array.isArray(p.to) && p.to.length > 0 && typeof p.subject === "string" && typeof p.body === "string";
  if (["reddit.post", "reddit.comment"].includes(submission.tool)) return typeof p.body === "string";
  return submission.tool === "reddit.read" && typeof p.what === "string";
}
function sgPlatformStatus(s) {
  const labels = { awaiting_approval: "Awaiting approval", queued: "Queued", not_started: "Preparing execution",
    awaiting_human_fulfillment: "Awaiting human execution", dispatched: "Awaiting delivery result", completed: "Completed",
    canceled: "Canceled", failed: "Failed", reconciliation_required: "Needs reconciliation" };
  return `${s.decision_state === "approved" ? "Approved" : s.decision_state === "rejected" ? "Rejected" : "Pending"} · ${labels[s.execution_state] || "Not started"}`;
}
function sgPlatformPreview(s) {
  if (!sgPlatformComplete(s)) return "";
  const sub = s.platform.submission, p = sub.payload, account = sub.account || {};
  const line = (label, value) => value == null || value === "" || Array.isArray(value) && !value.length ? "" : `<div class="sg-platform-field"><strong>${esc(label)}</strong><span>${esc(Array.isArray(value) ? value.join(", ") : String(value))}</span></div>`;
  const message = (subject, body) => `${subject ? `<h3>${esc(subject)}</h3>` : ""}<pre class="sg-message">${esc(body)}</pre>`;
  let content;
  if (sub.tool === "email.batch") {
    content = line("From", account.sender_address) + line("Sending account", account.sender_account) + line("Business BCC", account.bcc_address)
      + `<p>${p.recipients.length} recipients · one human execution task</p>`
      + p.recipients.map(r => `<section class="sg-recipient">${line("To", r.email)}${message(r.subject, r.body)}</section>`).join("");
  } else if (sub.tool === "email.send") {
    content = line("From", p.from_address) + line("To", p.to) + line("CC", p.cc) + line("BCC", p.bcc) + message(p.subject, p.body);
  } else {
    content = line("Account", account.handle) + line("Community", p.subreddits) + line("Target", p.target)
      + line("Read", p.what) + line("Query", p.query) + line("Flair", p.flair) + message(p.title, p.body || p.note || "");
  }
  return `<div class="sg-platform"><p class="sg-notice">${esc(sgPlatformStatus(s))}</p>${sub.why ? `<div class="sg-rationale"><h3>Purpose</h3><p>${esc(sub.why)}</p></div>` : ""}
    ${content}${line("Attachments", p.media)}${line("Scheduled after", sub.not_before)}${line("Window ends", sub.window_end)}
    <p class="sg-notice">Approval authorizes this exact content. Execution is recorded separately.</p></div>`;
}

// Scope-specific review. The server owns preparation, decisions and application.
const sgViews = new Set();
const sgShared = { drafts: new Map(), choices: new Map(), busy: new Map(), receipts: new Map(), refreshed: new Set(), executions: new Map() };
function sgCreateView(options) {
  const labels = { pending: "Pending", approved: "Approved, applying", applied: "Applied", rejected: "Rejected", conflicted: "Conflicted", failed: "Failed", answered: "Answered", cancelled: "Withdrawn" };
  const sg = {
    slug: null, businessId: null, generation: 0, mount: null, session: null,
    responded: false, items: new Map(), order: [], selected: null, selections: new Map(),
    verified: new Map(), detailErrors: new Map(), detailRequest: 0, detailLoading: null,
    counts: null, nextCursor: null, updatedCursor: null, loading: false, loaded: false,
    listRequest: 0, error: null, ...sgShared,
  };
  const active = options.active;
  const workspace = options.workspace || (() => state.biz);
  const prefix = options.prefix || "";
  const capture = () => ({ slug: sg.slug, generation: sg.generation, mount: sg.mount });
  const mounted = c => c.slug === workspace() && c.slug === sg.slug && c.generation === sg.generation && c.mount === sg.mount && c.mount && c.mount.isConnected;
  const current = c => active() && mounted(c);
  const path = (slug, id = "") => `${options.global ? "/root" : "/biz/" + encodeURIComponent(slug)}/signoffs${id ? "/" + encodeURIComponent(id) : ""}`;
  const binding = s => `${s.id}:${s.proposal_revision}:${s.payload_hash}`;
  const busyKey = (slug, s) => `${slug}:${binding(s)}`;
  const owner = () => sg.session && sg.session.principal_kind === "human" && sg.session.is_owner === true && sg.session.principal_id;
  const draftKey = s => owner() && (sg.businessId || options.global) ? `sgdraft:${owner()}:${options.global ? "scope:global" : sg.businessId}:${binding(s)}` : null;
  const isAuth = error => error && ([401, 403].includes(error.httpStatus) || /^(unauthorized|forbidden|authentication_required|owner_required)$/.test(error.code));
  const stamp = value => {
    const date = new Date(value);
    return value && !isNaN(date) ? date.toLocaleString(undefined, { dateStyle: "medium", timeStyle: "short" }) : "";
  };
  const age = value => {
    const minutes = Math.max(0, Math.floor((Date.now() - Date.parse(value)) / 60000));
    return !Number.isFinite(minutes) ? "" : minutes < 1 ? "Just now" : minutes < 60 ? `${minutes}m ago` : minutes < 1440 ? `${Math.floor(minutes / 60)}h ago` : `${Math.floor(minutes / 1440)}d ago`;
  };
  const bizName = id => ((state.bizs || []).find(b => b.id === id || b.business_id === id) || {}).name || id;
  const scopeText = scope => scope && scope.kind === "global" ? `Global${scope.namespace ? " / " + scope.namespace : ""}` :
    (scope && (scope.business_ids || (scope.business_id ? [scope.business_id] : [])) || []).map(bizName).join(", ") || bizName(sg.businessId) || sg.slug || "";
  const resourceText = resource => typeof resource === "string" ? resource : resource && (resource.label || resource.resource_id) || "Resource";
  const errorHtml = (error, action) => `<div class="sg-error" role="alert"><span>${esc(error.message || error)}</span>${action ? `<button type="button" class="sg-command" data-sg="${action}">Retry</button>` : ""}</div>`;
  function html(el, value) {
    if (el && el._sgHtml !== value) { el.innerHTML = value; el._sgHtml = value; }
  }
  function draft(s) {
    const key = draftKey(s);
    if (!key) return "";
    if (!sg.drafts.has(key)) sg.drafts.set(key, "");
    return sg.drafts.get(key);
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
    if (old && old.payload_hash && binding(old) !== binding(item)) {
      sg.verified.delete(item.id);
      sg.detailErrors.set(item.id, new Error("The proposal binding changed. Reload the proposal."));
      return false;
    }
    if (!old) sg.order.push(item.id);
    if (old && old.state !== item.state && sg.counts) {
      sg.counts[old.state] = Math.max(0, (sg.counts[old.state] || 0) - 1);
      sg.counts[item.state] = (sg.counts[item.state] || 0) + 1;
    }
    sg.items.set(item.id, { ...old, ...item });
    return true;
  }
  function previewHtml(s) {
    if (s.category === "platform_action") return sgPlatformPreview(s);
    const preview = s.preview;
    if (s.input) return `<div class="sg-rationale"><p>${esc(s.input.explanation)}</p></div>`;
    if (!preview || !Array.isArray(preview.changes)) return "";
    const resourcesById = new Map();
    for (const resource of [...(preview.resources || []), ...preview.changes.map(change => change && change.resource)].filter(Boolean)) {
      const key = typeof resource === "string" ? resource : resource.resource_id || resource.label;
      if (!resourcesById.has(key)) resourcesById.set(key, resourceText(resource));
    }
    const resources = [...new Set(resourcesById.values())];
    const effects = (preview.effects || []).map(effect => typeof effect === "string" ? effect : effect && effect.description).filter(Boolean);
    return `${s.legacy_review_routing ? '<p class="sg-notice">Created under previous review routing.</p>' : ""}${preview.rationale ? `<div class="sg-rationale"><h3>${s.proposer_agent_id ? "Agent's explanation" : "Proposer's explanation"}</h3><p>${esc(preview.rationale)}</p></div>` : ""}
      <div class="sg-impact"><h3>Affected scope</h3><p>${esc(scopeText(preview.affected_scope || s.affected_scope))}</p>${resources.length ? `<ul>${resources.map(resource => `<li>${esc(resource)}</li>`).join("")}</ul>` : ""}</div>
      ${effects.length ? `<div class="sg-effects"><h3>Effects</h3><ul>${effects.map(effect => `<li>${esc(effect)}</li>`).join("")}</ul></div>` : ""}`;
  }
  function completePreview(s) {
    if (s.input) return !!s.preview && Array.isArray(s.input.choices);
    if (s.category === "platform_action") return sgPlatformComplete(s);
    return s.preview && Array.isArray(s.preview.changes) && s.preview.changes.length > 0 && s.preview.changes.every(change =>
      change && (change.kind === "structured" ? Array.isArray(change.fields) : change.kind === "text" && typeof change.diff === "string" && "before" in change && "after" in change));
  }
  function appliedRefresh(s, slug) {
    const key = busyKey(slug, s);
    if (s.state !== "applied" || sg.refreshed.has(key) || workspace() !== slug) return;
    sg.refreshed.add(key);
    if (options.global) { if (typeof rtRefresh === "function") rtRefresh(); return; }
    if (typeof refreshTasksSoon === "function") refreshTasksSoon();
    // Each affected view owns its normal refresh and editor protections.
    if (typeof acRefresh === "function") acRefresh();
    if (typeof rtRefresh === "function" && tView === "routines") rtRefresh();
    if (typeof refreshSideSoon === "function") refreshSideSoon();
  }
  function detailPaint(row, s) {
    const detail = row.querySelector(".sg-detail");
    detail.hidden = s.id !== sg.selected;
    row.querySelector(".sg-rowhead").setAttribute("aria-expanded", String(!detail.hidden));
    if (detail.hidden) return;
    const error = sg.detailErrors.get(s.id), busy = sg.busy.get(busyKey(sg.slug, s));
    const verified = sg.verified.get(s.id) === s.status_revision;
    const canReview = verified && completePreview(s) && owner() && !sg.error && !error && !s.superseded_by_id;
    if (!detail.querySelector(".sg-preview")) {
      detail.innerHTML = `<div class="sg-detailnotice"></div><div class="sg-attention-meta"></div><div class="sg-preview"></div><div class="sg-choices"></div><div class="sg-savedresponse"></div><div class="sg-feedbackwrap"></div><div class="sg-manual"></div><div class="sg-actionarea"></div><div class="sg-stories" aria-label="Related stories"></div>`;
      if (!options.global) storyRelatedChips(detail.querySelector('.sg-stories'),sg.slug,'signoffs',s.id);
    }
    detail.setAttribute("aria-busy", String(!!busy || sg.detailLoading === s.id));
    const notice = error ? errorHtml(error, "detail") : !verified ? `<p class="sg-notice">${sg.detailLoading === s.id ? "Loading prepared change..." : "This detail is out of date."}</p>` : !completePreview(s) ? errorHtml("The proposal could not be loaded completely. Reload it before approving.", "detail") : "";
    html(detail.querySelector(".sg-detailnotice"), notice);
    const attention = s.attention || {};
    const attentionText = [attention.urgent ? 'Urgent' : '', attention.blocking ? 'Blocking' : '', attention.reason || '',
      attention.deadline ? `Deadline: ${stamp(attention.deadline)}` : '',
      attention.snoozed_until && Date.parse(attention.snoozed_until) > Date.now() ? `Snoozed until ${stamp(attention.snoozed_until)}` : ''].filter(Boolean).join(' · ');
    html(detail.querySelector('.sg-attention-meta'), attentionText ? `<p class="sg-notice">${esc(attentionText)}</p>` : '');
    html(detail.querySelector(".sg-preview"), previewHtml(s));
    const chosen = sg.choices.get(draftKey(s)) || [];
    const submitted = s.decision?.selected_choices || [];
    const choiceMode = s.input?.selection_mode;
    html(detail.querySelector(".sg-choices"), s.input && choiceMode !== "none" ?
      `<div role="${choiceMode === "single" ? "radiogroup" : "group"}" aria-label="Answer choices">${s.input.choices.map((choice,index) => {
        const checked = (s.state === "pending" ? chosen : submitted).includes(choice.id);
        return `<button type="button" class="sg-choice" role="${choiceMode === "single" ? "radio" : "checkbox"}" aria-checked="${checked}" data-choice="${esc(choice.id)}" ${!canReview || busy || s.state !== "pending" ? "disabled" : ""}>
          <span class="sg-choice-marker ${choiceMode === "single" ? "sg-choice-letter" : "sg-choice-ball"}" aria-hidden="true">${choiceMode === "single" ? String.fromCharCode(65+index) : ""}</span>
          <span class="sg-choice-name">${esc(choice.label)}</span><span aria-hidden="true">—</span>
          <span class="sg-choice-explanation" title="${esc(choice.explanation)}">${esc(choice.explanation)}</span></button>`;
      }).join("")}</div>` : "");
    html(detail.querySelector(".sg-savedresponse"), s.decision ? `<p class="sg-response-notes">${esc(s.decision.notes ?? s.decision.feedback ?? "")}</p><p class="sg-meta">Submitted ${esc(stamp(s.decision.at))}</p>` : "");
    const feedback = detail.querySelector(".sg-feedbackwrap");
    if (s.state === "pending" && completePreview(s) && owner() && !s.superseded_by_id) {
      if (!feedback.querySelector("textarea")) {
        feedback.innerHTML = `<label class="sg-label" for="sg-feedback-${prefix}${esc(s.id)}">Notes ${s.input?.notes_required ? "(required)" : "(optional)"}</label><textarea id="sg-feedback-${prefix}${esc(s.id)}" class="sg-feedback" rows="3" spellcheck="true"></textarea>`;
        feedback.querySelector("textarea").value = draft(s);
      }
      feedback.querySelector("textarea").disabled = !!busy || !canReview;
    } else feedback.innerHTML = "";
    const disabled = !canReview || !!busy ? " disabled" : "";
    let controls = "";
    if (s.state === "pending" && owner() && completePreview(s) && !s.superseded_by_id) controls = s.input ? `<div class="sg-actions"><button type="button" class="sg-approve" data-sg="respond"${disabled}>Submit</button></div>` : `<div class="sg-actions"><button type="button" class="sg-reject" data-sg="reject"${disabled}>${SVG_X}Reject</button><button type="button" class="sg-approve" data-sg="approve"${disabled}>${SVG_CHECK}Approve</button></div>`;
    if (busy) controls += `<p class="sg-notice" role="status">${busy.unknown ? "The response was lost. Checking the saved decision..." : "Saving decision..."}</p>${busy.unknown ? `<button type="button" class="sg-command" data-sg="reconcile"${busy.checking ? " disabled" : ""}>Check decision</button>` : ""}`;
    if (verified && !owner()) controls += `<p class="sg-notice">Signoff review requires an owner session.</p>`;
    const manualForm = detail.querySelector(".sg-manual");
    manualForm.hidden = true;
    if (options.execution && s.category === "platform_action" && s.state !== "pending") {
      const execution = s.platform?.execution || {}, route = s.platform?.submission?.route;
      const pending = sg.executions.get(`${sg.slug}:${s.id}:execution`);
      const disabledExecution = !verified || !owner() || !!sg.error || !!error || !!pending;
      const command = (action, label) => `<button type="button" class="sg-command" data-sg="${action}"${disabledExecution ? " disabled" : ""}>${label}</button>`;
      if (!execution.historical && s.state === "applied" && ["queued", "dispatched", "awaiting_human_fulfillment"].includes(s.execution_state) && !execution.cancellation)
        controls += command("cancel-execution", route === "human_api" ? "Stop batch" : "Cancel execution");
      if (!execution.historical && s.state === "applied" && (execution.cancellation?.state === "failed" || execution.error && !["external_failed", "manual_failed"].includes(execution.error.code) && ["failed", "reconciliation_required"].includes(s.execution_state)))
        controls += command("retry-execution", "Retry interrupted work");
      if (route === "human" && s.state === "applied" && s.execution_state === "awaiting_human_fulfillment" && owner()) {
        manualForm.hidden = false;
        const read = s.platform.submission.tool === "reddit.read";
        if (!manualForm.querySelector("textarea")) manualForm.innerHTML = `<label class="sg-label">Outcome<select class="sg-manual-mode"><option value="result">${read ? "Read completed" : "Published"}</option><option value="error">Failed</option></select></label><label class="sg-label">${read ? "Read result or failure reason" : "Published permalink or failure reason"}<textarea class="sg-manual-result sg-feedback" rows="3"></textarea></label><p class="sg-manual-validation sg-error" role="alert"></p>`;
        manualForm.querySelectorAll("textarea, select").forEach(input => input.disabled = disabledExecution);
        controls += command("fulfill-manual", "Record outcome");
      }
      if (execution.result?.body && s.platform?.submission?.tool === "reddit.read") controls += `<h3>Recorded read result</h3><pre class="sg-message">${esc(execution.result.body)}</pre>`;
      if (execution.error?.message) controls += `<p class="sg-notice">${esc(execution.error.message)}</p>`;
      if (execution.historical) controls += '<p class="sg-notice">Imported history. Execution is disabled.</p>';
      if (execution.cancellation) controls += `<p class="sg-notice">Cancellation: ${esc(execution.cancellation.outcome || execution.cancellation.state || "requested")}</p>`;
      if (execution.result?.url && /^https?:\/\//.test(execution.result.url)) controls += `<p><a href="${esc(execution.result.url)}" target="_blank" rel="noopener">View result</a></p>`;
      if (pending) controls += `<p class="sg-notice">${pending.unknown ? "Response lost. Check the saved execution request." : "Saving execution request…"}</p>${pending.unknown ? '<button type="button" class="sg-command" data-sg="check-execution">Check execution</button>' : ""}`;
    }
    html(detail.querySelector(".sg-actionarea"), controls);
  }
  function render() {
    if (!active() || !sg.mount || !sg.mount.isConnected) return;
    const scroll = sg.mount.scrollTop;
    const focused = document.activeElement;
    const oldRow = focused && focused.closest && focused.closest(".sg-row");
    const selectedRow = [...sg.mount.querySelectorAll(".sg-row")].find(row => row.dataset.id === sg.selected);
    const anchorTop = selectedRow && selectedRow.getBoundingClientRect().top;
    const business = (state.bizs || []).find(b => b.slug === sg.slug) || {};
    sg.mount.querySelector(".sg-business").textContent = options.global ? "Global" : business.name || sg.slug || "";
    sg.mount.querySelector(".sg-count").textContent = sg.counts ? `${sg.counts.pending || 0} pending` : "";
    sg.mount.querySelector(".sg").setAttribute("aria-busy", String(sg.loading));
    html(sg.mount.querySelector(".sg-listnotice"), !sg.slug ? `<p class="sg-empty">Pick a business first.</p>` : sg.error ? errorHtml(sg.error, "list") + (sg.items.size ? `<p class="sg-notice">Showing the last update. Review is disabled until refreshed.</p>` : "") : sg.loading && !sg.loaded ? `<p class="sg-empty">Loading signoffs...</p>` : "");
    const attention = typeof sgAttention !== 'undefined' ? sgAttention : {urgent:[]};
    if (!sg.responded && (options.global || sg.businessId)) attention.urgent.filter(item => item.review_business_id === sg.businessId && (!options.category || item.category === options.category)).forEach(merge);
    const urgentLocal = s => s.state === 'pending' && (s.attention?.urgent || s.attention?.blocking);
    if (options.execution) sg.mount.querySelector(".sg-execution").hidden = sg.responded;
    const groups = { urgent: sg.responded ? [] : sg.order.map(id=>sg.items.get(id)).filter(s=>s && urgentLocal(s)), waiting: sg.order.map(id => sg.items.get(id)).filter(s => s && (sg.responded ? !!s.decision : s.state === "pending" && !urgentLocal(s))) };
    html(sg.mount.querySelector('.sg-filter'), `<button type="button" data-sg="pending" aria-pressed="${!sg.responded}">Waiting</button><button type="button" data-sg="responded" aria-pressed="${sg.responded}">Responded</button>`);
    if (options.execution && !sg.responded) groups.execution = sg.order.map(id => sg.items.get(id)).filter(s => s && s.state !== "pending");
    for (const name of Object.keys(groups)) {
      const container = sg.mount.querySelector(`.sg-${name}`);
      const content = container.querySelector(".sg-rows");
      container.hidden = !sg.slug || (!sg.loaded && !sg.items.size);
      html(container.querySelector(".sg-sectionempty"), name === "waiting" && !groups[name].length ? `<p class="sg-empty">${sg.responded ? "No submitted responses." : attention.urgent.length ? "No other signoffs waiting." : "No signoffs waiting."}</p>` : "");
      groups[name].forEach((s, index) => {
        let row = [...sg.mount.querySelectorAll(".sg-row")].find(node => node.dataset.id === s.id);
        if (!row) {
          row = document.createElement("article"); row.className = "sg-row mkcard"; row.dataset.id = s.id;
          row.innerHTML = `<button type="button" class="sg-rowhead" aria-expanded="false" aria-controls="sg-detail-${prefix}${esc(s.id)}"><span class="sg-rowtitle"></span><span class="sg-state"></span><span class="sg-rowmeta"></span></button><div class="sg-detail" id="sg-detail-${prefix}${esc(s.id)}" hidden></div>`;
        }
        if (content.children[index] !== row) content.insertBefore(row, content.children[index] || null);
        row.dataset.state = s.state;
        row.querySelector(".sg-rowtitle").textContent = s.title || s.preview && s.preview.title || s.operation_name;
        row.querySelector(".sg-state").textContent = s.category === "platform_action" ? sgPlatformStatus(s) : labels[s.state];
        const proposer = s.proposer_display_name || s.proposer_principal_id || "Unknown proposer";
        const initiator = s.initiated_by_principal_id ? ` / initiated by ${s.initiated_by_display_name || s.initiated_by_principal_id}` : "";
        html(row.querySelector(".sg-rowmeta"), `<span>${esc(proposer + initiator)}</span><time datetime="${esc(s.created_at)}" title="${esc(stamp(s.created_at))}">${esc(age(s.created_at))}</time><span class="sg-scope${s.affected_scope && s.affected_scope.kind === "global" ? " sg-global" : ""}">${esc(scopeText(s.affected_scope))}</span>`);
        detailPaint(row, s);
      });
      [...content.children].forEach(row => { if (!groups[name].some(s => s.id === row.dataset.id)) row.remove(); });
    }
    const foreign = attention.urgent.filter(item=>item.review_business_id !== sg.businessId);
    const urgentSection = sg.mount.querySelector('.sg-urgent');
    urgentSection.hidden = sg.responded || (!groups.urgent.length && !foreign.length);
    html(urgentSection.querySelector('.sg-foreign'), foreign.map(item=>`<button type="button" class="sg-urgent-link" data-attention-id="${esc(item.id)}"><small>${esc(item.business_name)}</small><span>${esc(item.title)}</span></button>`).join(''));
    const more = sg.mount.querySelector(".sg-more");
    more.hidden = !sg.nextCursor; more.disabled = sg.loading;
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
      const data = coreData(await api(path(c.slug, id)));
      if (!current(c) || request !== sg.detailRequest || sg.selected !== id) return;
      const s = data.signoff;
      if (!s || s.id !== id || (options.global ? s.review_scope?.kind !== "global" || s.review_business_id !== null : s.review_business_id !== sg.businessId)) throw new Error("The proposal did not match this workspace.");
      if (merge(s)) {
        sg.verified.set(id, s.status_revision);
        const receipt = sg.busy.get(busyKey(c.slug, s));
        if (receipt && s.state !== "pending") finishReceipt(receipt, s);
        appliedRefresh(s, c.slug);
      }
    } catch (error) {
      if (current(c) && request === sg.detailRequest && sg.selected === id) {
        sg.detailErrors.set(id, error); sg.verified.delete(id);
        if (isAuth(error)) sg.session = null;
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
    const cursor = more ? sg.nextCursor : sg.updatedCursor;
    const query = new URLSearchParams({ limit: "50" });
    if (sg.responded) query.set("responded", "true");
    if (options.category) query.set("category", options.category);
    if (cursor) query.set(more ? "cursor" : "updated_after", cursor);
    sg.loading = true; render();
    try {
      const responses = await Promise.all([api(path(c.slug) + "?" + query), sg.session ? Promise.resolve(null) : api("/session")]);
      if (!current(c) || request !== sg.listRequest) return;
      if (responses[1]) sg.session = coreData(responses[1]);
      const data = coreData(responses[0]);
      if (!Array.isArray(data.items) || (options.global ? data.review_scope?.kind !== "global" || data.review_business_id !== null : !data.review_business_id || sg.businessId && data.review_business_id !== sg.businessId)) throw new Error("The signoff list did not match this workspace.");
      sg.businessId = data.review_business_id;
      data.items.forEach(merge);
      if (!cursor || more) sg.nextCursor = data.next_cursor || null;
      if (!more) sg.updatedCursor = data.updated_cursor || sg.updatedCursor;
      sg.counts = data.counts; sg.error = null; sg.loaded = true;
      if (options.onItems) options.onItems([...sg.items.values()]);
      if (!sg.selected) {
        const previous = sg.selections.get(sg.slug);
        if (previous && sg.items.has(previous)) sg.selected = previous;
      }
      render();
      if (sg.selected) await loadDetail(sg.selected, true);
      // Catch-up is bounded per request. The next active-view refresh resumes the cursor.
    } catch (error) {
      if (current(c) && request === sg.listRequest) { sg.error = error; if (isAuth(error)) sg.session = null; }
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
      if (s && s.id === receipt.id && s.state !== "pending") {
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
        }
      } else {
        receipt.unknown = true;
        if (autoReconcile) await reconcile(receipt);
      }
    } finally { render(); }
  }
  function decide(action) {
    const s = sg.items.get(sg.selected);
    if (!active() || !s || !owner() || !completePreview(s) || sg.error || sg.detailErrors.has(s.id) || sg.verified.get(s.id) !== s.status_revision || s.superseded_by_id) return;
    if (!["approve", "reject", "respond"].includes(action) || s.state !== "pending") return;
    const key = busyKey(sg.slug, s);
    if (sg.busy.has(key)) return;
    const body = { proposal_revision: s.proposal_revision, payload_hash: s.payload_hash, expected_status_revision: s.status_revision, idempotency_key: newRequestId() };
    if (action === "respond") {
      body.notes = draft(s);
      body.selected_choices = sg.choices.get(draftKey(s)) || [];
      if (s.input.selection_required && !body.selected_choices.length || s.input.notes_required && !body.notes.trim()) {
        const notice = sg.mount.querySelector('.sg-detail:not([hidden]) .sg-detailnotice');
        html(notice, errorHtml('Complete the required answer before submitting.')); return;
      }
    } else body.feedback = draft(s) || null;
    const receipt = { key, slug: sg.slug, businessId: sg.businessId, id: s.id, binding: binding(s), draftKey: draftKey(s), action, body, unknown: false };
    sg.busy.set(key, receipt);

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
        const manual = s.platform?.submission?.route === "human" && s.execution_state === "awaiting_human_fulfillment";
        if (!window.confirm(manual ? "Confirm this manual action has not started?" : "Request cancellation? Work that has already started may still need reconciliation.")) return;
        if (manual) body.confirm_not_started = true;
      }
      if (action === "fulfill-manual") {
        const form = [...sg.mount.querySelectorAll(".sg-row")].find(row => row.dataset.id === s.id)?.querySelector(".sg-manual");
        if (!form || form.hidden) return;
        const value = form.querySelector("textarea").value;
        const failure = form.querySelector("select").value === "error", read = s.platform.submission.tool === "reddit.read";
        const error = !value.trim() ? "Enter the actual result or failure reason." : !failure && !read && !/^https?:\/\//.test(value.trim()) ? "Enter an HTTP(S) permalink." : "";
        form.querySelector(".sg-manual-validation").textContent = error;
        if (error) return;
        body.outcome = failure ? {error: value} : read ? {body: value} : {url: value.trim()};
      }
      receipt = { slug: sg.slug, id: s.id, operation: action === "fulfill-manual" ? "signoffs_platform_fulfill" : action === "cancel-execution" ? "signoffs_platform_cancel" : "signoffs_platform_retry", body };
      sg.executions.set(key, receipt);
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
    body.classList.add("sg-body");
    body.innerHTML = `<div class="sg"><header class="sg-head"><div><p class="sg-business"></p></div><span class="sg-count"></span></header><div class="sg-filter"></div><div class="sg-listnotice"></div><section class="sg-urgent"><h2>Urgent / blocking · All businesses</h2><div class="sg-sectionempty"></div><div class="sg-rows mkcards"></div><div class="sg-foreign"></div></section><section class="sg-waiting"><div class="sg-sectionempty"></div><div class="sg-rows mkcards"></div></section>${options.execution ? '<section class="sg-execution"><h2>Execution and history</h2><div class="sg-sectionempty"></div><div class="sg-rows mkcards"></div></section>' : ""}<button type="button" class="sg-command sg-more" data-sg="more" hidden>Load more</button></div>`;
    body.onclick = event => {
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
        const action = command.dataset.sg;
        if (action === "pending" || action === "responded") {
          sg.responded = action === "responded"; sg.items.clear(); sg.verified.clear(); sg.detailErrors.clear(); sg.order=[]; sg.selected=null;
          sg.selections.delete(sg.slug); sg.nextCursor=null; sg.updatedCursor=null;
          sg.listRequest++; sg.detailRequest++; sg.loading=false; sg.loaded=false; refresh();
        }
        else if (action === "list") { sg.updatedCursor = null; refresh(); }
        else if (action === "more") refresh(true);
        else if (action === "detail") loadDetail(sg.selected, true);
        else if (action === "reconcile") { const s = sg.items.get(sg.selected); const receipt = s && sg.busy.get(busyKey(sg.slug, s)); if (receipt) reconcile(receipt); }
        else if (["cancel-execution", "retry-execution", "check-execution", "fulfill-manual"].includes(action)) executionCommand(action);
        else decide(action);
        return;
      }
      const row = event.target.closest(".sg-row"); if (row) select(row.dataset.id);
    };
    body.onkeydown = event => {
      const choice = event.target.closest('[role="radio"]');
      if (choice && ['ArrowDown','ArrowRight','ArrowUp','ArrowLeft'].includes(event.key)) {
        event.preventDefault();
        const choices = [...choice.parentElement.querySelectorAll('[role="radio"]:not(:disabled)')];
        const delta = ['ArrowDown','ArrowRight'].includes(event.key) ? 1 : -1;
        choices[(choices.indexOf(choice)+delta+choices.length)%choices.length]?.click();
      }
    };
    body.oninput = event => { if (event.target.matches(".sg-feedback:not(.sg-manual-result)")) { const s = sg.items.get(sg.selected); if (s) saveDraft(draftKey(s), event.target.value); } };
    render(); refresh();
  }
  function businessChanged() {
    if (sg.slug === workspace()) return;
    sg.slug = workspace(); sg.businessId = null; sg.generation++;
    sg.items.clear(); sg.order = []; sg.selected = null; sg.reveal=null;
    sg.verified.clear(); sg.detailErrors.clear(); sg.detailRequest++;
    sg.counts = null; sg.nextCursor = null; sg.updatedCursor = null; sg.error = null; sg.loaded = false; sg.loading = false;
    mount();
  };
  const view = {
    open(id) { sg.responded=false; sg.reveal=id; sg.selected=id; sg.selections.set(workspace(),id); if (active()) { refresh(); loadDetail(id,true); } },
    businessChanged,
    render,
    paint() { if (sg.slug !== workspace()) businessChanged(); else mount(); },
    refresh: () => refresh(),
    syncDraft(key, value) {
      const selected = sg.items.get(sg.selected);
      const row = sg.mount && [...sg.mount.querySelectorAll(".sg-row")].find(row => row.dataset.id === sg.selected);
      const textarea = row && row.querySelector(".sg-feedback");
      if (selected && draftKey(selected) === key && textarea && textarea.value !== value) textarea.value = value;
    },
    suspend() { sg.generation++; sg.mount = null; sg.loading = false; sg.detailLoading = null; },
    livePaint(slug, live) {
      if (slug !== workspace() || slug !== sg.slug || active() || !live || !Number.isInteger(live.signoffs_pending)) return;
      sg.counts = { ...(sg.counts || {}), pending: live.signoffs_pending };
    },
  };
  document.addEventListener('click', event => {
    if (!active() || !sg.selected || !sg.mount) return;
    if (event.composedPath().some(node => node.dataset?.action === 'open' || node.dataset?.attentionId)) return;
    const row = event.composedPath().find(node => node.classList?.contains('sg-row'));
    if (row && sg.mount.contains(row)) return;
    sg.selected=null; sg.selections.delete(sg.slug); sg.detailRequest++; render();
  });
  sgViews.add(view);
  return view;
}
const sgDrawer = sgCreateView({
  active: () => state.scope !== "global" && typeof tView !== "undefined" && tView === "signoffs" && tPanelOpen(),
  body: () => $("#tbody"),
});
window.sgBusinessChanged = () => sgDrawer.businessChanged();
const sgGlobal = sgCreateView({
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
window.sgRefresh = () => Promise.all([...sgViews].map(view => view.refresh()));
window.sgLivePaint = (slug, live) => sgViews.forEach(view => view.livePaint(slug, live));

window.sgOpenItem = id => (state.scope === 'global' ? sgGlobal : sgDrawer).open(id);

window.sgAttentionViewsPaint = () => sgViews.forEach(view => view.render());
