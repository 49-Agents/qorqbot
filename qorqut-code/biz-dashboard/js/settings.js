// Environment defaults belong to Global; a business saves only its explicit overrides.
let envView = null;
const ENV_KEY_JS = /^[A-Za-z_][A-Za-z0-9_]*$/;
const envScope = () => state.scope === "global" ? "global" : state.biz;
const envCurrent = p => envView === p && p.pane.isConnected && envScope() === p.scope;
const envBusiness = slug => (state.bizs || []).find(item => (item.slug || item.id) === slug);
function envHttpUrl(value) {
  if (typeof value !== "string" || value.length > 2048 || /[\u0000-\u001f\u007f]/.test(value)) return null;
  try {
    const url = new URL(value);
    return (url.protocol === "http:" || url.protocol === "https:") && !url.username && !url.password ? url.href : null;
  } catch (_) { return null; }
}
function envStateLabel(value) {
  return { pending: "Preparing", ready: "Ready", stopped: "Stopped", failed: "Unavailable" }[value] || "Status unavailable";
}
function environmentStatusHtml(p) {
  if (p.global) return "";
  const environment = p.environment;
  if (!environment || typeof environment !== "object") return "";
  const sandbox = environment.mode === "sandbox";
  const previews = Array.isArray(environment.previews) ? environment.previews.map(preview => {
    if (!preview || !Number.isInteger(preview.port) || preview.port < 1 || preview.port > 65535) return "";
    const url = envHttpUrl(preview.url);
    return url ? `<li><span>Port ${preview.port}</span><a href="${esc(url)}" target="_blank" rel="noopener noreferrer">Open private preview</a></li>` : "";
  }).filter(Boolean).join("") : "";
  return `<section class="env-runtime" aria-label="Project environment status">
    <div class="env-runtime-head"><h2>Project environment</h2><span class="env-runtime-state">${sandbox ? esc(envStateLabel(environment.state)) : "Host"}</span></div>
    <p class="muted small">${sandbox ? "The private project workspace is managed separately from the dashboard." : "This project uses the existing host workspace."}</p>
    ${previews ? `<div class="env-runtime-links"><h3>Private previews</h3><ul>${previews}</ul></div>` : ""}
  </section>`;
}
function paintEnvironmentStatus(p) {
  const host = p.pane.querySelector("#envruntime");
  if (host) host.innerHTML = environmentStatusHtml(p);
}

async function renderSettings(c) {
  const scope = envScope(), global = state.scope === "global";
  const pane = document.createElement("div");
  pane.className = "pane settingspane";
  const p = envView = { pane, scope, global, url: global ? "/root/env" : "/biz/" + encodeURIComponent(state.biz) + "/env",
    rows: [], inherited: [], loaded: false, saving: false, dirty: false,
    environment: envBusiness(scope)?.environment || null };
  pane.innerHTML = `
    <div class="set-head"><div>
      <h1 class="set-title">${global ? "Global environment" : "Environment"}</h1>
      <div class="muted small">${global ? "Shared defaults for all projects and Global agents. Projects can override individual values." : "Global values are inherited automatically. Override a value for this project, or use Global again."}</div>
    </div><button class="btn" id="envsave" disabled>Save</button></div>
    <div id="envruntime">${environmentStatusHtml(p)}</div>
    ${global ? '<p class="muted small">Agent Browser views come from the installation’s browser viewer bindings, not from these settings.</p>' : ''}
    <div class="envrows" id="envrows"></div>
    <button class="envadd" id="envadd" disabled>+ Add variable</button>
    <div class="set-note muted small" id="envnote" role="status"></div>
    <p class="muted small">Changes apply to new agent sessions and subsequent jobs. Already-running agent sessions keep their current environment.</p>`;
  c.appendChild(pane);
  pane.querySelector("#envsave").onclick = () => saveEnv(p);
  pane.querySelector("#envadd").onclick = () => {
    p.rows.push({ key: "", value: "" }); envDirty(p); paintEnv(p);
    pane.querySelector(".envrow:last-child .ek")?.focus();
  };
  await loadEnv(p);
}

function envError(p, message, retry = false) {
  const note = p.pane.querySelector("#envnote");
  note.textContent = message;
  if (retry) {
    const button = document.createElement("button");
    button.className = "btn quiet"; button.id = "envretry"; button.textContent = "Retry";
    button.onclick = () => loadEnv(p); note.append(" ", button);
  }
}
async function loadEnv(p) {
  envError(p, "Loading environment…");
  const statusRequest = !p.global ? api("/biz").catch(() => null) : null;
  try {
    const r = await api(p.url);
    if (!envCurrent(p)) return;
    if (r.error || !Array.isArray(r.pairs)) throw new Error(r.error || "Environment response is unavailable.");
    if (!p.global && r.environment && typeof r.environment === "object") {
      p.environment = r.environment;
      paintEnvironmentStatus(p);
    }
    p.rows = r.pairs.map(row => ({ ...row }));
    p.inherited = (r.inherited || []).map(row => ({ ...row }));
    p.loaded = true; p.dirty = false;
    envError(p, ""); paintEnv(p);
  } catch (error) {
    if (envCurrent(p)) envError(p, error.message || "Could not load environment.", true);
  }
  if (statusRequest) statusRequest.then(businesses => {
    if (!envCurrent(p) || !Array.isArray(businesses)) return;
    const current = businesses.find(item => (item.slug || item.id) === p.scope);
    if (current && current.environment && typeof current.environment === "object") {
      p.environment = current.environment;
      paintEnvironmentStatus(p);
    }
  });
}
function envDirty(p) {
  p.dirty = true;
  p.pane.querySelector("#envsave").disabled = p.saving || !p.loaded;
  envError(p, "");
}
function paintEnv(p) {
  const inherited = new Map(p.inherited.map(row => [row.key, row.value]));
  const local = new Set(p.rows.map(row => row.key.trim()));
  const shown = [
    ...p.inherited.filter(row => !local.has(row.key)).map(row => ({ ...row, inherited: true })),
    ...p.rows.map((row, index) => ({ ...row, index })),
  ];
  p.pane.querySelector("#envrows").innerHTML = shown.map(row => {
    const override = !p.global && inherited.has(row.key.trim()) && !row.inherited;
    const source = row.inherited ? "Global" : override ? "Project override" : p.global ? "Global" : "Project";
    return `<div class="envrow" data-index="${row.index ?? ""}">
      <div class="envkey"><input class="ek mono" aria-label="Variable name" placeholder="KEY" value="${esc(row.key)}" ${row.inherited ? "disabled" : override ? "readonly" : ""} spellcheck="false"><span class="envsource">${source}</span></div>
      <input class="ev mono" aria-label="Value for ${esc(row.key || "new variable")}" placeholder="value" value="${esc(row.value)}" ${row.inherited ? "disabled" : ""} spellcheck="false">
      <button class="envdel" type="button" data-key="${esc(row.key)}" data-action="${row.inherited ? "override" : "remove"}" aria-label="${row.inherited ? "Override " : override ? "Use Global for " : "Remove "}${esc(row.key || "variable")}">${row.inherited ? "Override" : override ? "Use Global" : "×"}</button>
    </div>`;
  }).join("") || '<p class="muted small">No environment variables yet.</p>';
  p.pane.querySelectorAll(".envrow").forEach(row => {
    const button = row.querySelector("button");
    if (button.dataset.action === "override") {
      button.onclick = () => {
        p.rows.push({ key: button.dataset.key, value: inherited.get(button.dataset.key) });
        envDirty(p); paintEnv(p);
        p.pane.querySelector(".envrow:last-child .ev")?.focus();
      };
      return;
    }
    const index = Number(row.dataset.index), key = row.querySelector(".ek");
    key.oninput = () => { p.rows[index].key = key.value; key.classList.toggle("bad", !!key.value.trim() && !ENV_KEY_JS.test(key.value.trim())); envDirty(p); };
    row.querySelector(".ev").oninput = e => { p.rows[index].value = e.target.value; envDirty(p); };
    button.onclick = () => { p.rows.splice(index, 1); envDirty(p); paintEnv(p); };
  });
  p.pane.querySelector("#envsave").disabled = !p.loaded || p.saving || !p.dirty;
  p.pane.querySelector("#envadd").disabled = !p.loaded || p.saving;
  if (p.saving) p.pane.querySelectorAll("#envrows input, #envrows button").forEach(el => el.disabled = true);
}

async function saveEnv(p = envView) {
  if (!p || !envCurrent(p) || !p.loaded || p.saving) return;
  const pairs = p.rows.map(row => ({ key: row.key.trim(), value: row.value })).filter(row => row.key);
  if (pairs.some(row => !ENV_KEY_JS.test(row.key))) return envError(p, "Use letters, digits and underscores for keys; do not start with a digit.");
  if (new Set(pairs.map(row => row.key)).size !== pairs.length) return envError(p, "Each variable name must be unique.");
  p.saving = true; paintEnv(p);
  try {
    const r = await post(p.url, { pairs });
    if (!envCurrent(p)) return;
    if (r.error || !Array.isArray(r.pairs)) throw new Error(r.error || "Could not save environment.");
    p.rows = r.pairs.map(row => ({ ...row })); p.inherited = (r.inherited || []).map(row => ({ ...row }));
    p.dirty = false; envError(p, ""); toast("Environment saved");
  } catch (error) {
    if (envCurrent(p)) envError(p, error.message || "Could not save environment.");
  } finally {
    p.saving = false;
    if (envCurrent(p)) paintEnv(p);
  }
}
