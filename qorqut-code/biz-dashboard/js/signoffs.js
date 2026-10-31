// Selected-business review. The server owns preparation, decisions and application.
const sgViews = new Set();
const sgShared = { drafts: new Map(), busy: new Map(), receipts: new Map(), refreshed: new Set() };
function sgCreateView(options) {
  const labels = { pending: "Pending", approved: "Approved, applying", applied: "Applied", rejected: "Rejected", conflicted: "Conflicted", failed: "Failed" };
  const sg = {
    slug: null, businessId: null, generation: 0, mount: null, session: null,
    items: new Map(), order: [], selected: null, selections: new Map(),
    verified: new Map(), detailErrors: new Map(), detailRequest: 0, detailLoading: null,
    counts: null, nextCursor: null, updatedCursor: null, loading: false, loaded: false,
    listRequest: 0, error: null, ...sgShared,
  };
  const active = options.active;
  const prefix = options.prefix || "";
  const capture = () => ({ slug: sg.slug, generation: sg.generation, mount: sg.mount });
  const mounted = c => c.slug === state.biz && c.slug === sg.slug && c.generation === sg.generation && c.mount === sg.mount && c.mount && c.mount.isConnected;
  const current = c => active() && mounted(c);
  const path = (slug, id = "") => `/biz/${encodeURIComponent(slug)}/signoffs${id ? "/" + encodeURIComponent(id) : ""}`;
  const binding = s => `${s.id}:${s.proposal_revision}:${s.payload_hash}`;
  const busyKey = (slug, s) => `${slug}:${binding(s)}`;
  const owner = () => sg.session && sg.session.principal_kind === "human" && sg.session.is_owner === true && sg.session.principal_id;
  const draftKey = s => owner() && sg.businessId ? `sgdraft:${owner()}:${sg.businessId}:${binding(s)}` : null;
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
  const scopeText = scope => scope && scope.kind === "global" ? `All businesses${scope.namespace ? " / " + scope.namespace : ""}` :
    (scope && (scope.business_ids || (scope.business_id ? [scope.business_id] : [])) || []).map(bizName).join(", ") || bizName(sg.businessId) || sg.slug || "";
  const resourceText = resource => typeof resource === "string" ? resource : resource && (resource.label || resource.resource_id) || "Resource";
  const errorHtml = (error, action) => `<div class="sg-error" role="alert"><span>${esc(error.message || error)}</span>${action ? `<button type="button" class="sg-command" data-sg="${action}">Retry</button>` : ""}</div>`;
  function html(el, value) {
    if (el && el._sgHtml !== value) { el.innerHTML = value; el._sgHtml = value; }
  }
  function draft(s) {
    const key = draftKey(s);
    if (!key) return "";
    if (!sg.drafts.has(key)) sg.drafts.set(key, ls(key) || "");
    return sg.drafts.get(key);
  }
  function saveDraft(key, value) {
    if (!key) return;
    sg.drafts.set(key, value);
    sgViews.forEach(view => view.syncDraft(key, value));
    if (value) ls(key, value);
    else { try { localStorage.removeItem(key); } catch (_) {} }
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
    const preview = s.preview;
    if (!preview || !Array.isArray(preview.changes)) return "";
    const resourcesById = new Map();
    for (const resource of [...(preview.resources || []), ...preview.changes.map(change => change && change.resource)].filter(Boolean)) {
      const key = typeof resource === "string" ? resource : resource.resource_id || resource.label;
      if (!resourcesById.has(key)) resourcesById.set(key, resourceText(resource));
    }
    const resources = [...new Set(resourcesById.values())];
    const effects = (preview.effects || []).map(effect => typeof effect === "string" ? effect : effect && effect.description).filter(Boolean);
    return `${preview.rationale ? `<div class="sg-rationale"><h3>${s.proposer_agent_id ? "Agent's explanation" : "Proposer's explanation"}</h3><p>${esc(preview.rationale)}</p></div>` : ""}
      <div class="sg-impact"><h3>Affected scope</h3><p>${esc(scopeText(preview.affected_scope || s.affected_scope))}</p>${resources.length ? `<ul>${resources.map(resource => `<li>${esc(resource)}</li>`).join("")}</ul>` : ""}</div>
      ${effects.length ? `<div class="sg-effects"><h3>Effects</h3><ul>${effects.map(effect => `<li>${esc(effect)}</li>`).join("")}</ul></div>` : ""}`;
  }
  function completePreview(s) {
    return s.preview && Array.isArray(s.preview.changes) && s.preview.changes.length > 0 && s.preview.changes.every(change =>
      change && (change.kind === "structured" ? Array.isArray(change.fields) : change.kind === "text" && typeof change.diff === "string" && "before" in change && "after" in change));
  }
  function appliedRefresh(s, slug) {
    const key = busyKey(slug, s);
    if (s.state !== "applied" || sg.refreshed.has(key) || state.biz !== slug) return;
    sg.refreshed.add(key);
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
    if (!detail.querySelector(".sg-preview")) detail.innerHTML = `<div class="sg-detailnotice"></div><div class="sg-preview"></div><div class="sg-feedbackwrap"></div><div class="sg-actionarea"></div>`;
    detail.setAttribute("aria-busy", String(!!busy || sg.detailLoading === s.id));
    const notice = error ? errorHtml(error, "detail") : !verified ? `<p class="sg-notice">${sg.detailLoading === s.id ? "Loading prepared change..." : "This detail is out of date."}</p>` : !completePreview(s) ? errorHtml("The proposal could not be loaded completely. Reload it before approving.", "detail") : "";
    html(detail.querySelector(".sg-detailnotice"), notice);
    html(detail.querySelector(".sg-preview"), previewHtml(s));
    const feedback = detail.querySelector(".sg-feedbackwrap");
    if (s.state === "pending" && completePreview(s) && owner() && !s.superseded_by_id) {
      if (!feedback.querySelector("textarea")) {
        feedback.innerHTML = `<label class="sg-label" for="sg-feedback-${prefix}${esc(s.id)}">Feedback for proposer (optional)</label><textarea id="sg-feedback-${prefix}${esc(s.id)}" class="sg-feedback" rows="3" spellcheck="true"></textarea>`;
        feedback.querySelector("textarea").value = draft(s);
      }
      feedback.querySelector("textarea").disabled = !!busy || !canReview;
    } else feedback.innerHTML = "";
    const disabled = !canReview || !!busy ? " disabled" : "";
    let controls = "";
    if (s.state === "pending" && owner() && completePreview(s) && !s.superseded_by_id) controls = `<div class="sg-actions"><button type="button" class="sg-reject" data-sg="reject"${disabled}>${SVG_X}Reject</button><button type="button" class="sg-approve" data-sg="approve"${disabled}>${SVG_CHECK}Approve</button></div>`;
    if (busy) controls += `<p class="sg-notice" role="status">${busy.unknown ? "The response was lost. Checking the saved decision..." : "Saving decision..."}</p>${busy.unknown ? `<button type="button" class="sg-command" data-sg="reconcile"${busy.checking ? " disabled" : ""}>Check decision</button>` : ""}`;
    if (verified && !owner()) controls += `<p class="sg-notice">Signoff review requires an owner session.</p>`;
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
    sg.mount.querySelector(".sg-business").textContent = business.name || sg.slug || "";
    sg.mount.querySelector(".sg-count").textContent = sg.counts ? `${sg.counts.pending || 0} pending` : "";
    sg.mount.querySelector(".sg").setAttribute("aria-busy", String(sg.loading));
    html(sg.mount.querySelector(".sg-listnotice"), !sg.slug ? `<p class="sg-empty">Pick a business first.</p>` : sg.error ? errorHtml(sg.error, "list") + (sg.items.size ? `<p class="sg-notice">Showing the last update. Review is disabled until refreshed.</p>` : "") : sg.loading && !sg.loaded ? `<p class="sg-empty">Loading signoffs...</p>` : "");
    const groups = { waiting: sg.order.map(id => sg.items.get(id)).filter(s => s && s.state === "pending") };
    for (const name of ["waiting"]) {
      const container = sg.mount.querySelector(`.sg-${name}`);
      const content = container.querySelector(".sg-rows");
      container.hidden = !sg.slug || (!sg.loaded && !sg.items.size);
      html(container.querySelector(".sg-sectionempty"), name === "waiting" && !groups[name].length ? `<p class="sg-empty">No signoffs waiting.</p>` : "");
      groups[name].forEach((s, index) => {
        let row = [...sg.mount.querySelectorAll(".sg-row")].find(node => node.dataset.id === s.id);
        if (!row) {
          row = document.createElement("article"); row.className = "sg-row mkcard"; row.dataset.id = s.id;
          row.innerHTML = `<button type="button" class="sg-rowhead" aria-expanded="false" aria-controls="sg-detail-${prefix}${esc(s.id)}"><span class="sg-rowtitle"></span><span class="sg-state"></span><span class="sg-rowmeta"></span></button><div class="sg-detail" id="sg-detail-${prefix}${esc(s.id)}" hidden></div>`;
        }
        if (content.children[index] !== row) content.insertBefore(row, content.children[index] || null);
        row.dataset.state = s.state;
        row.querySelector(".sg-rowtitle").textContent = s.title || s.preview && s.preview.title || s.operation_name;
        row.querySelector(".sg-state").textContent = labels[s.state];
        const proposer = s.proposer_display_name || s.proposer_principal_id || "Unknown proposer";
        const initiator = s.initiated_by_principal_id ? ` / initiated by ${s.initiated_by_display_name || s.initiated_by_principal_id}` : "";
        html(row.querySelector(".sg-rowmeta"), `<span>${esc(proposer + initiator)}</span><time datetime="${esc(s.created_at)}" title="${esc(stamp(s.created_at))}">${esc(age(s.created_at))}</time><span class="sg-scope${s.affected_scope && s.affected_scope.kind === "global" ? " sg-global" : ""}">${esc(scopeText(s.affected_scope))}</span>`);
        detailPaint(row, s);
      });
      [...content.children].forEach(row => { if (!groups[name].some(s => s.id === row.dataset.id)) row.remove(); });
    }
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
    if (!force && old && sg.verified.get(id) === old.status_revision) { render(); return; }
    sg.detailLoading = id; sg.detailErrors.delete(id); render();
    try {
      const data = coreData(await api(path(c.slug, id)));
      if (!current(c) || request !== sg.detailRequest || sg.selected !== id) return;
      const s = data.signoff;
      if (!s || s.id !== id || s.review_business_id !== sg.businessId) throw new Error("The proposal did not match this business.");
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
      if (mounted(c) && request === sg.detailRequest) { sg.detailLoading = null; render(); }
    }
  }
  async function refresh(more = false) {
    if (!active() || !sg.slug || sg.loading) return;
    const c = capture(), request = ++sg.listRequest;
    const cursor = more ? sg.nextCursor : sg.updatedCursor;
    const query = new URLSearchParams({ limit: "50" });
    if (cursor) query.set(more ? "cursor" : "updated_after", cursor);
    sg.loading = true; render();
    try {
      const responses = await Promise.all([api(path(c.slug) + "?" + query), sg.session ? Promise.resolve(null) : api("/session")]);
      if (!current(c) || request !== sg.listRequest) return;
      if (responses[1]) sg.session = coreData(responses[1]);
      const data = coreData(responses[0]);
      if (!Array.isArray(data.items) || !data.review_business_id || sg.businessId && data.review_business_id !== sg.businessId) throw new Error("The signoff list did not match this business.");
      sg.businessId = data.review_business_id;
      data.items.forEach(merge);
      if (!cursor || more) sg.nextCursor = data.next_cursor || null;
      if (!more) sg.updatedCursor = data.updated_cursor || sg.updatedCursor;
      sg.counts = data.counts; sg.error = null; sg.loaded = true;
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
    if (receipt.action === "approve" || s.state === "rejected") saveDraft(receipt.draftKey, "");
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
        if (state.biz === receipt.slug && sg.slug === receipt.slug) {
          merge(s); sg.verified.set(s.id, s.status_revision); sg.detailErrors.delete(s.id); appliedRefresh(s, receipt.slug);
        }
      } else {
        // Replaying this exact body/key is safe even when the original write still owns its claim.
        await submit(receipt, false);
      }
    } catch (error) {
      if (state.biz === receipt.slug && sg.selected === receipt.id) sg.detailErrors.set(receipt.id, error);
    } finally { receipt.checking = false; render(); }
  }
  async function submit(receipt, autoReconcile = true) {
    try {
      const data = coreData(await post(path(receipt.slug, receipt.id) + "/" + receipt.action, receipt.body));
      const s = data.signoff;
      if (!s || s.id !== receipt.id || binding(s) !== receipt.binding || s.review_business_id !== receipt.businessId) throw new Error("The decision response could not be verified.");
      finishReceipt(receipt, s);
      if (state.biz === receipt.slug && sg.slug === receipt.slug) {
        merge(s); sg.verified.set(s.id, s.status_revision); sg.detailErrors.delete(s.id); appliedRefresh(s, receipt.slug);
      }
    } catch (error) {
      const s = error.details && error.details.signoff;
      if (s && s.id === receipt.id && s.state !== "pending") {
        finishReceipt(receipt, s);
        if (state.biz === receipt.slug && sg.slug === receipt.slug) {
          merge(s); sg.verified.delete(s.id); sg.detailErrors.set(s.id, error);
          if (active() && sg.selected === s.id) loadDetail(s.id, true);
        }
      } else if (error.code && error.code !== "invalid_response") {
        sg.busy.delete(receipt.key);
        if (state.biz === receipt.slug && sg.slug === receipt.slug) {
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
    if (!["approve", "reject"].includes(action) || s.state !== "pending") return;
    const key = busyKey(sg.slug, s);
    if (sg.busy.has(key)) return;
    const body = { proposal_revision: s.proposal_revision, payload_hash: s.payload_hash, expected_status_revision: s.status_revision, idempotency_key: newRequestId() };
    if (action === "reject") body.feedback = draft(s) || null;
    const receipt = { key, slug: sg.slug, businessId: sg.businessId, id: s.id, binding: binding(s), draftKey: draftKey(s), action, body, unknown: false };
    sg.busy.set(key, receipt);
    if (action === "approve") {
      saveDraft(receipt.draftKey, "");
    }
    render(); submit(receipt);
  }
  function mount() {
    const body = options.body();
    sg.generation++; sg.loading = false; sg.detailLoading = null;
    sg.mount = active() ? body : null;
    if (!sg.mount) return;
    body.classList.add("sg-body");
    body.innerHTML = `<div class="sg"><header class="sg-head"><div><h1>Signoffs</h1><p class="sg-business"></p></div><span class="sg-count"></span></header><div class="sg-listnotice"></div><section class="sg-waiting"><h2>Waiting on you</h2><div class="sg-sectionempty"></div><div class="sg-rows mkcards"></div></section><button type="button" class="sg-command sg-more" data-sg="more" hidden>Load more</button></div>`;
    body.onclick = event => {
      const command = event.target.closest("[data-sg]");
      if (command && !command.disabled) {
        const action = command.dataset.sg;
        if (action === "list") { sg.updatedCursor = null; refresh(); }
        else if (action === "more") refresh(true);
        else if (action === "detail") loadDetail(sg.selected, true);
        else if (action === "reconcile") { const s = sg.items.get(sg.selected); const receipt = s && sg.busy.get(busyKey(sg.slug, s)); if (receipt) reconcile(receipt); }
        else decide(action);
        return;
      }
      const head = event.target.closest(".sg-rowhead"); if (head) select(head.closest(".sg-row").dataset.id);
    };
    body.removeEventListener("focusin", focusRow);
    body.addEventListener("focusin", focusRow);
    body.oninput = event => { if (event.target.matches(".sg-feedback")) { const s = sg.items.get(sg.selected); if (s) saveDraft(draftKey(s), event.target.value); } };
    render(); refresh();
  }
  function businessChanged() {
    if (sg.slug === state.biz) return;
    sg.slug = state.biz; sg.businessId = null; sg.generation++;
    sg.items.clear(); sg.order = []; sg.selected = null;
    sg.verified.clear(); sg.detailErrors.clear(); sg.detailRequest++;
    sg.counts = null; sg.nextCursor = null; sg.updatedCursor = null; sg.error = null; sg.loaded = false; sg.loading = false;
    mount();
  };
  const view = {
    businessChanged,
    paint() { if (sg.slug !== state.biz) businessChanged(); else mount(); },
    refresh: () => refresh(),
    syncDraft(key, value) {
      const selected = sg.items.get(sg.selected);
      const row = sg.mount && [...sg.mount.querySelectorAll(".sg-row")].find(row => row.dataset.id === sg.selected);
      const textarea = row && row.querySelector(".sg-feedback");
      if (selected && draftKey(selected) === key && textarea && textarea.value !== value) textarea.value = value;
    },
    suspend() { sg.generation++; sg.mount = null; sg.loading = false; sg.detailLoading = null; },
    livePaint(slug, live) {
      if (slug !== state.biz || slug !== sg.slug || active() || !live || !Number.isInteger(live.signoffs_pending)) return;
      sg.counts = { ...(sg.counts || {}), pending: live.signoffs_pending };
    },
  };
  sgViews.add(view);
  return view;
}
const sgDrawer = sgCreateView({
  active: () => typeof tView !== "undefined" && tView === "signoffs" && tPanelOpen(),
  body: () => $("#tbody"),
});
window.sgBusinessChanged = () => sgDrawer.businessChanged();
window.tPaintSignoffs = () => sgDrawer.paint();
window.sgRefresh = () => Promise.all([...sgViews].map(view => view.refresh()));
window.sgLivePaint = (slug, live) => sgViews.forEach(view => view.livePaint(slug, live));
