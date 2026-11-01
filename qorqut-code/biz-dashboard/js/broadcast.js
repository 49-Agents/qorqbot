/* The dispatcher owns delivery. This page only queues and manages broadcasts. */
let broadcastGeneration = 0;
const broadcastPath = (c) =>
  c.scope === "global" ? "/root" : `/biz/${encodeURIComponent(c.biz)}`;
const broadcastDraftKey = (c) =>
  `broadcast:draft:${c.scope}:${c.biz || "global"}`;
const broadcastData = (value) => {
  try {
    return typeof value === "string" ? JSON.parse(value) : value || {};
  } catch (_) {
    return {};
  }
};
const broadcastLocalDate = (value) => {
  const d = new Date(value);
  if (!value || Number.isNaN(d.getTime())) return "";
  return new Date(d - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);
};
function broadcastButton() { return `<button type="button" class="quiet agent-broadcast" data-broadcast-open aria-label="Broadcast">${aicon('megaphone','currentColor',16)}</button>`; }
function wireBroadcastButton(c, context) {
  c.querySelector('[data-broadcast-open]').onclick=()=>modal({title:'Broadcast',width:820,body:'<div class="broadcast-modal"></div>',onOpen:el=>renderBroadcast(el.querySelector('.broadcast-modal'),context)});
}
function readBroadcastDraft(context) {
  const blank = {
    message: "",
    delivery: "inbox",
    timing: "now",
    at: "",
    every: 1,
    unit: "minutes",
    cron: "",
    timezone: "UTC",
    max: "",
    filter: "all",
    selected: null,
  };
  try {
    const saved = {
      ...blank,
      ...(JSON.parse(ls(broadcastDraftKey(context)) || "null") || {}),
    };
    // Drafts saved under a retired role filter (or the old one-agent model) fall back to All.
    if (!["all", "business-agent", "dev"].includes(saved.filter)) saved.filter = "all";
    return saved;
  } catch (_) {
    return blank;
  }
}
function saveBroadcastDraft(context, draft) {
  ls(broadcastDraftKey(context), JSON.stringify(draft));
}
function broadcastStatus(item) {
  return item.status || "active";
}
function broadcastTimingLabel(item) {
  const t = broadcastData(item.timing_json || item.timing);
  if (t.kind === "once")
    return item.next_due_at
      ? `Once · ${broadcastLocalDate(item.next_due_at)}`
      : "Finished";
  if (t.kind === "interval") {
    const s = Number(t.every_seconds);
    return `Every ${s % 86400 === 0 ? s / 86400 + " days" : s % 3600 === 0 ? s / 3600 + " hours" : s / 60 + " minutes"}`;
  }
  return t.kind === "cron"
    ? `Cron · ${t.schedule} · ${t.timezone}`
    : "Scheduled";
}
function broadcastDefinition(draft, selected, sendNow) {
  const message = draft.message.trim();
  if (!message) throw Error("Enter a message.");
  if (!selected.size) throw Error("Choose at least one recipient.");
  let timing;
  // The server replaces this with its current time for send_now. Keep the
  // request stable so an uncertain response can be retried idempotently.
  if (sendNow) timing = { kind: "once", at: "1970-01-01T00:00:00Z" };
  else if (draft.timing === "once") {
    if (!draft.at || Number.isNaN(new Date(draft.at).getTime()))
      throw Error("Choose a delivery date and time.");
    timing = { kind: "once", at: new Date(draft.at).toISOString() };
  } else if (draft.timing === "interval") {
    const every = Number(draft.every);
    if (!Number.isInteger(every) || every < 1)
      throw Error("Repeat interval must be at least one.");
    timing = { kind: "interval", every, unit: draft.unit };
    if (draft.at) {
      if (Number.isNaN(new Date(draft.at).getTime()))
        throw Error("Choose a valid start date and time.");
      timing.at = new Date(draft.at).toISOString();
    }
  } else {
    if (!draft.cron.trim() || !draft.timezone.trim())
      throw Error("Enter a cron schedule and timezone.");
    timing = {
      kind: "cron",
      schedule: draft.cron.trim(),
      timezone: draft.timezone.trim(),
    };
  }
  const result = {
    name: message.slice(0, 120),
    delivery: draft.delivery,
    message,
    recipients: [...selected],
    timing,
  };
  if (!sendNow && timing.kind !== "once" && draft.max !== "") {
    const max = Number(draft.max);
    if (!Number.isInteger(max) || max < 1)
      throw Error("Maximum occurrences must be a positive whole number.");
    result.max_occurrences = max;
  }
  return result;
}
async function renderBroadcast(c, context) {
  const generation = ++broadcastGeneration,
    path = broadcastPath(context),
    current = () =>
      generation === broadcastGeneration &&
      c.isConnected &&
      (context.scope === "global"
        ? state.scope === "global" && state.globalTab === "agents"
        : state.biz === context.biz && state.tab === "agents");
  c.innerHTML = `<section class="broadcast" aria-label="Broadcast"><div class="broadcast-tabs" role="tablist"><button class="btn on" role="tab" aria-selected="true" data-broadcast-view="compose">Compose</button><button class="btn" role="tab" aria-selected="false" data-broadcast-view="scheduled">Scheduled</button></div><div id="broadcast-body"><p class="muted" role="status">Loading recipients…</p></div></section>`;
  const body = $("#broadcast-body", c);
  let recipients;
  try {
    recipients = coreData(
      await post(`${path}/operations/broadcast_recipients`, {
        scope: context.scope,
        all_businesses: context.scope === "global",
      }),
    ).items;
    if (!Array.isArray(recipients))
      throw Error("Recipient list is unavailable.");
  } catch (error) {
    if (!current()) return;
    body.innerHTML = `<p role="alert">${esc(error.message || "Could not load recipients.")} <button class="btn" id="broadcast-retry">Retry</button></p>`;
    $("#broadcast-retry", body).onclick = () => renderBroadcast(c, context);
    return;
  }
  if (!current()) return;
  const draft = readBroadcastDraft(context),
    selected = new Set(
      Array.isArray(draft.selected)
        ? draft.selected
        : recipients
            .filter(
              (r) => context.scope === "business" || r.business_id != null,
            )
            .map((r) => r.agent_id),
    );
  let editing = null,
    submitting = false,
    pendingId = null,
    pendingSignature = null,
    view = "compose";
  const save = () => {
    draft.selected = [...selected];
    saveBroadcastDraft(context, draft);
  };
  const capture = () => {
    for (const [id, key] of [
      ["broadcast-message", "message"],
      ["broadcast-max", "max"],
      ["broadcast-at", "at"],
      ["broadcast-every", "every"],
      ["broadcast-unit", "unit"],
      ["broadcast-cron", "cron"],
      ["broadcast-timezone", "timezone"],
    ]) {
      const el = $("#" + id, body);
      if (el) draft[key] = el.value;
    }
    save();
  };
  const businessRows = () =>
    recipients.filter(
      (r) =>
        r.business_id != null &&
        (draft.filter === "all" || r.role_id === draft.filter),
    );
  const groups = () =>
    Object.values(
      businessRows().reduce((o, r) => {
        const k = r.business_id || r.business_slug;
        (o[k] ||= {
          label: r.business_name || r.business_slug || "Business",
          rows: [],
        }).rows.push(r);
        return o;
      }, {}),
    );
  const agentTile = (r) => {
    const name = r.display_name || r.name;
    return `<label class="broadcast-recipient ${selected.has(r.agent_id) ? "selected" : ""}"><input type="checkbox" data-recipient="${esc(r.agent_id)}" ${selected.has(r.agent_id) ? "checked" : ""}><span>${identityAvatar("agent", name, { ...r, ...r.presentation, role: r.role || { id: r.role_id } })}<span><strong>${esc(name)}</strong><small>${esc(r.role_id || r.role?.id || "Agent")}</small></span></span></label>`;
  };
  const businessTile = (g) => {
    const on = g.rows.length && g.rows.every((r) => selected.has(r.agent_id));
    return `<button type="button" class="broadcast-business-tile ${on ? "selected" : ""}" data-business-group="${esc(g.rows[0].business_id)}" aria-pressed="${on}"><span class="broadcast-tile-check">${on ? "✓" : ""}</span><span><strong>${esc(g.label)}</strong><small>${g.rows.length} ${g.rows.length === 1 ? "agent" : "agents"}</small></span></button>`;
  };
  const timingFields = () =>
    draft.timing === "now"
      ? ""
      : draft.timing === "once"
        ? `<label>Delivery date and time<input id="broadcast-at" class="inp" type="datetime-local" value="${esc(draft.at)}" required></label>`
        : draft.timing === "interval"
          ? `<div class="broadcast-inline"><label>Every<input id="broadcast-every" class="inp" type="number" min="1" value="${esc(draft.every)}"></label><label>Unit<select id="broadcast-unit" class="inp"><option value="minutes" ${draft.unit === "minutes" ? "selected" : ""}>Minutes</option><option value="hours" ${draft.unit === "hours" ? "selected" : ""}>Hours</option><option value="days" ${draft.unit === "days" ? "selected" : ""}>Days</option></select></label><label>Optional start<input id="broadcast-at" class="inp" type="datetime-local" value="${esc(draft.at)}"></label><small class="muted">Without a start, the first delivery is one interval from now.</small></div>`
          : `<div class="broadcast-inline"><label>Cron expression<input id="broadcast-cron" class="inp" value="${esc(draft.cron)}" placeholder="0 9 * * 1-5"></label><label>Timezone<input id="broadcast-timezone" class="inp" value="${esc(draft.timezone)}" placeholder="UTC"></label></div>`;
  const recipientCount=()=>[...selected].filter(id=>{const r=recipients.find(r=>r.agent_id===id);return r&&(draft.filter==='all'||r.business_id==null||r.role_id===draft.filter);}).length;
  const paintCompose = () => {
    const globalRows = recipients.filter((r) => r.business_id == null),
      businessGroups = groups();
    body.innerHTML = `<form id="broadcast-form" novalidate><section class="broadcast-delivery"><span>Delivery</span><div role="group" aria-label="Delivery"><button type="button" class="btn ${draft.delivery === "inbox" ? "on" : ""}" data-delivery="inbox" aria-pressed="${draft.delivery === "inbox"}">Inbox</button><button type="button" class="btn ${draft.delivery === "input" ? "on" : ""}" data-delivery="input" aria-pressed="${draft.delivery === "input"}">Terminal input</button></div></section>${context.scope === "global" ? `<section class="broadcast-recipient-section"><h2>${globalIcon("globe")} Global</h2><div class="broadcast-recipient-grid">${globalRows.map(agentTile).join("") || '<p class="muted">No Global agents are available.</p>'}</div></section><section class="broadcast-recipient-section"><div class="broadcast-section-heading"><h2>Businesses</h2><label>Show <select id="broadcast-filter" class="inp"><option value="all">All agents</option><option value="business-agent">Onbashy</option><option value="dev">Dev</option></select></label><button class="btn quiet" type="button" id="broadcast-select-all">Select all</button><button class="btn quiet" type="button" id="broadcast-clear-all">Clear all</button></div><div class="broadcast-business-grid">${businessGroups.map(businessTile).join("") || '<p class="muted">No matching businesses are available.</p>'}</div></section>` : `<section class="broadcast-recipient-section"><div class="broadcast-section-heading"><h2>Recipients</h2><button class="btn quiet" type="button" id="broadcast-select-all">Select all</button><button class="btn quiet" type="button" id="broadcast-clear-all">Clear all</button></div><div class="broadcast-recipient-grid">${recipients.map(agentTile).join("") || '<p class="muted">No agents are available.</p>'}</div></section>`}<label class="broadcast-message-label">Message<textarea id="broadcast-message" class="inp" maxlength="16000" required>${esc(draft.message)}</textarea></label><fieldset><legend>When</legend><label><input type="radio" name="broadcast-timing" value="now" ${draft.timing === "now" ? "checked" : ""}> Send now</label><label><input type="radio" name="broadcast-timing" value="once" ${draft.timing === "once" ? "checked" : ""}> Once</label><label><input type="radio" name="broadcast-timing" value="interval" ${draft.timing === "interval" ? "checked" : ""}> Repeat</label><label><input type="radio" name="broadcast-timing" value="cron" ${draft.timing === "cron" ? "checked" : ""}> Cron</label></fieldset><div class="broadcast-timing-fields">${timingFields()}</div>${["interval", "cron"].includes(draft.timing) ? `<label class="broadcast-max-label">Maximum occurrences (optional)<input id="broadcast-max" class="inp" type="number" min="1" value="${esc(draft.max)}"></label>` : ""}<p id="broadcast-error" class="broadcast-error" role="alert"></p><div class="broadcast-actions"><span class="muted" data-recipient-count>${recipientCount()} recipients</span><button class="primary" type="submit">${editing ? "Save changes" : draft.timing === "now" ? "Send now" : "Schedule"}</button>${editing ? '<button class="btn" type="button" id="broadcast-cancel-edit">Cancel edit</button>' : ""}</div></form>`;
    const filter = $("#broadcast-filter", body);
    if (filter) {
      filter.value = draft.filter;
      filter.onchange = () => {
        capture();
        draft.filter = filter.value;
        save();
        paintCompose();
      };
    }
    body.querySelectorAll("[data-delivery]").forEach(
      (b) =>
        (b.onclick = () => {
          capture();
          draft.delivery = b.dataset.delivery;
          save();
          paintCompose();
        }),
    );
    body.querySelectorAll("[name=broadcast-timing]").forEach(
      (i) =>
        (i.onchange = () => {
          capture();
          draft.timing = i.value;
          save();
          paintCompose();
        }),
    );
    body.querySelectorAll("[data-recipient]").forEach(
      (i) =>
        (i.onchange = () => {
          i.checked
            ? selected.add(i.dataset.recipient)
            : selected.delete(i.dataset.recipient);
          save();
          body.querySelector("[data-recipient-count]").textContent=recipientCount()+" recipients";
          i.closest(".broadcast-recipient").classList.toggle(
            "selected",
            i.checked,
          );
        }),
    );
    body.querySelectorAll("[data-business-group]").forEach(
      (b) =>
        (b.onclick = () => {
          capture();
          const g = groups().find(
              (x) => String(x.rows[0].business_id) === b.dataset.businessGroup,
            ),
            all = g.rows.every((r) => selected.has(r.agent_id));
          g.rows.forEach((r) =>
            all ? selected.delete(r.agent_id) : selected.add(r.agent_id),
          );
          save();
          paintCompose();
        }),
    );
    const all = $("#broadcast-select-all", body);
    if (all)
      all.onclick = () => {
        capture();
        (context.scope === "global" ? businessRows() : recipients).forEach(
          (r) => selected.add(r.agent_id),
        );
        save();
        paintCompose();
      };
    const clear = $("#broadcast-clear-all", body);
    if (clear)
      clear.onclick = () => {
        capture();
        (context.scope === "global" ? businessRows() : recipients).forEach(
          (r) => selected.delete(r.agent_id),
        );
        save();
        paintCompose();
      };
    $("#broadcast-form", body).onsubmit = async (event) => {
      event.preventDefault();
      capture();
      const error = $("#broadcast-error", body);
      const outbound =
        draft.filter !== "all"
          ? new Set(
              [...selected].filter((id) => {
                const row = recipients.find((r) => r.agent_id === id);
                return (
                  !row || row.business_id == null || row.role_id === draft.filter
                );
              }),
            )
          : selected;
      let definition;
      try {
        definition = broadcastDefinition(
          draft,
          outbound,
          !editing && draft.timing === "now",
        );
      } catch (failure) {
        error.textContent = failure.message;
        return;
      }
      if (submitting) return;
      submitting = true;
      const submit = $("button[type=submit]", body),
        signature = JSON.stringify({
          definition,
          editing: editing && editing.id,
        });
      if (signature !== pendingSignature) {
        pendingId = newRequestId();
        pendingSignature = signature;
      }
      submit.disabled = true;
      try {
        if (editing) {
          coreData(
            await post(`${path}/operations/broadcasts_update`, {
              scope: context.scope,
              broadcast_id: editing.id,
              definition,
              expected_revision: editing.revision,
              idempotency_key: pendingId,
            }),
          );
          notify({ text: "Broadcast updated" });
          editing = null;
        } else {
          coreData(
            await post(`${path}/operations/broadcasts_create`, {
              scope: context.scope,
              definition,
              ...(draft.timing === "now" ? { send_now: true } : {}),
              idempotency_key: pendingId,
            }),
          );
          notify({
            text:
              draft.timing === "now"
                ? "Broadcast queued"
                : "Broadcast scheduled",
          });
          draft.message = "";
        }
        pendingId = null;
        pendingSignature = null;
        save();
        if (current() && view === "compose") paintCompose();
      } catch (failure) {
        error.textContent = failure.message || "Broadcast could not be saved.";
        submit.disabled = false;
      } finally {
        submitting = false;
      }
    };
    const cancel = $("#broadcast-cancel-edit", body);
    if (cancel)
      cancel.onclick = () => {
        capture();
        editing = null;
        pendingId = null;
        pendingSignature = null;
        draft.timing = "now";
        draft.max = "";
        save();
        paintCompose();
      };
  };
  const loadDeliveries = async (card, item) => {
    const box = $(".broadcast-results", card);
    box.textContent = "Loading delivery outcomes…";
    try {
      const data = coreData(
          await post(`${path}/operations/broadcasts_get`, {
            scope: context.scope,
            broadcast_id: item.id,
          }),
        ),
        names = Object.fromEntries(
          recipients.map((r) => [r.agent_id, r.display_name || r.name]),
        );
      box.innerHTML =
        (data.deliveries || [])
          .map(
            (d) =>
              `<div><strong>${esc(names[d.recipient_id] || "Unknown recipient")}</strong><span>${esc(d.state || "queued")}${d.detail ? ` · ${esc(d.detail)}` : ""}</span></div>`,
          )
          .join("") || "No delivery attempts yet.";
    } catch (error) {
      box.textContent = error.message || "Could not load delivery outcomes.";
    }
  };
  const edit = (item) => {
    const t = broadcastData(item.timing_json || item.timing);
    draft.filter = "all";
    pendingId = null;
    pendingSignature = null;
    draft.message = item.message || "";
    draft.delivery = item.delivery || "inbox";
    draft.max =
      item.max_occurrences == null ? "" : String(item.max_occurrences);
    draft.timing = t.kind || "once";
    draft.at = broadcastLocalDate(t.at);
    draft.cron = t.schedule || "";
    draft.timezone = t.timezone || "UTC";
    if (t.kind === "interval") {
      const s = Number(t.every_seconds) || 60;
      if (s % 86400 === 0) {
        draft.every = s / 86400;
        draft.unit = "days";
      } else if (s % 3600 === 0) {
        draft.every = s / 3600;
        draft.unit = "hours";
      } else {
        draft.every = s / 60;
        draft.unit = "minutes";
      }
    }
    selected.clear();
    broadcastData(item.recipients_json || item.recipients).forEach((r) =>
      selected.add(typeof r === "string" ? r : r.agent_id),
    );
    editing = item;
    save();
    setView("compose");
  };
  const manage = async (operation, item) => {
    const button = $(
      `[data-broadcast-action="${operation}"][data-broadcast-id="${item.id}"]`,
      body,
    );
    button.disabled = true;
    try {
      coreData(
        await post(`${path}/operations/broadcasts_${operation}`, {
          scope: context.scope,
          broadcast_id: item.id,
          expected_revision: item.revision,
          idempotency_key: newRequestId(),
        }),
      );
      notify({
        text:
          operation === "send_now"
            ? "Broadcast queued"
            : operation === "cancel"
              ? "Broadcast cancelled"
              : `Broadcast ${operation}d`,
      });
      paintScheduled();
    } catch (error) {
      button.disabled = false;
      $(
        ".broadcast-action-error",
        button.closest(".broadcast-schedule"),
      ).textContent = error.message || "Action failed.";
    }
  };
  const paintScheduled = async () => {
    if (!current() || view !== "scheduled") return;
    body.innerHTML =
      '<p class="muted" role="status">Loading scheduled broadcasts…</p>';
    try {
      const items =
        coreData(
          await post(`${path}/operations/broadcasts_list`, {
            scope: context.scope,
            limit: 200,
          }),
        ).items || [];
      if (!current() || view !== "scheduled") return;
      body.innerHTML = `<div class="broadcast-schedule-list">${items.map((item) => `<article class="broadcast-schedule"><div><strong>${esc(item.name || item.message)}</strong><span>${esc(broadcastStatus(item))} · ${esc(broadcastTimingLabel(item))}</span></div><div class="broadcast-schedule-actions">${item.status === "cancelled" ? "" : `<button class="btn" data-broadcast-edit="${esc(item.id)}">Edit</button><button class="btn" data-broadcast-action="send_now" data-broadcast-id="${esc(item.id)}">Send now</button>${item.status === "active" ? `<button class="btn" data-broadcast-action="pause" data-broadcast-id="${esc(item.id)}">Pause</button>` : item.status === "paused" && item.next_due_at ? `<button class="btn" data-broadcast-action="resume" data-broadcast-id="${esc(item.id)}">Resume</button>` : ""}<button class="btn" data-broadcast-action="cancel" data-broadcast-id="${esc(item.id)}">Cancel</button>`}</div><p class="broadcast-action-error" role="alert"></p><details><summary>Delivery outcomes</summary><div class="broadcast-results">Open to load named recipient outcomes.</div></details></article>`).join("") || '<p class="muted">No saved broadcasts.</p>'}</div>`;
      body
        .querySelectorAll("[data-broadcast-edit]")
        .forEach(
          (b) =>
            (b.onclick = () =>
              edit(items.find((i) => i.id === b.dataset.broadcastEdit))),
        );
      body.querySelectorAll("[data-broadcast-action]").forEach(
        (b) =>
          (b.onclick = () =>
            manage(
              b.dataset.broadcastAction,
              items.find((i) => i.id === b.dataset.broadcastId),
            )),
      );
      body.querySelectorAll("details").forEach(
        (d, i) =>
          (d.ontoggle = () => {
            if (d.open && !d.dataset.loaded) {
              d.dataset.loaded = "true";
              loadDeliveries(d.closest(".broadcast-schedule"), items[i]);
            }
          }),
      );
    } catch (error) {
      if (current() && view === "scheduled")
        body.innerHTML = `<p role="alert">${esc(error.message || "Could not load broadcasts.")}</p>`;
    }
  };
  const setView = (nextView) => {
    view = nextView;
    c.querySelectorAll("[data-broadcast-view]").forEach((b) => {
      const on = b.dataset.broadcastView === view;
      b.classList.toggle("on", on);
      b.setAttribute("aria-selected", String(on));
    });
    view === "scheduled" ? paintScheduled() : paintCompose();
  };
  c.querySelectorAll("[data-broadcast-view]").forEach(
    (b) =>
      (b.onclick = () => {
        capture();
        setView(b.dataset.broadcastView);
      }),
  );
  paintCompose();
}
